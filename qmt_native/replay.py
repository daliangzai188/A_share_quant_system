"""Replay the production close pipeline steps 2,3,4,6,7 inside an isolated workspace.

Arguments mirror scripts/trading_daemon.py (close pipeline, target date block).
Step 1 (Tushare collection) is skipped: data/raw in the workspace is the input,
which is what lets the native variant swap in QMT-rebuilt limit lists.

Usage: venv/bin/python replay.py WORKSPACE DATE [DATE ...] [--stop-on-error]
"""
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / 'venv' / 'bin' / 'python')
LIVE = {
    'limit': 'data/processed/live_limit_up_merged.csv',
    'sentiment': 'data/processed/live_market_sentiment.csv',
    'emotion': 'data/processed/live_market_emotion_features.csv',
    'theme': 'data/processed/live_theme_heat_features.csv',
    'scored': 'data/processed/live_limit_up_fill_scored.csv',
}


def trade_days(ws):
    rows = list(csv.DictReader(open(ws / 'data/raw/trade_calendar.csv', encoding='utf-8-sig')))
    days = sorted({r['cal_date'] for r in rows if str(r.get('is_open')) in ('1', '1.0', 'True')})
    return days


def build_theme_reference(ws):
    """Offline equivalent of collect_stock_theme_reference.py from its own raw output."""
    out = ws / 'data/processed/stock_theme_reference.csv'
    if out.exists():
        return
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "import pandas as pd\n"
        "from scripts.collect_stock_theme_reference import normalize_industry, PROCESSED_OUTPUT, RAW_OUTPUT\n"
        "raw = pd.read_csv(RAW_OUTPUT, dtype=str)\n"
        "ref = raw[['ts_code','name','industry','market','list_status','list_date','delist_date']].copy()\n"
        "ref['industry'] = ref['industry'].map(normalize_industry)\n"
        "ref['theme_name'] = ref['industry']\n"
        "ref['theme_source_column'] = 'stock_basic.industry'\n"
        "ref['theme_data_available'] = ref['theme_name'].ne('unknown')\n"
        "ref.to_csv(PROCESSED_OUTPUT, index=False, encoding='utf-8-sig')\n"
        "print('theme reference rows', len(ref))\n")
    subprocess.run([PY, '-c', code], cwd=ws, check=True)


def steps_for(day, start):
    return [
        ('clean', ['scripts/clean_collected_data.py', '--start-date', start, '--end-date', day,
                   '--incremental-replace']),
        ('features', ['scripts/build_dynamic_features.py', '--start-date', day, '--end-date', day,
                      '--limit-up-path', LIVE['limit'], '--market-emotion-output', LIVE['emotion'],
                      '--theme-heat-output', LIVE['theme']]),
        ('fill', ['scripts/score_limit_up_fill_probability.py', '--input-path', LIVE['limit'],
                  '--output-path', LIVE['scored'], '--market-sentiment-path', LIVE['sentiment']]),
        ('ac', ['scripts/run_paper_ab_filtered_daily_ops.py', '--signal-date', day, '--top-n', '10',
                '--input-trades-path', LIVE['scored'], '--fill-scored-path', LIVE['scored'],
                '--market-emotion-features-path', LIVE['emotion'],
                '--theme-heat-features-path', LIVE['theme']]),
        ('e', ['scripts/run_strategy_e_signal.py', '--signal-date', day]),
    ]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    stop = '--stop-on-error' in sys.argv
    snapshot = '--snapshot' in sys.argv
    ws = Path(args[0]).resolve()
    days = args[1:]
    calendar = trade_days(ws)
    build_theme_reference(ws)
    log_dir = ws / 'replay_logs'
    log_dir.mkdir(exist_ok=True)
    results = {}
    for day in days:
        idx = calendar.index(day)
        start = calendar[max(0, idx - 2)]  # live_signal_window_trade_days = 3
        results[day] = {}
        for name, cmd in steps_for(day, start):
            t0 = time.time()
            log = log_dir / ('%s_%s.log' % (day, name))
            with open(log, 'w') as handle:
                proc = subprocess.run([PY, '-B'] + cmd, cwd=ws, stdout=handle, stderr=subprocess.STDOUT,
                                      env={'PATH': '/usr/bin:/bin', 'HOME': str(ws), 'PYTHONUTF8': '1',
                                           'TZ': 'Asia/Shanghai'})
            results[day][name] = {'rc': proc.returncode, 'sec': round(time.time() - t0, 1)}
            print('%s %-8s rc=%d %5.1fs  %s' % (day, name, proc.returncode, time.time() - t0, log.name))
            if proc.returncode != 0 and stop:
                print(open(log).read()[-3000:])
                json.dump(results, open(ws / 'replay_status.json', 'w'), indent=1)
                sys.exit(1)
        if snapshot:
            snap = ws / 'snapshots' / day
            snap.mkdir(parents=True, exist_ok=True)
            for rel in LIVE.values():
                if (ws / rel).exists():
                    (snap / Path(rel).name).write_bytes((ws / rel).read_bytes())
            for rel in ['reports/strategy_e/e_signal_runs_recent.json']:
                if (ws / rel).exists():
                    (snap / Path(rel).name).write_bytes((ws / rel).read_bytes())
    json.dump(results, open(ws / 'replay_status.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
