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
  J. a lost body of an operator-named file stands in from its seed version; others still refuse;
  K. a cleared key is never written again: content that comes back gets a new body id, and a
     clear landing late on the old key leaves the snapshot intact; each save has its own manifest.

Usage: python test_state_store.py HOST_PY   (default qn_host.py)
"""
import base64, hashlib, json, os, random, sys, zlib
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


def manifest(kv, pointer_key='T_PTR'):
    mid, n, digest = kv.data[pointer_key].split(':')
    packed = ''.join(kv.data['T_M%s_%d' % (mid, i)] for i in range(int(n)))
    return json.loads(zlib.decompress(base64.b64decode(packed)).decode('utf-8'))


def body_keys(kv, rel, pointer_key='T_PTR'):
    bid, mtime, size, n = manifest(kv, pointer_key)['files'][rel]
    return ['T_F_%s_%d' % (bid, i) for i in range(n)]


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
    key = body_keys(kv, 'f')[1]                     # a chunk only the latest snapshot uses
    kv.data[key] = kv.data[key][:-50]
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
    unconfirmed = body_keys(kv, 'f')                # the pointer did get written
    st.save({'f': (blob(4), 3.0, 9000)}, {})
    st.save({'f': (blob(5), 4.0, 9000)}, {})
    c_ok = all(kv.data.get(k) for k in unconfirmed)
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
    slow_keys = body_keys(kv, 'f')
    C4 = blob(4)
    st.save({'f': (C4, 3.0, len(C4))}, {})
    reader = Store(lambda k: kv.get(k), kv.save, 'T')
    reader.slow_limit = 5.0
    reader.last_call_seconds = 30.0  # slow reads at start-up are waited for, not given up
    files, meta = reader.load()
    d_ok = given_up and files['f'][0] == C4 and all(kv.data.get(k) for k in slow_keys)
    print('D. slow call gives the save up; next save and load fine; its bodies kept:', d_ok)
    ok &= d_ok

    # E. both pointers name broken manifests; a manifest slot of an older release (A/B) that is
    #    complete on its own is used
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    mid, n, _ = kv.data['T_PTR'].split(':')
    for i in range(int(n)):
        kv.data['T_MA_%d' % i] = kv.data['T_M%s_%d' % (mid, i)]   # as a release before 10-23 stored it
    st.save({'f': (B, 2.0, len(B))}, {})
    kv.data['T_PTRPREV'] = kv.data['T_PTR']
    mid, n, _ = kv.data['T_PTR'].split(':')
    kv.data['T_M%s_0' % mid] = kv.data['T_M%s_0' % mid][:-40] + 'x' * 40
    try:
        files, meta = Store(kv.get, kv.save, 'T').load()
        e_ok = files['f'][0] == A and 'manifest slot A' in meta.get('restored_from', '')
        print('E. both pointers broken -> complete slot used:', e_ok, '|', meta.get('restored_from', '')[:60])
    except Exception as exc:
        e_ok = False
        print('E. error:', exc)
    ok &= e_ok

    # F. pointer reads empty while a saved state exists (flaky server): not a fresh start
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    st.save({'f': (B, 2.0, len(B))}, {})
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

    # H. a body chunk of the committed snapshot is damaged after its save: the read-back check
    #    finds it, the next save writes every body again (under new keys)
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A)), 'g': (B, 1.0, len(B))}, {})
    clean = st.verify()
    key = body_keys(kv, 'g')[1]
    kv.data[key] = kv.data[key][:-30]
    problem = st.verify()
    st.forget_bodies()
    st.save({'f': (A, 2.0, len(A)), 'g': (B, 2.0, len(B))}, {})
    files, meta = Store(kv.get, kv.save, 'T').load()
    h_ok = clean == '' and bool(problem) and st.verify() == '' and files['g'][0] == B and body_keys(kv, 'g')[1] != key
    print('H. damaged body found by the read-back check and rewritten by the next save:', h_ok, '|', problem[:50])
    ok &= h_ok

    # I. a body of both snapshots reads back empty but its content is shipped in the strategy file:
    #    taken from there, and written again by the next save
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A)), 'g': (B, 1.0, len(B))}, {})
    st.save({'f': (A, 2.0, len(A)), 'g': (B, 2.0, len(B))}, {})
    lost = body_keys(kv, 'g')[0]
    kv.data[lost] = ''
    try:
        Store(kv.get, kv.save, 'T').load()
        no_seed = False
    except Exception:
        no_seed = True
    reader = Store(kv.get, kv.save, 'T')
    reader.fallback_bodies = lambda sha, rel: {hashlib.sha256(B).hexdigest()[:32]: B}.get(sha)
    files, meta = reader.load()
    repaired = list(reader.repaired)
    reader.save({'f': (A, 3.0, len(A)), 'g': (B, 3.0, len(B))}, {})
    i_ok = no_seed and files['g'][0] == B and repaired == ['g'] and body_keys(kv, 'g')[0] != lost
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
    kv.data[body_keys(kv, 'g')[0]] = ''
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

    # K. content A -> B -> C (A's body cleared) -> A again: A gets a new body id; a clear that
    #    lands late on A's old keys leaves the snapshot intact; manifests: one id per save, the
    #    one before the previous snapshot is cleared
    kv = KV()
    st = Store(kv.get, kv.save, 'T')
    st.save({'f': (A, 1.0, len(A))}, {})
    old_a = body_keys(kv, 'f')
    first_mid = kv.data['T_PTR'].split(':')[0]
    st.save({'f': (B, 2.0, len(B))}, {})
    st.save({'f': (blob(3), 3.0, 9000)}, {})       # A's body is cleared here
    cleared = all(kv.data.get(k) == '' for k in old_a)
    st.save({'f': (A, 4.0, len(A))}, {})
    new_a = body_keys(kv, 'f')
    for k in old_a:
        kv.data[k] = ''                             # the late clear
    files, meta = Store(kv.get, kv.save, 'T').load()
    mids = set(k.split('_')[1] for k in kv.data if k.startswith('T_M') and kv.data[k])
    k_ok = cleared and set(new_a).isdisjoint(old_a) and files['f'][0] == A
    k_ok &= kv.data.get('T_M%s_0' % first_mid) == '' and len(mids) == 2
    print('K. cleared keys never written again; late clear harmless; old manifests cleared:', k_ok, '| live manifests', len(mids))
    ok &= k_ok
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
