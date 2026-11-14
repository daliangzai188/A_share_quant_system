"""
近两年首板风格研究：北京炒家 / 创世纪888公开风格的量化近似。

说明：
1. 本脚本不判断个人真实交易，只把公开常见描述抽象成可回测条件。
2. 只读取本地 next_day_premium_trades.csv，不调用外部接口，不接实盘。
3. 执行口径是首板当日涨停价打板，次日开盘卖出；net_return 已含费用。
4. 所有候选必须通过成交概率可靠性过滤，避免把买不到的一字板算进收益。

输出：
  reports/first_board_style_research/data_audit.csv
  reports/first_board_style_research/style_baselines.csv
  reports/first_board_style_research/search_summary.csv
  reports/first_board_style_research/search_yearly.csv
  reports/first_board_style_research/top_trades.csv
"""

from __future__ import annotations

import argparse
from itertools import combinations
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


INPUT_PATH = PROJECT_ROOT / "data" / "processed" / "next_day_premium_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "first_board_style_research"

INITIAL_CASH = 500_000.0
POSITION_PCT = 0.8
ABCD_EQUITY_MULTIPLE = 408.3792
DEFAULT_ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj", "other"}

FACTOR_COLUMNS = [
    "market_segment",
    "board_type",
    "first_time_bucket",
    "market_sentiment_level",
    "segment_market_sentiment_level",
    "open_times_bucket",
    "fill_probability_bucket",
    "amount_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "segment_limit_up_count_bucket",
    "limit_up_count_bucket",
]

SORT_RULES = [
    ("fill_probability", False, "fill_probability_desc"),
    ("fd_amount_to_circ_mv", False, "fd_ratio_desc"),
    ("amount", False, "amount_desc"),
    ("volume_ratio", False, "volume_ratio_desc"),
    ("turnover_rate", False, "turnover_rate_desc"),
    ("open_times", True, "open_times_asc"),
    ("first_time_minutes", True, "first_time_early"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究近两年首板风格是否优于 A+B+C+D。")
    parser.add_argument("--start-date", default="20240612", help="开始日期，默认最近两年窗口。")
    parser.add_argument("--end-date", default="20260611", help="结束日期。")
    parser.add_argument("--min-fill-probability", type=float, default=0.6, help="最低成交概率。")
    parser.add_argument("--min-trades", type=int, default=30, help="有效方案最少成交笔数。")
    parser.add_argument(
        "--allowed-segments",
        default="sh_main,sz_main,chi_next,star,bj,other",
        help="允许市场分段，逗号分隔。默认按当前主实盘口径包含科创和北交。",
    )
    parser.add_argument("--top-single", type=int, default=40, help="进入双因子扩展的单因子数量。")
    parser.add_argument("--top-pair", type=int, default=80, help="进入三因子扩展的双因子数量。")
    parser.add_argument("--max-combos", type=int, default=5000, help="最多评估条件组合数量。")
    parser.add_argument("--top-n", type=int, default=50, help="输出前 N 个方案。")
    return parser.parse_args()


def parse_time_to_minutes(value: object) -> float:
    if pd.isna(value):
        return float("nan")
    text = str(value).replace(":", "").replace(".", "")
    if not text or text.lower() == "nan":
        return float("nan")
    try:
        raw = int(float(text))
    except ValueError:
        return float("nan")
    if raw <= 0:
        return float("nan")
    hh = raw // 10000
    mm = (raw % 10000) // 100
    return float(hh * 60 + mm)


def bucket_numeric(series: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return pd.cut(numeric, bins=bins, labels=labels, include_lowest=True).astype(str).replace("nan", "missing")


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: list[float]) -> int:
    max_losses = 0
    current = 0
    for ret in returns:
        if ret <= 0:
            current += 1
            max_losses = max(max_losses, current)
        else:
            current = 0
    return max_losses


def profit_loss_ratio(returns: pd.Series) -> float:
    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns < 0].sum()))
    return gross_profit / gross_loss if gross_loss else 0.0


