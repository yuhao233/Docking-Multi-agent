#!/bin/bash
# 本地环境初始化：创建 .venv 并安装依赖（start.sh 会自动调用）。
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

# uv 缓存目录：受控/容器环境下 ~/.cache 可能不可写，退化为工作区内缓存
if [ -z "${UV_CACHE_DIR:-}" ] && [ ! -w "${HOME:-/root}/.cache" ]; then
  export UV_CACHE_DIR="$WORK_DIR/.uv-cache"
  echo "[setup] ~/.cache 不可写，UV_CACHE_DIR=$UV_CACHE_DIR"
fi

INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

# 依赖的**单一事实来源是 pyproject.toml**：这里用 `-e .` 安装本包（顺带装齐全部依赖），
# 这样 `import docking_agent` 与 `[project.scripts] docking-agent` 在开发环境里真的可用，
# 不必再依赖 PYTHONPATH=src。`requirements-local.txt` 只是同一份依赖的镜像（供无 pyproject
# 的 pip 场景），由 tests/test_packaging.py 守住两者一致。
if command -v uv >/dev/null 2>&1; then
  echo "[setup] 使用 uv 以可编辑方式安装本包（index: $INDEX_URL）"
  [ -d .venv ] || uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python --index-url "$INDEX_URL" -e .
else
  echo "[setup] 未找到 uv，回退 python3 -m venv + pip"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install --index-url "$INDEX_URL" -e .
fi

touch .venv/.uv_ready
echo "[setup] 完成。"
echo "[setup] 下一步：bash start.sh --check   # 自检（无需 LLM）"
echo "[setup] 或：    bash start.sh           # 启动服务并打开网页"
