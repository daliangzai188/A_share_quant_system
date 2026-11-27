"""L 计划的推送和回测同一逻辑（2026-11-27 用户要求）。

11-27 晚上推送“明日L计划：凡拓数创，11-30买入”，可是 L 还持有信息发展（计划 11-30 收盘卖出），L 同一时间最多1只，
回测里卖出日当天仍算占用，11-30 不买凡拓数创。推送和回测说法相反。现在：
- 虚拟机晚上的 L 计划推送（qmt_notify_forwarder.l_plan_message）按信号日收盘后的持仓下结论；
- 收盘和 08:50 的开仓计划推送（open_plan_notification.l_plan_notice）先看 L 持仓，写明持有哪只、哪天卖；
- 回测（ace_m_replay.replay_ace_m_l）在同样情况下确实不买。
"""
import importlib.util
import json
import tempfile
from pathlib import Path

from src import portfolio_ace_m as PM
from src import strategy_l as SL
from src.ace_m_replay import replay_ace_m_l
from src.open_plan_notification import l_plan_notice

PATH = Path(__file__).resolve().parents[1] / "qmt_native_phase0" / "qmt_notify_forwarder.py"
spec = importlib.util.spec_from_file_location("qmt_notify_forwarder_l_notice_test", PATH)
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

HEADER = ("trade_key,entry_date,ts_code,name,strategy_leg,signal_date,planned_exit_date,entry_filled_qty,"
          "exit_date,exit_filled_qty\n")
A_OPEN = "20261127|300324.SZ|A|20261126,20261127,300324.SZ,旋极信息,A,20261126,20261130,1900,,0\n"
L_OPEN = "20261127|300469.SZ|L|20261126,20261127,300469.SZ,信息发展,L,20261126,20261130,900,,0\n"
C_DONE = "20261124|002173.SZ|C|20261123,20261124,002173.SZ,创新医疗,C,20261123,20261126,1300,20261126,1300\n"
PLAN = {"signal_date": "20261127", "planned_buy_date": "20261130", "status": "SIGNAL_READY", "ts_code": "301313.SZ",
        "name": "凡拓数创", "branch": 1, "l_allowed": True}


def _forwarder_message(monkeypatch, tmp, rows, *, received="2026-11-27T19:45:52+08:00", plan=PLAN):
    export, mirror = Path(tmp) / "export", Path(tmp) / "mirror"
    export.mkdir()
    (export / "trade_completion_summary.csv").write_text(HEADER + "".join(rows), encoding="utf-8")
    (export / "trade_completion_summary.csv.json").write_text(json.dumps({"received_at": received}), encoding="utf-8")
    plan_path = Path(tmp) / "l.json"
    plan_path.write_text(json.dumps({"signals": [plan]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(F, "EXPORT_DIR", export)
    monkeypatch.setattr(F, "MIRROR_DIR", mirror)
    monkeypatch.setattr(F, "L_PLAN_PATH", plan_path)
    return F.l_plan_message("20261127", morning=False)


def test_evening_push_says_no_new_l_while_l_still_held(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        title, body = _forwarder_message(monkeypatch, tmp, [C_DONE, A_OPEN, L_OPEN])
    assert title == "📋 明日L计划：不开新L（仍有L持仓）"
    assert "L首选凡拓数创(301313.SZ)，分支1" in body
    assert "L仍持有信息发展(300469.SZ)，计划11-30收盘卖出；L同一时间最多1只，11-30不买凡拓数创，也不递补" in body
    assert "买入。" not in body


def test_evening_push_pick_held_by_ace(monkeypatch):
    plan = dict(PLAN, ts_code="300324.SZ", name="旋极信息")
    with tempfile.TemporaryDirectory() as tmp:
        title, body = _forwarder_message(monkeypatch, tmp, [A_OPEN], plan=plan)
    assert title == "📋 明日L计划：不开新L（首选是ACE（A）的持仓）"
    assert "这只股票是ACE（A）的持仓，计划11-30收盘卖出；L不买其他策略持有的股票，11-30不买" in body


def test_evening_push_buys_when_nothing_blocks(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        title, body = _forwarder_message(monkeypatch, tmp, [C_DONE])
    assert title == "📋 明日L计划：凡拓数创"
    assert "11-30买入" in body and "如果ACE当天计划买同一只，L不买" in body


def test_evening_push_does_not_judge_with_stale_positions(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        title, body = _forwarder_message(monkeypatch, tmp, [L_OPEN], received="2026-11-27T14:16:32+08:00")
    assert title == "📋 明日L计划：凡拓数创"                         # 收盘卖出可能还没反映进来，不下结论
    assert "持仓数据还没更新到信号日收盘，11-30买不买以开盘前持仓为准" in body


def test_evening_push_position_carried_over():
    assert F.l_exit_words("20261127", "20261130") == "原计划11-27卖出、还没卖掉（顺延）"
    assert F.l_exit_words("20261130", "20261130") == "计划11-30收盘卖出"


def _notice(signal, occasion):
    positions = [{"status": "open", "strategy_leg": "L", "ts_code": "300469.SZ", "name": "信息发展",
                  "planned_exit_date": "20261130"},
                 {"status": "open", "strategy_leg": "A", "ts_code": "300324.SZ", "name": "旋极信息",
                  "planned_exit_date": "20261130"}]
    return l_plan_notice(signal, occasion=occasion, action_date="20261130", gate_mode=PM.GATE_REDUCED,
                         has_l_position=True, streak_mult=0.3, ace_codes={"300324.SZ"}, l_version=SL.L_VERSION,
                         positions=positions)


def test_open_plan_push_same_words_at_close_and_morning():
    # 收盘时 L 计划还没算出：原来写“等晚上单独推送”，现在先看持仓
    text, first = _notice(None, "收盘")
    assert first == ""
    assert text == "L：仍有L持仓信息发展(300469.SZ)，计划11-30收盘卖出；L同一时间最多1只，11-30不开新L。"
    signal = {"planned_buy_date": "20261130", "l_version": SL.L_VERSION, "status": "SIGNAL_READY",
              "ts_code": "301313.SZ", "name": "凡拓数创"}
    text, first = _notice(signal, "早盘")
    assert first == ""
    assert text == ("L：仍有L持仓信息发展(300469.SZ)，计划11-30收盘卖出；L同一时间最多1只，11-30不开新L，"
                    "首选凡拓数创(301313.SZ)不买，也不递补。")


def test_replay_does_not_buy_new_l_on_the_old_ones_exit_day():
    """回测本身：11-27 买的 L 计划 11-30 收盘卖，11-30 的首选不买；12-01 才能买新的。"""
    days = ["20261127", "20261130", "20261201", "20261202"]
    nxt = dict(zip(days[:-1], days[1:]))
    picks = {"20261127": ["300469.SZ"], "20261130": ["301313.SZ"], "20261201": ["300100.SZ"]}

    def l_outcome(code, day):
        return nxt[day], 10.0, 0.01

    _, trades = replay_ace_m_l(days, {}, {d: True for d in days}, {}, lambda c, d: None, picks, l_outcome,
                               {d: True for d in days})
    l_trades = trades[trades["family"] == "L"]
    assert list(zip(l_trades["action_date"], l_trades["ts_code"])) == [("20261127", "300469.SZ"),
                                                                        ("20261201", "300100.SZ")]   # 11-30 没有 L 买入
