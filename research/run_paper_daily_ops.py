"""
运行模拟盘每日操作台。

文件作用：
1. 调用单日模拟盘流程，生成候选、计划委托、人工确认清单、成交和资金更新。
2. 汇总当天操作状态，输出每日操作清单。
3. 明确标记是否需要人工复核、是否仅计划观察、是否存在历史模拟成交闭环。
4. 全程只读写本地 CSV，不接实盘，不调用 QMT，不下真实订单。
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

from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行模拟盘每日操作台。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument(
        "--runtime-config",
        default="config/config.json",
        help="运行时通用配置文件路径，仅用于日志配置。",
    )
    parser.add_argument(
        "--signal-date",
        default=None,
        help="信号日期，格式 YYYYMMDD。不传则使用当前策略过滤后有候选的最新日期。",
    )
    parser.add_argument("--top-n", type=int, default=None, help="候选输出数量，不传则读取配置。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/daily_ops/a_clean_exclude_star_prev0_3_bj",
        help="每日操作台输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def to_float(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def setup_runtime_logger(runtime_config_path: str | Path) -> None:
    runtime_config = load_json_config(runtime_config_path)
    logging_config = runtime_config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )


def run_daily_flow(args: argparse.Namespace) -> tuple[dict[str, Path], str]:
    outputs = PaperDailyFlowRunner(strategy_config_path=args.strategy_config).run(
        signal_date=args.signal_date,
        top_n=args.top_n,
    )
    summary = read_csv_if_exists(outputs["summary"])
    if not summary.empty and "signal_date" in summary.columns:
        return outputs, normalize_date(summary["signal_date"].iloc[0])
    return outputs, str(args.signal_date or "latest")


def build_ops_checklist(outputs: dict[str, Path], signal_date: str) -> pd.DataFrame:
    summary = read_csv_if_exists(outputs["summary"])
    candidates = read_csv_if_exists(outputs["candidates"])
    planned_orders = read_csv_if_exists(outputs["planned_orders"])
    manual_review = read_csv_if_exists(outputs["manual_review"])
    executions = read_csv_if_exists(outputs["executions"])
    positions = read_csv_if_exists(outputs["positions"])
    equity_update = read_csv_if_exists(outputs["equity_update"])

    summary_row = summary.iloc[0] if not summary.empty else pd.Series(dtype=object)
    selected = candidates[candidates.get("planned_action", pd.Series(dtype=str)).astype(str) == "PLAN_BUY_T1_OPEN"]
    planned = planned_orders.iloc[0] if not planned_orders.empty else pd.Series(dtype=object)
    selected_row = selected.iloc[0] if not selected.empty else pd.Series(dtype=object)

    review_decision_path = build_review_decision_template(outputs, signal_date, manual_review)
    review_decision_status = resolve_review_decision_status(review_decision_path)
    operation_status = resolve_operation_status(summary_row, manual_review, positions)
    operation_status = apply_review_decision_to_operation_status(operation_status, review_decision_status)
    next_action = resolve_next_action(operation_status)
    rows = [
        {
            "signal_date": signal_date,
            "operation_status": operation_status,
            "next_action": next_action,
            "candidate_count": int(to_float(summary_row.get("candidate_count", len(candidates)))),
            "selected_count": int(to_float(summary_row.get("selected_count", len(selected)))),
            "planned_order_count": int(to_float(summary_row.get("planned_order_count", len(planned_orders)))),
            "manual_review_required": to_bool(summary_row.get("manual_review_required", False)),
            "manual_review_count": int(len(manual_review)),
            "review_decision_path": str(review_decision_path) if review_decision_path else "",
            "review_decision_status": review_decision_status,
            "paper_observation_allowed": review_decision_status == "APPROVED",
            "historical_execution_found": to_bool(summary_row.get("historical_execution_found", False)),
            "top_ts_code": str(summary_row.get("top_ts_code", selected_row.get("ts_code", ""))),
            "top_name": str(summary_row.get("top_name", selected_row.get("name", ""))),
            "risk_flags": str(summary_row.get("top_risk_flags", selected_row.get("risk_flags", ""))),
            "planned_order_date": str(planned.get("planned_order_date", "")),
            "planned_position_pct": to_float(planned.get("planned_position_pct", 0.0)),
            "planned_amount_by_equity": to_float(planned.get("planned_amount_by_equity", 0.0)),
            "reference_price": to_float(planned.get("reference_price", 0.0)),
            "round_lot_shares": int(to_float(planned.get("round_lot_shares", 0))),
            "execution_event_count": int(len(executions)),
            "position_update_count": int(len(positions)),
            "equity_update_count": int(len(equity_update)),
            "live_order_enabled": False,
            "safety_note": "只允许模拟观察；未完成人工复核、分钟K和盘口验证前，不允许实盘。",
        }
    ]
    return pd.DataFrame(rows)


def build_review_decision_template(
    outputs: dict[str, Path],
    signal_date: str,
    manual_review: pd.DataFrame,
) -> Path | None:
    if manual_review.empty:
        return None
    manual_review_path = outputs.get("manual_review")
    if manual_review_path is None:
        return None
    decision_path = manual_review_path.with_name(manual_review_path.stem + "_decisions.csv")
    if decision_path.exists():
        return decision_path

    template = manual_review.copy()
    template["review_decision"] = "PENDING"
    template["reviewer"] = ""
    template["review_time"] = ""
    template["review_note"] = ""
    template["decision_instruction"] = "review_decision 只能填写 APPROVED / REJECTED / PENDING。"
    template["paper_observation_allowed"] = False
    template["live_order_enabled"] = False
    template.to_csv(decision_path, index=False, encoding="utf-8-sig")
    return decision_path


def resolve_review_decision_status(decision_path: Path | None) -> str:
    if decision_path is None:
        return "NOT_REQUIRED"
    decisions = read_csv_if_exists(decision_path)
    if decisions.empty or "review_decision" not in decisions.columns:
        return "PENDING"
    decision_values = decisions["review_decision"].fillna("PENDING").astype(str).str.upper().str.strip()
    if decision_values.eq("APPROVED").all():
        return "APPROVED"
    if decision_values.eq("REJECTED").all():
        return "REJECTED"
    if decision_values.isin({"APPROVED", "REJECTED"}).all():
        return "MIXED_FINAL"
    return "PENDING"


def apply_review_decision_to_operation_status(operation_status: str, review_decision_status: str) -> str:
    if operation_status != "REVIEW_REQUIRED_PLAN_ONLY":
        return operation_status
    if review_decision_status == "APPROVED":
        return "MANUAL_APPROVED_PAPER_OBSERVATION_ONLY"
    if review_decision_status == "REJECTED":
        return "MANUAL_REJECTED_SKIP_OBSERVATION"
    if review_decision_status == "MIXED_FINAL":
        return "MANUAL_REVIEW_MIXED_FINAL"
    return operation_status


def resolve_operation_status(
    summary_row: pd.Series,
    manual_review: pd.DataFrame,
    positions: pd.DataFrame,
) -> str:
    if summary_row.empty:
        return "NO_SUMMARY"
    if int(to_float(summary_row.get("selected_count", 0))) == 0:
        return "NO_SELECTED"
    if not manual_review.empty or to_bool(summary_row.get("manual_review_required", False)):
        return "REVIEW_REQUIRED_PLAN_ONLY"
    if to_bool(summary_row.get("historical_execution_found", False)):
        return "HISTORICAL_SIM_FILLED"
    pending = positions.get("position_status", pd.Series(dtype=str)).astype(str).eq("PLANNED_OR_PENDING").any()
    if pending:
        return "PLAN_ONLY_PENDING"
    return "PLAN_ONLY"


def resolve_next_action(operation_status: str) -> str:
    if operation_status == "MANUAL_APPROVED_PAPER_OBSERVATION_ONLY":
        return "人工复核已通过，只允许进入模拟观察，不进入实盘。"
    if operation_status == "MANUAL_REJECTED_SKIP_OBSERVATION":
        return "人工复核已拒绝，跳过本次模拟观察。"
    if operation_status == "MANUAL_REVIEW_MIXED_FINAL":
        return "人工复核存在批准和拒绝混合结果，按明细逐条处理，不进入实盘。"
    if operation_status == "REVIEW_REQUIRED_PLAN_ONLY":
        return "先人工复核；通过后只进入模拟观察，不进入实盘。"
    if operation_status == "HISTORICAL_SIM_FILLED":
        return "复盘历史模拟成交闭环，检查买入、卖出、收益、回撤是否合理。"
    if operation_status == "PLAN_ONLY_PENDING":
        return "保留计划，等待后续成交数据或人工模拟结果回填。"
    if operation_status in {"NO_SELECTED", "NO_SUMMARY"}:
        return "今日不生成模拟买入计划。"
    return "只生成模拟计划，等待人工确认和后续数据验证。"


def build_failure_checklist(signal_date: str, error: Exception) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_date": signal_date or "",
                "operation_status": "FAILED_OR_NO_CANDIDATE",
                "next_action": "检查该日期是否已有本地数据、是否满足策略条件；失败时不生成任何交易计划。",
                "candidate_count": 0,
                "selected_count": 0,
                "planned_order_count": 0,
                "manual_review_required": False,
                "manual_review_count": 0,
                "review_decision_path": "",
                "review_decision_status": "NOT_REQUIRED",
                "paper_observation_allowed": False,
                "historical_execution_found": False,
                "top_ts_code": "",
                "top_name": "",
                "risk_flags": "",
                "planned_order_date": "",
                "planned_position_pct": 0.0,
                "planned_amount_by_equity": 0.0,
                "reference_price": 0.0,
                "round_lot_shares": 0,
                "execution_event_count": 0,
                "position_update_count": 0,
                "equity_update_count": 0,
                "live_order_enabled": False,
                "safety_note": f"流程失败或无候选：{error}",
            }
        ]
    )


def write_markdown(path: Path, checklist: pd.DataFrame, outputs: dict[str, Path]) -> None:
    output_rows = [{"name": name, "path": str(file_path)} for name, file_path in outputs.items()]
    outputs_table = pd.DataFrame(output_rows)
    content = f"""# 模拟盘每日操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

