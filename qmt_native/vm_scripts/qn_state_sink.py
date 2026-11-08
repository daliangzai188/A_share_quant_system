# coding: utf-8
"""A_System state from QMT's log into the registry (11-08).

QMT's key-value store goes through the broker's trade server; a call made while that server drops
may never return and leaves QMT's Python frozen until QMT is restarted and logged in again (10-24,
11-07, 11-08). The QMT-native A_System therefore prints its state as marked lines in QMT's log:

    [A_SYSTEM_STATE live] B <body id> <i>/<n> <base64 zlib chunk>      a file body
    [A_SYSTEM_STATE live] M <manifest id> <i>/<n> <base64 zlib chunk>  the manifest of a snapshot
    [A_SYSTEM_STATE live] C <manifest id>                               commit

The notification forwarder feeds every new piece of the log to StateSink.feed. A body id is the
SHA-256 prefix of the file content, a manifest id that of the packed manifest; both are checked.
Bodies and manifests go to HKCU\\Software\\A_System\\QmtNative\\State<Live|Shadow> as values
B_<id> / M_<id> (long ones split: "chunks:<n>" and <name>.<i>) and to a disk copy. On a commit
whose manifest and bodies are all there, Pointer is set (PointerPrev keeps the one before) and
what neither references is removed; otherwise Missing = "<manifest id> <body id>... [M]" so the
strategy prints those again. A commit older than one already made is never applied.
The strategy reads the snapshot back from the registry when it starts.

Python 3.6+ (the tests run it in QMT's Python), no third-party modules.
"""
import base64
import hashlib
import json
import os
import re
import time
import zlib

STATE_LINE = re.compile(rb'\[A_SYSTEM_STATE (live|shadow)\] (?:(B|M) ([0-9a-f]{32}) (\d+)/(\d+) ([A-Za-z0-9+/=]*)'
                        rb'|C ([0-9a-f]{32}))')
REG_KEY = r'Software\A_System\QmtNative\State'
REG_CHUNK = 60000
# a piece stored this recently is never removed: the manifest that names it may not be read yet
# (11-08 21:09: a commit removed a body of the next snapshot, whose lines came in the same read)
FRESH_SECONDS = 600.0


class DictRegistry(object):
    """The registry key as a dict (tests)."""

    def __init__(self, values=None):
        self.values = values if values is not None else {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)

    def names(self):
        return list(self.values)


