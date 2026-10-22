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
import re as _hre
import threading as _hthr
import time as _htime
import traceback as _htb
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


class QnStateStore(object):
    """Content-addressed VFS snapshot in QMT's key-value store.

    Each file body is stored once under a key derived from its SHA-256 (zlib + base64,
    split into chunks); a manifest maps path -> (sha, mtime, size). The manifest is
    written last and read back, so a snapshot only counts once complete. Bodies no
    longer referenced by the current or previous manifest are cleared."""

    def __init__(self, getter, saver, prefix=QN_STATE_PREFIX):
        self.getter = getter
        self.saver = saver
        self.prefix = prefix
        self.known = {}  # sha -> number of chunks already stored
        self.previous = {}
        self.slot = ''
        self.pointer = ''
        self.protected = set()  # bodies of a snapshot whose pointer write did not confirm
        # A key-value call blocks QMT's whole Python until the trade server answers (up to about
        # a minute when it is flaky). The caller reports each call's duration in
        # last_call_seconds; during a save, one call slower than slow_limit gives the save up.
        self.slow_limit = 0.0
        self.last_call_seconds = 0.0
        self.saving = False

    def _check_slow(self, what):
        if self.saving and self.slow_limit and self.last_call_seconds > self.slow_limit:
            raise RuntimeError('%s took %.0fs (broker trade server slow); save given up' % (
                what, self.last_call_seconds))

    def _get(self, key):
        self.last_call_seconds = 0.0
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
        result = self.saver(key, value)
        self._check_slow('save_py_key(%s)' % key)
        if isinstance(result, (tuple, list)) and len(result) >= 1 and result[0] not in (0, None):
            raise RuntimeError('save_py_key(%s, %d chars) rejected: %r' % (key, len(value), result))
        return result

    def _body_keys(self, sha, chunks):
        return ['%s_F_%s_%d' % (self.prefix, sha, i) for i in range(chunks)]

    def _put_body(self, data):
        sha = _hsha.sha256(data).hexdigest()[:32]
        if sha in self.known:
            return sha, self.known[sha]
        packed = _hb64.b64encode(_hzlib.compress(data, 6)).decode('ascii')
        chunks = [packed[i:i + QN_STATE_CHUNK] for i in range(0, len(packed), QN_STATE_CHUNK)] or ['']
        keys = self._body_keys(sha, len(chunks))
        for key, chunk in zip(keys, chunks):
            self._set(key, chunk)
        for key, chunk in zip(keys, chunks):
            if self._get(key) != chunk:
                raise RuntimeError('state body %s did not read back' % key)
        self.known[sha] = len(chunks)
        return sha, len(chunks)

    def _get_body(self, sha, chunks):
        packed = ''.join(self._get(k) for k in self._body_keys(sha, chunks))
        data = _hzlib.decompress(_hb64.b64decode(packed))
        if _hsha.sha256(data).hexdigest()[:32] != sha:
            raise RuntimeError('persisted state body %s checksum mismatch' % sha)
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
                sha, chunks = self._put_body(data)
                manifest[rel] = [sha, mtime, size, chunks]
        text = _hjson.dumps(dict(meta_extra, files=manifest, saved_at=_bj_now().strftime('%Y-%m-%d %H:%M:%S')),
                            sort_keys=True)
        packed = _hb64.b64encode(_hzlib.compress(text.encode('utf-8'), 9)).decode('ascii')
        slot = 'B' if self.slot == 'A' else 'A'
        chunks = self._put_text_chunks('%s_M%s' % (self.prefix, slot), packed)
        pointer = '%s:%d:%s' % (slot, chunks, _hsha.sha256(packed.encode('ascii')).hexdigest()[:32])
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
        self.slot = slot
        self.pointer = pointer
        older, self.previous = self.previous, manifest
        live = bodies | self.protected | set((v[0], v[3]) for v in older.values() if v[0])
        # Clear unreferenced bodies. Forget a body before clearing it: if a clear fails half way
        # the body is written again the next time some file has that content.
        for sha, n in list(self.known.items()):
            if (sha, n) in live:
                continue
            del self.known[sha]
            try:
                for key in self._body_keys(sha, n):
                    self._set(key, '')
            except Exception:
                break  # clean-up only; leftovers are harmless
        return dict(meta_extra, inventory=len(meta_extra.get('inventory') or {}), files=len(manifest),
                    bodies=len(self.known), bytes=len(packed),
                    saved_at=_bj_now().strftime('%Y-%m-%d %H:%M:%S'))

    def load(self):
        """The latest snapshot, or the one before it when the latest cannot be read completely."""
        pointer = self._get(self.prefix + '_PTR')
        if not pointer:
            return None, None
        try:
            return self._load_pointer(pointer)
        except Exception as exc:
            previous = self._get(self.prefix + '_PTRPREV')
            if not previous or previous == pointer:
                raise
            self.known, self.previous = {}, {}
            files, meta = self._load_pointer(previous)
            meta['restored_from'] = 'previous snapshot (latest unreadable: %s: %s)' % (type(exc).__name__, exc)
            return files, meta

    def _load_pointer(self, pointer):
        slot, chunks, digest = pointer.split(':')
        packed = ''.join(self._get('%s_M%s_%d' % (self.prefix, slot, i)) for i in range(int(chunks)))
        if _hsha.sha256(packed.encode('ascii')).hexdigest()[:32] != digest:
            raise RuntimeError('persisted state manifest checksum mismatch; refusing a partial state')
        meta = _hjson.loads(_hzlib.decompress(_hb64.b64decode(packed)).decode('utf-8'))
        self.slot = slot
        files = {}
        for rel, (sha, mtime, size, chunks) in meta['files'].items():
            if sha is None:
                files[rel] = (None, mtime, size)
            else:
                files[rel] = (self._get_body(sha, chunks), mtime, size)
                self.known[sha] = chunks
        self.previous = meta['files']
        self.pointer = pointer
        return files, dict((k, v) for k, v in meta.items() if k != 'files')


