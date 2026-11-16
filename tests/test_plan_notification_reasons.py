"""通知端到端回归：候选、持仓阻断、未知产物各说各的事实，所有数据仅在tmp_path。"""
from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.daemon import reports


SIGNAL_DAY = "20261116"
ACTION_DAY = "20261117"
A_STOCK = {"strategy": "A", "ts_code": "300824.SZ", "name": "北鼎股份"}


def _e_holdings():
    return [
        {"strategy_leg": "E", "ts_code": "001393.SZ", "name": "维通利",
         "status": "open", "shares": shares, "buy_price": price,
         "buy_date": "20261116", "signal_date": "20261113",
         "planned_exit_date": "20261117", "order_id": order_id}
        for shares, price, order_id in ((200, 44.68, "auction-test"), (100, 45.01, "pov-test"))
    ]


@pytest.fixture
def notices(tmp_path, monkeypatch):
    env = SimpleNamespace(
        root=tmp_path, positions=[], mode="REDUCED", stop_reason="",
        buy_gate=(True, ""), action_day=dt.date(2026, 11, 17),
        now=dt.datetime(2026, 11, 16, 16, 0),
    )
    env.notify = Mock(return_value=False)  # 不发消息，也不写“已推送”标记。
    env.broker = Mock(side_effect=AssertionError("通知测试不得请求券商"))
    env.sizing = Mock(side_effect=lambda *_args, **_kwargs: {
        "shares": 100, "order_price": 10.0, "target_amount": 20000.0,
        "min_acceptable_amount": 19393.94, "hard_cap_amount": 22500.0,
        "available_cash": 100000.0, "total_asset": 100000.0,
        "risk_flags": "", "ace_gate_mode": env.mode,
    })
    for name, value in {
        "PROJECT_ROOT": tmp_path,
        "_PLAN_PUSH_STATE": tmp_path / "data/state/open_plan_push.json",
        "M_SIGNAL_RECENT_PATH": tmp_path / "reports/strategy_m/m_signals_recent.json",
        "_last_final_plan": None,
        "logger": Mock(return_value=Mock()),
        "_notify": env.notify,
        "_qmt_get": env.broker,
        "load_positions": lambda: deepcopy(env.positions),
        "load_pending_buys": lambda: [],
        "_pov_active_today": lambda: False,
        "_is_recent_auto_ghost_clear": lambda *_args, **_kwargs: False,
        "_position_manual_exit_only": lambda _position: False,
        "has_position_bought_today": lambda *_args, **_kwargs: False,
        "has_open_local_position": lambda family=None: any(
            p.get("status") in {"open", "sell_pending"}
            and (family is None or (p.get("strategy_leg") == "M") == (family == "M"))
            for p in env.positions
        ),
        "load_json_config": lambda *_args, **_kwargs: {
            "broker_adapter_enabled": False, "qmt_enabled": False,
        },
        "now_beijing": lambda: env.now,
        "today_beijing": lambda: env.now.date(),
        "next_n_trade_days": lambda *_args, **_kwargs: env.action_day,
        "is_trade_day": lambda _day: True,
        "_ace_gate_mode_for": lambda *_args, **_kwargs: env.mode,
        "_equity_curve_stop_block_reason": lambda *_args, **_kwargs: env.stop_reason,
        "_new_buy_execution_gate": lambda: env.buy_gate,
        "_live_plan_sizing": env.sizing,
        "_load_e_signal_for_signal_date": lambda _day: None,
        "_m_plan_line": lambda *_args, **_kwargs: ("M：无候选。", ""),
        "_m_close_summary": lambda _day: "已计算｜昨日涨停今日平均0.99%｜20cm涨停2只｜候选0只",
    }.items():
        monkeypatch.setattr(reports, name, value)

    def artifacts(leg, rows=(), signal_day=SIGNAL_DAY):
        if leg in {"A", "C"}:
            path = tmp_path / "reports/paper_trade/ab_filtered_daily_ops" / (
                f"test_{signal_day}_{leg.lower()}_candidates.csv"
            )
        elif leg == "E":
            path = tmp_path / "reports/strategy_e" / f"e_signal_{signal_day}_candidates.csv"
        else:
            path = tmp_path / "reports/strategy_d" / f"intraday_signals_{signal_day}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(list(rows), columns=(
            ["signal_type", "ts_code", "name"] if leg == "D"
            else ["candidate_rank", "ts_code", "name"]
        )).to_csv(path, index=False)
        return path

    def orders(candidate=None, signal_day=SIGNAL_DAY):
        path = tmp_path / "reports/paper_trade/ab_filtered_daily_ops" / f"test_{signal_day}_planned_orders.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = ({"signal_date": signal_day, "side": "BUY", "strategy_leg": candidate["strategy"],
                "ts_code": candidate["ts_code"], "name": candidate["name"],
                "round_lot_shares": 100, "reference_price": 10.0} if candidate else None)
        pd.DataFrame([row] if row else [], columns=[
            "signal_date", "side", "strategy_leg", "ts_code", "name", "round_lot_shares", "reference_price",
        ]).to_csv(path, index=False)

    def publish(signal_day=SIGNAL_DAY):
        # 真实生产者负责保留候选和原因；不能直接伪造新增的展示字段掩盖回归。
        reports._log_decision_chain_summary(signal_day)
        reports.push_open_plan_notification("收盘")
        assert env.notify.call_count == 1
        return env.notify.call_args.args[1:3]

    def close_summary(signal_day=SIGNAL_DAY):
        reports._log_close_pipeline_candidate_summary(signal_day)
        assert env.notify.call_count == 1
        return env.notify.call_args.args[2]

    def d_decision(signal_day, reason):
        path = tmp_path / "reports/live_trade/combined" / f"combined_decisions_{signal_day}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"action": "BLOCK_D_INTRADAY_MONITOR", "strategy_leg": "D", "reason": reason}]).to_csv(
            path, index=False,
        )

    env.artifacts, env.orders, env.publish = artifacts, orders, publish
    env.close_summary, env.d_decision = close_summary, d_decision
    for leg in "ACE":
        artifacts(leg)
    orders()
    yield env
    env.broker.assert_not_called()
    assert not (tmp_path / "data/state/open_plan_push.json").exists()


