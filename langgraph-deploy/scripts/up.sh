#!/usr/bin/env bash
# 起 LangGraph Platform 全栈（api + postgres + redis，**尚未实测**）。
#   bash scripts/prepare_build.sh && bash scripts/up.sh
# 需要 docker + docker compose；首次会拉取基础镜像（数 GB）。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LG="$HERE/.venv/bin/langgraph"
[[ -x "$LG" ]] || { echo "缺少 langgraph CLI，请先 bash scripts/install.sh" >&2; exit 2; }
[[ -f "$HERE/langgraph.build.json" ]] || { echo "先运行 bash scripts/prepare_build.sh" >&2; exit 2; }
cd "$HERE"
exec "$LG" up -c langgraph.build.json --port "${PORT:-8123}"
