#!/usr/bin/env bash
set -euo pipefail

LABEL="${LABEL:-com.a-system.utm-windows-guard}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="${RUNTIME_DIR:-$HOME/.a_system_vm_guard}"

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
pkill -f guard_utm_windows_trading.sh || true

if [[ -f "$PLIST" ]]; then
  rm "$PLIST"
fi

rm -f "$RUNTIME_DIR/guard_utm_windows_trading.sh"

echo "已卸载 LaunchAgent：$LABEL"
