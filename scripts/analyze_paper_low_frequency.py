"""
分析模拟盘策略低频原因。

文件作用：
1. 读取当前策略候选数据和策略配置。
2. 对最近 N 个可用交易日执行逐层过滤漏斗统计。
3. 找出无候选日主要卡在哪个条件。
4. 统计单独放宽某个入选条件后可能增加的候选日期。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
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

from src.paper_candidate_generator import PaperCandidateGenerator
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析模拟盘策略低频原因。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--end-date", default=None, help="截止日期，格式 YYYYMMDD。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/low_frequency/a_clean_exclude_star_prev0_3_bj_recent",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def condition_label(condition: dict[str, Any]) -> str:
    return f"{condition.get('column', '')}={condition.get('value', '')}"


def rule_label(rule: dict[str, Any]) -> str:
    name = str(rule.get("name", "")).strip()
    if name:
        return name
    return "&".join(condition_label(condition) for condition in rule.get("conditions", []))


def resolve_recent_dates(candidates: pd.DataFrame, recent_days: int, end_date: str | None) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于低频分析的候选日期。")
    return dates[-recent_days:]


def apply_universe_filters(generator: PaperCandidateGenerator, data: pd.DataFrame) -> pd.DataFrame:
    return generator.apply_universe_filters(data)


def apply_condition(data: pd.DataFrame, condition: dict[str, Any]) -> pd.DataFrame:
    column = str(condition.get("column", ""))
    expected = str(condition.get("value", ""))
    if column not in data.columns:
        raise RuntimeError(f"条件字段不存在: {column}")
    return data[data[column].fillna("missing").astype(str) == expected].copy()


def apply_exclude_condition(data: pd.DataFrame, condition: dict[str, Any]) -> pd.DataFrame:
    column = str(condition.get("column", ""))
    expected = str(condition.get("value", ""))
    if column not in data.columns:
        raise RuntimeError(f"排除条件字段不存在: {column}")
    return data[data[column].fillna("missing").astype(str) != expected].copy()


def build_rule_mask(data: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=data.index)
    for condition in rule.get("conditions", []):
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in data.columns:
            raise RuntimeError(f"复合排除条件字段不存在: {column}")
        mask &= data[column].fillna("missing").astype(str) == expected
    return mask


def apply_exclude_rule(data: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    if not rule.get("conditions", []):
        return data
    mask = build_rule_mask(data, rule)
    return data[~mask].copy()


def build_daily_funnel(
    generator: PaperCandidateGenerator,
    candidates: pd.DataFrame,
    dates: list[str],
    config: dict[str, Any],
) -> pd.DataFrame:
    rows = []
    conditions = config.get("candidate_filters", {}).get("conditions", [])
    exclude_conditions = config.get("candidate_filters", {}).get("exclude_conditions", [])
    exclude_rules = config.get("candidate_filters", {}).get("exclude_rules", [])

    for date in dates:
        raw = candidates[candidates["trade_date"].map(normalize_date) == date].copy()
        row: dict[str, Any] = {
            "trade_date": date,
            "raw_count": int(len(raw)),
        }
        current = raw
        current = apply_universe_filters(generator, current)
        row["after_universe_count"] = int(len(current))

        for idx, condition in enumerate(conditions, start=1):
            current = apply_condition(current, condition)
            row[f"after_include_{idx}_{condition_label(condition)}"] = int(len(current))

        for idx, condition in enumerate(exclude_conditions, start=1):
            before_count = len(current)
            current = apply_exclude_condition(current, condition)
            label = condition_label(condition)
            row[f"exclude_condition_{idx}_{label}_removed"] = int(before_count - len(current))
            row[f"after_exclude_condition_{idx}_{label}"] = int(len(current))

        for idx, rule in enumerate(exclude_rules, start=1):
            before_count = len(current)
            current = apply_exclude_rule(current, rule)
            label = rule_label(rule)
            row[f"exclude_rule_{idx}_{label}_removed"] = int(before_count - len(current))
            row[f"after_exclude_rule_{idx}_{label}"] = int(len(current))

        row["final_count"] = int(len(current))
        row["first_zero_stage"] = resolve_first_zero_stage(row)
        rows.append(row)
    return pd.DataFrame(rows)


def resolve_first_zero_stage(row: dict[str, Any]) -> str:
    for key, value in row.items():
        if key in {"trade_date", "first_zero_stage"}:
            continue
        if key.endswith("_removed"):
            continue
        if isinstance(value, int) and value == 0:
            return key
    return "not_zero"


def build_stage_summary(daily_funnel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    count_columns = [
        column
        for column in daily_funnel.columns
        if column != "trade_date" and column != "first_zero_stage" and not column.endswith("_removed")
    ]
    for column in count_columns:
        values = pd.to_numeric(daily_funnel[column], errors="coerce").fillna(0)
        rows.append(
            {
                "stage": column,
                "days_with_candidate": int((values > 0).sum()),
                "zero_candidate_days": int((values == 0).sum()),
                "avg_count": float(values.mean()),
                "median_count": float(values.median()),
                "max_count": int(values.max()) if len(values) else 0,
            }
        )
    return pd.DataFrame(rows)


def build_zero_stage_summary(daily_funnel: pd.DataFrame) -> pd.DataFrame:
    if daily_funnel.empty:
        return pd.DataFrame(columns=["first_zero_stage", "day_count"])
    return (
        daily_funnel["first_zero_stage"]
        .value_counts()
        .rename_axis("first_zero_stage")
        .reset_index(name="day_count")
        .sort_values("day_count", ascending=False)
        .reset_index(drop=True)
    )


def build_single_relaxation_report(
    generator: PaperCandidateGenerator,
    candidates: pd.DataFrame,
    dates: list[str],
    config: dict[str, Any],
) -> pd.DataFrame:
    conditions = config.get("candidate_filters", {}).get("conditions", [])
    exclude_conditions = config.get("candidate_filters", {}).get("exclude_conditions", [])
    exclude_rules = config.get("candidate_filters", {}).get("exclude_rules", [])
    rows = []
    for condition_to_relax in conditions:
        condition_name = condition_label(condition_to_relax)
        created_days = []
        final_counts = []
        relaxed_counts = []
        for date in dates:
            raw = candidates[candidates["trade_date"].map(normalize_date) == date].copy()
            base = apply_universe_filters(generator, raw)
            relaxed = base.copy()
            strict = base.copy()
            for condition in conditions:
                strict = apply_condition(strict, condition)
                if condition is condition_to_relax:
                    continue
                relaxed = apply_condition(relaxed, condition)
            for exclude_condition in exclude_conditions:
                strict = apply_exclude_condition(strict, exclude_condition)
                relaxed = apply_exclude_condition(relaxed, exclude_condition)
            for exclude_rule in exclude_rules:
                strict = apply_exclude_rule(strict, exclude_rule)
                relaxed = apply_exclude_rule(relaxed, exclude_rule)
            strict_count = len(strict)
            relaxed_count = len(relaxed)
            final_counts.append(strict_count)
            relaxed_counts.append(relaxed_count)
            if strict_count == 0 and relaxed_count > 0:
                created_days.append(date)
        rows.append(
            {
                "relaxed_condition": condition_name,
                "created_candidate_day_count": int(len(created_days)),
                "created_candidate_days": ";".join(created_days[:30]),
                "strict_final_candidate_days": int(sum(count > 0 for count in final_counts)),
                "relaxed_candidate_days": int(sum(count > 0 for count in relaxed_counts)),
                "avg_relaxed_count": float(pd.Series(relaxed_counts).mean()) if relaxed_counts else 0.0,
                "max_relaxed_count": int(max(relaxed_counts)) if relaxed_counts else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("created_candidate_day_count", ascending=False).reset_index(drop=True)


def build_bucket_blocker_report(candidates: pd.DataFrame, dates: list[str], config: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for condition in config.get("candidate_filters", {}).get("conditions", []):
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in candidates.columns:
            continue
        scoped = candidates[candidates["trade_date"].map(normalize_date).isin(dates)].copy()
        grouped = (
            scoped.groupby(column, dropna=False)
            .size()
            .reset_index(name="raw_candidate_count")
            .sort_values("raw_candidate_count", ascending=False)
        )
        for row in grouped.head(12).itertuples(index=False):
            value = getattr(row, column)
            rows.append(
                {
                    "condition_column": column,
                    "required_value": expected,
                    "observed_value": value,
                    "raw_candidate_count": int(getattr(row, "raw_candidate_count")),
                    "is_required_value": str(value) == expected,
                }
            )
    return pd.DataFrame(rows)


def write_markdown(
    path: Path,
    stage_summary: pd.DataFrame,
    zero_stage_summary: pd.DataFrame,
    relaxation_report: pd.DataFrame,
    bucket_report: pd.DataFrame,
) -> None:
    content = f"""# 模拟盘策略低频原因分析

