# coding: utf-8
"""QnStateStore under broker-server failures (QMT's key-value store goes through the trade
server, which drops connections):
  A. a clean-up interrupted half way, then a file returns to the cleared content: still readable;
  B. latest snapshot unreadable: load falls back to the snapshot before it;
  C. pointer write not confirmed: that snapshot's bodies are never cleared;
  D. one call slower than the limit gives the save up (QMT is frozen during such a call);
  E. both pointers broken: the manifest slot that is complete on its own is used;
  F. an empty pointer read while a state exists is retried, never a start from the seed;
  G. an empty store (read three times) starts fresh, a pointer that reads empty once is still found;
     a store with nothing complete is refused;
  H. a damaged body of the committed snapshot is found by the read-back check and rewritten;
  I. a lost body whose content is shipped in the strategy file is taken from there and rewritten;
  J. a lost body of an operator-named file stands in from its seed version; others still refuse.

Usage: python test_state_store.py HOST_PY   (default qn_host.py)
"""
import os, random, sys
HERE = os.path.dirname(os.path.abspath(__file__))


class KV(object):
    def __init__(self):
        self.data, self.fail_at, self.calls = {}, None, 0
        self.fail_clear, self.clears = None, 0

    def get(self, key):
        self.reads = getattr(self, 'reads', 0) + 1
        return (self.data[key], 0, '') if key in self.data else ('', -1, 'KeyError: not exist')

    def save(self, key, value):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            return (200000, u'未建立连接')
        if value == '':
            self.clears += 1
            if self.fail_clear is not None and self.clears == self.fail_clear:
                return (200000, u'未建立连接')
        self.data[key] = value
        return (0, '')


def blob(seed, size=9000):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(size))