{checklist.to_markdown(index=False)}

## 人工复核决策

如果 `review_decision_status=PENDING`，先打开 `review_decision_path`，把 `review_decision` 填为 `APPROVED` / `REJECTED` / `PENDING`，并填写 `review_note`。

## 单日流程输出文件

{outputs_table.to_markdown(index=False) if not outputs_table.empty else "无单日流程输出。"}

## 执行限制

- `live_order_enabled` 必须为 `False`。
- `REVIEW_REQUIRED_PLAN_ONLY` 只能进入人工复核，不能直接买入。
- 当前仍未完成分钟 K、盘口五档、集合竞价和模拟盘连续运行验证，不允许实盘。
"""
    path.write_text(content, encoding="utf-8")


def output_paths(output_prefix: Path, signal_date: str) -> dict[str, Path]:
    normalized_signal_date = signal_date or "unknown"
    return {
        "checklist": output_prefix.with_name(output_prefix.name + f"_{normalized_signal_date}_checklist.csv"),
        "markdown": output_prefix.with_name(output_prefix.name + f"_{normalized_signal_date}.md"),
    }


def main() -> None:
    args = parse_args()
    setup_runtime_logger(args.runtime_config)
    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, Path] = {}
    try:
        outputs, signal_date = run_daily_flow(args)
        checklist = build_ops_checklist(outputs, signal_date)
    except Exception as exc:
        signal_date = str(args.signal_date or "latest")
        checklist = build_failure_checklist(signal_date, exc)

    paths = output_paths(output_prefix, signal_date)
    checklist.to_csv(paths["checklist"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], checklist, outputs)

    print("模拟盘每日操作台完成：")
    print(f"- checklist: {paths['checklist']}")
    print(f"- markdown: {paths['markdown']}")
    for name, path in outputs.items():
        print(f"- {name}: {path}")
    print(checklist.to_string(index=False))


if __name__ == "__main__":
    main()