本报告只分析本地候选和策略过滤条件，不接实盘，不调用 QMT，不下真实订单。

## 过滤漏斗

{stage_summary.to_markdown(index=False)}

## 首次归零阶段

{zero_stage_summary.to_markdown(index=False)}

## 单条件放宽机会

{relaxation_report.to_markdown(index=False)}

## 入选字段原始分布

{bucket_report.to_markdown(index=False) if not bucket_report.empty else "无字段分布。"}

## 解释限制

- 单条件放宽只说明“可能增加候选”，不代表收益会变好。
- 是否放宽必须再跑收益、回撤、人工复核和成交真实性验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_json_config(args.strategy_config)
    generator = PaperCandidateGenerator(args.strategy_config)
    candidates = generator.load_all_candidates()
    dates = resolve_recent_dates(candidates, args.recent_days, args.end_date)
    daily_funnel = build_daily_funnel(generator, candidates, dates, config)
    stage_summary = build_stage_summary(daily_funnel)
    zero_stage_summary = build_zero_stage_summary(daily_funnel)
    relaxation_report = build_single_relaxation_report(generator, candidates, dates, config)
    bucket_report = build_bucket_blocker_report(candidates, dates, config)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{args.recent_days}d"
    daily_path = output_prefix.with_name(output_prefix.name + suffix + "_daily_funnel.csv")
    stage_path = output_prefix.with_name(output_prefix.name + suffix + "_stage_summary.csv")
    zero_path = output_prefix.with_name(output_prefix.name + suffix + "_zero_stage.csv")
    relaxation_path = output_prefix.with_name(output_prefix.name + suffix + "_single_relaxation.csv")
    bucket_path = output_prefix.with_name(output_prefix.name + suffix + "_bucket_distribution.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    daily_funnel.to_csv(daily_path, index=False, encoding="utf-8-sig")
    stage_summary.to_csv(stage_path, index=False, encoding="utf-8-sig")
    zero_stage_summary.to_csv(zero_path, index=False, encoding="utf-8-sig")
    relaxation_report.to_csv(relaxation_path, index=False, encoding="utf-8-sig")
    bucket_report.to_csv(bucket_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, stage_summary, zero_stage_summary, relaxation_report, bucket_report)

    print("模拟盘策略低频原因分析完成：")
    print(f"- daily_funnel: {daily_path}")
    print(f"- stage_summary: {stage_path}")
    print(f"- zero_stage: {zero_path}")
    print(f"- single_relaxation: {relaxation_path}")
    print(f"- bucket_distribution: {bucket_path}")
    print(f"- markdown: {markdown_path}")
    print(stage_summary.to_string(index=False))
    print(relaxation_report.to_string(index=False))


if __name__ == "__main__":
    main()
