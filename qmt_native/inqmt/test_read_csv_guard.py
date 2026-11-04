# coding: utf-8
"""QMT 2.1.33 guards the name pandas.read_csv: every call raises
PermissionError('calling pandas.read_csv not allowed'), even on an in-memory buffer.

Loads a packaged strategy file under that guard and parses every CSV shipped in the strategy
(the 120 seed files, from the in-memory VFS) through the bundle's pandas.read_csv; each result
must equal the pre-guard parse (pandas' own parser function on the same bytes, round_trip floats),
with the dtype options production uses. An old build must fail exactly like QMT showed it.

Usage: python test_read_csv_guard.py STRATEGY_FILE [expect_fail]
"""
import base64
import io
import os
import sys
import zlib

import pandas as pd
import pandas.io.parsers as parsers

ORIGINAL = parsers.read_csv


class _guard_funcwrap(object):
    def __init__(self, wrapped):
        self.__wrapped__ = wrapped

    def __call__(self, *a, **k):
        raise PermissionError('calling pandas.read_csv not allowed')


def main():
    path = sys.argv[1]
    expect_fail = len(sys.argv) > 2 and sys.argv[2] == 'expect_fail'
    pd.read_csv = _guard_funcwrap(pd.read_csv)
    ns = {'__name__': 'qn_guard_test', 'get_py_key': lambda k: ('', 0, ''), 'save_py_key': lambda k, v: (0, '')}
    exec(compile(open(path, encoding='gbk').read(), path, 'exec'), ns)
    vfs, qpd = ns['QN_VFS'], ns['qn_pandas']
    seed = dict((rel, zlib.decompress(base64.b64decode(packed))) for rel, packed in ns['QN_DEPLOY']['seed'].items())
    for rel, data in seed.items():
        vfs.put_restored(rel, data, 0.0)
    csvs = sorted(rel for rel in seed if rel.endswith('.csv'))
    variants = [{}, {'low_memory': False}, {'dtype': {'trade_date': str, 'ts_code': str}, 'low_memory': False},
                {'dtype': str}, {'usecols': None, 'nrows': 5}]
    checked = failed = blocked = 0
    first_error = ''
    for rel in csvs:
        data = seed[rel]
        for kw in variants:
            try:
                ref = ORIGINAL(io.BytesIO(data), float_precision='round_trip', **kw)
            except Exception as exc:  # e.g. an empty file: the bundle must raise the same error
                ref = exc
            try:
                got = qpd.read_csv(os.path.join('/A_System', rel) if not rel.startswith('/') else rel, **kw)
            except PermissionError as exc:
                blocked += 1
                first_error = first_error or str(exc)
                continue
            except Exception as exc:
                got = exc
            checked += 1
            if isinstance(ref, Exception) or isinstance(got, Exception):
                same = type(ref) is type(got) and str(ref) == str(got)
            else:
                same = (got.equals(ref) and list(got.columns) == list(ref.columns)
                        and [str(d) for d in got.dtypes] == [str(d) for d in ref.dtypes])
            if not same:
                failed += 1
                print('DIFF', rel, kw)
    # an explicit sep / delimiter / positional sep must still be honoured
    vfs.put_restored('tmp/semi.csv', b'a;b\n1;2\n', 0.0)
    extra = []
    for args, kw in (((), {'sep': ';'}), ((), {'delimiter': ';'}), ((';',), {})):
        try:
            extra.append(qpd.read_csv('/A_System/tmp/semi.csv', *args, **kw).columns.tolist() == ['a', 'b'])
        except PermissionError:
            extra.append(False)
    print('1. csv files %d, parses compared %d, differing %d, blocked by the guard %d' % (len(csvs), checked,
                                                                                        failed, blocked))
    print('2. explicit separators honoured: %s' % extra)
    if blocked:
        print('   guard message: %s' % first_error)
    if expect_fail:
        ok = blocked > 0 and checked == 0
    else:
        ok = blocked == 0 and failed == 0 and checked > 0 and all(extra)
    print('RESULT %s' % ('PASS' if ok else 'FAIL'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
