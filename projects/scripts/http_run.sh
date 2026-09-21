#!/bin/bash
# 启动本地 HTTP 服务（等价于 bash start.sh，但不在前台等待）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

if [ -f "${SCRIPT_DIR}/load_env.sh" ]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/load_env.sh"
fi

PORT="${PORT:-${DEPLOY_RUN_PORT:-5000}}"
while getopts "p:h" opt; do
  case "$opt" in
    p) PORT="$OPTARG" ;;
    h) echo "用法: $0 -p <端口>"; exit 0 ;;
    \?) echo "无效选项: -$OPTARG"; exit 1 ;;
  esac
done

PYTHON="$WORK_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "未找到 $PYTHON，请先执行: bash scripts/setup.sh 或 bash start.sh" >&2
  exit 1
fi

export PYTHONPATH="$WORK_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
echo "[http_run] http://127.0.0.1:${PORT}/"
exec "$PYTHON" -m docking_agent -m http -p "$PORT"
