"""Generate the QMT phase-0 export strategy with the event list embedded.

The generated file must stay pure ASCII and Python 3.6 compatible: it runs inside
Guojin QMT 2.1.19 built-in Python (3.6, pandas 0.22, numpy 1.19).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = r'''# coding: gbk
# A_SYSTEM native data phase-0 export (2026-10-19).
# READ-ONLY public market data. No account, no set_account, no orders, no cancels.
# Pauses itself 09:00-15:30 (Beijing) so it never competes with the live model.
# Writes gzip CSV under OUT_DIR. Resumable: restart continues where it stopped.
import datetime
import gzip
import json
import os
import time

OUT_DIR = r'C:\A_System\qmt_native_phase0\out'
TICK_DIR = os.path.join(OUT_DIR, 'ticks')
BATCH = 8
CHUNK = 300
PROBE_CODE = '600000.SH'
PROBE_DATES = ['20261008', '20260901', '20260601', '20260302', '20260105',
               '20250602', '20250102', '20240102']
DAILY_START = '__DAILY_START__'
DAILY_END = '__DAILY_END__'
EVENTS = __EVENTS__
SECTORS = [u'\u6caa\u6df1A\u80a1', u'\u4eac\u5e02A\u80a1']
SCALARS = ['lastPrice', 'open', 'high', 'low', 'lastClose', 'amount', 'volume',
           'pvolume', 'stockStatus', 'transactionNum']
LEVELS = ['bidPrice', 'bidVol', 'askPrice', 'askVol']
DAILY_FIELDS = ['open', 'high', 'low', 'close', 'preClose', 'volume', 'amount']
DETAIL_FIELDS = ['InstrumentName', 'OpenDate', 'FloatVolume', 'TotalVolume',
                 'UpStopPrice', 'DownStopPrice', 'PreClose']

S = {'phase': 'probe', 'i': 0, 'chunk': 0, 'codes': None, 'retry': [],
     'retry_pass': False, 'busy': False, 'done': False, 'started': '',
     'counts': {'ok': 0, 'skip': 0, 'empty': 0, 'fail': 0}}


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _paused():
    hm = datetime.datetime.now().strftime('%H%M')
    return '0900' <= hm < '1530'


def _fmt(value):
    try:
        number = float(value)
    except Exception:
        return ''
    if number != number:
        return ''
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _write_json(name, payload):
    path = os.path.join(OUT_DIR, name)
    tmp = path + '.part'
    with open(tmp, 'w') as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _append(name, line):
    with open(os.path.join(OUT_DIR, name), 'a') as handle:
        handle.write(line + '\n')


def _read(C, code, period, start, end):
    result = C.get_market_data_ex([], [code], period=period, start_time=start,
                                  end_time=end, count=-1, dividend_type='none',
                                  fill_data=False, subscribe=False)
    if not result:
        return None
    return result.get(code)


def _progress():
    _write_json('progress.json', {
        'updated': _now(), 'started': S['started'], 'phase': S['phase'],
        'events_total': len(EVENTS), 'events_done': S['i'],
        'retry_pending': len(S['retry']), 'counts': S['counts'], 'done': S['done']})


def init(C):
    if bool(getattr(C, 'do_back_test', False)):
        raise RuntimeError('PHASE0 must be started with Run, not Backtest')
    for path in (OUT_DIR, TICK_DIR):
        if not os.path.isdir(path):
            os.makedirs(path)
    S['started'] = _now()
    C.run_time('phase0_pump', '2nSecond', '2020-01-01 00:00:00')
    print('PHASE0 STARTED events=%d out=%s' % (len(EVENTS), OUT_DIR))


def handlebar(C):
    pass


def phase0_pump(C):
    if S['done'] or S['busy']:
        return
    if _paused():
        return
    S['busy'] = True
    try:
        if S['phase'] == 'probe':
            _run_probe(C)
            S['phase'] = 'daily'
        elif S['phase'] == 'daily':
            if _run_daily_chunk(C):
                S['phase'] = 'ticks'
        elif S['phase'] == 'ticks':
            _run_tick_batch(C)
        _progress()
    except Exception as exc:
        _append('errors.log', '%s %s %s %s' % (_now(), S['phase'], type(exc).__name__, str(exc)[:300]))
    finally:
        S['busy'] = False


def _run_probe(C):
    out = {'started': _now()}
    out['api_globals'] = sorted(k for k, v in globals().items()
                                if callable(v) and not k.startswith('_'))
    out['context_attrs'] = sorted(a for a in dir(C) if not a.startswith('_'))
    for period in ('tick', '1m'):
        rows = []
        for day in PROBE_DATES:
            try:
                download_history_data(PROBE_CODE, period, day + '093000', day + '150000')
                frame = _read(C, PROBE_CODE, period, day + '093000', day + '150000')
                rows.append([day, 0 if frame is None else len(frame)])
            except Exception as exc:
                rows.append([day, 'ERR %s %s' % (type(exc).__name__, str(exc)[:80])])
        out['depth_' + period] = rows
    for code in ('600825.SH', '002058.SZ'):
        try:
            download_history_data(code, 'limitupperformance', '20261008', '20261008')
            frame = _read(C, code, 'limitupperformance', '20261008', '20261008')
            out['limitup_' + code] = 'rows=%s cols=%s' % (
                0 if frame is None else len(frame), [] if frame is None else list(frame.columns))
        except Exception as exc:
            out['limitup_' + code] = 'ERR %s %s' % (type(exc).__name__, str(exc)[:120])
    for period in ('l2quote', 'l2transaction', 'l2order'):
        try:
            download_history_data(PROBE_CODE, period, '20261008093000', '20261008100000')
            frame = _read(C, PROBE_CODE, period, '20261008093000', '20261008100000')
            out['l2_' + period] = 'rows=%s cols=%s' % (
                0 if frame is None else len(frame), [] if frame is None else list(frame.columns))
        except Exception as exc:
            out['l2_' + period] = 'ERR %s %s' % (type(exc).__name__, str(exc)[:120])
    try:
        detail = C.get_instrument_detail(PROBE_CODE)
        out['detail_keys'] = sorted(str(k) for k in detail.keys())
    except Exception as exc:
        out['detail_keys'] = 'ERR %s' % type(exc).__name__
    lister = globals().get('get_sector_list')
    if lister is not None:
        try:
            names = list(lister())
            out['sector_count'] = len(names)
            out['sector_names'] = [n for n in names][:3000]
        except Exception as exc:
            out['sector_count'] = 'ERR %s %s' % (type(exc).__name__, str(exc)[:80])
    out['finished'] = _now()
    _write_json('probe.json', out)
    print('PHASE0 PROBE DONE')


def _codes(C):
    if S['codes'] is None:
        codes = set()
        for sector in SECTORS:
            try:
                codes.update(C.get_stock_list_in_sector(sector))
            except Exception as exc:
                _append('errors.log', '%s sector %s' % (_now(), type(exc).__name__))
        S['codes'] = sorted(codes)
        _append('daily_meta.txt', 'codes=%d at %s' % (len(S['codes']), _now()))
    return S['codes']


def _run_daily_chunk(C):
    codes = _codes(C)
    start = S['chunk'] * CHUNK
    part = codes[start:start + CHUNK]
    if not part:
        return True
    bars = os.path.join(OUT_DIR, 'daily_bars.csv')
    details = os.path.join(OUT_DIR, 'instrument_details.csv')
    if S['chunk'] == 0:
        for path, header in ((bars, ['code', 'date'] + DAILY_FIELDS),
                             (details, ['code'] + DETAIL_FIELDS)):
            with open(path, 'w') as handle:
                handle.write(','.join(header) + '\n')
    data = C.get_market_data_ex(DAILY_FIELDS, part, period='1d', start_time=DAILY_START,
                                end_time=DAILY_END, count=-1, dividend_type='none',
                                fill_data=False, subscribe=False) or {}
    missing = [c for c in part if data.get(c) is None or len(data.get(c)) == 0]
    if missing:
        for code in missing:
            try:
                download_history_data(code, '1d', DAILY_START, DAILY_END)
            except Exception:
                pass
        again = C.get_market_data_ex(DAILY_FIELDS, missing, period='1d', start_time=DAILY_START,
                                     end_time=DAILY_END, count=-1, dividend_type='none',
                                     fill_data=False, subscribe=False) or {}
        data.update({c: f for c, f in again.items() if f is not None and len(f)})
        _append('daily_meta.txt', 'chunk %d missing_first_read=%d still_missing=%d' % (
            S['chunk'], len(missing), len([c for c in missing if data.get(c) is None or len(data.get(c)) == 0])))
    with open(bars, 'a') as handle:
        for code in part:
            frame = data.get(code)
            if frame is None:
                continue
            for index, row in frame.iterrows():
                handle.write(','.join([code, str(index)[:8]] +
                                      [_fmt(row.get(f)) for f in DAILY_FIELDS]) + '\n')
    with open(details, 'a', encoding='utf-8') as handle:
        for code in part:
            try:
                detail = C.get_instrument_detail(code) or {}
            except Exception:
                detail = {}
            values = []
            for field in DETAIL_FIELDS:
                value = detail.get(field, '')
                values.append(str(value).replace(',', ' ') if field == 'InstrumentName' else _fmt(value) if value != '' else '')
            handle.write(','.join([code] + values) + '\n')
    S['chunk'] += 1
    return False


def _write_ticks(path, frame):
    columns = set(frame.columns)
    stamps = [str(x) for x in frame.index]
    scalars = [frame[c].values if c in columns else None for c in SCALARS]
    levels = [frame[c].values if c in columns else None for c in LEVELS]
    header = ['ts'] + SCALARS + ['%s%d' % (c, k + 1) for c in LEVELS for k in range(5)]
    tmp = path + '.part'
    with gzip.open(tmp, 'wt') as handle:
        handle.write(','.join(header) + '\n')
        for i in range(len(stamps)):
            row = [stamps[i]]
            for arr in scalars:
                row.append('' if arr is None else _fmt(arr[i]))
            for arr in levels:
                cell = None if arr is None else arr[i]
                for k in range(5):
                    try:
                        row.append(_fmt(cell[k]))
                    except Exception:
                        row.append('')
            handle.write(','.join(row) + '\n')
    os.replace(tmp, path)
    return stamps[0] if stamps else '', stamps[-1] if stamps else ''


def _export_event(C, day, code, final_pass):
    folder = os.path.join(TICK_DIR, day)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, code + '.csv.gz')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        S['counts']['skip'] += 1
        return
    start, end = day + '091500', day + '150100'
    try:
        if not final_pass:
            download_history_data(code, 'tick', start, end)
        frame = _read(C, code, 'tick', start, end)
    except Exception as exc:
        S['counts']['fail'] += 1
        _append('ticks_index.csv', '%s,%s,0,,,FAIL %s' % (day, code, type(exc).__name__))
        return
    if frame is None or len(frame) == 0:
        if final_pass:
            S['counts']['empty'] += 1
            _append('ticks_index.csv', '%s,%s,0,,,EMPTY' % (day, code))
        else:
            S['retry'].append((day, code))
        return
    first, last = _write_ticks(path, frame)
    S['counts']['ok'] += 1
    _append('ticks_index.csv', '%s,%s,%d,%s,%s,OK' % (day, code, len(frame), first, last))


def _run_tick_batch(C):
    if not S['retry_pass']:
        done = 0
        while S['i'] < len(EVENTS) and done < BATCH:
            day, code = EVENTS[S['i']]
            _export_event(C, day, code, False)
            S['i'] += 1
            done += 1
        if S['i'] >= len(EVENTS):
            S['retry_pass'] = True
        return
    done = 0
    while S['retry'] and done < BATCH:
        day, code = S['retry'].pop(0)
        _export_event(C, day, code, True)
        done += 1
    if not S['retry']:
        S['done'] = True
        _progress()
        print('PHASE0 COMPLETE %s' % S['counts'])
'''


def main():
    # argv: TARGET [EVENTS_JSON] [OUT_SUFFIX] [DAILY_START]
    events_json = sys.argv[2] if len(sys.argv) > 2 else 'phase0_events.json'
    out_suffix = sys.argv[3] if len(sys.argv) > 3 else 'out'
    daily_start = sys.argv[4] if len(sys.argv) > 4 else '20260701'
    data = json.load(open(HERE / events_json))
    dates = data['dates']
    events = [tuple(e) for e in data['events']]
    body = TEMPLATE.replace('__EVENTS__', repr(events))
    body = body.replace('__DAILY_START__', daily_start).replace('__DAILY_END__', dates[-1])
    body = body.replace(r"qmt_native_phase0\out'", "qmt_native_phase0\\" + out_suffix + "'")
    target = Path(sys.argv[1])
    target.write_bytes(body.encode('ascii'))
    print('wrote %s (%d bytes, %d events)' % (target, len(body), len(events)))


if __name__ == '__main__':
    main()
