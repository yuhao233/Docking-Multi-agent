#!/bin/bash
# 本地运行入口：CLI 模式（http / flow / agent / runs / receptors）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

if [ -f "${SCRIPT_DIR}/load_env.sh" ]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/load_env.sh"
fi

PYTHON="$WORK_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "未找到 $PYTHON，请先执行: bash scripts/setup.sh 或 bash start.sh" >&2
  exit 1
fi

export PYTHONPATH="$WORK_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m docking_agent "$@"
