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
    assert F.l_signal_window(at(23, 0), DAYS) == "20261126"                    # 过了 23:00 没生成照样补
    assert F.l_signal_window(at(1, 30, 27), DAYS) == "20261126"                 # 半夜补上一个交易日
    assert F.l_signal_window(at(8, 20, 27), DAYS) == "20261126"
    assert F.l_signal_window(at(9, 10, 27), DAYS) == ""                         # 开盘前 09:10 以后来不及用，不补
    assert F.l_signal_window(at(15, 0, 27), DAYS) == ""                         # 盘中不跑
    assert F.l_signal_window(dt.datetime(2026, 11, 28, 17, 0, tzinfo=BJ), DAYS) == "20261127"   # 周末补最近交易日


class _Proc:
    def __init__(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_runs_until_done_then_marks_done_and_stops(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(F, "CHECKS_DIR", Path(tmp))
        monkeypatch.setattr(F, "L_SIGNAL", PATH)          # 任意存在的文件即可
        monkeypatch.setattr(F, "l_framework_on", lambda: True)
        monkeypatch.setattr(F, "l_plan_message", lambda sig, morning: ("t", "b"))
        launched = []

        def popen(cmd, **kw):
            launched.append(cmd)
            return _Proc(3 if len(launched) == 1 else 0)

        runner, now = {}, dt.datetime(2026, 11, 26, 17, 0, tzinfo=BJ)
        pushed = []
        push = lambda title, body, alert: pushed.append((title, alert)) or True
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen, push=push)
        assert launched[0][-3:] == ["--signal-date", "20261126", "--write-registry"]
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)      # 数据没出全（3），20 分钟内不再启动
        assert len(launched) == 1 and not (Path(tmp) / "l_done_20261126.json").exists()
        runner["l_started"] = time.time() - F.L_RETRY_SECONDS - 1
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)      # 20 分钟后再试
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen, push=push)  # 返回 0 → 完成标记+推送计划
        assert len(launched) == 2 and (Path(tmp) / "l_done_20261126.json").exists()
        assert pushed == [("t", False)]
        runner["l_started"] = 0
        F.maybe_run_l_signal("live", runner, now=now, days=DAYS, popen=popen)
        assert len(launched) == 2                                                  # 完成后当天不再启动
        F.maybe_run_l_signal("shadow", {}, now=now, days=DAYS, popen=popen)
        assert len(launched) == 2                                                  # 影子模式不跑


def test_current_framework_does_not_run_l(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(F, "CHECKS_DIR", Path(tmp))
        monkeypatch.setattr(F, "L_SIGNAL", PATH)
        monkeypatch.setattr(F, "l_framework_on", lambda: False)
        launched = []
        F.maybe_run_l_signal("live", {}, now=dt.datetime(2026, 11, 26, 17, 0, tzinfo=BJ), days=DAYS,
                             popen=lambda cmd, **kw: launched.append(cmd))
        assert launched == []


def test_missing_plan_alerts_once_each(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(F, "CHECKS_DIR", Path(tmp))
        pushed = []
        push = lambda title, body, alert: pushed.append((title, body, alert)) or True
        runner = {}
        F.maybe_alert_l_missing(runner, dt.datetime(2026, 11, 26, 22, 59, tzinfo=BJ), DAYS, push)
        assert pushed == []                                                          # 23:00 前不提醒
        F.maybe_alert_l_missing(runner, dt.datetime(2026, 11, 26, 23, 1, tzinfo=BJ), DAYS, push)
        assert len(pushed) == 1 and "08:20" in pushed[0][1] and pushed[0][2]
        runner["l_alert_tried"] = 0
        F.maybe_alert_l_missing(runner, dt.datetime(2026, 11, 26, 23, 30, tzinfo=BJ), DAYS, push)
        assert len(pushed) == 1                                                      # 同一信号日只推一次
        runner["l_alert_tried"] = 0
        F.maybe_alert_l_missing(runner, dt.datetime(2026, 11, 27, 9, 15, tzinfo=BJ), DAYS, push)
        assert len(pushed) == 2 and "今日L不开仓" in pushed[1][0] and "甲·临时复核" in pushed[1][1]
        (Path(tmp) / "l_done_20261127.json").write_text("{}", encoding="utf-8")
        runner["l_alert_tried"] = 0
        F.maybe_alert_l_missing(runner, dt.datetime(2026, 11, 27, 23, 5, tzinfo=BJ), DAYS, push)
        assert len(pushed) == 2                                                      # 当天已完成不提醒


def test_plan_message_ready_and_empty(monkeypatch):
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "l.json"
        monkeypatch.setattr(F, "L_PLAN_PATH", path)
        path.write_text(_json.dumps({"signals": [
            {"signal_date": "20261126", "planned_buy_date": "20261127", "status": "SIGNAL_READY", "ts_code": "300100.SZ",
             "name": "龙一", "branch": 1, "l_allowed": False},
            {"signal_date": "20261125", "planned_buy_date": "20261126", "status": "NO_CANDIDATE", "ts_code": "",
             "branch": 2}]}, ensure_ascii=False), encoding="utf-8")
        title, body = F.l_plan_message("20261126", morning=False)
        assert title == "📋 明日L计划：龙一" and "300100.SZ" in body and "自身停手" in body and "不递补" in body
        title, body = F.l_plan_message("20261125", morning=True)
        assert title == "📋 今日L无计划"
        title, _ = F.l_plan_message("20261120", morning=False)
        assert "读不到" in title
