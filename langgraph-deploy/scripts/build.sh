#!/usr/bin/env bash
# 构建 LangGraph Platform 镜像（**尚未实测**，首次使用请先跑 prepare_build.sh）。
#   bash scripts/prepare_build.sh && bash scripts/build.sh [镜像标签]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LG="$HERE/.venv/bin/langgraph"
TAG="${1:-docking-agent-langgraph:0.1.0}"
[[ -x "$LG" ]] || { echo "缺少 langgraph CLI，请先 bash scripts/install.sh" >&2; exit 2; }
[[ -f "$HERE/langgraph.build.json" ]] || { echo "先运行 bash scripts/prepare_build.sh" >&2; exit 2; }
cd "$HERE"
exec "$LG" build -c langgraph.build.json -t "$TAG"
