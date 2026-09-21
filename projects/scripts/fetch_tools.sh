#!/bin/bash
# 可选外部工具：P2Rank（口袋预测）按需获取到 assets/tools/。
#
#   bash scripts/fetch_tools.sh                 # 下载并解压 P2Rank 到 assets/tools/
#   bash scripts/fetch_tools.sh --check         # 只检查现有状态（不下载）
#   bash scripts/fetch_tools.sh --url <URL>     # 指定镜像（内网/离线源）
#
# 为什么单独一个脚本：`assets/tools` 有 290+ MB，**不进交付包**（见 scripts/pack.sh）。
# 目标机器联网时用它补齐；不装也能跑 —— 口袋分析会退化为内置几何法，报告如实写明引擎。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
cd "$WORK_DIR"

DEFAULT_URL="https://github.com/rdk/p2rank/releases/download/2.5.1/p2rank_2.5.1.tar.gz"
URL="${P2RANK_URL:-$DEFAULT_URL}"
CHECK_ONLY=0
DEST_DIR="assets/tools"

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --url) URL="${2:?--url 需要 URL}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

existing="$(find "$DEST_DIR" -maxdepth 2 -type f -name prank 2>/dev/null | head -1 || true)"
if [ -n "$existing" ]; then
  echo "[tools] ✓ 已就绪：$existing"
  echo "[tools]   Java 缺失时仍无法运行：doctor 会单独检查 java"
  exit 0
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "[tools] ○ 未找到 P2Rank（口袋分析将使用内置几何法）"
  echo "[tools]   补齐：bash scripts/fetch_tools.sh"
  exit 2
fi

mkdir -p "$DEST_DIR"
target="$DEST_DIR/$(basename "$URL")"
echo "[tools] 下载 $URL"
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 3 -o "$target" "$URL"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$target" "$URL"
else
  echo "[tools] ✗ 需要 curl 或 wget；也可手动下载后用 --url file:///path/to/pkg.tar.gz" >&2
  exit 1
fi

echo "[tools] 解压到 $DEST_DIR/"
tar xzf "$target" -C "$DEST_DIR"
rm -f "$target"

found="$(find "$DEST_DIR" -maxdepth 2 -type f -name prank | head -1 || true)"
if [ -z "$found" ]; then
  echo "[tools] ✗ 解压后未找到 prank，请检查包内容" >&2
  exit 1
fi
echo "[tools] ✓ 已安装：$found"
echo "[tools] 下一步：bash scripts/doctor.sh   # 确认 P2Rank 与 Java 都被识别"
