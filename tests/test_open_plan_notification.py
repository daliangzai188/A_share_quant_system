"""开仓计划展示回归：使用真实推送拼装，所有账户/配置/持仓输入均隔离。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.daemon import plan_notice, reports
from src.open_plan_notification import _m_budget_preview
from src.strategy_l import L_VERSION
from src.strategy_m import M_VERSION, M_VERSION_RET5


@pytest.fixture
def notice(tmp_path, monkeypatch):
    equity = 68126.44
    env = SimpleNamespace(
        mode="REDUCED", positions=[], now=datetime(2026, 11, 17, 16, 0), streak=1.0, l_signal=None,
        config={"portfolio_framework": "ace_m", "strategy_m": {"enabled": True},
                "live_trade": {"cash_buffer_amount": 1000}},
        signal={"m_version": M_VERSION, "status": "SIGNAL_READY", "signal_date": "20261117",
                "planned_buy_date": "20261118", "candidates": [
                    {"ts_code": "301288.SZ", "name": "清研环境", "rank": 1}]},
        plan={"action_date": "20261118", "signal_date": "20261117",
              "final_buy": {"strategy": "E", "ts_code": "002652.SZ", "name": "扬子新材", "shares": 3000},
              "live_sizing": {"shares": 3000, "order_price": 4.84, "target_amount": equity * .20,
                              "min_acceptable_amount": equity * .20 * .80 / .825,
                              "hard_cap_amount": equity * .225, "available_cash": equity,
                              "total_asset": equity, "ace_market_value": 0,
                              "risk_flags": "", "ace_gate_mode": "REDUCED"}},
    )
    env.notify = Mock(return_value=False)
    env.broker = Mock(side_effect=AssertionError("通知展示不得新增券商查询"))
    env.state = tmp_path / "data/state/open_plan_push.json"
    monkeypatch.setattr(reports, "_PLAN_PUSH_STATE", env.state)
    monkeypatch.setattr(reports, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(reports, "_qmt_get", env.broker)
    monkeypatch.setattr(reports, "_notify", env.notify)
    monkeypatch.setattr(reports, "logger", Mock(return_value=Mock()))
    monkeypatch.setattr(reports, "_ace_gate_mode_for", lambda _day: env.mode)
    monkeypatch.setattr(reports, "now_beijing", lambda: env.now)
    monkeypatch.setattr(reports, "load_positions", lambda: deepcopy(env.positions))
    monkeypatch.setattr(reports, "load_json_config", lambda *_args, **_kwargs: deepcopy(env.config))
    def has_open(family):
        return any(p.get("status") in {"open", "sell_pending"}
                   and (p.get("strategy_leg") in {"A", "C", "E", "D"} if family == "ACE"
                        else p.get("strategy_leg") == family) for p in env.positions)

    monkeypatch.setattr(reports, "has_open_local_position", has_open)
    monkeypatch.setattr(reports, "M_SIGNAL_RECENT_PATH", tmp_path / "reports/strategy_m/m_signals_recent.json")
    monkeypatch.setattr(plan_notice, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(plan_notice, "L_SIGNAL_RECENT_PATH", tmp_path / "reports/strategy_l/l_signals_recent.json")
    monkeypatch.setattr(plan_notice, "load_json_config", lambda *_args, **_kwargs: deepcopy(env.config))
    monkeypatch.setattr(plan_notice, "now_beijing", lambda: env.now)
    monkeypatch.setattr(plan_notice, "load_positions", lambda: deepcopy(env.positions))
    monkeypatch.setattr(plan_notice, "has_open_local_position", has_open)
    monkeypatch.setattr(plan_notice, "loss_streak_multiplier_for", lambda *_args: env.streak)
    monkeypatch.setattr(plan_notice, "logger", Mock(return_value=Mock()))

    def signal_for_path(path, _action):
        name = Path(path).name
        if name == "m_signals_recent.json":
            return deepcopy(env.signal)
        if name == "l_signals_recent.json":
            return deepcopy(env.l_signal)
        raise AssertionError("测试未隔离的信号路径：%s" % path)

    monkeypatch.setattr("src.rolling_signal_store.latest_signal_for_buy_date", signal_for_path)

    def push(occasion="收盘"):
        before = deepcopy((env.plan, env.signal, env.l_signal, env.positions, env.config))
        monkeypatch.setattr(reports, "_last_final_plan", env.plan)
        env.notify.reset_mock()
        reports.push_open_plan_notification(occasion)
        assert env.notify.call_count == 1
        assert (env.plan, env.signal, env.l_signal, env.positions, env.config) == before
        env.broker.assert_not_called()
        assert not env.state.exists()
        return env.notify.call_args.args[1:3]

    env.push = push
    yield env
    env.broker.assert_not_called()
    assert not env.state.exists()


@pytest.mark.parametrize("occasion,label", [("收盘", "明日"), ("早盘", "今日")])
def test_combined_plan_displays_two_stocks_and_m_amount_from_same_snapshot(notice, occasion, label):
    title, body = notice.push(occasion)
    assert f"{label}开仓计划（预计2只）" in title
    assert "E 扬子新材＋M 清研环境" in title
    assert "方案甲停手期" in title
    assert "预估开仓约1.19万（占总资产17.5%，执行前重算）" in body
    assert "计划实际开仓1.36万（目标20%" in body
    assert body.startswith("【方案甲停手期·ACE缩仓执行】")


def test_full_ace_plan_leaves_only_17_percent_under_combined_cap(notice):
    notice.mode = "FULL"
    notice.config["live_trade"]["cash_buffer_amount"] = 0  # 单独验证99.5%组合上限。
    live = notice.plan["live_sizing"]
    live.update(ace_gate_mode="FULL", target_amount=live["total_asset"] * .825,
                min_acceptable_amount=live["total_asset"] * .80, hard_cap_amount=live["total_asset"] * .85)
    title, body = notice.push()
    assert "预计2只" in title
    assert "预估开仓约1.16万（占总资产17%，执行前重算）" in body
    assert "规则仓位上限为总资产17.5%" in body


def test_full_ace_reserve_does_not_shrink_to_capacity_limited_notification_target(notice):
    notice.mode = "FULL"
    notice.plan["live_sizing"].update(
        ace_gate_mode="FULL", total_asset=100000, available_cash=30000,
        target_amount=10000, ace_market_value=0, m_market_value=0,
    )
    title, body = notice.push()
    assert "预计2只" not in title
    assert "预估可用预算0元，不开新M" in body


def test_full_cash_account_m_budget_matches_rule_reserve_and_total_cap(notice):
    notice.mode = "FULL"
    notice.config["live_trade"]["cash_buffer_amount"] = 0
    notice.plan["live_sizing"].update(
        ace_gate_mode="FULL", total_asset=100000, available_cash=100000,
        target_amount=82500, ace_market_value=0, m_market_value=0,
    )
    _title, body = notice.push()
    assert "预估开仓约1.70万（占总资产17%，执行前重算）" in body


def test_full_cash_account_default_buffer_can_bind_before_total_cap(notice):
    notice.mode = "FULL"
    notice.plan["live_sizing"].update(
        ace_gate_mode="FULL", total_asset=100000, available_cash=100000,
        target_amount=82500, ace_market_value=0, m_market_value=0,
    )
    _title, body = notice.push()
    assert "预估开仓约1.65万（占总资产16.5%，执行前重算）" in body


@pytest.mark.parametrize("issue", ["zero_shares", "expired"])
def test_zero_seed_or_expired_ace_plan_does_not_release_rule_reserve_to_m(notice, issue):
    notice.mode = "FULL"
    notice.config["live_trade"]["cash_buffer_amount"] = 0
    notice.plan["live_sizing"].update(
        ace_gate_mode="FULL", total_asset=100000, available_cash=100000,
        target_amount=10000, ace_market_value=0, m_market_value=0,
    )
    if issue == "zero_shares":
        notice.plan["live_sizing"]["shares"] = 0
    else:
        notice.plan["execution_expired"] = True
    title, body = notice.push()
    assert "预计2只" not in title
    m_body = body.split("M计划：", 1)[1]
    assert "预估开仓约1.70万（占总资产17%，执行前重算）" in m_body
    assert "规则仓位上限为总资产17.5%" in m_body
    assert "82.5%" not in m_body


@pytest.mark.parametrize("clock, closed", [("10:29:59", False), ("10:30:00", True)])
def test_m_window_boundary_does_not_report_closed_plan_as_second_stock(notice, clock, closed):
    notice.now = datetime.fromisoformat(f"2026-11-18T{clock}")
    title, body = notice.push("早盘")
    assert ("预计2只" not in title) if closed else ("预计2只" in title)
    if closed:
        assert "M：原计划开仓窗口已结束，不新增M持仓" in body


def test_cash_buffer_and_ace_reserve_constrain_m_amount(notice):
    notice.plan["live_sizing"]["available_cash"] = 18000
    notice.config["live_trade"]["cash_buffer_amount"] = 3000
    _title, body = notice.push()
    assert "预估开仓约0.14万（占总资产2.02%，执行前重算）" in body
    assert "预估开仓约1.19万" not in body


def test_no_m_budget_does_not_count_second_stock(notice):
    notice.plan["live_sizing"]["available_cash"] = 1000
    title, body = notice.push()
    assert "预计2只" not in title and "预计1只" in title
    assert "预估可用预算0元，不开新M" in body
    assert "清研环境" not in title


def test_missing_snapshot_never_uses_initial_capital_as_m_amount(notice):
    notice.plan["live_sizing"] = None
    notice.config["position"] = {"initial_cash": 500000}
    title, body = notice.push()
    assert "预计2只" in title
    m_body = body.split("M计划：", 1)[1]
    assert "预估金额待执行前定仓" in m_body
    assert "预估开仓约" not in m_body and "8.75万" not in m_body


@pytest.mark.parametrize("issue", ["expired", "buy_guard", "halt_invalid", "zero_shares", "gate_changed"])
def test_known_ace_block_is_never_counted_as_second_stock(notice, issue):
    if issue == "expired":
        notice.plan["execution_expired"] = True
    elif issue == "buy_guard":
        notice.plan["execution_block_reason"] = "TEST_BUY_GATE_BLOCKED"
    elif issue == "halt_invalid":
        notice.mode = "BLOCKED"
        notice.plan["equity_curve_stop_reason"] = "判定文件过期"
    elif issue == "zero_shares":
        notice.plan["live_sizing"]["shares"] = 0
    else:
        notice.mode = "FULL"  # 缓存仍为停手期，金额不能冒充新状态。
    title, body = notice.push()
    assert "预计2只" not in title
    if issue == "buy_guard":
        assert "TEST_BUY_GATE_BLOCKED" in body
        assert "另有M开仓计划" not in title and "M不开新仓" in body
    if issue == "gate_changed":
        assert "原定仓预估已失效" in body
        assert "预估金额待执行前定仓" in body
        assert "预估开仓约1.19万" not in body


def test_ace_old_holding_keeps_single_m_plan_and_holding_reason(notice):
    notice.plan.update(final_buy=None, live_sizing=None, ace_blocked_by_holdings=True,
                       hold_line="E维通利300股尚未平仓")
    notice.positions = [{"strategy_leg": "E", "ts_code": "001393.SZ", "status": "open",
                         "shares": 300, "buy_price": 44.79, "buy_date": "20261116"}]
    title, body = notice.push()
    assert "M 清研环境（预计1只）" in title and "预计2只" not in title
    assert "当前ACE仍有未平仓旧仓" in body and "维通利300股" in body
    assert "规则仓位上限为总资产17.5%" in body


def test_m_only_plan_is_one_stock_even_with_multiple_candidates(notice):
    notice.plan.update(final_buy=None, live_sizing=None)
    notice.signal["candidates"].append({"ts_code": "301999.SZ", "name": "备选M", "rank": 2})
    title, body = notice.push()
    assert "M 清研环境（预计1只）" in title and "预计2只" not in title
    assert "备选M" in body and "按顺序最多买1只" in body
    assert "规则仓位上限为总资产82.5%" in body
    assert "预估金额待执行前定仓" in body


@pytest.mark.parametrize("issue", ["old_m", "same_stock", "st", "version", "status", "disabled"])
def test_non_executable_m_candidate_is_not_counted(notice, issue):
    if issue == "old_m":
        notice.positions = [{"strategy_leg": "M", "ts_code": "301001.SZ", "status": "open"}]
    elif issue == "same_stock":
        notice.signal["candidates"][0]["ts_code"] = "002652"  # 验证带后缀/裸代码同票回避。
    elif issue == "st":
        notice.signal["candidates"][0]["name"] = "*ST测试"
    elif issue == "version":
        notice.signal["m_version"] = "OLD_VERSION"
    elif issue == "status":
        notice.signal["status"] = "ERROR"
    else:
        notice.config["strategy_m"]["enabled"] = False
    title, body = notice.push()
    assert "预计2只" not in title and "预计1只" in title
    assert "＋M" not in title
    assert "不开新M" in body


def test_same_stock_first_candidate_uses_next_m_without_adding_count(notice):
    notice.signal["candidates"].insert(0, {"ts_code": "002652.SZ", "name": "同票M", "rank": 0})
    title, body = notice.push()
    assert "预计2只" in title and "清研环境" in title
    assert "同票M" not in body and "同票M" not in title


def test_budget_subtracts_already_bought_ace_cost_once(notice):
    notice.plan["live_sizing"].update(total_asset=100000, available_cash=17500,
                                    target_amount=20000, ace_market_value=10000)
    positions = [{"strategy_leg": "E", "ts_code": "002652.SZ", "status": "open",
                  "shares": 1000, "buy_price": 10, "buy_date": "20261118"}]
    before = deepcopy((notice.plan, positions))
    preview = _m_budget_preview(notice.plan, notice.mode, True, positions, notice.config)
    assert preview["amount"] == 6500  # 17500-1000缓冲-(20000-10000已成交)
    assert (notice.plan, positions) == before


@pytest.mark.parametrize("occasion", ["收盘", "早盘"])
@pytest.mark.parametrize("m_has_candidate", [False, True])
def test_new_framework_nov27_a_and_l_plan_includes_reduced_targets_and_excludes_m(
    notice, occasion, m_has_candidate,
):
    """真实通知拼装覆盖11/27：A首选和L并行，连亏0.3后目标6%/18%；C候选不计开仓。"""
    equity = 149542.17
    notice.config["portfolio_framework"] = "ace_m_l"
    notice.config["strategy_l"] = {"enabled": True}
    notice.streak = 0.3
    notice.now = datetime(2026, 11, 26, 22, 0) if occasion == "收盘" else datetime(2026, 11, 27, 8, 50)
    notice.plan.update(
        action_date="20261127", signal_date="20261126",
        final_buy={"strategy": "A", "ts_code": "300324.SZ", "name": "旋极信息", "shares": 1700},
    )
    notice.plan["live_sizing"].update(
        shares=1700, order_price=6.05, target_amount=equity * .06,
        min_acceptable_amount=equity * .06 * .80 / .825, hard_cap_amount=equity * .085,
        available_cash=equity, total_asset=equity,
    )
    notice.l_signal = {
        "strategy_leg": "L", "l_version": L_VERSION, "status": "SIGNAL_READY",
        "signal_date": "20261126", "planned_buy_date": "20261127",
        "ts_code": "300469.SZ", "name": "信息发展", "l_allowed": True, "l_scale": 1.0,
    }
    # 新框架的M信号版本是M_VERSION_RET5（按涨停前5日涨幅排序；2026-11-27修复前测试数据误用了旧版本号）
    notice.signal.update(signal_date="20261126", planned_buy_date="20261127", m_version=M_VERSION_RET5)
    if not m_has_candidate:
        notice.signal.update(candidates=[], diagnostics={"mood": .0322, "n20_limit_up": 3})
    title, body = notice.push(occasion)
    assert "开仓计划（预计2只）:A 旋极信息＋L 信息发展" in title
    assert "＋M" not in title and "预计3只" not in title and "杭州解百" not in title
    assert "计划实际开仓0.90万（目标6%" in body
    assert "L计划：信息发展(300469.SZ)" in body
    assert "规则仓位上限为总资产18%" in body and "连亏降仓×0.3" in body
    assert "预估开仓约2.69万（占总资产18%，执行前重算）" in body   # 2026-11-27起L也显示预估金额
    assert "版本不符" not in body
    if m_has_candidate:
        assert "ACE今天有开仓计划或持仓，M不开仓" in body
    else:
        assert "M：无候选" in body
