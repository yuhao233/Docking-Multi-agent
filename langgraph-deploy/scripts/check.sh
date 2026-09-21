#!/usr/bin/env bash
# 部署自检（可重复运行）：
#   1) 依赖清单与 ../projects/pyproject.toml 逐条比对（防止一边加包、另一边漏装）；
#   2) langgraph.json 里每个图真加载一遍（含无 checkpointer 校验）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[[ -x "$PY" ]] || { echo "缺少 .venv，请先 bash scripts/install.sh" >&2; exit 2; }

echo "=== 1/2 依赖清单一致性（部署包 vs 项目）==="
"$PY" - "$HERE" <<'PY'
import sys, tomllib
from pathlib import Path

here = Path(sys.argv[1]).resolve()
deploy = tomllib.loads((here / "pyproject.toml").read_text(encoding="utf-8"))
project = tomllib.loads((here / "../projects/pyproject.toml").read_text(encoding="utf-8"))
d = set(deploy["project"]["dependencies"])
p = set(project["project"]["dependencies"])
if d == p:
    print(f"  [OK] {len(d)} 条依赖与项目完全一致")
    sys.exit(0)
print("  [FAIL] 依赖清单不一致（部署包必须镜像项目依赖）")
for x in sorted(p - d):
    print(f"    - 项目有、部署包缺：{x}")
for x in sorted(d - p):
    print(f"    + 部署包多出（确认是刻意的再保留）：{x}")
sys.exit(1)
PY

echo
echo "=== 2/2 图加载自检 ==="
"$PY" "$HERE/scripts/verify_graphs.py"
