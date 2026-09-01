#!/usr/bin/env python3
"""核对A>C>E>D生产计划选择与收益优先正式认证口径一致。

本脚本不连接券商、不生成文件、不提交委托。它从当前正式严格候选重建每个
action_date的A/C/E计划，把同日候选交给CombinedLiveEngine，再验证：

1. A优先C、C优先E；
2. 只生成唯一静态BUY；
3. 有A/C/E计划时D全天关闭；
4. 三条静态腿均无计划时只授权D盘中监控；
5. 正式认证锁定177笔、12483.978370389923倍及A85/C52/E29/D11。

这里仅执行只读核对，不连接券商、不落地计划、不提交委托。
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import certify_acde_return_first_release as certifier  # noqa: E402
from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from src.acde_monthly_research import (  # noqa: E402
    _build_plans,
    _context,
    _execution_kwargs,
    _metrics,
    _replay,
    _variant_sets,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_framework import build_monthly_research_window  # noqa: E402
from src.combined_live_engine import CombinedLiveEngine  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


EXPECTED_PRIORITY = ("A", "C", "E", "D")
def make_engine() -> CombinedLiveEngine:
    """创建只读计划引擎；所有外部状态都由逐日测试数据显式注入。"""

    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = PROJECT_ROOT
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "A_C_E_D"},
    }
    engine.load_positions = lambda: []
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "A_C_E_D"
    engine.is_b_strategy_removed = lambda: True
    engine.load_today_e_signal = lambda _today: None
    engine.compute_e_preview = lambda _today: {
        "has_candidate": False,
        "has_scored_data": True,
        "neutral_segs": [],
    }
    return engine


def ac_order(row: dict[str, Any], action_date: str) -> dict[str, Any]:
    return {
        "paper_order_id": f"VERIFY-{row['strategy_leg']}-{action_date}-{row['ts_code']}",
        "signal_date": str(row.get("signal_date", "")),
        "strategy_leg": str(row["strategy_leg"]),
        "planned_order_date": action_date,
        "side": "BUY",
        "ts_code": str(row["ts_code"]),
        "name": str(row.get("name", "")),
        "reference_price": 10.0,
        "round_lot_shares": 10_000,
        "estimated_shares": 10_000,
        "planned_position_pct": 0.825,
    }


def e_signal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_date": str(row.get("signal_date", "")),
        "ts_code": str(row["ts_code"]),
        "name": str(row.get("name", "")),
        "limit_close": 10.0,
        "exit_offset": int(row.get("hold_offset", 2) or 2),
    }


def verify_daily_plan_selection(
    legs: dict[str, pd.DataFrame],
    *,
    research_action_dates: list[str],
) -> dict[str, int]:
    maps = {
        leg: strict.action_candidate_map(legs[leg], leg)
        for leg in EXPECTED_PRIORITY
    }
    candidate_action_dates = {
        date for mapping in maps.values() for date in mapping
    }
    # 不能只核对“有候选”的日期；全市场无候选日也必须确认生产状态机不会
    # 静默关闭D。A/C/E在窗口最后一个信号日的T+1计划可能落到END之后，
    # 因此把候选真实开仓日并入正式窗口交易日。
    action_dates = sorted(set(research_action_dates) | candidate_action_dates)
    checked = {"action_dates": 0, "static_buy_dates": 0, "d_monitor_dates": 0}
    engine = make_engine()

    for action_date in action_dates:
        ac_rows = [
            ac_order(maps[leg][action_date], action_date)
            for leg in ("A", "C")
            if action_date in maps[leg]
        ]
        ac_frame = pd.DataFrame(ac_rows)
        engine.load_latest_abc_orders = lambda frame=ac_frame: (
            PROJECT_ROOT / "reports" / "verification" / "synthetic_ac_orders.csv",
            frame.copy(),
        )
        e_payload = (
            e_signal(maps["E"][action_date])
            if action_date in maps["E"]
            else None
        )
        engine.load_yesterday_e_signal = lambda _today, payload=e_payload: payload

        _state, decisions, orders = engine.build_mode1_plan(action_date)
        actions = set(decisions.get("action", pd.Series(dtype=str)).astype(str))
        buys = (
            orders[orders["side"].astype(str).str.upper().eq("BUY")].copy()
            if not orders.empty and "side" in orders
            else pd.DataFrame()
        )
        expected_leg = next(
            (leg for leg in ("A", "C", "E") if action_date in maps[leg]),
            "",
        )
        if expected_leg:
            if len(buys) != 1:
                raise RuntimeError(
                    f"{action_date}预期{expected_leg}唯一BUY，生产计划却有{len(buys)}行"
                )
            actual_leg = str(buys.iloc[0].get("strategy_leg", "")).upper()
            actual_code = str(buys.iloc[0].get("ts_code", ""))
            expected_code = str(maps[expected_leg][action_date]["ts_code"])
            if (actual_leg, actual_code) != (expected_leg, expected_code):
                raise RuntimeError(
                    f"{action_date}计划选择不一致：生产={actual_leg}/{actual_code}，"
                    f"认证={expected_leg}/{expected_code}"
                )
            if (
                "BLOCK_D_INTRADAY_MONITOR" not in actions
                or "ALLOW_D_INTRADAY_MONITOR" in actions
            ):
                raise RuntimeError(f"{action_date}存在{expected_leg}计划但D未被唯一关闭")
            checked["static_buy_dates"] += 1
        else:
            if not buys.empty:
                raise RuntimeError(f"{action_date}无静态候选却生成了BUY计划")
            if (
                "ALLOW_D_INTRADAY_MONITOR" not in actions
                or "BLOCK_D_INTRADAY_MONITOR" in actions
            ):
                raise RuntimeError(f"{action_date}无A/C/E计划但D未获唯一盘中授权")
            checked["d_monitor_dates"] += 1
        checked["action_dates"] += 1
    return checked


def main() -> int:
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    actual_priority = tuple(
        config.get("portfolio_certification", {}).get("strategy_priority_order", [])
    )
    if actual_priority != EXPECTED_PRIORITY:
        raise RuntimeError(
            f"配置腿序漂移：actual={actual_priority} expected={EXPECTED_PRIORITY}"
        )

    monthly = load_monthly_config(
        PROJECT_ROOT / "config" / "acde_rolling_optimization.json"
    )
    window = build_monthly_research_window(certifier.CUTOFF)
    paths = monthly_paths(monthly, certifier.CUTOFF)
    context = _context(
        window=window,
        feature_path=paths["strict_feature_pool"],
        sentiment_path=paths["market_sentiment"],
        d_event_path=paths["d_event_source"],
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=0,
    )
    baselines, _candidates = _variant_sets()
    legs = _build_plans(baselines, context=context, cutoff=certifier.CUTOFF)
    daily = _replay(
        legs,
        action_dates=context["action_dates"],
        execution=_execution_kwargs(monthly),
    )
    metrics = _metrics(daily, window)
    expected_metrics = {
        field: certifier.EXPECTED_COMBO[field]
        for field in ("trade_count", "equity_multiple", "max_drawdown")
    }
    for field, expected in expected_metrics.items():
        actual = float(metrics[field])
        if not certifier.close_enough(actual, expected):
            raise RuntimeError(f"收益优先认证{field}漂移：{actual} != {expected}")
    actual_counts = {
        leg: int(metrics["leg_counts"].get(leg, 0)) for leg in EXPECTED_PRIORITY
    }
    expected_counts = certifier.EXPECTED_COMBO["leg_counts"]
    if actual_counts != expected_counts:
        raise RuntimeError(
            f"收益优先认证分腿漂移：{actual_counts} != {expected_counts}"
        )

    checked = verify_daily_plan_selection(
        legs,
        research_action_dates=list(context["action_dates"]),
    )
    print("A>C>E>D生产计划与收益优先正式认证核对通过")
    print(
        f"{int(metrics['trade_count'])}笔 | {float(metrics['equity_multiple']):.12f}倍 | "
        f"回撤{float(metrics['max_drawdown']):.4%} | 分腿{actual_counts}"
    )
    print(
        f"逐日计划核对：{checked['action_dates']}个真实开仓日，"
        f"静态唯一BUY={checked['static_buy_dates']}日，D兜底授权={checked['d_monitor_dates']}日"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
