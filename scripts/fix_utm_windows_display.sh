#!/usr/bin/env bash
set -euo pipefail

VM_NAME="${1:-Windows}"
DISPLAY_MODE="${DISPLAY_MODE:-virtio-ramfb-gl}"
UTMCTL="${UTMCTL:-/Applications/UTM.app/Contents/MacOS/utmctl}"
UTM_DOCS="${UTM_DOCS:-$HOME/Library/Containers/com.utmapp.UTM/Data/Documents}"
STABLE_MODE="${STABLE_MODE:-0}"
FORCE_ON_STOP_TIMEOUT="${FORCE_ON_STOP_TIMEOUT:-1}"

usage() {
  cat <<'EOF'
用法：
  scripts/fix_utm_windows_display.sh [虚拟机名称]

默认修复：
  scripts/fix_utm_windows_display.sh

指定虚拟机名称：
  scripts/fix_utm_windows_display.sh Windows

更保守模式，会额外关闭动态分辨率：
  STABLE_MODE=1 scripts/fix_utm_windows_display.sh

可选环境变量：
  DISPLAY_MODE=virtio-ramfb-gl
  UTMCTL=/Applications/UTM.app/Contents/MacOS/utmctl
  FORCE_ON_STOP_TIMEOUT=1
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -x "$UTMCTL" ]]; then
  echo "错误：找不到 UTM 命令行工具：$UTMCTL" >&2
  echo "请确认 UTM 已安装在 /Applications/UTM.app" >&2
  exit 1
fi

if [[ ! -d "$UTM_DOCS" ]]; then
  echo "错误：找不到 UTM 虚拟机目录：$UTM_DOCS" >&2
  exit 1
fi

vm_line="$("$UTMCTL" list | awk -v name="$VM_NAME" 'NR > 1 && $3 == name { print; exit }')"
if [[ -z "$vm_line" ]]; then
  echo "错误：没有找到名为 '$VM_NAME' 的 UTM 虚拟机。" >&2
  echo "当前虚拟机列表：" >&2
  "$UTMCTL" list >&2
  exit 1
fi

vm_uuid="$(awk '{print $1}' <<<"$vm_line")"
vm_status="$(awk '{print $2}' <<<"$vm_line")"
was_running=0

if [[ "$vm_status" == "started" || "$vm_status" == "paused" ]]; then
  was_running=1
  echo "正在停止虚拟机：$VM_NAME ($vm_status)"
  if ! "$UTMCTL" stop "$vm_uuid" >/dev/null; then
    echo "警告：UTM 正常停止失败，准备等待后判断是否需要强制停止。" >&2
  fi

  for _ in {1..30}; do
    current_status="$("$UTMCTL" list | awk -v uuid="$vm_uuid" '$1 == uuid { print $2; exit }')"
    if [[ "$current_status" == "stopped" ]]; then
      break
    fi
    sleep 1
  done

  current_status="$("$UTMCTL" list | awk -v uuid="$vm_uuid" '$1 == uuid { print $2; exit }')"
  if [[ "$current_status" != "stopped" ]]; then
    if [[ "$FORCE_ON_STOP_TIMEOUT" == "1" ]]; then
      echo "警告：虚拟机未能正常停止，当前状态：$current_status，开始强制停止。" >&2
      pkill -f "$vm_uuid" || true
      sleep 2
      pkill -x UTM || true
      sleep 2
      current_status="$("$UTMCTL" list | awk -v uuid="$vm_uuid" '$1 == uuid { print $2; exit }')"
    fi

    if [[ "$current_status" != "stopped" ]]; then
      echo "错误：虚拟机未能停止，当前状态：$current_status" >&2
      exit 1
    fi
  fi
else
  echo "虚拟机当前不是运行状态：$vm_status"
fi

config_path=""
while IFS= read -r candidate; do
  candidate_name="$(/usr/libexec/PlistBuddy -c 'Print :Information:Name' "$candidate/config.plist" 2>/dev/null || true)"
  if [[ "$candidate_name" == "$VM_NAME" ]]; then
    config_path="$candidate/config.plist"
    break
  fi
done < <(find "$UTM_DOCS" -maxdepth 1 -name '*.utm' -type d | sort)

if [[ -z "$config_path" ]]; then
  echo "错误：找到 UTM 列表里的虚拟机，但没有找到对应 config.plist。" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_path="$config_path.bak_fix_display_$timestamp"
cp "$config_path" "$backup_path"

echo "已备份配置：$backup_path"
echo "正在修改显示模式：$DISPLAY_MODE"
plutil -replace Display.0.Hardware -string "$DISPLAY_MODE" "$config_path"
plutil -replace Display.0.DownscalingFilter -string Linear "$config_path"
plutil -replace Display.0.UpscalingFilter -string Linear "$config_path"

if [[ "$STABLE_MODE" == "1" ]]; then
  echo "启用保守模式：关闭动态分辨率"
  plutil -replace Display.0.DynamicResolution -bool NO "$config_path"
fi

echo "当前显示配置："
plutil -p "$config_path" | sed -n '/"Display"/,/]/p'

if [[ "$was_running" == "1" ]]; then
  echo "正在重新启动虚拟机：$VM_NAME"
  "$UTMCTL" start "$vm_uuid" >/dev/null
else
  echo "虚拟机修复完成。它原本没有运行，所以保持停止状态。"
fi

echo "完成。"
