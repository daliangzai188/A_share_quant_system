"""
研究策略 S：创业板 20CM 专用补位策略。

口径：
  - 只研究 market_segment=chi_next 且 limit_pct_bucket=20cm 的候选。
  - 只在当前 A/B/C/D/E2 组合不占用资金的日期触发。
  - 每个信号日最多选 1 只，T+1 开盘买入，T+2 收盘卖出。
  - 同一资金不允许重叠占用，上一笔 S 未完成退出前跳过新信号。
  - 本脚本只做历史研究，不接入实盘，不生成真实委托。

输出：
  reports/strategy_s/chinext20/s_chinext20_search_summary.csv
  reports/strategy_s/chinext20/s_chinext20_best_trades.csv
  reports/strategy_s/chinext20/s_chinext20_best_equity_curve.csv
  reports/strategy_s/chinext20/s_chinext20_best_yearly.csv
  reports/strategy_s/chinext20/s_chinext20_report.md

用法：
  .venv/bin/python scripts/research_strategy_s_chinext20.py
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
MIN_TRADES = 10
MIN_AVG_ACCOUNT_RETURN = 0.006

OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_s" / "chinext20"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
CURRENT_COMBO_CURVE_PATH = (
    PROJECT_ROOT / "reports" / "strategy_expansion" / "abcd_expansion_selected_e2_equity_curve.csv"
)
CURRENT_E2_TRADES_PATH = (
    PROJECT_ROOT / "reports" / "strategy_expansion" / "abcd_expansion_selected_e2_trades.csv"
)

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
    ("segment_market_leader_rank", True, "segment_leader_rank_asc"),
]


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_date(value: Any) -> str:
    text = str(value).strip()
    return text.replace(".0", "") if text.endswith(".0") else text


def load_open_dates() -> list[str]:
    if not CALENDAR_PATH.exists():
        return []
    calendar = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    if "is_open" in calendar.columns:
        calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    return sorted(calendar["cal_date"].astype(str).tolist())


def next_trade_day(date_str: str, n: int, open_dates: list[str]) -> str:
    future = [date for date in open_dates if date > date_str]
    if len(future) >= n:
        return future[n - 1]
    return date_str


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


def equity_stats(returns: pd.Series, initial_equity: float = INITIAL_EQUITY) -> dict[str, Any]:
    if len(returns) == 0:
        return {
            "final_equity": initial_equity,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_consecutive_losses": 0,
        }
    equity = initial_equity * (1 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1
    return {
        "final_equity": float(equity.iloc[-1]),
        "equity_multiple": float(equity.iloc[-1] / initial_equity),
        "max_drawdown": float(drawdown.min()),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def load_current_combo_and_free_dates(open_dates: list[str]) -> tuple[pd.DataFrame, set[str]]:
    curve = pd.read_csv(CURRENT_COMBO_CURVE_PATH, dtype={"date": str})
    curve["date"] = curve["date"].map(clean_date)
    curve = curve[(curve["date"] >= START_DATE) & (curve["date"] <= END_DATE)].copy()
    curve["current_combo_return"] = pd.to_numeric(curve["combined_return"], errors="coerce").fillna(0.0)

    busy_dates = set(
        curve.loc[
            curve["operation_status"].astype(str).isin(["HISTORICAL_SIM_FILLED", "POSITION_OCCUPIED_SKIP"]),
            "date",
        ]
    )
    busy_dates |= set(curve.loc[curve["current_combo_return"].abs() > 0, "date"])
    if "d_return" in curve.columns:
        busy_dates |= set(curve.loc[pd.to_numeric(curve["d_return"], errors="coerce").fillna(0).abs() > 0, "date"])
    if "expansion_return" in curve.columns:
        busy_dates |= set(
            curve.loc[pd.to_numeric(curve["expansion_return"], errors="coerce").fillna(0).abs() > 0, "date"]
        )

    if CURRENT_E2_TRADES_PATH.exists():
        e2 = pd.read_csv(CURRENT_E2_TRADES_PATH, low_memory=False)
        e2["trade_date"] = e2["trade_date"].map(clean_date)
        for _, row in e2.iterrows():
            signal_date = clean_date(row.get("trade_date", ""))
            exit_date = clean_date(row.get("exit_trade_date", next_trade_day(signal_date, 2, open_dates)))
            busy_dates.add(signal_date)
            for date in open_dates:
                if signal_date < date <= exit_date:
                    busy_dates.add(date)

    all_dates = set(curve["date"])
    free_dates = all_dates - busy_dates
    base = curve[["date", "current_combo_return"]].copy()
    return base, free_dates


def is_true_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "1.0"])


def load_source_candidates(path: Path, free_dates: set[str]) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data["trade_date"] = data["trade_date"].map(clean_date)
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
        "fill_probability",
        "market_leader_rank",
        "segment_market_leader_rank",
    ]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    data = data[data["net_return"].notna()].copy()
    data = data[data["market_segment"].astype(str) == "chi_next"].copy()
    if "limit_pct_bucket" in data.columns:
        data = data[data["limit_pct_bucket"].astype(str) == "20cm"].copy()
    elif "limit_pct" in data.columns:
        data = data[pd.to_numeric(data["limit_pct"], errors="coerce").between(0.195, 0.205)].copy()

    if "is_st" in data.columns:
        data = data[~is_true_series(data["is_st"])].copy()
    if "limit_data_quality" in data.columns:
        data = data[data["limit_data_quality"].astype(str).isin(["full", "nan"])].copy()
    if "strategy_compatible" in data.columns:
        data = data[is_true_series(data["strategy_compatible"])].copy()
    if "allow_buy_reliable" in data.columns:
        data = data[is_true_series(data["allow_buy_reliable"])].copy()
    if "is_fill_score_reliable" in data.columns:
        data = data[is_true_series(data["is_fill_score_reliable"])].copy()
    if "buy_executed" in data.columns:
        data = data[is_true_series(data["buy_executed"])].copy()
    if "sell_executed" in data.columns:
        data = data[is_true_series(data["sell_executed"])].copy()
    return data


def build_condition_pool(data: pd.DataFrame) -> list[tuple[tuple[str, str], ...]]:
    scored: list[tuple[float, tuple[tuple[str, str], ...]]] = []
    for column in FACTOR_COLUMNS:
        if column not in data.columns:
            continue
        for value, count in data[column].astype(str).value_counts().items():
            if value in {"nan", "None", "unknown", ""} or count < MIN_TRADES:
                continue
            sample = data[data[column].astype(str) == value]
            avg = float((sample["net_return"] * POSITION_PCT).mean())
            if avg >= MIN_AVG_ACCOUNT_RETURN:
                scored.append((avg, ((column, value),)))

    top_singles = [condition for _, condition in sorted(scored, reverse=True)[:100]]
    conditions: list[tuple[tuple[str, str], ...]] = list(top_singles)
    for left, right in combinations(top_singles[:70], 2):
        if left[0][0] != right[0][0]:
            conditions.append(left + right)
    for first, second, third in combinations(top_singles[:28], 3):
        columns = {first[0][0], second[0][0], third[0][0]}
        if len(columns) == 3:
            conditions.append(first + second + third)

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


def select_non_overlapping_daily(
    sample: pd.DataFrame,
    sort_column: str,
    ascending: bool,
    open_dates: list[str],
) -> pd.DataFrame:
    ordered = sample.sort_values(["trade_date", sort_column], ascending=[True, ascending]).copy()
    selected_rows: list[pd.Series] = []
    occupied_until = ""
    for trade_date, group in ordered.groupby("trade_date", sort=True):
        trade_date = clean_date(trade_date)
        if occupied_until and trade_date <= occupied_until:
            continue
        row = group.iloc[0].copy()
        exit_date = clean_date(row.get("exit_trade_date", next_trade_day(trade_date, 2, open_dates)))
        if not exit_date or exit_date == "nan":
            exit_date = next_trade_day(trade_date, 2, open_dates)
        row["s_signal_date"] = trade_date
        row["s_exit_date"] = exit_date
        selected_rows.append(row)
        occupied_until = exit_date
    if not selected_rows:
        return pd.DataFrame(columns=sample.columns)
    return pd.DataFrame(selected_rows)


def evaluate_strategy(
    base: pd.DataFrame,
    data: pd.DataFrame,
    conditions: tuple[tuple[str, str], ...],
    sort_column: str,
    ascending: bool,
    open_dates: list[str],
) -> tuple[dict[str, Any] | None, pd.DataFrame | None, pd.DataFrame | None]:
    if sort_column not in data.columns:
        return None, None, None
    sample = apply_conditions(data, conditions)
    if len(sample) < MIN_TRADES:
        return None, None, None

    daily = select_non_overlapping_daily(sample, sort_column, ascending, open_dates)
    if len(daily) < MIN_TRADES:
        return None, None, None

    daily["s_account_return"] = pd.to_numeric(daily["net_return"], errors="coerce") * POSITION_PCT
    daily = daily[daily["s_account_return"].notna()].copy()
    if len(daily) < MIN_TRADES:
        return None, None, None

    avg_return = float(daily["s_account_return"].mean())
    if avg_return < MIN_AVG_ACCOUNT_RETURN:
        return None, None, None

    s_stats = equity_stats(daily["s_account_return"])
    add_returns = daily.set_index("trade_date")["s_account_return"].to_dict()
    combined = base.copy()
    combined["s_return"] = combined["date"].map(add_returns).fillna(0.0)
    combined["combined_with_s_return"] = (
        (1 + combined["current_combo_return"]) * (1 + combined["s_return"]) - 1
    )
    combined_stats = equity_stats(combined["combined_with_s_return"])

    returns = daily["s_account_return"]
    row = {
        "conditions": ";".join(f"{column}={value}" for column, value in conditions),
        "condition_count": len(conditions),
        "sort_rule": f"{sort_column}_{'asc' if ascending else 'desc'}",
        "s_trades": int(len(daily)),
        "s_dates": int(daily["trade_date"].nunique()),
        "s_avg_account_return": avg_return,
        "s_median_account_return": float(returns.median()),
        "s_win_rate": float((returns > 0).mean()),
        "s_max_profit": float(returns.max()),
        "s_max_loss": float(returns.min()),
        "s_max_consecutive_losses": max_consecutive_losses(returns),
        "s_final_equity": s_stats["final_equity"],
        "s_equity_multiple": s_stats["equity_multiple"],
        "s_max_drawdown": s_stats["max_drawdown"],
        "combo_final_equity": combined_stats["final_equity"],
        "combo_equity_multiple": combined_stats["equity_multiple"],
        "combo_max_drawdown": combined_stats["max_drawdown"],
        "combo_max_consecutive_losses": combined_stats["max_consecutive_losses"],
    }
    daily["strategy_leg"] = "S"
    return row, daily, combined


def write_yearly(combined: pd.DataFrame, trades: pd.DataFrame) -> None:
    rows: list[dict[str, Any]] = []
    prev_combo_equity = INITIAL_EQUITY
    prev_s_equity = INITIAL_EQUITY
    combined = combined.copy()
    combined["year"] = combined["date"].astype(str).str[:4]
    trade_returns = trades.copy()
    trade_returns["year"] = trade_returns["trade_date"].astype(str).str[:4]
    for year, group in combined.groupby("year"):
        combo_equity = prev_combo_equity * (1 + group["combined_with_s_return"]).prod()
        year_trades = trade_returns[trade_returns["year"] == year]
        s_equity = prev_s_equity * (1 + year_trades["s_account_return"]).prod()
        rows.append(
            {
                "year": year,
                "combo_year_return": combo_equity / prev_combo_equity - 1,
                "s_year_return": s_equity / prev_s_equity - 1,
                "combo_active_days": int((group["combined_with_s_return"] != 0).sum()),
                "s_trades": int(len(year_trades)),
            }
        )
        prev_combo_equity = combo_equity
        prev_s_equity = s_equity
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / "s_chinext20_best_yearly.csv", index=False, encoding="utf-8-sig")


def write_report(summary: pd.DataFrame, best_trades: pd.DataFrame, best_combined: pd.DataFrame) -> None:
    best = summary.iloc[0]
    current_combo_stats = equity_stats(best_combined["current_combo_return"])
    best_yearly_path = OUTPUT_DIR / "s_chinext20_best_yearly.csv"
    yearly = pd.read_csv(best_yearly_path) if best_yearly_path.exists() else pd.DataFrame()
    reached_10x = "是" if float(best["s_equity_multiple"]) >= 10 else "否"
    improved_combo = "是" if float(best["combo_equity_multiple"]) > current_combo_stats["equity_multiple"] else "否"

    lines = [
        "# S策略创业板20CM研究报告",
        "",
        "## 研究口径",
        f"- 时间范围：{START_DATE} 至 {END_DATE}",
        "- 股票范围：仅创业板，且涨跌幅制度为20CM的涨停候选。",
        "- 触发位置：仅当前 A/B/C/D/E2 组合空闲时补位。",
        "- 交易口径：T日收盘信号，T+1开盘买入，T+2收盘卖出；同一资金不重叠占用。",
        "- 本报告只是历史模拟研究，不代表可以直接实盘。",
        "",
        "## 最优结果",
        f"- 数据源：{best['source']}",
        f"- 条件：{best['conditions']}",
        f"- 排序：{best['sort_rule']}",
        f"- S交易数：{int(best['s_trades'])}",
        f"- S胜率：{float(best['s_win_rate']):.2%}",
        f"- S平均账户收益：{float(best['s_avg_account_return']):.2%}",
        f"- S中位数账户收益：{float(best['s_median_account_return']):.2%}",
        f"- S最大单笔盈利：{float(best['s_max_profit']):.2%}",
        f"- S最大单笔亏损：{float(best['s_max_loss']):.2%}",
        f"- S最大连续亏损次数：{int(best['s_max_consecutive_losses'])}",
        f"- S独立复利倍数：{float(best['s_equity_multiple']):.2f}x",
        f"- S最大回撤：{float(best['s_max_drawdown']):.2%}",
        f"- 是否达到10x：{reached_10x}",
        "",
        "## 加入当前组合后的结果",
        f"- 当前ABCDE2组合复利倍数：{current_combo_stats['equity_multiple']:.2f}x",
        f"- 加入S后组合复利倍数：{float(best['combo_equity_multiple']):.2f}x",
        f"- 加入S后最大回撤：{float(best['combo_max_drawdown']):.2%}",
        f"- 是否改善组合复利：{improved_combo}",
        "",
        "## 年度拆分",
    ]
    if not yearly.empty:
        lines.append(yearly.to_markdown(index=False))
    lines += [
        "",
        "## 风险结论",
        "- 该研究仍使用历史日线和成交模型结果，尚未完成真实QMT逐笔委托验证。",
        "- 创业板20CM波动更大，最大单笔亏损和连续亏损必须单独纳入实盘风控。",
        "- 若后续要接入实盘，必须再做滑点、手续费、5万元单笔上限、账户余额、涨停买不到、跌停卖不出和小资金模拟认证。",
    ]
    (OUTPUT_DIR / "s_chinext20_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    open_dates = load_open_dates()
    base, free_dates = load_current_combo_and_free_dates(open_dates)
    summaries: list[dict[str, Any]] = []
    details: dict[tuple[str, str, str], tuple[pd.DataFrame, pd.DataFrame]] = {}

    for source_name, path in SOURCE_FILES:
        if not path.exists():
            continue
        data = load_source_candidates(path, free_dates)
        print(f"{source_name}: 创业板20CM候选={len(data)} 日期={data['trade_date'].nunique()}")
        if data.empty:
            continue
        conditions = build_condition_pool(data)
        print(f"{source_name}: 搜索条件={len(conditions)}")
        for condition in conditions:
            for sort_column, ascending, _ in SORT_RULES:
                row, trades, combined = evaluate_strategy(base, data, condition, sort_column, ascending, open_dates)
                if row is None or trades is None or combined is None:
                    continue
                row["source"] = source_name
                summaries.append(row)
                details[(source_name, row["conditions"], row["sort_rule"])] = (trades, combined)

    if not summaries:
        raise RuntimeError("没有找到满足最小样本和平均收益阈值的创业板20CM补位策略。")

    summary = pd.DataFrame(summaries).sort_values(
        ["s_equity_multiple", "combo_equity_multiple", "s_max_drawdown", "s_trades"],
        ascending=[False, False, False, False],
    )
    summary = summary.reset_index(drop=True)
    summary.to_csv(OUTPUT_DIR / "s_chinext20_search_summary.csv", index=False, encoding="utf-8-sig")

    best = summary.iloc[0]
    key = (str(best["source"]), str(best["conditions"]), str(best["sort_rule"]))
    best_trades, best_combined = details[key]
    best_trades.to_csv(OUTPUT_DIR / "s_chinext20_best_trades.csv", index=False, encoding="utf-8-sig")
    best_combined["equity"] = INITIAL_EQUITY * (1 + best_combined["combined_with_s_return"]).cumprod()
    best_combined["peak_equity"] = best_combined["equity"].cummax()
    best_combined["drawdown"] = best_combined["equity"] / best_combined["peak_equity"] - 1
    best_combined.to_csv(OUTPUT_DIR / "s_chinext20_best_equity_curve.csv", index=False, encoding="utf-8-sig")
    write_yearly(best_combined, best_trades)
    write_report(summary, best_trades, best_combined)

    print(summary.head(20).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
