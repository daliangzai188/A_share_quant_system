"""Package the QMT-native A_System into ONE strategy file for QMT's built-in Python.

The file is ASCII, `# coding: gbk`, and contains:
  * the runtime, the bundled production modules, the paper broker and the host,
    compressed (zlib + base64) and executed at load time;
  * QN_DEPLOY: account, mode (shadow|live), release id and seed files (calendar, limit
    lists, theme reference, fill tables, migrated state);
  * QMT entry points init / a_system_pump / handlebar / order/deal/error callbacks / stop.

Usage: venv/bin/python build_qmt_strategy.py MODE ACCOUNT CASH OUT_FILE
"""
import base64
import datetime
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
INQMT = HERE / 'inqmt'
REPO = Path('/Users/eass/Desktop/A_System')


def pack(data):
    return base64.b64encode(zlib.compress(data, 9)).decode('ascii')


EQ_SEED = Path(os.environ['QN_EQ_SEED']) if os.environ.get('QN_EQ_SEED') else None


def persist_patterns():
    """QN_PERSIST_INCLUDE of inqmt/qn_host.py (single source of truth)."""
    import re
    src = (INQMT / 'qn_host.py').read_text(encoding='utf-8')
    block = src[src.index('QN_PERSIST_INCLUDE = ('):]
    block = block[:block.index('\n)\n') + 3]
    ns = {'_hre': re}
    exec(block, ns)
    return ns['QN_PERSIST_INCLUDE']


def seed_files():
    seed = {}

    def add(rel, path):
        seed[rel] = pack(Path(path).read_bytes())
    add('data/raw/trade_calendar.csv', HERE / 'vm_raw/data/raw/trade_calendar.csv')
    lists = {}
    for src in (REPO / 'data/raw/limit_list', HERE / 'vm_raw/data/raw/limit_list',
                HERE / 'vm_raw_latest_unz/data/raw/limit_list'):
        if src.exists():
            for f in src.glob('2026*.csv'):
                lists[f.name] = f
    for name in sorted(lists)[-60:]:
        add('data/raw/limit_list/' + name, lists[name])
    for f in ('fill_rate_table.csv', 'fill_rate_fallback.csv'):
        add('data/processed/' + f, REPO / 'data/processed' / f)
    # theme reference only when production has one (10-22: it has none, so theme features are
    # unavailable there; shipping an old copy would make the theme columns differ from production)
    theme = HERE / 'vm_raw_latest_unz/data/processed/stock_theme_reference.csv'
    if theme.exists():
        add('data/processed/stock_theme_reference.csv', theme)
    basic = HERE / 'vm_raw_latest_unz/data/raw/stock_basic/stock_basic_all.csv'
    add('data/raw/stock_basic/stock_basic_all.csv', basic if basic.exists() else HERE / 'vm_raw/data/raw/stock_basic/stock_basic_all.csv')
    state = HERE / 'vm_state/files'
    # every production state file the QMT-native host persists (same whitelist), so the new
    # system starts exactly where production stopped
    include = persist_patterns()
    for p in sorted(state.rglob('*')):
        rel = p.relative_to(state).as_posix()
        if not p.is_file() or 'sync-conflict' in rel or rel.startswith('logs/'):
            continue
        if any(rx.search(rel) for rx in include):
            data = p.read_bytes()
            if rel.endswith('.sqlite3'):
                import sqlite3
                con = sqlite3.connect(str(p))
                data = b'-- QN_SQLITE_DUMP v1\n' + '\n'.join(con.iterdump()).encode('utf-8')
            seed[rel] = pack(data)
    if EQ_SEED is not None:
        # step-13 seed taken from the production run (see build_eq_seed.py)
        for p in sorted((EQ_SEED / 'qn_state' / 'equity_stop').glob('*')):
            add('qn_state/equity_stop/' + p.name, p)
        add('reports/equity_curve_stop/shadow_nav_latest.csv', EQ_SEED / 'reports/equity_curve_stop/shadow_nav_latest.csv')
        decision = EQ_SEED / 'data/state/equity_curve_stop_decision.json'
        if decision.exists():
            add('data/state/equity_curve_stop_decision.json', decision)
    else:
        add('reports/equity_curve_stop/shadow_nav_latest.csv', REPO / 'reports/equity_curve_stop/shadow_nav_latest.csv')
    return seed


