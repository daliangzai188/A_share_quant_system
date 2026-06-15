"""
运行 A严格策略 + B0018过滤版每日模拟盘操作台。

文件作用：
1. T 日收盘后先生成 A 严格策略候选。
2. 只有 A 无选中标的时，才启用 B0018 备用策略。
3. B 选中后先执行配置里的风险过滤，命中过滤则跳过，不寻找下一只替代。
4. 输出每日候选、计划委托、人工复核清单、历史成交参考和操作清单。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
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

from scripts.audit_backup_strategy_b import find_a_audit_row, replay_selected_b
from scripts.run_paper_ab_filtered_observation_window import (
    configured_b_conditions,
    condition_text,
    reject_b_risk_mask,
)
from scripts.search_paper_backup_strategy_b import (
    apply_and_rank,
    backup_config,
    build_generator,
    normalize_date,
    selected_candidate,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 A+B filtered 每日模拟盘操作台。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时通用配置文件路径。")
    parser.add_argument("--signal-date", default=None, help="信号日期，格式 YYYYMMDD。不传则使用本地最新日期。")
    parser.add_argument("--top-n", type=int, default=None, help="候选输出数量，不传则读取配置。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_plus_c_hold3",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


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


def assert_safe_config(config: dict[str, Any]) -> None:
    safe_modes = {"paper", "simulation", "dry_run", "research"}
    trade_mode = str(config.get("trade_mode", "")).strip().lower()
    if trade_mode not in safe_modes:
        raise RuntimeError(f"拒绝运行 A+B 每日操作台：trade_mode 不是安全模式: {trade_mode}")
    for key in ["live_trading_enabled", "broker_adapter_enabled", "qmt_enabled"]:
        if bool(config.get(key, False)):
            raise RuntimeError(f"拒绝运行 A+B 每日操作台：{key}=true")
    ab_config = config.get("paper_ab_filtered_strategy", {})
    if bool(ab_config.get("allow_live_order", False)) or bool(ab_config.get("live_order_enabled", False)):
        raise RuntimeError("拒绝运行 A+B 每日操作台：paper_ab_filtered_strategy 存在实盘开关。")


def latest_signal_date(all_candidates: pd.DataFrame) -> str:
    dates = sorted(all_candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if not dates:
        raise RuntimeError("本地候选数据为空，无法识别最新信号日。")
    return dates[-1]


def empty_frame_like(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def selected_rows(output: pd.DataFrame, selected_action: str) -> pd.DataFrame:
    if output.empty:
        return pd.DataFrame()
    return output[output["planned_action"].astype(str) == selected_action].copy()


def build_selected_row(
    strategy_leg: str,
    selected: pd.Series,
    operation_status: str,
    selection_status: str,
    account_return: float = 0.0,
    return_source: str = "",
    execution_note: str = "",
) -> pd.DataFrame:
    row = selected.copy()
    row["strategy_leg"] = strategy_leg
    row["selection_status"] = selection_status
    row["operation_status"] = operation_status
    row["account_return"] = float(account_return)
    row["return_source"] = return_source
    row["execution_note"] = execution_note
    row["live_order_enabled"] = False
    return pd.DataFrame([row])


def manual_review_required(config: dict[str, Any], selected: pd.Series) -> bool:
    requirements = config.get("pre_paper_trade_requirements", {})
    if bool(requirements.get("must_review_each_trade", True)):
        return True
    risk_flags = str(selected.get("risk_flags", ""))
    return risk_flags not in {"", "无"}


def resolve_a_execution(
    audit: pd.DataFrame,
    signal_date: str,
    selected: pd.Series,
) -> tuple[str, float, str, str]:
    audit_row = find_a_audit_row(audit, signal_date, str(selected.get("ts_code", "")))
    if audit_row is None:
        return "PLAN_ONLY_PENDING", 0.0, "", "A 未找到历史审计成交，只保留模拟计划。"
    account_return = to_float(audit_row.get("dynamic_account_return", 0.0))
    note = "A 命中历史审计成交，收益仅用于复盘参考。"
    return "HISTORICAL_SIM_FILLED", account_return, "a_audit_dynamic_account_return", note


def resolve_b_execution(
    selected_b: pd.DataFrame,
    runtime_config_path: str | Path,
) -> tuple[pd.DataFrame, str, float, str, str]:
    selected_b = selected_b.copy()
    if "trade_date" not in selected_b.columns and "signal_date" in selected_b.columns:
        selected_b["trade_date"] = selected_b["signal_date"].map(normalize_date)
    replayed = replay_selected_b(selected_b, runtime_config_path)
    if replayed.empty:
        return replayed, "PLAN_ONLY_PENDING", 0.0, "", "B 未生成历史回放结果，只保留模拟计划。"
    row = replayed.iloc[0]
    if not to_bool(row.get("buy_executed", False)):
        return replayed, "BUY_REJECTED", 0.0, "b_conservative_daily_replay", str(row.get("buy_reject_reason", ""))
    if not to_bool(row.get("sell_executed", False)):
        return replayed, "SELL_UNRESOLVED", 0.0, "b_conservative_daily_replay", str(row.get("sell_reject_reason", ""))
    account_return = to_float(row.get("strict_account_return", 0.0))
    return replayed, "HISTORICAL_SIM_FILLED", account_return, "b_conservative_daily_replay", "B 日线保守成交回放完成。"


def configured_c_conditions(config: dict[str, Any]) -> list[dict[str, str]]:
    ab_config = config.get("paper_ab_filtered_strategy", {})
    c_config = ab_config.get("c_strategy", {})
    if not bool(c_config.get("enabled", False)):
        return []
    conditions = []
    for condition in c_config.get("conditions", []):
        conditions.append(
            {
                "column": str(condition["column"]),
                "value": str(condition["value"]),
            }
        )
    return conditions


def configured_c_exit_rule(config: dict[str, Any]) -> ReplayRule:
    c_config = config.get("paper_ab_filtered_strategy", {}).get("c_strategy", {})
    rule = c_config.get("exit_rule", {})
    if not rule:
        rule = {"rule_name": "fixed_t2_close", "max_hold_days": 2, "exit_price_field": "close"}
    return ReplayRule(
        rule_name=str(rule["rule_name"]),
        max_hold_days=int(rule["max_hold_days"]),
        exit_price_field=str(rule.get("exit_price_field", "close")),
        stop_loss=float(rule["stop_loss"]) if "stop_loss" in rule else None,
        take_profit=float(rule["take_profit"]) if "take_profit" in rule else None,
    )


def replay_selected_with_rule(
    selected: pd.DataFrame,
    runtime_config_path: str | Path,
    rule: ReplayRule,
) -> pd.DataFrame:
    selected = selected.copy()
    if selected.empty:
        return pd.DataFrame()
    if "trade_date" not in selected.columns and "signal_date" in selected.columns:
        selected["trade_date"] = selected["signal_date"].map(normalize_date)
    replay_engine = ConservativeTradeReplay(config_path=runtime_config_path)
    forward = replay_engine.load_forward_prices()
    samples = selected.merge(forward, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replayed = replay_engine.replay_rule(samples, rule)
    replayed["strict_account_return"] = pd.to_numeric(replayed["daily_return"], errors="coerce").fillna(0.0)
    replayed["strict_return_source"] = f"c_conservative_daily_replay_{rule.rule_name}"
    return replayed.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def resolve_c_execution(
    selected_c: pd.DataFrame,
    runtime_config_path: str | Path,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, str, float, str, str]:
    replay_rule = configured_c_exit_rule(config)
    replayed = replay_selected_with_rule(selected_c, runtime_config_path, replay_rule)
    if replayed.empty:
        return replayed, "PLAN_ONLY_PENDING", 0.0, "", "C 未生成历史回放结果，只保留模拟计划。"
    row = replayed.iloc[0]
    if not to_bool(row.get("buy_executed", False)):
        return replayed, "BUY_REJECTED", 0.0, row.get("strict_return_source", "c_conservative_daily_replay"), str(row.get("buy_reject_reason", ""))
    if not to_bool(row.get("sell_executed", False)):
        return replayed, "SELL_UNRESOLVED", 0.0, row.get("strict_return_source", "c_conservative_daily_replay"), str(row.get("sell_reject_reason", ""))
    account_return = to_float(row.get("strict_account_return", 0.0))
    note = f"C 日线保守成交回放完成，卖出规则={replay_rule.rule_name}。"
    return replayed, "HISTORICAL_SIM_FILLED", account_return, row.get("strict_return_source", "c_conservative_daily_replay"), note


def estimate_planned_order(config: dict[str, Any], selected: pd.Series, signal_date: str) -> pd.DataFrame:
    position = config.get("position", {})
    paper_trade = config.get("paper_trade", {})
    planned_equity = float(position.get("initial_cash", 500000))
    planned_position_pct = to_float(selected.get("planned_position_pct", position.get("target_position_pct", 0.8)))
    planned_amount = planned_equity * planned_position_pct
    reference_price = to_float(selected.get("historical_reference_next_open", 0.0))
    round_lot = int(paper_trade.get("round_lot_size", 100))
    estimated_shares = int(planned_amount // reference_price) if reference_price > 0 else 0
    round_lot_shares = estimated_shares - estimated_shares % round_lot if round_lot > 0 else estimated_shares
    return pd.DataFrame(
        [
            {
                "paper_order_id": f"AB-{signal_date}-{selected.get('strategy_leg', '')}-{selected.get('ts_code', '')}",
                "signal_date": signal_date,
                "strategy_leg": selected.get("strategy_leg", ""),
                "planned_order_date": selected.get("historical_reference_next_trade_date", ""),
                "side": "BUY",
                "ts_code": selected.get("ts_code", ""),
                "name": selected.get("name", ""),
                "planned_action": selected.get("planned_action", ""),
                "order_status": "PLAN_ONLY",
                "planned_position_pct": planned_position_pct,
                "planned_equity": planned_equity,
                "planned_amount_by_equity": planned_amount,
                "reference_price": reference_price,
                "estimated_shares": estimated_shares,
                "round_lot_shares": round_lot_shares,
                "risk_flags": selected.get("risk_flags", ""),
                "live_order_enabled": False,
            }
        ]
    )


def build_manual_review(config: dict[str, Any], selected: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    row = selected.iloc[0]
    if not manual_review_required(config, row):
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "signal_date": signal_date,
                "strategy_leg": row.get("strategy_leg", ""),
                "ts_code": row.get("ts_code", ""),
                "name": row.get("name", ""),
                "risk_flags": row.get("risk_flags", ""),
                "operation_status": "REVIEW_REQUIRED_PLAN_ONLY",
                "review_decision": "PENDING",
                "reviewer": "",
                "review_time": "",
                "review_note": "",
                "decision_instruction": "review_decision 只能填写 APPROVED / REJECTED / PENDING。",
                "paper_observation_allowed": False,
                "live_order_enabled": False,
            }
        ]
    )


def build_checklist(
    signal_date: str,
    selected: pd.DataFrame,
    planned_orders: pd.DataFrame,
    manual_review: pd.DataFrame,
    a_candidates: pd.DataFrame,
    b_candidates: pd.DataFrame,
    b_rejected: pd.DataFrame,
    c_candidates: pd.DataFrame,
    c_rejected: pd.DataFrame,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame(
            [
                {
                    "signal_date": signal_date,
                    "strategy_leg": "NONE",
                    "operation_status": "NO_SELECTED",
                    "next_action": "A/B/C均无可用候选，今日不生成模拟买入计划。",
                    "a_candidate_count": int(len(a_candidates)),
                    "b_candidate_count": int(len(b_candidates)),
                    "b_rejected_by_filter_count": int(len(b_rejected)),
                    "c_candidate_count": int(len(c_candidates)),
                    "c_rejected_by_filter_count": int(len(c_rejected)),
                    "selected_count": 0,
                    "planned_order_count": 0,
                    "manual_review_required": False,
                    "manual_review_count": 0,
                    "top_ts_code": "",
                    "top_name": "",
                    "account_return": 0.0,
                    "return_source": "",
                    "live_order_enabled": False,
                }
            ]
        )
    row = selected.iloc[0]
    needs_review = not manual_review.empty
    operation_status = str(row.get("operation_status", ""))
    if needs_review and operation_status == "HISTORICAL_SIM_FILLED":
        operation_status = "REVIEW_REQUIRED_PLAN_ONLY"
    next_action = "先人工复核；通过后只进入模拟观察，不进入实盘。" if needs_review else "只生成模拟计划，等待后续数据验证。"
    return pd.DataFrame(
        [
            {
                "signal_date": signal_date,
                "strategy_leg": row.get("strategy_leg", ""),
                "operation_status": operation_status,
                "selection_status": row.get("selection_status", ""),
                "next_action": next_action,
                "a_candidate_count": int(len(a_candidates)),
                "b_candidate_count": int(len(b_candidates)),
                "b_rejected_by_filter_count": int(len(b_rejected)),
                "c_candidate_count": int(len(c_candidates)),
                "c_rejected_by_filter_count": int(len(c_rejected)),
                "selected_count": 1,
                "planned_order_count": int(len(planned_orders)),
                "manual_review_required": needs_review,
                "manual_review_count": int(len(manual_review)),
                "top_ts_code": row.get("ts_code", ""),
                "top_name": row.get("name", ""),
                "risk_flags": row.get("risk_flags", ""),
                "planned_order_date": row.get("historical_reference_next_trade_date", ""),
                "planned_position_pct": to_float(row.get("planned_position_pct", 0.0)),
                "account_return": to_float(row.get("account_return", 0.0)),
                "return_source": row.get("return_source", ""),
                "execution_note": row.get("execution_note", ""),
                "paper_observation_allowed": False,
                "live_order_enabled": False,
                "safety_note": "A+B+C filtered 只允许模拟观察；未完成分钟K、盘口和连续模拟验证前，不允许实盘。",
            }
        ]
    )


def output_paths(output_prefix: Path, signal_date: str) -> dict[str, Path]:
    return {
        "a_candidates": output_prefix.with_name(output_prefix.name + f"_{signal_date}_a_candidates.csv"),
        "b_candidates": output_prefix.with_name(output_prefix.name + f"_{signal_date}_b_candidates.csv"),
        "b_rejected": output_prefix.with_name(output_prefix.name + f"_{signal_date}_b_rejected_by_filter.csv"),
        "c_candidates": output_prefix.with_name(output_prefix.name + f"_{signal_date}_c_candidates.csv"),
        "c_rejected": output_prefix.with_name(output_prefix.name + f"_{signal_date}_c_rejected_by_filter.csv"),
        "selected": output_prefix.with_name(output_prefix.name + f"_{signal_date}_selected.csv"),
        "planned_orders": output_prefix.with_name(output_prefix.name + f"_{signal_date}_planned_orders.csv"),
        "manual_review": output_prefix.with_name(output_prefix.name + f"_{signal_date}_manual_review.csv"),
        "execution_reference": output_prefix.with_name(output_prefix.name + f"_{signal_date}_execution_reference.csv"),
        "checklist": output_prefix.with_name(output_prefix.name + f"_{signal_date}_checklist.csv"),
        "markdown": output_prefix.with_name(output_prefix.name + f"_{signal_date}.md"),
    }


def write_markdown(path: Path, checklist: pd.DataFrame, selected: pd.DataFrame, paths: dict[str, Path]) -> None:
    output_rows = [{"name": name, "path": str(file_path)} for name, file_path in paths.items()]
    outputs = pd.DataFrame(output_rows)

    def table_text(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "无。"
        try:
            return frame.to_markdown(index=False)
        except ImportError:
            return frame.to_string(index=False)

    content = f"""# A+B+C filtered 每日模拟盘操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

