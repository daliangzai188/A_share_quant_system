#!/bin/sh
# Full regression of the packaged QMT-native strategy (QMT's Python 3.6 / pandas 0.22).
# Usage: sh inqmt/run_regression.sh SHADOW_FILE LIVE_FILE OUT_DIR
cd "$(dirname "$0")/.."
PY="arch -x86_64 $(pwd)/tools/qmt36/bin/python"
S=$1; L=$2; O=$3; mkdir -p "$O"
export QN_OLD_SQLITE=1
echo "== restart";        $PY inqmt/test_restart.py qn_core_full.py 90 > "$O/restart.txt" 2>&1; grep -E "identity equal|rebuilt|persistent files" "$O/restart.txt"
echo "== stop threads";   $PY inqmt/test_stop_threads.py "$S" "$S" 40 45 > "$O/stop.txt" 2>&1; grep RESULT "$O/stop.txt"
echo "== takeover";       $PY inqmt/test_takeover.py "$S" "$L" > "$O/takeover.txt" 2>&1; grep RESULT "$O/takeover.txt"
echo "== night store";    $PY inqmt/test_night_store.py "$S" > "$O/night.txt" 2>&1; grep RESULT "$O/night.txt"
echo "== notify outbox";  (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_notify_outbox.py qn_core_full.py > "../$O/notify.txt" 2>&1); grep RESULT "$O/notify.txt"
echo "== step 13 pushes"; (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_eq_advisory.py qn_core_full.py > "../$O/eq_advisory.txt" 2>&1); grep -E "RESULT|equal|weekly" "$O/eq_advisory.txt"
echo "== forwarder";       python3 inqmt/test_forwarder.py > "$O/forwarder.txt" 2>&1; grep -E "RESULT|identical" "$O/forwarder.txt"
echo "== export";         $PY inqmt/test_export.py "$S" > "$O/export.txt" 2>&1; grep -E "RESULT|identical" "$O/export.txt"
echo "== cache keeper";   $PY inqmt/test_cache_keeper.py "$S" > "$O/cache_keeper.txt" 2>&1; grep -E "RESULT|keeper downloads" "$O/cache_keeper.txt"
echo "== process kill";   (cd inqmt && $PY test_process_kill.py qn_core_full.py > "../$O/kill.txt" 2>&1); grep -E "RESULT|queued" "$O/kill.txt"
echo "== daemon crash";   $PY inqmt/test_daemon_crash.py "$S" > "$O/daemon_crash.txt" 2>&1; grep -E "RESULT|restarted|os._exit" "$O/daemon_crash.txt"
echo "== state store";    (cd inqmt && $PY test_state_store.py qn_host.py > "../$O/state_store.txt" 2>&1); grep -E "^[A-L]\.|RESULT" "$O/state_store.txt"
echo "== save stall";     PYTHONIOENCODING=utf-8 $PY inqmt/test_save_stall.py "$S" > "$O/save_stall.txt" 2>&1; grep -E "RESULT|hang lasted|bridge-lost" "$O/save_stall.txt"
echo "== gil yield";      PYTHONIOENCODING=utf-8 $PY inqmt/test_gil_yield.py "$S" > "$O/gil_yield.txt" 2>&1; grep -E "^[0-9]\.|RESULT" "$O/gil_yield.txt"
echo "== state reset";    PYTHONIOENCODING=utf-8 $PY inqmt/test_state_reset.py "$S" > "$O/state_reset.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/state_reset.txt"
echo "== no disk reads";  PYTHONIOENCODING=utf-8 $PY inqmt/test_no_disk_reads.py "$S" > "$O/no_disk_reads.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/no_disk_reads.txt"
echo "== store link";     PYTHONIOENCODING=utf-8 $PY inqmt/test_store_link.py "$S" > "$O/store_link.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/store_link.txt"
echo "== trading switch"; PYTHONIOENCODING=utf-8 $PY inqmt/test_trading_switch.py "$S" > "$O/trading_switch.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/trading_switch.txt"
echo "== partial day";    (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_partial_day.py > "../$O/partial_day.txt" 2>&1); grep -E "^partial|^bars|^RESULT" "$O/partial_day.txt"
echo "== freeze";         PYTHONIOENCODING=utf-8 $PY inqmt/test_freeze.py "$S" > "$O/freeze.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/freeze.txt"
echo "== tracebacks";     grep -c Traceback "$O"/*.txt
[ -n "$SKIP_DRILL" ] && exit 0
echo "== drill 10-13 15:25 -> 10-15 15:15"
rm -rf "$O/vday"; mkdir -p "$O/vday"
$PY inqmt/run_vday.py "$S" 202610131525 202610151515 60 "$O/vday" > "$O/vday.log" 2>&1
echo "drill exit $?"; tail -2 "$O/vday.log"
