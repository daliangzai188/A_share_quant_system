# coding: utf-8
# QMT strategy host: boots the unchanged A_System daemon inside QMT's built-in Python.
#   * the qmt_inner execution engine runs on QMT's callback thread (pump every 100 ms),
#     exactly as the production bridge strategy did;
#   * the trading daemon runs on a background thread and reaches the engine through the
#     same request/response protocol, carried by the in-memory file store instead of disk;
#   * close-pipeline step 1 reads QMT data through QN_NATIVE_SERVICE on the callback thread;
#   * state survives restarts through QMT's get_py_key/save_py_key (no files, no network).
import base64 as _hb64
import csv as _hcsv
import datetime as _hdt
import hashlib as _hsha
import io as _hio
import json as _hjson
import os as _hos
import random as _hrandom
import re as _hre
import threading as _hthr
import time as _htime
import traceback as _htb
import urllib.parse as _hurlparse
import zlib as _hzlib

QN_STATE_PREFIX = 'AS_QN'
QN_STATE_CHUNK = 4500  # QMT get/save_py_key: at most 5000 bytes per value, ~0.25 s per call
# Only irreplaceable state is persisted. Market data, partitions and signals are rebuilt
# from QMT data by the daemon's own start-up catch-up when they are missing.
QN_PERSIST_INCLUDE = (
    _hre.compile(r'^data/state/'),
    _hre.compile(r'^data/processed/[^/]+\.json$'),
    _hre.compile(r'^reports/account_risk_shadow/'),
    _hre.compile(r'^reports/strategy_e/[^/]+\.json$'),
    _hre.compile(r'^reports/strategy_m/[^/]+\.json$'),
    _hre.compile(r'^reports/equity_curve_stop/'),
    _hre.compile(r'^reports/live_trade/qmt_live_order_submitted_orders\.csv$'),
    _hre.compile(r'^reports/strategy_equity/'),
    _hre.compile(r'^logs/(qmt_last_success|broker_health)\.json$'),
    _hre.compile(r'^logs/post_market_done_[0-9]{8}\.marker$'),
    _hre.compile(r'^runtime/qmt_inner/spool/journal\.sqlite3$'),
    _hre.compile(r'^qn_state/equity_stop/(state\.json|asof_delta_[0-9]{8}\.csv|sentiment_history\.csv)$'),
    _hre.compile(r'^data/raw/limit_list/[0-9]{8}\.csv$'),
    # accumulated trade records and the day's cached combined plan (09:00 plan, 09:20 orders)
    _hre.compile(r'^reports/execution_tracking/[^/]+\.(csv|json)$'),
    _hre.compile(r'^reports/live_execution_audit\.csv$'),
    _hre.compile(r'^reports/live_trade/combined/combined_(decisions|planned_orders|state)_[0-9]{8}\.csv$'),
    _hre.compile(r'^reports/strategy_d/intraday_signals_[0-9]{8}\.csv$'),
)
# Rebuilt after a restart (content not persisted); their inventory is, so identical rebuilds
# keep their mtime and the producer identity of step 13 stays the same across restarts.
QN_INVENTORY_INCLUDE = (
    _hre.compile(r'^data/raw/(daily|daily_basic|adj_factor)/[0-9]{8}\.csv$'),
    _hre.compile(r'^data/raw/[^/]+\.csv$'),
)
# Files a program outside QMT needs (from 2026-10-23 QMT cannot write files): written to QMT's
# log as marked chunks whenever they change; vm_scripts/qmt_notify_forwarder.py rebuilds them.
QN_EXPORT_FILES = ('reports/execution_tracking/trade_completion_summary.csv',)
QN_EXPORT_CHUNK = 1500
QN_PERSIST_SKIP_TABLES = {'runtime/qmt_inner/spool/journal.sqlite3': ('callbacks',)}
QN_SEED_MTIME = 1.5e9


def _bj_now():
    return _hdt.datetime.now(_hdt.timezone(_hdt.timedelta(hours=8)))


# Trading switch (10-27, the user's decision): QMT's built-in Python may not read files or use the
# network, so start_windows.py / stop_windows.py (outside QMT) leave the switch in the Windows
# registry: HKCU\\Software\\A_System\\QmtNative, value Trading = "run" / "pause". The live copy loads
# with QMT but runs its trading daemon only while the switch says "run"; anything else (not set,
# "pause", unreadable) keeps it paused. QMT itself stays open either way. QN_SWITCH_READER[0] may be
# replaced in tests; without winreg (not Windows) the switch reads "run".
QN_SWITCH_KEY = r'Software\A_System\QmtNative'
QN_SWITCH_READER = [None]


def _qn_read_switch():
    if QN_SWITCH_READER[0] is not None:
        return QN_SWITCH_READER[0]()
    try:
        import winreg
    except ImportError:
        return 'run'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, QN_SWITCH_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, 'Trading')
        return str(value).strip().lower()
    except OSError:
        return ''


# Trade calendar feed (11-05). QMT's built-in Python only knows trading days up to today
# (get_trading_dates returns nothing after it), and from 10-23 it may not reach Tushare, so the
# calendar shipped in the strategy file (to 20261231) would run out: from 12-02 the daemon's refresh
# fails and it guesses weekdays, from 2027 the collector finds no day to collect. The notification
# forwarder (outside QMT, network allowed) writes Tushare's SSE calendar into the same registry key
# as the trading switch: "v1|SSE|<first day YYYYMMDD>|<one 0/1 per calendar day>|<sha256[:8]>".
QN_CALENDAR_VALUE = 'TradeCalendar'
QN_CALENDAR_READER = [None]


def _qn_read_calendar_feed():
    if QN_CALENDAR_READER[0] is not None:
        return QN_CALENDAR_READER[0]()
    try:
        import winreg
    except ImportError:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, QN_SWITCH_KEY) as key:
            return str(winreg.QueryValueEx(key, QN_CALENDAR_VALUE)[0])
    except OSError:
        return ''


def _qn_parse_calendar_feed(text):
    """{YYYYMMDD: is_open} from a feed, or (None, reason). Weekends are never open."""
    parts = str(text).strip().split('|')
    if len(parts) != 5 or parts[0] != 'v1' or parts[1] != 'SSE':
        return None, 'unknown format'
    start, bits, digest = parts[2], parts[3], parts[4]
    if _hsha.sha256((start + bits).encode('ascii', 'replace')).hexdigest()[:8] != digest:
        return None, 'checksum mismatch'
    if not 60 <= len(bits) <= 1200 or set(bits) - set('01'):
        return None, 'bad day flags'
    try:
        first = _hdt.datetime.strptime(start, '%Y%m%d').date()
    except ValueError:
        return None, 'bad first day'
    days = {}
    for i, flag in enumerate(bits):
        day = first + _hdt.timedelta(days=i)
        if flag == '1' and day.weekday() >= 5:
            return None, 'weekend %s marked open' % day.strftime('%Y%m%d')
        days[day.strftime('%Y%m%d')] = flag == '1'
    return days, ''


QN_SWITCH_CHANGED_READER = [None]


def _qn_switch_changed_at():
    """'YYYY-MM-DD HH:MM:SS' (Beijing) of the last start/stop_windows.py, or ''."""
    if QN_SWITCH_CHANGED_READER[0] is not None:
        return QN_SWITCH_CHANGED_READER[0]()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, QN_SWITCH_KEY) as key:
            return str(winreg.QueryValueEx(key, 'ChangedAt')[0])
    except Exception:
        return ''


def _qn_switch_detail():
    """When and by whom the trading switch was last set (written by start/stop_windows.py)."""
    if QN_SWITCH_READER[0] is not None:
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, QN_SWITCH_KEY) as key:
            at = str(winreg.QueryValueEx(key, 'ChangedAt')[0])
            by = str(winreg.QueryValueEx(key, 'ChangedBy')[0])
        return '%s 由 %s 设置' % (at[5:] if len(at) > 5 else at, by)
    except Exception:
        return ''


def _status_text(status):
    return ', '.join('%s=%s' % kv for kv in status) or 'no status field'


# account statuses that mean the broker's trade server is not connected (seen: 登录失败, 初始化中)
QN_LINK_DOWN_MARKS = (u'\u5931\u8d25', u'\u521d\u59cb\u5316', u'\u65ad\u5f00', u'\u672a\u8fde\u63a5', u'\u672a\u767b\u5f55')


QN_LINK_DOWN_TEXT = 'stopped before the call'


def _status_up(status):
    """An account status that does not say the trade server is down (no status field: up)."""
    text = _status_text(status or ())
    return not any(mark in text for mark in QN_LINK_DOWN_MARKS)


def _qn_where(frame, depth):
    """'file:line function' of the innermost frames, read from the frames alone (traceback's
    stack summaries look up source lines, which may read files from disk)."""
    out = []
    while frame is not None and len(out) < depth:
        code = frame.f_code
        out.append('%s:%s %s' % (code.co_filename.replace('\\', '/').rsplit('/', 1)[-1], frame.f_lineno, code.co_name))
        frame = frame.f_back
    return ' <- '.join(out)