{table_text(checklist)}

## 选中标的

{table_text(selected) if not selected.empty else "今日无选中标的。"}

## 输出文件

{table_text(outputs)}

## 执行限制

- A 优先；只有 A 无选中标的时才启用 B。
- C 只在 A/B 没有生成历史模拟成交时启用。
- B/C 命中 `risk_reject_rules` 时直接跳过，不寻找下一只替代。
- `live_order_enabled` 必须为 `False`。
- 当前仍未完成分钟 K、盘口五档、集合竞价和连续模拟盘验证，不允许实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    setup_runtime_logger(args.runtime_config)
    config = load_json_config(args.strategy_config)
    assert_safe_config(config)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    signal_date = normalize_date(args.signal_date) if args.signal_date else latest_signal_date(all_candidates)
    selected_action = config.get("paper_candidate", {}).get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    audit = PaperDailyFlowRunner(args.strategy_config).load_audit_trades(
        PROJECT_ROOT / config.get("paper_daily_flow", {}).get("input_audit_trades_path", "")
    )

    a_config = copy.deepcopy(config)
    a_generator = build_generator(args.strategy_config, a_config)
    a_filtered = a_generator.apply_strategy_filters(all_candidates)
    a_candidates = apply_and_rank(a_generator, a_filtered, signal_date, top_n=args.top_n)
    a_selected = selected_candidate(a_candidates, selected_action)

    b_candidates = pd.DataFrame()
    b_rejected = pd.DataFrame()
    c_candidates = pd.DataFrame()
    c_rejected = pd.DataFrame()
    execution_reference = pd.DataFrame()
    selected = pd.DataFrame()
    selection_status = ""

    if a_selected is not None:
        operation_status, account_return, return_source, note = resolve_a_execution(audit, signal_date, a_selected)
        selected = build_selected_row("A", a_selected, operation_status, "A_SELECTED_HAS_PRIORITY", account_return, return_source, note)
    else:
        b_conditions = configured_b_conditions(config)
        b_config = backup_config(config, b_conditions)
        b_generator = build_generator(args.strategy_config, b_config)
        b_filtered = b_generator.apply_strategy_filters(all_candidates)
        b_candidates = apply_and_rank(b_generator, b_filtered, signal_date, top_n=args.top_n)
        b_selected = selected_candidate(b_candidates, selected_action)
        if b_selected is None:
            selection_status = "A_NO_SELECTED_B_NO_SELECTED"
        else:
            b_selected_frame = pd.DataFrame([b_selected])
            rejected_mask = reject_b_risk_mask(b_selected_frame, config)
            if bool(rejected_mask.iloc[0]):
                b_rejected = b_selected_frame.copy()
                b_rejected["reject_reason"] = "B_SELECTED_HIT_RISK_REJECT_RULES"
                selection_status = "A_NO_SELECTED_B_RISK_FILTERED"
            else:
                execution_reference, operation_status, account_return, return_source, note = resolve_b_execution(
                    b_selected_frame,
                    args.runtime_config,
                )
                selected = build_selected_row(
                    "B",
                    b_selected,
                    operation_status,
                    f"A_NO_SELECTED_B_SELECTED:{condition_text(b_conditions)}",
                    account_return,
                    return_source,
                    note,
                )
                if operation_status != "HISTORICAL_SIM_FILLED":
                    selection_status = f"A_NO_SELECTED_B_NOT_FILLED:{operation_status}"

        if selected.empty or (
            not selected.empty
            and str(selected.iloc[0].get("strategy_leg", "")) == "B"
            and str(selected.iloc[0].get("operation_status", "")) != "HISTORICAL_SIM_FILLED"
        ):
            c_conditions = configured_c_conditions(config)
            if c_conditions:
                c_config = backup_config(config, c_conditions)
                c_generator = build_generator(args.strategy_config, c_config)
                c_filtered = c_generator.apply_strategy_filters(all_candidates)
                c_candidates = apply_and_rank(c_generator, c_filtered, signal_date, top_n=args.top_n)
                c_selected = selected_candidate(c_candidates, selected_action)
                if c_selected is not None:
                    c_selected_frame = pd.DataFrame([c_selected])
                    c_rejected_mask = reject_b_risk_mask(c_selected_frame, config)
                    if bool(c_rejected_mask.iloc[0]):
                        c_rejected = c_selected_frame.copy()
                        c_rejected["reject_reason"] = "C_SELECTED_HIT_RISK_REJECT_RULES"
                        if selected.empty:
                            selection_status = "A_B_NO_FILLED_C_RISK_FILTERED"
                    else:
                        c_reference, c_status, c_return, c_source, c_note = resolve_c_execution(
                            c_selected_frame,
                            args.runtime_config,
                            config,
                        )
                        execution_reference = c_reference
                        selected = build_selected_row(
                            "C",
                            c_selected,
                            c_status,
                            f"A_B_NO_FILLED_C_SELECTED:{condition_text(c_conditions)}",
                            c_return,
                            c_source,
                            c_note,
                        )
                elif selected.empty:
                    selection_status = "A_B_NO_FILLED_C_NO_SELECTED"

    planned_orders = estimate_planned_order(config, selected.iloc[0], signal_date) if not selected.empty else pd.DataFrame()
    manual_review = build_manual_review(config, selected, signal_date)
    checklist = build_checklist(
        signal_date,
        selected,
        planned_orders,
        manual_review,
        a_candidates,
        b_candidates,
        b_rejected,
        c_candidates,
        c_rejected,
    )
    if selection_status and selected.empty:
        checklist["selection_status"] = selection_status

    paths = output_paths(output_prefix, signal_date)
    a_candidates.to_csv(paths["a_candidates"], index=False, encoding="utf-8-sig")
    b_candidates.to_csv(paths["b_candidates"], index=False, encoding="utf-8-sig")
    b_rejected.to_csv(paths["b_rejected"], index=False, encoding="utf-8-sig")
    c_candidates.to_csv(paths["c_candidates"], index=False, encoding="utf-8-sig")
    c_rejected.to_csv(paths["c_rejected"], index=False, encoding="utf-8-sig")
    selected.to_csv(paths["selected"], index=False, encoding="utf-8-sig")
    planned_orders.to_csv(paths["planned_orders"], index=False, encoding="utf-8-sig")
    manual_review.to_csv(paths["manual_review"], index=False, encoding="utf-8-sig")
    execution_reference.to_csv(paths["execution_reference"], index=False, encoding="utf-8-sig")
    checklist.to_csv(paths["checklist"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], checklist, selected, paths)

    print("A+B+C filtered 每日模拟盘操作台完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")
    print(checklist.to_string(index=False))


if __name__ == "__main__":
    main()
