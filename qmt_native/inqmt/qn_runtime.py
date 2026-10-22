# coding: utf-8
# Runtime for the embedded A_System modules: in-memory file store, 3.6 shims and the
# close-pipeline step runner. Python 3.6 / pandas 0.22 compatible. No disk or network
# access: every path the strategy code touches is resolved inside QN_VFS.
import builtins as _builtins
import datetime as _dt
import fnmatch as _fnmatch
import io as _io
import logging as _logging
import posixpath as _pp
import sys as _sys
import time as _time
import types as _types
from pathlib import PurePosixPath as _PurePosixPath

import numpy as _np
import pandas as _pd

QN_ROOT = '/A_System'


def _fspath(p):
    if isinstance(p, str):
        return p
    if hasattr(p, '__fspath__'):
        return p.__fspath__()
    if isinstance(p, bytes):
        return p.decode('utf-8')
    raise TypeError('expected str or path, got %r' % type(p))


class _Stat(object):
    def __init__(self, size, mtime, is_dir):
        self.st_size = size
        self.st_mtime = mtime
        self.st_mtime_ns = int(round(mtime * 1e9))
        self.st_mode = 0o040755 if is_dir else 0o100644


def qn_content_sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()[:32]


class QnVFS(object):
    """Flat in-memory file system. Paths are POSIX, absolute under QN_ROOT."""

    def __init__(self, cwd=QN_ROOT):
        self.files = {}
        self.mtimes = {}
        self.stubs = {}  # path -> size: content intentionally not kept (restored inventory only)
        self.dirs = set(['/'])
        self.cwd = cwd
        self.makedirs(cwd)
        self.clock = 1.0e9
        # path -> (mtime, size, sha) of files the previous run had but whose content is not
        # persisted (rebuilt after a restart). A rebuilt file with the same content keeps its
        # old mtime, like an untouched file on disk would.
        self.prev_inventory = {}

    def abspath(self, p):
        s = _fspath(p).replace('\\', '/')
        if not s.startswith('/'):
            s = self.cwd + '/' + s
        return _pp.normpath(s)

    def _tick(self):
        self.clock += 1.0
        return self.clock

    def makedirs(self, p, exist_ok=True):
        a = self.abspath(p)
        if a in self.files:
            raise FileExistsError(a)
        if a in self.dirs and not exist_ok:
            raise FileExistsError(a)
        while a not in self.dirs:
            self.dirs.add(a)
            a = _pp.dirname(a)

    def mkdir(self, p, parents=False, exist_ok=False):
        a = self.abspath(p)
        if a in self.dirs or a in self.files:
            if exist_ok and a in self.dirs:
                return
            raise FileExistsError(a)
        if not parents and _pp.dirname(a) not in self.dirs:
            raise FileNotFoundError(a)
        self.makedirs(a)

    def exists(self, p):
        a = self.abspath(p)
        return a in self.files or a in self.dirs or a in self.stubs

    def isfile(self, p):
        a = self.abspath(p)
        return a in self.files or a in self.stubs

    def put_stub(self, rel, size, mtime):
        a = self.abspath(rel)
        self.makedirs(_pp.dirname(a))
        self.files.pop(a, None)
        self.stubs[a] = int(size)
        self.mtimes[a] = float(mtime)
        self.clock = max(self.clock, float(mtime))

    def isdir(self, p):
        return self.abspath(p) in self.dirs

    def read(self, p):
        a = self.abspath(p)
        if a in self.stubs:
            raise OSError('QN_STUB: content of %s is not kept in QMT state (inventory only)' % a)
        if a not in self.files:
            if a in self.dirs:
                raise IsADirectoryError(a)
            raise FileNotFoundError(2, 'No such file or directory', a)
        return self.files[a]

    def write(self, p, data):
        a = self.abspath(p)
        if a in self.dirs:
            raise IsADirectoryError(a)
        parent = _pp.dirname(a)
        if parent not in self.dirs:
            raise FileNotFoundError(2, 'No such file or directory', a)
        if not isinstance(data, bytes):
            raise TypeError('vfs stores bytes')
        first = a not in self.files
        self.files[a] = data
        self.stubs.pop(a, None)
        prev = self.prev_inventory.get(a) if first else None
        if prev is not None and prev[1] == len(data) and prev[2] == qn_content_sha(data):
            self.mtimes[a] = float(prev[0])
            self.clock = max(self.clock, float(prev[0]))
        else:
            self.mtimes[a] = self._tick()

    def remove(self, p):
        a = self.abspath(p)
        if a in self.stubs:
            del self.stubs[a]
            self.mtimes.pop(a, None)
            return
        if a not in self.files:
            raise FileNotFoundError(2, 'No such file or directory', a)
        del self.files[a]
        self.mtimes.pop(a, None)

    def rmtree(self, p):
        a = self.abspath(p)
        for f in [f for f in self.files if f == a or f.startswith(a + '/')]:
            del self.files[f]
        for d in [d for d in self.dirs if d == a or d.startswith(a + '/')]:
            self.dirs.discard(d)

    def rename(self, src, dst):
        a, b = self.abspath(src), self.abspath(dst)
        if a in self.files:
            if b in self.dirs:
                raise IsADirectoryError(b)
            self.write(b, self.files.pop(a))
            self.mtimes.pop(a, None)
            return
        if a in self.dirs:
            for f in [f for f in self.files if f.startswith(a + '/')]:
                self.files[b + f[len(a):]] = self.files.pop(f)
            for d in [d for d in self.dirs if d == a or d.startswith(a + '/')]:
                self.dirs.discard(d)
                self.dirs.add(b + d[len(a):])
            return
        raise FileNotFoundError(2, 'No such file or directory', a)

    def listdir(self, p='.'):
        a = self.abspath(p)
        if a not in self.dirs:
            raise FileNotFoundError(2, 'No such file or directory', a)
        prefix = a.rstrip('/') + '/'
        names = set()
        for f in list(self.files) + list(self.dirs) + list(self.stubs):
            if f.startswith(prefix) and f != a:
                names.add(f[len(prefix):].split('/')[0])
        return sorted(names)

    def stat(self, p):
        a = self.abspath(p)
        if a in self.stubs:
            return _Stat(self.stubs[a], self.mtimes.get(a, 0.0), False)
        if a in self.files:
            return _Stat(len(self.files[a]), self.mtimes.get(a, 0.0), False)
        if a in self.dirs:
            return _Stat(0, 0.0, True)
        raise FileNotFoundError(2, 'No such file or directory', a)

    # convenience for the host strategy
    def put_text(self, rel, text, encoding='utf-8'):
        a = self.abspath(rel)
        self.makedirs(_pp.dirname(a))
        self.write(a, text.encode(encoding))

    def put_bytes(self, rel, data):
        a = self.abspath(rel)
        self.makedirs(_pp.dirname(a))
        self.write(a, data)

    def put_restored(self, rel, data, mtime):
        a = self.abspath(rel)
        self.makedirs(_pp.dirname(a))
        self.stubs.pop(a, None)
        self.files[a] = data
        self.mtimes[a] = float(mtime)
        self.clock = max(self.clock, float(mtime))

    def get_text(self, rel, encoding='utf-8-sig'):
        return self.read(rel).decode(encoding)

    def walk_files(self, rel=''):
        a = self.abspath(rel) if rel else self.cwd
        prefix = a.rstrip('/') + '/'
        return sorted(f[len(prefix):] for f in self.files if f.startswith(prefix))


QN_VFS = QnVFS()


# ---------------------------------------------------------------- open()
class _CommitBytes(_io.BytesIO):
    def __init__(self, vfs, path, initial=b''):
        _io.BytesIO.__init__(self, initial)
        if initial:
            self.seek(0, 2)
        self._qn_vfs = vfs
        self._qn_path = path

    def flush(self):
        if not self.closed:
            self._qn_vfs.write(self._qn_path, self.getvalue())
        _io.BytesIO.flush(self)

    def close(self):
        if not self.closed:
            self._qn_vfs.write(self._qn_path, self.getvalue())
        _io.BytesIO.close(self)

    def fileno(self):
        return -1  # lock helpers only; locking is a no-op in the single-process store


