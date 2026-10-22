# coding: utf-8
"""QnStateStore under broker-server failures (QMT's key-value store goes through the trade
server, which drops connections):
  A. a clean-up interrupted half way, then a file returns to the cleared content: still readable;
  B. latest snapshot unreadable: load falls back to the snapshot before it;
  C. pointer write not confirmed: that snapshot's bodies are never cleared.

Usage: python test_state_store.py HOST_PY   (default qn_host.py)
"""
import os, random, sys
HERE = os.path.dirname(os.path.abspath(__file__))


class KV(object):
    def __init__(self):
        self.data, self.fail_at, self.calls = {}, None, 0
        self.fail_clear, self.clears = None, 0

    def get(self, key):
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
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
