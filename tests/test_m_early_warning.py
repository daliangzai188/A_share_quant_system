"""M早期提醒（2026-11-24用户要求，初级=临时复核，只推送）：连亏超过上线前最长、最近10笔低于上线前最差。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.daily_report import value_lines
from src.review_triggers import REVIEW_ACTION, m_early_triggers
from src.strategy_warnings import WarningSettings, compute_metrics, load_warning_settings, m_recent_stats

EARLY = {"window_trades": 10, "max_loss_streak": 5, "worst_window_return": -0.362}
# 2026-06-24~11-18 回放里M最近10笔（截至11-23的实际数字）
RECENT_10 = [("20260624", 0.0131), ("20260716", -0.0723), ("20260812", 0.2476), ("20260814", -0.0117),
             ("20260819", 0.0022), ("20260825", -0.0176), ("20260902", -0.0657), ("20261008", -0.0740),
             ("20261028", -0.1191), ("20261118", -0.0784)]


def test_streak_and_window_from_trades():
    stats = m_recent_stats(RECENT_10, EARLY)
    assert stats["streak"] == 5 and stats["streak_from"] == "20260825" and stats["as_of"] == "20261118"
    assert round(stats["window_return"], 3) == -0.199 and stats["window_from"] == "20260624"
    assert stats["max_loss_streak"] == 5 and stats["worst_window_return"] == -0.362


def test_current_state_does_not_trigger():
    # 现在连亏5笔，等于上线前最长，不超过；最近10笔-19.9%，高于上线前最差-36.2%
    assert m_early_triggers(m_recent_stats(RECENT_10, EARLY)) == []


def test_sixth_loss_triggers_streak():
    found = m_early_triggers(m_recent_stats(RECENT_10 + [("20261201", -0.01)], EARLY))
    assert [t.key for t in found] == ["m_early:streak"]
    trigger = found[0]
    assert trigger.tier == 1 and trigger.action == REVIEW_ACTION and trigger.category == "M"
    assert "已连续亏损6笔（20260825起，最近一笔20261201），超过上线前历史最长5笔" in trigger.detail
    assert "年度复核" in trigger.detail


def test_window_below_worst_triggers():
    trades = [("202610%02d" % day, -0.05) for day in range(1, 11)]  # 0.95^10-1 = -40.1%
    found = m_early_triggers(m_recent_stats(trades, EARLY))
    assert [t.key for t in found] == ["m_early:streak", "m_early:window"]
    assert "最近10笔连乘-40.1%（20261001~20261010买入），低于上线前历史最差-36.2%" in found[1].detail


def test_a_win_resets_streak_and_short_history_has_no_window():
    stats = m_recent_stats([("20260101", -0.1), ("20260102", 0.01)], EARLY)
    assert stats["streak"] == 0 and stats["streak_from"] == "" and stats["window_return"] is None
    assert m_early_triggers(stats) == []


def test_not_configured_means_not_computed():
    assert m_recent_stats(RECENT_10, {}) is None
    assert m_early_triggers(None) == []


def test_production_config_has_frozen_lines():
    config = json.loads(Path("config/config.json").read_text(encoding="utf-8"))
    old = load_warning_settings(dict(config, portfolio_framework="ace_m")).m_early
    new = load_warning_settings(dict(config, portfolio_framework="ace_m_l")).m_early
    assert (old["window_trades"], old["max_loss_streak"], old["worst_window_return"]) == (10, 5, -0.362)
    assert (new["window_trades"], new["max_loss_streak"], new["worst_window_return"]) == (10, 5, -0.212)


def test_compute_metrics_adds_m_recent_only_when_configured():
    nav = pd.Series([1.0, 1.1], index=["20261117", "20261118"])
    live = pd.DataFrame({"action_date": [d for d, _ in RECENT_10], "family": ["M"] * 10, "ret": [r for _, r in RECENT_10]})
    shadow = {"legs": {leg: pd.DataFrame() for leg in "ACE"}, "nav": nav}
    with_early = compute_metrics(shadow, None, nav, nav, live, WarningSettings(enabled=True, m_early=EARLY))
    assert with_early["m_recent"]["streak"] == 5
    without = compute_metrics(shadow, None, nav, nav, live, WarningSettings(enabled=True))
    assert "m_recent" not in without


def test_daily_report_shows_m_recent_line():
    stats = m_recent_stats(RECENT_10, EARLY)
    lines = value_lines({"alignment": [], "warnings": {"legs": {}, "m_recent": stats}})
    assert "M近况（按规则）：连亏5笔（上线前最长5笔，超过就提醒）；最近10笔-19.9%（上线前最差-36.2%，低于就提醒）" in lines
    short = value_lines({"alignment": [], "warnings": {"legs": {}, "m_recent": m_recent_stats(RECENT_10[:3], EARLY)}})
    assert any(line.startswith("M近况（按规则）：连亏") and "最近10笔不足10笔" in line for line in short)
