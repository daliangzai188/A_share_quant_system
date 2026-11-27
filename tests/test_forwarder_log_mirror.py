"""推送转发程序把 QMT 策略日志复制到同步目录（qmt_native_phase0/qmt_notify_forwarder.py，2026-11-27 用户同意）。

Mac 上的值守检查和换版核对要读当天的 QMT 日志，原来只能在虚拟机界面里手动起复制循环；现在转发程序每 2 分钟
把最新两天的日志复制到 C:\\A_System\\reports\\qmt_native\\，先写 *.tmp（不同步）再改名。
"""
import importlib.util
import json
import tempfile
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "qmt_native_phase0" / "qmt_notify_forwarder.py"
spec = importlib.util.spec_from_file_location("qmt_notify_forwarder_mirror_test", PATH)
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def _setup(tmp: str):
    src, dest = Path(tmp) / "log", Path(tmp) / "synced"
    src.mkdir()
    for day, text in (("20261125", "a" * 10), ("20261126", "b" * 20), ("20261127", "c" * 30)):
        (src / ("XtClient_FormulaOutput_%s.log" % day)).write_bytes(text.encode())
    return [str(src / "XtClient_FormulaOutput_*.log")], src, dest


def test_copies_newest_two_logs_exactly_without_tmp_left():
    with tempfile.TemporaryDirectory() as tmp:
        patterns, src, dest = _setup(tmp)
        state = {}
        assert F.mirror_strategy_logs(patterns, state, 1000.0, 120, dest) == 2
        assert sorted(p.name for p in dest.iterdir()) == ["XtClient_FormulaOutput_20261126.log",
                                                          "XtClient_FormulaOutput_20261127.log"]
        for name in ("XtClient_FormulaOutput_20261126.log", "XtClient_FormulaOutput_20261127.log"):
            assert (dest / name).read_bytes() == (src / name).read_bytes()
        json.dumps(state)                                           # 状态随转发程序状态一起存成 JSON


def test_interval_and_copy_only_when_grown():
    with tempfile.TemporaryDirectory() as tmp:
        patterns, src, dest = _setup(tmp)
        state = {}
        assert F.mirror_strategy_logs(patterns, state, 1000.0, 120, dest) == 2
        assert F.mirror_strategy_logs(patterns, state, 1100.0, 120, dest) == 0      # 不到 2 分钟不看
        assert F.mirror_strategy_logs(patterns, state, 1130.0, 120, dest) == 0      # 到时间但没变长，不复制
        today = src / "XtClient_FormulaOutput_20261127.log"
        with today.open("ab") as handle:
            handle.write(b"\nnew line")
        assert F.mirror_strategy_logs(patterns, state, 1260.0, 120, dest) == 1      # 只复制变长的那一个
        assert (dest / today.name).read_bytes() == today.read_bytes()
        assert F.mirror_strategy_logs(patterns, state, 2000.0, 0, dest) == 0        # 0 = 关掉


def test_new_day_drops_old_bookkeeping_and_missing_copy_is_restored():
    with tempfile.TemporaryDirectory() as tmp:
        patterns, src, dest = _setup(tmp)
        state = {}
        F.mirror_strategy_logs(patterns, state, 1000.0, 120, dest)
        (src / "XtClient_FormulaOutput_20261130.log").write_bytes(b"monday")
        assert F.mirror_strategy_logs(patterns, state, 1200.0, 120, dest) == 1      # 11-27 没变，只复制新的一天
        assert set(state["mirror_sizes"]) == {"XtClient_FormulaOutput_20261127.log",
                                              "XtClient_FormulaOutput_20261130.log"}
        (dest / "XtClient_FormulaOutput_20261127.log").unlink()                  # Mac 端误删了副本
        assert F.mirror_strategy_logs(patterns, state, 1400.0, 120, dest) == 1      # 下一轮补回来


def test_error_skips_round_logs_once_and_retries(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        patterns, src, dest = _setup(tmp)
        (dest / "XtClient_FormulaOutput_20261127.log").mkdir(parents=True)      # 目标被占用：改名会失败
        notes = []
        monkeypatch.setattr(F, "log", notes.append)
        state = {}
        assert F.mirror_strategy_logs(patterns, state, 1000.0, 120, dest) == 1      # 另一个照常复制，不抛异常
        assert len(notes) == 1 and "20261127" in notes[0]
        assert F.mirror_strategy_logs(patterns, state, 1200.0, 120, dest) == 0
        assert len(notes) == 1                                                    # 同一个错误不重复记
        (dest / "XtClient_FormulaOutput_20261127.log").rmdir()
        assert F.mirror_strategy_logs(patterns, state, 1400.0, 120, dest) == 1      # 恢复后照常复制
        assert state["mirror_errors"] == {}
        json.dumps(state)


def test_main_loop_mirrors_only_for_live_and_not_once():
    text = PATH.read_text(encoding="utf-8")
    assert "if args.mode == 'live' and not args.once:\n            mirror_strategy_logs(" in text
    assert "parser.add_argument('--mirror-minutes', type=float, default=2.0" in text
