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
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


DEFAULT_FACTOR_COLUMNS = [
    "market_emotion_state_bucket",
    "segment_emotion_state_bucket",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_limit_max_height_bucket",
    "theme_limit_count_bucket",
    "theme_limit_height_bucket",
    "theme_heat_rank_bucket",
    "theme_leader_rank_bucket",
    "theme_height_rank_bucket",
    "theme_is_mainline_bucket",
    "market_segment",
    "limit_pct_bucket",
    "segment_market_sentiment_level",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "segment_market_leader_rank_bucket",
    "segment_limit_height_rank_bucket",
    "first_time_detail_bucket",
    "open_times_bucket",
    "volume_ratio_bucket",
    "amount_ratio_bucket",
    "turnover_rate_bucket",
    "fd_ratio_bucket",
    "pct_chg_bucket",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
]

DEFAULT_PRE_EXCLUSIONS = {
    "first_time_detail_bucket": {"1000_1100"},
    "turnover_rate_bucket": {"6_10"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按分板块市场情绪口径扫描保守成交策略组合。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--output-report", default="reports/segmented_market_strategy_optimization.csv")
    parser.add_argument("--output-yearly", default="reports/segmented_market_strategy_optimization_yearly.csv")
    parser.add_argument("--min-matched-samples", type=int, default=200, help="条件命中候选的最小样本数。")
    parser.add_argument("--min-executed-samples", type=int, default=180, help="保守成交后的最小样本数。")
    parser.add_argument("--max-factor-count", type=int, default=4, help="最多组合几个条件。")
    parser.add_argument("--max-values-per-factor", type=int, default=8, help="每个因子最多保留几个取值。")
    parser.add_argument("--max-scenarios", type=int, default=0, help="最多生成多少个有效场景；0 表示不限制。")
    parser.add_argument("--top-n", type=int, default=200, help="输出收益最高的前 N 个组合。")
    parser.add_argument("--target-return", type=float, default=301.8, help="目标总复利倍数，用于报告标记。")
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
    logger = get_logger("segmented_market_strategy_optimizer")

    optimizer = StrategyConditionOptimizer(config_path=args.config, optimization_config_key="optimization")
    candidates = optimizer.load_trades()
    replayed = replay_candidates(args.config, candidates)
    factor_columns = [column for column in DEFAULT_FACTOR_COLUMNS if column in replayed.columns]
    factor_values = build_factor_values(
        replayed,
        factor_columns=factor_columns,
        max_values_per_factor=args.max_values_per_factor,
    )
    single_conditions = [
        (column, value)
        for column, values in factor_values.items()
        for value in values
    ]
    logger.info("开始分板块策略扫描，候选: %s, 单条件数量: %s", len(replayed), len(single_conditions))

    rows: list[dict[str, object]] = []
    yearly_rows: list[dict[str, object]] = []
    scenario_count = 0
    for factor_count in range(1, args.max_factor_count + 1):
        for combo in combinations(single_conditions, factor_count):
            if has_duplicate_factor(combo):
                continue
            matched = apply_conditions(replayed, combo)
            if len(matched) < args.min_matched_samples:
                continue
            for variant_name, variant_data in build_variants(matched):
                selected = select_daily_top_one(variant_data)
                executed = select_executed(selected)
                if len(executed) < args.min_executed_samples:
                    continue
                scenario = format_scenario(combo, variant_name)
                summary = summarize_scenario(
                    scenario=scenario,
                    variant_name=variant_name,
                    combo=combo,
                    selected=selected,
                    executed=executed,
                    target_return=args.target_return,
                )
                rows.append(summary)
                yearly_rows.extend(build_yearly_rows(scenario, executed))
                scenario_count += 1
                if scenario_count % 1000 == 0:
                    logger.info("已生成有效场景: %s", scenario_count)
                if args.max_scenarios and scenario_count >= args.max_scenarios:
                    logger.info("达到有效场景上限，提前停止扫描: %s", args.max_scenarios)
                    break
            if args.max_scenarios and scenario_count >= args.max_scenarios:
                break
        if args.max_scenarios and scenario_count >= args.max_scenarios:
            break

    if not rows:
        raise RuntimeError("没有找到满足样本数要求的组合。")

    report = pd.DataFrame(rows).drop_duplicates("scenario")
    report = report.sort_values(["total_compound_return", "sample_count", "max_drawdown"], ascending=[False, False, True])
    report = report.head(args.top_n)
    yearly = pd.DataFrame(yearly_rows)
    yearly = yearly[yearly["scenario"].isin(set(report["scenario"]))].copy()

    output_report = PROJECT_ROOT / args.output_report
    output_yearly = PROJECT_ROOT / args.output_yearly
    output_report.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_report, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    logger.info("分板块策略优化报告已生成: %s, 行数: %s", output_report, len(report))
    logger.info("分板块策略优化年度报告已生成: %s, 行数: %s", output_yearly, len(yearly))
    print("分板块策略优化完成：")
    print(f"- report: {output_report}")
    print(f"- yearly: {output_yearly}")


def replay_candidates(config_path: str, candidates: pd.DataFrame) -> pd.DataFrame:
    replay_engine = ConservativeTradeReplay(config_path=config_path)
    replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
    forward_prices = replay_engine.load_forward_prices()
    samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replayed = replay_engine.replay_rule(samples, replay_rule)
    replayed["first_time_detail_bucket"] = replayed["first_time_detail_bucket"].astype(str)
    return replayed


def build_factor_values(
    data: pd.DataFrame,
    factor_columns: list[str],
    max_values_per_factor: int,
) -> dict[str, list[str]]:
    values_by_factor: dict[str, list[str]] = {}
    for column in factor_columns:
        counts = data[column].fillna("unknown").astype(str).value_counts()
        values = [
            value
            for value in counts.index.tolist()
            if value not in {"unknown", "nan"}
        ][:max_values_per_factor]
        if values:
            values_by_factor[column] = values
    return values_by_factor


def has_duplicate_factor(combo: tuple[tuple[str, str], ...]) -> bool:
    factors = [column for column, _ in combo]
    return len(factors) != len(set(factors))


def apply_conditions(data: pd.DataFrame, combo: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    result = data
    for column, value in combo:
        result = result[result[column].fillna("unknown").astype(str) == str(value)]
        if result.empty:
            return result
    return result.copy()


def build_variants(data: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    variants = [("raw", data)]
    filtered = data.copy()
    for column, values in DEFAULT_PRE_EXCLUSIONS.items():
        if column in filtered.columns:
            filtered = filtered[~filtered[column].fillna("unknown").astype(str).isin(values)].copy()
    variants.append(("pre_exclude_bad_time_turnover", filtered))
    return variants


def select_daily_top_one(data: pd.DataFrame) -> pd.DataFrame:
    sort_columns = [
        "trade_date",
        "fill_probability",
        "sample_count",
        "segment_market_leader_rank",
        "amount",
        "turnover_rate",
    ]
    existing_sort_columns = [column for column in sort_columns if column in data.columns]
    ascending = [True]
    for column in existing_sort_columns[1:]:
        ascending.append(column == "segment_market_leader_rank")
    selected = data.sort_values(existing_sort_columns, ascending=ascending)
    selected = selected.groupby("trade_date").head(1).copy()
    selected["daily_return"] = selected["net_return"] * 0.8
    return selected


def select_executed(selected: pd.DataFrame) -> pd.DataFrame:
    return selected[
        (selected["buy_executed"] == True)  # noqa: E712
        & (selected["sell_executed"] == True)  # noqa: E712
        & selected["daily_return"].notna()
    ].copy()


def summarize_scenario(
    scenario: str,
    variant_name: str,
    combo: tuple[tuple[str, str], ...],
    selected: pd.DataFrame,
    executed: pd.DataFrame,
    target_return: float,
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
        "conditions": format_conditions(combo),
        "factor_count": len(combo),
        "selected_signal_count": int(len(selected)),
        "sample_count": int(len(executed)),
        "signal_days": int(selected["trade_date"].nunique()),
        "executed_days": int(executed["exit_trade_date"].nunique()),
        "buy_rejected_count": int((selected["buy_executed"] == False).sum()),  # noqa: E712
        "sell_unresolved_count": int(((selected["buy_executed"] == True) & (selected["sell_executed"] == False)).sum()),  # noqa: E712
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
        "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
        "total_compound_return": total_compound_return,
        "final_equity": 1000000 * (1 + total_compound_return),
        "beats_target": bool(total_compound_return > target_return),
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


def format_conditions(combo: tuple[tuple[str, str], ...]) -> str:
    return ";".join(f"{column}={value}" for column, value in combo)


def format_scenario(combo: tuple[tuple[str, str], ...], variant_name: str) -> str:
    return f"{variant_name}|{format_conditions(combo)}"


if __name__ == "__main__":
    main()