def test_a_candidate_survives_e_holding_block_and_notification_preserves_weighted_fill_price(notices):
    notices.positions = _e_holdings()
    before = deepcopy(notices.positions)
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}])
    notices.orders(A_STOCK)
    title, body = notices.publish()
    assert "无新开仓" in title or "不开新仓" in title
    assert "北鼎" in body and "300824.SZ" in body
    assert "持仓" in body and "ACE" in body
    assert "均无符合条件" not in body and "均无候选" not in body
    assert "维通利" in body and "300股" in body and "2片" in body
    assert "成交均价" in body and "44.79" in body and "未含费用" in body
    assert "20261117" in body
    assert notices.positions == before
    notices.sizing.assert_not_called()


def test_m_title_does_not_erase_ace_holding_reason_or_its_a_candidate(notices, monkeypatch):
    notices.positions = _e_holdings()
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}])
    notices.orders(A_STOCK)
    monkeypatch.setattr(reports, "_m_plan_line", lambda *_args, **_kwargs: (
        "M计划：测试M（独立资金，实际受ACE预留及可用资金约束）。", "测试M",
    ))
    title, body = notices.publish()
    assert "M" in title and "测试M" in title
    assert "北鼎" in body and "持仓" in body and "ACE" in body
    assert "均无符合条件" not in body and "均无候选" not in body
    assert "300股" in body and "未含费用" in body


def test_m_only_position_does_not_block_an_ace_plan(notices):
    notices.positions = [{"strategy_leg": "M", "ts_code": "300001.SZ", "name": "测试M",
                          "status": "open", "shares": 100, "buy_price": 10.0,
                          "buy_date": "20261116", "planned_exit_date": "20261117"}]
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}])
    notices.orders(A_STOCK)
    title, body = notices.publish()
    assert "开仓计划:A" in title and "北鼎" in title
    assert "当前有ACE持仓" not in body and "ACE旧仓未清空" not in body
    notices.sizing.assert_called_once()


def test_known_empty_candidate_artifacts_can_be_called_zero_candidates(notices):
    _title, body = notices.publish()
    assert "均无候选" in body or "均无符合条件" in body or "候选0只" in body
    assert "状态未知" not in body and "候选未知" not in body


@pytest.mark.parametrize("missing_leg", ["A", "C", "E"])
def test_missing_candidate_artifact_is_unknown_rather_than_no_candidate(notices, missing_leg):
    notices.artifacts(missing_leg).unlink()
    _title, body = notices.publish()
    assert "未知" in body or "未完成" in body or "产物缺失" in body
    assert "均无符合条件" not in body and "均无候选" not in body


def test_readonly_candidate_without_formal_order_is_not_called_no_candidate(notices):
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}])
    _title, body = notices.publish()
    assert "北鼎" in body and "正式" in body
    assert "均无符合条件" not in body and "均无候选" not in body


@pytest.mark.parametrize("failure, expected", [
    ("gate", "判定文件已过期"),
    ("expired", "已错过执行窗口"),
    ("buy_guard", "TEST_BUY_GATE_BLOCKED"),
])
def test_original_nonholding_block_reason_is_not_lost(notices, failure, expected):
    signal_day = SIGNAL_DAY
    if failure == "gate":
        notices.mode = "BLOCKED"
        notices.stop_reason = expected
    elif failure == "expired":
        notices.action_day = notices.now.date()
        signal_day = "20261113"
    else:
        notices.buy_gate = (False, expected)
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}], signal_day)
    notices.orders(A_STOCK, signal_day)
    title, body = notices.publish(signal_day)
    assert expected in title + body
    assert "北鼎" in title + body
    assert "均无符合条件" not in body
    if failure == "expired":
        assert "阻断D" in body and "不扫描" in body
    notices.sizing.assert_not_called()


