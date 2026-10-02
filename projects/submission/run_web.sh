#!/bin/bash
# 启动网页端（本地 HTTP 服务）：简易模式 http://127.0.0.1:<端口>/ ，高级模式 /advanced
#
#   bash run_web.sh                 # 默认端口 5000
#   bash run_web.sh --port 8000     # 指定端口
#
# 说明：网页端的「自然语言对话」需要配置大模型（见 WEB.md 第 3 节）；
# 不配置也能使用表单参数、运行记录、结果与报告等全部功能。
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=5000
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) echo "未知参数：$1（支持 --port <端口>）" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
if [ ! -d ".venv" ] && command -v python3 >/dev/null 2>&1; then
  echo "提示：未检测到 .venv，将使用系统 python3（依赖见 requirements.txt）"
fi
[ -x ".venv/bin/python" ] && PYTHON=".venv/bin/python"

export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
echo "网页端启动中：http://127.0.0.1:${PORT}/  （高级模式 /advanced；按 Ctrl+C 停止）"
exec "$PYTHON" -m docking_agent -m http --host 127.0.0.1 --port "$PORT"
