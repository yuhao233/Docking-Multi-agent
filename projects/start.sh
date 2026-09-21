#!/bin/bash
# ============================================================================
# 一键启动：分子对接多 Agent 协作系统（本地版）
#
#   bash start.sh                 # 自动装依赖(首次) → 启动服务 → 打开浏览器
#   bash start.sh --port 8000     # 指定端口
#   bash start.sh --no-browser    # 不自动打开浏览器
#   bash start.sh --check         # 只做环境自检，不启动服务
#   bash start.sh --setup         # 强制重装依赖
#
# 启动后网页在 http://127.0.0.1:<端口>/ ，API 文档见 docs/api.md
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT_ARG=""
OPEN_BROWSER=1
DO_CHECK=0
FORCE_SETUP=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--port) PORT_ARG="$2"; shift 2 ;;
    --no-browser) OPEN_BROWSER=0; shift ;;
    --check) DO_CHECK=1; shift ;;
    --setup) FORCE_SETUP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

VENV_PY="$SCRIPT_DIR/.venv/bin/python"

port_free() {
  "$VENV_PY" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
# 与 uvicorn 一致开启 SO_REUSEADDR：TIME_WAIT 的端口仍可重新绑定，不应判定为占用
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

pick_port() {
  local start="$1" p
  for p in $(seq "$start" $((start + 20))); do
    if port_free "$p"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

py_ok() {
  [ -x "$VENV_PY" ] || return 1
  "$VENV_PY" - <<'PY' >/dev/null 2>&1
import importlib
for m in ("fastapi", "uvicorn", "rdkit", "vina", "meeko", "langchain", "langgraph"):
    importlib.import_module(m)
PY
}

echo "=============================================================="
echo " 分子对接多 Agent 协作系统 · 本地版"
echo "=============================================================="

if [ "$FORCE_SETUP" = "1" ] || ! py_ok; then
  echo "[1/3] 初始化运行环境（首次运行需要下载依赖，约几分钟）..."
  bash "$SCRIPT_DIR/scripts/setup.sh"
else
  echo "[1/3] 运行环境已就绪（.venv）"
fi

# 加载 .env（LLM 配置等）
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export DOCKING_WORKSPACE="$SCRIPT_DIR"
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
  echo "[2/3] 已加载 .env"
else
  echo "[2/3] 未找到 .env（可复制 .env.example）；多 Agent 模式需要 LLM_API_KEY"
fi

LLM_STATE="未配置（多 Agent 模式不可用，确定性流水线可用）"
if [ -n "${LLM_API_KEY:-}" ]; then
  LLM_STATE="已配置（模型 ${LLM_MODEL:-未指定}）"
fi
echo "      LLM：$LLM_STATE"

# 端口优先级：命令行 --port > 环境变量/.env 的 PORT > 5000
PORT="${PORT_ARG:-${PORT:-5000}}"
if ! port_free "$PORT"; then
  if [ -n "$PORT_ARG" ]; then
    echo "错误：端口 $PORT 已被占用，请换一个：bash start.sh --port <端口>" >&2
    exit 1
  fi
  NEW_PORT="$(pick_port $((PORT + 1)) || true)"
  if [ -z "$NEW_PORT" ]; then
    echo "错误：端口 $PORT 及其后 20 个端口均被占用" >&2
    exit 1
  fi
  echo "      端口 $PORT 已被占用，自动改用 $NEW_PORT"
  PORT="$NEW_PORT"
fi
export PORT

if [ "$DO_CHECK" = "1" ]; then
  echo "[3/3] 环境能力自检（doctor）..."
  # 先报"有什么/缺什么/缺了降级成什么"（含 P2Rank/pdb2pqr/AutoDock/字体/LLM），再跑一次真实对接。
  # doctor 退出码 1 = 缺必需组件（此时功能自检必然失败，直接停）；2 = 仅降级，继续跑。
  bash "$SCRIPT_DIR/scripts/doctor.sh" || {
    code=$?
    if [ "$code" = "1" ]; then
      echo "✗ 缺少必需组件，已停止。按上方提示补齐后重试：bash start.sh --setup" >&2
      exit 1
    fi
    echo "（上方 ○ 项为可选组件，相关能力会降级；继续做功能自检）"
  }
  echo "[3/3] 功能自检（多 Agent 编排，使用内置假 LLM，不消耗真实额度）..."
  "$VENV_PY" -m docking_agent -m receptors
  if ! "$VENV_PY" scripts/smoke_test.py --fast; then
    echo "✗ 功能自检未通过；常见原因见上方输出（对接引擎、依赖或端口占用）。" >&2
    exit 1
  fi
  echo "自检完成。"
  exit 0
fi

echo "[3/3] 启动服务： http://127.0.0.1:${PORT}/"
echo "      停止服务： Ctrl+C"
echo "=============================================================="

# 后台启动服务，等待健康检查通过后再打开浏览器
"$VENV_PY" -m docking_agent -m http -p "$PORT" &
SERVER_PID=$!
trap 'echo; echo "正在停止服务..."; kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; echo "已停止。"; exit 0' INT TERM

READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务启动失败，请查看上方日志。" >&2
    wait "$SERVER_PID" 2>/dev/null || true
    exit 1
  fi
  if "$VENV_PY" - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
    assert json.load(r)["status"] == "ok"
PY
  then
    READY=1
    break
  fi
  sleep 0.5
done

if [ "$READY" = "1" ]; then
  echo "服务已就绪： http://127.0.0.1:${PORT}/"
  if [ "$OPEN_BROWSER" = "1" ]; then
    (command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1) \
      || echo "（无法自动打开浏览器，请手动访问上面的地址）"
  fi
else
  echo "服务未在预期时间内就绪，请查看上方日志。" >&2
fi

wait "$SERVER_PID"
