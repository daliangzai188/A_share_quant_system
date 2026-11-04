# coding: gbk
# A_System probe 4: which Python functions does QMT 2.1.33 block, and how ("calling pandas.read_csv not allowed")?
# Read-only: inspects objects and parses tiny in-memory strings. Opens no file, touches no network, places no order.
import sys
import io
import types

TXT = 'a,b,c\n1,2.5,x\n3,4.25,y\n'


def _log(msg):
    print('[QN_BLK] ' + msg)


def _resolve(path):
    parts = path.split('.')
    obj = __import__(parts[0])
    for p in parts[1:]:
        obj = getattr(obj, p)
    return obj


def _describe(path):
    try:
        obj = _resolve(path)
    except BaseException as e:
        return '%s -> missing (%s: %s)' % (path, type(e).__name__, e)
    info = [type(obj).__name__]
    for attr in ('__module__', '__qualname__'):
        info.append('%s=%s' % (attr, getattr(obj, attr, '-')))
    code = getattr(obj, '__code__', None)
    if code is not None:
        info.append('code=%s:%s name=%s free=%s' % (code.co_filename, code.co_firstlineno, code.co_name,
                                                   ','.join(code.co_freevars)))
        cl = getattr(obj, '__closure__', None) or ()
        vals = []
        for c in cl:
            try:
                v = c.cell_contents
                vals.append('%s:%s' % (type(v).__name__, getattr(v, '__qualname__', getattr(v, '__name__', ''))))
            except BaseException:
                vals.append('?')
        if vals:
            info.append('closure=' + '|'.join(vals))
    wrapped = getattr(obj, '__wrapped__', None)
    if wrapped is not None:
        info.append('wrapped=%s' % getattr(wrapped, '__qualname__', wrapped))
    return '%s -> %s' % (path, ' '.join(info))


def _try(label, fn):
    try:
        r = fn()
        if hasattr(r, 'shape'):
            r = 'shape=%s dtypes=%s' % (r.shape, ','.join(str(d) for d in getattr(r, 'dtypes', [])))
        _log('TRY %s -> OK %s' % (label, str(r)[:120].replace('\n', '|')))
    except BaseException as e:
        _log('TRY %s -> %s: %s' % (label, type(e).__name__, str(e)[:160]))


def init(ContextInfo):
    import pandas as pd
    import numpy as np
    _log('python %s' % sys.version.replace('\n', ' '))
    _log('pandas %s numpy %s' % (pd.__version__, np.__version__))
    _log('profile=%r trace=%r' % (sys.getprofile(), sys.gettrace()))
    import builtins
    _log('builtins.__import__=%r open=%r' % (builtins.__import__, builtins.open))
    _log('meta_path=%r' % (sys.meta_path,))
    _log('path_hooks=%r' % (sys.path_hooks,))
    for path in ('pandas.read_csv', 'pandas.io.parsers.read_csv', 'pandas.read_table', 'pandas.io.parsers.read_table',
                 'pandas.read_fwf', 'pandas.io.parsers._read', 'pandas.io.parsers.TextFileReader',
                 'pandas.io.parsers.CParserWrapper', 'pandas._libs.parsers.TextReader', 'pandas.read_json',
                 'pandas.read_pickle', 'pandas.read_excel', 'pandas.read_hdf', 'pandas.read_sql', 'pandas.read_clipboard',
                 'pandas.read_html', 'pandas.read_msgpack', 'pandas.read_sas', 'pandas.read_stata', 'pandas.read_parquet',
                 'pandas.read_feather', 'pandas.DataFrame.to_csv', 'pandas.Series.to_csv', 'pandas.DataFrame.to_json',
                 'pandas.DataFrame.to_pickle', 'pandas.DataFrame.to_excel', 'pandas.io.common.get_filepath_or_buffer',
                 'numpy.loadtxt', 'numpy.genfromtxt', 'numpy.load', 'numpy.save', 'numpy.fromfile', 'numpy.ndarray.tofile',
                 'io.open', 'io.FileIO', 'os.open', 'os.listdir', 'os.remove', 'csv.reader', 'json.loads', 'json.load', 'pickle.loads',
                 'pickle.load', 'winreg.OpenKey', 'zlib.decompress',
                 'base64.b64decode'):
        _log('OBJ ' + _describe(path))
    df = pd.DataFrame({'a': [1, 3], 'b': [2.5, 4.25], 'c': ['x', 'y']})
    _try('pd.read_csv(StringIO)', lambda: pd.read_csv(io.StringIO(TXT)))
    _try('pd.read_csv(BytesIO)', lambda: pd.read_csv(io.BytesIO(TXT.encode())))
    _try('pd.read_table(StringIO,sep)', lambda: pd.read_table(io.StringIO(TXT), sep=','))
    _try('parsers._read(StringIO)', lambda: pd.io.parsers._read(io.StringIO(TXT), {'sep': ','}))
    _try('TextFileReader(StringIO).read()', lambda: pd.io.parsers.TextFileReader(io.StringIO(TXT), sep=',').read())
    _try('TextFileReader(engine=python)', lambda: pd.io.parsers.TextFileReader(io.StringIO(TXT), sep=',',
                                                                                 engine='python').read())
    _try('_libs TextReader(StringIO)', lambda: pd._libs.parsers.TextReader(io.StringIO(TXT), delimiter=',').read())
    _try('df.to_csv(None)', lambda: df.to_csv(None))
    _try('df.to_csv(StringIO)', lambda: (lambda b: (df.to_csv(b), b.getvalue())[1])(io.StringIO()))
    _try('df.to_json()', lambda: df.to_json())
    _try('pd.read_json(str)', lambda: pd.read_json(df.to_json()))
    _try('np.loadtxt(StringIO)', lambda: np.loadtxt(io.StringIO('1 2\n3 4\n')))
    _try('np.genfromtxt(StringIO)', lambda: np.genfromtxt(io.StringIO('1,2\n3,4\n'), delimiter=','))
    import csv
    _try('csv.reader(StringIO)', lambda: list(csv.reader(io.StringIO(TXT))))
    import json
    _try('json.loads', lambda: json.loads('{"a": 1}'))
    import pickle
    _try('pickle roundtrip', lambda: pickle.loads(pickle.dumps(df)))
    _try('df.to_pickle-less copy', lambda: df.copy())
    _log('done')


def handlebar(ContextInfo):
    pass
