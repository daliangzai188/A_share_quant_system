# coding: utf-8
"""QMT keeps one key-value store per trade server and switches servers on failures; nothing is
replicated (11-05: five servers; a switch at 08:47 left the next snapshots referencing bodies that
existed only on the first server, and a start at 16:42 read that server's 08:44 snapshot).
  1. save on server A, then QMT switches to an empty server B: the next save notices the change and
     writes the whole snapshot to B; B's snapshot loads completely, with the newest content;
  2. back to A (still holding its older snapshot): the next save reuses the bodies A holds (checked
     by a full read) and writes only the changed ones; A's snapshot loads with the newest content;
  3. another save on the same server: no change noticed, only the 4 changed bodies written;
  4. B's snapshot stays loadable (older but complete).

Usage: python test_store_switch.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_stop_threads import load


def main():
    ns = load(sys.argv[1], {})
    Store = ns['QnStateStore']
    servers = {'A': {}, 'B': {}}
    cur = ['A']
    writes = []

    def get(k):
        kv = servers[cur[0]]
        return (kv[k], 0, '') if k in kv else ('', -1, 'KeyError: not exist')

    def put(k, v):
        if v and '_F_' in k:
            writes.append(k)
        servers[cur[0]][k] = v
        return (0, '')

    def files(version, changed):
        out = {}
        for i in range(12):
            text = ('file %d version %d ' % (i, version if i in changed else 0)) * 400
            out['data/state/f%02d.json' % i] = (text.encode(), float(i), len(text))
        return out

    def fresh_load(server):
        cur[0], keep = server, cur[0]
        try:
            st = Store(get, put, 'AS_QNL')
            loaded, meta = st.load()
            return loaded
        except Exception as exc:
            return 'FAILED %s' % str(exc)[:80]
        finally:
            cur[0] = keep

    store = Store(get, put, 'AS_QNL')
    v1 = files(1, (0,))
    store.save(v1, {})
    ok = True
    # 1. switch to an empty server
    cur[0] = 'B'
    v2 = files(2, (0, 1))
    note = store.follow_server() if hasattr(store, 'follow_server') else ''  # builds before 11-05 evening
    del writes[:]
    store.save(v2, {})
    b_writes = len(writes)
    got = fresh_load('B')
    ok1 = bool(note) and not isinstance(got, str) and all(got[k][0] == v2[k][0] for k in v2) and b_writes >= 12
    print('1. switch to empty B: noticed %s, body writes %d, B loads newest %s' % (
        bool(note), b_writes, ok1 if not isinstance(got, str) else got))
    # 2. back to A, which holds the v1 snapshot
    cur[0] = 'A'
    v3 = files(3, (0, 1, 2))
    note = store.follow_server() if hasattr(store, 'follow_server') else ''  # builds before 11-05 evening
    del writes[:]
    store.save(v3, {})
    a_writes = len(writes)
    got = fresh_load('A')
    ok2 = bool(note) and not isinstance(got, str) and all(got[k][0] == v3[k][0] for k in v3) and a_writes == 3
    print('2. back to A: noticed %s, body writes %d (3 changed files, chunks %s), A loads newest %s' % (
        bool(note), a_writes, sorted(set(w.split('_')[-1] for w in writes)), ok2 if not isinstance(got, str) else got))
    # 3. same server again
    v4 = files(4, (0, 1, 2, 3))
    note = store.follow_server() if hasattr(store, 'follow_server') else ''  # builds before 11-05 evening
    del writes[:]
    store.save(v4, {})
    ok3 = note == '' and len(writes) == 4 and not isinstance(fresh_load('A'), str)
    print('3. same server: noticed %r, body writes %d' % (note, len(writes)))
    # 4. B still complete
    got = fresh_load('B')
    ok4 = not isinstance(got, str) and all(got[k][0] == v2[k][0] for k in v2)
    print('4. B still loads its own (older) snapshot completely: %s' % (ok4 if not isinstance(got, str) else got))
    ok = ok1 and ok2 and ok3 and ok4
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
