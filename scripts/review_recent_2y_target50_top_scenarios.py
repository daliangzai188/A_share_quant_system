"""
复盘最近 2 年 50 倍候选方案的逐笔交易。

文件作用：
1. 从 recent_2y_target50_validation.csv 读取最高复利候选。
2. 对前 N 个去重方案重新生成逐笔交易明细。
3. 输出年度收益、跳过原因、最大回撤区间、最大盈利/亏损交易。
4. 用于判断候选收益是否集中、是否过拟合、是否存在成交真实性问题。

本脚本只读取本地数据，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.search_recent_2y_realistic_strategy import Recent2YRealisticStrategySearch
from scripts.validate_recent_2y_target50 import build_sort_rules, parse_conditions, select_by_sort_rule
from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复盘最近2年50倍候选方案逐笔交易。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_validation.csv",
        help="50倍目标验证报告路径。",
    )
    parser.add_argument("--top", type=int, default=2, help="复盘前 N 个去重方案。")
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_target50_top_scenarios",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def select_top_unique_scenarios(input_path: Path, top_n: int) -> pd.DataFrame:
    report = pd.read_csv(input_path, low_memory=False)
    report = report.sort_values(["equity_multiple", "ranking_score"], ascending=[False, False])
    rows = []
    seen = set()
    for _, row in report.iterrows():
        key = (str(row["conditions"]), str(row["sort_rule"]))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= top_n:
            break
    if not rows:
        raise RuntimeError(f"没有可复盘的方案: {input_path}")
    return pd.DataFrame(rows).reset_index(drop=True)


def find_sort_rule(sort_rules: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for rule in sort_rules:
        if str(rule.get("name", "")) == name:
            return rule
    raise RuntimeError(f"找不到排序规则: {name}")


def add_drawdown_columns(executed: pd.DataFrame) -> pd.DataFrame:
    result = executed.copy()
    result["equity_peak"] = result["equity_after"].cummax()
    result["drawdown"] = result["equity_after"] / result["equity_peak"] - 1
    return result


def build_summary_row(
    scenario_rank: int,
    source_row: pd.Series,
    simulated: pd.DataFrame,
    scenario_label: str,
) -> dict[str, Any]:
    executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
    skipped = simulated[simulated["scenario_executed"] != True].copy()  # noqa: E712
    executed = add_drawdown_columns(executed) if not executed.empty else executed

    if executed.empty:
        return {
            "scenario_rank": scenario_rank,
            "scenario_label": scenario_label,
            "conditions": str(source_row["conditions"]),
            "sort_rule": str(source_row["sort_rule"]),
            "executed_trade_count": 0,
        }

    max_dd_idx = executed["drawdown"].idxmin()
    trough = executed.loc[max_dd_idx]
    before_trough = executed.loc[:max_dd_idx]
    peak_idx = before_trough["equity_after"].idxmax()
    peak = executed.loc[peak_idx]
    returns = executed["dynamic_account_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]

    return {
        "scenario_rank": scenario_rank,
        "scenario_label": scenario_label,
        "conditions": str(source_row["conditions"]),
        "sort_rule": str(source_row["sort_rule"]),
        "source_equity_multiple": float(source_row["equity_multiple"]),
        "final_equity": float(executed["equity_after"].iloc[-1]),
        "equity_multiple": float(executed["equity_after"].iloc[-1] / executed["equity_before"].iloc[0]),
        "executed_trade_count": int(len(executed)),
        "selected_signal_count": int(len(simulated)),
        "skipped_count": int(len(skipped)),
        "buy_rejected_count": int((skipped["skip_reason"].astype(str) == "open_limit_up_unbuyable").sum()) if not skipped.empty else 0,
        "position_occupied_skip_count": int((skipped["skip_reason"].astype(str) == "position_occupied").sum()) if not skipped.empty else 0,
        "sell_unresolved_count": int((skipped["skip_reason"].astype(str).str.contains("sell|limit_down|missing_exit", regex=True)).sum()) if not skipped.empty else 0,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        "max_drawdown": float(executed["drawdown"].min()),
        "max_drawdown_peak_date": normalize_date(peak.get("exit_trade_date", peak.get("trade_date", ""))),
        "max_drawdown_trough_date": normalize_date(trough.get("exit_trade_date", trough.get("trade_date", ""))),
        "max_drawdown_peak_equity": float(peak["equity_after"]),
        "max_drawdown_trough_equity": float(trough["equity_after"]),
        "avg_actual_buy_amount": float(executed["actual_buy_amount"].mean()),
        "max_actual_buy_amount": float(executed["actual_buy_amount"].max()),
        "avg_actual_position_pct": float(executed["actual_position_pct"].mean()),
        "avg_buy_slippage": float(executed["dynamic_buy_slippage_rate"].mean()),
        "avg_sell_slippage": float(executed["dynamic_sell_slippage_rate"].mean()),
        "max_buy_amount_ratio": float(executed["buy_amount_ratio"].max()),
        "max_sell_amount_ratio": float(executed["sell_amount_ratio"].max()),
        "top_5_profit_contribution": float(returns.sort_values(ascending=False).head(5).sum()),
        "top_5_loss_contribution": float(returns.sort_values().head(5).sum()),
    }


def build_yearly_rows(simulated: pd.DataFrame, scenario_rank: int, scenario_label: str) -> list[dict[str, Any]]:
    executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return []
    executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
    rows = []
    for year, group in executed.groupby("year"):
        first_equity = float(group["equity_before"].iloc[0])
        last_equity = float(group["equity_after"].iloc[-1])
        returns = group["dynamic_account_return"].dropna()
        equity_curve = group["equity_after"] / first_equity if first_equity else pd.Series(dtype=float)
        rows.append(
            {
                "scenario_rank": scenario_rank,
                "scenario_label": scenario_label,
                "year": str(year),
                "sample_count": int(len(group)),
                "first_equity": first_equity,
                "last_equity": last_equity,
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            }
        )
    return rows


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    input_path = PROJECT_ROOT / args.input
    scenarios = select_top_unique_scenarios(input_path, args.top)
    sort_rules = build_sort_rules(config)

    searcher = Recent2YRealisticStrategySearch(config_path=args.config)
    candidates = searcher.load_candidates()
    replayed = searcher.attach_daily_liquidity(searcher.replay_candidates(candidates))

    summary_rows = []
    yearly_rows = []
    detail_frames = []
    skip_rows = []

    for index, source_row in scenarios.iterrows():
        scenario_rank = int(index) + 1
        conditions_text = str(source_row["conditions"])
        sort_rule_name = str(source_row["sort_rule"])
        sort_rule = find_sort_rule(sort_rules, sort_rule_name)
        conditions = parse_conditions(conditions_text)
        matched = searcher.apply_inclusion_conditions(replayed, conditions)
        selected = select_by_sort_rule(matched, sort_rule)
        simulated = searcher.simulate_single_position(
            selected,
            [sort_rule_name],
            [False],
        )
        scenario_label = f"rank{scenario_rank}_{sort_rule_name}"
        simulated["scenario_rank"] = scenario_rank
        simulated["scenario_label"] = scenario_label
        simulated["conditions"] = conditions_text
        simulated["sort_rule"] = sort_rule_name
        simulated["selected_order"] = range(1, len(simulated) + 1)

        summary_rows.append(build_summary_row(scenario_rank, source_row, simulated, scenario_label))
        yearly_rows.extend(build_yearly_rows(simulated, scenario_rank, scenario_label))
        detail_frames.append(simulated)

        skipped = simulated[simulated["scenario_executed"] != True].copy()  # noqa: E712
        if not skipped.empty:
            skip_summary = skipped["skip_reason"].fillna("unknown").astype(str).value_counts().reset_index()
            skip_summary.columns = ["skip_reason", "count"]
            skip_summary["scenario_rank"] = scenario_rank
            skip_summary["scenario_label"] = scenario_label
            skip_rows.append(skip_summary)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_rows)
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    skips = pd.concat(skip_rows, ignore_index=True) if skip_rows else pd.DataFrame()

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_trades.csv")
    skips_path = output_prefix.with_name(output_prefix.name + "_skips.csv")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    skips.to_csv(skips_path, index=False, encoding="utf-8-sig")

    print("最近2年50倍候选逐笔复盘完成")
    print(summary[[
        "scenario_rank",
        "scenario_label",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "max_drawdown_peak_date",
        "max_drawdown_trough_date",
        "max_actual_buy_amount",
        "avg_buy_slippage",
        "avg_sell_slippage",
    ]].to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- trades: {detail_path}")
    print(f"- skips: {skips_path}")


if __name__ == "__main__":
    main()
