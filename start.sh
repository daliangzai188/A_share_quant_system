#!/bin/bash
# A_System 本地启动（Mac 模拟盘）
# 启动守护进程 + watchdog，实时显示日志
# Ctrl+C 断开日志显示，守护进程继续在后台运行

set -e
cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"

info()    { echo -e "\033[1;32m✅ $*\033[0m"; }
warn()    { echo -e "\033[1;33m⚠️  $*\033[0m"; }
error()   { echo -e "\033[1;31m❌ $*\033[0m"; exit 1; }
section() { echo -e "\033[1;36m\n▶ $*\033[0m"; }

echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
echo -e "\033[1;37m   A_System 启动  $(date '+%Y-%m-%d %H:%M:%S')\033[0m"
echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"

# ── 检查 Python 环境 ──────────────────────────────────────────────────────────
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -f "$PYTHON" ] || error "未找到 .venv/bin/python，请先运行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

# ── 加载环境变量 ──────────────────────────────────────────────────────────────
[ -f "$PROJECT_ROOT/.env" ] && { set -a; source "$PROJECT_ROOT/.env"; set +a; }

DAEMON="$PROJECT_ROOT/scripts/trading_daemon.py"
LOG="$PROJECT_ROOT/logs/trading_daemon.log"
PID_FILE="$PROJECT_ROOT/.daemon_pid"
W_PID_FILE="$PROJECT_ROOT/.watchdog_pid"

mkdir -p "$PROJECT_ROOT/logs"

# ── 第一步：停止旧进程 ────────────────────────────────────────────────────────
section "① 停止旧进程（如有）"

if [ -f "$W_PID_FILE" ]; then
  W=$(cat "$W_PID_FILE")
  kill "$W" 2>/dev/null && echo "  watchdog 已停止（PID $W）" || true
  rm -f "$W_PID_FILE"
fi

if [ -f "$PID_FILE" ]; then
  D=$(cat "$PID_FILE")
  kill "$D" 2>/dev/null && echo "  守护进程已停止（PID $D）" || true
  sleep 1
  kill -9 "$D" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

pkill -f "trading_daemon.py" 2>/dev/null && echo "  残留进程已清理" || true

# 等待所有残留进程完全退出，再启新进程
for i in 1 2 3 4 5; do
  pgrep -f "trading_daemon.py" > /dev/null 2>&1 || break
  sleep 1
done

# ── 第二步：启动守护进程 ──────────────────────────────────────────────────────
section "② 启动守护进程"

nohup "$PYTHON" -B "$DAEMON" > /dev/null 2>&1 &
DPID=$!
echo "$DPID" > "$PID_FILE"
info "守护进程已启动（PID $DPID）"

# ── 第三步：启动 watchdog ─────────────────────────────────────────────────────
section "③ 启动 watchdog（崩溃自动重启）"

(
  while true; do
    sleep 30
    DPID=$(cat "$PID_FILE" 2>/dev/null || echo 0)
    if ! ps -p "$DPID" > /dev/null 2>&1; then
      echo "[$(date '+%H:%M:%S')] watchdog: 守护进程挂了，重启..." >> "$LOG"
      set -a; [ -f "$PROJECT_ROOT/.env" ] && source "$PROJECT_ROOT/.env"; set +a
      nohup "$PYTHON" -B "$DAEMON" > /dev/null 2>&1 &
      NEW_PID=$!
      echo "$NEW_PID" > "$PID_FILE"
      echo "[$(date '+%H:%M:%S')] watchdog: 新 PID $NEW_PID" >> "$LOG"
    fi
  done
) &
echo $! > "$W_PID_FILE"

# ── 确认启动成功 ──────────────────────────────────────────────────────────────
sleep 2
DPID=$(cat "$PID_FILE" 2>/dev/null || echo 0)
if ps -p "$DPID" > /dev/null 2>&1; then
  info "启动成功（守护进程 PID $DPID，watchdog PID $(cat $W_PID_FILE)）"
else
  error "启动失败，查看日志：$LOG"
fi

# ── 第四步：实时显示日志 ──────────────────────────────────────────────────────
echo ""
echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
info "实时日志（Ctrl+C 断开日志，守护进程继续在后台运行）"
echo -e "\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
echo ""

tail -n 30 -f "$LOG"
