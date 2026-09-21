#!/bin/bash
# 环境能力自检（开箱即用第一步）：一次列清「有什么、缺什么、缺了会降级成什么」。
#
#   bash scripts/doctor.sh            # 人读能力矩阵
#   bash scripts/doctor.sh --json     # 机器可读（CI / 测试用）
#   bash scripts/doctor.sh --online   # 额外探测 LLM 端点可达性
#
# 退出码：0=全能力 / 2=仅降级（缺可选组件，核心流程仍可跑）/ 1=缺必需组件
#
# 探测逻辑在 scripts/doctor_probe.py（复用项目自己的 P2Rank / pdb2pqr / AutoDock 探测函数，
# 不另写一套判断）。这里只负责：选对解释器 → 找不到虚拟环境时给出可执行的下一步。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "$WORK_DIR/.venv/bin/python" ]; then
  PY="$WORK_DIR/.venv/bin/python"
else
  echo "✗ 未找到虚拟环境（.venv/）。先装依赖再自检：" >&2
  echo "    bash start.sh --setup     # 创建 .venv 并安装依赖（首次几分钟）" >&2
  exit 1
fi

# matplotlib 缓存目录：~/.config 不可写时退到工作区内（否则每次 import 都会打印警告，
# 也会拖慢多个进程的首次导入）
if [ -z "${MPLCONFIGDIR:-}" ] || [ ! -w "${MPLCONFIGDIR%/*}" ]; then
  export MPLCONFIGDIR="$WORK_DIR/var/cache/matplotlib"
  mkdir -p "$MPLCONFIGDIR" 2>/dev/null || true
fi

# 允许 PYTHONPATH=src 兜底（未 `pip install -e .` 的开发场景）
if ! "$PY" -c "import docking_agent" >/dev/null 2>&1 && [ -d "src" ]; then
  export PYTHONPATH="$WORK_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
fi

exec "$PY" "$SCRIPT_DIR/doctor_probe.py" "$@"