class QnStateStore(object):
    """Content-addressed VFS snapshot in QMT's key-value store.

    Each file body is stored once (zlib + base64, split into chunks) under a body id: its
    SHA-256 prefix plus a fresh tag per write; a manifest maps path -> (body id, mtime, size,
    chunks) and gets a fresh id per save too. The manifest is written last and read back, so a
    snapshot only counts once complete. Bodies and manifests no longer referenced by the
    current or previous snapshot are cleared, and a cleared key is never written again: on
    10-23 a clear that the trade server delivered late landed on a key written again since,
    and broke both snapshots. Body ids without a tag and the manifest slots A/B of older
    releases still load."""

    def __init__(self, getter, saver, prefix=QN_STATE_PREFIX):
        self.getter = getter
        self.saver = saver
        self.prefix = prefix
        self.known = {}  # sha -> (body id, chunks) stored and referenced by a snapshot
        self.previous = {}
        self.slot = ''
        self.pointer = ''
        self.prev_pointer = ''  # the snapshot before self.pointer (PTRPREV)
        self.protected = set()  # bodies of a snapshot whose pointer write did not confirm
        # A key-value call blocks QMT's whole Python until the trade server answers (up to about
        # a minute when it is flaky). The caller reports each call's duration in
        # last_call_seconds; during a save, one call slower than slow_limit gives the save up.
        self.slow_limit = 0.0
        self.last_call_seconds = 0.0
        # any call slower than slow_mark (set by the host) is remembered: the save before an order
        # is skipped for a while after one (QnHost.persist_before_order)
        self.slow_mark = 0.0
        self.last_slow_at = 0.0
        self.last_slow_seconds = 0.0
        # set by the host: False while the account status says the trade server is down. Asked
        # before EVERY key-value call (11-08 11:08: the server dropped in the middle of a periodic
        # save that had passed the check at its start; the next save_py_key never returned)
        self.link_check = None
        self.saving = False
        self.call_seconds = 0.0  # running total, for the host's status line
        # (sha, path) -> bytes or None: a body lost in the store whose content is shipped in the
        # strategy file (same checksum), or a file an operator named for this one start, is taken
        # from the seed and written again by the next save
        self.fallback_bodies = None
        self.repaired = []
        # bodies the current trade server already holds, from its own last snapshot (follow_server)
        self.candidates = {}

    def _check_slow(self, what):
        self.call_seconds += self.last_call_seconds
        if self.slow_mark and self.last_call_seconds > self.slow_mark:
            self.last_slow_at = _htime.time()
            self.last_slow_seconds = self.last_call_seconds
        if self.saving and self.slow_limit and self.last_call_seconds > self.slow_limit:
            raise RuntimeError('%s took %.0fs (broker trade server slow); save given up' % (
                what, self.last_call_seconds))

    def _link_down(self, what):
        if self.link_check is not None and not self.link_check():
            raise RuntimeError('%s not made: trade connection down (account status); %s' % (what, QN_LINK_DOWN_TEXT))

    def _get(self, key):
        self.last_call_seconds = 0.0
        self._link_down('get_py_key(%s)' % key)
        result = self.getter(key)
        self._check_slow('get_py_key(%s)' % key)
        if isinstance(result, (tuple, list)):
            if len(result) == 3 and result[1] != 0:
                message = str(result[2])
                # QMT reports a key that was never written as (-1, 'KeyError: ... not exist ...')
                if 'KeyError' in message or u'\u4e0d\u5b58\u5728' in message:
                    return ''
            if len(result) != 3 or result[1] != 0:
                raise RuntimeError('get_py_key(%s) failed: %r' % (key, result[1:] if len(result) == 3 else result))
            return result[0] or ''
        return result or ''

    def _set(self, key, value):
        self.last_call_seconds = 0.0
        self._link_down('save_py_key(%s)' % key)
        result = self.saver(key, value)
        self._check_slow('save_py_key(%s)' % key)
        if isinstance(result, (tuple, list)) and len(result) >= 1 and result[0] not in (0, None):
            raise RuntimeError('save_py_key(%s, %d chars) rejected: %r' % (key, len(value), result))
        return result

    @staticmethod
    def _tag():
        return '%08x' % _hrandom.getrandbits(32)

    def _body_keys(self, bid, chunks):
        return ['%s_F_%s_%d' % (self.prefix, bid, i) for i in range(chunks)]

    def follow_server(self):
        """QMT keeps one key-value store per trade server and switches between its servers on
        failures; nothing is replicated (11-05: five servers; at 08:47 a switch made the next saves
        write snapshots whose earlier bodies only existed on the first server, at 16:42 a start read
        that server's 08:44 snapshot). Before every save: read this server's pointer. Our own pointer
        means the same server. Anything else means another server: nothing is known to be on it any
        more; its own last snapshot names the bodies it holds, and a body is reused after a full read
        that matches its checksum, so only missing or changed bodies are written. Returns a log line
        when the server changed, else ''."""
        here = self._get(self.prefix + '_PTR')
        if not here and self.pointer:
            here = self._get(self.prefix + '_PTR')  # one empty read may be a flaky answer
        if here == self.pointer:
            return ''
        candidates, files_here = {}, {}
        if here:
            try:
                slot, chunks, digest = here.split(':')
                packed = ''.join(self._get('%s_M%s_%d' % (self.prefix, slot, i)) for i in range(int(chunks)))
                if _hsha.sha256(packed.encode('ascii')).hexdigest()[:32] == digest:
                    meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(packed)).decode('utf-8'))
                    files_here = meta.get('files') or {}
                    for rel, (bid, mtime, size, n) in files_here.items():
                        if bid:
                            candidates[bid[:32]] = (bid, n)
            except Exception as exc:
                if self._read_failure(exc):
                    raise
                files_here = {}
        ours = self.pointer
        self.known = {}
        self.protected = set()
        self.candidates = candidates
        self.previous = files_here  # its own last snapshot stays loadable until ours is complete here
        self.prev_pointer = ''      # the manifest before ours lives on the other server: not cleared here
        self.pointer = here         # becomes this server's PTRPREV with our next snapshot
        return ('trade server changed: its last snapshot %s, ours %s; %d bodies held there are checked and '
                'reused, the rest written again' % (here.split(':')[0] or 'none', ours.split(':')[0] or 'none',
                                                    len(candidates)))

    def _put_body(self, data):
        sha = _hsha.sha256(data).hexdigest()[:32]
        if sha in self.known:
            return self.known[sha]
        held = self.candidates.get(sha)
        if held is not None:
            try:
                self._get_body(*held)  # every chunk read back and the checksum compared
                self.known[sha] = held
                return held
            except Exception as exc:
                if self._read_failure(exc):
                    raise
                self.candidates.pop(sha, None)  # incomplete on this server: written again below
        packed = _hb64.b64encode(_hzlib.compress(data, 6)).decode('ascii')
        chunks = [packed[i:i + QN_STATE_CHUNK] for i in range(0, len(packed), QN_STATE_CHUNK)] or ['']
        bid = sha + 'x' + self._tag()
        keys = self._body_keys(bid, len(chunks))
        for key, chunk in zip(keys, chunks):
            self._set(key, chunk)
        for key, chunk in zip(keys, chunks):
            if self._get(key) != chunk:
                raise RuntimeError('state body %s did not read back' % key)
        self.known[sha] = (bid, len(chunks))
        return bid, len(chunks)

    def _get_body(self, bid, chunks):
        packed = ''.join(self._get(k) for k in self._body_keys(bid, chunks))
        data = _hzlib.decompress(_hb64.b64decode(packed))
        if _hsha.sha256(data).hexdigest()[:32] != bid[:32]:
            raise RuntimeError('persisted state body %s checksum mismatch' % bid)
        return data

    def _put_text_chunks(self, base, text):
        chunks = [text[i:i + QN_STATE_CHUNK] for i in range(0, len(text), QN_STATE_CHUNK)] or ['']
        for i, chunk in enumerate(chunks):
            self._set('%s_%d' % (base, i), chunk)
        for i, chunk in enumerate(chunks):
            if self._get('%s_%d' % (base, i)) != chunk:
                raise RuntimeError('state key %s_%d did not read back' % (base, i))
        return len(chunks)

    def save(self, files, meta_extra):
        """files: {path: (bytes or None for a stub, mtime, size)}; meta_extra may carry an
        'inventory' {path: [mtime, size, sha]} of rebuilt files (no bodies)."""
        self.saving = True
        try:
            return self._save(files, meta_extra)
        finally:
            self.saving = False

    def _save(self, files, meta_extra):
        manifest = {}
        for rel, (data, mtime, size) in sorted(files.items()):
            if data is None:
                manifest[rel] = [None, mtime, size, 0]
            else:
                bid, chunks = self._put_body(data)
                manifest[rel] = [bid, mtime, size, chunks]
        text = _hjson.dumps(dict(meta_extra, files=manifest, saved_at=_bj_now().strftime('%Y-%m-%d %H:%M:%S')),
                            sort_keys=True)
        packed = _hb64.b64encode(_hzlib.compress(text.encode('utf-8'), 9)).decode('ascii')
        mid = 'g' + self._tag()  # a fresh manifest id per save, never one of an earlier save
        chunks = self._put_text_chunks('%s_M%s' % (self.prefix, mid), packed)
        pointer = '%s:%d:%s' % (mid, chunks, _hsha.sha256(packed.encode('ascii')).hexdigest()[:32])
        bodies = set((v[0], v[3]) for v in manifest.values() if v[0])
        if self.pointer:
            # the snapshot in use until now stays loadable if the new one ever is not
            self._set(self.prefix + '_PTRPREV', self.pointer)
        try:
            self._set(self.prefix + '_PTR', pointer)
            if self._get(self.prefix + '_PTR') != pointer:
                raise RuntimeError('state pointer did not read back')
        except Exception:
            self.protected |= bodies  # the pointer may already name this snapshot
            raise
        retired = self.prev_pointer
        self.slot = mid
        self.prev_pointer, self.pointer = self.pointer, pointer
        older, self.previous = self.previous, manifest
        live = bodies | self.protected | set((v[0], v[3]) for v in older.values() if v[0])
        # Clear what neither the new nor the previous snapshot references: bodies, and the
        # manifest of the snapshot before the previous one. Forget a body before clearing it;
        # a cleared key is never written again (a content needed later gets a new body id).
        try:
            for sha, (bid, n) in list(self.known.items()):
                if (bid, n) in live:
                    continue
                del self.known[sha]
                for key in self._body_keys(bid, n):
                    self._set(key, '')
            if retired and retired not in (self.prev_pointer, self.pointer):
                slot, n, _ = retired.split(':')
                if slot not in ('A', 'B'):
                    for i in range(int(n)):
                        self._set('%s_M%s_%d' % (self.prefix, slot, i), '')
        except Exception:
            pass  # clean-up only; leftovers are harmless
        return dict(meta_extra, inventory=len(meta_extra.get('inventory') or {}), files=len(manifest),
                    bodies=len(self.known), bytes=len(packed),
                    saved_at=_bj_now().strftime('%Y-%m-%d %H:%M:%S'))

    MAX_MANIFEST_CHUNKS = 200
    EMPTY_CONFIRM_ROUNDS = 3
    EMPTY_CONFIRM_WAIT = 2.0

    def load(self):
        """The latest snapshot, or the one before it when the latest cannot be read completely, or
        else the newest manifest slot that is complete on its own (zlib stream and JSON intact,
        every file body matching its checksum). Never a partial state."""
        pointer = self._get(self.prefix + '_PTR')
        previous = self._get(self.prefix + '_PTRPREV')
        for attempt in range(self.EMPTY_CONFIRM_ROUNDS):
            if pointer:
                break
            # A flaky trade server can hand back empty values (10-23 15:53 and 16:31: starts from the
            # seed right after reads of the same pointer). Only a store with no pointer and no
            # manifest, read so several times, is new; anything else is retried later.
            leftovers = [k for k in ('_MA_0', '_MB_0') if self._get(self.prefix + k)] + (['_PTRPREV'] if previous else [])
            if leftovers:
                raise RuntimeError('state pointer empty but %s present; not starting from the seed' % ', '.join(leftovers))
            if attempt + 1 < self.EMPTY_CONFIRM_ROUNDS:
                _htime.sleep(self.EMPTY_CONFIRM_WAIT)
                pointer = self._get(self.prefix + '_PTR')
                previous = self._get(self.prefix + '_PTRPREV')
        if not pointer:
            return None, None
        errors = []
        candidates = [('latest', pointer)] + ([('previous snapshot', previous)] if previous and previous != pointer else [])
        for label, candidate in candidates:
            try:
                files, meta = self._load_pointer(candidate)
                self.prev_pointer = previous if label == 'latest' and previous != pointer else ''
                if errors:
                    meta['restored_from'] = '%s (%s)' % (label, '; '.join(errors))
                return files, meta
            except Exception as exc:
                if self._read_failure(exc):
                    raise  # the server did not answer: unreadable for now, not a broken snapshot
                errors.append('%s %s unreadable: %s: %s' % (label, candidate.split(':')[0], type(exc).__name__, exc))
                self.known, self.previous = {}, {}
        # Neither pointer names a complete snapshot: a write that outlived its save's wait can land
        # after a later save (10-23: both snapshots broken after an afternoon of 5-60 s calls).
        best = None
        for slot in ('A', 'B'):
            try:
                found = self._scan_slot(slot)
                if best is None or found[2] > best[2]:
                    best = found
            except Exception as exc:
                if self._read_failure(exc):
                    raise
                errors.append('slot %s: %s' % (slot, exc))
        if best is not None:
            try:
                files, meta = self._load_pointer(best[0])
                meta['restored_from'] = 'manifest slot %s saved at %s (%s)' % (best[0][:1], best[2], '; '.join(errors))
                return files, meta
            except Exception as exc:
                if self._read_failure(exc):
                    raise
                errors.append('slot %s files: %s: %s' % (best[0][:1], type(exc).__name__, exc))
                self.known, self.previous = {}, {}
        raise RuntimeError('no complete state snapshot; refusing a partial state (%s)' % '; '.join(errors))

    @staticmethod
    def _read_failure(exc):
        text = str(exc)
        return text.startswith('get_py_key(') or 'QMT data call' in text

    def _scan_slot(self, slot):
        """Read a manifest slot chunk by chunk until the chunks read so far form a complete zlib
        stream holding a manifest. Chunks of two different saves never decode together."""
        packed = ''
        for i in range(self.MAX_MANIFEST_CHUNKS):
            chunk = self._get('%s_M%s_%d' % (self.prefix, slot, i))
            if not chunk:
                break
            packed += chunk
            try:
                meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(packed)).decode('utf-8'))
            except Exception:
                continue
            if isinstance(meta, dict) and isinstance(meta.get('files'), dict):
                pointer = '%s:%d:%s' % (slot, i + 1, _hsha.sha256(packed.encode('ascii')).hexdigest()[:32])
                return pointer, meta, str(meta.get('saved_at', ''))
        raise RuntimeError('no complete manifest in slot %s' % slot)

    def verify(self, pointer=None):
        """Read the committed snapshot back completely without touching this store's state.
        Returns '' when it loads, else the reason."""
        pointer = pointer or self.pointer
        if not pointer:
            return ''
        try:
            slot, chunks, digest = pointer.split(':')
            packed = ''.join(self._get('%s_M%s_%d' % (self.prefix, slot, i)) for i in range(int(chunks)))
            if _hsha.sha256(packed.encode('ascii')).hexdigest()[:32] != digest:
                return 'manifest checksum mismatch'
            meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(packed)).decode('utf-8'))
            for rel, (bid, mtime, size, n) in sorted(meta['files'].items()):
                if bid is not None:
                    self._get_body(bid, n)
            return ''
        except Exception as exc:
            return '%s: %s' % (type(exc).__name__, exc)

    def forget_bodies(self):
        """The next save writes every file body again (repairs a damaged committed snapshot)."""
        self.known = {}

    def _load_pointer(self, pointer):
        slot, chunks, digest = pointer.split(':')
        packed = ''.join(self._get('%s_M%s_%d' % (self.prefix, slot, i)) for i in range(int(chunks)))
        if _hsha.sha256(packed.encode('ascii')).hexdigest()[:32] != digest:
            raise RuntimeError('persisted state manifest checksum mismatch; refusing a partial state')
        meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(packed)).decode('utf-8'))
        self.slot = slot
        files = {}
        for rel, (bid, mtime, size, chunks) in meta['files'].items():
            if bid is None:
                files[rel] = (None, mtime, size)
            else:
                try:
                    data = self._get_body(bid, chunks)
                    self.known[bid[:32]] = (bid, chunks)
                except Exception:
                    # 10-23: one body of both snapshots read back empty (a clear of the same key
                    # landed after its rewrite). Not in known: the next save writes it again.
                    data = self.fallback_bodies(bid[:32], rel) if self.fallback_bodies else None
                    if data is None:
                        raise
                    self.repaired.append(rel)
                    size = len(data)
                files[rel] = (data, mtime, size)
        self.previous = meta['files']
        self.pointer = pointer
        self.candidates = {}
        return files, dict((k, v) for k, v in meta.items() if k != 'files')


