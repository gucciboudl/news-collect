"""Collection-only Horizon run: fetch all sources (no LLM), URL-dedup, add
GitHub Trending (OSS Insight ranking is dead upstream since 2026-03), and dump
compact raw items grouped by topic to out/raw-YYYY-MM-DD.json."""
import argparse, asyncio, html, json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from rich.console import Console

from src.storage.manager import StorageManager
from src.orchestrator import HorizonOrchestrator

TOPICS = {"ai-tech": "AI与科技", "github-trending": "GitHub热门开源",
          "film-tv": "影视资讯", "world-politics": "国际政治"}
CAP = 40
SNIP = 240

# Per-feed title filters (no LLM). include: keep only matching titles; exclude: drop matches.
# IT之家: keep its AI news only, drop shopping deals / price-change posts.
FEED_FILTERS = {
    "IT之家": {
        "include": re.compile(r"AI|人工智能|大模型|智能体|机器人|OpenAI|ChatGPT|Claude|Gemini|DeepSeek|"
                              r"Anthropic|英伟达|NVIDIA|算力|LLM|Agent|Copilot|豆包|通义|千问|文心|Kimi|智谱|Sora|Grok",
                              re.I),
        "exclude": re.compile(r"→|元起|降价|直降|新低|好价|补贴|领券|券后|京东|天猫|淘宝|拼多多|11\.11|双十一|"
                              r"双 11|618|红包|到手价|折合|秒杀|涨价|\d+\s*元"),
    },
}


def passes_feed_filter(feed, title):
    f = FEED_FILTERS.get(feed)
    if not f:
        return True
    if f.get("include") and not f["include"].search(title):
        return False
    if f.get("exclude") and f["exclude"].search(title):
        return False
    return True


def clean(text):
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:SNIP] + ("…" if len(text) > SNIP else "")


def engagement(m):
    return (m.get("score") or 0) + 2 * (m.get("descendants") or m.get("num_comments") or 0)


async def github_trending(client):
    r = await client.get("https://github.com/trending?since=daily",
                         headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for row in soup.select("article.Box-row"):
        a = row.select_one("h2 a")
        if not a:
            continue
        repo = re.sub(r"\s+", "", a.get_text())
        desc = row.select_one("p")
        lang = row.select_one('[itemprop="programmingLanguage"]')
        total = row.select_one('a[href$="/stargazers"]')
        today = re.search(r"([\d,]+)\s+stars? today", row.get_text())
        out.append({
            "title": repo, "url": f"https://github.com/{repo}",
            "source": "GitHub Trending (daily)",
            "time": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="minutes"),
            "stars_today": int(today.group(1).replace(",", "")) if today else None,
            "stars_total": total.get_text(strip=True) if total else None,
            "language": lang.get_text(strip=True) if lang else None,
            "snippet": clean(desc.get_text() if desc else ""),
        })
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    console = Console(stderr=True)
    storage = StorageManager(data_dir="data")
    config = storage.load_config()
    orch = HorizonOrchestrator(config, storage, console=console)
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    items = await orch.fetch_all_sources(since)
    fetched = len(items)
    items = orch.merge_cross_source_duplicates(items)
    report = orch.last_fetch_report.to_dict() if orch.last_fetch_report else {}

    groups = {k: [] for k in TOPICS}
    seen_titles = set()
    filtered_out = {}
    for it in items:
        m = it.metadata
        cat = m.get("category") or "other"
        if cat not in groups:
            continue
        feed = m.get("feed_name")
        if feed and not passes_feed_filter(feed, it.title):
            filtered_out[feed] = filtered_out.get(feed, 0) + 1
            continue
        key = re.sub(r"\W+", "", it.title.lower())[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        rec = {"title": it.title, "url": str(it.url),
               "source": m.get("feed_name") or (f"r/{m['subreddit']}" if m.get("subreddit") else None)
               or ("Hacker News" if it.source_type.value == "hackernews" else it.source_type.value),
               "time": it.published_at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="minutes")}
        if m.get("score") is not None:
            rec["score"] = m.get("score")
        c = m.get("descendants") if m.get("descendants") is not None else m.get("num_comments")
        if c is not None:
            rec["comments"] = c
        if m.get("discussion_url") and m.get("discussion_url") != rec["url"]:
            rec["discussion"] = m["discussion_url"]
        rec["snippet"] = clean(it.content)
        rec["_eng"] = engagement(m)
        groups[cat].append(rec)

    gh_error = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            groups["github-trending"] = await github_trending(client)
    except Exception as e:
        gh_error = f"{type(e).__name__}: {e}"

    counts_raw = {k: len(v) for k, v in groups.items()}
    for k, v in groups.items():
        if k == "github-trending":
            v.sort(key=lambda r: r.get("stars_today") or 0, reverse=True)
        else:
            # engagement first (HN/Reddit), then recency; interleave sources for balance
            v.sort(key=lambda r: (r["_eng"], r["time"]), reverse=True)
            by_src = {}
            for r in v:
                by_src.setdefault(r["source"], []).append(r)
            mixed = []
            while any(by_src.values()):
                for s in list(by_src):
                    if by_src[s]:
                        mixed.append(by_src[s].pop(0))
            v[:] = mixed
        del v[CAP:]
        for r in v:
            r.pop("_eng", None)

    now = datetime.now(timezone(timedelta(hours=8)))
    out = Path(args.out or f"out/raw-{now:%Y-%m-%d}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"generated_at": now.isoformat(timespec="seconds"), "window_hours": args.hours,
           "fetched_total": fetched, "counts_before_cap": counts_raw,
           "topics": {TOPICS[k]: v for k, v in groups.items()},
           "source_status": report.get("sources", []),
           "feed_filtered_out": filtered_out,
           "github_trending_error": gh_error}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(f"Wrote {out} " + json.dumps({TOPICS[k]: len(v) for k, v in groups.items()}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