class JsonRegistry(DictRegistry):
    """The registry key as a JSON file (the forwarder's tests on a Mac)."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as handle:
                values = json.load(handle)
        except (OSError, ValueError):
            values = {}
        DictRegistry.__init__(self, values)

    def _flush(self):
        with open(self.path + '.tmp', 'w') as handle:
            json.dump(self.values, handle)
        os.replace(self.path + '.tmp', self.path)

    def set(self, name, value):
        DictRegistry.set(self, name, value)
        self._flush()

    def delete(self, name):
        DictRegistry.delete(self, name)
        self._flush()


class WinRegistry(object):
    def __init__(self, mode):
        import winreg
        self.winreg = winreg
        self.path = REG_KEY + ('Live' if mode == 'live' else 'Shadow')

    def _key(self, write=False):
        w = self.winreg
        if write:
            return w.CreateKey(w.HKEY_CURRENT_USER, self.path)
        return w.OpenKey(w.HKEY_CURRENT_USER, self.path)

    def get(self, name):
        try:
            with self._key() as key:
                return str(self.winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    def set(self, name, value):
        with self._key(True) as key:
            self.winreg.SetValueEx(key, name, 0, self.winreg.REG_SZ, value)

    def delete(self, name):
        try:
            with self._key(True) as key:
                self.winreg.DeleteValue(key, name)
        except OSError:
            pass

    def names(self):
        out = []
        try:
            with self._key() as key:
                i = 0
                while True:
                    try:
                        out.append(self.winreg.EnumValue(key, i)[0])
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return out


class StateSink(object):
    def __init__(self, registry, mode='live', disk_dir=None, log=None):
        self.reg = registry
        self.mode = mode
        self.disk = disk_dir
        self.log = log or (lambda text: None)
        self.parts = {}       # (kind, id) -> {index: chunk}, total
        self.waiting = []     # [(seq, manifest id)] commits still lacking something
        self.seq = 0
        self.committed_seq = 0
        self.done = []        # manifest ids committed lately (a re-read log piece never goes back)
        self.fresh = {}       # value name -> when this sink stored it

    # ---------------------------------------------------------------- values
    def _put_blob(self, name, packed):
        if len(packed) <= REG_CHUNK:
            self.reg.set(name, packed)
        else:
            parts = [packed[i:i + REG_CHUNK] for i in range(0, len(packed), REG_CHUNK)]
            for i, part in enumerate(parts):
                self.reg.set('%s.%d' % (name, i), part)
            self.reg.set(name, 'chunks:%d' % len(parts))
        if self.disk:
            folder = os.path.join(self.disk, self.mode)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            target = os.path.join(folder, name)
            with open(target + '.tmp', 'w') as handle:
                handle.write(packed)
            os.replace(target + '.tmp', target)

    def _get_blob(self, name):
        value = self.reg.get(name)
        if value is None or not value.startswith('chunks:'):
            return value
        parts = [self.reg.get('%s.%d' % (name, i)) for i in range(int(value[7:]))]
        return None if any(p is None for p in parts) else ''.join(parts)

    def _drop_blob(self, name):
        value = self.reg.get(name)
        if value is not None and value.startswith('chunks:'):
            for i in range(int(value[7:])):
                self.reg.delete('%s.%d' % (name, i))
        self.reg.delete(name)

    # ---------------------------------------------------------------- lines
    def feed(self, data):
        """Process complete log lines; returns notes for the forwarder's own log."""
        notes, stored = [], False
        for m in STATE_LINE.finditer(data):
            if m.group(1).decode('ascii') != self.mode:
                continue
            if m.group(7):
                mid = m.group(7).decode('ascii')
                if mid not in self.done:
                    self.seq += 1
                    self.waiting.append((self.seq, mid))
                continue
            kind, ident = m.group(2).decode('ascii'), m.group(3).decode('ascii')
            index, total = int(m.group(4)), int(m.group(5))
            item = self.parts.setdefault((kind, ident), {})
            item[index] = m.group(6).decode('ascii')
            if len(item) == total:
                del self.parts[(kind, ident)]
                stored |= self._store(kind, ident, ''.join(item[i] for i in range(1, total + 1)), notes)
        if self.waiting:
            notes.extend(self._try_commits())
        return notes

    def _store(self, kind, ident, packed, notes):
        try:
            if kind == 'B':
                ok = hashlib.sha256(zlib.decompress(base64.b64decode(packed))).hexdigest()[:32] == ident
            else:
                ok = hashlib.sha256(packed.encode('ascii')).hexdigest()[:32] == ident
        except Exception:
            ok = False
        if not ok:
            notes.append('state %s %s damaged in the log; not stored' % (kind, ident[:8]))
            return False
        if self._get_blob(kind + '_' + ident) != packed:
            self._put_blob(kind + '_' + ident, packed)  # new, or a damaged copy replaced
        self.fresh[kind + '_' + ident] = time.time()
        return True

    def _try_commits(self):
        notes, still = [], []
        for seq, mid in self.waiting:
            if seq <= self.committed_seq:
                continue  # an older snapshot than one already committed
            lack = self._lacking(mid)
            if lack:
                still.append((seq, mid))
                continue
            self._commit(mid)
            self.committed_seq = seq
            self.done = (self.done + [mid])[-50:]
            notes.append('state committed: %s' % mid[:8])
        still = [(s, m) for s, m in still if s > self.committed_seq]
        self.waiting = still[-5:]
        if still:
            seq, mid = still[-1]
            lack = self._lacking(mid)
            value = ' '.join([mid] + lack)
            if self.reg.get('Missing') != value:
                self.reg.set('Missing', value)
                notes.append('state %s waits for %d pieces: %s' % (mid[:8], len(lack), ' '.join(x[:8] for x in lack[:5])))
        return notes

    def _manifest(self, mid):
        """The manifest, or None when it is missing or damaged (its id is its checksum)."""
        packed = self._get_blob('M_' + mid)
        if packed is None or hashlib.sha256(packed.encode('ascii')).hexdigest()[:32] != mid:
            return None
        try:
            return json.loads(zlib.decompress(base64.b64decode(packed)).decode('utf-8'))
        except Exception:
            return None

    def _lacking(self, mid):
        if self.reg.get('Pointer') == mid:
            return []
        manifest = self._manifest(mid)
        if manifest is None:
            if self.reg.get('M_' + mid) is not None:
                self._drop_blob('M_' + mid)  # damaged: printed again by the strategy
            return ['M']
        bids = sorted(set(v[0] for v in manifest['files'].values() if v[0]))
        return [b for b in bids if self.reg.get('B_' + b) is None]

    def _commit(self, mid):
        old = self.reg.get('Pointer') or ''
        if old == mid:
            return
        self.reg.set('PointerPrev', old)
        self.reg.set('Pointer', mid)
        self.reg.set('CommittedAt', time.strftime('%Y-%m-%d %H:%M:%S'))
        missing = (self.reg.get('Missing') or '').split()
        if missing and (missing[0] == mid or missing[0] not in set(m for _s, m in self.waiting if m != mid)):
            self.reg.delete('Missing')  # answered, or about a snapshot nothing waits for any more
        if self.disk:
            folder = os.path.join(self.disk, self.mode)
            with open(os.path.join(folder, 'pointer.json.tmp'), 'w') as handle:
                json.dump({'Pointer': mid, 'PointerPrev': old}, handle)
            os.replace(os.path.join(folder, 'pointer.json.tmp'), os.path.join(folder, 'pointer.json'))
        keep = set(['M_' + mid])
        for m in [mid, old] + [w for _s, w in self.waiting]:  # also what later snapshots name
            manifest = self._manifest(m) if m else None
            if manifest:
                keep.add('M_' + m)
                keep.update('B_' + v[0] for v in manifest['files'].values() if v[0])
        waiting = set('M_' + m for _s, m in self.waiting)
        now = time.time()
        self.fresh = dict((k, t) for k, t in self.fresh.items() if now - t < FRESH_SECONDS)
        for name in self.reg.names():
            if (name[:2] in ('B_', 'M_') and '.' not in name and name not in keep and name not in waiting
                    and name not in self.fresh):
                self._drop_blob(name)
        if self.disk:
            # the disk copy keeps a day of older pieces (a damaged registry can be rebuilt from it)
            folder = os.path.join(self.disk, self.mode)
            cutoff = time.time() - 86400
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if name[:2] in ('B_', 'M_') and name not in keep and os.path.getmtime(path) < cutoff:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def restore_from_disk(self):
        """The registry key was lost: put the disk copy of the committed snapshot back."""
        if self.reg.get('Pointer') or not self.disk:
            return ''
        folder = os.path.join(self.disk, self.mode)
        try:
            with open(os.path.join(folder, 'pointer.json')) as handle:
                pointers = json.load(handle)
        except (OSError, ValueError):
            return ''
        restored = 0
        for key in ('PointerPrev', 'Pointer'):
            mid = pointers.get(key)
            if not mid:
                continue
            try:
                with open(os.path.join(folder, 'M_' + mid)) as handle:
                    mpacked = handle.read()
                manifest = json.loads(zlib.decompress(base64.b64decode(mpacked)).decode('utf-8'))
                self._put_blob('M_' + mid, mpacked)
                for v in manifest['files'].values():
                    if v[0]:
                        with open(os.path.join(folder, 'B_' + v[0])) as handle:
                            self._put_blob('B_' + v[0], handle.read())
                self.reg.set(key, mid)
                restored += 1
            except (OSError, ValueError, KeyError):
                continue
        return 'state restored into the registry from the disk copy (%d snapshots)' % restored if restored else ''
