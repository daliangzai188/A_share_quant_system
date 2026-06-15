from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


PRE_SELECTION_DANGER_EXCLUSIONS = {
    "limit_up_count_bucket": {"gte_180"},
    "first_time_detail_bucket": {"1000_1100"},
    "turnover_rate_bucket": {"6_10"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描入口条件叠加选股后不开仓过滤的保守复利表现。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--strategy-report", default="reports/strategy_optimization_report.csv", help="策略优化报告路径。")
    parser.add_argument("--output-report", default="reports/condition_post_filter_optimization_report.csv")
    parser.add_argument("--output-yearly", default="reports/condition_post_filter_optimization_yearly.csv")
    parser.add_argument("--top-n", type=int, default=500, help="复核原策略优化报告前 N 个入口条件。")
    parser.add_argument("--min-source-sample", type=int, default=100, help="原策略优化报告里的最小样本数。")
    parser.add_argument("--min-remaining-samples", type=int, default=200, help="过滤后最小成交样本数。")
    parser.add_argument("--min-excluded-samples", type=int, default=10, help="单个排除条件最小命中样本数。")
    parser.add_argument("--max-post-filter-count", type=int, default=3, help="最多叠加几个选股后不开仓过滤。")
    parser.add_argument("--max-post-filter-candidates", type=int, default=20, help="每个入口条件最多保留几个后置过滤候选。")
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
    logger = get_logger("condition_post_filter_optimizer")

    strategy_report = pd.read_csv(PROJECT_ROOT / args.strategy_report)
    strategy_report = strategy_report[strategy_report["sample_count"] >= args.min_source_sample].head(args.top_n).copy()
    if strategy_report.empty:
        raise RuntimeError("没有满足最小样本数的入口条件。")

    optimizer = StrategyConditionOptimizer(config_path=args.config, optimization_config_key="optimization")
    candidates = optimizer.load_trades()
    replayed_candidates = replay_all_candidates(args.config, candidates)
    factor_columns = list(config.get("failure_filter_optimization", {}).get("factor_columns", []))

    summary_rows = []
    yearly_rows = []
    logger.info("开始扫描入口条件 + 后置过滤，入口条件数量: %s", len(strategy_report))
    for row_index, source_row in strategy_report.iterrows():
        conditions = parse_conditions(str(source_row["condition_name"]))
        base_matched = apply_conditions(replayed_candidates, conditions)
        for pre_filter_name, matched in [
            ("raw_condition", base_matched),
            ("condition_plus_pre_danger_filter", apply_pre_selection_danger_exclusions(base_matched)),
        ]:
            if matched.empty:
                continue
            selected = optimizer.select_daily_candidates(matched, max_holding_count=1)
            evaluate_selected(
                rows=summary_rows,
                yearly_rows=yearly_rows,
                selected=selected,
                source_row=source_row,
                entry_conditions=conditions,
                pre_filter_name=pre_filter_name,
                post_filters=(),
                min_remaining_samples=args.min_remaining_samples,
            )

            post_candidates = build_post_filter_candidates(
                selected=selected,
                factor_columns=factor_columns,
                min_excluded_samples=args.min_excluded_samples,
                max_candidates=args.max_post_filter_candidates,
            )
            for filter_count in range(1, args.max_post_filter_count + 1):
                for combo in combinations(post_candidates, filter_count):
                    filtered_selected = apply_post_filters(selected, combo)
                    evaluate_selected(
                        rows=summary_rows,
                        yearly_rows=yearly_rows,
                        selected=filtered_selected,
                        source_row=source_row,
                        entry_conditions=conditions,
                        pre_filter_name=pre_filter_name,
                        post_filters=combo,
                        min_remaining_samples=args.min_remaining_samples,
                    )

        if (row_index + 1) % 50 == 0:
            logger.info("入口条件扫描进度: %s/%s，已生成场景: %s", row_index + 1, len(strategy_report), len(summary_rows))

    report = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_rows)
    report = report.drop_duplicates("scenario")
    report = report.sort_values(["total_compound_return", "sample_count", "max_drawdown"], ascending=[False, False, True])
    output_report = PROJECT_ROOT / args.output_report
    output_yearly = PROJECT_ROOT / args.output_yearly
    output_report.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_report, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    logger.info("入口条件后置过滤优化报告已生成: %s, 行数: %s", output_report, len(report))
    logger.info("入口条件后置过滤年度报告已生成: %s, 行数: %s", output_yearly, len(yearly))
    print("入口条件后置过滤优化完成：")
    print(f"- report: {output_report}")
    print(f"- yearly: {output_yearly}")


def replay_all_candidates(config_path: str, candidates: pd.DataFrame) -> pd.DataFrame:
    replay_engine = ConservativeTradeReplay(config_path=config_path)
    replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
    forward_prices = replay_engine.load_forward_prices()
    samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replayed = replay_engine.replay_rule(samples, replay_rule)
    executable = replayed[
        (replayed["buy_executed"] == True)  # noqa: E712
        & (replayed["sell_executed"] == True)  # noqa: E712
        & replayed["daily_return"].notna()
    ]
    get_logger("condition_post_filter_optimizer").info(
        "候选池保守成交预回放完成，候选: %s, 可买可卖样本: %s",
        len(replayed),
        len(executable),
    )
    return replayed


def evaluate_selected(
    rows: list[dict[str, object]],
    yearly_rows: list[dict[str, object]],
    selected: pd.DataFrame,
    source_row: pd.Series,
    entry_conditions: dict[str, str],
    pre_filter_name: str,
    post_filters: tuple[tuple[str, str], ...],
    min_remaining_samples: int,
) -> None:
    executed = select_executed(selected)
    if len(executed) < min_remaining_samples:
        return
    scenario = format_scenario(entry_conditions, pre_filter_name, post_filters)
    rows.append(build_summary_row(scenario, selected, executed, source_row, pre_filter_name, post_filters))
    yearly_rows.extend(build_yearly_rows(scenario, executed))


def select_executed(selected: pd.DataFrame) -> pd.DataFrame:
    return selected[
        (selected["buy_executed"] == True)  # noqa: E712
        & (selected["sell_executed"] == True)  # noqa: E712
        & selected["daily_return"].notna()
    ].copy()


def build_summary_row(
    scenario: str,
    selected: pd.DataFrame,
    executed: pd.DataFrame,
    source_row: pd.Series,
    pre_filter_name: str,
    post_filters: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    returns = executed["net_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
    equity_curve = (1 + daily_returns).cumprod()
    total_compound_return = float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0
    return {
        "scenario": scenario,
        "pre_filter": pre_filter_name,
        "post_filters": format_post_filters(post_filters),
        "post_filter_count": len(post_filters),
        "source_strategy_name": source_row["strategy_name"],
        "source_sample_count": int(source_row["sample_count"]),
        "selected_signal_count": int(len(selected)),
        "selected_signal_days": int(selected["trade_date"].nunique()),
        "sample_count": int(len(executed)),
        "executed_days": int(executed["exit_trade_date"].nunique()),
        "buy_rejected_count": int((selected["buy_executed"] == False).sum()),  # noqa: E712
        "sell_unresolved_count": int(((selected["buy_executed"] == True) & (selected["sell_executed"] == False)).sum()),  # noqa: E712
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
        "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
        "total_compound_return": total_compound_return,
        "final_equity": 1000000 * (1 + total_compound_return),
        "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
        "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
        "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
    }


def build_yearly_rows(scenario: str, executed: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
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


def build_post_filter_candidates(
    selected: pd.DataFrame,
    factor_columns: list[str],
    min_excluded_samples: int,
    max_candidates: int,
) -> list[tuple[str, str]]:
    candidates = []
    executed = select_executed(selected)
    if executed.empty:
        return []
    base_mean = executed["daily_return"].mean()
    for column in factor_columns:
        if column not in executed.columns:
            continue
        grouped = executed.copy()
        grouped[column] = grouped[column].fillna("missing").astype(str)
        for value, group in grouped.groupby(column):
            count = len(group)
            if count < min_excluded_samples:
                continue
            group_mean = group["daily_return"].mean()
            if group_mean >= base_mean:
                continue
            candidates.append(
                {
                    "condition": (column, str(value)),
                    "count": int(count),
                    "score": float((base_mean - group_mean) * count),
                }
            )
    candidates = sorted(candidates, key=lambda item: (item["score"], item["count"]), reverse=True)
    return [item["condition"] for item in candidates[:max_candidates]]


def apply_conditions(data: pd.DataFrame, conditions: dict[str, str]) -> pd.DataFrame:
    result = data
    for column, value in conditions.items():
        result = result[result[column].astype(str) == str(value)]
    return result.copy()


def apply_pre_selection_danger_exclusions(data: pd.DataFrame) -> pd.DataFrame:
    result = data
    for column, values in PRE_SELECTION_DANGER_EXCLUSIONS.items():
        result = result[~result[column].astype(str).isin(values)].copy()
    return result


def apply_post_filters(selected: pd.DataFrame, post_filters: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    result = selected
    for column, value in post_filters:
        result = result[result[column].astype(str) != str(value)]
    return result.copy()


def parse_conditions(condition_name: str) -> dict[str, str]:
    conditions = {}
    for item in condition_name.split(";"):
        if not item or "=" not in item:
            continue
        column, value = item.split("=", 1)
        conditions[column] = value
    return conditions


def format_scenario(
    entry_conditions: dict[str, str],
    pre_filter_name: str,
    post_filters: tuple[tuple[str, str], ...],
) -> str:
    return (
        f"{pre_filter_name}|{format_conditions(entry_conditions)}"
        f"|post_exclude:{format_post_filters(post_filters)}"
    )


def format_conditions(conditions: dict[str, str]) -> str:
    return ";".join(f"{column}={value}" for column, value in conditions.items())


def format_post_filters(post_filters: tuple[tuple[str, str], ...]) -> str:
    if not post_filters:
        return ""
    return ";".join(f"{column}={value}" for column, value in post_filters)


if __name__ == "__main__":
    main()
