#!/usr/bin/env bash
# 一键跑部署层的**离线测试**（不联网 / 不调 LLM / 不做真实对接）。
#
#   bash scripts/test.sh              # 全部用例
#   bash scripts/test.sh -k runtime   # 透传 pytest 参数
#   bash scripts/test.sh -m blockbuster
#
# 说明：
#   * 用部署 venv（deploy/.venv）里的 python -m pytest；pytest 配置见 ../pyproject.toml
#     （testpaths / asyncio_mode / markers）。
#   * 测试自身会把运行产物重定向到临时目录（DOCKING_WORKSPACE + 清 RunStore 单例），
#     不会污染 projects/var/runs/。
#   * 不启动 langgraph dev（那是另一路集成测试）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[[ -x "$PY" ]] || { echo "缺少 .venv，请先 bash scripts/install.sh" >&2; exit 2; }

if ! "$PY" -c "import pytest, pytest_asyncio" >/dev/null 2>&1; then
  echo "缺少测试依赖（pytest / pytest-asyncio）。安装：" >&2
  echo "  UV_CACHE_DIR=\"$HERE/../.uv-cache\" uv pip install --python \"$PY\" -e \"$HERE[test]\"" >&2
  echo "  或：uv pip install --python \"$PY\" pytest pytest-asyncio" >&2
  exit 2
fi

cd "$HERE"

# --live：额外跑真实服务集成测试（会自己拉起一个隔离的 langgraph dev：端口 2033 +
# 临时工作区，真实 Vina 对接 + 真实 LLM；用 LG_BASE_URL=http://127.0.0.1:2024 可复用已有服务）
if [[ "${1:-}" == "--live" ]]; then
  shift
  echo "== 离线用例 =="
  "$PY" -m pytest tests -q -m "not live" "$@"
  echo
  echo "== 真实服务集成用例 =="
  LG_LIVE=1 exec "$PY" -m pytest tests -q -m live "$@"
fi

exec "$PY" -m pytest tests -q "$@"
