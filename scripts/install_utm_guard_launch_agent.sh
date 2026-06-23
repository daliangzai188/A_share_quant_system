#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/user/Desktop/A_System}"
VM_NAME="${1:-Windows}"
LABEL="${LABEL:-com.a-system.utm-windows-guard}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="${RUNTIME_DIR:-$HOME/.a_system_vm_guard}"
RUNTIME_BIN="$RUNTIME_DIR/guard_utm_windows_trading.sh"
LOG_DIR="$RUNTIME_DIR/logs"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cp "$PROJECT_DIR/scripts/guard_utm_windows_trading.sh" "$RUNTIME_BIN"
chmod +x "$RUNTIME_BIN"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>LOG_DIR="$LOG_DIR" exec "$RUNTIME_BIN" "$VM_NAME"</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>WorkingDirectory</key>
  <string>$RUNTIME_DIR</string>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/launch_agent.out.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/launch_agent.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
pkill -f guard_utm_windows_trading.sh || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "已安装并启动 LaunchAgent：$LABEL"
echo "配置文件：$PLIST"
echo "运行脚本：$RUNTIME_BIN"
echo "查看状态：launchctl print gui/$UID/$LABEL"
echo "查看日志：tail -50 $LOG_DIR/utm_windows_guard_\$(date +%Y%m%d).log"
