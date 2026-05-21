from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


FACTOR_COLUMNS = [
    "market_segment",
    "limit_pct_bucket",
    "segment_market_sentiment_level",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "segment_market_leader_rank_bucket",
    "segment_limit_height_rank_bucket",
    "segment_retreat_state_bucket",
    "first_time_detail_bucket",
    "open_times_bucket",
    "volume_ratio_bucket",
    "amount_ratio_bucket",
    "turnover_rate_bucket",
    "fd_ratio_bucket",
    "pct_chg_bucket",
]

MANUAL_CANDIDATES = [
    ("market_segment", "bj"),
    ("market_segment", "star"),
    ("limit_pct_bucket", "30cm"),
    ("segment_retreat_state_bucket", "weak_below_3"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于分板块诊断结果生成少量不开仓过滤候选并验证。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--input-report", default="reports/trade_replay_report.csv", help="当前 A 保守成交回放明细。")
    parser.add_argument("--replay-rule", default="fixed_t2_close", help="要优化的回放规则。")
    parser.add_argument("--output-report", default="reports/diagnostic_filter_optimization.csv")
    parser.add_argument("--output-yearly", default="reports/diagnostic_filter_optimization_yearly.csv")
    parser.add_argument("--min-excluded-samples", type=int, default=5)
    parser.add_argument("--min-remaining-samples", type=int, default=350)
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--max-filter-count", type=int, default=3)
    parser.add_argument("--target-return", type=float, default=301.8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    logger = get_logger("diagnostic_filter_optimizer")

    selected = load_selected_trades(args.input_report, args.replay_rule)
    executed = select_executed(selected)
    candidates = build_filter_candidates(
        selected=selected,
        executed=executed,
        min_excluded_samples=args.min_excluded_samples,
        max_candidates=args.max_candidates,
    )
    logger.info("诊断过滤候选数量: %s", len(candidates))

    summary_rows = [summarize("base_no_filter", selected, executed, [], args.target_return)]
    yearly_rows = build_yearly_rows("base_no_filter", executed)
    for count in range(1, args.max_filter_count + 1):
        for combo in combinations(candidates, count):
            filtered_selected = apply_exclusions(selected, combo)
            filtered_executed = select_executed(filtered_selected)
            if len(filtered_executed) < args.min_remaining_samples:
                continue
            scenario = "exclude_" + ";".join(f"{column}={value}" for column, value in combo)
            summary_rows.append(summarize(scenario, filtered_selected, filtered_executed, combo, args.target_return))
            yearly_rows.extend(build_yearly_rows(scenario, filtered_executed))

    report = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_rows)
    report = report.sort_values(
        ["total_compound_return", "max_drawdown", "sample_count"],
        ascending=[False, True, False],
    )

    output_report = PROJECT_ROOT / args.output_report
    output_yearly = PROJECT_ROOT / args.output_yearly
    output_report.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_report, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    logger.info("诊断过滤优化报告已生成: %s, 行数: %s", output_report, len(report))
    logger.info("诊断过滤年度报告已生成: %s, 行数: %s", output_yearly, len(yearly))
    print("诊断过滤优化完成：")
    print(f"- report: {output_report}")
    print(f"- yearly: {output_yearly}")


def load_selected_trades(input_report: str, replay_rule: str) -> pd.DataFrame:
    path = PROJECT_ROOT / input_report
    data = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str}, low_memory=False)
    data = data[data["replay_rule"].astype(str) == replay_rule].copy()
    if data.empty:
        raise RuntimeError(f"没有找到 replay_rule={replay_rule} 的记录: {path}")
    for column in FACTOR_COLUMNS:
        if column not in data.columns:
            data[column] = "missing"
        data[column] = data[column].fillna("missing").astype(str)
    data["daily_return"] = pd.to_numeric(data["daily_return"], errors="coerce")
    data["net_return"] = pd.to_numeric(data["net_return"], errors="coerce")
    return data


