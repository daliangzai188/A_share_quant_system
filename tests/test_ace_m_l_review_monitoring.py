"""新框架的三级警告、实盘检查及日报；只读配置，用合成交易验证，不接券商。"""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.daily_report import build_daily_report, strategy_health_details, value_lines
from src.portfolio_ace_m import FRAMEWORK_ACE_M, FRAMEWORK_ACE_M_L
from src.review_triggers import (
    CHECK_REPAIR_ACTION,
    FINAL_STANDARD,
    REVIEW_ACTION,
    check_failure_trigger,
    l_early_triggers,
    l_slippage_stats,
    l_slippage_triggers,
    loss_streak_state,
    m_early_triggers,
    signal_drought_triggers,
)
from src.strategy_warnings import (
    compute_metrics,
    escalate,
    load_warning_settings,
    m_recent_stats,
    signal_activity,
    warning_triggers,
)

ROOT = Path(__file__).resolve().parents[1]
DAY = "20261126"


def _config(framework=FRAMEWORK_ACE_M_L):
    """阈值以当前待审配置为准，不在测试里另设真实冻结线。"""
    section = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))["strategy_warnings"]
    return {"portfolio_framework": framework, "strategy_warnings": deepcopy(section)}


def _metrics():
    return {
        "framework": FRAMEWORK_ACE_M_L,
        "legs": {leg: {"value": 0.1, "trades": 20, "as_of": DAY} for leg in ("A", "C", "E", "M", "L")},
        "factor_health": {leg: {"months_below": 0} for leg in ("A", "C", "E")},
        "shadow_drawdown": {"value": -0.1, "peak": "20260101", "as_of": DAY},
        "combo_drawdown": {"value": -0.1, "peak": "20260101", "as_of": DAY},
        "combo_relative": {"value": 0.1, "days_behind": 0, "as_of": DAY},
    }


def _report(triggers, values=(), *, acknowledged=None, recheck=None):
    return build_daily_report(DAY, triggers, acknowledged or {}, recheck or {}, {}, list(values),
                              final_standard=FINAL_STANDARD)


def test_new_frozen_settings_replace_old_blocks_without_mutating_config():
    config = _config()
    before = deepcopy(config)
    settings = load_warning_settings(config)
    frozen = config["strategy_warnings"]["ace_m_l"]
    assert settings.framework == FRAMEWORK_ACE_M_L
    for name in ("middle", "final", "reference", "m_early", "l_early", "signal_gap_days", "relative_days"):
        assert getattr(settings, name) == frozen[name], name
    assert config == before


@pytest.mark.parametrize("missing", [None, {}, [], ""])
def test_new_framework_rejects_missing_or_empty_frozen_block(missing):
    config = _config()
    config["strategy_warnings"]["ace_m_l"] = missing
    with pytest.raises(ValueError, match="strategy_warnings.ace_m_l"):
        load_warning_settings(config)


@pytest.mark.parametrize("path", [
    "middle", "final", "reference", "m_early", "l_early", "signal_gap_days", "relative_days",
    "middle.L", "middle.M", "middle.combo_drawdown", "final.combo_drawdown",
    "reference.middle_L", "reference.relative_history",
    "m_early.worst_window_return", "l_early.worst_window_return",
])
def test_incomplete_new_frozen_block_cannot_silently_inherit_old_lines(path):
    config = _config()
    target = config["strategy_warnings"]["ace_m_l"]
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    del target[keys[-1]]
    with pytest.raises(ValueError, match="冻结配置不完整"):
        load_warning_settings(config)


@pytest.mark.parametrize("empty_reference", [None, "", "   "])
def test_new_reference_explanation_cannot_be_missing_or_blank(empty_reference):
    config = _config()
    config["strategy_warnings"]["ace_m_l"]["reference"]["middle_L"] = empty_reference
    with pytest.raises(ValueError, match="reference.middle_L"):
        load_warning_settings(config)


def test_old_framework_ignores_even_incomplete_new_block_and_disabled_checks_stay_disabled():
    config = _config(FRAMEWORK_ACE_M)
    original = load_warning_settings(config)
    config["strategy_warnings"]["ace_m_l"] = {"signal_gap_days": 1}
    assert load_warning_settings(config) == original
    config["portfolio_framework"] = FRAMEWORK_ACE_M_L
    config["strategy_warnings"]["enabled"] = False
    assert load_warning_settings(config).enabled is False


