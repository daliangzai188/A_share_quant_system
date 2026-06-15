"""
应用单日模拟盘人工复核决策。

文件作用：
1. 读取单日模拟盘 manual_review.csv 和人工填写的 manual_review_decisions.csv。
2. 校验 review_decision 只能是 APPROVED / REJECTED / PENDING。
3. 输出单日复核结果明细、汇总和 Markdown。
4. 回写每日操作台 checklist 的复核状态，供后续模拟盘观察汇总读取。

本脚本只处理本地 CSV，不接实盘，不调用 QMT，不下真实订单。
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

from scripts.apply_manual_review_decisions import (
    apply_suggested_rejections,
    build_summary,
    load_or_create_decisions,
    merge_decisions,
    read_csv,
    write_markdown as write_review_markdown,
)
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="应用单日模拟盘人工复核决策。")
    parser.add_argument("--signal-date", required=True, help="信号日期，格式 YYYYMMDD。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument(
        "--daily-flow-prefix",
        default=None,
        help="单日模拟盘流程输出前缀。不传则读取 strategy_config.paper_daily_flow.output_prefix。",
    )
    parser.add_argument(
        "--daily-ops-prefix",
        default="reports/paper_trade/daily_ops/a_clean_exclude_star_prev0_3_bj",
        help="每日操作台 checklist 输出前缀。",
    )
    parser.add_argument("--manual-review", default=None, help="单日 manual_review.csv 路径。")
    parser.add_argument("--decisions", default=None, help="人工复核决策 CSV 路径。")
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="复核结果输出前缀。不传则写到 daily_flow 同前缀。",
    )
    parser.add_argument(
        "--apply-suggested-rejections",
        action="store_true",
        help="把 suggested_decision=REJECTED 且 review_decision=PENDING 的记录写成 REJECTED。",
    )
    parser.add_argument(
        "--no-update-checklist",
        action="store_true",
        help="只输出复核结果，不回写每日操作台 checklist。",
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


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def default_daily_flow_prefix(args: argparse.Namespace) -> Path:
    if args.daily_flow_prefix:
        return resolve_path(args.daily_flow_prefix)
    config = load_json_config(args.strategy_config)
    flow_config = config.get("paper_daily_flow", {})
    return resolve_path(flow_config.get("output_prefix", "reports/paper_trade/daily_flow/current_strategy"))


def default_path(prefix: Path, signal_date: str, suffix: str) -> Path:
    return prefix.with_name(prefix.name + f"_{signal_date}{suffix}")


def resolve_input_paths(args: argparse.Namespace) -> dict[str, Path]:
    signal_date = normalize_date(args.signal_date)
    daily_flow_prefix = default_daily_flow_prefix(args)
    daily_ops_prefix = resolve_path(args.daily_ops_prefix)
    manual_review = (
        resolve_path(args.manual_review)
        if args.manual_review
        else default_path(daily_flow_prefix, signal_date, "_manual_review.csv")
    )
    decisions = (
        resolve_path(args.decisions)
        if args.decisions
        else default_path(daily_flow_prefix, signal_date, "_manual_review_decisions.csv")
    )
    output_prefix = (
        resolve_path(args.output_prefix)
        if args.output_prefix
        else default_path(daily_flow_prefix, signal_date, "_manual_review_result")
    )
    checklist = default_path(daily_ops_prefix, signal_date, "_checklist.csv")
    return {
        "manual_review": manual_review,
        "decisions": decisions,
        "output_prefix": output_prefix,
        "checklist": checklist,
    }


def resolve_review_decision_status(result: pd.DataFrame) -> str:
    if result.empty or "review_decision" not in result.columns:
        return "NOT_REQUIRED"
    decisions = result["review_decision"].fillna("PENDING").astype(str).str.upper().str.strip()
    if decisions.eq("APPROVED").all():
        return "APPROVED"
    if decisions.eq("REJECTED").all():
        return "REJECTED"
    if decisions.isin({"APPROVED", "REJECTED"}).all():
        return "MIXED_FINAL"
    return "PENDING"


def resolve_operation_status(review_status: str) -> str:
    if review_status == "APPROVED":
        return "MANUAL_APPROVED_PAPER_OBSERVATION_ONLY"
    if review_status == "REJECTED":
        return "MANUAL_REJECTED_SKIP_OBSERVATION"
    if review_status == "MIXED_FINAL":
        return "MANUAL_REVIEW_MIXED_FINAL"
    return "REVIEW_REQUIRED_PLAN_ONLY"


def resolve_next_action(review_status: str) -> str:
    if review_status == "APPROVED":
        return "人工复核已通过，只允许进入模拟观察，不允许实盘。"
    if review_status == "REJECTED":
        return "人工复核已拒绝，跳过本次模拟观察，不进入实盘。"
    if review_status == "MIXED_FINAL":
        return "人工复核存在批准和拒绝混合结果，按明细逐条处理，仍不允许实盘。"
    return "人工复核未完成，保持计划冻结，不允许进入模拟成交或实盘。"


def update_checklist(
    checklist_path: Path,
    result: pd.DataFrame,
    detail_path: Path,
    summary_path: Path,
    markdown_path: Path,
) -> tuple[pd.DataFrame, bool]:
    checklist = read_csv_if_exists(checklist_path)
    if checklist.empty:
        return pd.DataFrame(), False

    review_status = resolve_review_decision_status(result)
    approved_count = int(result["review_decision"].astype(str).str.upper().eq("APPROVED").sum())
    rejected_count = int(result["review_decision"].astype(str).str.upper().eq("REJECTED").sum())
    pending_count = int(result["review_decision"].astype(str).str.upper().eq("PENDING").sum())

    updated = checklist.copy()
    updated["operation_status"] = resolve_operation_status(review_status)
    updated["next_action"] = resolve_next_action(review_status)
    updated["review_decision_status"] = review_status
    updated["manual_review_approved_count"] = approved_count
    updated["manual_review_rejected_count"] = rejected_count
    updated["manual_review_pending_count"] = pending_count
    updated["manual_review_result_detail_path"] = str(detail_path)
    updated["manual_review_result_summary_path"] = str(summary_path)
    updated["manual_review_result_markdown_path"] = str(markdown_path)
    updated["paper_observation_allowed"] = review_status == "APPROVED"
    updated["live_order_enabled"] = False
    updated["safety_note"] = "只允许模拟观察；人工复核通过也不代表可以实盘。"
    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    updated.to_csv(checklist_path, index=False, encoding="utf-8-sig")
    return updated, True


def build_daily_apply_summary(
    result: pd.DataFrame,
    review_summary: pd.DataFrame,
    checklist_updated: bool,
    checklist_path: Path,
) -> pd.DataFrame:
    review_status = resolve_review_decision_status(result)
    base = review_summary.iloc[0].to_dict() if not review_summary.empty else {}
    base.update(
        {
            "review_decision_status": review_status,
            "operation_status_after_apply": resolve_operation_status(review_status),
            "paper_observation_allowed": review_status == "APPROVED",
            "checklist_updated": bool(checklist_updated),
            "checklist_path": str(checklist_path),
            "live_order_enabled": False,
        }
    )
    return pd.DataFrame([base])


def write_daily_markdown(
    path: Path,
    daily_summary: pd.DataFrame,
    result: pd.DataFrame,
    checklist: pd.DataFrame,
) -> None:
    preview_columns = [
        "signal_date",
        "planned_order_date",
        "ts_code",
        "name",
        "review_decision",
        "suggested_decision",
        "suggestion_confidence",
        "final_review_status",
        "paper_observation_allowed",
        "risk_flags",
        "review_note",
    ]
    preview_columns = [column for column in preview_columns if column in result.columns]
    content = f"""# 单日人工复核应用结果

