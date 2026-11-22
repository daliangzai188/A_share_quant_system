#!/bin/sh
# One probe batch: copy ${PREFIX:-PROBE_TICK_QUEUE_}$1.py to the VM clipboard (terminal must be front), switch to QMT,
# paste into the scratch editor and run. Guarded: idle >= 60 s and VMware frontmost before every input.
cd "$(dirname "$0")"
[ -n "$SKIP_IDLE" ] || [ "$(./idle.sh)" -ge 60 ] || { echo "USER ACTIVE"; exit 1; }   # checked once: our own synthetic input resets the idle timer
g() { ./vmfront.sh >/dev/null || { echo "NOT FRONT"; exit 1; }; }
g; ./vmctl click 600 400; sleep 0.3
g; ./vmctl type "Get-Content -Raw C:\\A_System\\qmt_native_phase0\\${PREFIX:-PROBE_TICK_QUEUE_}$1.py | Set-Clipboard; 'copied $1'"; ./vmctl key return; sleep 2
./shot.sh bs_copy >/dev/null
g; ./vmctl click 1075 853; sleep 1.5
g; ./vmctl click 985 438; sleep 0.5
g; ./vmctl key a ctrl; sleep 0.5; ./vmctl key v ctrl; sleep 3
g; ./vmctl click 962 191
echo "batch $1 run clicked $(TZ=Asia/Shanghai date +%H:%M:%S)"