@pytest.mark.parametrize("leg", ["M", "L"])
def test_each_new_leg_uses_its_own_middle_line_and_strict_boundary(leg):
    settings = load_warning_settings(_config())
    metrics = _metrics()
    metrics["legs"][leg]["value"] = settings.middle[leg]
    assert warning_triggers(metrics, settings) == []
    metrics["legs"][leg]["value"] -= 0.00001
    found = warning_triggers(metrics, settings)
    assert [item.key for item in found] == ["warn2:leg:" + leg]
    assert found[0].scope == leg and found[0].tier == 2
    assert found[0].action == "请在Claude发送：甲·策略复核（%s）" % leg
    report = _report(found)
    assert report["level"] == "middle" and leg + "需要策略复核" in report["title"]


def test_new_combo_uses_new_drawdown_line_and_final_is_advisory_only():
    settings = load_warning_settings(_config())
    metrics = _metrics()
    metrics["combo_drawdown"]["value"] = settings.middle["combo_drawdown"] - 0.00001
    found = warning_triggers(metrics, settings)
    assert [item.key for item in found] == ["warn2:combo:drawdown"]
    assert "ACE+M+L" in found[0].detail
    metrics["combo_drawdown"]["value"] = settings.final["combo_drawdown"] - 0.00001
    found = warning_triggers(metrics, settings)
    assert [item.key for item in found] == ["warn3:combo"]
    assert _report(found)["level"] == "final"
    assert "系统不会自己停止交易，停不停由你决定" in _report(found)["body"]


def test_m_and_l_do_not_invent_unconfigured_final_lines():
    settings = load_warning_settings(_config())
    metrics = _metrics()
    metrics["legs"]["M"]["value"] = -0.95
    metrics["legs"]["L"]["value"] = -0.95
    found = warning_triggers(metrics, settings)
    assert {item.key for item in found} == {"warn2:leg:M", "warn2:leg:L", "warn2:all"}
    assert all(item.tier == 2 for item in found)
    assert _report(found)["level"] == "middle"


def test_l_review_acknowledgement_and_sixty_open_day_escalation_are_preserved():
    settings = load_warning_settings(_config())
    metrics = _metrics()
    metrics["legs"]["L"]["value"] = settings.middle["L"] - 0.01
    found = warning_triggers(metrics, settings)
    days = pd.bdate_range("2026-01-01", periods=61).strftime("%Y%m%d").tolist()
    state = {"first_seen": {"warn2:leg:L": days[0]}}
    due = escalate(found, state, acknowledged={}, today=days[60], open_dates=days, open_days=60)
    assert [item.key for item in due] == ["warn2:leg:L", "warn3:escalated:warn2:leg:L"]
    report = _report(found, acknowledged={"warn2:leg:L": "维持观察"},
                     recheck={"warn2:leg:L": "2027-01-02"})
    assert report["level"] == "ok" and report["first_seen"] == {}
    assert "观察中（已复核，不用处理）" in report["body"] and "2027-01-02复查" in report["body"]
    kept = escalate(found, state, acknowledged={"warn2:leg:L": "维持观察"},
                    today=days[60], open_dates=days, open_days=60)
    assert [item.key for item in kept] == ["warn2:leg:L"]


def test_rule_metrics_keep_m_and_l_separate_and_daily_health_labels_the_new_replay():
    settings = load_warning_settings(_config())
    days = pd.bdate_range("2026-11-02", periods=10).strftime("%Y%m%d").tolist()
    trades = pd.DataFrame([{"action_date": day, "family": leg, "ret": ret}
                           for leg, ret in (("M", 0.01), ("L", -0.02)) for day in days])
    nav = pd.Series([1.0, 1.05], index=[days[0], DAY])
    shadow = {"legs": {leg: pd.DataFrame() for leg in ("A", "C", "E")}, "nav": nav}
    metrics = compute_metrics(shadow, None, nav, nav, trades, settings)
    assert metrics["legs"]["M"]["value"] == pytest.approx(1.01 ** 10 - 1)
    assert metrics["legs"]["L"]["value"] == pytest.approx(0.98 ** 10 - 1)
    assert metrics["m_recent"]["streak"] == 0 and metrics["l_recent"]["streak"] == 10
    assert metrics["l_recent"]["replay"] == "ACE+M+L"
    details = strategy_health_details({"warnings": metrics, "strategy_health_context": {
        "signal_date": DAY, "enabled": True, "min_trades": 10, "window_days": 365}})
    assert any(line.startswith("L：") and "有效收益10笔" in line for line in details)
    assert "M、L为ACE+M+L组合内回放" in details[-1]


