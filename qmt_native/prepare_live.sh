#!/bin/sh
# Build and check the live QMT strategy file from production's state on signal day D0.
# Run after production's close pipeline finished and, on the VM, upload_eq_dataset.ps1,
# upload_raw.ps1 and state_inventory.ps1 uploaded their zips to received/.
#
# Usage: sh prepare_live.sh D0 [OUT_FILE]
#   1. unpack the uploads, require production's stop decision to be for D0
#   2. refresh the offline inputs (raw since 10-09, production state snapshot, workspace code)
#   3. step-13 seed for D0 (full replay; NAV must match production's)
#   4. step 13 for D0 inside the bundle must give production's decision, bit for bit
#   5. build the live file, takeover test, copy it to the VM folder
set -e
cd "$(dirname "$0")"
D0=$1
OUT=${2:-inqmt/A_SYSTEM_QMT_NATIVE_live.py}
[ -n "$D0" ] || { echo "usage: sh prepare_live.sh D0 [OUT_FILE]"; exit 2; }
W=live_inputs_$D0
SEED=$PWD/eqseed_live_$D0
REPO=/Users/eass/Desktop/A_System
# broker account and paper cash stay out of git (AGENTS.md rule 11): environment or ./local.env
[ -f local.env ] && . ./local.env
[ -n "$QMT_ACCOUNT_ID" ] || { echo "set QMT_ACCOUNT_ID (environment or ./local.env)"; exit 2; }
QN_PAPER_CASH=${QN_PAPER_CASH:-100000}
PY36="arch -x86_64 $PWD/tools/qmt36/bin/python"

echo "== 1. unpack uploads"
rm -rf "$W"; mkdir -p "$W"
venv/bin/python - "$W" <<'EOF'
import sys, zipfile
from pathlib import Path
out = Path(sys.argv[1])
for name, sub in (('eq_dataset.zip', 'dataset'), ('eq_state.zip', 'state'), ('phase0_raw.zip', 'raw'),
                  ('as_state_inventory.zip', 'inventory')):
    with zipfile.ZipFile(Path('received') / name) as z:
        for info in z.infolist():
            rel = info.filename.replace('\\', '/')  # Windows Compress-Archive writes backslashes
            if rel.endswith('/'):
                continue
            dest = out / sub / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))
    print(name, 'unpacked')
EOF
venv/bin/python - "$W" "$D0" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1] + '/state/equity_curve_stop_decision.json', encoding='utf-8-sig'))
print('production decision: signal', d['signal_date'], 'action', d['action_date'], 'allowed', d['allowed'])
if d['signal_date'] != sys.argv[2]:
    raise SystemExit('production decision is for %s, not %s: wait for production step 13' % (d['signal_date'], sys.argv[2]))
EOF

echo "== 2. refresh offline inputs"
mkdir -p vm_raw_latest_unz eq_full_20261019
rsync -a "$W/raw/" vm_raw_latest_unz/
rsync -a "$W/raw/data/raw/" eq_full_20261019/data/raw/
rsync -a --delete "$REPO/src/" eq_full_20261019/src/
rsync -a --delete "$REPO/scripts/" eq_full_20261019/scripts/
rsync -a "$REPO/config/" eq_full_20261019/config/
rm -rf vm_state; mkdir -p vm_state; cp -R "$W/inventory/." vm_state/
ls vm_state/files/reports/execution_tracking 2>/dev/null || echo "(no execution tracking files in the snapshot)"

echo "== 3. step-13 seed for $D0"
rm -rf "$SEED"
venv/bin/python build_eq_seed.py "$PWD/eq_full_20261019" "$PWD/$W/dataset" "$PWD/$W/state/shadow_nav_latest.csv" "$D0" "$SEED" \
  2>&1 | grep -v " | \|INFO" | tee "$W/seed.log" | tail -12
grep -q "mismatches 0 " "$W/seed.log" || { echo "SEED NAV DOES NOT MATCH PRODUCTION"; exit 1; }
rm -rf "$SEED/tmp_replay"
mkdir -p "$SEED/data/processed" "$SEED/data/raw/stock_basic" "$SEED/data/state" "$SEED/reports/equity_curve_stop"
cp "$W/state/shadow_nav_latest.csv" "$SEED/reports/equity_curve_stop/"
cp "$W/state/equity_curve_stop_decision.json" "$SEED/data/state/"
cp "$REPO"/data/processed/fill_rate_*.csv "$SEED/data/processed/"
[ -f "$W/raw/data/processed/stock_theme_reference.csv" ] && cp "$W/raw/data/processed/stock_theme_reference.csv" "$SEED/data/processed/"
cp "$W/raw/data/raw/stock_basic/stock_basic_all.csv" "$SEED/data/raw/stock_basic/"

echo "== 4. step 13 for $D0 inside the bundle vs production"
venv/bin/python build_inqmt.py "$REPO" inqmt/qn_core_full.py full | tail -1
(cd inqmt && PYTHONIOENCODING=utf-8 $PY36 - "$SEED" "$D0" "$PWD/../$W/state/equity_curve_stop_decision.json" <<'EOF'
import json, sys
sys.argv = [sys.argv[0], 'qn_core_full.py'] + sys.argv[1:]
import test_eq_advisory as t
seed, day, prod_path = sys.argv[2], sys.argv[3], sys.argv[4]
prod = json.load(open(prod_path, encoding='utf-8-sig'))
d, state, lines, pushes = t.run(seed, day)
keys = ('signal_date', 'action_date', 'allowed', 'lag_nav', 'lag_ma', 'shadow_last_nav')
diff = dict((k, (d.get(k), prod.get(k))) for k in keys if d.get(k) != prod.get(k))
for l in lines:
    print('  ', l[:140])
print('decision identical to production:', not diff, diff or '')
if diff:
    raise SystemExit(1)
EOF
)

echo "== 5. build live file"
QN_EQ_SEED="$SEED" venv/bin/python build_qmt_strategy.py live "$QMT_ACCOUNT_ID" 0 "$OUT" | tail -1
QN_EQ_SEED="$SEED" venv/bin/python build_qmt_strategy.py shadow "$QMT_ACCOUNT_ID" "$QN_PAPER_CASH" inqmt/A_SYSTEM_QMT_NATIVE_shadow.py | tail -1
QN_OLD_SQLITE=1 $PY36 inqmt/test_takeover.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py "$OUT" 2>&1 | grep -E "^[0-9]\.|RESULT"
grep -q "release" "$OUT" && head -2 "$OUT" | tail -1
case "$OUT" in inqmt/A_SYSTEM_QMT_NATIVE_live.py) cp "$OUT" "$HOME/Desktop/A_System/qmt_native_phase0/" && echo "copied to the VM folder";; esac
echo "PREPARE_LIVE DONE $D0"
