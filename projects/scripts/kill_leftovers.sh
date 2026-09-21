#!/bin/bash
# ============================================================================
# 清理残留的对接进程（大库对接被中断后可能留下的 Vina 工作进程）
#
#   bash scripts/kill_leftovers.sh              # 只清理工作进程，保留 GUI 服务
#   bash scripts/kill_leftovers.sh --dry-run    # 只列出将被清理的进程
#   bash scripts/kill_leftovers.sh --include-server   # 连服务进程一起停掉
#
# 为什么需要它：大库（上千～上万分子）对接会起多进程池；如果服务/脚本被强杀
# （Ctrl-C 多次、kill -9、断线），进程池 worker 可能被 init 收养成为孤儿，
# 继续吃满 CPU。此时 **必须从宿主 shell 运行本脚本** —— 若你的 shell 处于
# PID 命名空间内（容器/沙箱），只能看到自己命名空间的进程，无法触达这些孤儿。
#
# 识别规则（只处理**当前用户**且属于**本项目**的进程）：
#   * /proc/PID/exe 指向 projects/.venv/bin/python（本项目的虚拟环境）
#   * cwd 在 projects/ 下且 cmdline 含 multiprocessing（进程池 worker）
#   * cmdline 含 vina / mk_prepare_receptor / prank（对接相关外部进程）
# 默认保护：本项目 http 服务（cmdline 含 "docking_agent -m http"）与脚本自身。
# ============================================================================
set -u

DRY_RUN=0
INCLUDE_SERVER=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --include-server) INCLUDE_SERVER=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（支持 --dry-run / --include-server）" >&2; exit 2 ;;
  esac
done

MARK="kill_leftovers.sh"
ME=$(id -un)
SELF=$$
ROOT="/home/biolab/Tools/docking-agent/projects"

echo "===== 清理前 ====="
uptime
echo "线程数: $(ps -eLf 2>/dev/null | wc -l)"
echo

protected=" $SELF "
p=$SELF
while [ -n "$p" ] && [ "$p" != "1" ]; do
  pp=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)
  [ -n "${pp:-}" ] || break
  protected="$protected $pp "
  p=$pp
done

victims=""
echo "===== 扫描本项目相关进程 ====="
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  case "$protected" in *" $pid "*) continue;; esac
  [ -r "$d/exe" ] || continue
  owner=$(stat -c %U "$d" 2>/dev/null) || continue
  [ "$owner" = "$ME" ] || continue
  exe=$(readlink -f "$d/exe" 2>/dev/null) || continue
  cwd=$(readlink -f "$d/cwd" 2>/dev/null) || continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
  [ -n "${cmd// /}" ] || continue
  case "$cmd" in *"$MARK"*) continue;; esac
  case "$cmd" in *npm/_npx*) continue;; esac
  if [ "$INCLUDE_SERVER" = "0" ]; then
    case "$cmd" in *"docking_agent -m http"*) echo "  跳过(GUI 服务，如需一并停止请加 --include-server): $pid"; continue;; esac
  fi

  ours=0
  case "$exe" in "$ROOT"/.venv/bin/python*) ours=1;; esac
  case "$cwd" in "$ROOT"*)
      case "$cmd" in *multiprocessing*) ours=1;; esac ;;
  esac
  case "$cmd" in *vina*|*mk_prepare_receptor*|*prank*) ours=1;; esac
  [ "$ours" = "1" ] || continue

  echo "  命中: pid=$pid  ${cmd:0:150}"
  victims="$victims $pid"
done

if [ -z "${victims// /}" ]; then
  echo "  （未发现残留对接进程）"
  echo
  echo "===== 清理后 ====="; uptime
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "--dry-run：以上进程**未被**结束。去掉 --dry-run 即执行清理。"
  exit 0
fi

echo
echo "===== 发送 TERM ====="
# shellcheck disable=SC2086
kill -TERM $victims 2>/dev/null
sleep 4

echo "===== 幸存者强制 KILL ====="
for pid in $victims; do
  if kill -0 "$pid" 2>/dev/null; then
    echo "  KILL $pid"
    kill -KILL "$pid" 2>/dev/null
  fi
done
sleep 2

echo
echo "===== 清理后 ====="
uptime
echo "线程数: $(ps -eLf 2>/dev/null | wc -l)"
