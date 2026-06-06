"""
应用模拟盘人工确认决策。

文件作用：
1. 读取批量模拟盘人工确认清单 manual_review.csv。
2. 生成可填写的人工确认决策模板。
3. 合并人工填写的 APPROVED / REJECTED / PENDING 决策。
4. 输出审核结果、汇总报告和 Markdown 报告。

本脚本只处理本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

VALID_DECISIONS = {"APPROVED", "REJECTED", "PENDING"}
SUGGESTION_POLICY_VERSION = "loss_overlay_watch_v1"
HIGH_LOSS_REJECT_THRESHOLD = -0.08
MEDIUM_LOSS_WATCH_THRESHOLD = -0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="应用模拟盘人工确认决策。")
    parser.add_argument(
        "--manual-review",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_manual_review.csv",
        help="批量模拟盘人工确认清单。",
    )
    parser.add_argument(
        "--decisions",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_manual_review_decisions.csv",
        help="人工填写的确认决策文件。不存在时会自动生成模板。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_manual_review_result",
        help="输出文件前缀。",
    )
    parser.add_argument(
        "--apply-suggested-rejections",
        action="store_true",
        help="把 suggested_decision=REJECTED 且 review_decision=PENDING 的记录写成 REJECTED。",
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


def read_csv(path: Path, **kwargs: Any) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    return pd.read_csv(path, low_memory=False, **kwargs)


def normalize_keys(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    for column in ["signal_date", "planned_order_date"]:
        if column in result.columns:
            result[column] = result[column].map(normalize_date)
    if "ts_code" in result.columns:
        result["ts_code"] = result["ts_code"].fillna("").astype(str)
    return result


def build_template(manual_review: pd.DataFrame) -> pd.DataFrame:
    base = normalize_keys(manual_review)
    template = apply_suggestions(base)
    template["review_decision"] = "PENDING"
    template["reviewer"] = ""
    template["review_time"] = ""
    template["review_note"] = ""
    template["decision_instruction"] = "review_decision 只能填写 APPROVED / REJECTED / PENDING。"
    return template


def load_or_create_decisions(manual_review: pd.DataFrame, decisions_path: Path) -> tuple[pd.DataFrame, bool]:
    if not decisions_path.exists():
        template = build_template(manual_review)
        decisions_path.parent.mkdir(parents=True, exist_ok=True)
        template.to_csv(decisions_path, index=False, encoding="utf-8-sig")
        return template, True
    decisions = apply_suggestions(normalize_keys(read_csv(decisions_path)))
    decisions.to_csv(decisions_path, index=False, encoding="utf-8-sig")
    return decisions, False


def apply_suggested_rejections(decisions: pd.DataFrame) -> pd.DataFrame:
    result = validate_decisions(apply_suggestions(normalize_keys(decisions)))
    mask = result["review_decision"].eq("PENDING") & result["suggested_decision"].eq("REJECTED")
    result.loc[mask, "review_decision"] = "REJECTED"
    result.loc[mask, "review_note"] = result.loc[mask, "suggestion_reason"]
    return result


def to_float(value: object) -> float | None:
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_loss_overlay_watch(value: object) -> bool:
    return "LOSS_OVERLAY_WATCH" in str(value)


def suggest_decision(row: pd.Series) -> dict[str, str]:
    historical_return = to_float(row.get("historical_reference_net_return"))
    risk_flags = row.get("risk_flags", "")

    if not has_loss_overlay_watch(risk_flags):
        return {
            "suggested_decision": "PENDING",
            "suggestion_confidence": "LOW",
            "suggestion_reason": "未命中 LOSS_OVERLAY_WATCH，保持人工复核。",
        }

    if historical_return is None:
        return {
            "suggested_decision": "PENDING",
            "suggestion_confidence": "LOW",
            "suggestion_reason": "命中 LOSS_OVERLAY_WATCH，但缺少历史参考收益，不能自动判断。",
        }

    if historical_return <= HIGH_LOSS_REJECT_THRESHOLD:
        return {
            "suggested_decision": "REJECTED",
            "suggestion_confidence": "HIGH",
            "suggestion_reason": "命中 LOSS_OVERLAY_WATCH，且历史参考亏损不高于 -8%，建议跳过模拟观察。",
        }

    if historical_return <= MEDIUM_LOSS_WATCH_THRESHOLD:
        return {
            "suggested_decision": "PENDING",
            "suggestion_confidence": "MEDIUM",
            "suggestion_reason": "命中 LOSS_OVERLAY_WATCH，历史参考亏损在 -5% 到 -8% 区间，建议重点复核。",
        }

    if historical_return < 0:
        return {
            "suggested_decision": "PENDING",
            "suggestion_confidence": "MEDIUM",
            "suggestion_reason": "命中 LOSS_OVERLAY_WATCH，历史参考收益为负，建议保留人工复核。",
        }

    return {
        "suggested_decision": "PENDING",
        "suggestion_confidence": "LOW",
        "suggestion_reason": "命中 LOSS_OVERLAY_WATCH，但历史参考未亏损，仍需人工确认后只进入模拟观察。",
    }


def apply_suggestions(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    if result.empty:
        for column in ["suggested_decision", "suggestion_confidence", "suggestion_reason", "suggestion_policy_version"]:
            if column not in result.columns:
                result[column] = pd.Series(dtype=str)
        return result

    suggestions = result.apply(suggest_decision, axis=1, result_type="expand")
    result["suggested_decision"] = suggestions["suggested_decision"]
    result["suggestion_confidence"] = suggestions["suggestion_confidence"]
    result["suggestion_reason"] = suggestions["suggestion_reason"]
    result["suggestion_policy_version"] = SUGGESTION_POLICY_VERSION
    return result


def validate_decisions(decisions: pd.DataFrame) -> pd.DataFrame:
    result = decisions.copy()
    if "review_decision" not in result.columns:
        result["review_decision"] = "PENDING"
    result["review_decision"] = result["review_decision"].fillna("PENDING").astype(str).str.upper().str.strip()
    invalid = sorted(set(result["review_decision"]) - VALID_DECISIONS)
    if invalid:
        raise ValueError(f"review_decision 存在非法值: {invalid}，只允许 {sorted(VALID_DECISIONS)}")
    for column in ["reviewer", "review_time", "review_note"]:
        if column not in result.columns:
            result[column] = ""
        result[column] = result[column].fillna("").astype(str)
    return result


def merge_decisions(manual_review: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    base = normalize_keys(manual_review)
    decisions = apply_suggestions(validate_decisions(normalize_keys(decisions)))
    required = {"signal_date", "planned_order_date", "ts_code"}
    missing_manual = sorted(required - set(base.columns))
    missing_decisions = sorted(required - set(decisions.columns))
    if missing_manual:
        raise RuntimeError(f"人工确认清单缺少字段: {missing_manual}")
    if missing_decisions:
        raise RuntimeError(f"决策文件缺少字段: {missing_decisions}")

    decision_columns = [
        "signal_date",
        "planned_order_date",
        "ts_code",
        "review_decision",
        "reviewer",
        "review_time",
        "review_note",
        "suggested_decision",
        "suggestion_confidence",
        "suggestion_reason",
        "suggestion_policy_version",
    ]
    merged = base.merge(
        decisions[decision_columns],
        on=["signal_date", "planned_order_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    merged["review_decision"] = merged["review_decision"].fillna("PENDING")
    merged["reviewer"] = merged["reviewer"].fillna("")
    merged["review_time"] = merged["review_time"].fillna("")
    merged["review_note"] = merged["review_note"].fillna("")
    for column in ["suggested_decision", "suggestion_confidence", "suggestion_reason", "suggestion_policy_version"]:
        if column not in merged.columns:
            merged[column] = ""
        merged[column] = merged[column].fillna("").astype(str)
    merged["final_review_status"] = merged["review_decision"].map(resolve_final_status)
    merged["paper_observation_allowed"] = merged["review_decision"].eq("APPROVED")
    merged["live_order_enabled"] = False
    return merged


def resolve_final_status(decision: str) -> str:
    if decision == "APPROVED":
        return "MANUAL_APPROVED_FOR_PAPER_OBSERVATION"
    if decision == "REJECTED":
        return "MANUAL_REJECTED_SKIP_OBSERVATION"
    return "PENDING_MANUAL_REVIEW"


def build_summary(result: pd.DataFrame, template_created: bool, decisions_path: Path) -> pd.DataFrame:
    decision_counts = result["review_decision"].value_counts().to_dict() if not result.empty else {}
    suggestion_counts = result["suggested_decision"].value_counts().to_dict() if "suggested_decision" in result.columns else {}
    return pd.DataFrame(
        [
            {
                "manual_review_count": int(len(result)),
                "approved_count": int(decision_counts.get("APPROVED", 0)),
                "rejected_count": int(decision_counts.get("REJECTED", 0)),
                "pending_count": int(decision_counts.get("PENDING", 0)),
                "suggested_approved_count": int(suggestion_counts.get("APPROVED", 0)),
                "suggested_rejected_count": int(suggestion_counts.get("REJECTED", 0)),
                "suggested_pending_count": int(suggestion_counts.get("PENDING", 0)),
                "paper_observation_allowed_count": int(result.get("paper_observation_allowed", pd.Series(dtype=bool)).sum()),
                "template_created": bool(template_created),
                "decisions_path": str(decisions_path),
                "live_order_enabled": False,
            }
        ]
    )


def write_markdown(path: Path, summary: pd.DataFrame, result: pd.DataFrame) -> None:
    preview_columns = [
        "signal_date",
        "planned_order_date",
        "ts_code",
        "name",
        "review_decision",
        "suggested_decision",
        "suggestion_confidence",
        "suggestion_reason",
        "final_review_status",
        "paper_observation_allowed",
        "risk_flags",
        "planned_amount_by_equity",
        "review_note",
    ]
    preview_columns = [column for column in preview_columns if column in result.columns]
    content = f"""# 人工确认决策结果

