# coding: utf-8
"""Boot, run, stop, wipe memory, boot again from the persisted state: the equity-stop
producer identity and the persistent files must be identical across the restart."""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs

def identity(ns):
    loader = ns['QnLoader'](ns['QnCodeBase'](ns['QN_MODULES']))
    pmf = loader.load('src.post_market_freshness')
    import json
    cfg = json.loads(ns['QN_VFS'].get_text('config/config.json'))
    return pmf.producer_identity(ns['QnPath']('/A_System'), '20261019', cfg)

def raw_inventory(ns):
    v = ns['QN_VFS']; out = {}
    for a in v.walk_files():
        rel = a.split('/A_System/', 1)[-1]
        if rel.startswith('data/raw/'):
            st = v.stat(a); out[rel] = (st.st_size, st.st_mtime)
    return out

def main():
    core, seconds = sys.argv[1], float(sys.argv[2])
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', core, 'qn_paper_broker.py', 'qn_host.py'):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    kv = {}
    C = rs.MockContext(); g = rs.mock_globals()
    g['get_py_key'] = lambda key: (kv.get(key, ''), 0, '')
    g['save_py_key'] = lambda key, value: kv.__setitem__(key, value)
    deploy = {'release': 'restart-test', 'account_id': '8880000001', 'mode': 'shadow', 'seed': rs.seed_files(),
              'store_day_hours': [0, 24], 'cache_keeper': False}
    def boot():
        broker = ns['QnPaperBroker']('8880000001', 1000000.0, C.quote)
        store = ns['QnStateStore'](g['get_py_key'], g['save_py_key'])
        host = ns['QnHost'](C, g, deploy, ns['QN_MODULES'], ns['QN_STATIC_FILES'], market=C, broker=broker, store=store)
        host.boot(); return host
    host = boot(); t0 = time.time()
    while time.time() - t0 < seconds:
        host.pump(); time.sleep(0.1)
    before = identity(ns)
    raw_before = raw_inventory(ns)
    files_before = dict(host._persist_files())
    host.stop(); time.sleep(1)
    v = ns['QN_VFS']; v.files.clear(); v.stubs.clear(); v.mtimes.clear(); v.dirs = set(['/']); v.makedirs(v.cwd)
    ns['QN_SQLITE_DATABASES'].clear()
    host2 = boot()
    # Raw tables are not persisted: the collector rebuilds them (same size, deterministic
    # mtimes). The identity must match again once that catch-up has run.
    t1 = time.time()
    while time.time() - t1 < 400:
        cur = raw_inventory(ns)
        if all(cur.get(k) == v for k, v in raw_before.items()):
            break
        host2.pump(); time.sleep(0.1)
    rebuilt = raw_inventory(ns)
    missing = [k for k, v in raw_before.items() if rebuilt.get(k) != v]
    print('raw files rebuilt identically after %.0fs: %d of %d (different: %s)' % (
        time.time() - t1, len(raw_before) - len(missing), len(raw_before), missing[:5]))
    after = identity(ns)
    files_after = dict(host2._persist_files())
    print('identity equal across restart:', before == after)
    diff = [k for k in set(files_before) | set(files_after) if files_before.get(k, (None,))[0:2] != files_after.get(k, (None,))[0:2]]
    print('persistent files', len(files_before), len(files_after), 'differing', len(diff), sorted(diff)[:15])
    sizes = sorted(len(v) for v in kv.values())
    print('kv keys', len(kv), 'total chars', sum(sizes), 'largest', sizes[-3:])
    host2.stop()

if __name__ == '__main__':
    main()
