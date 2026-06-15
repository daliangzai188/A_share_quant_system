"""
大亏风控规则 Walk-Forward 验证。

文件作用：
1. 读取当前策略审计逐笔交易文件。
2. 按年份拆分训练期和测试期。
3. 只用训练期大亏交易生成风控规则，再应用到测试期。
4. 对比测试期基准和测试期风控后的复利、回撤、胜率和样本数。
5. 输出样本外验证报告，判断大亏风控规则是否存在过拟合风险。

本脚本只读取本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.stress_test_paper_loss_overlays import (  # noqa: E402
    build_loss_derived_rules,
    load_trades,
    rule_mask,
    rule_name,
    rule_set_mask,
    rule_set_name,
    simulate,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="大亏风控规则 Walk-Forward 验证。")
    parser.add_argument(
        "--input",
        default="reports/a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv",
        help="当前策略审计逐笔交易文件。",
    )
    parser.add_argument("--initial-cash", type=float, default=500000.0, help="每个验证区间独立初始资金。")
    parser.add_argument("--base-position-pct", type=float, default=0.8, help="基准仓位。")
    parser.add_argument("--loss-threshold", type=float, default=-0.08, help="训练期大亏账户收益阈值。")
    parser.add_argument("--max-rule-size", type=int, default=2, help="训练期自动生成规则最多组合几个因子。")
    parser.add_argument("--max-rule-hit-count", type=int, default=12, help="组合规则最多命中训练期多少笔。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_loss_overlay_walk_forward",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def add_period_columns(trades: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    if "exit_trade_date" in result.columns:
        result["exit_trade_date"] = result["exit_trade_date"].map(normalize_date)
        result["exit_year"] = result["exit_trade_date"].astype(str).str[:4]
    else:
        result["exit_year"] = result["trade_date"].astype(str).str[:4]
    return result


def split_definitions() -> list[dict[str, str]]:
    return [
        {
            "split_name": "train_2024_test_2025_2026",
            "train_start": "20240101",
            "train_end": "20241231",
            "test_start": "20250101",
            "test_end": "20260430",
        },
        {
            "split_name": "train_2024_2025_test_2026",
            "train_start": "20240101",
            "train_end": "20251231",
            "test_start": "20260101",
            "test_end": "20260430",
        },
        {
            "split_name": "train_2025_test_2026",
            "train_start": "20250101",
            "train_end": "20251231",
            "test_start": "20260101",
            "test_end": "20260430",
        },
    ]


def between_dates(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    date = trades["exit_trade_date"].astype(str)
    return trades[(date >= start) & (date <= end)].copy()


def build_rule_sets(
    train: pd.DataFrame,
    loss_threshold: float,
    max_rule_size: int,
    max_rule_hit_count: int,
) -> list[tuple[tuple[tuple[str, str], ...], ...]]:
    rules = build_loss_derived_rules(train, loss_threshold=loss_threshold, max_rule_size=max_rule_size)
    rule_sets: list[tuple[tuple[tuple[str, str], ...], ...]] = [(rule,) for rule in rules]
    compact_rules = []
    for rule in rules:
        hit = train[rule_mask(train, rule)].copy()
        if hit.empty:
            continue
        loss_hit_count = int((hit["dynamic_account_return"].astype(float) <= loss_threshold).sum())
        if loss_hit_count >= 1 and len(hit) <= max_rule_hit_count:
            compact_rules.append(rule)
    for first, second in combinations(compact_rules, 2):
        rule_set = tuple(sorted((first, second), key=rule_name))
        hit = train[rule_set_mask(train, rule_set)].copy()
        loss_hit_count = int((hit["dynamic_account_return"].astype(float) <= loss_threshold).sum()) if not hit.empty else 0
        if loss_hit_count >= 2 and len(hit) <= max_rule_hit_count:
            rule_sets.append(rule_set)
    deduped: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {}
    for rule_set in rule_sets:
        deduped[rule_set_name(rule_set)] = rule_set
    return list(deduped.values())


def summarize_period(
    trades: pd.DataFrame,
    initial_cash: float,
    base_position_pct: float,
    rule_set: tuple[tuple[tuple[str, str], ...], ...] | None,
    action: str,
    reduced_position_pct: float | None = None,
) -> dict[str, Any]:
    summary, _ = simulate(
        trades=trades,
        initial_cash=initial_cash,
        base_position_pct=base_position_pct,
        rule_set=rule_set,
        action=action,
        reduced_position_pct=reduced_position_pct,
    )
    return summary


def evaluate_split(trades: pd.DataFrame, split: dict[str, str], args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    train = between_dates(trades, split["train_start"], split["train_end"])
    test = between_dates(trades, split["test_start"], split["test_end"])
    if train.empty or test.empty:
        return pd.DataFrame(), pd.DataFrame()

    train_baseline = summarize_period(
        train,
        initial_cash=args.initial_cash,
        base_position_pct=args.base_position_pct,
        rule_set=None,
        action="baseline",
    )
    test_baseline = summarize_period(
        test,
        initial_cash=args.initial_cash,
        base_position_pct=args.base_position_pct,
        rule_set=None,
        action="baseline",
    )
    rule_sets = build_rule_sets(
        train=train,
        loss_threshold=args.loss_threshold,
        max_rule_size=args.max_rule_size,
        max_rule_hit_count=args.max_rule_hit_count,
    )
    candidate_rows: list[dict[str, Any]] = []
    for rule_set in rule_sets:
        for action, reduced_position_pct in [
            ("hard_exclude", None),
            ("reduce_position", 0.4),
            ("reduce_position", 0.2),
        ]:
            train_summary = summarize_period(
                train,
                initial_cash=args.initial_cash,
                base_position_pct=args.base_position_pct,
                rule_set=rule_set,
                action=action,
                reduced_position_pct=reduced_position_pct,
            )
            test_summary = summarize_period(
                test,
                initial_cash=args.initial_cash,
                base_position_pct=args.base_position_pct,
                rule_set=rule_set,
                action=action,
                reduced_position_pct=reduced_position_pct,
            )
            candidate_rows.append(
                {
                    "split_name": split["split_name"],
                    "train_range": f"{split['train_start']}-{split['train_end']}",
                    "test_range": f"{split['test_start']}-{split['test_end']}",
                    "overlay_rule": train_summary["overlay_rule"],
                    "overlay_action": action,
                    "overlay_reduced_position_pct": reduced_position_pct if reduced_position_pct is not None else "",
                    "train_baseline_multiple": train_baseline["equity_multiple"],
                    "train_overlay_multiple": train_summary["equity_multiple"],
                    "train_multiple_improvement": train_summary["equity_multiple"] / train_baseline["equity_multiple"]
                    if train_baseline["equity_multiple"]
                    else 0.0,
                    "train_overlay_max_drawdown": train_summary["max_drawdown"],
                    "train_rule_hit_count": train_summary["rule_hit_count"],
                    "train_rule_hit_loss_count": train_summary["rule_hit_loss_count"],
                    "test_baseline_multiple": test_baseline["equity_multiple"],
                    "test_overlay_multiple": test_summary["equity_multiple"],
                    "test_multiple_improvement": test_summary["equity_multiple"] / test_baseline["equity_multiple"]
                    if test_baseline["equity_multiple"]
                    else 0.0,
                    "test_baseline_max_drawdown": test_baseline["max_drawdown"],
                    "test_overlay_max_drawdown": test_summary["max_drawdown"],
                    "test_executed_trade_count": test_summary["executed_trade_count"],
                    "test_skipped_trade_count": test_summary["skipped_trade_count"],
                    "test_rule_hit_count": test_summary["rule_hit_count"],
                    "test_rule_hit_loss_count": test_summary["rule_hit_loss_count"],
                    "test_win_rate": test_summary["win_rate"],
                    "test_max_loss": test_summary["max_loss"],
                }
            )

    candidates = pd.DataFrame(candidate_rows)
    if candidates.empty:
        return candidates, pd.DataFrame()
    candidates = candidates.sort_values(
        ["split_name", "train_multiple_improvement", "train_overlay_max_drawdown", "train_rule_hit_count"],
        ascending=[True, False, False, True],
    )
    best_rows = []
    for split_name, group in candidates.groupby("split_name"):
        best = group.iloc[0].to_dict()
        best["selected_by"] = "best_train_multiple"
        best_rows.append(best)
    return candidates, pd.DataFrame(best_rows)


def write_markdown(path: Path, selected: pd.DataFrame, candidates: pd.DataFrame) -> None:
    selected_columns = [
        "split_name",
        "overlay_rule",
        "overlay_action",
        "overlay_reduced_position_pct",
        "train_baseline_multiple",
        "train_overlay_multiple",
        "test_baseline_multiple",
        "test_overlay_multiple",
        "test_multiple_improvement",
        "test_baseline_max_drawdown",
        "test_overlay_max_drawdown",
        "test_skipped_trade_count",
        "test_rule_hit_count",
        "test_rule_hit_loss_count",
    ]
    selected_columns = [column for column in selected_columns if column in selected.columns]
    top = candidates.sort_values("test_multiple_improvement", ascending=False).head(30) if not candidates.empty else pd.DataFrame()
    content = f"""# 大亏风控规则 Walk-Forward 验证

