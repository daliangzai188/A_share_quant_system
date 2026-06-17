"""
Search supplementary strategies for A+B+C+D.

The search only considers dates where the current A/B/C/D portfolio does not
use capital.  A candidate expansion must have average account return above 1%
under the 80% position sizing convention.
"""
from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]

START_DATE = "20240520"
END_DATE = "20260514"
INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.8
D_FILL_RATE = 0.8
MIN_ADDED_AVG_ACCOUNT_RETURN = 0.01
MIN_ADDED_TRADES = 15
SELECTED_E2_SOURCE = "full_exclude_st_top500"
SELECTED_E2_CONDITIONS = "segment_retreat_state_bucket=neutral"
SELECTED_E2_SORT_RULE = "circ_mv_asc"

OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_expansion"

ABC_DETAIL_PATH = (
    PROJECT_ROOT
    / "reports"
    / "paper_trade"
    / "backup_strategy_c"
    / "current_config_c_exit_refine_exit5_20240520_20260514_481d_best_abc_detail.csv"
)
D_TRADES_PATH = PROJECT_ROOT / "reports" / "strategy_d" / "d_trades.csv"

SOURCE_FILES = [
    (
        "full_exclude_st_top500",
        PROJECT_ROOT / "reports" / "recent_2y_full_strategy_exclude_st_exclude_amount_ratio_top500_detail.csv",
    ),
    (
        "full_optimization",
        PROJECT_ROOT / "reports" / "recent_2y_full_strategy_optimization_detail.csv",
    ),
    (
        "realistic_condition",
        PROJECT_ROOT / "reports" / "recent_2y_realistic_condition_search_detail.csv",
    ),
]

FACTOR_COLUMNS = [
    "market_segment",
    "limit_pct_bucket",
    "market_sentiment_level",
    "segment_market_sentiment_level",
    "market_emotion_state_bucket",
    "segment_emotion_state_bucket",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_limit_max_height_bucket",
    "market_leader_rank_bucket",
    "segment_market_leader_rank_bucket",
    "limit_height_rank_bucket",
    "segment_limit_height_rank_bucket",
    "first_time_bucket",
    "first_time_detail_bucket",
    "limit_times_bucket",
    "limit_times_detail_bucket",
    "open_times_bucket",
    "amount_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "pct_chg_bucket",
    "prev_pct_chg_bucket",
    "amount_ratio_bucket",
    "limit_up_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
    "board_type",
]

SORT_RULES = [
    ("fill_probability", False, "fill_probability_desc"),
    ("amount", False, "amount_desc"),
    ("turnover_rate", False, "turnover_desc"),
    ("volume_ratio", False, "volume_ratio_desc"),
    ("fd_amount_to_circ_mv", False, "fd_ratio_desc"),
    ("circ_mv", True, "circ_mv_asc"),
    ("market_leader_rank", True, "leader_rank_asc"),
]


def load_current_abcd() -> tuple[pd.DataFrame, set[str]]:
    abc = pd.read_csv(ABC_DETAIL_PATH, low_memory=False)
    abc = abc[abc["scenario"].astype(str) == "A_plus_B_plus_C_refined"].copy()
    abc["signal_date"] = abc["signal_date"].astype(str)
    abc = abc.sort_values("signal_date").drop_duplicates("signal_date", keep="last")

    d = pd.read_csv(D_TRADES_PATH, dtype={"signal_date": str}, low_memory=False)
    d_returns = d.set_index("signal_date")["account_return"].to_dict()

    rows: list[dict[str, Any]] = []
    for _, row in abc.sort_values("signal_date").iterrows():
        signal_date = str(row["signal_date"])
        abc_return = to_float(row.get("account_return", 0.0))
        d_return = to_float(d_returns.get(signal_date, 0.0)) * D_FILL_RATE
        rows.append(
            {
                "date": signal_date,
                "abc_return": abc_return,
                "d_return": d_return,
                "base_return": (1 + abc_return) * (1 + d_return) - 1,
                "operation_status": row.get("operation_status", ""),
            }
        )
    base = pd.DataFrame(rows)

    abc_busy = set(
        abc.loc[
            abc["operation_status"].astype(str).isin(["HISTORICAL_SIM_FILLED", "POSITION_OCCUPIED_SKIP"]),
            "signal_date",
        ]
    )
    d_busy = set(d["signal_date"].astype(str))
    free_dates = set(abc["signal_date"]) - abc_busy - d_busy
    return base, free_dates


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_source_candidates(path: Path, free_dates: set[str]) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[(data["trade_date"] >= START_DATE) & (data["trade_date"] <= END_DATE)].copy()
    data = data.drop_duplicates(["trade_date", "ts_code"]).copy()
    data = data[data["trade_date"].isin(free_dates)].copy()

    for column in [
        "net_return",
        "dynamic_net_return",
        "dynamic_account_return",
        "amount",
        "turnover_rate",
        "volume_ratio",
        "circ_mv",
        "fd_amount_to_circ_mv",
        "market_leader_rank",
    ]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    data = data[data["net_return"].notna()].copy()
    if "is_st" in data.columns:
        data = data[~data["is_st"].astype(str).str.lower().isin(["true", "1"])].copy()
    if "allow_buy_reliable" in data.columns:
        data = data[data["allow_buy_reliable"].astype(str).str.lower().isin(["true", "1"])].copy()
    if "is_fill_score_reliable" in data.columns:
        data = data[data["is_fill_score_reliable"].astype(str).str.lower().isin(["true", "1"])].copy()
    if "buy_executed" in data.columns:
        data = data[data["buy_executed"].astype(str).str.lower().isin(["true", "1"])].copy()
    if "sell_executed" in data.columns:
        data = data[data["sell_executed"].astype(str).str.lower().isin(["true", "1"])].copy()
    return data