本报告只处理本地模拟盘复核决策，不接实盘，不调用 QMT，不下真实订单。

## 应用汇总

{daily_summary.to_markdown(index=False)}

## 复核明细

{result[preview_columns].to_markdown(index=False) if not result.empty else "无人工复核项。"}

## 更新后的每日操作清单

{checklist.to_markdown(index=False) if not checklist.empty else "未更新 checklist。"}

## 安全限制

- `APPROVED` 只表示允许进入模拟观察，不代表可以实盘。
- `REJECTED` 表示跳过该模拟观察。
- `PENDING` 表示继续冻结计划。
- `live_order_enabled` 必须始终为 `False`。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    paths = resolve_input_paths(args)

    manual_review = read_csv(paths["manual_review"])
    decisions, template_created = load_or_create_decisions(manual_review, paths["decisions"])
    if args.apply_suggested_rejections:
        decisions = apply_suggested_rejections(decisions)
        decisions.to_csv(paths["decisions"], index=False, encoding="utf-8-sig")

    result = merge_decisions(manual_review, decisions)
    review_summary = build_summary(result, template_created=template_created, decisions_path=paths["decisions"])

    output_prefix = paths["output_prefix"]
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    base_summary_path = output_prefix.with_name(output_prefix.name + "_base_summary.csv")
    daily_summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    review_markdown_path = output_prefix.with_name(output_prefix.name + "_review.md")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    result.to_csv(detail_path, index=False, encoding="utf-8-sig")
    review_summary.to_csv(base_summary_path, index=False, encoding="utf-8-sig")
    write_review_markdown(review_markdown_path, review_summary, result)

    checklist = pd.DataFrame()
    checklist_updated = False
    if not args.no_update_checklist:
        checklist, checklist_updated = update_checklist(
            paths["checklist"],
            result,
            detail_path=detail_path,
            summary_path=daily_summary_path,
            markdown_path=markdown_path,
        )

    daily_summary = build_daily_apply_summary(
        result,
        review_summary,
        checklist_updated=checklist_updated,
        checklist_path=paths["checklist"],
    )
    daily_summary.to_csv(daily_summary_path, index=False, encoding="utf-8-sig")
    write_daily_markdown(markdown_path, daily_summary, result, checklist)

    print("单日人工复核决策应用完成：")
    print(f"- decisions: {paths['decisions']}")
    print(f"- summary: {daily_summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- markdown: {markdown_path}")
    print(f"- checklist: {paths['checklist']}")
    print(daily_summary.to_string(index=False))


if __name__ == "__main__":
    main()