本报告只用训练期大亏生成规则，再应用到后续测试期。不接实盘，不调用 QMT，不下真实订单。

## 训练期最优规则的测试期表现

{selected[selected_columns].to_markdown(index=False) if not selected.empty else "无可用拆分。"}

## 测试期改善排名前 30

{top[selected_columns].to_markdown(index=False) if not top.empty else "无候选规则。"}

## 解读限制

最近两年只有 59 笔成交，且大亏样本只有 2 笔。Walk-Forward 结果只能判断过拟合迹象，不能证明规则可直接实盘。若测试期改善不稳定，规则应保留为观察预警，而不是硬过滤。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    trades = add_period_columns(load_trades(PROJECT_ROOT / args.input))
    candidate_frames = []
    selected_frames = []
    for split in split_definitions():
        candidates, selected = evaluate_split(trades, split, args)
        if not candidates.empty:
            candidate_frames.append(candidates)
        if not selected.empty:
            selected_frames.append(selected)
    candidates_df = pd.concat(candidate_frames, ignore_index=True) if candidate_frames else pd.DataFrame()
    selected_df = pd.concat(selected_frames, ignore_index=True) if selected_frames else pd.DataFrame()

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    candidates_path = output_prefix.with_name(output_prefix.name + "_candidates.csv")
    selected_path = output_prefix.with_name(output_prefix.name + "_selected.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")
    candidates_df.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    selected_df.to_csv(selected_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, selected_df, candidates_df)

    print("大亏风控规则 Walk-Forward 验证完成：")
    print(f"- candidates: {candidates_path}")
    print(f"- selected: {selected_path}")
    print(f"- markdown: {markdown_path}")
    if not selected_df.empty:
        columns = [
            "split_name",
            "overlay_rule",
            "overlay_action",
            "train_baseline_multiple",
            "train_overlay_multiple",
            "test_baseline_multiple",
            "test_overlay_multiple",
            "test_multiple_improvement",
            "test_overlay_max_drawdown",
            "test_skipped_trade_count",
        ]
        print(selected_df[columns].to_string(index=False))


if __name__ == "__main__":
    main()
