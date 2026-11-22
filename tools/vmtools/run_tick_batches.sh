#!/bin/sh
# Run QMT probe batches PROBE_TICK_QUEUE_<k>.py (k from $1 to $2) in the scratch editor, one after another.
# Every click/keystroke is guarded: the Mac user must be idle >= 60 s and VMware must be frontmost.
cd "$(dirname "$0")"
guard() {
  i=0
  while [ "$(./idle.sh)" -lt 60 ]; do sleep 15; i=$((i+1)); [ $i -gt 240 ] && { echo "user active too long"; exit 1; }; done
  ./vmfront.sh >/dev/null || { echo "VMware not front"; exit 1; }
}
for k in $(seq -f "%02g" "$1" "$2"); do
  guard; ./vmctl click 987 853; sleep 1.5
  guard; ./vmctl click 600 400; sleep 0.3
  guard; ./vmctl type "Get-Content -Raw C:\\A_System\\qmt_native_phase0\\PROBE_TICK_QUEUE_$k.py | Set-Clipboard"; ./vmctl key return; sleep 2
  guard; ./vmctl click 1075 853; sleep 1.5
  guard; ./vmctl click 985 438; sleep 0.5
  guard; ./vmctl key a ctrl; sleep 0.5; ./vmctl key v ctrl; sleep 3
  guard; ./vmctl click 962 191
  echo "batch $k started $(TZ=Asia/Shanghai date +%H:%M:%S)"
  sleep 330
  ./shot.sh tq_$k >/dev/null
done
echo all done
