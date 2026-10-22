# coding: gbk
# A_SYSTEM native data phase-0 export (2026-10-19).
# READ-ONLY public market data. No account, no set_account, no orders, no cancels.
# Pauses itself 09:00-15:30 (Beijing) so it never competes with the live model.
# Writes gzip CSV under OUT_DIR. Resumable: restart continues where it stopped.
import datetime
import gzip
import json
import os
import time

OUT_DIR = r'C:\A_System\qmt_native_phase0\out_b'
TICK_DIR = os.path.join(OUT_DIR, 'ticks')
BATCH = 8
CHUNK = 300
PROBE_CODE = '600000.SH'
PROBE_DATES = ['20261008', '20260901', '20260601', '20260302', '20260105',
               '20250602', '20250102', '20240102']
DAILY_START = '20261009'
DAILY_END = '20261019'
EVENTS = [('20261009', '000420.SZ'), ('20261009', '000546.SZ'), ('20261009', '000710.SZ'), ('20261009', '000736.SZ'), ('20261009', '000892.SZ'), ('20261009', '001300.SZ'), ('20261009', '001330.SZ'), ('20261009', '001336.SZ'), ('20261009', '002058.SZ'), ('20261009', '002181.SZ'), ('20261009', '002212.SZ'), ('20261009', '002242.SZ'), ('20261009', '002317.SZ'), ('20261009', '002343.SZ'), ('20261009', '002480.SZ'), ('20261009', '002675.SZ'), ('20261009', '002692.SZ'), ('20261009', '002696.SZ'), ('20261009', '002733.SZ'), ('20261009', '002805.SZ'), ('20261009', '002811.SZ'), ('20261009', '002912.SZ'), ('20261009', '002979.SZ'), ('20261009', '300364.SZ'), ('20261009', '300369.SZ'), ('20261009', '300413.SZ'), ('20261009', '300530.SZ'), ('20261009', '300821.SZ'), ('20261009', '300981.SZ'), ('20261009', '600127.SH'), ('20261009', '600207.SH'), ('20261009', '600241.SH'), ('20261009', '600354.SH'), ('20261009', '600371.SH'), ('20261009', '600400.SH'), ('20261009', '600408.SH'), ('20261009', '600478.SH'), ('20261009', '600621.SH'), ('20261009', '600629.SH'), ('20261009', '600715.SH'), ('20261009', '600812.SH'), ('20261009', '600825.SH'), ('20261009', '600843.SH'), ('20261009', '600865.SH'), ('20261009', '600882.SH'), ('20261009', '601069.SH'), ('20261009', '601086.SH'), ('20261009', '601118.SH'), ('20261009', '601519.SH'), ('20261009', '601566.SH'), ('20261009', '601595.SH'), ('20261009', '601811.SH'), ('20261009', '601929.SH'), ('20261009', '603067.SH'), ('20261009', '603096.SH'), ('20261009', '603101.SH'), ('20261009', '603155.SH'), ('20261009', '603216.SH'), ('20261009', '603232.SH'), ('20261009', '603533.SH'), ('20261009', '605058.SH'), ('20261009', '605136.SH'), ('20261009', '605168.SH'), ('20261009', '605177.SH'), ('20261009', '605188.SH'), ('20261009', '605287.SH'), ('20261009', '605399.SH'), ('20261009', '605577.SH'), ('20261009', '688152.SH'), ('20261012', '000892.SZ'), ('20261012', '001231.SZ'), ('20261012', '001330.SZ'), ('20261012', '002242.SZ'), ('20261012', '002264.SZ'), ('20261012', '002343.SZ'), ('20261012', '002441.SZ'), ('20261012', '002531.SZ'), ('20261012', '002576.SZ'), ('20261012', '002591.SZ'), ('20261012', '002692.SZ'), ('20261012', '002696.SZ'), ('20261012', '002908.SZ'), ('20261012', '002990.SZ'), ('20261012', '300006.SZ'), ('20261012', '300829.SZ'), ('20261012', '301052.SZ'), ('20261012', '301419.SZ'), ('20261012', '301658.SZ'), ('20261012', '600192.SH'), ('20261012', '600241.SH'), ('20261012', '600560.SH'), ('20261012', '600693.SH'), ('20261012', '600812.SH'), ('20261012', '600825.SH'), ('20261012', '600831.SH'), ('20261012', '600851.SH'), ('20261012', '601127.SH'), ('20261012', '601595.SH'), ('20261012', '601811.SH'), ('20261012', '601929.SH'), ('20261012', '603032.SH'), ('20261012', '603096.SH'), ('20261012', '603230.SH'), ('20261012', '603232.SH'), ('20261012', '603336.SH'), ('20261012', '603500.SH'), ('20261012', '603528.SH'), ('20261012', '603533.SH'), ('20261012', '603968.SH'), ('20261012', '603992.SH'), ('20261012', '605136.SH'), ('20261012', '605287.SH'), ('20261012', '605577.SH'), ('20261012', '688004.SH'), ('20261012', '688517.SH'), ('20261013', '000530.SZ'), ('20261013', '000558.SZ'), ('20261013', '000668.SZ'), ('20261013', '000715.SZ'), ('20261013', '000823.SZ'), ('20261013', '000833.SZ'), ('20261013', '000995.SZ'), ('20261013', '001229.SZ'), ('20261013', '001231.SZ'), ('20261013', '001318.SZ'), ('20261013', '002172.SZ'), ('20261013', '002184.SZ'), ('20261013', '002322.SZ'), ('20261013', '002343.SZ'), ('20261013', '002430.SZ'), ('20261013', '002549.SZ'), ('20261013', '002579.SZ'), ('20261013', '002587.SZ'), ('20261013', '002623.SZ'), ('20261013', '002639.SZ'), ('20261013', '002640.SZ'), ('20261013', '002693.SZ'), ('20261013', '002696.SZ'), ('20261013', '002748.SZ'), ('20261013', '002811.SZ'), ('20261013', '002862.SZ'), ('20261013', '002878.SZ'), ('20261013', '002917.SZ'), ('20261013', '002949.SZ'), ('20261013', '002980.SZ'), ('20261013', '003037.SZ'), ('20261013', '300107.SZ'), ('20261013', '301010.SZ'), ('20261013', '301041.SZ'), ('20261013', '600199.SH'), ('20261013', '600202.SH'), ('20261013', '600228.SH'), ('20261013', '600237.SH'), ('20261013', '600281.SH'), ('20261013', '600391.SH'), ('20261013', '600593.SH'), ('20261013', '600693.SH'), ('20261013', '600712.SH'), ('20261013', '600802.SH'), ('20261013', '600831.SH'), ('20261013', '600847.SH'), ('20261013', '600851.SH'), ('20261013', '600853.SH'), ('20261013', '601188.SH'), ('20261013', '603001.SH'), ('20261013', '603042.SH'), ('20261013', '603096.SH'), ('20261013', '603099.SH'), ('20261013', '603188.SH'), ('20261013', '603328.SH'), ('20261013', '603353.SH'), ('20261013', '603386.SH'), ('20261013', '603466.SH'), ('20261013', '603767.SH'), ('20261013', '603956.SH'), ('20261013', '603968.SH'), ('20261013', '603980.SH'), ('20261013', '605058.SH'), ('20261013', '605136.SH'), ('20261013', '605258.SH'), ('20261013', '605577.SH'), ('20261013', '688655.SH'), ('20261014', '000530.SZ'), ('20261014', '000573.SZ'), ('20261014', '000601.SZ'), ('20261014', '000668.SZ'), ('20261014', '000823.SZ'), ('20261014', '001216.SZ'), ('20261014', '001299.SZ'), ('20261014', '001318.SZ'), ('20261014', '002029.SZ'), ('20261014', '002343.SZ'), ('20261014', '002469.SZ'), ('20261014', '002552.SZ'), ('20261014', '002696.SZ'), ('20261014', '002813.SZ'), ('20261014', '002860.SZ'), ('20261014', '002917.SZ'), ('20261014', '300006.SZ'), ('20261014', '300299.SZ'), ('20261014', '600097.SH'), ('20261014', '600121.SH'), ('20261014', '600202.SH'), ('20261014', '600252.SH'), ('20261014', '600391.SH'), ('20261014', '600408.SH'), ('20261014', '600847.SH'), ('20261014', '601188.SH'), ('20261014', '601811.SH'), ('20261014', '601858.SH'), ('20261014', '603096.SH'), ('20261014', '603258.SH'), ('20261014', '603533.SH'), ('20261014', '603598.SH'), ('20261014', '603656.SH'), ('20261014', '603968.SH'), ('20261014', '603992.SH'), ('20261014', '605189.SH'), ('20261015', '000530.SZ'), ('20261015', '000626.SZ'), ('20261015', '000722.SZ'), ('20261015', '000995.SZ'), ('20261015', '001225.SZ'), ('20261015', '001318.SZ'), ('20261015', '002103.SZ'), ('20261015', '002134.SZ'), ('20261015', '002343.SZ'), ('20261015', '002543.SZ'), ('20261015', '002696.SZ'), ('20261015', '002748.SZ'), ('20261015', '002815.SZ'), ('20261015', '002836.SZ'), ('20261015', '002889.SZ'), ('20261015', '002945.SZ'), ('20261015', '003029.SZ'), ('20261015', '300111.SZ'), ('20261015', '301282.SZ'), ('20261015', '600281.SH'), ('20261015', '600668.SH'), ('20261015', '600721.SH'), ('20261015', '600812.SH'), ('20261015', '601811.SH'), ('20261015', '603172.SH'), ('20261015', '603200.SH'), ('20261015', '603230.SH'), ('20261015', '603237.SH'), ('20261015', '603329.SH'), ('20261015', '603386.SH'), ('20261015', '603535.SH'), ('20261015', '603690.SH'), ('20261015', '603999.SH'), ('20261015', '605003.SH'), ('20261015', '605058.SH'), ('20261015', '605189.SH'), ('20261016', '000099.SZ'), ('20261016', '000420.SZ'), ('20261016', '000530.SZ'), ('20261016', '000573.SZ'), ('20261016', '000576.SZ'), ('20261016', '000607.SZ'), ('20261016', '000701.SZ'), ('20261016', '000887.SZ'), ('20261016', '001319.SZ'), ('20261016', '001358.SZ'), ('20261016', '001696.SZ'), ('20261016', '002076.SZ'), ('20261016', '002085.SZ'), ('20261016', '002510.SZ'), ('20261016', '002535.SZ'), ('20261016', '002614.SZ'), ('20261016', '002683.SZ'), ('20261016', '002724.SZ'), ('20261016', '002767.SZ'), ('20261016', '002869.SZ'), ('20261016', '002889.SZ'), ('20261016', '002917.SZ'), ('20261016', '002933.SZ'), ('20261016', '002949.SZ'), ('20261016', '003029.SZ'), ('20261016', '300045.SZ'), ('20261016', '300411.SZ'), ('20261016', '300581.SZ'), ('20261016', '300681.SZ'), ('20261016', '301586.SZ'), ('20261016', '600148.SH'), ('20261016', '600178.SH'), ('20261016', '600319.SH'), ('20261016', '600418.SH'), ('20261016', '600501.SH'), ('20261016', '600668.SH'), ('20261016', '600699.SH'), ('20261016', '600802.SH'), ('20261016', '600825.SH'), ('20261016', '600843.SH'), ('20261016', '600860.SH'), ('20261016', '601606.SH'), ('20261016', '601811.SH'), ('20261016', '601999.SH'), ('20261016', '603013.SH'), ('20261016', '603015.SH'), ('20261016', '603168.SH'), ('20261016', '603173.SH'), ('20261016', '603197.SH'), ('20261016', '603239.SH'), ('20261016', '603278.SH'), ('20261016', '603305.SH'), ('20261016', '603348.SH'), ('20261016', '603636.SH'), ('20261016', '603668.SH'), ('20261016', '603955.SH'), ('20261016', '603999.SH'), ('20261016', '605178.SH'), ('20261016', '688592.SH'), ('20261016', '920006.BJ'), ('20261016', '920690.BJ'), ('20261019', '688507.SH'), ('20261019', '605488.SH'), ('20261019', '605258.SH'), ('20261019', '605255.SH'), ('20261019', '605058.SH'), ('20261019', '603859.SH'), ('20261019', '603693.SH'), ('20261019', '603626.SH'), ('20261019', '603348.SH'), ('20261019', '603330.SH'), ('20261019', '603286.SH'), ('20261019', '603278.SH'), ('20261019', '603197.SH'), ('20261019', '603188.SH'), ('20261019', '603119.SH'), ('20261019', '603040.SH'), ('20261019', '601778.SH'), ('20261019', '600860.SH'), ('20261019', '600686.SH'), ('20261019', '600487.SH'), ('20261019', '600329.SH'), ('20261019', '600267.SH'), ('20261019', '600178.SH'), ('20261019', '600148.SH'), ('20261019', '600032.SH'), ('20261019', '301666.SZ'), ('20261019', '301591.SZ'), ('20261019', '301313.SZ'), ('20261019', '301119.SZ'), ('20261019', '300852.SZ'), ('20261019', '300798.SZ'), ('20261019', '003029.SZ'), ('20261019', '003021.SZ'), ('20261019', '002929.SZ'), ('20261019', '002889.SZ'), ('20261019', '002886.SZ'), ('20261019', '002724.SZ'), ('20261019', '002626.SZ'), ('20261019', '002368.SZ'), ('20261019', '002343.SZ'), ('20261019', '002164.SZ'), ('20261019', '002085.SZ'), ('20261019', '001323.SZ'), ('20261019', '001216.SZ'), ('20261019', '001211.SZ'), ('20261019', '000576.SZ')]
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
