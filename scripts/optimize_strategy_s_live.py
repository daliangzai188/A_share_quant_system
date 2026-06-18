"""
策略S-live实盘口径优化搜索。

目标：
  - 允许重做策略S的筛选逻辑，但保留“深市/创业板补充策略”的定位。
  - 每组参数直接用实盘认证口径复放：10.05万资金、单笔<5万、整百股、费用、滑点压力。
  - 只有压力滑点场景也能通过认证闸门的参数，才允许进入后续实盘接入候选。

不做的事：
  - 不连接券商。
  - 不提交任何真实委托。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_strategy_s_live import (
    CertificationGate,
    ReplayConfig,
    evaluate_gate,
    replay_live_cap,
)
from scripts.research_strategy_s import (
    TEST_END,
    TEST_START,
    WARMUP_START,
    build_all_candidates,
    build_market_sentiment,
    build_price_lookup,
    compute_rolling_features,
    load_all_basic,
    load_all_daily,
    load_all_limitup,
    load_trade_calendar,
)
from src.utils.config import load_json_config, mkdir_p

OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_s" / "live_optimization"
DEFAULT_SLIPPAGES = [0.001, 0.003, 0.005, 0.01]


SORT_COLUMNS: dict[str, tuple[list[str], list[bool]]] = {
    "vr_desc": (["volume_ratio5", "turnover_rate"], [False, False]),
    "turnover_desc": (["turnover_rate", "volume_ratio5"], [False, False]),
    "pct_desc": (["pct_chg", "turnover_rate"], [False, False]),
    "circ_asc": (["circ_mv", "volume_ratio5"], [True, False]),
    "strength_score": (["live_score"], [False]),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="策略S-live实盘口径优化")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--initial-cash", type=float, default=100_500.0)
    parser.add_argument("--max-order-amount", type=float, default=50_000.0)
    parser.add_argument("--cash-buffer", type=float, default=500.0)
    parser.add_argument("--slippage-grid", default="0.001,0.003,0.005,0.01")
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--stage1-keep", type=int, default=800, help="第一阶段保留进入压力认证的候选数")
    parser.add_argument("--max-combos", type=int, default=0, help="调试用，0表示不限制")
    parser.add_argument("--variants", default="all", help="只搜索指定变体，逗号分隔：H1,H4,H3 或 all")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return parser.parse_args()


def add_live_score(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["live_score"] = (
        pd.to_numeric(result["pct_chg"], errors="coerce").fillna(0) * 1.8
        + pd.to_numeric(result["turnover_rate"], errors="coerce").fillna(0) * 0.7
        + pd.to_numeric(result["volume_ratio5"], errors="coerce").fillna(0) * 1.2
        + pd.to_numeric(result["market_limit_count"], errors="coerce").fillna(0) * 0.03
        - pd.to_numeric(result["circ_mv"], errors="coerce").fillna(0) / 10000 * 0.08
    )
    return result


def build_live_param_grid() -> list[dict[str, Any]]:
    grid: list[dict[str, Any]] = []
    base = {
        "universe": "chi_next",
        "amount_min_yi": 1.0,
        "circ_mv_min_yi": 0.0,
        "require_bullish": False,
    }

    # H1: 近涨停动量，但加入量比上限、换手下限和市场情绪，避免2026的过热放量回落。
    for pct_lo in [5.5, 6.0, 6.5, 7.0]:
        for pct_hi in [9.3, 9.9]:
            for vr_lo in [2.5, 3.0, 3.5]:
                for vr_hi in [6.5, 7.5, 8.0]:
                    if vr_lo >= vr_hi:
                        continue
                    for turnover_min in [10.0, 12.0, 14.0]:
                        for circ_mv_max_yi in [30.0, 50.0, 80.0]:
                            for market_min in [30, 40, 50]:
                                for sort_by in ["vr_desc", "turnover_desc", "pct_desc", "strength_score"]:
                                    for sell_after_buy_days in [1, 2, 3]:
                                        grid.append({
                                            **base,
                                            "strategy_variant": "H1_live_momentum",
                                            "pct_lo": pct_lo,
                                            "pct_hi": pct_hi,
                                            "volume_ratio_min": vr_lo,
                                            "volume_ratio_max": vr_hi,
                                            "turnover_min": turnover_min,
                                            "circ_mv_max_yi": circ_mv_max_yi,
                                            "market_min_limit_count": market_min,
                                            "sort_by": sort_by,
                                            "sell_after_buy_days": sell_after_buy_days,
                                        })

    # H4: 近涨停 + 突破，牺牲频率换取更强趋势确认。
    for pct_lo in [5.5, 6.0, 6.5]:
        for vr_lo in [2.0, 2.5]:
            for vr_hi in [6.5, 8.0]:
                if vr_lo >= vr_hi:
                    continue
                for turnover_min in [8.0, 10.0, 12.0]:
                    for breakout_window in [20, 60]:
                        for circ_mv_max_yi in [50.0, 80.0]:
                            for market_min in [30, 40, 50]:
                                for sort_by in ["turnover_desc", "strength_score", "pct_desc"]:
                                    for sell_after_buy_days in [1, 2, 3]:
                                        grid.append({
                                            **base,
                                            "strategy_variant": "H4_breakout_momentum",
                                            "pct_lo": pct_lo,
                                            "pct_hi": 9.9,
                                            "volume_ratio_min": vr_lo,
                                            "volume_ratio_max": vr_hi,
                                            "turnover_min": turnover_min,
                                            "breakout_window": breakout_window,
                                            "circ_mv_max_yi": circ_mv_max_yi,
                                            "market_min_limit_count": market_min,
                                            "sort_by": sort_by,
                                            "sell_after_buy_days": sell_after_buy_days,
                                        })

    # H3: 强势回踩，收益可能不如H1，但滑点压力通常更稳。
    for cum_min in [15.0, 20.0, 25.0]:
        for pullback_lo in [1.5, 2.0]:
            for pullback_hi in [4.0, 5.0]:
                if pullback_lo >= pullback_hi:
                    continue
                for vr_hi in [1.0, 1.2, 1.5]:
                    for turnover_min in [6.0, 8.0, 10.0]:
                        for circ_mv_max_yi in [50.0, 80.0, 120.0]:
                            for market_min in [0, 30, 40]:
                                for sort_by in ["turnover_desc", "strength_score"]:
                                    for sell_after_buy_days in [1, 2, 3]:
                                        grid.append({
                                            **base,
                                            "strategy_variant": "H3_pullback",
                                            "cum_return15_min": cum_min,
                                            "pullback_lo": pullback_lo,
                                            "pullback_hi": pullback_hi,
                                            "volume_ratio_max": vr_hi,
                                            "turnover_min": turnover_min,
                                            "circ_mv_max_yi": circ_mv_max_yi,
                                            "market_min_limit_count": market_min,
                                            "sort_by": sort_by,
                                            "sell_after_buy_days": sell_after_buy_days,
                                        })
    return grid


def filter_pool(pool: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    df = pool.copy()
    df = df[(df["segment"] == "chi_next") & (~df["is_limitup"])].copy()
    df = df[pd.to_numeric(df["amount"], errors="coerce") >= float(params["amount_min_yi"]) * 10000]
    df = df[pd.to_numeric(df["circ_mv"], errors="coerce") <= float(params["circ_mv_max_yi"]) * 10000]
    df = df[pd.to_numeric(df["circ_mv"], errors="coerce") >= float(params.get("circ_mv_min_yi", 0.0)) * 10000]
    df = df[pd.to_numeric(df["turnover_rate"], errors="coerce") >= float(params.get("turnover_min", 0.0))]
    df = df[pd.to_numeric(df["market_limit_count"], errors="coerce") >= float(params.get("market_min_limit_count", 0))]
    if params.get("require_bullish", False):
        df = df[df["close"] >= df["open"]]

    variant = str(params["strategy_variant"])
    if variant.startswith("H1"):
        df = df[
            (pd.to_numeric(df["pct_chg"], errors="coerce") >= float(params["pct_lo"]))
            & (pd.to_numeric(df["pct_chg"], errors="coerce") <= float(params["pct_hi"]))
            & (pd.to_numeric(df["volume_ratio5"], errors="coerce") >= float(params["volume_ratio_min"]))
            & (pd.to_numeric(df["volume_ratio5"], errors="coerce") <= float(params["volume_ratio_max"]))
        ]
    elif variant.startswith("H4"):
        high_col = "high20" if int(params["breakout_window"]) == 20 else "high60"
        df = df[
            (pd.to_numeric(df["pct_chg"], errors="coerce") >= float(params["pct_lo"]))
            & (pd.to_numeric(df["pct_chg"], errors="coerce") <= float(params["pct_hi"]))
            & (pd.to_numeric(df["volume_ratio5"], errors="coerce") >= float(params["volume_ratio_min"]))
            & (pd.to_numeric(df["volume_ratio5"], errors="coerce") <= float(params["volume_ratio_max"]))
            & (pd.to_numeric(df["close"], errors="coerce") > pd.to_numeric(df[high_col], errors="coerce"))
        ]
    elif variant.startswith("H3"):
        df = df[
            (pd.to_numeric(df["cum_return15"], errors="coerce") >= float(params["cum_return15_min"]))
            & (pd.to_numeric(df["pct_chg"], errors="coerce") <= -float(params["pullback_lo"]))
            & (pd.to_numeric(df["pct_chg"], errors="coerce") >= -float(params["pullback_hi"]))
            & (pd.to_numeric(df["volume_ratio5"], errors="coerce") <= float(params["volume_ratio_max"]))
        ]
    return add_live_score(df).reset_index(drop=True)


def build_strategy_trades(
    pool: pd.DataFrame,
    price_lookup: dict,
    params: dict[str, Any],
    sell_date_maps: dict[int, dict[str, str]],
) -> pd.DataFrame:
    candidates = filter_pool(pool, params)
    if candidates.empty:
        return pd.DataFrame()

    sort_cols, ascending = SORT_COLUMNS[str(params["sort_by"])]
    rows: list[dict[str, Any]] = []
    position_sell_date = ""

    for signal_date in sorted(candidates["signal_date"].astype(str).unique()):
        if position_sell_date and signal_date < position_sell_date:
            continue
        day = candidates[candidates["signal_date"].astype(str) == signal_date].copy()
        if day.empty:
            continue
        day = day.sort_values(sort_cols, ascending=ascending)

        for _, row in day.iterrows():
            buy_date = str(row.get("buy_date", ""))
            sell_after_buy_days = int(params.get("sell_after_buy_days", 1))
            sell_date = sell_date_maps.get(sell_after_buy_days, {}).get(signal_date, "")
            buy_key = (str(row["ts_code"]), buy_date)
            sell_key = (str(row["ts_code"]), sell_date)
            if buy_key not in price_lookup or sell_key not in price_lookup:
                continue
            buy_open, _ = price_lookup[buy_key]
            _, sell_close = price_lookup[sell_key]
            if buy_open <= 0 or sell_close <= 0:
                continue
            rows.append({
                "signal_date": signal_date,
                "buy_date": buy_date,
                "sell_date": sell_date,
                "ts_code": str(row["ts_code"]),
                "segment": str(row.get("segment", "")),
                "pct_chg": float(row.get("pct_chg", 0.0)),
                "volume_ratio5": float(row.get("volume_ratio5", 0.0)),
                "turnover_rate": float(row.get("turnover_rate", 0.0)),
                "circ_mv_yi": float(row.get("circ_mv", 0.0)) / 10000,
                "cum_return15": float(row.get("cum_return15", 0.0)),
                "market_limit_count": int(row.get("market_limit_count", 0)),
                "buy_price": float(buy_open),
                "sell_price": float(sell_close),
                "year": signal_date[:4],
                "live_score": float(row.get("live_score", 0.0)),
            })
            position_sell_date = sell_date
            break
    return pd.DataFrame(rows)


def load_candidate_pool() -> tuple[pd.DataFrame, dict, list[str]]:
    calendar = load_trade_calendar()
    print(f"[数据] 交易日历 {len(calendar)} 天")
    daily_all = load_all_daily(WARMUP_START, TEST_END)
    print(f"[数据] 日线 {len(daily_all):,} 行")
    daily_feat = compute_rolling_features(daily_all)
    basic_df = load_all_basic(WARMUP_START, TEST_END)
    limitup_by_date = load_all_limitup(WARMUP_START, TEST_END)
    market_sentiment = build_market_sentiment(limitup_by_date)
    pool = build_all_candidates(daily_feat, basic_df, limitup_by_date, market_sentiment, calendar)
    pool = pool[
        (pool["trade_date"] >= TEST_START)
        & (pool["trade_date"] <= TEST_END)
        & (pool["segment"] == "chi_next")
    ].copy()
    pool = pool[
        (~pool["is_limitup"])
        & (
            ((pool["pct_chg"] >= 5.0) & (pool["pct_chg"] <= 9.9) & (pool["volume_ratio5"] >= 1.8))
            | ((pool["cum_return15"] >= 12.0) & (pool["pct_chg"] <= -1.5) & (pool["pct_chg"] >= -6.0))
        )
    ].copy()
    price_lookup = build_price_lookup(daily_all)
    print(f"[数据] S-live候选池 {len(pool):,} 行")
    return pool, price_lookup, calendar


def build_sell_date_maps(calendar: list[str], max_sell_after_buy_days: int = 3) -> dict[int, dict[str, str]]:
    maps: dict[int, dict[str, str]] = {}
    for sell_after_buy_days in range(1, max_sell_after_buy_days + 1):
        signal_to_sell: dict[str, str] = {}
        offset = sell_after_buy_days + 1
        for i, signal_date in enumerate(calendar):
            if i + offset < len(calendar):
                signal_to_sell[signal_date] = calendar[i + offset]
        maps[sell_after_buy_days] = signal_to_sell
    return maps


def replay_param(
    trades: pd.DataFrame,
    calendar: list[str],
    slippages: list[float],
    args: argparse.Namespace,
    risk_config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summaries = []
    detail_frames = []
    for slip in slippages:
        replay_config = ReplayConfig(
            scenario=f"slippage_{slip:.3%}",
            initial_cash=float(args.initial_cash),
            max_single_order_amount=float(args.max_order_amount),
            cash_buffer=float(args.cash_buffer),
            buy_slippage_rate=slip,
            sell_slippage_rate=slip,
            commission_rate=float(risk_config.get("commission_rate", 0.0003)),
            stamp_tax_rate=float(risk_config.get("stamp_tax_rate", 0.001)),
            transfer_fee_rate=float(risk_config.get("transfer_fee_rate", 0.00001)),
            min_commission=0.0,
            lot_size=100,
        )
        detail, summary = replay_live_cap(trades, calendar, replay_config)
        detail_frames.append(detail)
        summaries.append(summary)
    return pd.DataFrame(summaries), pd.concat(detail_frames, ignore_index=True)


def stage1_score(summary: dict[str, Any]) -> float:
    return (
        float(summary["equity_multiple"]) * 80
        + float(summary["return_2026"]) * 120
        + float(summary["max_drawdown"]) * 80
        + float(summary["win_rate"]) * 10
        - max(0, int(summary["max_consecutive_losses"]) - 6) * 3
    )


def replay_one_slippage(
    trades: pd.DataFrame,
    calendar: list[str],
    slip: float,
    args: argparse.Namespace,
    risk_config: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    replay_config = ReplayConfig(
        scenario=f"stage1_slippage_{slip:.3%}",
        initial_cash=float(args.initial_cash),
        max_single_order_amount=float(args.max_order_amount),
        cash_buffer=float(args.cash_buffer),
        buy_slippage_rate=slip,
        sell_slippage_rate=slip,
        commission_rate=float(risk_config.get("commission_rate", 0.0003)),
        stamp_tax_rate=float(risk_config.get("stamp_tax_rate", 0.001)),
        transfer_fee_rate=float(risk_config.get("transfer_fee_rate", 0.00001)),
        min_commission=0.0,
        lot_size=100,
    )
    return replay_live_cap(trades, calendar, replay_config)


def score_result(summary_df: pd.DataFrame, gate: CertificationGate) -> tuple[bool, float, str]:
    if summary_df.empty:
        return False, -999.0, "无复放结果"
    base = summary_df.iloc[0].to_dict()
    statuses = []
    failures: list[str] = []
    for _, row in summary_df.iterrows():
        status, failed = evaluate_gate(row.to_dict(), base, gate)
        statuses.append(status)
        failures.extend(failed)
    pass_all = all(status == "PASS" for status in statuses)
    worst = summary_df.iloc[-1]
    base_row = summary_df.iloc[0]
    score = (
        float(worst["equity_multiple"]) * 100
        + float(worst["return_2026"]) * 80
        + float(worst["max_drawdown"]) * 50
        + float(base_row["equity_multiple"]) * 15
        + (30 if pass_all else 0)
        - max(0, int(worst["max_consecutive_losses"]) - gate.max_consecutive_losses) * 2
    )
    return pass_all, score, "; ".join(sorted(set(failures)))


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    mkdir_p(out_dir)
    config = load_json_config(args.config)
    risk_config = config.get("analysis", {})
    slippages = [float(x.strip()) for x in str(args.slippage_grid).split(",") if x.strip()]
    gate = CertificationGate(min_trade_count=int(args.min_trades))

    pool, price_lookup, calendar = load_candidate_pool()
    sell_date_maps = build_sell_date_maps(calendar, 3)
    param_grid = build_live_param_grid()
    variants_arg = str(args.variants).strip()
    if variants_arg.lower() != "all":
        wanted = {item.strip().upper() for item in variants_arg.split(",") if item.strip()}
        prefix_map = {
            "H1": "H1_",
            "H4": "H4_",
            "H3": "H3_",
        }
        param_grid = [
            p for p in param_grid
            if any(str(p["strategy_variant"]).startswith(prefix_map.get(v, v)) for v in wanted)
        ]
    if args.max_combos > 0:
        param_grid = param_grid[: int(args.max_combos)]
    print(f"[搜索] 参数组合 {len(param_grid):,} 组；滑点={slippages}")

    stage1_rows: list[dict[str, Any]] = []
    stage1_packs: list[tuple[float, dict[str, Any], pd.DataFrame, dict[str, Any]]] = []
    best_pack: tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame] | None = None
    best_score = -999.0

    for i, params in enumerate(param_grid, 1):
        if i % 500 == 0:
            print(f"[阶段1] 扫描 {i}/{len(param_grid)}，当前保留 {len(stage1_packs)}")
        trades = build_strategy_trades(pool, price_lookup, params, sell_date_maps)
        if len(trades) < int(args.min_trades):
            continue
        _, stage1_summary = replay_one_slippage(trades, calendar, slippages[0], args, risk_config)
        score1 = stage1_score(stage1_summary)
        row1 = {
            **params,
            "stage1_score": score1,
            "stage1_trade_count": stage1_summary["trade_count"],
            "stage1_equity_multiple": stage1_summary["equity_multiple"],
            "stage1_return_2026": stage1_summary["return_2026"],
            "stage1_max_drawdown": stage1_summary["max_drawdown"],
            "stage1_max_consecutive_losses": stage1_summary["max_consecutive_losses"],
        }
        stage1_rows.append(row1)
        # 明显不适合实盘的组合不进入压力认证。
        if (
            stage1_summary["return_2026"] <= 0
            or stage1_summary["max_drawdown"] < -0.20
            or stage1_summary["max_consecutive_losses"] > 8
        ):
            continue
        stage1_packs.append((score1, params, trades, stage1_summary))
        if i % 1000 == 0:
            print(f"[阶段1] {i}/{len(param_grid)} 已保留 {len(stage1_packs)} 个候选")

    stage1_df = pd.DataFrame(stage1_rows).sort_values("stage1_score", ascending=False)
    stage1_path = out_dir / "s_live_optimization_stage1.csv"
    stage1_df.to_csv(stage1_path, index=False, encoding="utf-8-sig")
    stage1_packs = sorted(stage1_packs, key=lambda item: item[0], reverse=True)[: int(args.stage1_keep)]
    print(f"[阶段1] 完成，保留 {len(stage1_packs)} 个候选进入压力认证；明细={stage1_path}")

    rows: list[dict[str, Any]] = []
    for i, (score1, params, trades, stage1_summary) in enumerate(stage1_packs, 1):
        summary_df, detail_df = replay_param(trades, calendar, slippages, args, risk_config)
        pass_all, score, failures = score_result(summary_df, gate)
        worst = summary_df.iloc[-1].to_dict()
        base = summary_df.iloc[0].to_dict()
        row = {
            **params,
            "pass_all": pass_all,
            "score": score,
            "failure_reasons": failures,
            "stage1_score": score1,
            "raw_signal_count": len(trades),
            "base_equity_multiple": base["equity_multiple"],
            "base_return_2026": base["return_2026"],
            "base_max_drawdown": base["max_drawdown"],
            "stress_equity_multiple": worst["equity_multiple"],
            "stress_return_2026": worst["return_2026"],
            "stress_max_drawdown": worst["max_drawdown"],
            "stress_trade_count": worst["trade_count"],
            "stress_trade_count_2026": worst["trade_count_2026"],
            "stress_max_consecutive_losses": worst["max_consecutive_losses"],
        }
        rows.append(row)
        if score > best_score:
            best_score = score
            best_pack = (params, trades, summary_df, detail_df)
            print(
                f"[NEW BEST stage2 {i}/{len(stage1_packs)}] pass={pass_all} score={score:.2f} "
                f"stress={worst['equity_multiple']:.2f}x 2026={worst['return_2026']:.2%} "
                f"dd={worst['max_drawdown']:.2%} trades={worst['trade_count']} params={params}"
            )

    result_df = pd.DataFrame(rows)
    if result_df.empty:
        raise RuntimeError("没有找到满足最小交易笔数的S-live参数。")
    result_df = result_df.sort_values(["pass_all", "score", "stress_equity_multiple"], ascending=[False, False, False])

    search_path = out_dir / "s_live_optimization_search.csv"
    best_trades_path = out_dir / "s_live_optimized_trades.csv"
    best_summary_path = out_dir / "s_live_optimized_certification_summary.csv"
    best_detail_path = out_dir / "s_live_optimized_certification_detail.csv"
    best_config_path = out_dir / "s_live_optimized_config.json"
    report_path = out_dir / "s_live_optimization_report.md"

    result_df.to_csv(search_path, index=False, encoding="utf-8-sig")

    if best_pack is None:
        raise RuntimeError("搜索结果异常：best_pack为空。")
    best_params, best_trades, best_summary, best_detail = best_pack
    best_trades.to_csv(best_trades_path, index=False, encoding="utf-8-sig")
    best_summary.to_csv(best_summary_path, index=False, encoding="utf-8-sig")
    best_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")

    best_row = result_df.iloc[0].to_dict()
    best_config_path.write_text(
        json.dumps(
            {
                "best_params": best_params,
                "best_row": best_row,
                "gate": gate.__dict__,
                "slippages": slippages,
                "input_window": {"start": TEST_START, "end": TEST_END, "warmup_start": WARMUP_START},
                "outputs": {
                    "search": str(search_path.relative_to(PROJECT_ROOT)),
                    "trades": str(best_trades_path.relative_to(PROJECT_ROOT)),
                    "certification_summary": str(best_summary_path.relative_to(PROJECT_ROOT)),
                    "certification_detail": str(best_detail_path.relative_to(PROJECT_ROOT)),
                },
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    top = result_df.head(20).copy()
    content = [
        "# 策略S-live 实盘口径优化报告",
        "",
        "## 最优结果",
        "",
        f"- 是否全压力滑点通过：{best_row['pass_all']}",
        f"- 压力场景资金倍数：{best_row['stress_equity_multiple']:.2f}x",
        f"- 压力场景2026收益：{best_row['stress_return_2026']:.2%}",
        f"- 压力场景最大回撤：{best_row['stress_max_drawdown']:.2%}",
        f"- 压力场景交易笔数：{int(best_row['stress_trade_count'])}",
        f"- 压力场景2026交易笔数：{int(best_row['stress_trade_count_2026'])}",
        f"- 压力场景最大连续亏损：{int(best_row['stress_max_consecutive_losses'])}",
        f"- 失败原因：{best_row.get('failure_reasons', '') or '无'}",
        "",
        "## 最优参数",
        "",
    ]
    for key, value in best_params.items():
        content.append(f"- {key}: {value}")
    show_cols = [
        "pass_all",
        "score",
        "strategy_variant",
        "pct_lo",
        "pct_hi",
        "volume_ratio_min",
        "volume_ratio_max",
        "turnover_min",
        "circ_mv_max_yi",
        "market_min_limit_count",
        "sort_by",
        "stress_equity_multiple",
        "stress_return_2026",
        "stress_max_drawdown",
        "stress_trade_count",
        "stress_max_consecutive_losses",
    ]
    for col in show_cols:
        if col not in top.columns:
            top[col] = np.nan
    content.extend(["", "## Top 20", "", top[show_cols].to_markdown(index=False)])
    report_path.write_text("\n".join(content), encoding="utf-8")

    print("[完成] 搜索结果:", search_path)
    print("[完成] 最优交易:", best_trades_path)
    print("[完成] 最优认证:", best_summary_path)
    print("[完成] 报告:", report_path)
    print(result_df.head(10)[[
        "pass_all",
        "score",
        "strategy_variant",
        "stress_equity_multiple",
        "stress_return_2026",
        "stress_max_drawdown",
        "stress_trade_count",
        "failure_reasons",
    ]].to_string(index=False))


if __name__ == "__main__":
    main()
