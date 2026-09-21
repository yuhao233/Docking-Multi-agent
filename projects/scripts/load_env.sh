#!/bin/bash
# 加载 .env 到当前 shell（替代原平台的 coze_workload_identity 环境变量下发）
# 用法: source scripts/load_env.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DOCKING_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
ENV_FILE="${ENV_FILE:-$WORK_DIR/.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "[load_env] 未找到 $ENV_FILE（可复制 .env.example）" >&2
  return 0 2>/dev/null || exit 0
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export DOCKING_WORKSPACE="$WORK_DIR"