def max_consecutive_losses(returns: pd.Series) -> int:
    max_count = 0
    current = 0
    for value in returns:
        if value <= 0:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def equity_stats(returns: pd.Series) -> dict[str, Any]:
    equity = INITIAL_EQUITY * (1 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1
    return {
        "final_equity": float(equity.iloc[-1]) if len(equity) else INITIAL_EQUITY,
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def build_condition_pool(data: pd.DataFrame) -> list[tuple[tuple[str, str], ...]]:
    singles: list[tuple[tuple[str, str], ...]] = []
    scored: list[tuple[float, tuple[tuple[str, str], ...]]] = []
    for column in FACTOR_COLUMNS:
        if column not in data.columns:
            continue
        counts = data[column].astype(str).value_counts()
        for value, count in counts.items():
            if value in {"nan", "None", "unknown", ""} or count < MIN_ADDED_TRADES:
                continue
            condition = ((column, value),)
            sample = data[data[column].astype(str) == value]
            avg = float((sample["net_return"] * POSITION_PCT).mean())
            if avg > MIN_ADDED_AVG_ACCOUNT_RETURN:
                singles.append(condition)
                scored.append((avg, condition))

    top_singles = [condition for _, condition in sorted(scored, reverse=True)[:80]]
    conditions = list(top_singles)
    for left, right in combinations(top_singles[:50], 2):
        if left[0][0] != right[0][0]:
            conditions.append(left + right)

    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[tuple[tuple[str, str], ...]] = []
    for condition in conditions:
        key = tuple(sorted(condition))
        if key not in seen:
            seen.add(key)
            unique.append(condition)
    return unique


def apply_conditions(data: pd.DataFrame, conditions: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    result = data
    for column, value in conditions:
        result = result[result[column].astype(str) == str(value)]
        if result.empty:
            return result
    return result


def evaluate_expansion(
    base: pd.DataFrame,
    data: pd.DataFrame,
    conditions: tuple[tuple[str, str], ...],
    sort_column: str,
    ascending: bool,
) -> tuple[dict[str, Any] | None, pd.DataFrame | None]:
    sample = apply_conditions(data, conditions)
    if len(sample) < MIN_ADDED_TRADES:
        return None, None
    if sort_column not in sample.columns:
        return None, None
    daily = (
        sample.sort_values(["trade_date", sort_column], ascending=[True, ascending])
        .groupby("trade_date", as_index=False)
        .head(1)
        .copy()
    )
    if len(daily) < MIN_ADDED_TRADES:
        return None, None

    daily["account_return_fixed"] = daily["net_return"] * POSITION_PCT
    daily["account_return_dynamic"] = daily.get("dynamic_account_return", daily["account_return_fixed"])
    avg_fixed = float(daily["account_return_fixed"].mean())
    if avg_fixed <= MIN_ADDED_AVG_ACCOUNT_RETURN:
        return None, None

    add_returns = daily.set_index("trade_date")["account_return_fixed"].to_dict()
    combined = base.copy()
    combined["expansion_return"] = combined["date"].map(add_returns).fillna(0.0)
    combined["combined_return"] = (1 + combined["base_return"]) * (1 + combined["expansion_return"]) - 1
    stats = equity_stats(combined["combined_return"])
    expansion_returns = daily["account_return_fixed"]
    row = {
        "conditions": ";".join(f"{column}={value}" for column, value in conditions),
        "condition_count": len(conditions),
        "sort_rule": f"{sort_column}_{'asc' if ascending else 'desc'}",
        "added_trades": int(len(daily)),
        "added_dates": int(daily["trade_date"].nunique()),
        "added_avg_account_return": avg_fixed,
        "added_avg_dynamic_account_return": float(pd.to_numeric(daily["account_return_dynamic"], errors="coerce").mean()),
        "added_median_account_return": float(expansion_returns.median()),
        "added_win_rate": float((expansion_returns > 0).mean()),
        "added_max_profit": float(expansion_returns.max()),
        "added_max_loss": float(expansion_returns.min()),
        "added_max_consecutive_losses": max_consecutive_losses(expansion_returns),
        **stats,
    }
    daily["expansion_account_return"] = daily["account_return_fixed"]
    return row, daily


def write_expansion_outputs(base: pd.DataFrame, trades: pd.DataFrame, stem: str) -> None:
    trades = trades.copy()
    trades["strategy_leg"] = "E2" if stem.endswith("selected_e2") else "E"
    trades.to_csv(OUTPUT_DIR / f"{stem}_trades.csv", index=False, encoding="utf-8-sig")

    add_returns = trades.set_index("trade_date")["expansion_account_return"].to_dict()
    combined = base.copy()
    combined["expansion_return"] = combined["date"].map(add_returns).fillna(0.0)
    combined["combined_return"] = (1 + combined["base_return"]) * (1 + combined["expansion_return"]) - 1
    combined["equity"] = INITIAL_EQUITY * (1 + combined["combined_return"]).cumprod()
    combined["peak_equity"] = combined["equity"].cummax()
    combined["drawdown"] = combined["equity"] / combined["peak_equity"] - 1
    combined.to_csv(OUTPUT_DIR / f"{stem}_equity_curve.csv", index=False, encoding="utf-8-sig")

    rows = []
    prev_equity = INITIAL_EQUITY
    combined["year"] = combined["date"].astype(str).str[:4]
    for year, group in combined.groupby("year"):
        last_equity = float(group["equity"].iloc[-1])
        rows.append(
            {
                "year": year,
                "year_return": last_equity / prev_equity - 1,
                "active_days": int((group["combined_return"] != 0).sum()),
                "expansion_trades": int((group["expansion_return"] != 0).sum()),
            }
        )
        prev_equity = last_equity
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / f"{stem}_yearly.csv", index=False, encoding="utf-8-sig")


def write_best_outputs(base: pd.DataFrame, summary: pd.DataFrame, details: dict[tuple[str, str, str], pd.DataFrame]) -> None:
    best = summary.iloc[0]
    key = (str(best["source"]), str(best["conditions"]), str(best["sort_rule"]))
    trades = details[key].copy()
    write_expansion_outputs(base, trades, "abcd_expansion_top_e")

    selected_key = (SELECTED_E2_SOURCE, SELECTED_E2_CONDITIONS, SELECTED_E2_SORT_RULE)
    if selected_key in details:
        write_expansion_outputs(base, details[selected_key].copy(), "abcd_expansion_selected_e2")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base, free_dates = load_current_abcd()
    summaries: list[dict[str, Any]] = []
    details: dict[tuple[str, str, str], pd.DataFrame] = {}

    for source_name, path in SOURCE_FILES:
        if not path.exists():
            continue
        data = load_source_candidates(path, free_dates)
        print(f"{source_name}: candidates={len(data)} dates={data['trade_date'].nunique()}")
        for conditions in build_condition_pool(data):
            for sort_column, ascending, _label in SORT_RULES:
                row, trades = evaluate_expansion(base, data, conditions, sort_column, ascending)
                if row is None or trades is None:
                    continue
                row["source"] = source_name
                summaries.append(row)
                details[(source_name, row["conditions"], row["sort_rule"])] = trades

    if not summaries:
        raise RuntimeError("没有找到平均每笔账户收益大于 1% 的新增策略。")

    summary = pd.DataFrame(summaries)
    summary = summary.sort_values(
        ["equity_multiple", "added_trades", "added_avg_account_return"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    summary.to_csv(OUTPUT_DIR / "abcd_expansion_search_summary.csv", index=False, encoding="utf-8-sig")
    write_best_outputs(base, summary, details)
    print(summary.head(20).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
