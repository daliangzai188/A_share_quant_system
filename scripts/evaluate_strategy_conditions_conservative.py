from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


BEST_DANGER_EXCLUSIONS = {
    "limit_up_count_bucket": {"gte_180"},
    "first_time_detail_bucket": {"1000_1100"},
    "turnover_rate_bucket": {"6_10"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复核策略优化条件在日线保守成交口径下的表现。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--strategy-report", default="reports/strategy_optimization_report.csv", help="策略优化报告路径。")
    parser.add_argument("--output-report", default="reports/conservative_condition_eval_report.csv", help="输出汇总报告路径。")
    parser.add_argument("--output-yearly", default="reports/conservative_condition_eval_yearly.csv", help="输出年度报告路径。")
    parser.add_argument("--top-n", type=int, default=300, help="按原策略优化报告顺序复核前 N 个条件。")
    parser.add_argument("--min-source-sample", type=int, default=300, help="原策略优化报告里的最小样本数。")
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
    logger = get_logger("conservative_condition_eval")

    strategy_report = pd.read_csv(PROJECT_ROOT / args.strategy_report)
    strategy_report = strategy_report[strategy_report["sample_count"] >= args.min_source_sample].copy()
    strategy_report = strategy_report.head(args.top_n)
    if strategy_report.empty:
        raise RuntimeError("没有满足最小样本数的策略条件可复核。")

    optimizer = StrategyConditionOptimizer(config_path=args.config, optimization_config_key="optimization")
    candidates = optimizer.load_trades()
    replay_engine = ConservativeTradeReplay(config_path=args.config)
    replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
    replayed_candidates = replay_all_candidates(candidates, replay_engine, replay_rule)

    summary_rows = []
    yearly_rows = []
    logger.info("开始保守复核策略条件，条件数量: %s", len(strategy_report))
    for index, row in strategy_report.iterrows():
        condition_name = str(row["condition_name"])
        conditions = parse_conditions(condition_name)
        matched = apply_conditions(replayed_candidates, conditions)
        if matched.empty:
            continue
        summary_rows, yearly_rows = evaluate_variant(
            optimizer=optimizer,
            matched=matched,
            conditions=conditions,
            variant_name="raw_condition",
            source_row=row,
            summary_rows=summary_rows,
            yearly_rows=yearly_rows,
        )
        filtered = apply_danger_exclusions(matched)
        if not filtered.empty:
            summary_rows, yearly_rows = evaluate_variant(
                optimizer=optimizer,
                matched=filtered,
                conditions=conditions,
                variant_name="condition_plus_best_danger_filter",
                source_row=row,
                summary_rows=summary_rows,
                yearly_rows=yearly_rows,
            )
        if len(summary_rows) and len(summary_rows) % 50 == 0:
            logger.info("保守复核已生成场景: %s", len(summary_rows))

    report = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_rows)
    report = report.sort_values(
        ["total_compound_return", "sample_count", "max_drawdown"],
        ascending=[False, False, True],
    )
    output_report = PROJECT_ROOT / args.output_report
    output_yearly = PROJECT_ROOT / args.output_yearly
    output_report.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_report, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    logger.info("保守条件复核报告已生成: %s, 行数: %s", output_report, len(report))
    logger.info("保守条件复核年度报告已生成: %s, 行数: %s", output_yearly, len(yearly))
    print("保守条件复核完成：")
    print(f"- report: {output_report}")
    print(f"- yearly: {output_yearly}")


def replay_all_candidates(
    candidates: pd.DataFrame,
    replay_engine: ConservativeTradeReplay,
    replay_rule: ReplayRule,
) -> pd.DataFrame:
    forward_prices = replay_engine.load_forward_prices()
    samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replayed = replay_engine.replay_rule(samples, replay_rule)
    executable = replayed[
        (replayed["buy_executed"] == True)  # noqa: E712
        & (replayed["sell_executed"] == True)  # noqa: E712
        & replayed["daily_return"].notna()
    ]
    get_logger("conservative_condition_eval").info(
        "候选池保守成交预回放完成，候选: %s, 可买可卖样本: %s",
        len(replayed),
        len(executable),
    )
    return replayed


def evaluate_variant(
    optimizer: StrategyConditionOptimizer,
    matched: pd.DataFrame,
    conditions: dict[str, str],
    variant_name: str,
    source_row: pd.Series,
    summary_rows: list[dict[str, object]],
    yearly_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    selected = optimizer.select_daily_candidates(matched, max_holding_count=1)
    executed = selected[
        (selected["buy_executed"] == True)  # noqa: E712
        & (selected["sell_executed"] == True)  # noqa: E712
        & selected["daily_return"].notna()
    ].copy()
    if executed.empty:
        return summary_rows, yearly_rows

    scenario = f"{variant_name}|{format_conditions(conditions)}"
    summary_rows.append(build_summary_row(scenario, variant_name, source_row, selected, executed))
    yearly_rows.extend(build_yearly_rows(scenario, executed))
    return summary_rows, yearly_rows


def build_summary_row(
    scenario: str,
    variant_name: str,
    source_row: pd.Series,
    selected: pd.DataFrame,
    executed: pd.DataFrame,
) -> dict[str, object]:
    returns = executed["net_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
    equity_curve = (1 + daily_returns).cumprod()
    total_compound_return = float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0
    return {
        "scenario": scenario,
        "variant": variant_name,
        "source_strategy_name": source_row["strategy_name"],
        "source_sample_count": int(source_row["sample_count"]),
        "source_total_compound_return": float(source_row["total_compound_return"]),
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


def parse_conditions(condition_name: str) -> dict[str, str]:
    conditions = {}
    for item in condition_name.split(";"):
        if not item or "=" not in item:
            continue
        column, value = item.split("=", 1)
        conditions[column] = value
    return conditions


def apply_conditions(data: pd.DataFrame, conditions: dict[str, str]) -> pd.DataFrame:
    result = data
    for column, value in conditions.items():
        result = result[result[column].astype(str) == str(value)]
    return result.copy()


def apply_danger_exclusions(data: pd.DataFrame) -> pd.DataFrame:
    result = data
    for column, values in BEST_DANGER_EXCLUSIONS.items():
        result = result[~result[column].astype(str).isin(values)].copy()
    return result


def format_conditions(conditions: dict[str, str]) -> str:
    return ";".join(f"{column}={value}" for column, value in conditions.items())


if __name__ == "__main__":
    main()