# State store without the broker (11-08). QMT's key-value store goes through the broker's trade
# server: a call made while that server drops may never return, and QMT's Python then stays frozen
# until QMT is restarted and logged in again (10-24, 11-07 11:54, 11-08 11:08). The state now goes
# out as marked lines in QMT's own log (a local write that cannot hang):
#     [A_SYSTEM_STATE live] B <body id> <i>/<n> <base64 zlib chunk>     a file body
#     [A_SYSTEM_STATE live] M <manifest id> <i>/<n> <base64 zlib chunk> the manifest of a snapshot
#     [A_SYSTEM_STATE live] C <manifest id>                              commit
# The notification forwarder (outside QMT) stores bodies and manifests on disk and in the registry
# key below, and on a commit whose bodies are all there sets Pointer (PointerPrev keeps the one
# before); bodies a commit lacks are named in Missing and printed again. A start reads the snapshot
# back from the registry (a local read, like the trading switch). Ids are checksums of the content.
QN_STATE_REG_KEY = QN_SWITCH_KEY + r'\State'
QN_STATE_READER = [None]  # tests: function(value name) -> str or None
QN_STATE_TAP = [None]     # tests: also called with every printed state line


def _qn_state_print(line):
    print(line)
    if QN_STATE_TAP[0] is not None:
        QN_STATE_TAP[0](line)


def _qn_read_state_value(key_path, name):
    if QN_STATE_READER[0] is not None:
        return QN_STATE_READER[0](name)
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return value if isinstance(value, str) else str(value)
    except OSError:
        return None


class QnLogStore(object):
    log_store = True  # saves never go through the broker
    CHUNK = 1500
    RESEND_SECONDS = 30.0
    RESEND_ALL_SECONDS = 300.0  # no answer at all (the forwarder skipped the lines): print it all again
    UNACKED_WARN_SECONDS = 600.0

    def __init__(self, mode, legacy=None, printer=None, key=None):
        self.mode = 'live' if mode == 'live' else 'shadow'
        self.key = key or (QN_STATE_REG_KEY + ('Live' if self.mode == 'live' else 'Shadow'))
        self.legacy = legacy          # the broker key-value store, read once to move its state here
        self.printer = printer or _qn_state_print
        self.acked = set()            # body ids the registry holds (committed snapshot and the one before)
        self.committed_bids = set()
        self.pending = None           # the last snapshot printed and not yet committed
        self.print_all_next = False   # the next save prints every body (the save on stop: nothing can be
                                      # printed again after it)
        self.pointer = ''
        self.prev_pointer = ''
        self.warned = False
        # attributes the host shares with the broker store
        self.known, self.previous, self.candidates, self.repaired = {}, {}, {}, []
        self.fallback_bodies = None
        self.saving = False
        self.slow_limit = self.slow_mark = 0.0
        self.last_slow_at = self.last_slow_seconds = 0.0
        self.last_call_seconds = self.call_seconds = 0.0
        self.link_check = None

    # ---------------------------------------------------------------- registry (read only)
    def _read(self, name):
        return _qn_read_state_value(self.key, name)

    def _read_blob(self, name):
        """A value written by the forwarder: the packed string, or 'chunks:<n>' and <name>.<i>."""
        value = self._read(name)
        if value is None:
            return None
        if value.startswith('chunks:'):
            parts = [self._read('%s.%d' % (name, i)) for i in range(int(value[7:]))]
            if any(p is None for p in parts):
                return None
            value = ''.join(parts)
        return value

    def uses_broker(self):
        """True only before the first snapshot of this store is committed: the start then reads
        the broker key-value store once (with the trade connection checks)."""
        return self.legacy is not None and not self._read('Pointer')

    # ---------------------------------------------------------------- save
    def _emit(self, kind, ident, packed):
        chunks = [packed[i:i + self.CHUNK] for i in range(0, len(packed), self.CHUNK)] or ['']
        for i, chunk in enumerate(chunks):
            self.printer('[A_SYSTEM_STATE %s] %s %s %d/%d %s' % (self.mode, kind, ident, i + 1, len(chunks), chunk))
        return len(chunks)

    def save(self, files, meta_extra):
        manifest, data_by_bid = {}, {}
        for rel, (data, mtime, size) in sorted(files.items()):
            if data is None:
                manifest[rel] = [None, mtime, size, 0]
                continue
            bid = _hsha.sha256(data).hexdigest()[:32]
            manifest[rel] = [bid, mtime, size, 0]
            data_by_bid[bid] = data
        saved_at = _bj_now().strftime('%Y-%m-%d %H:%M:%S')
        text = _hjson.dumps(dict(meta_extra, files=manifest, saved_at=saved_at), sort_keys=True)
        mpacked = _hb64.b64encode(_hzlib.compress(text.encode('utf-8'), 9)).decode('ascii')
        mid = _hsha.sha256(mpacked.encode('ascii')).hexdigest()[:32]
        printed = 0
        print_all, self.print_all_next = self.print_all_next, False
        for bid in sorted(data_by_bid):
            # skipped only when the registry holds it (the forwarder may have removed an older piece)
            if print_all or bid not in self.acked or self._read('B_' + bid) is None:
                printed += 1
                self._emit('B', bid, _hb64.b64encode(_hzlib.compress(data_by_bid[bid], 9)).decode('ascii'))
        self._emit('M', mid, mpacked)
        self.printer('[A_SYSTEM_STATE %s] C %s' % (self.mode, mid))
        self.pending = dict(mid=mid, data=data_by_bid, manifest=mpacked, at=_htime.time(), sent=_htime.time())
        return dict(meta_extra, inventory=len(meta_extra.get('inventory') or {}), files=len(manifest),
                    bodies=printed, bytes=len(mpacked), saved_at=saved_at)

    def service(self):
        """Called every few seconds: a commit of the pending snapshot updates what the registry
        holds; bodies the forwarder names missing are printed again. Returns a log line or ''."""
        pending = self.pending
        if not pending:
            return ''
        pointer = self._read('Pointer')
        if pointer == pending['mid']:
            self.acked = set(pending['data']) | self.committed_bids
            self.committed_bids = set(pending['data'])
            self.prev_pointer, self.pointer = self.pointer, pointer
            self.pending = None
            self.warned = False
            return ''
        now = _htime.time()
        missing = (self._read('Missing') or '').split()
        if (not missing or missing[0] != pending['mid']) and now - pending['sent'] >= self.RESEND_ALL_SECONDS:
            missing = [pending['mid'], 'M'] + sorted(b for b in pending['data'] if b not in self.committed_bids)
            pending['sent'] = 0.0
        if missing and missing[0] == pending['mid'] and now - pending['sent'] >= self.RESEND_SECONDS:
            pending['sent'] = now
            again = [b for b in missing[1:] if b in pending['data']]
            for bid in again:
                self.acked.discard(bid)
                self._emit('B', bid, _hb64.b64encode(_hzlib.compress(pending['data'][bid], 9)).decode('ascii'))
            if 'M' in missing[1:]:
                self._emit('M', pending['mid'], pending['manifest'])
            self.printer('[A_SYSTEM_STATE %s] C %s' % (self.mode, pending['mid']))
            return 'state store: printed again what the forwarder lacked (%d bodies)' % len(again)
        if now - pending['at'] > self.UNACKED_WARN_SECONDS and not self.warned:
            self.warned = True
            return ('STATE NOT CONFIRMED: snapshot %s printed %.0f min ago is not in the registry yet (is the '
                    'notification forwarder running?)' % (pending['mid'][:8], (now - pending['at']) / 60))
        return ''

    def confirmed(self):
        return self.pending is None

    # ---------------------------------------------------------------- load
    def _load_mid(self, mid):
        mpacked = self._read_blob('M_' + mid)
        if not mpacked or _hsha.sha256(mpacked.encode('ascii')).hexdigest()[:32] != mid:
            raise RuntimeError('manifest %s missing or damaged' % mid[:8])
        meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(mpacked)).decode('utf-8'))
        files, bids = {}, set()
        for rel, (bid, mtime, size, _n) in meta['files'].items():
            if bid is None:
                files[rel] = (None, mtime, size)
                continue
            packed = self._read_blob('B_' + bid)
            if not packed:
                raise RuntimeError('body of %s missing' % rel)
            data = _hzlib.decompress(_hb64.b64decode(packed))
            if _hsha.sha256(data).hexdigest()[:32] != bid:
                raise RuntimeError('body of %s damaged' % rel)
            files[rel] = (data, mtime, size)
            bids.add(bid)
        return files, dict((k, v) for k, v in meta.items() if k != 'files'), bids

    def load(self):
        pointer = self._read('Pointer')
        if not pointer:
            if self.legacy is None:
                return {}, {}
            files, meta = self.legacy.load()
            meta = dict(meta or {})
            meta['restored_from'] = ('broker key-value store (state %s); saved to the QMT log + registry store '
                                     'from now on' % meta.get('saved_at', '?'))
            return files, meta
        errors = []
        previous = self._read('PointerPrev') or ''
        for mid in [pointer] + ([previous] if previous and previous != pointer else []):
            try:
                files, meta, bids = self._load_mid(mid)
            except Exception as exc:
                errors.append('%s: %s' % (mid[:8], exc))
                continue
            self.pointer, self.prev_pointer = mid, (previous if mid == pointer else '')
            self.committed_bids = bids
            self.acked = set(bids)
            if mid != pointer:
                meta['restored_from'] = 'the snapshot before the latest (%s)' % '; '.join(errors)
            return files, meta
        raise RuntimeError('no complete state snapshot; refusing a partial state (%s)' % '; '.join(errors))

    def verify(self, pointer=None):
        """'' when the committed snapshot reads back completely from the registry, else why."""
        if not self.pointer:
            return ''
        try:
            if self._read('Pointer') not in (self.pointer, (self.pending or {}).get('mid')):
                return 'registry pointer is %s, ours %s' % ((self._read('Pointer') or 'none')[:8], self.pointer[:8])
            self._load_mid(self.pointer)
            return ''
        except Exception as exc:
            return '%s: %s' % (type(exc).__name__, exc)

    def forget_bodies(self):
        """The next save prints every file body again."""
        self.acked = set()

    def follow_server(self):
        return ''


