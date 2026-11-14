#!/bin/sh
# Keep the host awake while this Fusion VM exists; leave display and lock alone.
set -eu
VM_PATTERN="${1:?Pass the target vmware-vmx process pattern}"
guard_caffeine_pid=''
cleanup() {
    if [ -n "$guard_caffeine_pid" ]; then
        /bin/kill "$guard_caffeine_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM
while :; do
    vm_pid="$(/usr/bin/pgrep -f "$VM_PATTERN" | /usr/bin/head -n 1)"
    if [ -n "$vm_pid" ]; then
        /bin/date '+%Y-%m-%d %H:%M:%S host awake; display and screen lock allowed'
        /usr/bin/caffeinate -i -w "$vm_pid" &
        guard_caffeine_pid=$!
        wait "$guard_caffeine_pid" || true
        guard_caffeine_pid=''
    fi
    /bin/sleep 15
done
