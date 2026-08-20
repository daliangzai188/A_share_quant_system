"""策略N研究公共口径。

该模块只提供 v2/v3 研究脚本需要的稳定函数，不写实盘信号。历史候选强制读取
严格 as-of 成交空间打分表，收益统一使用前复权、固定开盘成交、到期日盘中止盈
以及收盘跌停顺延规则。
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts import certify_current_executable_portfolio as cert  # noqa: E402
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.strategy_n import apply_n_base_filters, load_n_spec  # noqa: E402
from src.trading_fees import account_return_after_fees  # noqa: E402


START_DATE = "20240520"
END_DATE = "20260514"

CONDITION_FIELDS = (
    "amount_bucket", "amount_ratio_bucket", "board_type", "fd_ratio_bucket",
    "first_time_detail_bucket", "limit_height_rank_bucket",
    "limit_times_detail_bucket", "limit_up_count_bucket",
    "market_chain_count_bucket", "market_emotion_state_bucket",
    "market_limit_down_count_bucket", "market_segment", "market_sentiment_level",
    "pct_chg_bucket", "prev_pct_chg_bucket", "retreat_state_bucket",
    "segment_chain_count_bucket", "segment_emotion_state_bucket",
    "segment_limit_down_count_bucket", "segment_limit_down_ratio_bucket",
    "segment_limit_height_rank_bucket", "segment_limit_max_height_bucket",
    "segment_limit_up_count_bucket", "segment_limit_up_ratio_bucket",
    "segment_market_sentiment_level", "segment_retreat_state_bucket",
    "volume_ratio_bucket",
)

FORBIDDEN_EXACT = {
    "buy_executed", "sell_executed", "scenario_executed", "net_return",
    "gross_return", "exit_trade_date", "exit_close", "is_win", "equity",
    "stock_return_before_fees", "execution_status",
}
FORBIDDEN_TOKENS = (
    "next_", "future_", "exit_", "return", "profit", "d1_", "d2_", "d3_", "d4_", "d5_",
)

RANKERS: dict[str, tuple[list[str], list[bool]]] = {
    "amount_desc": (["amount", "circ_mv", "ts_code"], [False, True, True]),
    "circ_mv_asc": (["circ_mv", "ts_code"], [True, True]),
    "fd_ratio_desc": (["fd_amount_to_circ_mv", "circ_mv", "ts_code"], [False, True, True]),
    "first_time_asc": (["first_time_minutes", "circ_mv", "ts_code"], [True, True, True]),
    "market_leader_rank_asc": (["market_leader_rank", "circ_mv", "ts_code"], [True, True, True]),
    "segment_leader_rank_asc": (["segment_leader_rank", "circ_mv", "ts_code"], [True, True, True]),
    "turnover_rate_desc": (["turnover_rate", "circ_mv", "ts_code"], [False, True, True]),
}


def _config() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))


def load_signal_pool() -> pd.DataFrame:
    config = _config()
    pool = load_historical_bucketed_pool(START_DATE, END_DATE, 80)
    pool = apply_n_base_filters(pool, load_n_spec(config))
    method = pool.get("fill_probability_method", pd.Series(index=pool.index, dtype=str)).astype(str)
    if method.empty or not method.eq("asof_turnover_space_proxy_v2").all():
        raise RuntimeError("N研究池不是严格as-of成交空间打分结果")
    return pool.copy()


@lru_cache(maxsize=None)
def account_outcome(signal_date: str, ts_code: str, name: str) -> dict[str, Any]:
    config = _config()
    live = config.get("live_trade", {})
    analysis = config.get("analysis", {})
    outcome = trade_return_details(
        str(signal_date),
        str(ts_code),
        2,
        name=str(name),
        use_intraday_takeprofit=bool(live.get("intraday_takeprofit_enabled", True)),
        takeprofit_offset=float(live.get("intraday_takeprofit_offset", 0.01)),
    )
    result: dict[str, Any] = {
        "strategy_leg": "N",
        "ts_code": str(ts_code),
        "name": str(name),
        "buy_date": outcome.buy_date,
        "exit_date": outcome.exit_date,
        "execution_status": outcome.status,
        "exit_rule": outcome.exit_rule,
        "return_source": f"N研究v3:{outcome.exit_rule or outcome.status}",
    }
    if outcome.status != "OK" or outcome.stock_return is None:
        result["account_return"] = 0.0
        return result
    result["account_return"] = account_return_after_fees(
        stock_return_before_fees=float(outcome.stock_return),
        exit_date=outcome.exit_date,
        position_pct=float(config.get("portfolio_certification", {}).get("position_pct", 0.825)),
        commission_rate=float(analysis.get("commission_rate", 0.0003)),
        transfer_fee_rate=float(analysis.get("transfer_fee_rate", 0.00001)),
        stamp_tax_schedule=analysis.get("stamp_tax_schedule"),
    )
    return result


@lru_cache(maxsize=None)
def cached_hit_limit_up(trade_date: str, ts_code: str) -> bool:
    return cert.hit_limit_up(str(trade_date), str(ts_code))


def max_consecutive_losses(returns: pd.Series) -> int:
    maximum = current = 0
    for value in pd.to_numeric(returns, errors="coerce").dropna():
        if float(value) <= 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def metrics(detail: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    trades = detail[
        detail["status"].eq("EXECUTED") & detail["signal_date"].between(start, end)
    ].copy()
    values = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").dropna()
    curve = (1.0 + values).cumprod()
    peak = curve.cummax().clip(lower=1.0) if len(curve) else pd.Series([1.0])
    drawdown = curve / peak - 1.0 if len(curve) else pd.Series([0.0])
    n_trades = trades[trades["strategy_leg"].eq("N")]
    return {
        "trade_count": int(len(trades)),
        "n_trade_count": int(len(n_trades)),
        "n_win_rate": float((n_trades["account_return"] > 0).mean()) if len(n_trades) else 0.0,
        "equity_multiple": float(curve.iloc[-1]) if len(curve) else 1.0,
        "max_drawdown": float(drawdown.min()),
        "ulcer_index": float(np.sqrt(np.mean(np.square(drawdown)))) if len(drawdown) else 0.0,
    }
