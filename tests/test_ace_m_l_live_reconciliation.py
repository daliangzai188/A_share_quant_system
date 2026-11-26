"""用小型可手算记录核对ACE+M+L：首选、漏单、同票、持仓证据及回放顺序。

只使用内存数据及pytest临时目录，不读实盘账本、不连接QMT、不发送通知。
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pandas as pd
import pytest

from src.ace_m_replay import replay_ace_m_l
from src.equity_curve_stop import build_shadow_replay, gated_replay
from src.live_alignment import run_alignment
from src.review_triggers import CHECK_REPAIR_ACTION, alignment_triggers
from src.strategy_l import L_VERSION
from src.strategy_l_reference import LReference


START = "20261126"
NEXT = "20261127"
AFTER = "20261130"
L_CODE = "300100.SZ"
OTHER = "300200.SZ"
ACE_CODE = "000001.SZ"
LIVE_COLUMNS = ["entry_date", "ts_code", "strategy_leg", "name", "execution_status",
                "exit_date", "entry_fill_amount"]


def _buy(day, code=L_CODE, leg="L", *, status="持仓中", exit_date="", name="测试"):
    return dict(zip(LIVE_COLUMNS, [day, code, leg, name, status, exit_date, 20_000.0]))


def _live(*rows):
    return pd.DataFrame(rows, columns=LIVE_COLUMNS)


def _replay(*rows):
    return pd.DataFrame([
        {"action_date": day, "ts_code": code, "strategy_leg": leg, "position_opened": True}
        for day, code, leg in rows
    ], columns=["action_date", "ts_code", "strategy_leg", "position_opened"])


def _first(code=L_CODE, *, opening=10.0, upper=12.0):
    return {"ts_code": code, "buy_open": opening, "buy_limit_up": upper}


class LDays:
    def __init__(self, by_day):
        self.by_day = by_day

    def __call__(self, day):
        return self.by_day.get(day, {"status": "ERROR", "error": "当日缓存缺失", "first": None})

    def buy_days(self, start, end):
        return sorted(day for day in self.by_day if start <= day <= end)


def _reference(first=None, day=START):
    return LDays({day: {"status": "OK", "error": "", "first": first}})


def _check(live, reference, *, replay=None, plans=None, l_start=START, m_candidates=(), end=AFTER):
    return alignment_triggers(
        _replay() if replay is None else replay, live, stop_dates=set(), start=START, end=end,
        ace_m_start="20261113", ace_m_l_start=l_start, l_reference=reference,
        ace_plan_codes=plans,
        m_reference=lambda _day: {"status": "OK", "error": "", "candidates": list(m_candidates)},
    )


def _keys(triggers):
    return {trigger.key for trigger in triggers}


def test_l_first_pick_matches_and_split_fills_are_not_two_stocks():
    rows = _live(_buy(START), _buy(START))
    assert _check(rows, _reference(_first())) == []


def test_l_cannot_replace_locked_first_pick_with_second():
    triggers = _check(_live(_buy(START, OTHER)), _reference(_first(opening=12.0)))
    assert _keys(triggers) == {f"alignment:l_extra:{START}:{OTHER}"}
    assert "只买首选" in triggers[0].detail
    assert L_CODE in triggers[0].detail


def test_l_buy_without_a_plan_is_an_extra_trade():
    assert _keys(_check(_live(_buy(START)), _reference())) == {f"alignment:l_extra:{START}:{L_CODE}"}


def test_l_reference_failure_is_unchecked_instead_of_a_false_extra_trade():
    reference = LDays({START: {"status": "ERROR", "error": "龙虎榜数据未取全", "first": None}})
    triggers = _check(_live(_buy(START)), reference)
    assert _keys(triggers) == {f"alignment:l_unchecked:{START}:{L_CODE}"}
    assert (triggers[0].category, triggers[0].action) == ("检查失效", CHECK_REPAIR_ACTION)
    assert "龙虎榜数据未取全" in triggers[0].detail


def test_l_st_first_pick_is_still_forbidden():
    keys = _keys(_check(_live(_buy(START, name="*ST测试")), _reference(_first())))
    assert keys == {f"alignment:l_st:{START}:{L_CODE}"}


def test_l_buyable_first_pick_without_fill_is_reported_as_missing():
    triggers = _check(_live(), _reference(_first()))
    assert _keys(triggers) == {f"alignment:l_missing:{START}:{L_CODE}"}
    assert "没成交" in triggers[0].detail
    assert "可用额度不够买一手" in triggers[0].detail


@pytest.mark.parametrize("first", [None, _first(opening=12.0), _first(opening=None), _first(upper=None)])
def test_l_missing_alert_requires_proof_first_pick_could_open(first):
    assert _check(_live(), _reference(first)) == []


def test_l_missing_is_excused_when_ace_plans_the_same_stock():
    assert _check(_live(), _reference(_first()), plans={START: {L_CODE}}) == []


def test_l_previous_position_on_its_exit_day_blocks_a_new_l_trade():
    old = _buy("20261125", OTHER, status="已平仓", exit_date=START)
    triggers = _check(_live(old, _buy(START)), _reference(_first()))
    assert _keys(triggers) == {f"alignment:l_overlap:{START}:{L_CODE}"}
    assert _check(_live(old), _reference(_first())) == []


def test_missing_holding_status_cannot_prove_l_overlap():
    rows = _live(_buy("20261125", OTHER), _buy(START)).drop(columns="execution_status")
    assert _check(rows, _reference(_first())) == []


def test_l_two_different_stocks_are_reported_as_multiple_buys():
    keys = _keys(_check(_live(_buy(START), _buy(START, OTHER)), _reference(_first())))
    assert keys == {f"alignment:l_multi:{START}", f"alignment:l_extra:{START}:{OTHER}"}


def test_l_cannot_buy_aces_actual_stock():
    rows = _live(_buy(START, L_CODE, "A"), _buy(START))
    triggers = _check(rows, _reference(_first()), replay=_replay((START, L_CODE, "A")))
    assert _keys(triggers) == {f"alignment:l_ace_same:{START}:{L_CODE}"}


def test_l_cannot_buy_aces_planned_stock_even_if_ace_did_not_fill():
    triggers = _check(_live(_buy(START)), _reference(_first()), plans={START: {L_CODE}})
    assert f"alignment:l_ace_same:{START}:{L_CODE}" in _keys(triggers)


def test_blocked_ace_signal_is_not_an_executable_same_stock_plan_for_l():
    old_ace = _buy("20261125", OTHER, "A", status="已平仓", exit_date=START)
    assert _check(_live(old_ace, _buy(START)), _reference(_first()), plans={START: {L_CODE}}) == []


def test_blocked_ace_signal_cannot_excuse_a_missing_l_trade():
    old_ace = _buy("20261125", OTHER, "A", status="已平仓", exit_date=START)
    triggers = _check(_live(old_ace), _reference(_first()), plans={START: {L_CODE}})
    assert _keys(triggers) == {f"alignment:l_missing:{START}:{L_CODE}"}


def test_l_cannot_buy_a_stock_still_held_by_m():
    old_m = _buy("20261125", L_CODE, "M", status="已平仓", exit_date=START)
    triggers = _check(_live(old_m, _buy(START)), _reference(_first()))
    assert f"alignment:l_m_same:{START}:{L_CODE}" in _keys(triggers)


def test_l_skipping_ms_still_held_stock_is_not_a_missing_trade():
    old_m = _buy("20261125", L_CODE, "M", status="已平仓", exit_date=START)
    assert _check(_live(old_m), _reference(_first())) == []


def test_m_closed_before_today_does_not_excuse_a_missing_l_trade():
    old_m = _buy("20261124", L_CODE, "M", status="已平仓", exit_date="20261125")
    assert _keys(_check(_live(old_m), _reference(_first()))) == {f"alignment:l_missing:{START}:{L_CODE}"}


def test_missing_m_holding_status_does_not_invent_l_same_stock_violation():
    rows = _live(_buy("20261125", L_CODE, "M"), _buy(START)).drop(columns="execution_status")
    assert _check(rows, _reference(_first())) == []


def test_l_rules_do_not_apply_before_the_configured_start():
    triggers = _check(_live(_buy(START)), _reference(_first()), l_start=NEXT)
    assert _keys(triggers) == {f"alignment:extra:{START}:{L_CODE}"}


def test_new_framework_forbids_m_when_ace_only_has_a_plan():
    candidate = {**_first(OTHER), "name": "M候选"}
    rows = _live(_buy(START, OTHER, "M"))
    new = _check(rows, _reference(), plans={START: {ACE_CODE}}, m_candidates=[candidate])
    assert _keys(new) == {f"alignment:m_beside_ace:{START}:{OTHER}"}
    assert _check(rows, _reference(), plans={START: {ACE_CODE}}, m_candidates=[candidate], l_start="") == []


def test_new_framework_detects_m_buying_l_stock():
    rows = _live(_buy(START), _buy(START, L_CODE, "M"))
    triggers = _check(rows, _reference(_first()), m_candidates=[{**_first(), "name": "M候选"}])
    assert _keys(triggers) == {f"alignment:m_l_same:{START}:{L_CODE}"}


@pytest.mark.parametrize("has_position", [False, True])
def test_d_is_blocked_by_l_plan_or_held_position(has_position):
    rows = [_buy(START, OTHER, "D")]
    if has_position:
        rows.append(_buy("20261125"))
    triggers = _check(_live(*rows), _reference(_first()))
    suffix = "position" if has_position else "plan"
    assert f"alignment:d_l_{suffix}:{START}:{OTHER}" in _keys(triggers)


def test_d_cannot_be_cleared_when_l_reference_is_missing():
    triggers = _check(_live(_buy(START, OTHER, "D")), None)
    assert _keys(triggers) == {f"alignment:d_l_unchecked:{START}:{OTHER}"}
    assert triggers[0].action == CHECK_REPAIR_ACTION


def test_l_real_reference_returns_first_only_and_missing_cache_is_unchecked(tmp_path):
    reference = LReference(tmp_path)
    triggers = _check(_live(_buy(START)), reference)
    assert _keys(triggers) == {f"alignment:l_unchecked:{START}:{L_CODE}"}
    cache = tmp_path / "reports" / "strategy_l" / "l_picks_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"l_version": L_VERSION, "picks": {START: [L_CODE, OTHER]}}), encoding="utf-8")
    daily = tmp_path / "data" / "raw" / "daily"
    daily.mkdir(parents=True)
    pd.DataFrame([{"ts_code": L_CODE, "open": 12.0, "close": 12.0, "pre_close": 10.0, "vol": 1000}]).to_csv(
        daily / f"{START}.csv", index=False)
    reference = LReference(tmp_path)
    assert reference(START)["first"] == _first(opening=12.0)
    assert reference.buy_days(START, AFTER) == [START]
    assert _check(_live(), reference) == []


def _ace_plan(code=ACE_CODE, *, opened=True):
    return {"strategy_leg": "A", "ts_code": code, "position_opened": opened, "outcome_observable": True,
            "status": "OK", "entry_price": 10.0, "stock_return_before_fees": 0.0,
            "exit_date": NEXT, "position_open_until": NEXT}


def _simulate(*, ace_plans=None, ace_allowed=None, l_picks=None, l_outcome=None, m_candidates=None):
    outcome = lambda _code, _day: (NEXT, 10.0, 0.0)
    return replay_ace_m_l(
        [START, NEXT, AFTER], ace_plans or {}, ace_allowed or {}, m_candidates or {}, outcome,
        l_picks or {}, l_outcome or outcome, {}, cash0=100_000.0, commission_rate=0.0,
        minimum_commission=0.0, transfer_rate=0.0,
        stamp_schedule=[{"start_date": "19000101", "end_date": "99991231", "rate": 0.0}],
    )


def test_replay_obeys_ace_then_l_and_blocks_m_beside_ace():
    equity, trades = _simulate(ace_plans={START: [_ace_plan()]}, l_picks={START: [L_CODE]},
                               m_candidates={START: [OTHER]})
    assert list(zip(trades["leg"], trades["ts_code"])) == [("A", ACE_CODE), ("L", L_CODE)]
    # 10元、100股整手：ACE 82,500元只能买82,000元；L剩余额度17,500元只能买17,000元。
    assert trades["weight"].tolist() == pytest.approx([0.82, 0.17])
    assert equity.tolist() == pytest.approx([100_000.0] * 3)


def test_replay_does_not_promote_second_l_pick_when_first_is_unbuyable():
    calls = []

    def outcome(code, day):
        calls.append((code, day))
        return None if code == L_CODE else (NEXT, 10.0, 0.0)

    _equity, trades = _simulate(l_picks={START: [L_CODE, OTHER]}, l_outcome=outcome)
    assert trades.empty
    assert calls == [(L_CODE, START)]


def test_replay_l_same_as_failed_ace_plan_is_not_bought_as_a_fallback():
    _equity, trades = _simulate(ace_plans={START: [_ace_plan(L_CODE, opened=False)]},
                                l_picks={START: [L_CODE, OTHER]})
    assert trades.empty


def test_replay_does_not_treat_ace_signal_blocked_by_old_holding_as_a_new_plan():
    plans = {START: [_ace_plan()], NEXT: [_ace_plan(L_CODE)]}
    _equity, trades = _simulate(ace_plans=plans, l_picks={NEXT: [L_CODE]},
                                l_outcome=lambda _code, _day: (AFTER, 10.0, 0.0))
    assert list(zip(trades["action_date"], trades["leg"], trades["ts_code"])) == [
        (START, "A", ACE_CODE), (NEXT, "L", L_CODE),
    ]


def test_replay_l_retains_capital_until_after_exit_date():
    _equity, trades = _simulate(l_picks={START: [L_CODE], NEXT: [OTHER]})
    assert list(zip(trades["action_date"], trades["ts_code"])) == [(START, L_CODE)]


def _shadow(dates, *, nav=None, e_plans=None):
    return {"nav": pd.Series(nav if nav is not None else [100_000.0] * len(dates), index=dates),
            "action_dates": dates, "execution": {"initial_cash": 100_000.0},
            "legs": {"A": pd.DataFrame(), "C": pd.DataFrame(),
                     "E": pd.DataFrame() if e_plans is None else e_plans, "D": pd.DataFrame()}}


def test_alignment_uses_amount_before_framework_date_and_ret5_after():
    dates = ["20261124", "20261125", START]
    shadow = _shadow(dates)
    shadow["legacy_alignment_shadow"] = _shadow(dates)
    live = _live(_buy(dates[0], L_CODE, "M", status="已平仓", exit_date=dates[1]),
                 _buy(START, OTHER, "M"))
    references = []

    class Reference:
        def __init__(self, _raw, *, sort_by):
            self.sort_by = sort_by
            references.append(self)

        def __call__(self, _day):
            codes = [L_CODE, OTHER] if self.sort_by == "amount" else [OTHER, L_CODE]
            return {"status": "OK", "candidates": [{**_first(code), "name": "M候选"} for code in codes]}

    with patch("src.live_alignment.MReference", Reference), \
            patch("src.live_alignment.gated_replay", return_value=_replay()):
        triggers = run_alignment(shadow, live, ma_window=60, lag=6, start=dates[0], end=START,
                                 ace_m_start="20261113", ace_m_l_start=START, raw_dir="unused",
                                 m_sort_by="ret5")
    assert triggers == []
    assert sorted(reference.sort_by for reference in references) == ["amount", "ret5"]


def test_alignment_splices_plans_and_each_versions_own_stop_flags():
    dates = ["20261119", "20261120", "20261123", "20261124", "20261125", START, NEXT]
    old_plans = pd.DataFrame({"action_date": [dates[2], START], "ts_code": [L_CODE, L_CODE]})
    new_plans = pd.DataFrame({"action_date": [dates[2], START], "ts_code": [OTHER, OTHER]})
    shadow = _shadow(dates, nav=[1, 1, 2, 1, 2, 1, 2], e_plans=new_plans)
    shadow["legacy_alignment_shadow"] = _shadow(dates, nav=[1, 2, 1, 2, 1, 2, 1], e_plans=old_plans)
    with patch("src.live_alignment.gated_replay", return_value=_replay()) as replay, \
            patch("src.live_alignment.alignment_triggers", return_value=[]) as check:
        run_alignment(shadow, _live(), ma_window=2, lag=0, start=dates[0], end=NEXT,
                      ace_m_start=dates[0], ace_m_l_start=START)
    aligned = replay.call_args.args[0]
    assert list(zip(aligned["legs"]["E"].action_date, aligned["legs"]["E"].ts_code)) == [
        (dates[2], L_CODE), (START, OTHER)]
    assert check.call_args.kwargs["stop_dates"] == {dates[2], dates[4], START}
    assert replay.call_args.kwargs["allowed_dates_override"] == set(dates) - {dates[2], dates[4], START}
    assert check.call_args.kwargs["ace_plan_codes"] == {dates[2]: {L_CODE}, START: {OTHER}}
    pd.testing.assert_frame_equal(shadow["legs"]["E"], new_plans)
    pd.testing.assert_series_equal(shadow["nav"], pd.Series([1, 1, 2, 1, 2, 1, 2], index=dates))


@pytest.mark.parametrize("problem", ["missing", "dates", "execution", "legs"])
def test_alignment_refuses_missing_or_mismatched_historical_inputs(problem):
    dates = ["20261125", START]
    shadow = _shadow(dates)
    if problem != "missing":
        shadow["legacy_alignment_shadow"] = _shadow(dates)
    if problem == "dates":
        shadow["legacy_alignment_shadow"]["action_dates"] = [START]
    elif problem == "execution":
        shadow["legacy_alignment_shadow"]["execution"] = {"initial_cash": 1.0}
    elif problem == "legs":
        shadow["legacy_alignment_shadow"]["legs"].pop("E")
    with pytest.raises(ValueError, match="历史对账"):
        run_alignment(shadow, _live(), ma_window=60, lag=6, start=dates[0], end=START, ace_m_l_start=START)


def test_legacy_alignment_path_ignores_new_reference_and_retains_original_call():
    shadow = _shadow(["20261125", START])
    with patch("src.live_alignment.gated_replay", return_value=_replay()) as replay, \
            patch("src.live_alignment.alignment_triggers", return_value=[]) as check, \
            patch("src.live_alignment.MReference") as reference:
        run_alignment(shadow, _live(), ma_window=60, lag=6, start="20261125", end=START,
                      ace_m_start="20261113", raw_dir="unused")
    assert replay.call_args.args[0] is shadow
    assert replay.call_args.kwargs == {"ma_window": 60, "lag": 6, "ungated_from": "20261113"}
    reference.assert_called_once_with("unused", sort_by="amount")
    assert check.call_args.kwargs["ace_m_l_start"] == ""


def _cash_plan(day, code, exit_day):
    return {"signal_date": "20261124" if day < START else "20261125", "buy_date": day,
            "action_date": day, "ts_code": code, "status": "OK", "entry_filled": True,
            "position_opened": True, "outcome_observable": True, "entry_price": 10.0,
            "entry_reference_price": 10.0, "exit_price": 10.0, "exit_reference_price": 10.0,
            "stock_return_before_fees": 0.0, "exit_date": exit_day, "position_open_until": exit_day,
            "position_scale": 1.0}


def test_alignment_preserves_old_position_capital_across_framework_date():
    dates = ["20261124", "20261125", START, NEXT, AFTER]
    old = _shadow(dates, e_plans=pd.DataFrame([_cash_plan("20261125", L_CODE, NEXT)]))
    shadow = _shadow(dates, e_plans=pd.DataFrame([_cash_plan(START, OTHER, AFTER)]))
    shadow["legacy_alignment_shadow"] = old
    # 只替换最终报告函数，实际执行拼接后的单账户gated_replay；不能在切换日重置资金。
    with patch("src.live_alignment.alignment_triggers", return_value=[]) as check:
        run_alignment(shadow, _live(), ma_window=60, lag=6, start="20261125", end=AFTER,
                      ace_m_start="20261113", ace_m_l_start=START)
    replay = check.call_args.args[0]
    opened = replay.loc[replay.position_opened.eq(True)]
    assert list(zip(opened.action_date, opened.ts_code)) == [("20261125", L_CODE)]
    assert replay.loc[replay.action_date.eq(START), "status"].item() == "SKIP_OCCUPIED"


@pytest.mark.parametrize("new_framework,include_legacy", [(False, True), (True, False), (True, True)])
def test_shadow_optional_legacy_reuses_context_and_cache_without_changing_new_results(
    tmp_path, new_framework, include_legacy,
):
    dates = ["20261125", START]
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"cal_date": ["20261124"] + dates, "is_open": 1}).to_csv(calendar, index=False)
    baselines = {"A": "a", "C": "c", "E": "old_e"}
    selected = {**baselines, "E": "new_e"} if new_framework else baselines
    calls = []

    def plan(variant, **kwargs):
        calls.append((variant, kwargs["outcome_cache"]))
        return pd.DataFrame({"action_date": [dates[0]], "ts_code": [L_CODE], "variant": [variant]})

    def cash(legs, **_kwargs):
        value = 120_000.0 if legs["E"].variant.iloc[0] == "new_e" else 90_000.0
        return pd.DataFrame({"action_date": dates, "equity_after": [value, value]})

    with ExitStack() as stack:
        stack.enter_context(patch("src.acde_plan.load_monthly_config", return_value={
            "market_controller": {"minimum_limit_up_count": 50}}))
        context = stack.enter_context(patch("src.acde_plan.plan_context", return_value={
            "signal_pool": "shared_pool", "d_events": "empty", "allowed_actions": set(dates),
            "action_dates": dates}))
        stack.enter_context(patch("src.acde_plan.variant_sets", return_value=(baselines, {})))
        stack.enter_context(patch("src.acde_plan.execution_kwargs", return_value={"initial_cash": 100_000.0}))
        stack.enter_context(patch("src.equity_curve_stop._apply_framework_to_baselines", return_value=selected))
        stack.enter_context(patch("src.acde_plan.build_variant_plan", side_effect=plan))
        stack.enter_context(patch("src.acde_rolling_framework.replay_action_date_cash_portfolio", side_effect=cash))
        result = build_shadow_replay(project_root=tmp_path, feature_path=tmp_path / "features.csv",
                                     sentiment_path=tmp_path / "sentiment.csv", calendar_path=calendar,
                                     start=dates[0], end=START, work_dir=tmp_path / "work",
                                     include_legacy_alignment=include_legacy)
    has_legacy = new_framework and include_legacy
    assert ("legacy_alignment_shadow" in result) == has_legacy
    assert result["nav"].tolist() == [1.2 if new_framework else 0.9] * 2
    assert context.call_count == 1
    assert [variant for variant, _cache in calls] == ["a", "c", "new_e" if new_framework else "old_e"] + (
        ["old_e"] if has_legacy else [])
    assert len({id(cache) for _variant, cache in calls}) == 1
    if has_legacy:
        assert result["legacy_alignment_shadow"]["nav"].tolist() == [0.9] * 2
