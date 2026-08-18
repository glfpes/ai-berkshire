#!/usr/bin/env bash
# 定位并停止后台运行的 SNDK 监控进程 (sndk_alert.py)
#
# 用法:
#   ./stop.sh          # 列出进程并逐个确认后 kill
#   ./stop.sh -y       # 直接 kill 全部, 不确认
#   ./stop.sh -9       # 用 SIGKILL 强制结束 (先试普通 kill 无效时用)
#
set -u

PATTERN="sndk_alert.py"
FORCE_YES=0
SIGNAL="TERM"

for arg in "$@"; do
  case "$arg" in
    -y|--yes)   FORCE_YES=1 ;;
    -9|--force) SIGNAL="KILL" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

# 找到所有匹配进程的 PID (排除本脚本自身和 grep)
PIDS=$(pgrep -f "$PATTERN" 2>/dev/null)

if [ -z "$PIDS" ]; then
  echo "未发现正在运行的 $PATTERN 进程。"
  exit 0
fi

echo "发现以下 $PATTERN 进程:"
# shellcheck disable=SC2086
ps -o pid,stat,start,time,command -p $PIDS

echo
for pid in $PIDS; do
  if [ "$FORCE_YES" -ne 1 ]; then
    printf "结束进程 %s ? [y/N] " "$pid"
    read -r ans </dev/tty
    case "$ans" in
      y|Y) ;;
      *) echo "跳过 $pid"; continue ;;
    esac
  fi
  if kill -"$SIGNAL" "$pid" 2>/dev/null; then
    echo "已发送 SIG$SIGNAL 给 $pid"
  else
    echo "kill $pid 失败 (可能已退出或需要 -9)" >&2
  fi
done

# 等待并复查
sleep 1
LEFT=$(pgrep -f "$PATTERN" 2>/dev/null)
if [ -z "$LEFT" ]; then
  echo "全部已停止。"
else
  echo "仍在运行: $LEFT (可尝试 ./stop.sh -9 强制结束)"
fi
