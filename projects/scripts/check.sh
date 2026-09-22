#!/bin/bash
# projects 侧**聚合门禁**：把分散的检查收拢成一个入口（此前必须手工逐条敲）。
#
#   bash scripts/check.sh            # 全量：lint + 前端契约 + 全部用例（需本机引擎，约 6 min）
#   bash scripts/check.sh --fast     # 离线/快：跳过需要本机引擎的 engine 组（等价 CI，约 20 s）
#   bash scripts/check.sh --static   # 只跑两个静态门禁（约 1 s，提交前随手跑）
#
# 环境变量：
#   COVERAGE=1   追加覆盖率报告（只报告、不设阈值）
#
# **不在本脚本内**（需要额外前置，故保持显式调用）：
#   · 真实浏览器门禁：先起服务，再 `PLAYWRIGHT_BROWSERS_PATH=... python scripts/browser_check.py <url>`
#   · DOM 级前端门禁：先起服务，再 `node scripts/ui_e2e.js <url>`
#   · 部署层自检：`cd ../langgraph-deploy && bash scripts/check.sh && bash scripts/test.sh`
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(dirname "$SCRIPT_DIR")"
cd "$WORK_DIR"

MODE="full"
case "${1:-}" in
  "")        MODE="full" ;;
  --fast)    MODE="fast" ;;
  --static)  MODE="static" ;;
  -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  *) echo "用法: $0 [--fast|--static]" >&2; exit 2 ;;
esac

PY="$WORK_DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "未找到 $PY，请先执行: bash scripts/setup.sh" >&2
  exit 2
fi

# 与 scripts/local_run.sh 保持同一套运行环境约定（缓存目录固定到工作区内）
export UV_CACHE_DIR="${UV_CACHE_DIR:-$(dirname "$WORK_DIR")/.uv-cache}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$WORK_DIR/var/cache/matplotlib}"
export PYTHONPATH="$WORK_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$MPLCONFIGDIR"

echo "=== 1/5 静态门禁：lint（手写 AST 检查 + 基线棘轮）==="
"$PY" scripts/lint_local.py

echo
echo "=== 2/5 静态门禁：前端契约与安全姿态（check_web）==="
"$PY" scripts/check_web.py

echo
echo "=== 3/5 静态门禁：ruff（pyflakes 高信号规则）==="
if "$PY" -c "import ruff" >/dev/null 2>&1; then
  "$PY" -m ruff check src/ scripts/ tests/
else
  echo "缺少 ruff（dev 依赖）：UV_CACHE_DIR=... uv pip install --python .venv/bin/python ruff" >&2
  exit 2
fi

echo
echo "=== 4/5 静态门禁：mypy（渐进白名单，只查已能过的模块）==="
if "$PY" -c "import mypy" >/dev/null 2>&1; then
  "$PY" -m mypy
else
  echo "缺少 mypy（dev 依赖）：UV_CACHE_DIR=... uv pip install --python .venv/bin/python mypy" >&2
  exit 2
fi

if [[ "$MODE" == "static" ]]; then
  echo
  echo "结果：静态门禁通过 ✅（--static：未跑用例）"
  exit 0
fi

echo
COVERAGE_ARGS=()
if [[ "${COVERAGE:-}" == "1" ]]; then
  # 覆盖率**只报告、不设阈值**（阈值会在还没准备好时逼人删测试，先让它可见）
  COVERAGE_ARGS=(--cov=docking_agent --cov-report=term-missing:skip-covered)
  echo "（COVERAGE=1：同时输出覆盖率报告）"
fi
if [[ "$MODE" == "fast" ]]; then
  echo "=== 5/5 用例：跳过需要本机引擎的 engine 组（等价 CI）==="
  DOCKING_ENGINE_TESTS=0 "$PY" -m pytest -q ${COVERAGE_ARGS[@]+"${COVERAGE_ARGS[@]}"}
else
  echo "=== 5/5 用例：全量（含真实 Vina / P2Rank / pdb2pqr）==="
  "$PY" -m pytest -q ${COVERAGE_ARGS[@]+"${COVERAGE_ARGS[@]}"}
fi

echo
echo "结果：全部门禁通过 ✅"