def normalize_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def parse_segments(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def load_candidates(
    start_date: str,
    end_date: str,
    min_fill_probability: float,
    allowed_segments: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not INPUT_PATH.exists():
        raise RuntimeError(f"缺少输入文件: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH, low_memory=False)
    df["trade_date"] = df["trade_date"].astype(str)
    df["first_time_minutes"] = df["first_time"].apply(parse_time_to_minutes)
    df["is_st_name"] = df["name"].fillna("").astype(str).str.upper().str.contains("ST")

    audit_rows = [
        {"item": "raw_rows", "value": len(df)},
        {"item": "raw_start_date", "value": df["trade_date"].min()},
        {"item": "raw_end_date", "value": df["trade_date"].max()},
        {"item": "raw_trade_days", "value": df["trade_date"].nunique()},
    ]

    recent = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)].copy()
    first_board = recent[
        (pd.to_numeric(recent["limit_times"], errors="coerce") == 1)
        & (~recent["is_st_name"])
        & (recent["net_return"].notna())
        & (recent["next_trade_date"].notna())
        & (recent["exit_trade_date"].notna())
    ].copy()

    reliable = first_board[
        (pd.to_numeric(first_board["fill_probability"], errors="coerce") >= min_fill_probability)
        & normalize_bool(first_board["is_fill_score_reliable"])
        & (first_board["board_type"].astype(str) != "one_word")
        & (first_board["market_segment"].isin(allowed_segments or DEFAULT_ALLOWED_SEGMENTS))
    ].copy()

    audit_rows.extend(
        [
            {"item": "recent_rows", "value": len(recent)},
            {"item": "recent_trade_days", "value": recent["trade_date"].nunique()},
            {"item": "first_board_rows", "value": len(first_board)},
            {"item": "first_board_trade_days", "value": first_board["trade_date"].nunique()},
            {"item": "executable_first_board_rows", "value": len(reliable)},
            {"item": "executable_first_board_trade_days", "value": reliable["trade_date"].nunique()},
            {"item": "one_word_first_board_rows", "value": int((first_board["board_type"] == "one_word").sum())},
            {"item": "min_fill_probability", "value": min_fill_probability},
            {"item": "allowed_market_segments", "value": ",".join(sorted(allowed_segments))},
        ]
    )

    reliable["open_times_bucket"] = bucket_numeric(
        reliable["open_times"], [-0.1, 0.1, 1.0, 2.0, 3.0, 99], ["0", "1", "2", "3", "gte_4"]
    )
    reliable["fill_probability_bucket"] = bucket_numeric(
        reliable["fill_probability"], [0, 0.6, 0.8, 0.95, 1.01], ["lt_60", "60_80", "80_95", "gte_95"]
    )
    reliable["amount_bucket"] = bucket_numeric(
        reliable["amount"], [0, 100000, 300000, 800000, 2_000_000, 99_999_999], ["lt_1e8", "1e8_3e8", "3e8_8e8", "8e8_20e8", "gte_20e8"]
    )
    reliable["turnover_rate_bucket"] = bucket_numeric(
        reliable["turnover_rate"], [0, 3, 8, 15, 30, 100], ["lt_3", "3_8", "8_15", "15_30", "gte_30"]
    )
    reliable["volume_ratio_bucket"] = bucket_numeric(
        reliable["volume_ratio"], [0, 1.5, 3, 6, 10, 999], ["lt_1_5", "1_5_3", "3_6", "6_10", "gte_10"]
    )
    reliable["fd_ratio_bucket"] = bucket_numeric(
        reliable["fd_amount_to_circ_mv"], [-1, 0, 0.002, 0.006, 0.015, 1], ["lte_0", "0_0_2", "0_2_0_6", "0_6_1_5", "gte_1_5"]
    )
    reliable["segment_limit_up_count_bucket"] = bucket_numeric(
        reliable["segment_limit_up_count"], [-1, 5, 15, 40, 80, 9999], ["lte_5", "5_15", "15_40", "40_80", "gt_80"]
    )
    reliable["limit_up_count_bucket"] = bucket_numeric(
        reliable["limit_up_count"], [-1, 50, 100, 150, 9999], ["lt_50", "50_100", "100_150", "gt_150"]
    )

    return reliable.reset_index(drop=True), pd.DataFrame(audit_rows)


def select_daily_top(candidates: pd.DataFrame, sort_col: str, ascending: bool) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    if sort_col not in candidates.columns:
        sort_col = "fill_probability"
    ordered = candidates.sort_values(["trade_date", sort_col, "amount"], ascending=[True, ascending, False])
    return ordered.groupby("trade_date", as_index=False).head(1).sort_values("trade_date").reset_index(drop=True)


def simulate(selected: pd.DataFrame, scenario: str, sort_rule: str) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = INITIAL_CASH
    occupied_until = ""
    trade_rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        buy_date = str(row.get("trade_date", ""))
        if occupied_until and buy_date <= occupied_until:
            continue
        net_return = row.get("net_return")
        if pd.isna(net_return):
            continue
        account_return = float(net_return) * POSITION_PCT
        before = equity
        equity = equity * (1.0 + account_return)
        occupied_until = str(row.get("exit_trade_date", ""))
        trade_rows.append(
            {
                "scenario": scenario,
                "sort_rule": sort_rule,
                "trade_date": row["trade_date"],
                "ts_code": row.get("ts_code", ""),
                "name": row.get("name", ""),
                "market_segment": row.get("market_segment", ""),
                "board_type": row.get("board_type", ""),
                "first_time_bucket": row.get("first_time_bucket", ""),
                "open_times": row.get("open_times", ""),
                "fill_probability": row.get("fill_probability", ""),
                "amount": row.get("amount", ""),
                "turnover_rate": row.get("turnover_rate", ""),
                "volume_ratio": row.get("volume_ratio", ""),
                "fd_amount_to_circ_mv": row.get("fd_amount_to_circ_mv", ""),
                "net_return": float(net_return),
                "account_return": account_return,
                "equity_before": before,
                "equity_after": equity,
                "exit_trade_date": row.get("exit_trade_date", ""),
            }
        )

    trades = pd.DataFrame(trade_rows)
    if trades.empty:
        return {}, trades
    returns = trades["account_return"].astype(float)
    summary = {
        "scenario": scenario,
        "sort_rule": sort_rule,
        "signal_days": int(selected["trade_date"].nunique()),
        "executed_trade_count": int(len(trades)),
        "final_equity": float(equity),
        "equity_multiple": float(equity / INITIAL_CASH),
        "beats_abcd": bool(equity / INITIAL_CASH > ABCD_EQUITY_MULTIPLE),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "total_account_return_sum": float(returns.sum()),
        "max_drawdown": max_drawdown(trades["equity_after"].astype(float)),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "profit_loss_ratio": profit_loss_ratio(returns),
        "max_consecutive_losses": max_consecutive_losses(returns.tolist()),
        "avg_fill_probability": float(pd.to_numeric(trades["fill_probability"], errors="coerce").mean()),
    }
    return summary, trades


def build_yearly(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    trades = trades.copy()
    trades["year"] = trades["trade_date"].astype(str).str[:4]
    for (scenario, sort_rule, year), group in trades.groupby(["scenario", "sort_rule", "year"]):
        returns = group["account_return"].astype(float)
        start_equity = float(group["equity_before"].iloc[0])
        end_equity = float(group["equity_after"].iloc[-1])
        rows.append(
            {
                "scenario": scenario,
                "sort_rule": sort_rule,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": end_equity / start_equity - 1.0 if start_equity else 0.0,
                "win_rate": float((returns > 0).mean()),
                "max_drawdown": max_drawdown(group["equity_after"].astype(float)),
                "avg_account_return": float(returns.mean()),
            }
        )
    return pd.DataFrame(rows)


def apply_conditions(df: pd.DataFrame, conditions: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    result = df
    for column, value in conditions:
        if column not in result.columns:
            return result.iloc[0:0].copy()
        result = result[result[column].fillna("missing").astype(str) == str(value)]
    return result.copy()


def condition_name(conditions: tuple[tuple[str, str], ...]) -> str:
    return ";".join(f"{column}={value}" for column, value in conditions)


def build_condition_candidates(df: pd.DataFrame, min_count: int) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    max_count = len(df) * 0.85
    for column in FACTOR_COLUMNS:
        if column not in df.columns:
            continue
        counts = df[column].fillna("missing").astype(str).value_counts()
        for value, count in counts.items():
            if value in {"missing", "nan", "None", "unknown"}:
                continue
            if count < min_count or count > max_count:
                continue
            result.append((column, value))
    return result


def evaluate_condition_set(
    df: pd.DataFrame,
    conditions: tuple[tuple[str, str], ...],
    min_trades: int,
) -> list[tuple[dict[str, Any], pd.DataFrame]]:
    matched = apply_conditions(df, conditions)
    if matched.empty:
        return []
    rows: list[tuple[dict[str, Any], pd.DataFrame]] = []
    for sort_col, ascending, sort_label in SORT_RULES:
        selected = select_daily_top(matched, sort_col, ascending)
        summary, trades = simulate(selected, condition_name(conditions), sort_label)
        if not summary or summary["executed_trade_count"] < min_trades:
            continue
        summary["condition_count"] = len(conditions)
        summary["matched_candidate_count"] = int(len(matched))
        rows.append((summary, trades))
    return rows


def build_style_baselines(df: pd.DataFrame, min_trades: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    styles = {
        "beijing_chaojia_like_executable_first_board": df[
            (df["board_type"].isin(["multi_open", "t_board"]))
            & (df["first_time_bucket"].isin(["early_morning", "midday", "afternoon"]))
            & (pd.to_numeric(df["open_times"], errors="coerce").between(1, 3))
            & (pd.to_numeric(df["amount"], errors="coerce") >= 100000)
        ],
        "chuangshiji888_like_strong_capacity_first_board": df[
            (df["board_type"].isin(["multi_open", "t_board"]))
            & (df["first_time_bucket"].isin(["early_morning", "midday"]))
            & (pd.to_numeric(df["amount"], errors="coerce") >= 300000)
            & (pd.to_numeric(df["volume_ratio"], errors="coerce") >= 3)
            & (pd.to_numeric(df["fd_amount_to_circ_mv"], errors="coerce") > 0)
        ],
    }

    summary_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    for name, sub in styles.items():
        for sort_col, ascending, sort_label in SORT_RULES:
            selected = select_daily_top(sub, sort_col, ascending)
            summary, trades = simulate(selected, name, sort_label)
            if not summary or summary["executed_trade_count"] < min_trades:
                continue
            summary_rows.append(summary)
            trade_frames.append(trades)
    summary = pd.DataFrame(summary_rows).sort_values(
        ["beats_abcd", "equity_multiple", "max_drawdown"], ascending=[False, False, True]
    )
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    return summary, trades


def run_search(df: pd.DataFrame, min_trades: int, top_single: int, top_pair: int, max_combos: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    min_count = max(min_trades, 40)
    condition_candidates = build_condition_candidates(df, min_count=min_count)
    scored: list[tuple[dict[str, Any], pd.DataFrame]] = []

    single_scores: list[tuple[tuple[tuple[str, str], ...], float]] = []
    for condition in condition_candidates:
        results = evaluate_condition_set(df, (condition,), min_trades)
        scored.extend(results)
        if results:
            single_scores.append(((condition,), max(item[0]["equity_multiple"] for item in results)))

    single_scores.sort(key=lambda item: item[1], reverse=True)
    top_single_conditions = [item[0][0] for item in single_scores[:top_single]]

    pair_scores: list[tuple[tuple[tuple[str, str], ...], float]] = []
    for pair in combinations(top_single_conditions, 2):
        if pair[0][0] == pair[1][0]:
            continue
        results = evaluate_condition_set(df, tuple(pair), min_trades)
        scored.extend(results)
        if results:
            pair_scores.append((tuple(pair), max(item[0]["equity_multiple"] for item in results)))

    pair_scores.sort(key=lambda item: item[1], reverse=True)
    top_pairs = [item[0] for item in pair_scores[:top_pair]]

    combo_count = 0
    seen: set[tuple[tuple[str, str], ...]] = set()
    for base_pair in top_pairs:
        used = {condition[0] for condition in base_pair}
        for condition in top_single_conditions:
            if condition[0] in used:
                continue
            combo = tuple(sorted((*base_pair, condition), key=lambda item: item[0]))
            if combo in seen:
                continue
            seen.add(combo)
            results = evaluate_condition_set(df, combo, min_trades)
            scored.extend(results)
            combo_count += 1
            if combo_count >= max_combos:
                break
        if combo_count >= max_combos:
            break

    if not scored:
        return pd.DataFrame(), pd.DataFrame()

    summary = pd.DataFrame([item[0] for item in scored]).sort_values(
        ["beats_abcd", "equity_multiple", "max_drawdown"], ascending=[False, False, True]
    ).reset_index(drop=True)
    top_keys = set(summary.head(50)[["scenario", "sort_rule"]].apply(tuple, axis=1))
    trades = pd.concat(
        [item[1] for item in scored if (item[0]["scenario"], item[0]["sort_rule"]) in top_keys],
        ignore_index=True,
    )
    return summary, trades


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    allowed_segments = parse_segments(args.allowed_segments)
    candidates, audit = load_candidates(
        args.start_date,
        args.end_date,
        args.min_fill_probability,
        allowed_segments,
    )
    baselines, baseline_trades = build_style_baselines(candidates, args.min_trades)
    search_summary, search_trades = run_search(
        candidates,
        min_trades=args.min_trades,
        top_single=args.top_single,
        top_pair=args.top_pair,
        max_combos=args.max_combos,
    )

    all_top_trades = pd.concat(
        [frame for frame in [baseline_trades, search_trades] if not frame.empty],
        ignore_index=True,
    ) if (not baseline_trades.empty or not search_trades.empty) else pd.DataFrame()

    audit.to_csv(OUTPUT_DIR / "data_audit.csv", index=False, encoding="utf-8-sig")
    baselines.to_csv(OUTPUT_DIR / "style_baselines.csv", index=False, encoding="utf-8-sig")
    search_summary.to_csv(OUTPUT_DIR / "search_summary.csv", index=False, encoding="utf-8-sig")
    build_yearly(all_top_trades).to_csv(OUTPUT_DIR / "search_yearly.csv", index=False, encoding="utf-8-sig")
    all_top_trades.to_csv(OUTPUT_DIR / "top_trades.csv", index=False, encoding="utf-8-sig")

    print("近两年首板风格研究完成")
    print(f"窗口: {args.start_date}-{args.end_date}")
    print(f"A+B+C+D 对照倍数: {ABCD_EQUITY_MULTIPLE:.4f}")
    print("\n数据体检:")
    print(audit.to_string(index=False))
    print("\n风格基准 TOP:")
    if baselines.empty:
        print("无满足最小成交笔数的风格基准。")
    else:
        print(
            baselines[
                [
                    "scenario",
                    "sort_rule",
                    "equity_multiple",
                    "executed_trade_count",
                    "win_rate",
                    "avg_account_return",
                    "median_account_return",
                    "max_drawdown",
                    "profit_loss_ratio",
                    "max_consecutive_losses",
                    "beats_abcd",
                ]
            ].head(10).to_string(index=False)
        )
    print("\n条件搜索 TOP:")
    if search_summary.empty:
        print("无满足最小成交笔数的搜索结果。")
    else:
        print(
            search_summary[
                [
                    "scenario",
                    "sort_rule",
                    "equity_multiple",
                    "executed_trade_count",
                    "win_rate",
                    "avg_account_return",
                    "median_account_return",
                    "max_drawdown",
                    "profit_loss_ratio",
                    "max_consecutive_losses",
                    "beats_abcd",
                ]
            ].head(20).to_string(index=False)
        )
    print("\n报告目录:")
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
