#!/usr/bin/env bash
# 第二阶段（Docker）准备：把项目源码复制进构建上下文，并生成 langgraph.build.json。
#
# 为什么需要复制：`langgraph build/up` 的构建上下文 = langgraph.json 所在目录，
# 而依赖里写 `../projects` 是**目录外路径**，Docker 不会带进镜像。因此构建前把源码
# 同步到 build-src/projects（排除 .venv / var / 缓存），构建配置指向它。
# 副本是**生成物**（已 gitignore），projects/ 本身不会被改动。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/../projects"
DST="$HERE/build-src/projects"

command -v rsync >/dev/null || { echo "需要 rsync" >&2; exit 2; }
mkdir -p "$(dirname "$DST")"
echo "同步源码：$SRC → $DST"
rsync -a --delete \
  --exclude '.venv/' --exclude 'var/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude 'node_modules/' --exclude '.git/' \
  --exclude 'assets/uploads/' --exclude 'assets/cache/' \
  "$SRC/" "$DST/"

echo "生成 langgraph.build.json"
python3 - "$HERE" <<'PY'
import json, sys
from pathlib import Path
here = Path(sys.argv[1])
cfg = json.loads((here / "langgraph.json").read_text(encoding="utf-8"))
cfg["dependencies"] = ["./build-src/projects", "."]
(here / "langgraph.build.json").write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("  依赖：", cfg["dependencies"])
PY
echo "完成。现在可以：bash scripts/build.sh  /  bash scripts/up.sh"
