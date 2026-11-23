"""决策优先级流程图写明D为什么不启动（2026-11-24，用户要求）。

11-23 方案甲停手期，组合状态机按规则不启动D，但流程图写“④ D盘中兜底：不触发｜无候选”，
“D未成交/未触发 → 空仓”标成本次路径，看起来像D扫描过没找到。改后：D被停手、M占用、判定无效挡住时，
【2】“否”这一支标成本次路径并写原因，D那一行和最终计划也写原因；有A/C/E计划时【1】“是”标成本次路径。
只改显示：同样的输入，组合状态机里D是否启动和流程图说的一致（下面最后一个测试）。
"""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import src.rolling_signal_store as signal_store
from src.daemon import reports
from src.equity_curve_stop import LiveGateCheck
from tests.test_current_portfolio_runtime import make_engine

SIGNAL_DAY = "20261120"
ACTION_DAY = "20261123"
STOP_REASON = "方案甲停手期（影子净值低于60日均线），ACE按20%小仓执行，D今日不启动"
M_REASON = "M持仓或M开仓计划占用资金，D今日不启动（D只在账户含M完全空仓时运行）"
INVALID_REASON = "方案甲判定无效（缺失、过期或版本不符），A/C/E/D都不开新仓，D今日不启动"
TAG = " ◄―本次路径"


@pytest.fixture
def chain(tmp_path, monkeypatch):
    env = SimpleNamespace(mode="REDUCED", positions=[], m_signal=None, log=Mock())
    now = dt.datetime(2026, 11, 23, 8, 50)
    for name, value in {
        "PROJECT_ROOT": tmp_path,
        "M_SIGNAL_RECENT_PATH": tmp_path / "reports/strategy_m/m_signals_recent.json",
        "_last_final_plan": None,
        "logger": Mock(return_value=env.log),
        "load_positions": lambda: deepcopy(env.positions),
        "_is_recent_auto_ghost_clear": lambda *_args, **_kwargs: False,
        "now_beijing": lambda: now,
        "today_beijing": lambda: now.date(),
        "next_n_trade_days": lambda *_args, **_kwargs: dt.date(2026, 11, 23),
        "_ace_gate_mode_for": lambda *_args, **_kwargs: env.mode,
        "_equity_curve_stop_block_reason": lambda *_args, **_kwargs: "",
        "_new_buy_execution_gate": lambda: (True, ""),
        "_live_plan_sizing": lambda *_args, **_kwargs: None,
        "_load_e_signal_for_signal_date": lambda _day: None,
        "_load_readonly_candidate_for_broadcast": lambda *_args: (None, ""),
        "_ordinary_open_plan_expired": lambda *_args: False,
    }.items():
        monkeypatch.setattr(reports, name, value)
    monkeypatch.setattr(signal_store, "latest_signal_for_buy_date",
                        lambda _path, day: env.m_signal if day == ACTION_DAY else None)

    def orders(leg=None):
        path = tmp_path / "reports/paper_trade/ab_filtered_daily_ops" / f"test_{SIGNAL_DAY}_planned_orders.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"signal_date": SIGNAL_DAY, "side": "BUY", "strategy_leg": leg, "ts_code": "002173.SZ",
                 "name": "测试股", "round_lot_shares": 100, "reference_price": 10.0}] if leg else []
        pd.DataFrame(rows, columns=["signal_date", "side", "strategy_leg", "ts_code", "name",
                                    "round_lot_shares", "reference_price"]).to_csv(path, index=False)

    def run():
        env.log.reset_mock()
        reports._log_decision_chain_summary(SIGNAL_DAY)
        env.log.warning.assert_not_called()
        message = str(env.log.info.call_args.args[0])
        return message.splitlines()

    env.orders, env.run = orders, run
    orders()
    yield env


def _line(lines, head):
    found = [line for line in lines if head in line]
    assert len(found) == 1, (head, lines)
    return found[0]


def _tagged(lines):
    return [line for line in lines if TAG in line]


def test_stop_period_says_d_is_not_started_and_why(chain):
    lines = chain.run()
    assert _line(lines, "【2】") == "┃ 【2】D今日能启动吗？（方案甲停手期、M有持仓或开仓计划时不启动）"
    assert _tagged(lines) == [f"┃   ├─ 否 → D全日不扫描、不记录、不下单｜{STOP_REASON}{TAG}"]
    assert _line(lines, "④ D盘中兜底") == f"┃ ④ D盘中兜底：不启动｜{STOP_REASON}"
    assert _line(lines, "★") == f"┃ ★ 今日A/C/E均无开仓计划；{STOP_REASON}"
    assert "不触发｜无候选" not in _line(lines, "④ D盘中兜底")


