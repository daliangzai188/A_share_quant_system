#!/usr/bin/env bash
set -euo pipefail

VM_NAME="${1:-Windows}"
UTMCTL="${UTMCTL:-/Applications/UTM.app/Contents/MacOS/utmctl}"
CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
AUTO_START="${AUTO_START:-0}"
LOG_DIR="${LOG_DIR:-logs/vm_guard}"
LOG_FILE="$LOG_DIR/utm_windows_guard_$(date +%Y%m%d).log"
LOCK_DIR="${LOCK_DIR:-/tmp/a_system_utm_guard_${VM_NAME}.lock}"

usage() {
  cat <<'EOF'
用法：
  scripts/guard_utm_windows_trading.sh [虚拟机名称]

默认：
  scripts/guard_utm_windows_trading.sh

后台运行，推荐交易时段使用：
  nohup scripts/guard_utm_windows_trading.sh Windows >> logs/vm_guard/guard.nohup.log 2>&1 &

可选环境变量：
  CHECK_INTERVAL=60   每多少秒检查一次
  AUTO_START=0        发现虚拟机停止时是否自动启动，默认只提醒不自动启动
  LOG_DIR=logs/vm_guard

说明：
  这个脚本会定时记录 UTM 虚拟机状态。
  只有虚拟机处于 started/paused 时，才会用 caffeinate 防止 Mac 睡眠。
  交易时段默认不自动重启虚拟机，避免在有未处理委托时擅自重启交易终端。
EOF
}

notify() {
  local title="$1"
  local message="$2"
  /usr/bin/osascript -e "display notification \"$message\" with title \"$title\"" >/dev/null 2>&1 || true
}

log() {
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -x "$UTMCTL" ]]; then
  echo "错误：找不到 UTM 命令行工具：$UTMCTL" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  existing_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "守护脚本已经在运行：pid=$existing_pid"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi

echo "$$" > "$LOCK_DIR/pid"
caffeinate_pid=""
trap 'if [[ -n "$caffeinate_pid" ]]; then kill "$caffeinate_pid" >/dev/null 2>&1 || true; fi; rm -rf "$LOCK_DIR"' EXIT INT TERM

start_caffeinate() {
  if [[ -n "$caffeinate_pid" ]] && kill -0 "$caffeinate_pid" 2>/dev/null; then
    return
  fi
  caffeinate -dimsu &
  caffeinate_pid=$!
  log "CAFFEINATE started pid=$caffeinate_pid"
}

stop_caffeinate() {
  if [[ -n "$caffeinate_pid" ]]; then
    kill "$caffeinate_pid" >/dev/null 2>&1 || true
    wait "$caffeinate_pid" 2>/dev/null || true
    log "CAFFEINATE stopped pid=$caffeinate_pid"
    caffeinate_pid=""
  fi
}

log "启动守护：vm=$VM_NAME interval=${CHECK_INTERVAL}s auto_start=$AUTO_START"
notify "UTM 守护已启动" "正在监控 $VM_NAME；仅在虚拟机运行时阻止 Mac 睡眠。"

last_status=""
while true; do
  vm_line="$("$UTMCTL" list | awk -v name="$VM_NAME" 'NR > 1 && $3 == name { print; exit }' || true)"

  if [[ -z "$vm_line" ]]; then
    log "ERROR vm_not_found name=$VM_NAME"
    notify "UTM 虚拟机未找到" "没有找到名为 $VM_NAME 的虚拟机。"
    sleep "$CHECK_INTERVAL"
    continue
  fi

  vm_uuid="$(awk '{print $1}' <<<"$vm_line")"
  vm_status="$(awk '{print $2}' <<<"$vm_line")"
  memory_pressure="$(memory_pressure 2>/dev/null | awk -F': ' '/System-wide memory free percentage/ { print $2 }' | tr -d '%' || true)"

  if [[ "$vm_status" != "$last_status" ]]; then
    log "STATUS uuid=$vm_uuid status=$vm_status memory_free_pct=${memory_pressure:-unknown}"
    last_status="$vm_status"
  else
    log "HEARTBEAT status=$vm_status memory_free_pct=${memory_pressure:-unknown}"
  fi

  if [[ "$vm_status" == "stopped" ]]; then
    stop_caffeinate
    notify "Windows 虚拟机已停止" "$VM_NAME 当前是 stopped，请检查 QMT/交易终端。"
    if [[ "$AUTO_START" == "1" ]]; then
      log "AUTO_START vm=$VM_NAME"
      "$UTMCTL" start "$vm_uuid" >/dev/null || log "ERROR auto_start_failed vm=$VM_NAME"
    fi
  elif [[ "$vm_status" == "paused" ]]; then
    start_caffeinate
    notify "Windows 虚拟机已暂停" "$VM_NAME 当前是 paused，交易时段不建议暂停。"
  elif [[ "$vm_status" == "started" ]]; then
    start_caffeinate
  else
    stop_caffeinate
  fi

  if [[ -n "${memory_pressure:-}" && "$memory_pressure" =~ ^[0-9]+$ && "$memory_pressure" -lt 15 ]]; then
    notify "Mac 内存偏紧" "剩余内存约 ${memory_pressure}%，建议关闭无关程序。"
    log "WARN low_memory_free_pct=$memory_pressure"
  fi

  sleep "$CHECK_INTERVAL"
done
