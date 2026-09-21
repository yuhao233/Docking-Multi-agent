#!/bin/bash
# 打包源码，便于迁移到其他机器部署。
#
#   bash scripts/pack.sh                      # → ../docking-agent-local.tar.gz
#   bash scripts/pack.sh -o /tmp/pkg.tar.gz   # 指定输出
#   bash scripts/pack.sh --list               # ★ 只预演：列出会包含的目录与体积（不写文件）
#   bash scripts/pack.sh --with-cache         # 额外带上 assets/cache（受体准备缓存，体积大）
#   bash scripts/pack.sh --with-tools         # 额外带上 assets/tools（P2Rank 等外部工具）
#   bash scripts/pack.sh --max-size 120       # 体积上限 MB（默认 80；超限则失败）
#
# 默认**不带**：.venv / var（运行历史）/ 各类缓存 / 用户上传 / 外部工具 / .env / 本地设置。
# 真实缺陷（本轮修复）：旧版排除项写的是 `assets/receptor/cache`（只 1.3 MB），导致 tar 实测把
# `assets/cache`（786 MB）与 `assets/uploads`（245 MB，**含用户上传数据**）一起打进"源码包"，
# 体积 1.4 GB。现在排除项对齐真实目录，并加了体积上限、`--list` 预演与包内硬校验。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

DEFAULT_OUT="$WORK_DIR/../docking-agent-local.tar.gz"
OUT="$DEFAULT_OUT"
LIST_ONLY=0
WITH_CACHE=0
WITH_TOOLS=0
MAX_MB="${PACK_MAX_MB:-80}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST_ONLY=1; shift ;;
    --with-cache) WITH_CACHE=1; shift ;;
    --with-tools) WITH_TOOLS=1; shift ;;
    --max-size) MAX_MB="${2:?--max-size 需要数值（MB）}"; shift 2 ;;
    -o|--out) OUT="${2:?--out 需要路径}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

# ---- 排除项：必须与真实目录对齐 ----
EXCLUDES=(
  '.venv'
  '.uv-cache'
  '.git'            # 版本库元数据：交付包不需要（也不该外泄历史）
  'var'
  '__pycache__'
  '*.pyc'
  '.pytest_cache'
  '.mypy_cache'
  'node_modules'
  '.env'
  'config/local_settings.json'
  'assets/receptor'         # 旧版受体缓存容器（内含 cache/*.pdb，1.3 MB）
  'assets/uploads'          # ★ 用户上传原件：绝不进交付包
)
[ "$WITH_CACHE" -eq 1 ] || EXCLUDES+=('assets/cache')
[ "$WITH_TOOLS" -eq 1 ] || EXCLUDES+=('assets/tools')

TAR_EXCLUDES=()
for e in "${EXCLUDES[@]}"; do
  TAR_EXCLUDES+=("--exclude=$e")
done

if [ -f config/local_settings.json ]; then
  echo "[pack] 注意：已排除 config/local_settings.json（可能含 API Key）"
fi

# ---- 计算「会被包含的条目」（与 TAR_EXCLUDES 同源，避免两处规则漂移） ----
# is_excluded <相对路径>：命中排除项（支持 *.pyc 这类 glob 与目录前缀）
is_excluded() {
  local path="$1" e
  for e in "${EXCLUDES[@]}"; do
    case "$e" in
      *'*'*) case "$path" in $e) return 0 ;; esac ;;
      *) case "$path" in "$e"|"$e"/*) return 0 ;; esac ;;
    esac
  done
  return 1
}

INCLUDED=()
while IFS= read -r p; do
  rel="${p#./}"
  [ "$rel" = "assets" ] && continue          # assets 单独展开一级（默认只排除其子目录）
  is_excluded "$rel" && continue
  INCLUDED+=("$rel")
done < <(find . -mindepth 1 -maxdepth 1 -printf '%p\n' | sort)
if [ -d assets ]; then
  while IFS= read -r p; do
    rel="${p#./}"
    is_excluded "$rel" && continue
    INCLUDED+=("$rel")
  done < <(find assets -mindepth 1 -maxdepth 1 -printf '%p\n' | sort)
fi

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "[pack] 预演（不写文件）"
  echo "[pack] 工作区：$WORK_DIR"
  echo "[pack] 排除项："
  for e in "${EXCLUDES[@]}"; do
    if [ -e "$e" ]; then
      printf '        - %-28s %8s KB\n' "$e" "$(du -sk "$e" 2>/dev/null | cut -f1)"
    else
      printf '        - %-28s （当前不存在）\n' "$e"
    fi
  done
  echo "[pack] 会包含的条目："
  total_kb=0
  for entry in "${INCLUDED[@]}"; do
    [ -n "$entry" ] || continue
    [ -e "$entry" ] || continue
    kb="$(du -sk "$entry" 2>/dev/null | cut -f1)"
    total_kb=$((total_kb + kb))
    printf '        %-24s %8s KB\n' "$entry" "$kb"
  done
  if [ -d assets/receptors/registry ]; then
    printf '        %-24s %8s KB  （随包：已准备的受体 PDBQT）\n' \
      'assets/receptors/registry' "$(du -sk assets/receptors/registry | cut -f1)"
  fi
  printf '[pack] 预计体积：约 %s MB（阈值 %s MB）\n' "$((total_kb / 1024))" "$MAX_MB"
  if [ "$((total_kb / 1024))" -gt "$MAX_MB" ]; then
    echo "[pack] ✗ 超出体积阈值：请检查是否把缓存 / 上传 / 工具打进来了" >&2
    exit 1
  fi
  echo "[pack] ✓ 预演通过"
  exit 0
fi

# ---- 真打包 ----
rm -f "$OUT"
tar czf "$OUT" "${TAR_EXCLUDES[@]}" .

size_mb=$(( $(stat -c%s "$OUT") / 1024 / 1024 ))
echo "[pack] 已生成 $OUT（${size_mb} MB）"
if [ "$size_mb" -gt "$MAX_MB" ]; then
  echo "[pack] ✗ 体积 ${size_mb} MB 超过阈值 ${MAX_MB} MB：" >&2
  echo "        检查以上排除项；确实需要更大包时用 --max-size 明确放宽。" >&2
  exit 1
fi

# 包内**不得**出现用户上传与缓存（硬校验，避免规则再次腐坏）
if tar tzf "$OUT" | grep -qE '(^|/)assets/(uploads|cache)/'; then
  echo "[pack] ✗ 包内出现 assets/uploads 或 assets/cache —— 排除规则失效" >&2
  exit 1
fi
echo "[pack] ✓ 已确认包内无 assets/uploads、assets/cache"
echo "[pack] 目标机器：tar xzf $(basename "$OUT") && cd projects && bash scripts/doctor.sh && bash start.sh"