def main():
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', sys.argv[1] if len(sys.argv) > 1 else 'qn_host.py'):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    Store = ns['QnStateStore']
    Store.EMPTY_CONFIRM_WAIT = 0.0
    A, B = blob(1), blob(2)
    ok = True

    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    st.save({'f': (B, 2.0, len(B))}, {})          # A is still the previous snapshot: kept
    kv.clears, kv.fail_clear = 0, 2                 # next clean-up (A) fails after its first chunk
    try:
        st.save({'f': (B, 3.0, len(B)), 'g': (blob(3), 3.0, 9000)}, {})
    except Exception:
        pass  # the host logs STATE SAVE FAILED and carries on
    kv.fail_clear = None
    try:
        st.save({'f': (A, 4.0, len(A))}, {})      # A again
        files, meta = Store(kv.get, kv.save, 'T').load()
        a_ok = files['f'][0] == A and not meta.get('restored_from')
    except Exception as exc:
        a_ok = False
        print('   A error:', exc)
    print('A. interrupted clean-up, content returns: readable', a_ok)
    ok &= a_ok

    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    st.save({'f': (B, 2.0, len(B))}, {})
    for key in list(kv.data):
        if key.startswith('T_F_') and kv.data[key] and key.endswith('_1'):
            body_b = [k for k in kv.data if k.startswith('T_F_')]
    sha_b = Store(kv.get, kv.save, 'T')._put_body.__self__ if False else None
    victim = [k for k in kv.data if k.startswith('T_F_') and k.endswith('_1')]
    # corrupt every body that only the latest snapshot uses (B's)
    import hashlib
    b_key = 'T_F_%s_1' % hashlib.sha256(B).hexdigest()[:32]
    kv.data[b_key] = kv.data[b_key][:-50]
    try:
        files, meta = Store(kv.get, kv.save, 'T').load()
        b_ok = files['f'][0] == A and 'previous snapshot' in meta.get('restored_from', '')
        print('B. latest unreadable -> previous snapshot:', b_ok, '|', meta.get('restored_from', '')[:70])
    except Exception as exc:
        b_ok = False
        print('B. error:', exc)
    ok &= b_ok

    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    real_get = kv.get
    kv.get = lambda key: ('x', 0, '') if key == 'T_PTR' else real_get(key)  # pointer read-back fails
    st.getter = kv.get
    try:
        st.save({'f': (B, 2.0, len(B))}, {})
    except Exception:
        pass
    kv.get = real_get
    st.getter = real_get
    st.save({'f': (blob(4), 3.0, 9000)}, {})
    b_key = 'T_F_%s_0' % hashlib.sha256(B).hexdigest()[:32]
    c_ok = bool(kv.data.get(b_key))
    print('C. unconfirmed pointer: its bodies kept:', c_ok)
    ok &= c_ok

    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.slow_limit = 5.0
    st.save({'f': (A, 1.0, len(A))}, {})

    def slow_pointer(key, value):
        result = kv.save(key, value)
        if key == 'T_PTR':
            st.last_call_seconds = 12.0  # written, but the call hung: QMT was frozen meanwhile
        return result
    st.saver = slow_pointer
    try:
        st.save({'f': (B, 2.0, len(B))}, {})
        given_up = False
    except RuntimeError as exc:
        given_up = 'given up' in str(exc)
    st.saver = kv.save
    C4 = blob(4)
    st.save({'f': (C4, 3.0, len(C4))}, {})
    reader = Store(lambda k: kv.get(k), kv.save, 'T')
    reader.slow_limit = 5.0
    reader.last_call_seconds = 30.0  # slow reads at start-up are waited for, not given up
    files, meta = reader.load()
    b_kept = bool(kv.data.get('T_F_%s_0' % hashlib.sha256(B).hexdigest()[:32]))
    d_ok = given_up and files['f'][0] == C4 and b_kept
    print('D. slow call gives the save up; next save and load fine; its bodies kept:', d_ok)
    ok &= d_ok

    # E. both pointers name broken manifests (a late write landed on one slot, a half-written save
    #    on the other's pointer); the slot still holding a complete manifest is used.
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})            # slot A
    st.save({'f': (B, 2.0, len(B))}, {})            # slot B, PTR -> B, PTRPREV -> A
    ptr_b = kv.data['T_PTR']
    kv.data['T_PTRPREV'] = ptr_b                    # PTRPREV now names B as well
    kv.data['T_MB_0'] = kv.data['T_MB_0'][:-40] + 'x' * 40   # B's manifest broken
    try:
        files, meta = Store(kv.get, kv.save, 'T').load()
        e_ok = files['f'][0] == A and 'manifest slot A' in meta.get('restored_from', '')
        print('E. both pointers broken -> complete slot used:', e_ok, '|', meta.get('restored_from', '')[:80])
    except Exception as exc:
        e_ok = False
        print('E. error:', exc)
    ok &= e_ok

    # F. pointer reads empty while a saved state exists (flaky server): not a fresh start
    real_get = kv.get
    kv.get = lambda key: ('', -1, 'KeyError: not exist') if key == 'T_PTR' else real_get(key)
    try:
        Store(kv.get, kv.save, 'T').load()
        f_ok = False
    except RuntimeError as exc:
        f_ok = 'not starting from the seed' in str(exc)
    kv.get = real_get
    print('F. empty pointer but state present -> retried, not a seed start:', f_ok)
    ok &= f_ok

    # G. nothing stored at all (read three times), and nothing complete anywhere; a pointer that
    #    reads empty once and then appears is used
    empty = KV()
    files, meta = Store(empty.get, empty.save, 'T').load()
    g_ok = files is None and meta is None and empty.reads >= 6
    blink = KV()
    Store(blink.get, blink.save, 'T').save({'f': (A, 1.0, len(A))}, {})
    first = {'n': 0}
    real_blink = blink.get

    def blinking(key):
        if key in ('T_PTR', 'T_MA_0', 'T_MB_0', 'T_PTRPREV') and first['n'] < 4:
            first['n'] += 1
            return ('', -1, 'KeyError: not exist')
        return real_blink(key)
    files, meta = Store(blinking, blink.save, 'T').load()
    g_ok &= files is not None and files['f'][0] == A
    kv2 = KV()
    st2 = Store(kv2.get, kv2.save, 'T')
    st2.save({'f': (A, 1.0, len(A))}, {})
    for key in list(kv2.data):
        if key.startswith('T_M'):
            kv2.data[key] = kv2.data[key][:-10]
    try:
        Store(kv2.get, kv2.save, 'T').load()
        g_ok = False
    except RuntimeError as exc:
        g_ok &= 'no complete state snapshot' in str(exc)
    print('G. empty store -> fresh; nothing complete -> refused:', g_ok)
    ok &= g_ok

    # H. a body chunk of the committed snapshot is damaged after its save (10-23: a chunk no longer
    #    decoded): the read-back check finds it, the next save writes every body again.
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A)), 'g': (B, 1.0, len(B))}, {})
    clean = st.verify()
    b_key = 'T_F_%s_1' % hashlib.sha256(B).hexdigest()[:32]
    kv.data[b_key] = kv.data[b_key][:-30]
    problem = st.verify()
    st.forget_bodies()
    st.save({'f': (A, 2.0, len(A)), 'g': (B, 2.0, len(B))}, {})
    files, meta = Store(kv.get, kv.save, 'T').load()
    h_ok = clean == '' and bool(problem) and st.verify() == '' and files['g'][0] == B
    print('H. damaged body found by the read-back check and rewritten by the next save:', h_ok, '|', problem[:60])
    ok &= h_ok

    # I. a body of both snapshots reads back empty but its content is shipped in the strategy file
    #    (10-23: equity_ledger.json): taken from there, and written again by the next save
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A)), 'g': (B, 1.0, len(B))}, {})
    st.save({'f': (A, 2.0, len(A)), 'g': (B, 2.0, len(B))}, {})
    b_key = 'T_F_%s_0' % hashlib.sha256(B).hexdigest()[:32]
    kv.data[b_key] = ''
    reader = Store(kv.get, kv.save, 'T')
    try:
        reader.load()
        no_seed = False
    except Exception:
        no_seed = True
    reader = Store(kv.get, kv.save, 'T')
    reader.fallback_bodies = lambda sha, rel: {hashlib.sha256(B).hexdigest()[:32]: B}.get(sha)
    files, meta = reader.load()
    repaired = list(reader.repaired)
    reader.save({'f': (A, 3.0, len(A)), 'g': (B, 3.0, len(B))}, {})
    i_ok = no_seed and files['g'][0] == B and repaired == ['g'] and bool(kv.data.get(b_key))
    files, meta = Store(kv.get, kv.save, 'T').load()
    i_ok &= files['g'][0] == B
    print('I. lost body with seed content restored from the seed and written again:', i_ok)
    ok &= i_ok

    # J. a lost body of a file an operator named stands in from the seed version (other content);
    #    an unnamed file with no seed copy still refuses the start
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A)), 'g': (B, 1.0, len(B))}, {})
    st.save({'f': (A, 2.0, len(A)), 'g': (B, 2.0, len(B))}, {})
    kv.data['T_F_%s_0' % hashlib.sha256(B).hexdigest()[:32]] = ''
    old_seed = b'seed version of g'
    reader = Store(kv.get, kv.save, 'T')
    reader.fallback_bodies = lambda sha, rel: old_seed if rel == 'g' else None
    files, meta = reader.load()
    j_ok = files['g'][0] == old_seed and files['g'][2] == len(old_seed) and files['f'][0] == A
    reader = Store(kv.get, kv.save, 'T')
    reader.fallback_bodies = lambda sha, rel: None
    try:
        reader.load()
        j_ok = False
    except RuntimeError:
        pass
    print('J. operator-named file stands in from its seed version; unnamed loss still refused:', j_ok)
    ok &= j_ok
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