def select_executed(data: pd.DataFrame) -> pd.DataFrame:
    return data[
        (data["buy_executed"].fillna(False) == True)  # noqa: E712
        & (data["sell_executed"].fillna(False) == True)  # noqa: E712
        & data["daily_return"].notna()
        & data["exit_trade_date"].notna()
    ].copy()


def build_filter_candidates(
    selected: pd.DataFrame,
    executed: pd.DataFrame,
    min_excluded_samples: int,
    max_candidates: int,
) -> list[tuple[str, str]]:
    base_mean = executed["daily_return"].mean()
    candidates: list[dict[str, object]] = []
    for column in FACTOR_COLUMNS:
        for value, group in executed.groupby(column, dropna=False):
            if value in {"missing", "unknown", "nan"}:
                continue
            count = len(group)
            if count < min_excluded_samples:
                continue
            group_mean = group["daily_return"].mean()
            year_returns = calculate_yearly_returns(group)
            weak_year_count = sum(value < 0 for value in year_returns.values())
            if group_mean >= base_mean and weak_year_count == 0:
                continue
            score = (base_mean - group_mean) * count + weak_year_count * 0.02
            candidates.append({"condition": (column, str(value)), "score": float(score), "count": int(count)})

    existing = {item["condition"] for item in candidates}
    for condition in MANUAL_CANDIDATES:
        if condition not in existing and condition[0] in selected.columns:
            hit_count = int((selected[condition[0]].astype(str) == condition[1]).sum())
            if hit_count >= min_excluded_samples:
                candidates.append({"condition": condition, "score": 1.0, "count": hit_count})

    candidates = sorted(candidates, key=lambda item: (item["score"], item["count"]), reverse=True)
    return [item["condition"] for item in candidates[:max_candidates]]


def apply_exclusions(data: pd.DataFrame, combo: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    mask = pd.Series(True, index=data.index)
    for column, value in combo:
        mask &= data[column].astype(str) != str(value)
    return data[mask].copy()


def summarize(
    scenario: str,
    selected: pd.DataFrame,
    executed: pd.DataFrame,
    combo: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    target_return: float,
) -> dict[str, object]:
    returns = executed["net_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
    equity_curve = (1 + daily_returns).cumprod()
    total_compound_return = float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0
    yearly_returns = calculate_yearly_returns(executed)
    return {
        "scenario": scenario,
        "excluded_conditions": ";".join(f"{column}={value}" for column, value in combo),
        "excluded_condition_count": len(combo),
        "signal_count": int(len(selected)),
        "sample_count": int(len(executed)),
        "buy_rejected_count": int((selected["buy_executed"].fillna(False) == False).sum()),  # noqa: E712
        "sell_unresolved_count": int((selected["buy_executed"].fillna(False) & ~selected["sell_executed"].fillna(False)).sum()),
        "total_compound_return": total_compound_return,
        "final_equity": 1000000 * (1 + total_compound_return),
        "beats_target": bool(total_compound_return > target_return),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
        "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
        "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
        "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
        "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
        "negative_year_count": int(sum(value < 0 for value in yearly_returns.values())),
        "return_2022": yearly_returns.get("2022", 0.0),
        "return_2026": yearly_returns.get("2026", 0.0),
    }


def build_yearly_rows(scenario: str, executed: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    if executed.empty:
        return rows
    data = executed.copy()
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    for year, group in data.groupby("year"):
        returns = group["net_return"].dropna()
        daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        rows.append(
            {
                "scenario": scenario,
                "year": str(year),
                "sample_count": int(len(group)),
                "year_return": float((1 + daily_returns).prod() - 1),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            }
        )
    return rows


def calculate_yearly_returns(executed: pd.DataFrame) -> dict[str, float]:
    if executed.empty:
        return {}
    data = executed.copy()
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    yearly_returns = {}
    for year, group in data.groupby("year"):
        daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        yearly_returns[str(year)] = float((1 + daily_returns).prod() - 1)
    return dict(sorted(yearly_returns.items()))


if __name__ == "__main__":
    main()
