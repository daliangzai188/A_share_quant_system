#!/bin/sh
# usage: eq_full_run.sh WORKSPACE SIGNAL_DATE
cd "$1" && env -i PATH=/usr/bin:/bin HOME="$PWD" PYTHONUTF8=1 TZ=Asia/Shanghai \
  /Users/eass/A_System_Rebuild/2026-10-19/qmt_native_full/venv/bin/python -B scripts/update_equity_curve_stop.py --signal-date "$2" > eq_run.log 2>&1
echo "EXIT $?" >> eq_run.log
