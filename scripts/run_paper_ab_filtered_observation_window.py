"""
A严格主策略 + B0018备用策略风险过滤版观察窗口回放。

文件作用：
1. 固化当前A严格策略和B0018备用策略的组合观察口径。
2. B只在A无候选日启用，避免同一天同时抢信号。
3. 对B交易应用事前风险过滤：封单/流通市值偏高、LOSS_OVERLAY_WATCH。
4. 使用日线保守成交回放重新计算A、B、A+B filtered的资金曲线。

本脚本只使用本地CSV和本地配置，不接实盘，不调用QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_backup_strategy_b import (
    build_gap_report,
    replay_selected_b,
    selected_b_signals,
    simulate_a_plus_b_strict,
    simulate_b_strict,
    summarize,
)
from scripts.search_paper_backup_strategy_b import (
    backup_config,
    build_generator,
    normalize_date,
    scoped_no_candidate_dates,
    simulate_single_strategy,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行A+B filtered模拟盘观察窗口回放。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--start-date", default=None, help="观察窗口开始日期，格式YYYYMMDD。")
    parser.add_argument("--end-date", default=None, help="观察窗口截止日期，格式YYYYMMDD。")
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--limit", type=int, default=None, help="观察交易日数量。设置后覆盖--recent-days。")
    parser.add_argument("--output-prefix", default=None, help="输出文件前缀。默认读取配置。")
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def resolve_window_dates(
    candidates: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
    recent_days: int,
    limit: int | None,
) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if start_date:
        dates = [date for date in dates if date >= str(start_date)]
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于A+B filtered观察回放的交易日。")
    window_size = int(limit or recent_days)
    return dates[-window_size:]


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    runner = PaperDailyFlowRunner(strategy_config_path)
    return runner.load_audit_trades(runner.audit_trades_path)


def configured_b_conditions(config: dict[str, Any]) -> list[dict[str, str]]:
    ab_config = config.get("paper_ab_filtered_strategy", {})
    b_config = ab_config.get("b_strategy", {})
    conditions = []
    for condition in b_config.get("conditions", []):
        conditions.append(
            {
                "column": str(condition["column"]),
                "value": str(condition["value"]),
            }
        )
    if not conditions:
        raise RuntimeError("paper_ab_filtered_strategy.b_strategy.conditions 为空。")
    return conditions


def is_b_strategy_enabled(config: dict[str, Any]) -> bool:
    """读取B退役标记；当前策略配置必须返回False。"""
    b_config = config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
    return bool(b_config.get("enabled", False)) and not bool(
        b_config.get("new_entries_disabled", False)
    )


def condition_text(conditions: list[dict[str, str]]) -> str:
    return ";".join(f"{condition['column']}={condition['value']}" for condition in conditions)


def _apply_numeric_condition(values: pd.Series, operator: str, threshold: float) -> pd.Series:
    if operator == ">=":
        return values >= threshold
    if operator == ">":
        return values > threshold
    if operator == "<=":
        return values <= threshold
    if operator == "<":
        return values < threshold
    if operator == "==":
        return values == threshold
    return pd.Series(False, index=values.index)


def reject_strategy_risk_mask(
    replayed: pd.DataFrame,
    config: dict[str, Any],
    strategy_key: str,
) -> pd.Series:
    """按指定策略自身的风险规则过滤，避免C继续依赖已删除的B配置。"""
    if replayed.empty:
        return pd.Series(False, index=replayed.index)
    ab_config = config.get("paper_ab_filtered_strategy", {})
    strategy_config = ab_config.get(strategy_key, {})
    rules = strategy_config.get("risk_reject_rules", [])
    mask = pd.Series(False, index=replayed.index)
    risk_flags = replayed.get("risk_flags", pd.Series("", index=replayed.index)).fillna("").astype(str)
    for rule in rules:
        for keyword in rule.get("risk_flags_contains_any", []):
            mask = mask | risk_flags.str.contains(str(keyword), regex=False)
        for condition in rule.get("numeric_conditions", []):
            column = str(condition.get("column", ""))
            operator = str(condition.get("operator", "==")).strip()
            threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
            if not column or pd.isna(threshold):
                continue
            values = pd.to_numeric(replayed.get(column, pd.Series(0.0, index=replayed.index)), errors="coerce")
            mask = mask | _apply_numeric_condition(values, operator, float(threshold))
        # compound_conditions: list of AND-groups, OR'd together
        for group in rule.get("compound_conditions", []):
            sub = pd.Series(True, index=replayed.index)
            for condition in group:
                column = str(condition.get("column", ""))
                operator = str(condition.get("operator", "==")).strip()
                threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
                if not column or pd.isna(threshold):
                    sub = pd.Series(False, index=replayed.index)
                    break
                values = pd.to_numeric(replayed.get(column, pd.Series(0.0, index=replayed.index)), errors="coerce")
                sub = sub & _apply_numeric_condition(values, operator, float(threshold))
            mask = mask | sub.fillna(False)
    return mask.fillna(False).astype(bool)


def reject_b_risk_mask(replayed_b: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    """仅供历史B回放脚本复现旧报告；当前交易链路不再调用。"""
    return reject_strategy_risk_mask(replayed_b, config, "b_strategy")


def add_filter_summary_columns(
    summary: pd.DataFrame,
    selected_b: pd.DataFrame,
    rejected_b: pd.DataFrame,
    replayed_b_filtered: pd.DataFrame,
) -> pd.DataFrame:
    result = summary.copy()
    result["b_selected_signal_count_before_filter"] = int(len(selected_b))
    result["b_rejected_by_risk_filter_count"] = int(len(rejected_b))
    result["b_replayed_signal_count_after_filter"] = int(len(replayed_b_filtered))
    result["live_order_enabled"] = False
    return result


def build_filter_gap(replayed_b: pd.DataFrame, rejected_b: pd.DataFrame, replayed_b_filtered: pd.DataFrame) -> pd.DataFrame:
    gap = build_gap_report(replayed_b)
    extra = pd.DataFrame(
        [
            {
                "check_item": "b_risk_filtered_rejected_count",
                "value": int(len(rejected_b)),
                "note": "B交易中被事前风险过滤跳过的数量。",
            },
            {
                "check_item": "b_replayed_after_filter_count",
                "value": int(len(replayed_b_filtered)),
                "note": "B风险过滤后仍进入日线保守回放的数量。",
            },
            {
                "check_item": "live_order_enabled",
                "value": 0,
                "note": "本脚本只做paper/simulation，不允许实盘下单。",
            },
        ]
    )
    return pd.concat([gap, extra], ignore_index=True) if not gap.empty else extra


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    gap: pd.DataFrame,
    rejected_b: pd.DataFrame,
    combo_detail: pd.DataFrame,
) -> None:
    detail_columns = [
        "signal_date",
        "strategy_leg",
        "operation_status",
        "ts_code",
        "name",
        "account_return",
        "equity_after",
        "drawdown",
        "return_source",
        "risk_flags",
        "buy_reject_reason",
        "sell_reject_reason",
    ]
    detail_columns = [column for column in detail_columns if column in combo_detail.columns]
    rejected_columns = [
        "trade_date",
        "ts_code",
        "name",
        "daily_return",
        "strict_account_return",
        "risk_flags",
        "fd_ratio_bucket",
        "market_chain_count_bucket",
    ]
    rejected_columns = [column for column in rejected_columns if column in rejected_b.columns]
    content = f"""# A严格策略 + B0018过滤版观察窗口回放

