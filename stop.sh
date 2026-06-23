#!/bin/bash
# A_System 本地停止（Mac 模拟盘）
# 停止守护进程和 watchdog

cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"

info()  { echo -e "\033[1;32m✅ $*\033[0m"; }
warn()  { echo -e "\033[1;33m⚠️  $*\033[0m"; }

echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
echo -e "\033[1;37m   A_System 停止  $(date '+%Y-%m-%d %H:%M:%S')\033[0m"
echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"

PID_FILE="$PROJECT_ROOT/.daemon_pid"
W_PID_FILE="$PROJECT_ROOT/.watchdog_pid"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

send_notify() {
  local event="$1"
  local title="$2"
  local body="$3"
  local level="${4:-active}"
  if [ -f "$PYTHON" ]; then
    "$PYTHON" "$PROJECT_ROOT/scripts/send_notify.py" "$event" "$title" "$body" --level "$level" >/dev/null 2>&1 || true
  fi
}

# 先停 watchdog，防止它重启 daemon
if [ -f "$W_PID_FILE" ]; then
  W=$(cat "$W_PID_FILE")
  kill "$W" 2>/dev/null && echo "  watchdog 已停止（PID $W）" || warn "watchdog 未运行"
  rm -f "$W_PID_FILE"
else
  warn "未找到 watchdog PID 文件"
fi

# 停守护进程
if [ -f "$PID_FILE" ]; then
  D=$(cat "$PID_FILE")
  if kill "$D" 2>/dev/null; then
    echo "  守护进程已停止（PID $D）"
    send_notify "system_error" "🔌 守护进程已停止" "守护进程已手动停止，实盘自动交易已暂停。如需恢复请重新启动。" "timeSensitive"
  else
    warn "守护进程未运行"
  fi
  sleep 2
  kill -9 "$D" 2>/dev/null || true
  rm -f "$PID_FILE"
else
  warn "未找到守护进程 PID 文件"
fi

# 清理残留
pkill -f "trading_daemon.py" 2>/dev/null && echo "  残留进程已清理" || true

info "所有进程已停止"
echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