@pytest.mark.parametrize("leg,trigger_fn", [("M", m_early_triggers), ("L", l_early_triggers)])
def test_early_reminders_are_first_tier_with_independent_frozen_lines_and_annual_review(leg, trigger_fn):
    settings = load_warning_settings(_config())
    early = getattr(settings, leg.lower() + "_early")
    recent = {"replay": "ACE+M+L", "streak": early["max_loss_streak"],
              "max_loss_streak": early["max_loss_streak"], "streak_from": "20261102", "as_of": DAY,
              "window_trades": early["window_trades"], "window_from": "20261001",
              "window_return": early["worst_window_return"], "worst_window_return": early["worst_window_return"]}
    assert trigger_fn(recent) == []
    recent["streak"] += 1
    recent["window_return"] -= 0.00001
    found = trigger_fn(recent)
    assert [item.key for item in found] == [leg.lower() + "_early:streak", leg.lower() + "_early:window"]
    assert all(item.tier == 1 and item.action == REVIEW_ACTION for item in found)
    assert all("ACE+M+L回放" in item.detail and "年度复核决定" in item.detail for item in found)
    assert _report(found)["level"] == "review"


def test_short_l_history_does_not_invent_a_ten_trade_loss_window():
    settings = load_warning_settings(_config())
    recent = m_recent_stats([("20261125", -0.01), (DAY, 0.01)], settings.l_early, "ACE+M+L")
    assert recent["window_return"] is None and recent["streak"] == 0
    assert l_early_triggers(recent) == []
    lines = value_lines({"alignment": [], "warnings": {"framework": FRAMEWORK_ACE_M_L,
                          "legs": {}, "l_recent": recent}})
    assert any(line.startswith("L近况（按规则）") and "最近10笔不足10笔" in line for line in lines)


def _slippage_rows(bps=180.0, count=10):
    return pd.DataFrame([{"entry_date": day, "ts_code": "%06d.SZ" % (300001 + index),
                          "strategy_leg": "L", "execution_status": "已平仓", "entry_vwap": 10.1,
                          "benchmark_open": 10.0, "exit_vwap": 10.8, "benchmark_close": 11.0,
                          "total_slippage_bps": bps}
                         for index, day in enumerate(pd.bdate_range("2026-11-02", periods=count).strftime("%Y%m%d"))])


def test_l_live_slippage_filters_other_legs_old_open_and_missing_benchmark_trades():
    live = _slippage_rows()
    excluded = [dict(live.iloc[0], strategy_leg="M", total_slippage_bps=999.0),
                dict(live.iloc[0], entry_date="20261030", total_slippage_bps=999.0),
                dict(live.iloc[0], execution_status="持仓中", total_slippage_bps=999.0),
                dict(live.iloc[0], benchmark_open=0.0, total_slippage_bps=999.0)]
    stats = l_slippage_stats(pd.concat([live, pd.DataFrame(excluded)], ignore_index=True),
                             start="20261101", trades=10, limit_pct=1.5)
    assert stats["completed"] == 10 and stats["average_pct"] == pytest.approx(1.8)
    found = l_slippage_triggers(stats)
    assert [item.key for item in found] == ["l_slippage"]
    assert found[0].tier == 1 and found[0].action == REVIEW_ACTION
    assert "L最近10笔" in found[0].detail and "+1.80%" in found[0].detail
    assert "L滑点：最近10笔平均双边+1.80%" in value_lines({"alignment": [], "l_slippage": stats})[0]


@pytest.mark.parametrize("count,bps,expected", [(9, 180.0, False), (10, 150.0, False), (10, 150.1, True)])
def test_l_slippage_needs_ten_closed_trades_and_strictly_exceeds_limit(count, bps, expected):
    stats = l_slippage_stats(_slippage_rows(bps, count), start="20261101", trades=10, limit_pct=1.5)
    assert stats["triggered"] is expected
    assert bool(l_slippage_triggers(stats)) is expected


