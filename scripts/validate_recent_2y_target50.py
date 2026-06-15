"""
最近 2 年 50 倍目标轻量验证。

文件作用：
1. 读取已有最近 2 年搜索报告里的高分条件组合。
2. 使用真实执行口径重新回放：买入可成交、卖出可成交、动态滑点、容量限制、费用。
3. 只做局部强化验证，避免全矩阵优化占用过多内存。
4. 输出是否存在总复利 >= 50 倍的方案。

注意：
本脚本不接实盘、不调用 Tushare，只读取本地 CSV 和已有报告。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.search_recent_2y_realistic_strategy import Recent2YRealisticStrategySearch
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="最近2年50倍目标轻量验证。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--top-conditions", type=int, default=120, help="每个历史报告读取的高分条件数量。")
    parser.add_argument("--output", default="reports/recent_2y_target50_validation.csv", help="输出报告路径。")
    return parser.parse_args()


def parse_conditions(text: object) -> tuple[tuple[str, str], ...]:
    result = []
    for part in str(text).split(";"):
        if "=" not in part:
            continue
        factor, value = part.split("=", 1)
        if factor and value:
            result.append((factor, value))
    return tuple(result)


def conditions_to_name(conditions: tuple[tuple[str, str], ...]) -> str:
    return ";".join(f"{factor}={value}" for factor, value in conditions)


def load_condition_sets(paths: list[Path], top_n: int) -> list[tuple[tuple[str, str], ...]]:
    condition_sets: list[tuple[tuple[str, str], ...]] = []
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        data = pd.read_csv(path, low_memory=False)
        if data.empty or "conditions" not in data.columns:
            continue
        sort_columns = [column for column in ["hit_target", "hit_user_target", "equity_multiple", "ranking_score"] if column in data.columns]
        ascending = [False] * len(sort_columns)
        if sort_columns:
            data = data.sort_values(sort_columns, ascending=ascending)
        for text in data["conditions"].dropna().head(top_n):
            conditions = parse_conditions(text)
            if not conditions:
                continue
            key = conditions_to_name(conditions)
            if key in seen:
                continue
            seen.add(key)
            condition_sets.append(conditions)
    return condition_sets


def build_sort_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    full_config = config.get("recent_2y_full_strategy_optimization", {})
    rules = list(full_config.get("sort_rules", []))
    extra_rules = [
        {"name": "fill_probability_desc", "columns": ["fill_probability"], "ascending": [False]},
        {"name": "amount_desc", "columns": ["amount"], "ascending": [False]},
        {"name": "turnover_desc", "columns": ["turnover_rate"], "ascending": [False]},
        {"name": "turnover_asc", "columns": ["turnover_rate"], "ascending": [True]},
        {"name": "volume_ratio_desc", "columns": ["volume_ratio"], "ascending": [False]},
        {"name": "volume_ratio_asc", "columns": ["volume_ratio"], "ascending": [True]},
        {"name": "circ_mv_desc", "columns": ["circ_mv"], "ascending": [False]},
        {"name": "circ_mv_asc", "columns": ["circ_mv"], "ascending": [True]},
        {"name": "market_leader_rank_desc", "columns": ["market_leader_rank"], "ascending": [False]},
        {"name": "limit_height_rank_desc", "columns": ["limit_height_rank"], "ascending": [False]},
    ]
    seen = set()
    result = []
    for rule in rules + extra_rules:
        key = (tuple(rule.get("columns", [])), tuple(rule.get("ascending", [])))
        if key in seen:
            continue
        seen.add(key)
        result.append(rule)
    return result


def select_by_sort_rule(candidates: pd.DataFrame, sort_rule: dict[str, Any]) -> pd.DataFrame:
    columns = [column for column in sort_rule.get("columns", []) if column in candidates.columns]
    if not columns:
        columns = ["fill_probability"]
    ascending = list(sort_rule.get("ascending", []))[: len(columns)]
    if len(ascending) != len(columns):
        ascending = [False] * len(columns)
    selected = candidates.sort_values(
        ["trade_date"] + columns,
        ascending=[True] + ascending,
        na_position="last",
    )
    selected = selected.groupby("trade_date", as_index=False).head(1).copy()
    return selected.sort_values(["buy_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    condition_paths = [
        PROJECT_ROOT / "reports/recent_2y_fresh_summary.csv",
        PROJECT_ROOT / "reports/recent_2y_realistic_condition_search_summary.csv",
        PROJECT_ROOT / "reports/recent_2y_realistic_condition_search_four_factor_probe.csv",
        PROJECT_ROOT / "reports/recent_2y_best_condition_exit_sort_matrix.csv",
    ]
    condition_sets = load_condition_sets(condition_paths, args.top_conditions)
    if not condition_sets:
        raise RuntimeError("没有读取到可验证的最近2年条件组合。")

    searcher = Recent2YRealisticStrategySearch(config_path=args.config)
    candidates = searcher.load_candidates()
    replayed = searcher.attach_daily_liquidity(searcher.replay_candidates(candidates))
    sort_rules = build_sort_rules(config)

    rows: list[dict[str, Any]] = []
    total = len(condition_sets) * len(sort_rules)
    progress = 0
    for conditions in condition_sets:
        matched = searcher.apply_inclusion_conditions(replayed, conditions)
        if matched.empty:
            continue
        condition_name = conditions_to_name(conditions)
        for sort_rule in sort_rules:
            progress += 1
            selected = select_by_sort_rule(matched, sort_rule)
            simulated = searcher.simulate_single_position(
                selected,
                [str(sort_rule.get("name", "custom"))],
                [False],
            )
            summary = searcher.summarize_scenario(
                simulated,
                [str(sort_rule.get("name", "custom"))],
                [False],
            )
            summary.update(
                {
                    "conditions": condition_name,
                    "condition_count": len(conditions),
                    "sort_rule": str(sort_rule.get("name", "")),
                    "matched_candidate_count": int(len(matched)),
                    "matched_signal_days": int(matched["trade_date"].nunique()),
                }
            )
            rows.append(summary)
            if progress % 300 == 0:
                print(f"验证进度: {progress}/{total}")

    if not rows:
        raise RuntimeError("没有生成任何验证结果。")

    report = pd.DataFrame(rows).sort_values(
        ["hit_user_target", "equity_multiple", "ranking_score", "max_drawdown"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
    output_path = PROJECT_ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_path, index=False, encoding="utf-8-sig")

    hit = report[report["equity_multiple"] >= 50.0]
    print("\n最近2年50倍目标轻量验证完成")
    print(f"条件组合数: {len(condition_sets)}")
    print(f"排序规则数: {len(sort_rules)}")
    print(f"评估方案数: {len(report)}")
    print(f"达到 50 倍方案数: {len(hit)}")
    show = report.head(10).copy()
    columns = [
        "conditions",
        "sort_rule",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "avg_actual_position_pct",
        "avg_buy_slippage",
        "avg_sell_slippage",
        "return_2024",
        "return_2025",
        "return_2026",
    ]
    print(show[[column for column in columns if column in show.columns]].to_string(index=False))
    print(f"\n报告文件: {output_path}")


if __name__ == "__main__":
    main()