ENTRY = r'''
QN_HOST = [None]


def _qn_globals():
    return globals()


def init(ContextInfo):
    if QN_HOST[0] is not None:
        print('[A_SYSTEM] already initialised in this QMT session')
        return
    if bool(getattr(ContextInfo, 'do_back_test', False)):
        raise RuntimeError('A_System must not run in backtest mode')
    g = _qn_globals()
    deploy = QN_DEPLOY
    if deploy['mode'] == 'live':
        ContextInfo.set_account(deploy['account_id'])
        broker = None
    else:
        def _quote(code, _C=ContextInfo):
            try:
                tick = (_C.get_full_tick([code]) or {}).get(code)
                return dict(tick) if tick else None
            except Exception:
                return None
        broker = QnPaperBroker(deploy['account_id'], deploy['paper_cash'], _quote)
    # shadow state never leaks into live; a validation run may start from a fresh prefix
    prefix = deploy.get('state_prefix') or ('AS_QNL' if deploy['mode'] == 'live' else 'AS_QNS')
    store = None
    if callable(g.get('get_py_key')):
        import threading as _qn_th
        callback_thread = _qn_th.get_ident()

        def _on_callback_thread(name, *args):
            # QMT's key-value API only works on the strategy callback thread
            if _qn_th.get_ident() == callback_thread:
                return g[name](*args)
            return QN_NATIVE_SERVICE.call(lambda C, gg: gg[name](*args), _timeout=60.0)
        store = QnStateStore(lambda k: _on_callback_thread('get_py_key', k),
                             lambda k, v: _on_callback_thread('save_py_key', k, v), prefix)
    host = QnHost(ContextInfo, g, deploy, QN_MODULES, QN_STATIC_FILES, broker=broker, store=store)
    QN_HOST[0] = host
    try:
        host.boot()
    except BaseException:
        QN_HOST[0] = None
        raise
    ContextInfo.run_time('a_system_pump', '100nMilliSecond', '2020-01-01 00:00:00')
    print('[A_SYSTEM] started: mode=%s release=%s' % (deploy['mode'], deploy['release']))


def a_system_pump(ContextInfo):
    if QN_HOST[0] is not None:
        QN_HOST[0].pump()


def handlebar(ContextInfo):
    pass


def order_callback(ContextInfo, orderInfo):
    if QN_HOST[0] is not None and QN_DEPLOY['mode'] == 'live':
        QN_HOST[0].callback('order', orderInfo)


def deal_callback(ContextInfo, dealInfo):
    if QN_HOST[0] is not None and QN_DEPLOY['mode'] == 'live':
        QN_HOST[0].callback('deal', dealInfo)


def orderError_callback(ContextInfo, passOrderInfo, msg):
    if QN_HOST[0] is not None and QN_DEPLOY['mode'] == 'live':
        QN_HOST[0].callback('error', {'message': str(msg), 'request': str(passOrderInfo)})


def stop(ContextInfo):
    if QN_HOST[0] is not None:
        QN_HOST[0].stop()
        QN_HOST[0] = None
'''


def main():
    mode, account, cash, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), Path(sys.argv[4])
    assert mode in ('shadow', 'live')
    custom = os.environ.get('QN_STATE_PREFIX', '')
    # live always keeps its state under AS_QNL (a different prefix would lose it on the next build)
    if custom and (mode == 'live' or not custom.startswith('AS_QNS') or not custom.isalnum() and '_' not in custom):
        raise SystemExit('QN_STATE_PREFIX is for shadow validation runs only and must start with AS_QNS')
    parts = []
    for name in ('qn_runtime.py', 'qn_core_full.py', 'qn_paper_broker.py', 'qn_host.py'):
        parts.append('# ===== %s =====\n' % name + (INQMT / name).read_text(encoding='utf-8'))
    source = '\n'.join(parts)
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]
    release = '%s-%s' % (datetime.datetime.now().strftime('%Y%m%d%H%M'), digest)
    deploy = {'release': release, 'account_id': account, 'mode': mode, 'paper_cash': cash, 'seed': seed_files(),
              'state_prefix': os.environ.get('QN_STATE_PREFIX', ''),
              'debug_export_dir': r'C:\qn_shadow_out' if mode == 'shadow' else ''}
    blob = pack(source.encode('utf-8'))
    lines = ['# coding: gbk',
             '# A_System QMT-native strategy, release %s, mode %s.' % (release, mode),
             '# Generated by build_qmt_strategy.py. Paste the whole file into a QMT Python strategy.',
             '# No file access, no network: everything runs in memory inside QMT.',
             'import base64 as _qn_b64', 'import zlib as _qn_zlib',
             '_QN_BLOB = (']
    for i in range(0, len(blob), 4000):
        lines.append("    '%s'" % blob[i:i + 4000])
    lines.append(')')
    lines.append("exec(compile(_qn_zlib.decompress(_qn_b64.b64decode(_QN_BLOB)).decode('utf-8'), "
                 "'<A_SYSTEM_QMT_NATIVE>', 'exec'))")
    lines.append('del _QN_BLOB')
    lines.append('QN_DEPLOY = %s' % json.dumps({k: v for k, v in deploy.items() if k != 'seed'}, sort_keys=True))
    lines.append('QN_DEPLOY["seed"] = {')
    for rel, data in sorted(deploy['seed'].items()):
        lines.append('    %s: (' % json.dumps(rel))
        for i in range(0, len(data), 4000):
            lines.append("        '%s'" % data[i:i + 4000])
        lines.append('    ),')
    lines.append('}')
    text = '\n'.join(lines) + '\n' + ENTRY
    assert all(ord(c) < 128 for c in text)
    out.write_text(text, encoding='ascii')
    print('release', release, 'bytes', len(text), 'seed files', len(deploy['seed']), '->', out)


if __name__ == '__main__':
    main()