@pytest.mark.parametrize("reason", [
    "方案甲停手期，D本日不启动。",
    "已有ACE持仓，D本日不启动。",
])
def test_d_with_exact_dated_block_record_is_not_reported_as_unfinished(notices, reason):
    notices.d_decision(SIGNAL_DAY, reason)
    body = notices.close_summary()
    line = next(line for line in body.splitlines() if "④ D盘中兜底" in line)
    assert "未启动" in line or "不启动" in line
    assert "停手" in line if "停手" in reason else "持仓" in line
    assert "未完成统计" not in line


def test_d_current_holding_explains_why_no_intraday_file_is_required(notices):
    notices.positions = _e_holdings()
    body = notices.close_summary()
    line = next(line for line in body.splitlines() if "④ D盘中兜底" in line)
    assert "持仓" in line and ("未启动" in line or "不启动" in line)
    assert "未完成统计" not in line


def test_real_d_artifact_keeps_its_buy_and_watch_records_even_when_gate_is_blocked(notices):
    notices.positions = _e_holdings()
    notices.d_decision(SIGNAL_DAY, "方案甲停手期，D本日不启动。")
    notices.artifacts("D", [
        {"signal_type": "BUY", "ts_code": "600001.SH", "name": "D真实记录"},
        {"signal_type": "WATCH", "ts_code": "600002.SH", "name": "D观察记录"},
    ])
    line = next(line for line in notices.close_summary().splitlines() if "④ D盘中兜底" in line)
    assert "已计算" in line and "候选1只" in line and "600001.SH" in line
    assert "BUY记录=1" in line and "WATCH记录=1" in line
    assert "未启动" not in line


@pytest.mark.parametrize("signal_day, with_current_holding", [("20261113", True), (SIGNAL_DAY, False)])
def test_d_without_dated_evidence_remains_unknown_instead_of_using_current_or_next_day_gate(
    notices, signal_day, with_current_holding,
):
    if with_current_holding:
        notices.positions = _e_holdings()
        notices.d_decision(SIGNAL_DAY, "当前日有ACE持仓，D不启动。")
    for leg in "ACE":
        notices.artifacts(leg, signal_day=signal_day)
    # REDUCED是次日开仓判定；缺少该信号日的D记录时不能据此编造历史禁用原因。
    line = next(line for line in notices.close_summary(signal_day).splitlines() if "④ D盘中兜底" in line)
    assert "未完成统计" in line or "未知" in line
    assert "不能据此断言D无候选" in line
    assert "未启动" not in line and "已有ACE持仓" not in line


def test_tomorrow_stop_decision_cannot_explain_today_missing_d_artifact(notices, monkeypatch):
    decision = notices.root / "data/state/equity_curve_stop_decision.json"
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text(json.dumps({
        "signal_date": SIGNAL_DAY, "action_date": ACTION_DAY, "allowed": False,
    }), encoding="utf-8")
    monkeypatch.setattr(reports, "load_json_config", lambda *_args, **_kwargs: {
        "equity_curve_stop": {"enabled": True, "decision_path": "data/state/equity_curve_stop_decision.json"},
    })
    gate = Mock(side_effect=AssertionError("不能把明日判定套到今日D统计"))
    monkeypatch.setattr("src.equity_curve_stop.live_gate", gate)
    line = next(line for line in notices.close_summary().splitlines() if "④ D盘中兜底" in line)
    assert "未完成统计" in line or "未知" in line
    assert "不能据此断言D无候选" in line
    assert "未启动" not in line and "不启动" not in line
    gate.assert_not_called()


def test_unreadable_positions_keeps_other_candidate_statistics_and_d_unknown(notices, monkeypatch):
    notices.artifacts("A", [{"candidate_rank": 1, **A_STOCK}])
    monkeypatch.setattr(reports, "load_positions", Mock(side_effect=RuntimeError("TEST_POSITION_READ_ERROR")))
    body = notices.close_summary()
    assert "A主策略：已计算｜候选1只" in body and "北鼎股份" in body
    assert "C强势龙头：已计算｜候选0只" in body
    assert "E策略：已计算｜候选0只" in body
    line = next(line for line in body.splitlines() if "④ D盘中兜底" in line)
    assert "未完成统计" in line or "未知" in line
    assert "不能据此断言D无候选" in line
    assert "未启动" not in line and "不启动" not in line