@pytest.mark.parametrize("m_case", ["plan", "position"])
def test_m_plan_or_position_blocks_d(chain, m_case):
    chain.mode = "FULL"
    if m_case == "plan":
        chain.m_signal = {"status": "SIGNAL_READY", "candidates": [{"ts_code": "300001.SZ", "rank": 1}]}
    else:
        chain.positions = [{"strategy_leg": "M", "status": "open", "ts_code": "300001.SZ", "shares": 100}]
    lines = chain.run()
    assert _tagged(lines) == [f"┃   ├─ 否 → D全日不扫描、不记录、不下单｜{M_REASON}{TAG}"]
    assert _line(lines, "④ D盘中兜底") == f"┃ ④ D盘中兜底：不启动｜{M_REASON}"


def test_m_signal_without_candidates_does_not_block_d(chain):
    chain.mode = "FULL"
    chain.m_signal = {"status": "NO_CANDIDATE", "candidates": []}
    lines = chain.run()
    assert _tagged(lines) == [f"┃        └─ D未成交/未触发 → 空仓{TAG}"]
    assert _line(lines, "④ D盘中兜底") == "┃ ④ D盘中兜底：规则允许启动｜09:30起盘中扫描，满足正式条件才开仓"
    assert _line(lines, "★") == "┃ ★ 今日A/C/E均无开仓计划；D按规则09:30起盘中扫描，满足正式条件才开仓"


def test_invalid_stop_decision_blocks_d(chain):
    chain.mode = "BLOCKED"
    lines = chain.run()
    assert _tagged(lines) == [f"┃   ├─ 否 → D全日不扫描、不记录、不下单｜{INVALID_REASON}{TAG}"]


def test_ace_plan_marks_step_one_and_d_line(chain):
    chain.orders("C")
    lines = chain.run()
    assert _tagged(lines) == ["┃   ├─ 是 → 按A>C>E取首个计划执行；D全日不扫描、不记录、不下单" + TAG]
    assert _line(lines, "④ D盘中兜底") == (
        "┃ ④ D盘中兜底：不启动｜今日已有C正式开仓计划，按全日互斥D不扫描、不记录、不下单")


def test_ace_holding_marks_step_zero_and_d_line(chain):
    chain.positions = [{"strategy_leg": "A", "status": "open", "ts_code": "000001.SZ", "shares": 100}]
    lines = chain.run()
    assert _tagged(lines) == ["┃   ├─ 是 → 不开新仓，确认实际清仓后再等待下一候选" + TAG]
    assert _line(lines, "④ D盘中兜底") == "┃ ④ D盘中兜底：不启动｜券商仍有本系统策略仓"


def test_reason_lookup_failure_only_changes_the_line(chain, monkeypatch):
    def broken(*_args):
        raise OSError("磁盘读不了")
    monkeypatch.setattr(signal_store, "latest_signal_for_buy_date", broken)
    chain.mode = "FULL"
    lines = chain.run()
    assert "D能否启动读取失败（只影响本条显示，不影响交易）：磁盘读不了" in _line(lines, "④ D盘中兜底")
    assert reports._last_final_plan["final_buy"] is None


def _engine_allows_d(mode: str, m_plan: bool, m_position: bool) -> bool:
    positions = [{"strategy_leg": "M", "status": "open", "ts_code": "300001.SZ", "shares": 100}] if m_position else []
    engine = make_engine(positions=positions)
    engine.has_m_plan_today = lambda _today: m_plan
    engine.equity_curve_stop_check = lambda _today: LiveGateCheck(
        allowed=mode == "FULL", reason="测试", source="测试", payload={}, halt_valid=mode == "REDUCED")
    _state, decisions, _orders = engine.build_mode1_plan("20260803")
    actions = set(decisions["action"].astype(str))
    assert ("ALLOW_D_INTRADAY_MONITOR" in actions) != ("BLOCK_D_INTRADAY_MONITOR" in actions)
    return "ALLOW_D_INTRADAY_MONITOR" in actions


@pytest.mark.parametrize("mode", ["FULL", "REDUCED", "BLOCKED"])
@pytest.mark.parametrize("m_plan", [False, True])
@pytest.mark.parametrize("m_position", [False, True])
def test_flowchart_agrees_with_state_machine(chain, mode, m_plan, m_position):
    chain.mode = mode
    chain.m_signal = {"status": "SIGNAL_READY", "candidates": [{"ts_code": "300001.SZ"}]} if m_plan else None
    chain.positions = ([{"strategy_leg": "M", "status": "open", "ts_code": "300001.SZ", "shares": 100}]
                       if m_position else [])
    lines = chain.run()
    flowchart_allows = _tagged(lines) == [f"┃        └─ D未成交/未触发 → 空仓{TAG}"]
    assert flowchart_allows == _engine_allows_d(mode, m_plan, m_position)