class QnHost(object):
    PUMP_MS = 100
    # Orders are covered by the forced save before each one; periodic saves only bound what a QMT
    # crash can lose. Each key-value call holds the GIL, 1-5 s apiece on a slow afternoon (10-23:
    # a save every 30 s froze QMT a third of the time with only health files changing).
    PERSIST_INTERVAL = 120.0
    # While a pipeline script runs its files are intermediate, and each key-value call holds the
    # GIL: on 10-23 15:56 saves took 12.7 s of every 20 s and slowed the pipeline (bg_lag up to 10 s).
    PERSIST_INTERVAL_BUSY = 300.0
    # Outside the trading session and the pipeline, the committed snapshot is read back in full now
    # and then; damage (10-23: a body chunk of both snapshots no longer decoded) is repaired by
    # writing every body again with the next save, long before a restart would need the snapshot.
    VERIFY_INTERVAL = 7200.0
    DAEMON_RESTART_DELAY = 60.0
    SWITCH_POLL_SECONDS = 2.0
    # QMT's 模型交易 row keeps showing 运行中 while trading is paused (the strategy shell must keep
    # running to receive start). While paused, QMT's log says so in plain Chinese every minute;
    # status lines then come every 60 s instead of 20 s so that notice stays visible.
    STATUS_SECONDS = 20.0
    STATUS_PAUSED_SECONDS = 60.0
    TRADING_NOTE_SECONDS = 600.0
    # Every trading-daemon log line (candidates, plans, account/position checks, pipeline progress,
    # pushes) is echoed to QMT's strategy log as "[A_SYSTEM_LOG] <line>", so start_windows.py can
    # follow it in the terminal as it followed logs/trading_daemon.log before 10-23. Printing is the
    # strategy's normal output, not file access. At most this many lines per tick (100 ms).
    ECHO_LINES_PER_TICK = 40
    BOOT_RETRY_SECONDS = 60.0
    RESET_AFTER_BROKEN_BOOTS = 3
    # QMT's key-value store goes through the broker's trade server, which disconnects at night
    # (calls then fail, time out, or may not return at all). Periodic saves run in the day only,
    # back off after a failure, and night-time state (the evening pipeline catch-up) is either
    # saved the next morning or rebuilt after a restart.
    STORE_DAY_START = 8
    STORE_DAY_END = 20
    STORE_BACKOFF_MAX = 1800.0
    # every call blocks the QMT callback thread (and the engine's bridge heartbeat with it)
    # Periodic saves give up after one slower call; the save on stop must complete (10-23 morning
    # single calls took 5-18 s several times an hour). Outside the trading session a frozen QMT
    # costs nothing, and on 10-23 afternoon calls took 5-7 s for hours (nothing saved after the
    # close pipeline), so the limit is looser there.
    STORE_SLOW_CALL = 5.0
    STORE_SLOW_CALL_OFF_HOURS = 30.0
    TRADING_SESSION = ('0910', '1505')
    # The save before an order (11-07): on 11-03..11-06 single calls took 5-60 s 21-55 times a
    # session, and that save makes about 7 calls, so an order could wait minutes (an auction order
    # or a stop-loss too late). Since 11-05 a start compares the restored journal with the broker
    # and holds trading for an order of today it does not know, so an order no longer waits for a
    # slow store: within STORE_SLOW_RECENT of a slow call the save is skipped, otherwise one call
    # slower than STORE_SLOW_CALL gives it up; the order goes ahead, the journal into the next save.
    STORE_SLOW_RECENT = 900.0
    # In the session, while nothing trades, only health files change (logs/broker_health.json every
    # minute); saving them every 2 minutes cost about 7 calls each. They are saved at most this often.
    SESSION_LOG_SAVE_SECONDS = 1800.0
    # 10-24: the trade server dropped at 17:48; periodic saves failed at once ("200000 未建立连接")
    # and were retried 1, 2, 4 and 8 minutes apart; the fifth call (18:04:06) never returned and
    # froze QMT's Python (output, pushes, orders) until QMT itself was restarted on 10-27. A call
    # that does not return cannot be undone from Python, so after a failure that says the trade
    # server is unreachable, periodic saves wait 30 minutes, then 1 and 2 hours (a call that was
    # merely slow keeps the short backoff). Matched on the error text, not on key names.
    STORE_LINK_ERRORS = ('(200000,', u'未建立连接', '(10060,', 'timed out', 'End of file')
    # 11-07 11:54: a start retried reading the state store while the trade server was down (11:52
    # to 11:59); that get_py_key never returned and froze QMT's Python until QMT was restarted (as
    # on 10-24). No key-value call is made while the account status says the trade server is down:
    # a start reads the store only after the status has said connected for BOOT_LINK_STABLE seconds,
    # and the saves before an order and on stop are skipped while it does not.
    BOOT_LINK_STABLE = 60.0
    BOOT_LINK_POLL = 10.0
    # 11-08 the account status flapped (08:15-08:17, 11:08); periodic saves start only once it has
    # said connected for this long
    PERIODIC_LINK_STABLE = 120.0
    STORE_LINK_BACKOFF = (1800.0, 3600.0, 7200.0)

    def __init__(self, C, qmt_globals, deploy, modules, static_files, market=None, broker=None, store=None):
        self.C = C
        self.g = qmt_globals
        self.deploy = deploy
        self.started = _htime.time()
        self.messages = []
        self.codebase = QnCodeBase(modules)
        self.static_files = static_files
        self.market = market
        self.broker = broker
        self.store = store
        for st in (store, getattr(store, 'legacy', None)):
            if st is not None:
                st.slow_mark = self.STORE_SLOW_CALL
                st.link_check = self._query_link
        self.engine = None
        self.daemon_thread = None
        self.daemon_restarts = 0
        self.daemon_next_start = 0.0
        self.stopped = False
        self.persist_dirty = False
        self.persist_last = 0.0
        self.persist_clock = 0.0
        self.persist_core = None    # fingerprint without logs/ of the last committed snapshot
        self.persist_saved_at = 0.0
        self.persist_meta = None
        self.persist_error = ''
        self.persist_lock = _hthr.RLock()
        self.persist_thread = None
        self.status_last = 0.0
        self.daemon_was_alive = False
        self.bg_lag = []        # how late a background thread woke from a 20 ms sleep (seconds)
        self.yield_ticks = [0, 0]  # [ticks that handed the lock to background threads, ticks]
        self.save_stats = [0, 0]   # [state saves completed, attempted] since the last status line
        self.verified_at = 0.0
        self.save_seconds_mark = 0.0
        self.boot_pending = False
        self.boot_retry_at = 0.0
        self.broken_boots = 0
        self.broken_store_pushed = set()
        self.calendar_checked = 0.0
        # restored state vs the broker (11-05): '' = checked, else why trading waits
        self.state_hold = ''
        self.state_hold_at = ''
        self.state_hold_pushed = ''
        self.state_checked = 0.0
        self.calendar_warned = ''
        self.calendar_problem = ''
        if deploy.get('store_day_hours'):  # tests only
            self.STORE_DAY_START, self.STORE_DAY_END = deploy['store_day_hours']
        self.persist_failures = 0
        self.persist_retry_at = 0.0
        self.persist_night_logged = ''
        # status fields of the broker account object (read from the engine's own queries): the
        # values seen while the key-value store works mean "trade server reachable"
        self.trading = None          # 'run' / 'pause' from the trading switch (live copy)
        self.switch_checked = 0.0
        self.trading_note_last = 0.0
        self.account_status = None
        self.account_status_good = set()
        self.link_up_since = 0.0     # when the account status last turned to connected (0: not connected)
        self.link_wait_logged = None
        self.account_status_learn = False
        self.store_waits_for_link = False
        self.exported = {}
        self.cache_thread = None
        self.cache_done = ''

    # ------------------------------------------------------------------ boot
    def log(self, text):
        line = '[A_SYSTEM] %s %s' % (_bj_now().strftime('%H:%M:%S'), text)
        self.messages.append(line)
        del self.messages[:-500]
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode('ascii', 'backslashreplace').decode('ascii'))

    @staticmethod
    def _kill_thread(thread):
        """Stopping a QMT strategy does not end its Python threads (the interpreter lives on).
        Threads of a stopped run end at their next checked wait (QnStopped); the asynchronous
        QnStopped also reaches a thread busy in Python code between such waits."""
        import ctypes
        if thread is None or not thread.is_alive() or thread.ident is None:
            return False
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread.ident), ctypes.py_object(QnStopped))
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread.ident), None)
            return False
        return res == 1

    @staticmethod
    def _stop_foreign_run(thread):
        """A thread of an earlier copy of the strategy file runs that copy's code: mark that copy
        stopped so its own checked waits end the thread. Copies built before run checks existed
        get a sleep that ends the calling thread instead."""
        init = getattr(type(thread), '__init__', None)
        g = getattr(init, '__globals__', None)
        if not isinstance(g, dict) or g.get('QN_THREADS') is QN_THREADS:
            return
        if isinstance(g.get('QN_SHUTDOWN'), list):
            g['QN_SHUTDOWN'][0] = True
            return
        old_time = g.get('qn_time')
        if old_time is not None and not getattr(old_time, '_qn_stopped', False):
            def stopped_sleep(*args, **kwargs):
                raise QnStopped('A_System strategy run stopped')
            old_time.sleep = stopped_sleep
            old_time._qn_stopped = True

    @staticmethod
    def _active_live_thread(thread):
        """A thread of another copy of the strategy file that is a running live copy."""
        g = getattr(getattr(type(thread), '__init__', None), '__globals__', None)
        if not isinstance(g, dict) or g.get('QN_THREADS') is QN_THREADS or not thread.is_alive():
            return False
        deploy, shutdown, run = g.get('QN_DEPLOY'), g.get('QN_SHUTDOWN'), g.get('QN_RUN')
        return (isinstance(deploy, dict) and deploy.get('mode') == 'live' and isinstance(shutdown, list)
                and not shutdown[0] and isinstance(run, list) and getattr(thread, '_qn_run', None) == run[0])

    def end_leftover_threads(self):
        """Threads of an earlier run of this strategy in the same QMT process (daemon, scripts,
        state saver) are ended before a new instance starts: one daemon per account, ever.
        Called again on every status tick until all of them are gone."""
        ended = []
        for t in _hthr.enumerate():
            if t is _hthr.current_thread():
                continue
            own = getattr(t, '_qn_system', False) and getattr(t, '_qn_run', None) == QN_RUN[0]
            if own or (self.deploy.get('mode') != 'live' and self._active_live_thread(t)):
                continue
            if getattr(t, '_qn_system', False) or t.name in ('a-system-daemon', 'a-system-state') \
                    or t.name.startswith('qn-script-'):
                try:
                    self._stop_foreign_run(t)
                except Exception:
                    pass
                if self._kill_thread(t):
                    ended.append(t.name)
        return ended

    @staticmethod
    def reset_logging():
        """QMT keeps the logging module between runs: drop handlers left by an earlier instance."""
        import logging as _hlog
        removed = 0
        loggers = [_hlog.getLogger()] + [l for l in _hlog.Logger.manager.loggerDict.values()
                                          if isinstance(l, _hlog.Logger)]
        for logger in loggers:
            for handler in list(logger.handlers):
                if type(handler).__name__ in ('_MemoryFileHandler', '_QuietStreamHandler', 'SafeRotatingFileHandler'):
                    logger.removeHandler(handler)
                    removed += 1
        return removed

    def boot(self):
        if self.deploy.get('mode') != 'live':
            live = sorted(set(t.name for t in _hthr.enumerate() if self._active_live_thread(t)))
            if live:
                raise RuntimeError('A_System live copy is running in this QMT (%s); the shadow copy does not '
                                   'start beside it' % ', '.join(live))
        qn_new_run()
        ended = self.end_leftover_threads()
        removed = self.reset_logging()
        if ended or removed:
            self.log('ended %d leftover threads of an earlier run (%s); removed %d stale log handlers' % (
                len(ended), ', '.join(sorted(set(ended))), removed))
        self._boot_state()

    def _boot_state(self):
        """Restore state and start the engine and daemon. Without the persisted state nothing
        may trade, so a state store that cannot be read means: wait and try again."""
        if self._store_uses_broker() and not self.link_ready(self.BOOT_LINK_STABLE):
            self.boot_pending = True
            self.boot_retry_at = _htime.time() + self.BOOT_LINK_POLL
            if self.link_wait_logged != self.account_status:
                self.link_wait_logged = self.account_status
                self.log('waiting for the trade connection before reading the state store (account %s; '
                         'reading starts %ds after it says connected)' % (
                             _status_text(self.account_status or ()), self.BOOT_LINK_STABLE))
            return
        try:
            self._seed_vfs()
        except Exception as exc:
            self.boot_pending = True
            self.boot_retry_at = _htime.time() + self.BOOT_RETRY_SECONDS
            self.log('QMT state store not readable (%s: %s); trading not started, retry in %ds' % (
                type(exc).__name__, exc, self.BOOT_RETRY_SECONDS))
            return
        self.boot_pending = False
        self._poll_calendar()  # before the daemon starts: it checks the calendar at once
        self._hold_check()     # restored state vs the broker, before trading can start
        self.account_status_learn = self.store is not None  # the store was just read: link is up
        settings = self._write_runtime_config()
        QN_NOTIFY_MODE[0] = 'live' if self.deploy.get('mode') == 'live' else 'shadow'
        if not QN_ENVIRON.get('BARK_URL'):
            QN_ENVIRON['BARK_URL'] = QN_NOTIFY_BASE
        QN_NATIVE_SERVICE.bind(self.C, self.g)
        QN_SQLITE_SKIP_TABLES.update(QN_PERSIST_SKIP_TABLES)
        engine_loader = QnLoader(self.codebase)
        Engine = engine_loader.load('qmt_inner.engine').Engine
        api = self._trading_api()
        self.engine = Engine(self.C, api, settings)
        self.log('engine started; mode=%s release=%s' % (settings['mode'], self.deploy.get('release')))
        QN_PROCESS_EXIT[0] = self._daemon_process_exit
        self._poll_switch(force=True)
        if self.store is not None:
            self.persist_thread = QnThread(target=self._persist_loop, name='a-system-state', daemon=True)
            self.persist_thread.start()
        if self.deploy.get('cache_keeper', True):
            self.cache_thread = QnThread(target=self._cache_keeper_loop, name='a-system-cache', daemon=True)
            self.cache_thread.start()
        self.probe_thread = QnThread(target=self._gil_probe_loop, name='a-system-gil-probe', daemon=True)
        self.probe_thread.start()

    def _seed_vfs(self):
        # Deterministic mtimes for everything shipped in the strategy file, so the file
        # inventory is identical after every QMT restart.
        for rel, text in self.static_files.items():
            QN_VFS.put_restored(rel, text.encode('utf-8'), QN_SEED_MTIME)
        for name, (rel, is_pkg, src) in self.codebase.sources.items():
            QN_VFS.put_restored(rel, src.encode('utf-8'), QN_SEED_MTIME)
        for rel, packed in sorted(self.deploy.get('seed', {}).items()):
            QN_VFS.put_restored(rel, _hzlib.decompress(_hb64.b64decode(packed)), QN_SEED_MTIME)
        for rel, (size, mtime) in sorted(self.deploy.get('seed_stubs', {}).items()):
            QN_VFS.put_stub(rel, size, mtime)
        restored = 0
        if self.store is not None:
            by_sha, by_rel = {}, {}
            for rel, packed in self.deploy.get('seed', {}).items():
                data = _hzlib.decompress(_hb64.b64decode(packed))
                by_sha[_hsha.sha256(data).hexdigest()[:32]] = by_rel[rel] = data
            for rel, text in self.static_files.items():
                data = text.encode('utf-8')
                by_sha[_hsha.sha256(data).hexdigest()[:32]] = data
            # an operator may name files whose seed version may stand in when their body is lost
            # (set per build with QN_REPAIR_FROM_SEED; normal builds name none and stay strict)
            named = set(self.deploy.get('repair_from_seed') or ())
            self.store.fallback_bodies = lambda sha, rel: by_sha.get(sha) or (by_rel.get(rel) if rel in named else None)
            self.store.repaired = []
            up_since = self.link_up_since
            via_broker = self._store_uses_broker()
            try:
                files, meta = self.store.load()
                self.broken_boots = 0
            except RuntimeError as exc:
                if 'no complete state snapshot' in str(exc) and via_broker and (
                        not self.link_ready() or self.link_up_since != up_since):
                    # 11-07 11:53: the trade server dropped during a 5-minute read and returned cut
                    # values; that says nothing about the stored state
                    raise RuntimeError('the trade connection dropped while the state was read (account %s); '
                                       'read again once it is back' % _status_text(self.account_status or ()))
                # Only a snapshot that is broken in content counts, never a store that cannot be read.
                # 10-26 an operator build (QN_RESET_STATE_IF_BROKEN) started from the shipped seed; 11-05
                # the broker's key-value store lost the bodies of both snapshots and that took a manual
                # build again. Now a live copy does it by itself when the broker itself shows the
                # account flat (no position, no unfinished order): then the shipped seed plus the
                # automatic catch-up is the whole trading state (11-05: identical results). With a
                # position or an open order it never guesses; it waits and asks for a person.
                if 'no complete state snapshot' not in str(exc):
                    raise
                operator = bool(self.deploy.get('reset_if_broken'))
                auto = bool(self.deploy.get('auto_reset_when_flat', self.deploy.get('mode') == 'live'))
                if not operator and not auto:
                    raise
                self.broken_boots += 1
                if self.broken_boots < self.RESET_AFTER_BROKEN_BOOTS:
                    self._notify_broken_store('checking')
                    raise
                if operator:
                    reason = 'operator-approved for this build'
                else:
                    flat, detail = self._broker_flat()
                    if not flat:
                        self._notify_broken_store('blocked', detail)
                        raise RuntimeError('%s; not reset automatically: the broker does not show a flat '
                                           'account (%s)' % (exc, detail))
                    reason = 'the broker shows the account flat: %s' % detail
                self.log('STATE RESET (%s): %d starts in a row found no complete snapshot; starting from the '
                         'shipped seed (%s)' % (reason, self.broken_boots, str(exc)[:300]))
                self._notify_broken_store('reset')
                self.store.known, self.store.previous = {}, {}
                self.store.pointer = self.store.prev_pointer = ''
                files, meta = None, None
            if self.store.repaired:
                self.log('state store: lost file bodies taken from the shipped seed and written again with the next '
                         'save: %s' % ', '.join(self.store.repaired))
            if files:
                for rel, (data, mtime, size) in files.items():
                    if data is None:
                        QN_VFS.put_stub(rel, size, mtime)
                    else:
                        QN_VFS.put_restored(rel, data, mtime)
                restored = len(files)
                inventory = meta.get('inventory') or {}
                QN_VFS.prev_inventory = dict((QN_VFS.abspath(rel), tuple(v)) for rel, v in inventory.items())
                self.persist_meta = dict(meta, inventory=len(inventory))
                if meta.get('restored_from'):
                    self.log('state restored from the %s' % meta['restored_from'])
        for key, value in self.deploy.get('config_overrides', {}).items():
            self._override_config(key, value)
        self.log('in-memory files ready: seed=%d restored=%d (state saved at %s)' % (
            len(self.deploy.get('seed', {})), restored, (self.persist_meta or {}).get('saved_at', 'never')))

    CALENDAR_PATH = 'data/raw/trade_calendar.csv'
    CALENDAR_POLL_SECONDS = 1800.0
    CALENDAR_WARN_DAYS = 40

    def _merge_calendar_feed(self):
        """Days after today from the forwarder's feed replace the shipped calendar's; days up to
        today are never changed and must agree with the feed (a feed that disagrees about the past
        is rejected). The merged file gets an mtime derived from its content, so a restart with the
        same calendar does not change step 13's input inventory."""
        today = _bj_now().strftime('%Y%m%d')
        try:
            text = QN_VFS.get_text(self.CALENDAR_PATH)
        except Exception as exc:
            return self._calendar_note('calendar unreadable: %s' % exc)
        lines = text.splitlines()
        header, rows = lines[0], [l for l in lines[1:] if l.strip()]
        current = {}
        for line in rows:
            cells = line.split(',')
            if len(cells) >= 4:
                current[cells[1]] = (cells[2].strip() in ('1', '1.0'), line)
        raw = _qn_read_calendar_feed()
        if raw:
            feed, problem = _qn_parse_calendar_feed(raw)
            if feed is not None:
                past = [d for d in feed if d <= today and d in current]
                wrong = [d for d in past if feed[d] != current[d][0]]
                if len(past) < 20:
                    problem = 'feed overlaps the known past by %d days only' % len(past)
                elif wrong:
                    problem = 'feed disagrees with the known past on %s' % ', '.join(wrong[:5])
            if problem:
                self._calendar_note('trade calendar feed rejected: %s' % problem)
            else:
                merged = dict((d, v[0]) for d, v in current.items())
                merged.update((d, v) for d, v in feed.items() if d > today)
                opens = sorted(d for d, v in merged.items() if v)
                out, prev = [], ''
                pre = {}
                for d in sorted(merged):
                    pre[d] = prev
                    if merged[d]:
                        prev = d
                for d in sorted(merged, reverse=True):
                    if d <= today and d in current:
                        out.append(current[d][1])
                    else:
                        out.append('SSE,%s,%d,%s' % (d, 1 if merged[d] else 0, pre[d]))
                new_text = '\n'.join([header] + out) + '\n'
                if new_text.replace('\r', '') != text.replace('\r', ''):
                    data = new_text.encode('utf-8')
                    mtime = QN_SEED_MTIME + 1 + int(_hsha.sha256(data).hexdigest()[:6], 16) / 1000.0
                    QN_VFS.put_restored(self.CALENDAR_PATH, data, mtime)
                    self.log('trade calendar extended from the forwarder feed: open days through %s (was %s)' % (
                        opens[-1] if opens else '-', max((d for d, v in current.items() if v[0]), default='-')))
        last_open = max((d for d, v in current.items() if v[0]), default='')
        try:
            text2 = QN_VFS.get_text(self.CALENDAR_PATH)
            last_open = max((l.split(',')[1] for l in text2.splitlines()[1:] if l.count(',') >= 3
                             and l.split(',')[2].strip() in ('1', '1.0')), default=last_open)
        except Exception:
            pass
        warn_days = int(self.deploy.get('calendar_warn_days', self.CALENDAR_WARN_DAYS))  # < 0: off (tests)
        horizon = (_bj_now() + _hdt.timedelta(days=max(warn_days, 0))).strftime('%Y%m%d')
        if warn_days >= 0 and last_open < horizon and self.calendar_warned != today:
            self.calendar_warned = today
            self.log('trade calendar only reaches %s (needs %s)' % (last_open or '-', horizon))
            self._push('⚠️ 交易日历快用完了',
                       'QMT里的交易日历只到 %s。日历由QMT外的推送转发程序每天从Tushare取来写入注册表；请检查推送转发'
                       '（计划任务 A_System_QmtNotifyForwarder）和Tushare令牌。日历用完后无法判断交易日、收盘流水线会停。'
                       % (last_open or '无'))

    def _calendar_note(self, message):
        if self.calendar_problem != message:
            self.calendar_problem = message
            self.log(message)

    def _poll_calendar(self):
        now = _htime.time()
        if now - self.calendar_checked < self.CALENDAR_POLL_SECONDS:
            return
        self.calendar_checked = now
        try:
            self._merge_calendar_feed()
        except Exception as exc:
            self._calendar_note('trade calendar merge failed: %s: %s' % (type(exc).__name__, exc))

    STATE_CHECK_RETRY_SECONDS = 60.0

    def _journal_tags(self):
        """Order tags (remarks) of every submission the restored state knows."""
        path = 'runtime/qmt_inner/spool/journal.sqlite3'
        if not QN_VFS.isfile(path):
            return set()
        data = QN_VFS.read(path)
        if not data.startswith(QN_SQLITE_DUMP_MAGIC):
            return set()
        import sqlite3 as _hsql
        db = _hsql.connect(':memory:')
        try:
            db.executescript(data[len(QN_SQLITE_DUMP_MAGIC):].decode('utf-8'))
            return set(str(r[0]) for r in db.execute('SELECT tag FROM submissions'))
        except Exception:
            return set()
        finally:
            db.close()

    def _last_close_before_today(self):
        try:
            days = sorted(l.split(',')[1] for l in QN_VFS.get_text(self.CALENDAR_PATH).splitlines()[1:]
                          if l.count(',') >= 3 and l.split(',')[2].strip() in ('1', '1.0'))
        except Exception:
            return ''
        today = _bj_now().strftime('%Y%m%d')
        before = [d for d in days if d < today]
        return '%s-%s-%s 15:00:00' % (before[-1][:4], before[-1][4:6], before[-1][6:]) if before else ''

    def _check_restored_state(self):
        """QMT's key-value store is per trade server and not replicated (11-05), so a start may
        restore an older snapshot from another server. Before trading starts, compare with the
        broker itself: an order of this system today (remark = our tag) that the restored journal
        does not know, or positions held while the snapshot predates the last trading day's close,
        mean the state may be out of date: trading waits and a person is asked (running
        start_windows.py afterwards confirms). A flat account without such orders always passes.
        Returns '' when checked and fine, 'pending' when the broker could not be asked yet, else
        the reason trading waits."""
        account = str(self.deploy['account_id'])
        query = self.g['get_trade_detail_data'] if self.deploy.get('mode') == 'live' else self.broker.get_trade_detail_data
        try:
            accounts = query(account, 'stock', 'account')
            orders = query(account, 'stock', 'order')
            positions = query(account, 'stock', 'position')
        except Exception as exc:
            self.log('restored state check waits for the broker (%s: %s)' % (type(exc).__name__, exc))
            return 'pending'
        if not isinstance(accounts, (list, tuple)) or not accounts or not isinstance(orders, (list, tuple)) \
                or not isinstance(positions, (list, tuple)):
            return 'pending'
        known = self._journal_tags()
        ours = sorted(set(str(getattr(o, 'm_strRemark', '') or '') for o in orders) - set(['']))
        ours = [t for t in ours if t.startswith('AS') and len(t) == 30]
        unknown = [t for t in ours if t not in known]
        held = sorted('%s%s' % (getattr(p, 'm_strInstrumentID', ''), '.' + str(getattr(p, 'm_strExchangeID', '')))
                      for p in positions if int(getattr(p, 'm_nVolume', 0) or 0) > 0)
        saved_at = str((self.persist_meta or {}).get('saved_at', ''))
        last_close = self._last_close_before_today()
        if unknown:
            return ('今天有 %d 笔本系统委托不在恢复的状态里（状态保存于 %s，可能来自另一台券商服务器的旧快照）'
                    % (len(unknown), saved_at or '未知'))
        if held and saved_at and last_close and saved_at < last_close:
            return ('账户有持仓 %s，但恢复的状态保存于 %s，早于上一交易日收盘 %s（可能来自另一台券商服务器的旧快照）'
                    % (','.join(held[:5]), saved_at, last_close))
        return ''

    def _hold_check(self):
        """Run the restored-state check until it could be done; keep trading waiting meanwhile."""
        if not self.deploy.get('state_check', self.deploy.get('mode') == 'live') or self.store is None:
            return
        if self.state_hold not in ('pending', '') or self.state_checked < 0:
            return
        now = _htime.time()
        if self.state_hold == 'pending' and now - self.state_checked < self.STATE_CHECK_RETRY_SECONDS:
            return
        self.state_checked = now
        result = self._check_restored_state()
        if result == 'pending':
            if self.state_hold != 'pending':
                self.log('trading waits until the broker can be asked about the restored state')
            self.state_hold = 'pending'
            return
        self.state_checked = -1.0  # done for this run
        if not result:
            if self.state_hold == 'pending':
                self.log('restored state checked against the broker: consistent')
            self.state_hold = ''
            return
        self.state_hold = result
        self.state_hold_at = _bj_now().strftime('%Y-%m-%d %H:%M:%S')
        self.log('⛔ trading held: %s' % result)
        self._push('🛑 A_System恢复的状态可能不是最新，交易未启动',
                   '%s。请在QMT核对持仓和今天的委托；确认无误后在终端运行 py -3.11 start_windows.py，交易随即启动。'
                   % result)

    TERMINAL_ORDER_STATUS = (53, 54, 56, 57)  # cancelled, part-cancelled, rejected, filled

    def _broker_flat(self):
        """(flat, detail) read from the broker itself, never from our own state: no position with a
        volume and no order that is not finished. A failed or empty answer is never "flat"."""
        account = str(self.deploy['account_id'])
        query = self.g['get_trade_detail_data'] if self.deploy.get('mode') == 'live' else self.broker.get_trade_detail_data
        try:
            rows = dict((kind, query(account, 'stock', kind)) for kind in ('account', 'position', 'order'))
        except Exception as exc:
            return False, 'broker query failed (%s: %s)' % (type(exc).__name__, exc)
        for kind, value in rows.items():
            if not isinstance(value, (list, tuple)):
                return False, 'broker query %s returned %s' % (kind, type(value).__name__)
        if not rows['account']:
            return False, 'broker returned no account row (trade server not connected?)'
        for kind, value in rows.items():
            if any(str(getattr(r, 'm_strAccountID', account)) != account for r in value):
                return False, 'broker %s rows belong to another account' % kind
        held = [r for r in rows['position'] if int(getattr(r, 'm_nVolume', 0) or 0) > 0]
        unfinished = [r for r in rows['order'] if int(getattr(r, 'm_nOrderStatus', 0) or 0) not in self.TERMINAL_ORDER_STATUS]
        return (not held and not unfinished), 'positions with volume %d, unfinished orders %d' % (len(held), len(unfinished))

    def _push(self, title, body):
        """A phone push from the host itself (the trading daemon is not running yet)."""
        QN_NOTIFY_MODE[0] = 'live' if self.deploy.get('mode') == 'live' else 'shadow'
        query = _hurlparse.urlencode({'group': 'A股实盘', 'level': 'timeSensitive', 'sound': 'alarm'})
        try:
            _qn_urlopen(QN_NOTIFY_BASE + '/' + _hurlparse.quote(title, safe='') + '/'
                        + _hurlparse.quote(body, safe='') + '?' + query)
        except Exception as exc:
            self.log('push failed: %s: %s' % (type(exc).__name__, exc))

    def _notify_broken_store(self, stage, detail=''):
        """One push per stage and strategy run: checking -> reset, or checking -> blocked."""
        if stage in self.broken_store_pushed:
            return
        self.broken_store_pushed.add(stage)
        if stage == 'checking':
            self._push('⛔ A_System状态读不全，交易未启动',
                       'QMT状态存储里没有完整的保存状态（券商键值服务丢了数据），交易程序不启动、不下单。约%d分钟内再确认几次：'
                       '券商账户空仓且无未完成委托时自动从策略自带的初始状态重建并补跑数据；否则保持停止，等人工处理。'
                       % (int(self.RESET_AFTER_BROKEN_BOOTS * self.BOOT_RETRY_SECONDS / 60) + 1))
        elif stage == 'blocked':
            self._push('🛑 A_System状态丢失且账户非空仓，需人工处理',
                       '保存的状态读不全，券商账户显示：%s。有持仓或未完成委托时不自动重建（不知道入场日期和平仓计划），'
                       '交易程序保持停止，请人工处理。' % detail)
        else:
            self._push('♻️ A_System已从初始状态自动重建',
                       '保存的状态读不全；券商账户确认空仓且无未完成委托，已从策略自带的初始状态重建，接着自动补跑最近交易日数据，'
                       '然后照常运行（交易开关不变）。')

    def _override_config(self, dotted, value):
        path = 'config/config.json'
        cfg = _hjson.loads(QN_VFS.get_text(path))
        node = cfg
        keys = dotted.split('.')
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        QN_VFS.put_restored(path, _hjson.dumps(cfg, ensure_ascii=False, indent=2).encode('utf-8'), QN_SEED_MTIME)

    def _write_runtime_config(self):
        account = str(self.deploy['account_id'])
        token = _hsha.sha256(('%s:%s:%s' % (account, self.started, id(self))).encode()).hexdigest()
        settings = dict(account_id=account, account_type='STOCK', token=token,
                        spool_dir=QN_ROOT + '/runtime/qmt_inner/spool', mode='live',
                        runtime_config=QN_ROOT + '/config/config.json',
                        intent_database=QN_ROOT + '/data/state/execution_events.sqlite3',
                        trade_calendar=QN_ROOT + '/data/raw/trade_calendar.csv',
                        manual_stop_path=QN_ROOT + '/.manual_stop.json', max_order_notional=0,
                        buy_fee_reserve_rate=0.001, minimum_fee_reserve=5, poll_interval_ms=self.PUMP_MS,
                        # In one process the engine's heartbeat only stops while a QMT call blocks the
                        # callback thread, and such a call (a broker key-value write while the trade
                        # server is flaky) freezes every thread, the daemon too, for up to about a
                        # minute. With the external bridge's 5 s the daemon then read the old heartbeat
                        # as a lost bridge and pushed an alarm (10-22 19:07). A stopped engine still
                        # shows within 90 s; a stopped strategy removes the heartbeat at once.
                        request_timeout_seconds=8, query_timeout_seconds=20, heartbeat_max_age_seconds=90,
                        max_minute_subscriptions=50)
        QN_VFS.put_text('runtime/qmt_inner/config.json', _hjson.dumps(settings))
        QN_ENVIRON.update({'QMT_INNER_CONFIG': QN_ROOT + '/runtime/qmt_inner/config.json',
                           'QMT_TRANSPORT': 'qmt_inner', 'QMT_ACCOUNT_ID': account, 'QMT_ACCOUNT_TYPE': 'STOCK',
                           'PYTHONUTF8': '1', 'TZ': 'Asia/Shanghai'})
        QN_ENVIRON.update(self.deploy.get('env', {}))
        env_lines = ['%s=%s' % (k, v) for k, v in sorted(QN_ENVIRON.items())
                     if k.startswith(('QMT_', 'A_SYSTEM_', 'LOG_'))]
        QN_VFS.put_text('.env', '\n'.join(env_lines) + '\n')
        return settings

    def _trading_api(self):
        if self.deploy.get('mode') == 'live':
            api = dict((name, self.g[name]) for name in ('get_trade_detail_data', 'passorder', 'cancel',
                                                        'download_history_data'))
        else:
            api = dict(get_trade_detail_data=self.broker.get_trade_detail_data, passorder=self.broker.passorder,
                       cancel=self.broker.cancel, download_history_data=self.g['download_history_data'])
        real_passorder = api['passorder']
        real_query = api['get_trade_detail_data']
        host = self

        def get_trade_detail_data(account, market, kind, *rest):
            rows = real_query(account, market, kind, *rest)
            if str(kind).lower() == 'account':
                host._note_account_status(rows)
            return rows
        api['get_trade_detail_data'] = get_trade_detail_data

        def passorder(*args, **kwargs):
            if host.stopped or QN_SHUTDOWN[0]:
                raise RuntimeError('A_System copy stopped or taken over; order refused')
            # The engine's submission journal is committed just before this call. Make it durable
            # in QMT's key-value store before the order can exist at the broker, unless the trade
            # server is slow (persist_before_order).
            host.persist_before_order()
            return real_passorder(*args, **kwargs)
        api['passorder'] = passorder
        return api

    def _note_account_status(self, rows):
        """Status fields of the account object the engine queries every minute anyway (no extra
        QMT call): logged when they change; periodic saves wait while they differ from the
        values seen when the key-value store last worked."""
        try:
            row = rows[0] if rows else None
            if row is None:
                return
            names = sorted(n for n in dir(row) if n.startswith('m_') and 'status' in n.lower())
            status = tuple((n, str(getattr(row, n, ''))) for n in names)
        except Exception:
            return
        if status != self.account_status:
            self.log('trade account status: %s%s' % (
                _status_text(status), '' if self.account_status is None else ' (was %s)' % _status_text(self.account_status)))
            if not _status_up(status):
                self.link_up_since = 0.0
            elif self.account_status is None or not _status_up(self.account_status) or not self.link_up_since:
                self.link_up_since = _htime.time()
            self.account_status = status
        if self.account_status_learn:
            self.account_status_learn = False
            self.account_status_good.add(status)

    def store_link_down(self):
        """True while the account shows a status the key-value store has not worked with, or one
        that says the trade server is not connected."""
        if self.account_status is not None and not _status_up(self.account_status):
            return True
        return bool(self.account_status and self.account_status_good
                    and self.account_status not in self.account_status_good)

    def _saves_via_broker(self):
        return self.store is not None and not getattr(self.store, 'log_store', False)

    def _store_uses_broker(self):
        """True while state reads/writes go through the broker's key-value store (always for the
        old store; for the log store only until its first snapshot is committed)."""
        if self.store is None:
            return False
        uses = getattr(self.store, 'uses_broker', None)
        return True if uses is None else bool(uses())

    def _query_link(self):
        """Ask the broker's account object for its status (a local read, it does not wait for the
        trade server; it answered every minute through the 11-07 outage). An empty answer counts
        as not connected."""
        account = str(self.deploy.get('account_id', ''))
        query = self.g.get('get_trade_detail_data') if self.deploy.get('mode') == 'live' else \
            getattr(self.broker, 'get_trade_detail_data', None)
        if query is None:
            return True
        try:
            rows = query(account, 'stock', 'account')
        except Exception:
            rows = None
        if not rows:
            if self.account_status != ((u'account', u'no answer'),):
                self.log('trade account status: no account row from the broker')
            self.account_status = ((u'account', u'no answer'),)
            self.link_up_since = 0.0
            return False
        self._note_account_status(rows)
        return _status_up(self.account_status)

    def link_ready(self, stable_seconds=0.0):
        if not self._query_link():
            return False
        if not self.account_status:  # no status field (paper broker, tests): nothing to wait for
            return True
        return _htime.time() - self.link_up_since >= stable_seconds

    # ------------------------------------------------------------------ daemon thread
    def _start_daemon(self):
        token = [False]

        def target():
            try:
                run()
            finally:
                # like a daemon process exiting: the threads and scripts it started end too,
                # so a restarted daemon never runs beside its predecessor's pipeline
                token[0] = True

        def run():
            self.log('trading daemon thread starting (restart #%d)' % self.daemon_restarts)
            try:
                QnLoader(self.codebase).run_main('scripts.trading_daemon', [], raise_on_exit=False)
                self.log('trading daemon returned')
            except SystemExit as exc:
                self.log('trading daemon exited: %r' % (exc.code,))
            except BaseException as exc:
                if qn_is_stopped(exc):
                    self.log('trading daemon thread ended (%s)' % (
                        'paused by the trading switch' if self.trading == 'pause' else 'strategy run stopped'))
                else:
                    self.log('trading daemon crashed:\n' + _htb.format_exc()[-3000:])
        self.daemon_thread = QnThread(target=target, name='a-system-daemon', daemon=True)
        self.daemon_thread._qn_kill = tuple(self.daemon_thread._qn_kill) + (token,)
        self.daemon_thread.start()

    def _echo_daemon_log(self):
        if not self.deploy.get('echo_daemon_log', True):
            return
        for _ in range(self.ECHO_LINES_PER_TICK):
            try:
                line = QN_LOG_ECHO.popleft()
            except IndexError:
                return
            try:
                print('[A_SYSTEM_LOG] ' + line)
            except UnicodeEncodeError:
                print('[A_SYSTEM_LOG] ' + line.encode('ascii', 'backslashreplace').decode('ascii'))

    def _trading_notice(self, now):
        if not self.deploy.get('trading_switch', self.deploy.get('mode') == 'live') or self.trading is None:
            return
        detail = _qn_switch_detail()
        if self.trading == 'pause' and self.state_hold:
            self.log('⛔ 交易未启动：%s' % ('正在等券商连接，核对恢复的状态后才启动交易' if self.state_hold == 'pending'
                                          else self.state_hold + '。核对后运行 py -3.11 start_windows.py 启动'))
        elif self.trading == 'pause':
            self.log('⏸ 交易已暂停（交易开关 pause%s）：不开仓也不平仓（止损、止盈、到期卖出都不执行），不查账户、不跑流水线。QMT“模型交易”里这一行仍显示'
                     '“运行中”是正常的（外壳要一直运行才能接收 start）。恢复交易：py -3.11 start_windows.py'
                     % ('，' + detail if detail else ''))
        elif now - self.trading_note_last >= self.TRADING_NOTE_SECONDS:
            self.trading_note_last = now
            self.log('▶ 交易中（交易开关 run%s）。暂停：py -3.11 stop_windows.py' % ('，' + detail if detail else ''))

    def _poll_switch(self, force=False):
        """Follow the trading switch: start the trading daemon on "run", end it on anything else
        (its pipeline scripts end with it). The engine, state saves and QMT stay as they are."""
        now = _htime.time()
        if not force and now - self.switch_checked < self.SWITCH_POLL_SECONDS:
            return
        self.switch_checked = now
        if not self.deploy.get('trading_switch', self.deploy.get('mode') == 'live'):
            want = 'run'  # shadow/validation copies always run
        else:
            try:
                want = 'run' if _qn_read_switch() == 'run' else 'pause'
            except Exception:
                want = 'pause'
            if want == 'run' and self.state_hold:
                if self.state_hold != 'pending' and _qn_switch_changed_at() > self.state_hold_at:
                    self.log('restored state confirmed by start_windows.py after the hold: trading starts')
                    self.state_hold = ''
                else:
                    want = 'pause'
        if want == self.trading and not force:
            return
        before, self.trading = self.trading, want
        self.status_last = 0.0        # a status line (and the plain-language notice) right away
        self.trading_note_last = 0.0
        alive = bool(self.daemon_thread and self.daemon_thread.is_alive())
        if want == 'run':
            if not alive:
                self.daemon_next_start = 0.0
                self.log('trading switch: run (start_windows.py) - trading daemon starts')
                self._start_daemon()
        else:
            if alive:
                self.log('trading switch: pause (stop_windows.py) - trading daemon ends; QMT stays open')
                self._kill_thread(self.daemon_thread)
            elif before is None:
                self.log('trading switch: pause - A_System waits; run start_windows.py to begin trading')

    def _daemon_process_exit(self, code):
        """The trading daemon asked for a process exit (os._exit): end it and its scripts; the
        keeper-style supervisor starts a fresh one after DAEMON_RESTART_DELAY (QMT stays up)."""
        if self.stopped or QN_SHUTDOWN[0]:
            return
        self.log('trading daemon requested a process exit (os._exit(%s)): ending it and its scripts; QMT stays '
                 'up, a fresh daemon starts in %ds (keeper behaviour)' % (code, self.DAEMON_RESTART_DELAY))
        thread = self.daemon_thread
        if thread is not None and thread is not _hthr.current_thread():
            self._kill_thread(thread)

    def _supervise(self):
        if self.trading != 'run':
            return  # paused by the trading switch: no keeper-style restart
        if self.stopped or self.daemon_thread is None or self.daemon_thread.is_alive():
            return
        now = _htime.time()
        if not self.daemon_next_start:
            self.daemon_next_start = now + self.DAEMON_RESTART_DELAY
            self.log('daemon stopped; restarting in %ds (keeper behaviour)' % self.DAEMON_RESTART_DELAY)
            return
        if now >= self.daemon_next_start:
            self.daemon_next_start = 0.0
            self.daemon_restarts += 1
            self._start_daemon()

    # ------------------------------------------------------------------ persistence
    def _persist_files(self):
        out = {}
        prefix = QN_ROOT + '/'
        for a in sorted(set(QN_VFS.files) | set(QN_VFS.stubs)):
            if not a.startswith(prefix):
                continue
            rel = a[len(prefix):]
            if not any(p.search(rel) for p in QN_PERSIST_INCLUDE):
                continue
            st = QN_VFS.stat(a)
            if st.st_mtime == QN_SEED_MTIME:
                continue  # shipped in the strategy file and unchanged since boot
            if a in QN_VFS.stubs:
                out[rel] = (None, st.st_mtime, st.st_size)
            else:
                out[rel] = (QN_VFS.read(a), st.st_mtime, st.st_size)
        return out

    def _export_files(self):
        mode = 'live' if self.deploy.get('mode') == 'live' else 'shadow'
        for rel in QN_EXPORT_FILES:
            data = QN_VFS.files.get(QN_ROOT + '/' + rel)
            if data is None:
                continue
            sha = qn_content_sha(data)
            last = self.exported.get(rel)
            if last and last[0] == sha and _htime.time() - last[1] < 1800:
                continue  # unchanged: repeated every 30 minutes for a reader that restarted
            packed = _hb64.b64encode(_hzlib.compress(data, 9)).decode('ascii')
            chunks = [packed[i:i + QN_EXPORT_CHUNK] for i in range(0, len(packed), QN_EXPORT_CHUNK)] or ['']
            ident = '%s-%s' % (QN_RUN[0], sha[:12])
            for i, chunk in enumerate(chunks):
                print('[A_SYSTEM_EXPORT %s] %s %s %d/%d %s' % (mode, rel, ident, i + 1, len(chunks), chunk))
            self.exported[rel] = (sha, _htime.time())

    # ------------------------------------------------------------------ QMT daily-bar cache
    # QMT does not keep the whole market's daily history in its local cache by itself. The close
    # pipeline reads 15 days of it after every QMT restart (the old system kept those files on
    # disk), so the cache is kept current here: outside trading hours, every stock gets the
    # previous trading day's bar. The current day is left alone: the pipeline builds it from the
    # 15:00 snapshot, as validated.
    # outside trading hours only; the lunch break lets a restart in the night still finish the
    # previous day before the close pipeline (10-28: 2131 bars of 10-27 were missing at 09:49)
    CACHE_WINDOWS = (('0500', '0905'), ('1135', '1255'), ('1535', '2300'))
    CACHE_HISTORY_DAYS = 80  # the collector reads ENSURE_DAYS + HISTORY_DAYS open days back

    def _cache_allowed(self):
        hm = _bj_now().strftime('%H%M')
        return any(a <= hm < b for a, b in self.CACHE_WINDOWS)

    def _cache_target(self, days, today):
        """The open day whose daily bars the keeper keeps in QMT's cache. 10-28: the keeper only
        cached the previous open day, so day T's bars waited for the 05:00-09:05 window of T+1; QMT
        stopped the strategy at 02:58 (false "illegal IP"), the morning window was lost, and the
        catch-up of T after the 08:53 restart found only 2092 of ~5400 bars of T. Now, once today's
        close pipeline has written today's raw daily table (so the cache cannot change today's
        result), today's bars are cached the same evening."""
        past = [d for d in days if d < today]
        if (today in days and _bj_now().strftime('%H%M') >= '1535'
                and QN_VFS.isfile(QN_ROOT + '/data/raw/daily/%s.csv' % today)):
            return today
        return past[-1] if past else None

    def _cache_keeper_loop(self):
        while not self.stopped and not QN_SHUTDOWN[0]:
            try:
                self._cache_keeper_pass()
            except Exception as exc:
                self.log('daily cache keeper: %s: %s' % (type(exc).__name__, exc))
            for _ in range(120):  # ten minutes, in steps that notice a stop
                if self.stopped or QN_SHUTDOWN[0]:
                    return
                _htime.sleep(5)

    def _cache_keeper_pass(self):
        if not self._cache_allowed():
            return
        text = QN_VFS.get_text('data/raw/trade_calendar.csv')
        days = sorted(r['cal_date'] for r in _hcsv.DictReader(_hio.StringIO(text))
                      if str(r.get('is_open', '')).strip() in ('1', '1.0'))
        today = _bj_now().strftime('%Y%m%d')
        target = self._cache_target(days, today)
        if target is None or self.cache_done == target:
            return
        start = days[max(0, days.index(target) - self.CACHE_HISTORY_DAYS)]
        native = QnLoader(self.codebase).load('qn.native')
        universe = QN_NATIVE_SERVICE.call(native.qmt_universe)
        stale = []
        for i in range(0, len(universe), 200):
            part = universe[i:i + 200]
            bars = QN_NATIVE_SERVICE.call(native.qmt_daily_bars, part, start, target)
            stale.extend(c for c in part if not bars.get(c) or bars[c][-1]['date'] < target)
        self.log('daily cache keeper: %d of %d stocks lack %s in QMT\'s cache' % (len(stale), len(universe), target))
        if target == today and stale:
            # today's bars only exist on QMT's data server some time after the close: try a few first
            probe = stale[:3]
            for code in probe:
                QN_NATIVE_SERVICE.call(native.qmt_download_daily, code, start, target)
            again = QN_NATIVE_SERVICE.call(native.qmt_daily_bars, probe, start, target)
            if not any(again.get(c) and again[c][-1]['date'] >= target for c in probe):
                self.log('daily cache keeper: QMT has no %s daily bars yet; trying again in 10 minutes' % target)
                return
        t0 = _htime.time()
        for n, code in enumerate(stale, 1):
            if self.stopped or QN_SHUTDOWN[0] or not self._cache_allowed():
                self.log('daily cache keeper: paused after %d of %d downloads' % (n - 1, len(stale)))
                return
            QN_NATIVE_SERVICE.call(native.qmt_download_daily, code, start, target)
            if n % 500 == 0:
                self.log('daily cache keeper: %d of %d downloaded (%.0fs)' % (n, len(stale), _htime.time() - t0))
        self.cache_done = target
        self.log('daily cache keeper: cache current through %s (%d downloads, %.0fs)' % (
            target, len(stale), _htime.time() - t0))

    def _persist_inventory(self):
        out = {}
        prefix = QN_ROOT + '/'
        for a in sorted(QN_VFS.files):
            if not a.startswith(prefix):
                continue
            rel = a[len(prefix):]
            if not any(p.search(rel) for p in QN_INVENTORY_INCLUDE):
                continue
            mtime = QN_VFS.mtimes.get(a, 0.0)
            if mtime == QN_SEED_MTIME:
                continue
            data = QN_VFS.files[a]
            out[rel] = [mtime, len(data), qn_content_sha(data)]
        # files of the previous run not rebuilt yet keep their entry until they are
        for a, v in QN_VFS.prev_inventory.items():
            rel = a[len(prefix):] if a.startswith(prefix) else None
            if rel and rel not in out and a not in QN_VFS.files:
                out[rel] = list(v)
        return out

    def _persist_loop(self):
        last_export = 0.0
        while not self.stopped and not QN_SHUTDOWN[0]:
            for _ in range(50):  # 5 s, ending at once when the strategy stops
                if self.stopped or QN_SHUTDOWN[0]:
                    return
                _htime.sleep(0.1)
            try:
                self.persist(reason='periodic')
            except Exception:
                pass
            service = getattr(self.store, 'service', None)
            if service is not None:
                try:
                    note = service()
                    if note:
                        self.log(note)
                except Exception as exc:
                    self.log('state store check failed: %s' % exc)
            if self.deploy.get('debug_export_dir') and _htime.time() - last_export > 120:
                last_export = _htime.time()
                self._debug_export()

    def _debug_export(self):
        """Shadow validation only (before 2026-10-23 file access still works): copy reports,
        live processed tables, today's raw tables, state and logs to a disk folder."""
        try:
            out_dir = self.deploy['debug_export_dir']
            prefix = QN_ROOT + '/'
            keep = _hre.compile(r'^(reports/|data/processed/[^/]+\.(csv|json)$|data/raw/limit_list/|data/state/|'
                                r'qn_state/equity_stop/(state\.json|equity_stop_work/strict_feature_pool\.csv)|logs/)')
            today = _bj_now().strftime('%Y%m%d')
            for a in list(QN_VFS.files):
                rel = a[len(prefix):] if a.startswith(prefix) else None
                if rel is None or not (keep.search(rel) or (rel.startswith('data/raw/') and today in rel)):
                    continue
                dest = _hos.path.join(out_dir, *rel.split('/'))
                if not _hos.path.isdir(_hos.path.dirname(dest)):
                    _hos.makedirs(_hos.path.dirname(dest))
                with open(dest, 'wb') as handle:
                    handle.write(QN_VFS.files[a])
            with open(_hos.path.join(out_dir, 'daemon_log_tail.txt'), 'w', encoding='utf-8') as handle:
                handle.write('\n'.join(list(QN_LOG_LINES)[-4000:] + ['--- host'] + self.messages[-200:]
                                       + ['--- print'] + list(QN_PRINT_LINES)[-2000:]))
            if self.broker is not None and hasattr(self.broker, 'log'):
                with open(_hos.path.join(out_dir, 'paper_broker_log.txt'), 'w', encoding='utf-8') as handle:
                    handle.write('\n'.join(self.broker.log[-2000:]))
        except Exception as exc:
            self.log('debug export skipped: %s' % exc)
            self.deploy['debug_export_dir'] = ''

    def persist(self, force=False, reason=''):
        if self.store is None:
            return
        with self.persist_lock:
            return self._persist_locked(force, reason)

    def persist_on_callback_thread(self, reason='', slow_limit=None):
        """Forced save from the QMT callback thread. The state thread may hold the lock while
        it waits for this very thread to run its py_key calls, so keep serving those jobs
        until the lock is free instead of blocking on it."""
        if self.store is None:
            return
        while not self.persist_lock.acquire(False):
            QN_NATIVE_SERVICE.pump(0.5)
            _htime.sleep(0.01)
        try:
            return self._persist_locked(True, reason, slow_limit)
        finally:
            self.persist_lock.release()

    def persist_before_order(self):
        """The save before an order, bounded by the trade server's speed (STORE_SLOW_RECENT).
        It never refuses the order: a journal that misses the order is caught by the start-up
        check against the broker (_check_restored_state), which holds trading until checked."""
        if self.store is None:
            return
        if not self._saves_via_broker():
            try:
                self.persist_on_callback_thread(reason='before passorder')
            except Exception as exc:
                self.persist_clock = None
                self.log('order placed although the save before it did not complete (%s)' % str(exc)[:160])
            return
        if not self.link_ready():
            self.persist_clock = None
            self.log('order placed without the save before it: trade account shows %s (trade server not '
                     'connected); the journal goes into the next save' % _status_text(self.account_status or ()))
            return
        ago = _htime.time() - self.store.last_slow_at
        if self.store.last_slow_at and ago < self.STORE_SLOW_RECENT:
            self.persist_clock = None
            self.log('order placed without the save before it: a key-value call took %.0fs %.0fs ago '
                     '(trade server slow); the journal goes into the next save' % (self.store.last_slow_seconds, ago))
            return
        try:
            self.persist_on_callback_thread(reason='before passorder', slow_limit=self.STORE_SLOW_CALL)
        except Exception as exc:
            self.persist_clock = None
            self.log('order placed although the save before it did not complete (%s); the journal goes '
                     'into the next save' % str(exc)[:160])

    def _verify_store(self, force):
        if force or self._background_busy() or self.periodic_slow_limit() == self.STORE_SLOW_CALL:
            return  # not before an order, not during the pipeline, not in the trading session
        now = _htime.time()
        if now - self.verified_at < self.VERIFY_INTERVAL:
            return
        self.verified_at = now
        problem = self.store.verify()
        if problem:
            self.store.forget_bodies()
            self.persist_clock = None
            self.persist_last = 0.0
            self.log('STATE CHECK FAILED (%s): every file is written again with the next save' % problem)
        else:
            self.log('state check: committed snapshot reads back completely')

    def periodic_slow_limit(self):
        hm = _bj_now().strftime('%H%M')
        if self.TRADING_SESSION[0] <= hm < self.TRADING_SESSION[1]:
            return self.STORE_SLOW_CALL
        return self.STORE_SLOW_CALL_OFF_HOURS

    def store_daytime(self):
        hour = _bj_now().hour
        return self.STORE_DAY_START <= hour < self.STORE_DAY_END

    def _persist_core(self, files, inventory):
        return _hsha.sha256(_hjson.dumps([sorted((k, v[1], v[2]) for k, v in files.items() if not k.startswith('logs/')),
                                          sorted(inventory.items())]).encode()).hexdigest()

    def _persist_locked(self, force, reason, slow_limit=None):
        if QN_SHUTDOWN[0]:
            # a stopped or taken-over copy must never overwrite the state of the copy that runs now
            if force:
                raise RuntimeError('A_System copy stopped or taken over; state not saved')
            return
        if self.boot_pending:
            return  # nothing restored yet: saving now would overwrite the real state
        now = _htime.time()
        broker = self._saves_via_broker()
        if not force and broker:
            if not self.store_daytime():
                night = _bj_now().strftime('%Y%m%d%H')[:8]
                if self.persist_night_logged != night:
                    self.persist_night_logged = night
                    self.log('night: periodic state saves paused until %02d:00 (broker trade server '
                             'disconnects at night)' % self.STORE_DAY_START)
                return
            if self.store_link_down():
                if not self.store_waits_for_link:
                    self.store_waits_for_link = True
                    self.log('periodic state saves wait: trade account shows %s; the state store worked with %s' % (
                        _status_text(self.account_status), ' / '.join(sorted(_status_text(g) for g in self.account_status_good))))
                return
            if self.account_status and self.link_up_since and now - self.link_up_since < self.PERIODIC_LINK_STABLE:
                return  # connected again (or just started), but not for long enough yet
            if self.store_waits_for_link:
                self.store_waits_for_link = False
                self.log('trade account status back to %s: periodic state saves resume' % _status_text(self.account_status))
        if not force:
            interval = self.PERSIST_INTERVAL_BUSY if self._background_busy() else self.PERSIST_INTERVAL
            if now < self.persist_retry_at or now - self.persist_last < interval:
                return
            if ((not broker or self.periodic_slow_limit() == self.STORE_SLOW_CALL) and self.persist_clock is not None
                    and now - self.persist_saved_at < self.SESSION_LOG_SAVE_SECONDS):
                qn_sqlite_persist_all()
                if self._persist_core(self._persist_files(), self._persist_inventory()) == self.persist_core:
                    self.persist_last = now  # only health files changed: no key-value call now
                    return
        limit = slow_limit if slow_limit is not None else (0.0 if force else self.periodic_slow_limit())
        try:
            self.store.slow_limit = limit
            self.store.saving = True
            try:
                switched = self.store.follow_server()
            finally:
                self.store.saving = False
            if switched:
                self.persist_clock = None   # this server needs our snapshot even if no file changed
                self.verified_at = 0.0      # and it is read back right after (outside the session)
                self.log(switched)
            qn_sqlite_persist_all()
            files = self._persist_files()
            inventory = self._persist_inventory()
            fingerprint = _hsha.sha256(_hjson.dumps([sorted((k, v[1], v[2]) for k, v in files.items()),
                                                     sorted(inventory.items())]).encode()).hexdigest()
            if not force and fingerprint == self.persist_clock:
                self.persist_last = now
                return
            self.store.slow_limit = limit
            self.save_stats[1] += 1
            self.persist_meta = self.store.save(files, dict(release=self.deploy.get('release'), reason=reason,
                                                            inventory=inventory))
            self.save_stats[0] += 1
            self.persist_clock = fingerprint
            self.persist_core = self._persist_core(files, inventory)
            self.persist_saved_at = now
            self.persist_last = now
            self.persist_error = ''
            if self.persist_failures:
                self.log('state save works again after %d failed attempts' % self.persist_failures)
            self.persist_failures = 0
            if self.account_status is not None:
                self.account_status_good.add(self.account_status)  # the store works with this status
            self.persist_retry_at = 0.0
            self._verify_store(force)
        except Exception as exc:
            self.persist_error = '%s: %s' % (type(exc).__name__, exc)
            self.persist_failures += 1
            delay = min(self.STORE_BACKOFF_MAX, 60.0 * (2 ** min(self.persist_failures - 1, 6)))
            if any(mark in self.persist_error for mark in self.STORE_LINK_ERRORS):
                delay = self.STORE_LINK_BACKOFF[min(self.persist_failures, len(self.STORE_LINK_BACKOFF)) - 1]
            self.persist_retry_at = _htime.time() + delay
            self.log('STATE SAVE FAILED (%s, attempt %d, next periodic try in %ds): %s' % (
                reason, self.persist_failures, delay, self.persist_error))
            if force:
                raise

    # ------------------------------------------------------------------ QMT callbacks
    def pump(self):
        if QN_SHUTDOWN[0] and not self.stopped:
            self.stopped = True
            self.log('another A_System copy in this QMT took over; this copy is inactive and saves no state')
        if self.stopped:
            return
        if self.boot_pending:
            if _htime.time() >= self.boot_retry_at:
                self._boot_state()
            return
        if self.broker is not None and hasattr(self.broker, 'match'):
            try:
                for deal in self.broker.match():
                    self.engine.callback('deal', deal)
            except Exception as exc:
                self.log('paper broker error: %s' % exc)
        self.engine.pump()
        QN_NATIVE_SERVICE.pump()
        self._hold_check()
        self._poll_switch()
        self._poll_calendar()
        self._supervise()
        self._echo_daemon_log()
        now = _htime.time()
        alive_now = bool(self.daemon_thread and self.daemon_thread.is_alive())
        if alive_now != self.daemon_was_alive:
            # the trading daemon started or ended (e.g. ~0.3 s after a pause): report it right away,
            # stop_windows.py / start_windows.py wait for this line (11-05: stop waited 60 s for it)
            self.daemon_was_alive = alive_now
            self.status_last = 0.0
        interval = self.STATUS_PAUSED_SECONDS if self.trading == 'pause' else self.STATUS_SECONDS
        if now - self.status_last >= interval:
            self.status_last = now
            self._trading_notice(now)
            last = list(QN_LOG_LINES)[-1:] or list(QN_PRINT_LINES)[-1:] or ['']
            where = ''
            try:
                import sys as _hsys
                frame = _hsys._current_frames().get(self.daemon_thread.ident) if self.daemon_thread else None
                if frame is not None:
                    where = _qn_where(frame, 4)
            except Exception as exc:
                where = 'stack unavailable: %s' % exc
            self._export_files()
            self._report_silent_scripts(now)
            leftover = self.end_leftover_threads()
            self.log('status: trading=%s daemon_alive=%s log_lines=%d data_jobs_waiting=%d leftover_threads=%d %s last="%s" daemon_at=%s' % (
                self.trading, bool(self.daemon_thread and self.daemon_thread.is_alive()), len(QN_LOG_LINES),
                QN_NATIVE_SERVICE.waiting(), len(leftover), self._gil_report(), last[0][-120:], where))
        self._yield_to_background()

    # In 模型交易 QMT keeps Python's lock (GIL) between our timer callbacks, so background threads
    # only run while a callback runs. Measured 10-22 evening: the close pipeline ran 4-35x slower
    # than in the editor's run mode while the QMT process idled (step 13 hit its 30-minute limit).
    # While pipeline work is pending, each tick sleeps this long at its end, releasing the lock.
    # Otherwise a short window per tick still lets the daemon finish its small steps (each sqlite
    # call or sleep gives the lock up and waited a whole tick to get it back: bg_lag 80-100 ms
    # idle on 10-22) and read the engine's reply in the same tick.
    YIELD_SECONDS = 0.08
    YIELD_IDLE_SECONDS = 0.02
    PROBE_SLEEP = 0.02

    def _gil_probe_loop(self):
        while not self.stopped and not QN_SHUTDOWN[0]:
            t0 = _htime.monotonic()
            _qn_sleep(self.PROBE_SLEEP)
            qn_freeze_beat()
            self.bg_lag.append(_htime.monotonic() - t0 - self.PROBE_SLEEP)
            if len(self.bg_lag) > 2000:
                del self.bg_lag[:1000]

    def _background_busy(self):
        if QN_NATIVE_SERVICE.waiting():
            return True
        for proc in list(QN_PROCESSES.values()):
            thread = getattr(proc, '_thread', None)
            if thread is not None and thread.is_alive():
                return True
        return False

    def _yield_to_background(self):
        self.yield_ticks[1] += 1
        if self._background_busy():
            self.yield_ticks[0] += 1
            _htime.sleep(self.YIELD_SECONDS)
        else:
            _htime.sleep(self.YIELD_IDLE_SECONDS)

    def _gil_report(self):
        lag, self.bg_lag = self.bg_lag, []
        ticks, self.yield_ticks = self.yield_ticks, [0, 0]
        avg = sum(lag) / len(lag) if lag else 0.0
        text = 'bg_lag=%dms/%dms n=%d yield=%d/%d' % (avg * 1000, max(lag or [0]) * 1000, len(lag), ticks[0], ticks[1])
        if self.store is not None:
            spent = self.store.call_seconds - self.save_seconds_mark
            self.save_seconds_mark = self.store.call_seconds
            saves, self.save_stats = self.save_stats, [0, 0]
            text += ' saves=%d/%d kv=%.1fs' % (saves[0], saves[1], spent)
        return text

    SILENT_SCRIPT_SECONDS = 300.0

    def _report_silent_scripts(self, now):
        """A pipeline script that printed nothing for 5 minutes: log where its thread is (every
        5 minutes while it lasts), so a hang shows its place in the QMT log (10-22 step 13 hung
        without output or CPU until its 30-minute limit)."""
        try:
            import sys as _hsys
            frames = _hsys._current_frames()
            for proc in list(QN_PROCESSES.values()):
                thread = getattr(proc, '_thread', None)
                if thread is None or not thread.is_alive():
                    continue
                quiet = now - getattr(proc, 'last_output', now)
                if quiet < self.SILENT_SCRIPT_SECONDS or now - getattr(proc, '_qn_reported', 0.0) < self.SILENT_SCRIPT_SECONDS:
                    continue
                proc._qn_reported = now
                frame = frames.get(thread.ident)
                where = _qn_where(frame, 12) if frame is not None else ''
                self.log('script %s silent for %ds (running %ds%s); at: %s' % (
                    thread.name, quiet, now - getattr(proc, 'started', now), ', killed' if proc.killed else '',
                    where or 'no frame'))
        except Exception as exc:
            self.log('silent-script report failed: %s' % exc)

    def callback(self, kind, value):
        if self.engine is not None:
            self.engine.callback(kind, value)

    def stop(self):
        taken_over = QN_SHUTDOWN[0]
        self.stopped = True
        if not taken_over and self.deploy.get('stop_push', self.deploy.get('mode') == 'live'):
            # 11-05 23:29: QMT was closed and nothing was pushed (the stall alarm comes 10 minutes later).
            # Say so at once, before the final save that can take minutes.
            self._push('⏹ A_System策略已停止',
                       'QMT里的A_SYSTEM在 %s（北京时间）停止运行：在“模型交易”点了■，或QMT被关闭/退出。停止期间不交易，'
                       '也不执行止损、止盈、到期卖出。恢复：QMT开着就在“模型交易”的A_SYSTEM行点▶；QMT关了就重新打开并登录，'
                       'A_SYSTEM会自动启动（交易开关不变）。' % _bj_now().strftime('%m-%d %H:%M'))
        try:
            if taken_over:
                self.log('strategy stop: this copy was taken over earlier; state left to the running copy')
            elif self.boot_pending:
                self.log('strategy stop: state was never restored; nothing saved')
            elif self._saves_via_broker() and not self.store_daytime():
                self.log('strategy stop at night: last saved state (%s) kept; night changes are rebuilt '
                         'on the next start' % (self.persist_meta or {}).get('saved_at', 'never'))
            elif self._saves_via_broker() and not self.link_ready():
                self.log('strategy stop: trade account shows %s (trade server not connected); last saved state '
                         '(%s) kept, later changes are rebuilt on the next start' % (
                             _status_text(self.account_status or ()), (self.persist_meta or {}).get('saved_at', 'never')))
            else:
                # 11-05 11:49: this save took minutes (slow trade server) while the trading daemon kept
                # running; it saw the engine heartbeat stop and pushed false "bridge lost" alarms. End
                # trading first (as a pause does), then save.
                daemon = self.daemon_thread
                if self._kill_thread(daemon):
                    daemon.join(5)
                    self.log('strategy stop: trading daemon ended before the final state save')
                if hasattr(self.store, 'print_all_next'):
                    self.store.print_all_next = True  # complete on its own: no second chance after a stop
                try:
                    self.persist_on_callback_thread(reason='strategy stop')
                except Exception:
                    pass  # logged by the save itself; the last saved state stays the restart point
        finally:
            QN_SHUTDOWN[0] = True
            if self.engine is not None:
                self.engine.close()
            ended = [t.name for t in list(QN_THREADS) if t is not _hthr.current_thread() and self._kill_thread(t)]
            self.log('strategy stopped; ended threads: %s' % (', '.join(ended) or 'none'))
