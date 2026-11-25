#!/bin/sh
# Full regression of the packaged QMT-native strategy (QMT's Python 3.6 / pandas 0.22).
# Usage: sh inqmt/run_regression.sh SHADOW_FILE LIVE_FILE OUT_DIR
# 2026-11-26（用户要求加快换版，覆盖不减）：分两路同时跑——一路是下面全部单项测试，仍按原顺序一节一节跑（时间敏感的
# 测试不会彼此抢CPU）；另一路是多日演练。两路输出按原顺序拼回，日志格式与以前逐行同构，deploy.sh 照常核对。
# QN_REGRESS_SERIAL=1 时按以前的顺序一路跑完（对照用）。
cd "$(dirname "$0")/.."
PY="arch -x86_64 $(pwd)/tools/qmt36/bin/python"
S=$1; L=$2; O=$3; mkdir -p "$O"
echo "== core $(sed '2d' inqmt/qn_core_full.py | shasum | cut -c1-16)"   # deploy.sh 按这一行核对回归的是同一个核心包
# 2026-11-15：策略文件第3行的完整代码摘要（运行时、核心包、模拟券商、宿主、包装）；deploy.sh 要求部署的文件与它相同
echo "== artifact $(sed -n '3s/^# code \([0-9a-f]*\), seed \([0-9a-f]*\)\./code \1 seed \2/p' "$1")"
[ "$(sed -n 3p "$1" | cut -d, -f1)" = "$(sed -n 3p "$2" | cut -d, -f1)" ] || echo "== artifact WARNING：影子版和实盘版的代码摘要不同"
export QN_OLD_SQLITE=1
lane_tests() {
echo "== restart";        $PY inqmt/test_restart.py qn_core_full.py 90 > "$O/restart.txt" 2>&1; grep -E "identity equal|rebuilt|persistent files" "$O/restart.txt"
echo "== stop threads";   $PY inqmt/test_stop_threads.py "$S" "$S" 40 45 > "$O/stop.txt" 2>&1; grep RESULT "$O/stop.txt"
echo "== takeover";       $PY inqmt/test_takeover.py "$S" "$L" > "$O/takeover.txt" 2>&1; grep RESULT "$O/takeover.txt"
echo "== night store";    $PY inqmt/test_night_store.py "$S" > "$O/night.txt" 2>&1; grep RESULT "$O/night.txt"
echo "== notify outbox";  (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_notify_outbox.py qn_core_full.py > "../$O/notify.txt" 2>&1); grep RESULT "$O/notify.txt"
echo "== step 13 pushes"; (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_eq_advisory.py qn_core_full.py > "../$O/eq_advisory.txt" 2>&1); grep -E "RESULT|equal|weekly" "$O/eq_advisory.txt"
echo "== forwarder";       python3 inqmt/test_forwarder.py > "$O/forwarder.txt" 2>&1; grep -E "RESULT|identical" "$O/forwarder.txt"
echo "== external weekly"; python3 inqmt/test_external_weekly.py > "$O/external_weekly.txt" 2>&1; grep -E "^[0-9]\. |RESULT" "$O/external_weekly.txt"
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
# 2026-11-23：第①步取不全逐笔就拒绝、被终止后不写涨停池、取全才写全部候选（提速不能造成缺数据）
echo "== missing ticks";  (cd inqmt && PYTHONIOENCODING=utf-8 $PY test_missing_ticks.py qn_core_full.py > "../$O/missing_ticks.txt" 2>&1); grep -E "^[0-9]\. |^RESULT" "$O/missing_ticks.txt"
echo "== freeze";         PYTHONIOENCODING=utf-8 $PY inqmt/test_freeze.py "$S" > "$O/freeze.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/freeze.txt"
echo "== stop slow save";  PYTHONIOENCODING=utf-8 $PY inqmt/test_stop_slow_save.py "$S" > "$O/stop_slow_save.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/stop_slow_save.txt"
echo "== store switch";   PYTHONIOENCODING=utf-8 $PY inqmt/test_store_switch.py "$S" > "$O/store_switch.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/store_switch.txt"
echo "== order save bound"; PYTHONIOENCODING=utf-8 $PY inqmt/test_order_save_bound.py "$S" > "$O/order_save_bound.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/order_save_bound.txt"
echo "== link gate";      PYTHONIOENCODING=utf-8 $PY inqmt/test_link_gate.py "$S" > "$O/link_gate.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/link_gate.txt"
echo "== log store";      PYTHONIOENCODING=utf-8 $PY inqmt/test_log_store.py "$S" > "$O/log_store.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/log_store.txt"
# 11-08: production keeps its state in the QMT log + registry; the scenario tests again on that store
for t in stop_threads:"$S $S 40 45" takeover:"$S $L" trading_switch:"$S" state_check:"$S" state_reset:"$S"; do
  name=${t%%:*}; args=${t#*:}
  echo "== $name (log store)"; QN_TEST_STATE_STORE=log PYTHONIOENCODING=utf-8 $PY inqmt/test_$name.py $args > "$O/${name}_log.txt" 2>&1; grep -E "^RESULT" "$O/${name}_log.txt"
done
echo "== state check";    PYTHONIOENCODING=utf-8 $PY inqmt/test_state_check.py "$S" > "$O/state_check.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/state_check.txt"
echo "== calendar feed";  PYTHONIOENCODING=utf-8 $PY inqmt/test_calendar_feed.py "$S" > "$O/calendar_feed.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/calendar_feed.txt"
echo "== read_csv guard";  PYTHONIOENCODING=utf-8 $PY inqmt/test_read_csv_guard.py "$S" > "$O/read_csv_guard.txt" 2>&1; grep -E "^[0-9]\. |^RESULT" "$O/read_csv_guard.txt"
echo "== tracebacks";     grep -c Traceback "$O"/*.txt
}
lane_drill() {
echo "== drill 10-13 15:25 -> 10-15 15:15"
rm -rf "$O/vday"; mkdir -p "$O/vday"
QN_TEST_GUARD_READ_CSV=1 QN_TEST_GUARD_FILES=1 $PY inqmt/run_vday.py "$S" 202610131525 202610151515 60 "$O/vday" > "$O/vday.log" 2>&1  # QMT 2.1.33 read_csv + file guards on
echo "drill exit $?"; tail -2 "$O/vday.log"
echo "drill tracebacks $(grep -c Traceback "$O/vday.log")"
}
if [ -n "$SKIP_DRILL" ]; then lane_tests; exit 0; fi
if [ "${QN_REGRESS_SERIAL:-}" = "1" ]; then lane_tests; lane_drill; exit 0; fi
lane_tests > "$O/_lane_tests.out" 2>&1 &
TESTS_PID=$!
lane_drill > "$O/_lane_drill.out" 2>&1 &
DRILL_PID=$!
wait "$TESTS_PID"; wait "$DRILL_PID"
cat "$O/_lane_tests.out" "$O/_lane_drill.out"
