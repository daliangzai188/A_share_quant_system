"""开仓通知回归：状态可见，比例与当笔执行金额一致，不拿旧模板冒充实际仓位。"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.daemon import buy, reports


BUY = {"strategy": "E", "ts_code": "001393.SZ", "name": "维通利", "shares": 300}


def sizing(target=20000, minimum=19393.94, hard=22500, equity=100000):
    return {"shares": 300, "order_price": 47.45, "target_amount": target,
            "min_acceptable_amount": minimum, "hard_cap_amount": hard,
            "available_cash": equity, "total_asset": equity, "risk_flags": ""}


@pytest.mark.parametrize("live, expected", [
    (sizing(82500, 80000, 85000), ("目标82.5%", "8.00万/80%", "8.50万/85%")),
    (sizing(), ("目标20%", "1.94万/19.39%", "2.25万/22.5%")),
    (sizing(17000, 16484.85, 17000), ("目标17%", "1.65万/16.48%", "1.70万/17%")),
    (sizing(5000, 5000, 5000), ("目标5%", "0.50万/5%")),
])
def test_plan_uses_effective_amounts_in_full_reduced_and_constrained_accounts(live, expected):
    before = deepcopy(live)
    message = reports._format_live_plan_line(BUY, live)
    for text in expected:
        assert text in message
    assert "09:24按实时竞价容量定量后" in message
    assert "执行前预估竞价种子单300股" in message
    assert live == before
    assert "09:20按涨停价" in reports._format_live_plan_line({**BUY, "strategy": "A"}, live)


@pytest.mark.parametrize("mode, phase, rule", [
    ("REDUCED", "方案甲停手期", "ACE规则目标20%"),
    ("FULL", "方案甲正常期", "ACE规则目标82.5%"),
    ("BLOCKED", "方案甲判定无效", "ACE禁止新开仓"),
])
def test_morning_notification_puts_gate_state_first(mode, phase, rule, tmp_path):
    plan = {"action_date": "20261116", "final_buy": BUY, "live_sizing": sizing()}
    if mode == "BLOCKED":
        plan["equity_curve_stop_reason"] = "判定文件过期"
    with patch.object(reports, "_last_final_plan", plan), \
         patch.object(reports, "_PLAN_PUSH_STATE", tmp_path / "push.json"), \
         patch.object(reports, "_ace_gate_mode_for", return_value=mode), \
         patch.object(reports, "_m_plan_line", return_value=("M：无候选。", "")), \
         patch.object(reports, "_notify", return_value=False) as notify:
        reports.push_open_plan_notification("早盘")
    event, title, body = notify.call_args.args
    assert event == "buy_result"
    assert phase in title
    assert body.startswith("【" + phase)
    assert rule in body
    if mode == "REDUCED":
        assert "D不启动" in body
        assert "82.5%" not in body and "/85%" not in body
    if mode == "BLOCKED":
        assert "判定文件过期" in body and "计划实际开仓" not in body
    assert not (tmp_path / "push.json").exists()


@pytest.mark.parametrize("equity", [100000, None, 0, float("nan")])
def test_pov_progress_uses_persisted_snapshot_not_new_preview(equity):
    item = {"ts_code": "001393.SZ", "auction_planned_qty": 200,
            "target_actual_amount": 20000, "hard_cap_amount": 22500,
            "equity_snapshot": equity}
    plan = {"final_buy": BUY, "live_sizing": sizing(82500, 80000, 85000)}
    with patch.object(reports, "load_positions", return_value=[]), \
         patch.object(reports, "load_pending_buys", return_value=[]), \
         patch.object(reports, "_pov_load_state", return_value={"items": [item]}):
        message = reports._open_plan_execution_detail(plan)
    assert "实盘竞价种子计划200股" in message
    assert "82.5%" not in message and "/85%" not in message
    if equity == 100000:
        assert "目标2.00万/20%" in message and "硬顶2.25万/22.5%" in message
    else:
        assert "目标2.00万/比例待核" in message


def test_e_accepted_order_notice_preserves_reduced_amounts():
    adapter = SimpleNamespace(place_order=Mock(return_value=SimpleNamespace(accepted=True, order_id="test")))
    with patch.object(buy, "_qmt_get", return_value=adapter), \
         patch.object(buy, "_append_pending_buys"), \
         patch.object(buy, "_notify", return_value=False) as notify:
        buy._e_auction_submit(
            broker_cfg={}, today_str="20261116", OrderRequest=lambda **values: SimpleNamespace(**values),
            row={}, log=Mock(), new_pending=[], ts_code="001393.SZ", qty=200, price=47.45,
            signal_date_s="20261113", planned_exit="20261118", name_s="维通利", exit_n=2,
            total_target_qty=300, planned_amt=20000, min_acceptable_amount=19393.94,
            hard_cap_amount=22500, equity_snapshot=100000, src="竞价容量约束",
        )
    body = notify.call_args.args[2]
    assert "20%" in body and "22.5%" in body
    assert "82.5%" not in body and "85%" not in body
    request = adapter.place_order.call_args.args[0]
    assert (request.quantity, request.price) == (200, 47.45)


@pytest.mark.parametrize("mode, old_mode", [("BLOCKED", "REDUCED"), ("FULL", "REDUCED"), ("REDUCED", "FULL")])
def test_changed_gate_never_presents_cached_sizing_as_executable(mode, old_mode, tmp_path):
    plan = {"action_date": "20261116", "final_buy": BUY,
            "live_sizing": {**sizing(), "ace_gate_mode": old_mode}}
    with patch.object(reports, "_last_final_plan", plan), \
         patch.object(reports, "_PLAN_PUSH_STATE", tmp_path / "push.json"), \
         patch.object(reports, "_ace_gate_mode_for", return_value=mode), \
         patch.object(reports, "_m_plan_line", return_value=("", "")), \
         patch.object(reports, "_notify", return_value=False) as notify:
        reports.push_open_plan_notification("早盘")
    body = notify.call_args.args[2]
    assert "计划实际开仓" not in body
    assert "预挂" not in body
    assert "2.00万" not in body
    assert "ACE禁止新开仓" in body if mode == "BLOCKED" else "原定仓预估已失效" in body


def test_m_plan_reserves_ace_holdings_even_without_a_new_ace_candidate():
    signal = {"candidates": [{"ts_code": "300001.SZ", "name": "测试M", "rank": 1}]}
    with patch("src.rolling_signal_store.latest_signal_for_buy_date", return_value=signal), \
         patch.object(reports, "has_open_local_position", side_effect=lambda family: family == "ACE"):
        message, _ = reports._m_plan_line("20261116", ace_active=False)
    assert "规则仓位上限闲置资金≤17.5%" in message
    assert "82.5%" not in message


def test_reduced_pov_shortfall_alert_uses_its_own_acceptance_line():
    item = {"ts_code": "001393.SZ", "name": "维通利", "strategy_leg": "E",
            "filled_qty": 0, "cost_amt": 0, "confirmed_actual_amount": 18000,
            "target_actual_amount": 20000, "min_acceptable_amount": 19393.94,
            "hard_cap_amount": 22500, "equity_snapshot": 100000}
    with patch.object(buy, "_today_local_entry_cost", return_value=18000), \
         patch.object(buy, "_track_execution"), \
         patch.object(buy, "_notify", return_value=False) as notify:
        buy._pov_finalize_item(item, Mock(), "开盘价+2%保护，不追买")
    alerts = [call.args for call in notify.call_args_list if "低于" in call.args[1]]
    assert len(alerts) == 1
    assert "1.94万/19.39%" in alerts[0][2]
    assert "18%" in alerts[0][2] and "80%" not in alerts[0][2]
    assert item["confirmed_actual_amount"] == 18000 and item["finalized"]
