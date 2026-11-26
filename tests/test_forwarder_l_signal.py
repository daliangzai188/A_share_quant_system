"""推送转发程序里的策略 L 晚间信号调度（qmt_native_phase0/qmt_notify_forwarder.py，2026-11-26）。"""
import datetime as dt
import importlib.util
import tempfile
import time
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "qmt_native_phase0" / "qmt_notify_forwarder.py"
spec = importlib.util.spec_from_file_location("qmt_notify_forwarder_l_test", PATH)
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
BJ = dt.timezone(dt.timedelta(hours=8))
DAYS = ["20261125", "20261126", "20261127"]


def test_window_evening_today_and_morning_previous_day():
    at = lambda h, m, d=26: dt.datetime(2026, 11, d, h, m, tzinfo=BJ)
    assert F.l_signal_window(at(16, 29), DAYS) == ""
    assert F.l_signal_window(at(16, 30), DAYS) == "20261126"
    assert F.l_signal_window(at(22, 59), DAYS) == "20261126"
    assert F.l_signal_window(at(23, 0), DAYS) == ""
    assert F.l_signal_window(at(8, 20, 27), DAYS) == "20261126"
    assert F.l_signal_window(at(9, 10, 27), DAYS) == ""
    assert F.l_signal_window(dt.datetime(2026, 11, 28, 17, 0, tzinfo=BJ), DAYS) == ""   # 非交易日


class _Proc:
    def __init__(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_runs_until_done_then_marks_done_and_stops(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(F, "CHECKS_DIR", Path(tmp))
        monkeypatch.setattr(F, "L_SIGNAL", PATH)          # 任意存在的文件即可
        launched = []

        def popen(cmd, **kw):
            launched.append(cmd)
            return _Proc(3 if len(launched) == 1 else 0)

        runner, now = {}, dt.datetime(2026, 11, 26, 17, 0, tzinfo=BJ)
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)
        assert launched[0][-3:] == ["--signal-date", "20261126", "--write-registry"]
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)      # 数据没出全（3），20 分钟内不再启动
        assert len(launched) == 1 and not (Path(tmp) / "l_done_20261126.json").exists()
        runner["l_started"] = time.time() - F.L_RETRY_SECONDS - 1
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)      # 20 分钟后再试
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)      # 这次返回 0 → 写完成标记
        assert len(launched) == 2 and (Path(tmp) / "l_done_20261126.json").exists()
        runner["l_started"] = 0
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)
        assert len(launched) == 2                                                  # 完成后当天不再启动
        F.maybe_run_l_signal("shadow", {}, now=now, days=DAYS, popen=popen)
        assert len(launched) == 2                                                  # 影子模式不跑