def test_loss_streak_uses_exit_order_and_ace_l_m_ties_and_only_displays_state():
    complete = pd.DataFrame([{"entry_date": "20261123", "exit_date": "20261125", "ts_code": code,
                              "strategy_leg": leg, "net_return": ret}
                             for leg, code, ret in (("M", "300003.SZ", 0.01), ("A", "300001.SZ", -0.01),
                                                    ("L", "300002.SZ", -0.01))])
    state = loss_streak_state(complete)
    assert [item["leg"] for item in state["recent"]] == ["L", "M"]
    assert state["multiplier"] == 1.0
    complete.loc[complete.strategy_leg.eq("M"), "net_return"] = -0.01
    reduced = loss_streak_state(complete)
    assert reduced["multiplier"] == 0.3
    lines = value_lines({"alignment": [], "loss_streak": reduced})
    assert any("连亏降仓：已触发，新开仓仓位×0.3" in line for line in lines)
    assert _report([], lines)["level"] == "ok", "降仓状态只显示，不伪造新警报"


def test_new_l_signal_resets_drought_and_expired_signal_notice_includes_l():
    settings = load_warning_settings(_config())
    days = pd.bdate_range("2026-11-02", periods=settings.signal_gap_days + 2).strftime("%Y%m%d").tolist()
    active = signal_activity({}, {}, days, {days[-1]: ["300001.SZ"]})
    assert active["gap"] == 0 and signal_drought_triggers(active, line=settings.signal_gap_days) == []
    expired = signal_activity({}, {}, days, {days[0]: ["300001.SZ"]})
    found = signal_drought_triggers(expired, line=settings.signal_gap_days)
    assert len(found) == 1 and found[0].action == REVIEW_ACTION
    assert "A/C/E和M、L已连续" in found[0].detail and "L " + days[0] in found[0].detail
    assert _report(found)["level"] == "review"


def test_old_drought_notice_bytes_stay_unchanged_without_l():
    signals = {"gap": 23, "last_any": "20261009", "last": {"A": "20261009", "C": "", "E": "", "M": ""}}
    assert signal_drought_triggers(signals, line=22)[0].detail == (
        "A/C/E和M已连续23个行动日没有选出一只股票（最近一次20261009，历史最长22天）：先查数据和程序，"
        "都正常才是行情让条件长期不满足。各腿最近一次：A 20261009、C 无、E 无、M 无")


def _external_new_checks():
    """只加载既有适配函数，避免导入Windows入口时修改cwd或接触真实环境。"""
    path = ROOT / "qmt_native_phase0/qmt_external_checks.py"
    node = next(item for item in ast.parse(path.read_text(encoding="utf-8")).body
                if isinstance(item, ast.FunctionDef) and item.name == "_ace_m_l_checks")
    namespace = {"traceback": SimpleNamespace(print_exc=lambda: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_ace_m_l_checks"]


def test_external_new_checks_keep_legacy_noop_and_surface_missing_live_data():
    check = _external_new_checks()
    triggers = []
    assert check(_config(FRAMEWORK_ACE_M), {}, None, None, triggers, check_failure_trigger) == {}
    assert triggers == []
    out = check(_config(), {"ace_m_l_start_date": "20261101"}, None, None, triggers, check_failure_trigger)
    assert out["l_slippage_error"] and out["loss_streak_error"]
    assert [item.key for item in triggers] == ["check_failed:L实盘滑点"]
    assert triggers[0].action == CHECK_REPAIR_ACTION
    lines = value_lines(dict(alignment=[], **out))
    assert "L滑点没算成" in lines[0] and any("连亏降仓：状态没算成" in line for line in lines)
    assert _report(triggers, lines)["level"] == "review"


def test_external_new_checks_use_configured_l_slippage_window_and_display_streak():
    complete = pd.DataFrame([
        {"entry_date": "20261123", "exit_date": "20261124", "ts_code": "300001.SZ",
         "strategy_leg": "A", "net_return": -0.01},
        {"entry_date": "20261124", "exit_date": "20261125", "ts_code": "300002.SZ",
         "strategy_leg": "L", "net_return": -0.01},
    ])
    triggers = []
    out = _external_new_checks()(_config(), {"ace_m_l_start_date": "20261101", "l_slippage_trades": 3,
                                            "l_slippage_limit_pct": 1.7},
                                 _slippage_rows(count=3), complete, triggers, check_failure_trigger)
    assert out["l_slippage"]["trades"] == 3 and out["l_slippage"]["limit_pct"] == 1.7
    assert not out["l_slippage_error"] and not out["loss_streak_error"]
    assert out["loss_streak"]["multiplier"] == 0.3
    assert [item.key for item in triggers] == ["l_slippage"], "组合降仓状态不另造警报"