class QnHost(object):
    PUMP_MS = 100
    PERSIST_INTERVAL = 30.0
    DAEMON_RESTART_DELAY = 60.0
    BOOT_RETRY_SECONDS = 60.0
    # QMT's key-value store goes through the broker's trade server, which disconnects at night
    # (calls then fail, time out, or may not return at all). Periodic saves run in the day only,
    # back off after a failure, and night-time state (the evening pipeline catch-up) is either
    # saved the next morning or rebuilt after a restart.
    STORE_DAY_START = 8
    STORE_DAY_END = 20
    STORE_BACKOFF_MAX = 1800.0
    # every call blocks the QMT callback thread (and the engine's bridge heartbeat with it)
    STORE_SLOW_CALL = 5.0

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
        if store is not None:
            store.slow_limit = self.STORE_SLOW_CALL
        self.engine = None
        self.daemon_thread = None
        self.daemon_restarts = 0
        self.daemon_next_start = 0.0
        self.stopped = False
        self.persist_dirty = False
        self.persist_last = 0.0
        self.persist_clock = 0.0
        self.persist_meta = None
        self.persist_error = ''
        self.persist_lock = _hthr.RLock()
        self.persist_thread = None
        self.status_last = 0.0
        self.bg_lag = []        # how late a background thread woke from a 20 ms sleep (seconds)
        self.yield_ticks = [0, 0]  # [ticks that handed the lock to background threads, ticks]
        self.boot_pending = False
        self.boot_retry_at = 0.0
        if deploy.get('store_day_hours'):  # tests only
            self.STORE_DAY_START, self.STORE_DAY_END = deploy['store_day_hours']
        self.persist_failures = 0
        self.persist_retry_at = 0.0
        self.persist_night_logged = ''
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
        try:
            self._seed_vfs()
        except Exception as exc:
            self.boot_pending = True
            self.boot_retry_at = _htime.time() + self.BOOT_RETRY_SECONDS
            self.log('QMT state store not readable (%s: %s); trading not started, retry in %ds' % (
                type(exc).__name__, exc, self.BOOT_RETRY_SECONDS))
            return
        self.boot_pending = False
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
        self._start_daemon()
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
            files, meta = self.store.load()
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
        host = self

        def passorder(*args, **kwargs):
            if host.stopped or QN_SHUTDOWN[0]:
                raise RuntimeError('A_System copy stopped or taken over; order refused')
            # The engine's submission journal is committed just before this call. Make it durable
            # in QMT's key-value store before the order can exist at the broker.
            host.persist_on_callback_thread(reason='before passorder')
            return real_passorder(*args, **kwargs)
        api['passorder'] = passorder
        return api

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
                    self.log('trading daemon thread ended (strategy run stopped)')
                else:
                    self.log('trading daemon crashed:\n' + _htb.format_exc()[-3000:])
        self.daemon_thread = QnThread(target=target, name='a-system-daemon', daemon=True)
        self.daemon_thread._qn_kill = tuple(self.daemon_thread._qn_kill) + (token,)
        self.daemon_thread.start()

    def _supervise(self):
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
    CACHE_WINDOWS = (('0500', '0905'), ('1535', '2300'))
    CACHE_HISTORY_DAYS = 80  # the collector reads ENSURE_DAYS + HISTORY_DAYS open days back

    def _cache_allowed(self):
        hm = _bj_now().strftime('%H%M')
        return any(a <= hm < b for a, b in self.CACHE_WINDOWS)

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
        past = [d for d in days if d < today]
        if not past or self.cache_done == past[-1]:
            return
        target = past[-1]
        start = days[max(0, days.index(target) - self.CACHE_HISTORY_DAYS)]
        native = QnLoader(self.codebase).load('qn.native')
        universe = QN_NATIVE_SERVICE.call(native.qmt_universe)
        stale = []
        for i in range(0, len(universe), 200):
            part = universe[i:i + 200]
            bars = QN_NATIVE_SERVICE.call(native.qmt_daily_bars, part, start, target)
            stale.extend(c for c in part if not bars.get(c) or bars[c][-1]['date'] < target)
        self.log('daily cache keeper: %d of %d stocks lack %s in QMT\'s cache' % (len(stale), len(universe), target))
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
            _htime.sleep(5.0)
            try:
                self.persist(reason='periodic')
            except Exception:
                pass
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

    def persist_on_callback_thread(self, reason=''):
        """Forced save from the QMT callback thread. The state thread may hold the lock while
        it waits for this very thread to run its py_key calls, so keep serving those jobs
        until the lock is free instead of blocking on it."""
        if self.store is None:
            return
        while not self.persist_lock.acquire(False):
            QN_NATIVE_SERVICE.pump(0.5)
            _htime.sleep(0.01)
        try:
            return self._persist_locked(True, reason)
        finally:
            self.persist_lock.release()

    def store_daytime(self):
        hour = _bj_now().hour
        return self.STORE_DAY_START <= hour < self.STORE_DAY_END

    def _persist_locked(self, force, reason):
        if QN_SHUTDOWN[0]:
            # a stopped or taken-over copy must never overwrite the state of the copy that runs now
            if force:
                raise RuntimeError('A_System copy stopped or taken over; state not saved')
            return
        if self.boot_pending:
            return  # nothing restored yet: saving now would overwrite the real state
        now = _htime.time()
        if not force:
            if not self.store_daytime():
                night = _bj_now().strftime('%Y%m%d%H')[:8]
                if self.persist_night_logged != night:
                    self.persist_night_logged = night
                    self.log('night: periodic state saves paused until %02d:00 (broker trade server '
                             'disconnects at night)' % self.STORE_DAY_START)
                return
            if now < self.persist_retry_at or now - self.persist_last < self.PERSIST_INTERVAL:
                return
        try:
            qn_sqlite_persist_all()
            files = self._persist_files()
            inventory = self._persist_inventory()
            fingerprint = _hsha.sha256(_hjson.dumps([sorted((k, v[1], v[2]) for k, v in files.items()),
                                                     sorted(inventory.items())]).encode()).hexdigest()
            if not force and fingerprint == self.persist_clock:
                self.persist_last = now
                return
            self.persist_meta = self.store.save(files, dict(release=self.deploy.get('release'), reason=reason,
                                                            inventory=inventory))
            self.persist_clock = fingerprint
            self.persist_last = now
            self.persist_error = ''
            if self.persist_failures:
                self.log('state save works again after %d failed attempts' % self.persist_failures)
            self.persist_failures = 0
            self.persist_retry_at = 0.0
        except Exception as exc:
            self.persist_error = '%s: %s' % (type(exc).__name__, exc)
            self.persist_failures += 1
            delay = min(self.STORE_BACKOFF_MAX, 60.0 * (2 ** min(self.persist_failures - 1, 6)))
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
        self._supervise()
        now = _htime.time()
        if now - self.status_last >= 20:
            self.status_last = now
            last = list(QN_LOG_LINES)[-1:] or list(QN_PRINT_LINES)[-1:] or ['']
            where = ''
            try:
                import sys as _hsys
                frame = _hsys._current_frames().get(self.daemon_thread.ident) if self.daemon_thread else None
                if frame is not None:
                    stack = _htb.extract_stack(frame)[-4:]
                    where = ' <- '.join('%s:%s %s' % (f[0].rsplit('/', 1)[-1], f[1], f[2]) for f in reversed(stack))
            except Exception as exc:
                where = 'stack unavailable: %s' % exc
            self._export_files()
            self._report_silent_scripts(now)
            leftover = self.end_leftover_threads()
            self.log('status: daemon_alive=%s log_lines=%d data_jobs_waiting=%d leftover_threads=%d %s last="%s" daemon_at=%s' % (
                bool(self.daemon_thread and self.daemon_thread.is_alive()), len(QN_LOG_LINES),
                QN_NATIVE_SERVICE.waiting(), len(leftover), self._gil_report(), last[0][-120:], where))
        self._yield_to_background()

    # In 模型交易 QMT keeps Python's lock (GIL) between our timer callbacks, so background threads
    # only run while a callback runs. Measured 10-22 evening: the close pipeline ran 4-35x slower
    # than in the editor's run mode while the QMT process idled (step 13 hit its 30-minute limit).
    # While pipeline work is pending, each tick sleeps this long at its end, releasing the lock.
    YIELD_SECONDS = 0.08
    PROBE_SLEEP = 0.02

    def _gil_probe_loop(self):
        while not self.stopped and not QN_SHUTDOWN[0]:
            t0 = _htime.monotonic()
            _qn_sleep(self.PROBE_SLEEP)
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

    def _gil_report(self):
        lag, self.bg_lag = self.bg_lag, []
        ticks, self.yield_ticks = self.yield_ticks, [0, 0]
        avg = sum(lag) / len(lag) if lag else 0.0
        return 'bg_lag=%dms/%dms n=%d yield=%d/%d' % (avg * 1000, max(lag or [0]) * 1000, len(lag), ticks[0], ticks[1])

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
                stack = _htb.extract_stack(frame)[-12:] if frame is not None else []
                where = ' <- '.join('%s:%s %s' % (f[0].replace('\\', '/').rsplit('/', 1)[-1], f[1], f[2])
                                    for f in reversed(stack))
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
        try:
            if taken_over:
                self.log('strategy stop: this copy was taken over earlier; state left to the running copy')
            elif self.boot_pending:
                self.log('strategy stop: state was never restored; nothing saved')
            elif not self.store_daytime():
                self.log('strategy stop at night: last saved state (%s) kept; night changes are rebuilt '
                         'on the next start' % (self.persist_meta or {}).get('saved_at', 'never'))
            else:
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