本报告只使用本地日线数据和保守成交模型，不接实盘，不调用QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 审计缺口

{gap.to_markdown(index=False) if not gap.empty else "无审计缺口。"}

## B风险过滤跳过清单

{rejected_b[rejected_columns].to_markdown(index=False) if not rejected_b.empty else "无B风险过滤跳过项。"}

## A+B filtered逐日明细

{combo_detail[detail_columns].to_markdown(index=False) if not combo_detail.empty else "无逐日明细。"}

## 口径限制

- A使用当前严格策略配置。
- B只在A无候选日启用，当前条件为`segment_emotion_state_bucket=warming`。
- B额外使用配置中的`risk_reject_rules`过滤高风险候选。
- 当前仍是日线保守成交口径，不等于盘口五档真实成交。
- 后续仍需要分钟K、集合竞价、五档盘口和连续模拟盘验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    ab_config = base_config.get("paper_ab_filtered_strategy", {})
    if bool(ab_config.get("allow_live_order", False)) or bool(ab_config.get("live_order_enabled", False)):
        raise RuntimeError("拒绝运行A+B filtered观察：配置中存在实盘开关。")
    if not is_b_strategy_enabled(base_config):
        raise RuntimeError("拒绝运行B观察回放：策略B已彻底删除，旧脚本仅保留历史代码审计。")

    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_window_dates(all_candidates, args.start_date, args.end_date, args.recent_days, args.limit)
    audit = load_audit(args.strategy_config)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(base_config.get("position", {}).get("target_position_pct", 0.8))

    a_config = copy.deepcopy(base_config)
    a_generator = build_generator(args.strategy_config, a_config)
    a_filtered = a_generator.apply_strategy_filters(all_candidates)
    a_detail = simulate_single_strategy(
        scenario="A_strict",
        generator=a_generator,
        filtered=a_filtered,
        audit=audit,
        dates=dates,
        initial_equity=initial_equity,
        position_pct=position_pct,
    )
    no_candidate_dates = scoped_no_candidate_dates(a_detail)

    b_conditions = configured_b_conditions(base_config)
    b_config = backup_config(base_config, b_conditions)
    b_generator = build_generator(args.strategy_config, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, args.runtime_config)

    rejected_mask = reject_b_risk_mask(replayed_b, base_config)
    rejected_b = replayed_b[rejected_mask].copy()
    replayed_b_filtered = replayed_b[~rejected_mask].copy()

    b_strict_detail = simulate_b_strict(replayed_b_filtered, no_candidate_dates, initial_equity)
    combo_detail = simulate_a_plus_b_strict(a_detail, replayed_b_filtered, audit, dates, initial_equity)
    summary = pd.DataFrame(
        [
            summarize(a_detail, "A_strict", "current_config"),
            summarize(b_strict_detail, "B0018_filtered_on_A_no_candidate_days", condition_text(b_conditions)),
            summarize(combo_detail, "A_plus_B0018_filtered", condition_text(b_conditions)),
        ]
    )
    summary = add_filter_summary_columns(summary, selected_b, rejected_b, replayed_b_filtered)
    gap = build_filter_gap(replayed_b, rejected_b, replayed_b_filtered)

    output_prefix_value = args.output_prefix or ab_config.get(
        "output_prefix",
        "reports/paper_trade/ab_filtered/a_strict_plus_b0018_filtered",
    )
    output_prefix = resolve_path(output_prefix_value)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    window_size = int(args.limit or args.recent_days)
    suffix = f"_{dates[0]}_{dates[-1]}_{window_size}d"

    selected_path = output_prefix.with_name(output_prefix.name + suffix + "_b_selected.csv")
    replayed_path = output_prefix.with_name(output_prefix.name + suffix + "_b_replayed_before_filter.csv")
    rejected_path = output_prefix.with_name(output_prefix.name + suffix + "_b_rejected_by_filter.csv")
    filtered_path = output_prefix.with_name(output_prefix.name + suffix + "_b_replayed_after_filter.csv")
    b_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_b_filtered_detail.csv")
    combo_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_a_plus_b_filtered_detail.csv")
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    gap_path = output_prefix.with_name(output_prefix.name + suffix + "_gap.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    selected_b.to_csv(selected_path, index=False, encoding="utf-8-sig")
    replayed_b.to_csv(replayed_path, index=False, encoding="utf-8-sig")
    rejected_b.to_csv(rejected_path, index=False, encoding="utf-8-sig")
    replayed_b_filtered.to_csv(filtered_path, index=False, encoding="utf-8-sig")
    b_strict_detail.to_csv(b_detail_path, index=False, encoding="utf-8-sig")
    combo_detail.to_csv(combo_detail_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    gap.to_csv(gap_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, gap, rejected_b, combo_detail)

    print("A严格策略 + B0018过滤版观察窗口回放完成：")
    print(f"- selected_b: {selected_path}")
    print(f"- b_replayed_before_filter: {replayed_path}")
    print(f"- b_rejected_by_filter: {rejected_path}")
    print(f"- b_replayed_after_filter: {filtered_path}")
    print(f"- b_filtered_detail: {b_detail_path}")
    print(f"- a_plus_b_filtered_detail: {combo_detail_path}")
    print(f"- summary: {summary_path}")
    print(f"- gap: {gap_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
