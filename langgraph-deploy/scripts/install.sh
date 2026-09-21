#!/usr/bin/env bash
# 建立/更新部署用的独立 venv（**不动** projects/.venv）。
#
#   bash scripts/install.sh            # 首次安装（或补装缺失依赖）
#   bash scripts/install.sh --recreate # 删掉重建
#
# 做四件事：
#   1) 建 .venv（Python 3.12）；
#   2) 以 **editable** 方式安装 ../projects  → 单一事实来源，改源码立即生效；
#   3) 安装本部署包（docking_graphs）与 langgraph-cli[inmem]；
#   4) 若缺少 .env，从 ../projects/.env 复制一份（600 权限，已 gitignore）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT_DIR:-$HERE/../projects}"
VENV="$HERE/.venv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HERE/../.uv-cache}"

say() { printf '\033[36m[install]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[install] 失败：\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f "$PROJECT/pyproject.toml" ]] || die "找不到项目：$PROJECT（可用 PROJECT_DIR=... 指定）"
command -v uv >/dev/null || die "缺少 uv（本机在 /home/biolab/pymol/bin/uv，请加入 PATH）"

if [[ "${1:-}" == "--recreate" ]]; then
  say "删除旧 venv：$VENV"
  rm -rf "$VENV"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  say "创建 venv：$VENV"
  uv venv "$VENV" --python 3.12
fi
PY="$VENV/bin/python"

say "安装项目本体（editable）：$PROJECT"
uv pip install --python "$PY" -e "$PROJECT"

say "安装部署包（docking_graphs）"
uv pip install --python "$PY" -e "$HERE"

say "安装 LangGraph CLI（内存运行时，供 langgraph dev / Studio 使用）"
uv pip install --python "$PY" "langgraph-cli[inmem]>=0.4"

if [[ ! -f "$HERE/.env" ]]; then
  if [[ -f "$PROJECT/.env" ]]; then
    install -m 600 "$PROJECT/.env" "$HERE/.env"
    # matplotlib 缓存目录固定到项目内：首次 import matplotlib 会创建该目录（os.mkdir），
    # 若留在事件循环里，langgraph dev 的 blockbuster 会判为 BlockingError。
    mkdir -p "$PROJECT/var/cache/matplotlib"
    printf '\n# LangGraph 部署追加：matplotlib 缓存目录固定到项目内\nMPLCONFIGDIR=%s/var/cache/matplotlib\n' \
      "$PROJECT" >> "$HERE/.env"
    say "已从 projects/.env 复制部署 .env（600，已 gitignore）并固定 MPLCONFIGDIR"
  else
    say "警告：$PROJECT/.env 不存在，请自行创建 $HERE/.env（参考 projects/.env.example）"
  fi
fi

say "版本确认"
"$VENV/bin/langgraph" --version || true
"$PY" - <<'PY'
import importlib.metadata as md
for pkg in ("docking-agent", "docking-agent-langgraph", "langgraph", "langchain",
            "langgraph-cli", "langgraph-runtime-inmem"):
    try:
        print(f"  {pkg:26s} {md.version(pkg)}")
    except md.PackageNotFoundError:
        print(f"  {pkg:26s} 未安装")
PY

say "完成。下一步：bash scripts/check.sh && bash scripts/dev.sh"