def qn_open(file, mode='r', buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    if isinstance(file, int):
        raise OSError('file descriptors are not available in QMT native mode')
    vfs = QN_VFS
    path = vfs.abspath(file)
    binary = 'b' in mode
    if '+' in mode:
        exists = vfs.isfile(path)
        if 'r' in mode and not exists:
            raise FileNotFoundError(2, 'No such file or directory', path)
        initial = vfs.read(path) if exists and 'w' not in mode else b''
        vfs.write(path, initial)
        raw = _CommitBytes(vfs, path, initial)
        if 'r' in mode:
            raw.seek(0)
        if binary:
            return raw
        return _io.TextIOWrapper(raw, encoding=encoding or 'utf-8', errors=errors, newline=newline)
    if 'r' in mode:
        raw = _io.BytesIO(vfs.read(path))
    elif 'w' in mode:
        vfs.write(path, b'')
        raw = _CommitBytes(vfs, path)
    elif 'a' in mode:
        initial = vfs.read(path) if vfs.isfile(path) else b''
        if not vfs.isfile(path):
            vfs.write(path, b'')
        raw = _CommitBytes(vfs, path, initial)
    elif 'x' in mode:
        if vfs.exists(path):
            raise FileExistsError(path)
        vfs.write(path, b'')
        raw = _CommitBytes(vfs, path)
    else:
        raise ValueError('invalid mode: %r' % mode)
    if binary:
        return raw
    # Text writes never translate newlines so stored bytes do not depend on the host OS.
    nl = newline if ('r' in mode or newline is not None) else ''
    return _io.TextIOWrapper(raw, encoding=encoding or 'utf-8', errors=errors, newline=nl)


# ---------------------------------------------------------------- pathlib
def _qn_glob_match(names, parts):
    if not parts:
        return not names
    if parts[0] == '**':
        return any(_qn_glob_match(names[i:], parts[1:]) for i in range(len(names) + 1))
    return bool(names) and _fnmatch.fnmatchcase(names[0], parts[0]) and _qn_glob_match(names[1:], parts[1:])


class QnPath(_PurePosixPath):
    """pathlib.Path replacement backed by QN_VFS."""

    def __new__(cls, *args, **kwargs):
        args = tuple(_fspath(a).replace('\\', '/') if not isinstance(a, _PurePosixPath) else a for a in args)
        return _PurePosixPath.__new__(cls, *args)

    @classmethod
    def cwd(cls):
        return cls(QN_VFS.cwd)

    @classmethod
    def home(cls):
        return cls(QN_ROOT)

    def absolute(self):
        return self if self.is_absolute() else QnPath(QN_VFS.cwd) / self

    def resolve(self, strict=False):
        return QnPath(QN_VFS.abspath(str(self)))

    def expanduser(self):
        return self

    def exists(self):
        return QN_VFS.exists(self)

    def is_file(self):
        return QN_VFS.isfile(self)

    def is_dir(self):
        return QN_VFS.isdir(self)

    def stat(self):
        return QN_VFS.stat(self)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        QN_VFS.mkdir(self, parents=parents, exist_ok=exist_ok)

    def iterdir(self):
        for name in QN_VFS.listdir(self):
            yield self / name

    def glob(self, pattern):
        if '/' in pattern or '**' in pattern:
            base = QN_VFS.abspath(self)
            prefix = base.rstrip('/') + '/'
            parts = pattern.split('/')
            for f in sorted(list(QN_VFS.files) + list(QN_VFS.stubs)):
                if f.startswith(prefix) and _qn_glob_match(f[len(prefix):].split('/'), parts):
                    yield self / f[len(prefix):]
            return
        base = QN_VFS.abspath(self) if QN_VFS.isdir(self) else None
        if base is None:
            return
        for name in QN_VFS.listdir(base):
            if _fnmatch.fnmatchcase(name, pattern):
                yield self / name

    def rglob(self, pattern):
        base = QN_VFS.abspath(self)
        prefix = base.rstrip('/') + '/'
        for f in sorted(QN_VFS.files):
            if f.startswith(prefix) and _fnmatch.fnmatchcase(_pp.basename(f), pattern):
                yield self / f[len(prefix):]

    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        return qn_open(self, mode, buffering, encoding, errors, newline)

    def read_text(self, encoding=None, errors=None):
        with self.open('r', encoding=encoding, errors=errors) as f:
            return f.read()

    def read_bytes(self):
        return QN_VFS.read(self)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        with self.open('w', encoding=encoding, errors=errors, newline=newline) as f:
            return f.write(data)

    def write_bytes(self, data):
        QN_VFS.write(self, bytes(data))
        return len(data)

    def touch(self, mode=0o666, exist_ok=True):
        if QN_VFS.isfile(self):
            if not exist_ok:
                raise FileExistsError(str(self))
            return
        QN_VFS.write(self, b'')

    def unlink(self, missing_ok=False):
        try:
            QN_VFS.remove(self)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self):
        a = QN_VFS.abspath(self)
        if QN_VFS.listdir(a):
            raise OSError('directory not empty: %s' % a)
        QN_VFS.dirs.discard(a)

    def rename(self, target):
        QN_VFS.rename(self, target)
        return QnPath(target)

    def replace(self, target):
        QN_VFS.rename(self, target)
        return QnPath(target)

    def samefile(self, other):
        return QN_VFS.abspath(self) == QN_VFS.abspath(other)


def _module(name, attrs):
    m = _types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


def _copy_module(real, name, overrides):
    attrs = dict((k, v) for k, v in real.__dict__.items() if not k.startswith('__'))
    attrs.update(overrides)
    return _module(name, attrs)


# ---------------------------------------------------------------- os / os.path
class _DirEntry(object):
    def __init__(self, parent, name):
        self.name = name
        self.path = _pp.join(parent, name)

    def is_file(self, follow_symlinks=True):
        return QN_VFS.isfile(self.path)

    def is_dir(self, follow_symlinks=True):
        return QN_VFS.isdir(self.path)

    def stat(self, follow_symlinks=True):
        return QN_VFS.stat(self.path)

    def __fspath__(self):
        return self.path


class _ScanIter(object):
    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _scandir(p='.'):
    a = _fspath(p)
    return _ScanIter([_DirEntry(a, n) for n in QN_VFS.listdir(a)])


def _getmtime(p):
    return QN_VFS.stat(p).st_mtime


def _getsize(p):
    return QN_VFS.stat(p).st_size


def _remove(p, dir_fd=None):
    QN_VFS.remove(p)


def _makedirs(p, mode=0o777, exist_ok=False):
    QN_VFS.makedirs(p, exist_ok=exist_ok)


def _mkdir(p, mode=0o777, dir_fd=None):
    QN_VFS.mkdir(p)


def _rmdir(p, dir_fd=None):
    QnPath(p).rmdir()


qn_os_path = _copy_module(_pp, 'os.path', {
    'exists': QN_VFS.exists, 'lexists': QN_VFS.exists, 'isfile': QN_VFS.isfile, 'isdir': QN_VFS.isdir,
    'islink': lambda p: False, 'getsize': _getsize, 'getmtime': _getmtime,
    'abspath': QN_VFS.abspath, 'realpath': QN_VFS.abspath,
    'expanduser': lambda p: p, 'expandvars': lambda p: p,
})

qn_os = _copy_module(__import__('os'), 'os', {
    'path': qn_os_path, 'sep': '/', 'altsep': None, 'curdir': '.', 'pardir': '..', 'linesep': '\n',
    'listdir': QN_VFS.listdir, 'scandir': _scandir, 'stat': QN_VFS.stat, 'lstat': QN_VFS.stat,
    'remove': _remove, 'unlink': _remove, 'rename': QN_VFS.rename, 'replace': QN_VFS.rename,
    'makedirs': _makedirs, 'mkdir': _mkdir, 'rmdir': _rmdir,
    'getcwd': lambda: QN_VFS.cwd, 'fsync': lambda fd: None, 'fspath': _fspath,
    'open': None, 'fdopen': None, 'system': None, 'popen': None, 'startfile': None,
})

qn_pathlib = _module('pathlib', {'Path': QnPath, 'PurePath': _PurePosixPath, 'PurePosixPath': _PurePosixPath,
                                 'PosixPath': QnPath, 'WindowsPath': QnPath})


