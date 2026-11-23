#!/usr/bin/env bash
# 每日简报采集：新容器一条命令即可跑（首次自动用 uv 按 uv.lock 装依赖）。
# 用法：bash /mnt/project-files/简报/horizon/collect.sh [--hours 24] [--out PATH] [--offline-dir DIR]
set -euo pipefail
cd "$(dirname "$0")"

# 虚拟环境放容器本地，不写进项目共享文件夹（体积大、文件多）
export UV_PROJECT_ENVIRONMENT="${HORIZON_VENV:-$HOME/.cache/horizon-venv}"
export UV_LINK_MODE=copy

if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || python3 -m pip install --user --quiet uv
fi

uv sync --frozen --no-dev --quiet
exec uv run --frozen --no-sync --quiet python collect.py "$@"
