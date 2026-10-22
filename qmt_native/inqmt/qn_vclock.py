# coding: utf-8
# Offline test only: accelerated virtual clock for the embedded modules (never shipped).
# Installs scoped replacements of time / datetime / threading.Event / queue.Queue waits so
# a whole trading day of the unchanged daemon runs in minutes against the mock QMT.
import datetime as _vdt
import queue as _vq
import threading as _vth
import time as _vt


class QnVirtualClock(object):
    def __init__(self, start_epoch, speed):
        self.base_real = _vt.time()
        self.base_virtual = float(start_epoch)
        self.speed = float(speed)

    def now(self):
        return self.base_virtual + (_vt.time() - self.base_real) * self.speed

    def set_speed(self, speed):
        if float(speed) != self.speed:
            self.base_virtual = self.now()
            self.base_real = _vt.time()
            self.speed = float(speed)

    def jump_to(self, epoch):
        self.base_virtual = float(epoch)
        self.base_real = _vt.time()

    def real_seconds(self, virtual_seconds):
        return max(0.0, float(virtual_seconds) / self.speed)


def install_virtual_clock(loader_cls, clock):
    real_dt = _vdt.datetime
    real_date = _vdt.date

    class _DtMeta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, real_dt)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, real_dt)

    class VDatetime(real_dt, metaclass=_DtMeta):
        @classmethod
        def now(cls, tz=None):
            return real_dt.fromtimestamp(clock.now(), tz)

        @classmethod
        def today(cls):
            return real_dt.fromtimestamp(clock.now())

        @classmethod
        def utcnow(cls):
            return real_dt.utcfromtimestamp(clock.now())

    class _DateMeta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, real_date)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, real_date)

    class VDate(real_date, metaclass=_DateMeta):
        @classmethod
        def today(cls):
            return real_dt.fromtimestamp(clock.now()).date()

    vdatetime = loader_cls_module('datetime', _vdt, {'datetime': VDatetime, 'date': VDate})

    def _sleep(s):
        _vt.sleep(clock.real_seconds(s))

    def _strftime(fmt, t=None):
        return _vt.strftime(fmt, t if t is not None else _vt.localtime(clock.now()))

    vtime = loader_cls_module('time', _vt, {
        'time': clock.now, 'sleep': _sleep, 'monotonic': clock.now, 'perf_counter': clock.now,
        'time_ns': lambda: int(clock.now() * 1e9), 'monotonic_ns': lambda: int(clock.now() * 1e9),
        'strftime': _strftime, 'localtime': lambda t=None: _vt.localtime(clock.now() if t is None else t),
        'gmtime': lambda t=None: _vt.gmtime(clock.now() if t is None else t)})

    class VEvent(_vth.Event):
        def wait(self, timeout=None):
            return _vth.Event.wait(self, None if timeout is None else clock.real_seconds(timeout))

    class VCondition(_vth.Condition):
        def wait(self, timeout=None):
            return _vth.Condition.wait(self, None if timeout is None else clock.real_seconds(timeout))

    vthreading = loader_cls_module('threading', _vth, {'Event': VEvent, 'Condition': VCondition})

    class VQueue(_vq.Queue):
        def get(self, block=True, timeout=None):
            return _vq.Queue.get(self, block, None if timeout is None else clock.real_seconds(timeout))

    vqueue = loader_cls_module('queue', _vq, {'Queue': VQueue})
    original_init = loader_cls.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.shims.update({'time': vtime, 'datetime': vdatetime, 'threading': vthreading, 'queue': vqueue})
    loader_cls.__init__ = patched_init
    return vtime, vdatetime


def loader_cls_module(name, real, overrides):
    import types
    m = types.ModuleType(name)
    m.__dict__.update(dict((k, v) for k, v in real.__dict__.items() if not k.startswith('__')))
    m.__dict__.update(overrides)
    return m