# ---------------------------------------------------------------- tempfile / shutil / glob
class _TemporaryDirectory(object):
    _n = [0]

    def __init__(self, suffix='', prefix='tmp', dir=None):
        _TemporaryDirectory._n[0] += 1
        self.name = '/tmp/%s%06d%s' % (prefix, _TemporaryDirectory._n[0], suffix)
        QN_VFS.makedirs(self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()
        return False

    def cleanup(self):
        QN_VFS.rmtree(self.name)


qn_tempfile = _module('tempfile', {'TemporaryDirectory': _TemporaryDirectory, 'gettempdir': lambda: '/tmp'})


def _glob(pattern, recursive=False):
    a = QN_VFS.abspath(pattern) if not _fspath(pattern).startswith('/') else _fspath(pattern)
    parent, pat = _pp.split(a)
    if any(c in parent for c in '*?['):
        raise NotImplementedError('glob on wildcard directories not supported: %s' % pattern)
    if not QN_VFS.isdir(parent):
        return []
    rel = not _fspath(pattern).startswith('/')
    out = []
    for n in QN_VFS.listdir(parent):
        if _fnmatch.fnmatchcase(n, pat):
            full = _pp.join(parent, n)
            out.append(_pp.relpath(full, QN_VFS.cwd) if rel else full)
    return out


qn_glob = _module('glob', {'glob': _glob, 'iglob': lambda p, recursive=False: iter(_glob(p))})


def _copyfile(src, dst, follow_symlinks=True):
    QN_VFS.write(dst, QN_VFS.read(src))
    return dst


qn_shutil = _module('shutil', {'copyfile': _copyfile, 'copy': _copyfile, 'copy2': _copyfile,
                               'move': lambda s, d: QN_VFS.rename(s, d), 'rmtree': lambda p, ignore_errors=False: QN_VFS.rmtree(p)})


# ---------------------------------------------------------------- logging / zoneinfo / dataclasses
QN_LOG_LINES = []


class _MemoryFileHandler(_logging.Handler):
    """Stands in for (Rotating)FileHandler: records go to an in-memory list."""

    def __init__(self, filename=None, mode='a', maxBytes=0, backupCount=0, encoding=None, delay=False, *a, **k):
        _logging.Handler.__init__(self)
        self.baseFilename = str(filename)
        self.stream = None

    def emit(self, record):
        if qn_foreign_thread():
            return
        try:
            QN_LOG_LINES.append(self.format(record))
            if len(QN_LOG_LINES) > 5000:
                del QN_LOG_LINES[:1000]
        except Exception:
            self.handleError(record)

    def doRollover(self):
        pass

    def _open(self):
        return None


class _QuietStreamHandler(_logging.Handler):
    """Console handler: only WARNING and above reach QMT's log window."""

    def __init__(self, stream=None):
        _logging.Handler.__init__(self)

    def emit(self, record):
        if record.levelno >= _logging.WARNING and not qn_foreign_thread():
            qn_print(self.format(record))


qn_logging_handlers = _module('logging.handlers', {'RotatingFileHandler': _MemoryFileHandler,
                                                   'TimedRotatingFileHandler': _MemoryFileHandler})
qn_logging = _copy_module(_logging, 'logging', {'FileHandler': _MemoryFileHandler,
                                               'StreamHandler': _QuietStreamHandler,
                                               'handlers': qn_logging_handlers})

_BJ = _dt.timezone(_dt.timedelta(hours=8), 'Asia/Shanghai')


def _zoneinfo(key):
    if key not in ('Asia/Shanghai', 'Asia/Chongqing', 'PRC'):
        raise ValueError('only Asia/Shanghai is available in QMT native mode')
    return _BJ


qn_zoneinfo = _module('zoneinfo', {'ZoneInfo': _zoneinfo})


class FrozenInstanceError(AttributeError):
    pass


def _dc_fields(cls):
    return cls.__dict__['__qn_dc_fields__']


def _dataclass(cls=None, frozen=False, **unused):
    def wrap(c):
        names = list(c.__dict__.get('__annotations__', {}).keys())
        defaults = {}
        for n in names:
            if n in c.__dict__:
                defaults[n] = c.__dict__[n]
        c.__qn_dc_fields__ = tuple(names)
        c.__dataclass_fields__ = dict((n, None) for n in names)

        def __init__(self, *args, **kwargs):
            if len(args) > len(names):
                raise TypeError('%s takes %d positional arguments' % (c.__name__, len(names)))
            values = dict(zip(names, args))
            for k, v in kwargs.items():
                if k not in names:
                    raise TypeError('%s got an unexpected keyword argument %r' % (c.__name__, k))
                if k in values:
                    raise TypeError('%s got multiple values for %r' % (c.__name__, k))
                values[k] = v
            for n in names:
                if n not in values:
                    if n not in defaults:
                        raise TypeError('%s missing required argument %r' % (c.__name__, n))
                    values[n] = defaults[n]
                object.__setattr__(self, n, values[n])

        def __repr__(self):
            return '%s(%s)' % (c.__name__, ', '.join('%s=%r' % (n, getattr(self, n)) for n in names))

        def _astuple(self):
            return tuple(getattr(self, n) for n in names)

        def __eq__(self, other):
            if other.__class__ is self.__class__:
                return _astuple(self) == _astuple(other)
            return NotImplemented

        c.__init__ = __init__
        c.__repr__ = __repr__
        c.__eq__ = __eq__
        if frozen:
            def __setattr__(self, name, value):
                raise FrozenInstanceError('cannot assign to field %r' % name)

            def __delattr__(self, name):
                raise FrozenInstanceError('cannot delete field %r' % name)

            c.__setattr__ = __setattr__
            c.__delattr__ = __delattr__
            c.__hash__ = lambda self: hash(_astuple(self))
        else:
            c.__hash__ = None
        return c
    return wrap if cls is None else wrap(cls)


def _asdict(obj):
    if hasattr(type(obj), '__qn_dc_fields__'):
        return dict((n, _asdict(getattr(obj, n))) for n in _dc_fields(type(obj)))
    if isinstance(obj, tuple) and hasattr(obj, '_fields'):
        return type(obj)(*[_asdict(v) for v in obj])
    if isinstance(obj, (list, tuple)):
        return type(obj)(_asdict(v) for v in obj)
    if isinstance(obj, dict):
        return dict((_asdict(k), _asdict(v)) for k, v in obj.items())
    import copy
    return copy.deepcopy(obj)


def _is_dataclass(obj):
    return hasattr(obj if isinstance(obj, type) else type(obj), '__qn_dc_fields__')


qn_dataclasses = _module('dataclasses', {'dataclass': _dataclass, 'asdict': _asdict, 'is_dataclass': _is_dataclass,
                                         'FrozenInstanceError': FrozenInstanceError})


# ---------------------------------------------------------------- pandas
def _pd_version():
    out = []
    for part in _pd.__version__.split('.')[:2]:
        digits = ''.join(c for c in part if c.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


_PD_VERSION = _pd_version()
_PD_HAS_CONCAT_SORT = _PD_VERSION >= (0, 23)
_PD_HAS_LINETERMINATOR = _PD_VERSION >= (1, 5)
_PD_SERIES_CSV_HEADER_FALSE = _PD_VERSION < (1, 0)


def _qn_read_csv(filepath_or_buffer, *args, **kwargs):
    src = filepath_or_buffer
    if isinstance(src, (str, bytes)) or hasattr(src, '__fspath__'):
        src = _io.BytesIO(QN_VFS.read(src))
    return _pd.read_csv(src, *args, **kwargs)


def _qn_concat(objs, *args, **kwargs):
    sort = kwargs.pop('sort', False)
    if _PD_HAS_CONCAT_SORT:
        return _pd.concat(objs, *args, sort=sort, **kwargs)
    objs = list(objs.values()) if isinstance(objs, dict) else list(objs)
    result = _pd.concat(objs, *args, **kwargs)
    axis = kwargs.get('axis', args[0] if args else 0)
    if not sort and axis in (0, 'index') and isinstance(result, _pd.DataFrame):
        # pandas < 0.23 always sorted the other axis when columns differed; restore the
        # first-seen column order that sort=False produces in the production pandas.
        order = []
        seen = set()
        for o in objs:
            if o is None:
                continue
            cols = o.columns if isinstance(o, _pd.DataFrame) else [o.name]
            for c in cols:
                if c not in seen:
                    seen.add(c)
                    order.append(c)
        if len(order) == len(result.columns) and set(order) == set(result.columns):
            result = result[order]
    return result


def _qn_caller_embedded(depth=2):
    """True when the pandas call comes straight from an embedded A_System module."""
    try:
        return bool(_sys._getframe(depth).f_globals.get('__qn_embedded__'))
    except ValueError:
        return False


_QN_NA_STR = '\U0010ffff\U0010ffff<NA>'  # sorts after every real string key


class _QnNaGroupBy(object):
    """pandas < 1.1 groupby(dropna=False): NaN keys become a sentinel that sorts last (where
    pandas >= 1.1 puts the NaN group); wherever group keys come back out (iteration, index of
    aggregated results) the sentinel is turned back into NaN. Row-aligned results such as
    cumcount, rank and transform never expose keys and need no mapping."""

    def __init__(self, inner, source_index, sentinels):
        self._inner = inner
        self._source_index = source_index
        self._sentinels = sentinels

    def _key(self, value):
        if isinstance(value, tuple):
            return tuple(self._key(v) for v in value)
        for s in self._sentinels:
            if value is s or (isinstance(value, type(s)) and value == s):
                return _np.nan
        return value

    def _unmap_index(self, index):
        if isinstance(index, _pd.MultiIndex):
            arrays = [[self._key(v) for v in index.get_level_values(i)] for i in range(index.nlevels)]
            return _pd.MultiIndex.from_arrays(arrays, names=index.names)
        return _pd.Index([self._key(v) for v in index], name=index.name)

    def _wrap(self, result):
        if isinstance(result, (_pd.DataFrame, _pd.Series)):
            if result.index.equals(self._source_index) or not len(result):
                return result
            mapped = self._unmap_index(result.index)
            if not mapped.equals(result.index):
                result = result.copy()
                result.index = mapped
        return result

    def __iter__(self):
        for key, group in self._inner:
            yield self._key(key), group

    def __len__(self):
        return len(self._inner)

    def __getitem__(self, item):
        return _QnNaGroupBy(self._inner[item], self._source_index, self._sentinels)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):
            def call(*args, **kwargs):
                return self._wrap(attr(*args, **kwargs))
            return call
        return self._wrap(attr)


def _qn_groupby_keep_na(obj, by, orig, args, kwargs):
    keys = by if isinstance(by, list) else [by]
    series_keys = []
    sentinels = []
    has_na = False
    for k in keys:
        if isinstance(obj, _pd.DataFrame) and not isinstance(k, (_pd.Series, _np.ndarray, list)) and k in obj.columns:
            col = obj[k]
        elif isinstance(k, _pd.Series):
            col = k
        else:
            raise NotImplementedError('groupby(dropna=False) key type not supported on pandas<1.1: %r' % (k,))
        na = col.isna()
        if na.any():
            has_na = True
            valid = col[~na]
            if _pd.api.types.is_numeric_dtype(col) and not _pd.api.types.is_bool_dtype(col):
                if len(valid) and _np.isinf(valid.astype(float)).any():
                    raise NotImplementedError('groupby(dropna=False): numeric key already contains inf')
                sentinel = float('inf')
                col = col.astype(float).fillna(sentinel)
            elif all(isinstance(v, str) for v in valid.tolist()):
                sentinel = _QN_NA_STR
                col = col.astype(object).where(~na, sentinel)
            else:
                raise NotImplementedError('groupby(dropna=False): mixed-type key with NaN: %r' % (k,))
            sentinels.append(sentinel)
            col = col.rename(k if not isinstance(k, _pd.Series) else k.name)
        series_keys.append(col)
    if not has_na:
        return orig(obj, by, *args, **kwargs)
    new_by = series_keys if isinstance(by, list) else series_keys[0]
    return _QnNaGroupBy(orig(obj, new_by, *args, **kwargs), obj.index, sentinels)


def _qn_patch_old_pandas():
    """Give pandas 0.22 the pandas-2 semantics the strategy code was written against.

    Additive patches (keywords or methods 0.22 rejects) apply to every caller since no
    existing caller can depend on an error. Patches that change a default (dict key order,
    str.replace regex default) only apply when the caller is an embedded A_System module.
    """
    import re as _re
    from collections import OrderedDict as _OD
    patched = []
    if _PD_VERSION < (0, 23):
        orig_df_init = _pd.DataFrame.__init__

        def df_init(self, data=None, index=None, columns=None, dtype=None, copy=False):
            if columns is None and _qn_caller_embedded():
                if isinstance(data, dict) and not isinstance(data, _OD):
                    columns = list(data.keys())
                elif isinstance(data, (list, tuple)) and data and all(isinstance(r, dict) for r in data):
                    seen = _OD()
                    for r in data:
                        for k in r:
                            seen[k] = None
                    columns = list(seen.keys())
            orig_df_init(self, data=data, index=index, columns=columns, dtype=dtype, copy=copy)
        _pd.DataFrame.__init__ = df_init
        orig_s_init = _pd.Series.__init__

        def s_init(self, data=None, index=None, dtype=None, name=None, copy=False, fastpath=False):
            if index is None and isinstance(data, dict) and not isinstance(data, _OD) and _qn_caller_embedded():
                data = _OD(data)
            orig_s_init(self, data=data, index=index, dtype=dtype, name=name, copy=copy, fastpath=fastpath)
        _pd.Series.__init__ = s_init
        patched.append('dict key order')
    _SM = type(_pd.Series([''], dtype=object).str)
    if _PD_VERSION < (2, 0):
        orig_replace = _SM.replace

        def str_replace(self, pat, repl, n=-1, case=None, flags=0, regex=None):
            if regex is None:
                regex = not _qn_caller_embedded()  # pandas >= 2.0 default is literal
            if not regex:
                if case is not None or flags:
                    raise ValueError('case/flags require regex=True')
                if callable(repl):
                    raise ValueError('callable repl requires regex=True')
                pat = _re.escape(pat)
                repl = repl.replace('\\', '\\\\')
            return orig_replace(self, pat, repl, n=n, case=case, flags=flags)
        _SM.replace = str_replace
        patched.append('str.replace regex')
    if not hasattr(_SM, 'fullmatch'):
        def fullmatch(self, pat, case=True, flags=0, na=_np.nan):
            return self.match('(?:%s)\\Z' % pat, case=case, flags=flags, na=na)
        _SM.fullmatch = fullmatch
        patched.append('str.fullmatch')
    if _PD_VERSION < (1, 0):
        def _to_string(series):
            out = series.astype(object).copy()
            mask = series.isna()
            out[~mask] = [str(v) for v in series[~mask].tolist()]
            out[mask] = _np.nan
            return out
        orig_s_astype = _pd.Series.astype
        orig_df_astype = _pd.DataFrame.astype

        def s_astype(self, dtype, *args, **kwargs):
            if isinstance(dtype, str) and dtype == 'string':
                return _to_string(self)
            return orig_s_astype(self, dtype, *args, **kwargs)

        def df_astype(self, dtype, *args, **kwargs):
            if isinstance(dtype, str) and dtype == 'string':
                out = self.copy()
                for c in out.columns:
                    out[c] = _to_string(out[c])
                return out
            return orig_df_astype(self, dtype, *args, **kwargs)
        _pd.Series.astype = s_astype
        _pd.DataFrame.astype = df_astype
        patched.append('astype string')
    if not hasattr(_pd.Series, 'to_list'):
        _pd.Series.to_list = _pd.Series.tolist
        patched.append('Series.to_list')
    if _PD_VERSION < (1, 1):
        for cls in (_pd.DataFrame, _pd.Series):
            orig_groupby = cls.groupby

            def groupby(self, by=None, *args, **kwargs):
                dropna = kwargs.pop('dropna', True)
                if dropna or by is None:
                    return groupby.orig(self, by, *args, **kwargs)
                return _qn_groupby_keep_na(self, by, groupby.orig, args, kwargs)
            groupby.orig = orig_groupby
            cls.groupby = groupby
        patched.append('groupby dropna')
    if _PD_VERSION < (0, 25):
        from pandas.core.groupby import DataFrameGroupBy as _DGB
        orig_agg = _DGB.aggregate

        def aggregate(self, arg=None, *args, **kwargs):
            if arg is None and not args and kwargs and all(isinstance(v, tuple) and len(v) == 2
                                                          for v in kwargs.values()):
                names = list(kwargs.keys())
                parts = [self[col].agg(func) for col, func in kwargs.values()]
                out = _pd.concat(parts, axis=1)
                out.columns = names
                return out
            return orig_agg(self, arg, *args, **kwargs)
        _DGB.aggregate = aggregate
        _DGB.agg = aggregate
        patched.append('named aggregation')
    return patched


QN_PANDAS_PATCHES = _qn_patch_old_pandas()


def _qn_read_csv_precise(filepath_or_buffer, *args, **kwargs):
    if _PD_VERSION < (1, 3):
        # pandas >= 1.3 parses floats to the correctly rounded double; in 0.22 only the
        # round_trip parser does ('high' can be off by one ulp on 17-digit text).
        kwargs.setdefault('float_precision', 'round_trip')
    return _qn_read_csv(filepath_or_buffer, *args, **kwargs)


qn_pandas = _copy_module(_pd, 'pandas', {'read_csv': _qn_read_csv_precise, 'concat': _qn_concat,
                                         'NA': getattr(_pd, 'NA', _np.nan)})
qn_pandas.__version__ = _pd.__version__


def _qn_float_repr_column(values):
    out = _np.empty(len(values), dtype=object)
    for i, v in enumerate(values):
        out[i] = _np.nan if v != v else repr(float(v))
    return out


def _qn_exact_float_text(obj):
    """pandas < 1.0 writes floats with 16 significant digits; pandas >= 1.0 writes the
    shortest text that reads back to the same double (Python repr). Pre-format float
    columns with repr so values survive the CSV round trip exactly as in production."""
    if isinstance(obj, _pd.Series):
        if obj.dtype.kind == 'f':
            return _pd.Series(_qn_float_repr_column(obj.values), index=obj.index, name=obj.name)
        return obj
    float_cols = [i for i, dt in enumerate(obj.dtypes) if dt.kind == 'f']
    if not float_cols:
        return obj
    out = obj.copy()
    for i in float_cols:
        out.iloc[:, i] = _qn_float_repr_column(obj.iloc[:, i].values)
    return out


def __qn_to_csv__(obj, path_or_buf=None, *args, **kwargs):
    if path_or_buf is None or hasattr(path_or_buf, 'write'):
        return obj.to_csv(path_or_buf, *args, **kwargs)
    encoding = kwargs.pop('encoding', None) or 'utf-8'
    mode = kwargs.pop('mode', 'w')
    kwargs.pop('errors', None)
    if 'lineterminator' in kwargs and not _PD_HAS_LINETERMINATOR:
        kwargs['line_terminator'] = kwargs.pop('lineterminator')
    if not _PD_HAS_LINETERMINATOR:
        kwargs.setdefault('line_terminator', '\n')
    else:
        kwargs.setdefault('lineterminator', '\n')
    if isinstance(obj, _pd.Series) and 'header' not in kwargs and _PD_SERIES_CSV_HEADER_FALSE:
        kwargs['header'] = True  # pandas < 1.0 Series.to_csv defaulted to header=False
    if _PD_VERSION < (1, 0) and kwargs.get('float_format') is None:
        obj = _qn_exact_float_text(obj)
    text = obj.to_csv(None, *args, **kwargs)
    data = text.encode(encoding)
    if 'a' in mode and QN_VFS.isfile(path_or_buf):
        if encoding.lower().replace('_', '-') == 'utf-8-sig' and data.startswith(b'\xef\xbb\xbf'):
            data = data[3:]
        data = QN_VFS.read(path_or_buf) + data
    QN_VFS.write(path_or_buf, data)
    return None




def __qn_to_markdown__(obj, buf=None, mode='wt', index=True, **kwargs):
    if hasattr(obj, 'to_markdown') and _PD_VERSION >= (1, 0):
        try:
            return obj.to_markdown(buf, mode=mode, index=index, **kwargs)
        except ImportError:
            pass
    frame = obj.to_frame() if isinstance(obj, _pd.Series) else obj
    cols = ([''] if index else []) + [str(c) for c in frame.columns]
    lines = ['| ' + ' | '.join(cols) + ' |', '|' + '|'.join(['---'] * len(cols)) + '|']
    for idx, row in frame.iterrows():
        cells = ([str(idx)] if index else []) + ['' if v is None else str(v) for v in row.tolist()]
        lines.append('| ' + ' | '.join(c.replace('|', '/').replace('\n', ' ') for c in cells) + ' |')
    text = '\n'.join(lines)
    if buf is None:
        return text
    if hasattr(buf, 'write'):
        buf.write(text)
        return None
    QN_VFS.write(buf, text.encode('utf-8'))
    return None


def __qn_to_numpy__(obj, dtype=None, copy=False, **kwargs):
    if kwargs:
        raise TypeError('unsupported to_numpy arguments: %s' % sorted(kwargs))
    if hasattr(obj, 'to_numpy'):
        return obj.to_numpy(dtype=dtype, copy=copy)
    values = _np.asarray(obj.values, dtype=dtype)
    return values.copy() if copy else values


def __qn_isascii__(obj):
    """str.isascii() is Python 3.7+."""
    if isinstance(obj, str):
        return all(ord(c) < 128 for c in obj)
    if isinstance(obj, (bytes, bytearray)):
        return all(b < 128 for b in obj)
    return obj.isascii()


# ---------------------------------------------------------------- thread-local script context
import threading as _threading
import queue as _queue

_QN_TLS = _threading.local()


# ---------------------------------------------------------------- run lifecycle
# QMT keeps the interpreter and every thread alive across strategy stop/start. Threads of
# a stopped run must end by themselves: each run gets an id, threads carry the id of the
# run that started them, and checked waits (sleep, QMT data calls, script pipes) raise
# QnStopped on a thread whose run has stopped or been replaced.
QN_RUN = ['%x' % int(_time.time() * 1000)]
QN_SHUTDOWN = [False]


class QnStopped(BaseException):
    """Ends a thread of a stopped run. Not an Exception (handlers for Exception let it
    through) and not SystemExit (a script's sys.exit is a normal end, this is not)."""


def qn_is_stopped(exc):
    """QnStopped of this or of another copy of the strategy file (another copy's host may
    inject its own class into a thread of this copy)."""
    return type(exc).__name__ == 'QnStopped'


def qn_foreign_thread():
    """True on a thread of this system that belongs to a stopped or replaced run, or to an
    embedded script its parent has killed (a timed-out child process)."""
    t = _threading.current_thread()
    if not getattr(t, '_qn_system', False):
        return False
    kill = getattr(t, '_qn_kill', None) or ()
    return bool(QN_SHUTDOWN[0] or getattr(t, '_qn_run', None) != QN_RUN[0] or any(k[0] for k in kill))


def qn_check_alive():
    if qn_foreign_thread():
        raise QnStopped('A_System strategy run stopped')


def qn_new_run():
    QN_RUN[0] = '%x' % int(_time.time() * 1000)
    QN_SHUTDOWN[0] = False
    return QN_RUN[0]


def _qn_sleep(seconds):
    qn_check_alive()
    end = _time.monotonic() + max(0.0, float(seconds))
    while True:
        left = end - _time.monotonic()
        if left <= 0:
            return None
        _time.sleep(min(left, 0.5))
        qn_check_alive()


def _qn_wait(event, timeout):
    """Event.wait in short slices so a stopped run's thread notices and ends."""
    end = None if timeout is None else _time.monotonic() + timeout
    while True:
        step = 0.5 if end is None else min(0.5, end - _time.monotonic())
        if step <= 0:
            return event.is_set()
        if event.wait(step):
            return True
        qn_check_alive()


def _qn_current_argv(default):
    argv = getattr(_QN_TLS, 'argv', None)
    return argv if argv is not None else default


# ---------------------------------------------------------------- print
QN_PRINT_LINES = []
QN_ECHO = [False]


def qn_print(*args, **kwargs):
    """Embedded output: a child script's lines go to its parent's pipe (like a subprocess
    stdout); everything else is kept in memory and echoed to QMT's log only when enabled."""
    target = kwargs.get('file')
    if target not in (None, _sys.stdout, _sys.stderr):
        return _builtins.print(*args, **kwargs)
    if qn_foreign_thread():
        return None
    text = kwargs.get('sep', ' ').join(str(a) for a in args)
    sink = getattr(_QN_TLS, 'sink', None)
    if sink is not None:
        for line in (text + kwargs.get('end', '\n')).splitlines():
            sink(line)
        return
    QN_PRINT_LINES.append(text)
    if len(QN_PRINT_LINES) > 5000:
        del QN_PRINT_LINES[:1000]
    if QN_ECHO[0]:
        try:
            _builtins.print(text)
        except UnicodeEncodeError:
            _builtins.print(text.encode('ascii', 'backslashreplace').decode('ascii'))


def __qn_isascii__(obj):
    """str.isascii() is Python 3.7+."""
    if isinstance(obj, str):
        return all(ord(c) < 128 for c in obj)
    if isinstance(obj, (bytes, bytearray)):
        return all(b < 128 for b in obj)
    return obj.isascii()


_QN_ISO_TIME = __import__('re').compile(
    r'^(\d{2})(?::(\d{2})(?::(\d{2})(?:\.(\d{3}|\d{6}))?)?)?'
    r'(?:([+-])(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{6}))?)?)?$')


def _qn_parse_iso_time(text):
    m = _QN_ISO_TIME.match(text)
    if not m:
        raise ValueError('Invalid isoformat string: %r' % text)
    hh, mm, ss, frac, sign, oh, om, osec, ofrac = m.groups()
    micro = int((frac or '0').ljust(6, '0'))
    tz = None
    if sign:
        off = _dt.timedelta(hours=int(oh), minutes=int(om), seconds=int(osec or 0), microseconds=int(ofrac or 0))
        tz = _dt.timezone(-off if sign == '-' else off)
    return _dt.time(int(hh), int(mm or 0), int(ss or 0), micro, tzinfo=tz)


def __qn_fromisoformat__(cls, text):
    """date/time/datetime.fromisoformat are Python 3.7+; same accepted formats as 3.7."""
    if _sys.version_info >= (3, 7) or not isinstance(cls, type) or not issubclass(cls, (_dt.date, _dt.time)):
        return cls.fromisoformat(text)
    if not isinstance(text, str):
        raise TypeError('fromisoformat: argument must be str')
    if issubclass(cls, _dt.datetime):
        day = _dt.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        if len(text) == 10:
            return cls(day.year, day.month, day.day)
        t = _qn_parse_iso_time(text[11:])
        return cls(day.year, day.month, day.day, t.hour, t.minute, t.second, t.microsecond, tzinfo=t.tzinfo)
    if issubclass(cls, _dt.date):
        if len(text) != 10 or text[4] != '-' or text[7] != '-':
            raise ValueError('Invalid isoformat string: %r' % text)
        return cls(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    t = _qn_parse_iso_time(text)
    return cls(t.hour, t.minute, t.second, t.microsecond, tzinfo=t.tzinfo)


qn_time = _copy_module(_time, 'time', {'sleep': _qn_sleep})


def _qn_checked_queue(base):
    class _QnQueue(base):
        """A blocking get() waits in short slices so a stopped run's worker ends."""

        def get(self, block=True, timeout=None):
            if not block:
                return base.get(self, False)
            end = None if timeout is None else _time.monotonic() + timeout
            while True:
                step = 0.5 if end is None else min(0.5, end - _time.monotonic())
                if step <= 0:
                    return base.get(self, False)
                try:
                    return base.get(self, True, step)
                except _queue.Empty:
                    qn_check_alive()
    _QnQueue.__name__ = base.__name__
    return _QnQueue


qn_queue = _copy_module(_queue, 'queue', {'Queue': _qn_checked_queue(_queue.Queue),
                                          'LifoQueue': _qn_checked_queue(_queue.LifoQueue),
                                          'PriorityQueue': _qn_checked_queue(_queue.PriorityQueue)})
for _name in ('time', 'monotonic', 'perf_counter'):
    if not hasattr(_time, _name + '_ns'):
        setattr(qn_time, _name + '_ns', (lambda f: (lambda: int(f() * 1e9)))(getattr(_time, _name)))


# ---------------------------------------------------------------- argparse
import argparse as _argparse


def _make_argparse(loader):
    class _QnArgumentParser(_argparse.ArgumentParser):
        """argparse reads the real sys.argv; embedded scripts get their own argv instead."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault('prog', _qn_current_argv(loader.sys.argv)[0].rsplit('/', 1)[-1])
            _argparse.ArgumentParser.__init__(self, *args, **kwargs)

        def parse_known_args(self, args=None, namespace=None):
            if args is None:
                args = _qn_current_argv(loader.sys.argv)[1:]
            return _argparse.ArgumentParser.parse_known_args(self, args, namespace)
    return _copy_module(_argparse, 'argparse', {'ArgumentParser': _QnArgumentParser})


# ---------------------------------------------------------------- environment
QN_ENVIRON = dict(__import__('os').environ)


def _qn_getenv(key, default=None):
    return QN_ENVIRON.get(key, default)


def _qn_putenv(key, value):
    QN_ENVIRON[key] = value


qn_os.environ = QN_ENVIRON
qn_os.getenv = _qn_getenv
qn_os.putenv = _qn_putenv
qn_os.name = 'posix'  # every path is a POSIX path inside QN_VFS


# ---------------------------------------------------------------- sqlite3 (in memory, dumped into QN_VFS)
import sqlite3 as _sqlite3

QN_SQLITE_DUMP_MAGIC = b'-- QN_SQLITE_DUMP v1\n'


class _QnDatabase(object):
    """One real in-memory connection per database path. Logical connections take the
    database lock for a whole transaction, like a file database's write lock, so
    transactions of different threads never interleave."""

    def __init__(self, path):
        self.path = path
        self.real = _sqlite3.connect(':memory:', check_same_thread=False, isolation_level=None)
        self.lock = _threading.RLock()
        self.owner = None
        self.dirty = False
        if path and QN_VFS.isfile(path):
            data = QN_VFS.read(path)
            if data.startswith(QN_SQLITE_DUMP_MAGIC):
                self.real.executescript(data[len(QN_SQLITE_DUMP_MAGIC):].decode('utf-8'))
            elif data:
                raise _sqlite3.DatabaseError('file is not a QN sqlite dump: %s' % path)

    def persist(self):
        if not self.path:
            return
        skip = ()
        for suffix, tables in QN_SQLITE_SKIP_TABLES.items():
            if self.path.endswith(suffix):
                skip = tuple('INSERT INTO "%s"' % t for t in tables)
        text = '\n'.join(line for line in self.real.iterdump() if not (skip and line.startswith(skip)))
        QN_VFS.makedirs(_pp.dirname(self.path))
        QN_VFS.write(self.path, QN_SQLITE_DUMP_MAGIC + text.encode('utf-8'))
        self.dirty = False


QN_SQLITE_DATABASES = {}
QN_SQLITE_SKIP_TABLES = {}  # path suffix -> tables whose rows are not kept in the dump (audit-only)
_QN_SQLITE_REGISTRY_LOCK = _threading.Lock()


def _qn_sqlite_path(database, uri):
    text = _fspath(database)
    if text == ':memory:' or text == '':
        return None
    if uri:
        if text.startswith('file:'):
            text = text[5:]
            if text.startswith('//'):
                text = text[2:]
        text = text.split('?', 1)[0]
        try:
            from urllib.parse import unquote
            text = unquote(text)
        except ImportError:
            pass
        if text == ':memory:' or not text:
            return None
    return QN_VFS.abspath(text)


_QN_UPSERT = __import__('re').compile(
    r'^\s*INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)\s*ON\s+CONFLICT\s*\(([^)]*)\)\s*'
    r'DO\s+UPDATE\s+SET\s+(.*?)\s*$', __import__('re').S | __import__('re').I)
QN_SQLITE_NATIVE_UPSERT = [_sqlite3.sqlite_version_info >= (3, 24, 0)]


def _qn_split_top(text):
    parts, depth, quote, cur = [], 0, None, ''
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _qn_upsert_plan(sql, params):
    """`INSERT ... ON CONFLICT(k) DO UPDATE SET c=excluded.c` needs SQLite 3.24; QMT's is older.
    Equivalent: plain INSERT, and on a UNIQUE conflict an UPDATE of the same columns by key."""
    m = _QN_UPSERT.match(sql)
    if not m:
        return None
    table, cols, vals, keys, sets = m.groups()
    cols = [c.strip() for c in cols.split(',')]
    vals = _qn_split_top(vals)
    if len(cols) != len(vals):
        return None
    params = list(params or ())
    bound, it = {}, iter(params)
    for c, v in zip(cols, vals):
        bound[c] = ('?', next(it)) if v == '?' else (v, None)
    assignments = []
    for item in _qn_split_top(sets):
        left, right = [x.strip() for x in item.split('=', 1)]
        if not right.lower().startswith('excluded.'):
            return None
        assignments.append((left, right.split('.', 1)[1].strip()))
    key_cols = [k.strip() for k in keys.split(',')]
    insert_sql = 'INSERT INTO %s(%s) VALUES(%s)' % (table, ', '.join(cols), ', '.join(vals))
    set_sql, set_params = [], []
    for left, src in assignments:
        token, value = bound[src]
        set_sql.append('%s=%s' % (left, token))
        if token == '?':
            set_params.append(value)
    where_sql, where_params = [], []
    for k in key_cols:
        token, value = bound[k]
        where_sql.append('%s=%s' % (k, token))
        if token == '?':
            where_params.append(value)
    update_sql = 'UPDATE %s SET %s WHERE %s' % (table, ', '.join(set_sql), ' AND '.join(where_sql))
    return insert_sql, params, update_sql, set_params + where_params


class _QnCursor(object):
    def __init__(self, conn, rows, description, rowcount, lastrowid):
        self.connection = conn
        self._rows = rows
        self._pos = 0
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.arraysize = 1

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=None):
        size = size or self.arraysize
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self):
        self._rows = []

    def execute(self, sql, parameters=()):
        return self.connection._run('execute', sql, parameters, cursor=self)

    def executemany(self, sql, seq):
        return self.connection._run('executemany', sql, seq, cursor=self)

    def executescript(self, script):
        return self.connection.executescript(script)


class _QnConnection(object):
    def __init__(self, db, timeout, isolation_level):
        self._db = db
        self._timeout = float(timeout)
        self.isolation_level = isolation_level
        self.row_factory = None
        self.text_factory = str
        self._held = 0
        self._closed = False

    # transaction ownership -------------------------------------------------
    def _acquire(self):
        if self._held:
            return
        if not self._db.lock.acquire(timeout=max(self._timeout, 0.001)):
            raise _sqlite3.OperationalError('database is locked')
        self._held = 1
        self._db.owner = self

    def _release_if_idle(self):
        if self._held and not self._db.real.in_transaction:
            self._held = 0
            self._db.owner = None
            if self._db.dirty:
                self._db.persist()
            self._db.lock.release()

    def _needs_begin(self, sql):
        if self.isolation_level is None:
            return False
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        return head in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')

    def _run(self, kind, sql, params, cursor=None):
        if self._closed:
            raise _sqlite3.ProgrammingError('Cannot operate on a closed database.')
        self._acquire()
        try:
            real = self._db.real
            real.row_factory = self.row_factory
            real.text_factory = self.text_factory
            if self._needs_begin(sql) and not real.in_transaction:
                level = self.isolation_level or ''
                real.execute('BEGIN ' + level if level else 'BEGIN')
            head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
            plan = _qn_upsert_plan(sql, params) if kind == 'execute' and not QN_SQLITE_NATIVE_UPSERT[0] else None
            if plan is not None:
                try:
                    cur = real.execute(plan[0], plan[1])
                except _sqlite3.IntegrityError as exc:
                    if 'UNIQUE' not in str(exc).upper() and 'PRIMARY KEY' not in str(exc).upper():
                        raise
                    cur = real.execute(plan[2], plan[3])
            else:
                cur = real.execute(sql, params) if kind == 'execute' else real.executemany(sql, params)
            rows = cur.fetchall() if cur.description else []
            result = _QnCursor(self, rows, cur.description, cur.rowcount, cur.lastrowid)
            if head not in ('SELECT', 'PRAGMA', 'EXPLAIN', 'WITH') or real.total_changes:
                if head not in ('SELECT', 'EXPLAIN'):
                    self._db.dirty = True
            if cursor is not None:
                cursor._rows, cursor._pos = result._rows, 0
                cursor.description, cursor.rowcount, cursor.lastrowid = (
                    result.description, result.rowcount, result.lastrowid)
                return cursor
            return result
        finally:
            self._release_if_idle()

    def execute(self, sql, parameters=()):
        return self._run('execute', sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self._run('executemany', sql, seq_of_parameters)

    def executescript(self, script):
        self._acquire()
        try:
            real = self._db.real
            if real.in_transaction:
                real.execute('COMMIT')
            real.executescript(script)
            self._db.dirty = True
            return _QnCursor(self, [], None, -1, None)
        finally:
            self._release_if_idle()

    def cursor(self, factory=None):
        return _QnCursor(self, [], None, -1, None)

    def commit(self):
        if self._held:
            if self._db.real.in_transaction:
                self._db.real.execute('COMMIT')
            self._release_if_idle()

    def rollback(self):
        if self._held:
            if self._db.real.in_transaction:
                self._db.real.execute('ROLLBACK')
            self._db.dirty = True
            self._release_if_idle()

    def close(self):
        if self._closed:
            return
        if self._held:
            # sqlite3 discards uncommitted work when a connection closes
            self.rollback()
        self._closed = True

    @property
    def in_transaction(self):
        return bool(self._held and self._db.real.in_transaction)

    @property
    def total_changes(self):
        return self._db.real.total_changes

    def iterdump(self):
        self._acquire()
        try:
            return iter(list(self._db.real.iterdump()))
        finally:
            self._release_if_idle()

    def create_function(self, *args, **kwargs):
        return self._db.real.create_function(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def _qn_sqlite_connect(database, timeout=5.0, detect_types=0, isolation_level='', check_same_thread=True,
                       factory=None, cached_statements=100, uri=False):
    path = _qn_sqlite_path(database, uri)
    with _QN_SQLITE_REGISTRY_LOCK:
        if path is None:
            db = _QnDatabase(None)
        else:
            db = QN_SQLITE_DATABASES.get(path)
            if db is None:
                db = _QnDatabase(path)
                QN_SQLITE_DATABASES[path] = db
    return _QnConnection(db, timeout, isolation_level)


def qn_sqlite_persist_all():
    for db in list(QN_SQLITE_DATABASES.values()):
        if db.lock.acquire(timeout=5):
            try:
                if db.dirty:
                    db.persist()
            finally:
                db.lock.release()


qn_sqlite3 = _copy_module(_sqlite3, 'sqlite3', {'connect': _qn_sqlite_connect, 'Connection': _QnConnection,
                                               'Cursor': _QnCursor})


# ---------------------------------------------------------------- subprocess (embedded scripts as threads)
import subprocess as _subprocess

QN_PROCESSES = {}
_QN_PID = [900000]


class QnProcess(object):
    """Runs `python .../scripts/name.py args` as an embedded script in its own module set,
    on a thread. Exposes the Popen surface the daemon uses: stdout lines, poll, wait, kill."""

    def __init__(self, codebase, module, argv, text, cwd=None):
        qn_check_alive()
        _QN_PID[0] += 1
        self.pid = _QN_PID[0]
        self.args = argv
        self.returncode = None
        self.killed = False
        self.started = self.last_output = _time.time()
        self._text = text
        self._lines = _queue.Queue()
        self._done = _threading.Event()
        self.stdout = self
        self.stderr = None
        self.stdin = None
        QN_PROCESSES[self.pid] = self
        self._thread = QnThread(target=self._run, args=(codebase, module, argv),
                                name='qn-script-%s' % module.rsplit('.', 1)[-1], daemon=True)
        self._kill_token = [False]  # shared with the threads this script starts
        self._thread._qn_kill = tuple(self._thread._qn_kill) + (self._kill_token,)
        self._thread.start()

    def _emit(self, line):
        self.last_output = _time.time()
        self._lines.put(line if self._text else (line + '\n').encode('utf-8'))

    def _run(self, codebase, module, argv):
        _QN_TLS.sink = self._emit
        code = 0
        try:
            QnLoader(codebase).run_main(module, argv, raise_on_exit=False)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            if exc.code not in (None, 0) and not isinstance(exc.code, int):
                self._emit(str(exc.code))
        except BaseException as exc:
            if qn_is_stopped(exc):
                self.killed = True
            else:
                import traceback
                for line in traceback.format_exc().splitlines():
                    self._emit(line)
                code = 1
        finally:
            _QN_TLS.sink = None
            self.returncode = -9 if self.killed else code
            self._lines.put(None)
            self._done.set()

    # file-like stdout ------------------------------------------------------
    def _next_line(self):
        while True:
            try:
                return self._lines.get(timeout=0.5)
            except _queue.Empty:
                qn_check_alive()

    def __iter__(self):
        while True:
            item = self._next_line()
            if item is None:
                self._lines.put(None)
                return
            yield item

    def readline(self):
        item = self._next_line()
        if item is None:
            self._lines.put(None)
            return '' if self._text else b''
        return item

    def read(self):
        return ('' if self._text else b'').join(list(self))

    def close(self):
        pass

    # Popen surface ---------------------------------------------------------
    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not _qn_wait(self._done, timeout):
            raise _subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.wait(timeout)
        return self.read(), None

    def kill(self):
        # Like killing a child process: the script's threads end at their next checked wait
        # (sleep, QMT data call, pipe read; at most 0.5 s) and its queued QMT calls are dropped.
        self.killed = True
        self._kill_token[0] = True
        if not self._done.is_set():
            self.returncode = -9

    terminate = kill

    def send_signal(self, sig):
        self.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _qn_script_module(cmd, codebase):
    parts = [cmd] if isinstance(cmd, str) else list(cmd)
    for i, part in enumerate(parts):
        text = _fspath(part).replace('\\', '/')
        if text.endswith('.py') and '/scripts/' in text:
            name = 'scripts.' + text.rsplit('/scripts/', 1)[1][:-3].replace('/', '.')
            if name in codebase.sources:
                return name, [_fspath(a) for a in parts[i + 1:]]
    return None, None


def _make_subprocess(codebase):
    class _Popen(object):
        def __new__(cls, args, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None,
                    preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None,
                    universal_newlines=None, startupinfo=None, creationflags=0, text=None,
                    encoding=None, errors=None, **kwargs):
            module, argv = _qn_script_module(args, codebase)
            if module is None:
                raise FileNotFoundError(2, 'external programs are not available in QMT native mode', str(args))
            return QnProcess(codebase, module, argv, bool(text or universal_newlines or encoding), cwd)

    def run(args, input=None, capture_output=False, timeout=None, check=False, **kwargs):
        module, argv = _qn_script_module(args, codebase)
        text = bool(kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding'))
        if module is None:
            first = _fspath(args[0] if not isinstance(args, str) else args.split()[0]).lower()
            if first.endswith('taskkill') or first.endswith('taskkill.exe'):
                return _subprocess.CompletedProcess(args, 0, '' if text else b'', '' if text else b'')
            raise FileNotFoundError(2, 'external programs are not available in QMT native mode', str(args))
        proc = QnProcess(codebase, module, argv, text)
        out, _ = proc.communicate(timeout=timeout)
        result = _subprocess.CompletedProcess(args, proc.returncode, out, None)
        if check and proc.returncode:
            raise _subprocess.CalledProcessError(proc.returncode, args, out)
        return result

    def check_output(args, **kwargs):
        kwargs['check'] = True
        return run(args, **kwargs).stdout

    def call(args, **kwargs):
        return run(args, **kwargs).returncode

    return _copy_module(_subprocess, 'subprocess', {'Popen': _Popen, 'run': run, 'check_output': check_output,
                                                     'call': call, 'check_call': lambda a, **k: run(a, check=True, **k).returncode})


def _qn_kill(pid, sig):
    proc = QN_PROCESSES.get(pid)
    if proc is None:
        raise ProcessLookupError(3, 'No such process')
    proc.kill()


qn_os.kill = _qn_kill


# ---------------------------------------------------------------- signal / network / misc stubs
import signal as _signal
qn_signal = _copy_module(_signal, 'signal', {'signal': lambda signum, handler: None,
                                             'getsignal': lambda signum: None,
                                             'alarm': lambda seconds: 0})


class _NetworkDisabled(OSError):
    pass


# Phone notifications: QMT's Python may not use the network, so the production notifier's
# Bark request (to a placeholder base URL) becomes one marked line in QMT's log; a program
# outside QMT forwards marked lines of the live copy with the real Bark address.
QN_NOTIFY_BASE = 'http://a-system-notify.invalid'
QN_NOTIFY_MODE = ['shadow']
QN_NOTIFY_SEQ = [0]


class _QnRequest(object):
    def __init__(self, url, data=None, headers=None, method=None, **kwargs):
        self.full_url = url
        self.method = method or ('POST' if data is not None else 'GET')


class _QnNotifyResponse(object):
    status = 200

    def getcode(self):
        return 200

    def read(self, *args):
        return b'{"code":200,"message":"queued in QMT log"}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _qn_urlopen(req, *args, **kwargs):
    url = getattr(req, 'full_url', req)
    if isinstance(url, str) and url.startswith(QN_NOTIFY_BASE + '/'):
        QN_NOTIFY_SEQ[0] += 1
        line = '[A_SYSTEM_NOTIFY %s] %s-%d %s' % (QN_NOTIFY_MODE[0], QN_RUN[0], QN_NOTIFY_SEQ[0],
                                                  url[len(QN_NOTIFY_BASE):].replace(' ', '%20'))
        _builtins.print(line)
        QN_PRINT_LINES.append(line)
        return _QnNotifyResponse()
    raise _NetworkDisabled('network access is not available in QMT native mode')


import urllib as _urllib
import urllib.parse as _urllib_parse
import urllib.error as _urllib_error
qn_urllib_request = _module('urllib.request', {'urlopen': _qn_urlopen, 'Request': _QnRequest,
                                               'URLError': _urllib_error.URLError,
                                               'HTTPError': _urllib_error.HTTPError})
qn_urllib = _module('urllib', {'parse': _urllib_parse, 'error': _urllib_error, 'request': qn_urllib_request})


def _qn_tushare_unavailable(*args, **kwargs):
    raise RuntimeError('Tushare is not available inside QMT; native QMT data is used instead')


qn_tushare = _module('tushare', {'pro_api': _qn_tushare_unavailable, 'set_token': _qn_tushare_unavailable,
                                 'pro_bar': _qn_tushare_unavailable, '__version__': 'qmt-native-stub'})


def _qn_load_dotenv(dotenv_path=None, *args, **kwargs):
    path = dotenv_path or (QN_ROOT + '/.env')
    if not QN_VFS.isfile(path):
        return False
    for line in QN_VFS.read(path).decode('utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key.startswith('export '):
            key = key[7:].strip()
        value = value.strip().strip('"').strip("'")
        if kwargs.get('override') or key not in QN_ENVIRON:
            QN_ENVIRON[key] = value
    return True


qn_dotenv = _module('dotenv', {'load_dotenv': _qn_load_dotenv,
                               'dotenv_values': lambda *a, **k: {}})
def _qn_retry(*dargs, **dkwargs):
    """tenacity.retry stand-in: only wraps network calls, which cannot run in QMT anyway."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


qn_tenacity = _module('tenacity', dict((n, (lambda *a, **k: None)) for n in (
    'stop_after_attempt', 'wait_exponential', 'wait_fixed', 'retry_if_exception_type', 'wait_random',
    'stop_after_delay', 'before_sleep_log', 'after_log', 'retry_if_result')))
qn_tenacity.retry = _qn_retry
qn_tenacity.RetryError = RuntimeError
qn_getpass = _module('getpass', {'getuser': lambda: 'qmt', 'getpass': lambda *a, **k: ''})
qn_fcntl = _module('fcntl', {'flock': lambda fd, op: None, 'lockf': lambda fd, op, *a: None,
                             'LOCK_EX': 2, 'LOCK_NB': 4, 'LOCK_SH': 1, 'LOCK_UN': 8})


# ---------------------------------------------------------------- dataclasses: field / fields
_QN_MISSING = object()


class _QnField(object):
    def __init__(self, default=_QN_MISSING, default_factory=_QN_MISSING, repr=True, hash=None, init=True,
                 compare=True, metadata=None):
        self.name = None
        self.default = default
        self.default_factory = default_factory
        self.repr = repr
        self.init = init
        self.compare = compare
        self.metadata = metadata or {}


def _qn_field(**kwargs):
    return _QnField(**kwargs)


def _qn_dataclass(cls=None, frozen=False, **unused):
    def wrap(c):
        names = list(c.__dict__.get('__annotations__', {}).keys())
        specs = []
        for n in names:
            raw = c.__dict__.get(n, _QN_MISSING)
            f = raw if isinstance(raw, _QnField) else _QnField(default=raw)
            f.name = n
            specs.append(f)
            if isinstance(raw, _QnField):
                if raw.default is not _QN_MISSING:
                    setattr(c, n, raw.default)
                else:
                    try:
                        delattr(c, n)
                    except AttributeError:
                        pass
        c.__qn_dc_fields__ = tuple(names)
        c.__qn_dc_specs__ = tuple(specs)
        c.__dataclass_fields__ = dict((f.name, f) for f in specs)
        init_specs = [f for f in specs if f.init]

        def __init__(self, *args, **kwargs):
            if len(args) > len(init_specs):
                raise TypeError('%s takes %d positional arguments' % (c.__name__, len(init_specs)))
            values = {}
            for f, v in zip(init_specs, args):
                values[f.name] = v
            for k, v in kwargs.items():
                if k not in [f.name for f in init_specs]:
                    raise TypeError('%s got an unexpected keyword argument %r' % (c.__name__, k))
                if k in values:
                    raise TypeError('%s got multiple values for %r' % (c.__name__, k))
                values[k] = v
            for f in specs:
                if f.name not in values:
                    if f.default is not _QN_MISSING:
                        values[f.name] = f.default
                    elif f.default_factory is not _QN_MISSING:
                        values[f.name] = f.default_factory()
                    else:
                        raise TypeError('%s missing required argument %r' % (c.__name__, f.name))
                object.__setattr__(self, f.name, values[f.name])
            post = getattr(self, '__post_init__', None)
            if post is not None:
                post()

        def _astuple(self):
            return tuple(getattr(self, f.name) for f in specs if f.compare)

        def __repr__(self):
            return '%s(%s)' % (c.__name__, ', '.join('%s=%r' % (f.name, getattr(self, f.name))
                                                      for f in specs if f.repr))

        def __eq__(self, other):
            if other.__class__ is self.__class__:
                return _astuple(self) == _astuple(other)
            return NotImplemented

        c.__init__ = __init__
        if '__repr__' not in c.__dict__:
            c.__repr__ = __repr__
        c.__eq__ = __eq__
        if frozen:
            def __setattr__(self, name, value):
                raise FrozenInstanceError('cannot assign to field %r' % name)

            def __delattr__(self, name):
                raise FrozenInstanceError('cannot delete field %r' % name)
            c.__setattr__ = __setattr__
            c.__delattr__ = __delattr__
            c.__hash__ = lambda self: hash(_astuple(self))
        else:
            c.__hash__ = None
        return c
    return wrap if cls is None else wrap(cls)


def _qn_fields(obj):
    cls = obj if isinstance(obj, type) else type(obj)
    return cls.__dict__['__qn_dc_specs__'] if '__qn_dc_specs__' in cls.__dict__ else tuple(
        getattr(cls, '__qn_dc_specs__', ()))


def _qn_replace(obj, **changes):
    values = dict((f.name, getattr(obj, f.name)) for f in _qn_fields(obj) if f.init)
    values.update(changes)
    return type(obj)(**values)


qn_dataclasses.dataclass = _qn_dataclass
qn_dataclasses.field = _qn_field
qn_dataclasses.fields = _qn_fields
qn_dataclasses.replace = _qn_replace
qn_dataclasses.MISSING = _QN_MISSING
qn_dataclasses.Field = _QnField


# ---------------------------------------------------------------- threads of the embedded system
QN_THREADS = []


class QnThread(_threading.Thread):
    """threading.Thread for the embedded modules: every thread is registered and marked, so a
    stopped strategy can end all of them (QMT keeps the interpreter and its threads alive)."""

    def __init__(self, *args, **kwargs):
        _threading.Thread.__init__(self, *args, **kwargs)
        parent = _threading.current_thread()
        self._qn_run = getattr(parent, '_qn_run', None) if getattr(parent, '_qn_system', False) else QN_RUN[0]
        # kill tokens of every owner up the chain (daemon thread, embedded script)
        self._qn_kill = getattr(parent, '_qn_kill', None) or () if getattr(parent, '_qn_system', False) else ()
        self._qn_system = True
        QN_THREADS.append(self)
        if len(QN_THREADS) > 500:
            QN_THREADS[:] = [t for t in QN_THREADS if t.is_alive()]

    def start(self):
        qn_check_alive()
        return _threading.Thread.start(self)

    def run(self):
        try:
            _threading.Thread.run(self)
        except BaseException as exc:
            if not qn_is_stopped(exc):
                raise
            # the run this thread belongs to has stopped: end quietly


qn_threading = _copy_module(_threading, 'threading', {'Thread': QnThread})


# ---------------------------------------------------------------- host services for embedded code
class QnNativeService(object):
    """Runs QMT data calls on the QMT callback thread in small slices.

    Worker threads (the close pipeline) submit callables and wait; QMT's timer callback
    calls pump(), which executes queued calls within a time budget, so QMT stays
    responsive and no ContextInfo method is called from a foreign thread."""

    def __init__(self):
        self._jobs = []
        self._lock = _threading.Lock()
        self.context = None
        self.globals = None

    def bind(self, context, qmt_globals):
        self.context = context
        self.globals = qmt_globals

    def waiting(self):
        with self._lock:
            return len(self._jobs)

    def call(self, fn, *args, **kwargs):
        timeout = kwargs.pop('_timeout', 180.0)
        qn_check_alive()
        done = _threading.Event()
        box = {}
        with self._lock:
            self._jobs.append((fn, args, kwargs, done, box))
        try:
            finished = _qn_wait(done, timeout)
        except BaseException:
            self._drop(done)  # the caller is gone (stopped or killed): do not run its call later
            raise
        if not finished:
            self._drop(done)
            raise RuntimeError('QMT data call timed out: %s' % getattr(fn, '__name__', fn))
        if 'error' in box:
            raise RuntimeError('QMT data call failed: %s: %s' % (getattr(fn, '__name__', fn), box['error']))
        return box['result']

    def _drop(self, done):
        with self._lock:
            self._jobs = [job for job in self._jobs if job[3] is not done]

    def pump(self, budget_seconds=0.08):
        deadline = _time.time() + budget_seconds
        while _time.time() < deadline:
            with self._lock:
                if not self._jobs:
                    return
                fn, args, kwargs, done, box = self._jobs.pop(0)
            try:
                box['result'] = fn(self.context, self.globals, *args, **kwargs)
            except Exception as exc:
                box['error'] = '%s: %s' % (type(exc).__name__, exc)
            done.set()


QN_NATIVE_SERVICE = QnNativeService()
qn_host_module = _module('qn_host', {'native_service': QN_NATIVE_SERVICE})


# ---------------------------------------------------------------- sys proxy
class _SysProxy(_types.ModuleType):
    def __init__(self, argv, modules):
        _types.ModuleType.__init__(self, 'sys')
        self.argv = list(argv)
        self.path = []
        self.modules = modules
        self.platform = 'linux'

    def __getattr__(self, name):
        return getattr(_sys, name)


# ---------------------------------------------------------------- module loader
class QnCodeBase(object):
    """Embedded sources compiled once and shared by every module set."""

    def __init__(self, modules, root=QN_ROOT):
        self.sources = modules
        self.root = root
        self.codes = {}
        for name, (rel, is_pkg, src) in modules.items():
            self.codes[name] = compile(src, root + '/' + rel, 'exec')
        self.by_path = dict((root + '/' + rel, name) for name, (rel, is_pkg, src) in modules.items())


class QnLoader(object):
    """One module set (like one Python process) running the embedded modules with scoped
    builtins and shims; QMT's own interpreter state is untouched."""

    BLOCKED = ('socket', 'http', 'requests', 'ftplib', 'smtplib', 'multiprocessing', 'xtquant', 'ctypes',
               'msvcrt', 'winreg', '_winapi', 'psutil', 'baostock', 'openpyxl', 'tabulate')

    def __init__(self, codebase, argv=None):
        if isinstance(codebase, dict):
            codebase = QnCodeBase(codebase)
        self.codebase = codebase
        self.sources = codebase.sources
        self.root = codebase.root
        self.codes = codebase.codes
        self.loaded = {}
        self.sys = _SysProxy(argv or ['qn'], self.loaded)
        self.shims = {'os': qn_os, 'os.path': qn_os_path, 'pathlib': qn_pathlib, 'tempfile': qn_tempfile,
                      'glob': qn_glob, 'shutil': qn_shutil, 'logging': qn_logging,
                      'logging.handlers': qn_logging_handlers, 'zoneinfo': qn_zoneinfo,
                      'dataclasses': qn_dataclasses, 'pandas': qn_pandas, 'argparse': _make_argparse(self),
                      'sqlite3': qn_sqlite3, 'subprocess': _make_subprocess(codebase), 'signal': qn_signal,
                      'urllib': qn_urllib, 'urllib.request': qn_urllib_request, 'urllib.parse': _urllib_parse,
                      'urllib.error': _urllib_error, 'tushare': qn_tushare, 'dotenv': qn_dotenv,
                      'getpass': qn_getpass, 'fcntl': qn_fcntl, 'tenacity': qn_tenacity, 'importlib': self._importlib(),
                      'importlib.util': self._importlib().util, 'qn_host': qn_host_module, 'time': qn_time, 'queue': qn_queue,
                      'threading': qn_threading}
        builtins = dict(_builtins.__dict__)
        builtins.update({'open': qn_open, '__import__': self._import, '__qn_to_csv__': __qn_to_csv__,
                         '__qn_to_markdown__': __qn_to_markdown__, '__qn_to_numpy__': __qn_to_numpy__,
                         '__qn_isascii__': __qn_isascii__, '__qn_fromisoformat__': __qn_fromisoformat__,
                         'exit': self._exit, 'quit': self._exit,
                         'print': qn_print})
        self.builtins = builtins

    def _importlib(self):
        if getattr(self, '_qn_importlib', None) is not None:
            return self._qn_importlib
        import importlib as _il
        import importlib.util as _ilu
        loader = self

        class _Spec(object):
            def __init__(self, name, module_name):
                self.name = name
                self.qn_module = module_name
                self.loader = self
                self.origin = loader.root + '/' + loader.sources[module_name][0]

            def exec_module(self, module):
                module.__builtins__ = loader.builtins
                module.__qn_embedded__ = True
                module.__file__ = self.origin
                exec(loader.codes[self.qn_module], module.__dict__)

        def spec_from_file_location(name, location=None, *args, **kwargs):
            path = QN_VFS.abspath(location)
            module_name = loader.codebase.by_path.get(path)
            if module_name is None:
                return None
            return _Spec(name, module_name)

        def module_from_spec(spec):
            mod = _types.ModuleType(spec.name)
            mod.__spec__ = spec
            return mod

        def import_module(name, package=None):
            return loader._import(name, fromlist=('__name__',))

        def reload(module):
            name = module.__name__
            if name in loader.sources:
                loader.loaded.pop(name, None)
                return loader.load(name)
            return _il.reload(module)

        util = _copy_module(_ilu, 'importlib.util', {'spec_from_file_location': spec_from_file_location,
                                                     'module_from_spec': module_from_spec})
        self._qn_importlib = _copy_module(_il, 'importlib', {'import_module': import_module, 'reload': reload,
                                                             'util': util})
        return self._qn_importlib

    @staticmethod
    def _exit(code=None):
        raise SystemExit(code)

    def _new_module(self, name, run_name=None):
        rel, is_pkg, src = self.sources[name]
        mod = _types.ModuleType(run_name or name)
        mod.__file__ = self.root + '/' + rel
        mod.__builtins__ = self.builtins
        mod.__qn_embedded__ = True
        if is_pkg:
            mod.__path__ = [self.root + '/' + _pp.dirname(rel)]
            mod.__package__ = name
        else:
            mod.__package__ = name.rpartition('.')[0]
        return mod

    def load(self, name):
        if name in self.loaded:
            return self.loaded[name]
        if name not in self.sources:
            raise ImportError('No module named %r (not embedded)' % name)
        parent = name.rpartition('.')[0]
        if parent:
            self.load(parent)
        mod = self._new_module(name)
        self.loaded[name] = mod
        try:
            exec(self.codes[name], mod.__dict__)
        except BaseException:
            self.loaded.pop(name, None)
            raise
        if parent:
            setattr(self.loaded[parent], name.rpartition('.')[2], mod)
        return mod

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError('relative imports are not supported')
        top = name.split('.')[0]
        if name == 'sys':
            return self.sys
        if name in self.shims:
            if not fromlist and '.' in name:
                return self.shims.get(top, self.shims[name])
            return self.shims[name]
        if top in self.BLOCKED:
            raise ImportError('module %r is not available in QMT native mode' % name)
        if top in ('src', 'scripts', 'qmt_inner', 'qn'):
            mod = self.load(name)
            for item in fromlist or ():
                sub = name + '.' + item
                if item != '*' and not hasattr(mod, item) and sub in self.sources:
                    self.load(sub)
            return mod if fromlist else self.loaded[top]
        return _builtins.__import__(name, None, None, fromlist, 0)

    def run_main(self, name, argv, raise_on_exit=True):
        """Run an embedded script like `python script.py argv...` in this module set."""
        full_argv = [self.root + '/' + self.sources[name][0]] + list(argv)
        self.sys.argv = full_argv
        previous = getattr(_QN_TLS, 'argv', None)
        _QN_TLS.argv = full_argv
        mod = self._new_module(name, run_name='__main__')
        try:
            exec(self.codes[name], mod.__dict__)
        except SystemExit as exc:
            if not raise_on_exit:
                raise
            code = exc.code
            if code not in (None, 0):
                raise RuntimeError('%s exited with %r' % (name, code))
        finally:
            _QN_TLS.argv = previous
        return mod

    # compatibility with the pipeline runner and tests
    def reset(self, argv):
        self.loaded.clear()
        self.sys.argv = list(argv)

# ---------------------------------------------------------------- close pipeline
LIVE = {
    'limit': 'data/processed/live_limit_up_merged.csv',
    'sentiment': 'data/processed/live_market_sentiment.csv',
    'emotion': 'data/processed/live_market_emotion_features.csv',
    'theme': 'data/processed/live_theme_heat_features.csv',
    'scored': 'data/processed/live_limit_up_fill_scored.csv',
}


def qn_close_steps(day, start):
    """Same arguments the trading daemon passes for the close pipeline steps 2,3,4,6,7."""
    return [
        ('clean', 'scripts.clean_collected_data', ['--start-date', start, '--end-date', day, '--incremental-replace']),
        ('features', 'scripts.build_dynamic_features', ['--start-date', day, '--end-date', day,
                                                        '--limit-up-path', LIVE['limit'],
                                                        '--market-emotion-output', LIVE['emotion'],
                                                        '--theme-heat-output', LIVE['theme']]),
        ('fill', 'scripts.score_limit_up_fill_probability', ['--input-path', LIVE['limit'],
                                                             '--output-path', LIVE['scored'],
                                                             '--market-sentiment-path', LIVE['sentiment']]),
        ('ac', 'scripts.run_paper_ab_filtered_daily_ops', ['--signal-date', day, '--top-n', '10',
                                                          '--input-trades-path', LIVE['scored'],
                                                          '--fill-scored-path', LIVE['scored'],
                                                          '--market-emotion-features-path', LIVE['emotion'],
                                                          '--theme-heat-features-path', LIVE['theme']]),
        ('e', 'scripts.run_strategy_e_signal', ['--signal-date', day]),
    ]


def qn_run_close_pipeline(loader, day, start, only=None, timer=None):
    status = []
    for name, module, argv in qn_close_steps(day, start):
        if only and name not in only:
            continue
        t0 = _time.time()
        QnLoader(loader.codebase).run_main(module, argv)  # fresh module set per step, like a child process
        status.append((name, round(_time.time() - t0, 2)))
    return status