本报告只处理本地人工确认 CSV，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 明细

{result[preview_columns].to_markdown(index=False) if not result.empty else "无人工确认项。"}

## 填写说明

- `APPROVED`：允许进入模拟买入观察，不代表可以实盘。
- `REJECTED`：跳过该模拟观察。
- `PENDING`：未确认，不能进入实盘或半自动流程。
- `suggested_decision` 只是基于历史亏损叠加标签的保守建议，不会自动覆盖 `review_decision`。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    manual_review_path = resolve_path(args.manual_review)
    decisions_path = resolve_path(args.decisions)
    output_prefix = resolve_path(args.output_prefix)

    manual_review = read_csv(manual_review_path)
    decisions, template_created = load_or_create_decisions(manual_review, decisions_path)
    if args.apply_suggested_rejections:
        decisions = apply_suggested_rejections(decisions)
        decisions.to_csv(decisions_path, index=False, encoding="utf-8-sig")
    result = merge_decisions(manual_review, decisions)
    summary = build_summary(result, template_created=template_created, decisions_path=decisions_path)

    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    result_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")
    result.to_csv(result_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, result)

    print("人工确认决策处理完成：")
    print(f"- decisions: {decisions_path}")
    print(f"- summary: {summary_path}")
    print(f"- detail: {result_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
