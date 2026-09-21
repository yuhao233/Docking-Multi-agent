#!/usr/bin/env bash
# 启动 LangGraph 开发服务器（内存运行时）并连 LangGraph Studio。
#
#   bash scripts/dev.sh                 # 默认 127.0.0.1:2024
#   PORT=8123 bash scripts/dev.sh       # 换端口
#   HOST=0.0.0.0 bash scripts/dev.sh    # 换监听地址（注意本机无鉴权）
#
# 启动后：
#   - Agent Server API   http://127.0.0.1:2024   （/ok /assistants /threads ...）
#   - LangGraph Studio   https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
# 说明：Studio 是浏览器里的托管前端，通过 baseUrl 直连你本机的 2024 端口；
# 数据不出本机（除 Studio 前端自身）。不想用 Studio 也可以只用 REST API。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HERE/.venv/bin/python"
LG="$HERE/.venv/bin/langgraph"
[[ -x "$LG" ]] || { echo "缺少 langgraph CLI，请先 bash scripts/install.sh" >&2; exit 2; }

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-2024}"
# 默认关掉热重载：本目录里的日志/临时文件会被 watchfiles 当"代码变更"，
# 造成「写日志 → 触发重载 → 再写日志」的死循环。要边改边看就 RELOAD=1。
RELOAD_FLAG="--no-reload"
if [[ "${RELOAD:-0}" == "1" ]]; then
  RELOAD_FLAG=""
fi
[[ -f "$HERE/.env" ]] || { echo "缺少 $HERE/.env，请先 bash scripts/install.sh" >&2; exit 2; }

# 端口预检：langgraph dev 在端口被占用时会**静默改用随机端口**（只打一行 WARNING），
# 结果是我们给的 Studio 链接指向旧/错的实例 —— 这里直接拦住，绝不让它漂移。
if "$PY" - "$HOST" "$PORT" <<'PYCHK'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket()
s.settimeout(0.6)
sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
PYCHK
then
  echo "端口 $PORT 已被占用（可能是上一次的 dev 服务没停干净）。" >&2
  echo "  查看：ss -ltnp | grep :$PORT" >&2
  echo "  换端口：PORT=8123 bash scripts/dev.sh" >&2
  echo "  或停掉旧进程后再启动。" >&2
  exit 2
fi

cd "$HERE"
echo "启动 langgraph dev（cwd=$HERE, host=$HOST, port=$PORT）"
echo "  API   ： http://$HOST:$PORT"
echo "  Studio： https://smith.langchain.com/studio/?baseUrl=http://$HOST:$PORT"
[[ "$RELOAD_FLAG" == "--no-reload" ]] && echo "  （热重载已关；需要时 RELOAD=1 bash scripts/dev.sh）"
exec "$LG" dev --host "$HOST" --port "$PORT" --no-browser $RELOAD_FLAG
