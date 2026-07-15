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
from scripts.run_strategy_e2_signal import next_trade_day
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
from src.utils.config import load_json_config, mkdir_p
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
    parser.add_argument("--input-trades-path", help="候选输入表。实盘流水线传 live_limit_up_fill_scored.csv。")
    parser.add_argument("--fill-scored-path", help="数据质量检查用打分表。默认读取配置中的研究表。")
    parser.add_argument("--market-emotion-features-path", help="实盘市场情绪特征表。")
    parser.add_argument("--theme-heat-features-path", help="实盘题材热度特征表。")
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


def operation_status_desc(status: object) -> str:
    text = str(status)
    mapping = {
        "DATA_QUALITY_BLOCKED": "数据口径不满足历史策略要求，禁止生成开仓计划。",
        "NO_SELECTED": "A/B/C 策略均未选出可执行标的。",
        "HISTORICAL_SIM_FILLED": "历史保守成交模型判定可成交，仅作为模拟参考。",
        "PLAN_ONLY_PENDING": "只生成模拟观察计划，未确认历史成交。",
        "REVIEW_REQUIRED_PLAN_ONLY": "需要人工复核，只能进入模拟观察。",
        "BUY_REJECTED": "保守成交模型判定买入不可成交。",
        "SELL_UNRESOLVED": "保守成交模型判定卖出未解决或需顺延。",
    }
    return mapping.get(text, text)


def selection_status_desc(status: object) -> str:
    text = str(status)
    mapping = {
        "OK": "数据检查通过。",
        "LIMIT_UP_FILL_SCORED_MISSING": "成交概率打标文件不存在。",
        "SIGNAL_DATE_LIMIT_ROWS_MISSING": "成交概率打标文件中没有该信号日记录。",
        "LIMIT_DATA_QUALITY_NOT_COMPATIBLE": "涨停池不是完整 limit_list_d 历史口径。",
        "REQUIRED_COLUMNS_MISSING": "缺少策略必需字段。",
        "REQUIRED_COLUMNS_EMPTY": "策略必需字段全为空。",
        "A_SELECTED_HAS_PRIORITY": "A 主策略已选中，优先使用 A。",
        "A_NO_SELECTED_B_NO_SELECTED": "A 无候选，B 备用策略也无候选。",
        "A_NO_SELECTED_B_RISK_FILTERED": "A 无候选，B 首选标的命中风险过滤。",
        "A_B_NO_FILLED_C_RISK_FILTERED": "A/B 未形成可成交标的，C 首选标的命中风险过滤。",
        "A_B_NO_FILLED_C_NO_SELECTED": "A/B 未形成可成交标的，C 也无候选。",
    }
    if text.startswith("A_NO_SELECTED_B_SELECTED:"):
        return "A 无候选，B 备用策略选中；冒号后为命中条件。"
    if text.startswith("A_NO_SELECTED_B_NOT_FILLED:"):
        return "A 无候选，B 选中但保守成交模型未判定成交。"
    if text.startswith("A_B_NO_FILLED_C_SELECTED:"):
        return "A/B 未形成可成交标的，C 补位策略选中；冒号后为命中条件。"
    return mapping.get(text, text)


def bool_desc(value: object, true_text: str, false_text: str) -> str:
    return true_text if to_bool(value) else false_text


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


def validate_signal_data_quality(
    runtime_config_path: str | Path,
    strategy_config: dict[str, Any],
    signal_date: str,
    fill_scored_path: str | Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    runtime_config = load_json_config(runtime_config_path)
    fill_path = resolve_path(fill_scored_path) if fill_scored_path else PROJECT_ROOT / runtime_config.get("fill_model", {}).get(
        "output_limit_up_fill_scored_path",
        "data/processed/limit_up_fill_scored.csv",
    )
    requirements = strategy_config.get("paper_ab_filtered_strategy", {}).get("data_quality_requirements", {})
    required_quality = str(requirements.get("required_limit_data_quality", "full"))
    required_columns = [str(column) for column in requirements.get("required_columns", [])]

    if not fill_path.exists():
        return False, {
            "reason": "LIMIT_UP_FILL_SCORED_MISSING",
            "detail": f"成交概率打标文件不存在: {fill_path}",
            "limit_data_quality": "missing",
            "limit_data_source": "missing",
            "row_count": 0,
        }

    data = pd.read_csv(fill_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    daily = data[data["trade_date"].map(normalize_date) == signal_date].copy()
    if daily.empty:
        return False, {
            "reason": "SIGNAL_DATE_LIMIT_ROWS_MISSING",
            "detail": f"limit_up_fill_scored.csv 没有 {signal_date} 记录。",
            "limit_data_quality": "missing",
            "limit_data_source": "missing",
            "row_count": 0,
        }

    quality = (
        daily.get("limit_data_quality", pd.Series("full", index=daily.index))
        .fillna("full")
        .astype(str)
    )
    source = (
        daily.get("limit_data_source", pd.Series("limit_list_d", index=daily.index))
        .fillna("limit_list_d")
        .astype(str)
    )
    compatible = (
        daily.get("strategy_compatible", pd.Series(True, index=daily.index))
        .fillna(True)
        .astype(str)
        .str.lower()
        .isin({"true", "1"})
    )
    quality_ok = quality.eq(required_quality).all() and compatible.all()
    missing_columns = [column for column in required_columns if column not in daily.columns]
    incomplete_columns = [
        column
        for column in required_columns
        if column in daily.columns and daily[column].isna().all()
    ]
    ok = bool(quality_ok and not missing_columns and not incomplete_columns)
    reason = "OK"
    detail = "数据口径满足策略要求。"
    if not quality_ok:
        reason = "LIMIT_DATA_QUALITY_NOT_COMPATIBLE"
        detail = f"{signal_date} 数据口径为 {','.join(sorted(quality.unique()))}，来源 {','.join(sorted(source.unique()))}，不满足 {required_quality}。"
    elif missing_columns:
        reason = "REQUIRED_COLUMNS_MISSING"
        detail = f"缺少策略必需字段: {missing_columns}"
    elif incomplete_columns:
        reason = "REQUIRED_COLUMNS_EMPTY"
        detail = f"策略必需字段全为空: {incomplete_columns}"

    return ok, {
        "reason": reason,
        "detail": detail,
        "limit_data_quality": ",".join(sorted(quality.unique())),
        "limit_data_source": ",".join(sorted(source.unique())),
        "row_count": int(len(daily)),
        "strategy_compatible": bool(compatible.all()),
        "missing_columns": ",".join(missing_columns),
        "incomplete_columns": ",".join(incomplete_columns),
    }


def write_data_quality_block_outputs(
    output_prefix: Path,
    signal_date: str,
    quality: dict[str, Any],
) -> dict[str, Path]:
    paths = output_paths(output_prefix, signal_date)
    empty = pd.DataFrame()
    checklist = pd.DataFrame(
        [
            {
                "signal_date": signal_date,
                "strategy_leg": "NONE",
                "operation_status": "DATA_QUALITY_BLOCKED",
                "selection_status": quality.get("reason", ""),
                "next_action": "当天涨停池口径不满足历史最佳策略字段要求，不生成开仓计划；等待完整 limit_list_d 或人工确认新口径。",
                "a_candidate_count": 0,
                "b_candidate_count": 0,
                "b_rejected_by_filter_count": 0,
                "c_candidate_count": 0,
                "c_rejected_by_filter_count": 0,
                "selected_count": 0,
                "planned_order_count": 0,
                "manual_review_required": False,
                "manual_review_count": 0,
                "top_ts_code": "",
                "top_name": "",
                "account_return": 0.0,
                "return_source": "",
                "live_order_enabled": False,
                "limit_data_quality": quality.get("limit_data_quality", ""),
                "limit_data_source": quality.get("limit_data_source", ""),
                "limit_row_count": quality.get("row_count", 0),
                "data_quality_detail": quality.get("detail", ""),
            }
        ]
    )
    checklist = enrich_checklist(checklist)
    for name, path in paths.items():
        if name == "checklist":
            checklist.to_csv(path, index=False, encoding="utf-8-sig")
        elif name == "markdown":
            write_markdown(path, checklist, empty, paths)
        else:
            empty.to_csv(path, index=False, encoding="utf-8-sig")
    return paths


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


def risk_reject_detail(frame: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=str)
    ab_config = config.get("paper_ab_filtered_strategy", {})
    rules = ab_config.get("b_strategy", {}).get("risk_reject_rules", [])
    details: list[str] = []
    for _, row in frame.iterrows():
        hits: list[str] = []
        risk_flags = str(row.get("risk_flags", ""))
        for rule in rules:
            rule_name = str(rule.get("name", "unnamed_rule"))
            rule_desc = str(rule.get("description", rule_name))
            for keyword in rule.get("risk_flags_contains_any", []):
                keyword_text = str(keyword)
                if keyword_text and keyword_text in risk_flags:
                    hits.append(f"{rule_name}: {rule_desc}；risk_flags包含“{keyword_text}”")
            for condition in rule.get("numeric_conditions", []):
                column = str(condition.get("column", ""))
                operator = str(condition.get("operator", "==")).strip()
                threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
                value = pd.to_numeric(row.get(column, pd.NA), errors="coerce")
                if not column or pd.isna(threshold) or pd.isna(value):
                    continue
                matched = (
                    (operator == ">=" and float(value) >= float(threshold))
                    or (operator == ">" and float(value) > float(threshold))
                    or (operator == "<=" and float(value) <= float(threshold))
                    or (operator == "<" and float(value) < float(threshold))
                    or (operator == "==" and float(value) == float(threshold))
                )
                if matched:
                    hits.append(
                        f"{rule_name}: {rule_desc}；{column}={float(value):g} {operator} {float(threshold):g}"
                    )
            for group in rule.get("compound_conditions", []):
                group_parts: list[str] = []
                group_matched = True
                for condition in group:
                    column = str(condition.get("column", ""))
                    operator = str(condition.get("operator", "==")).strip()
                    threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
                    value = pd.to_numeric(row.get(column, pd.NA), errors="coerce")
                    if not column or pd.isna(threshold) or pd.isna(value):
                        group_matched = False
                        break
                    cond_ok = (
                        (operator == ">=" and float(value) >= float(threshold))
                        or (operator == ">" and float(value) > float(threshold))
                        or (operator == "<=" and float(value) <= float(threshold))
                        or (operator == "<" and float(value) < float(threshold))
                        or (operator == "==" and float(value) == float(threshold))
                    )
                    if not cond_ok:
                        group_matched = False
                        break
                    group_parts.append(f"{column}={float(value):g} {operator} {float(threshold):g}")
                if group_matched and group_parts:
                    hits.append(f"{rule_name}: {rule_desc}；" + "，".join(group_parts))
        details.append("；".join(hits) if hits else "未命中可解释风险规则，请检查 reject_b_risk_mask 配置。")
    return pd.Series(details, index=frame.index)


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


def estimate_planned_order(
    config: dict[str, Any],
    selected: pd.Series,
    signal_date: str,
    live_plan_mode: bool = False,
    reference_price_fallback: float = 0.0,
    runtime_config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    position = config.get("position", {})
    paper_trade = config.get("paper_trade", {})
    planned_equity = float(position.get("initial_cash", 500000))
    planned_position_pct = to_float(selected.get("planned_position_pct", position.get("target_position_pct", 0.8)))
    planned_amount = planned_equity * planned_position_pct
    reference_price = to_float(selected.get("historical_reference_next_open", 0.0))
    planned_order_date = str(selected.get("historical_reference_next_trade_date", "") or "")
    live_order_enabled = False
    if live_plan_mode:
        # 实盘计划模式：信号日当晚没有次日开盘价，参考价用信号日涨停收盘价（与E2口径一致）。
        # selected 行经过策略过滤后不含价格列，由调用方从 all_candidates 按
        # ts_code+signal_date 反查 limit_close 传入 reference_price_fallback。
        # 实际下单时 resize_buy_orders_for_live_account 会按实时行情和账户资金重算股数，
        # 这里的股数只是种子，必须>0 才能通过组合状态机 qty>0 的过滤进入实盘执行。
        if reference_price <= 0:
            reference_price = to_float(selected.get("limit_close", selected.get("close", 0.0)))
        if reference_price <= 0:
            reference_price = float(reference_price_fallback or 0.0)
        # 单笔限额与 E2/执行层同一口径，种子金额不超过 live_trade.max_single_order_amount。
        # 注意 config 参数是策略配置（无 trade_mode/live_trade），限额必须读运行时配置。
        rt_cfg = runtime_config or {}
        if str(rt_cfg.get("trade_mode", "")).lower() == "live":
            # 0=不限额（80%仓位接管），>0=单笔限额。
            max_single = float(rt_cfg.get("live_trade", {}).get("max_single_order_amount", 0) or 0)
            if max_single > 0:
                planned_amount = min(planned_amount, max_single)
        # 计划执行日=信号日的下一交易日；组合状态机按 planned_order_date==today 校验，
        # 防止收盘流水线失败后第二天误执行陈旧计划（E2 的 planned_buy_date 同款保护）。
        planned_order_date = next_trade_day(signal_date, 1)
        live_order_enabled = True
    round_lot = int(paper_trade.get("round_lot_size", 100))
    estimated_shares = int(planned_amount // reference_price) if reference_price > 0 else 0
    round_lot_shares = estimated_shares - estimated_shares % round_lot if round_lot > 0 else estimated_shares
    strategy_leg = str(selected.get("strategy_leg", "")).upper()
    # exit_n_days 按买入日计算：A/B 信号日T、T+1买、T+2卖，所以买入后1个交易日卖；
    # C 使用 fixed_hold3：信号日T、T+1买、T+3卖，所以买入后2个交易日卖。
    exit_n_days = 2 if strategy_leg == "C" else 1
    return pd.DataFrame(
        [
            {
                "paper_order_id": f"AB-{signal_date}-{selected.get('strategy_leg', '')}-{selected.get('ts_code', '')}",
                "signal_date": signal_date,
                "strategy_leg": selected.get("strategy_leg", ""),
                "planned_order_date": planned_order_date,
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
                "live_order_enabled": live_order_enabled,
                "exit_n_days": exit_n_days,
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
    live_plan_mode: bool = False,
) -> pd.DataFrame:
    if selected.empty:
        return enrich_checklist(pd.DataFrame(
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
        ))
    row = selected.iloc[0]
    needs_review = not manual_review.empty
    operation_status = str(row.get("operation_status", ""))
    if needs_review and operation_status == "HISTORICAL_SIM_FILLED":
        operation_status = "REVIEW_REQUIRED_PLAN_ONLY"
    if live_plan_mode:
        next_action = (
            "已生成实盘开仓计划；下一交易日09:15集合竞价预挂、09:30确认/补单，"
            "股数下单时按账户资金和单笔限额重算。"
        )
        if needs_review:
            next_action = "命中人工复核条件，请复核；" + next_action
    else:
        next_action = "先人工复核；通过后只进入模拟观察，不进入实盘。" if needs_review else "只生成模拟计划，等待后续数据验证。"
    return enrich_checklist(pd.DataFrame(
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
                "paper_observation_allowed": not live_plan_mode,
                "live_order_enabled": live_plan_mode,
                "safety_note": (
                    "A+B+C 实盘已放开：计划单经组合状态机（planned_order_date==today校验）→ "
                    "LiveOrderGateway 校验 → 按账户资金/单笔限额缩放后下单。"
                    if live_plan_mode
                    else "A+B+C filtered 只允许模拟观察；未完成分钟K、盘口和连续模拟验证前，不允许实盘。"
                ),
            }
        ]
    ))


def enrich_checklist(checklist: pd.DataFrame) -> pd.DataFrame:
    result = checklist.copy()
    if "operation_status" in result.columns:
        result["operation_status_desc"] = result["operation_status"].map(operation_status_desc)
    if "selection_status" in result.columns:
        result["selection_status_desc"] = result["selection_status"].map(selection_status_desc)
    if "manual_review_required" in result.columns:
        result["manual_review_required_desc"] = result["manual_review_required"].map(
            lambda value: bool_desc(value, "需要人工复核", "不需要人工复核")
        )
    if "live_order_enabled" in result.columns:
        result["live_order_enabled_desc"] = result["live_order_enabled"].map(
            lambda value: bool_desc(value, "允许实盘下单", "禁止实盘下单，仅模拟/观察")
        )
    if "limit_data_quality" in result.columns:
        result["limit_data_quality_desc"] = result["limit_data_quality"].map(
            lambda value: "完整 limit_list_d 历史口径" if str(value) == "full" else f"非完整历史口径：{value}"
        )
    return result


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
    mkdir_p(output_prefix.parent)

    base_generator = PaperCandidateGenerator(
        args.strategy_config,
        input_trades_path=args.input_trades_path,
        market_emotion_features_path=args.market_emotion_features_path,
        theme_heat_features_path=args.theme_heat_features_path,
    )
    if args.signal_date:
        signal_date = normalize_date(args.signal_date)
        data_ok, quality = validate_signal_data_quality(args.runtime_config, config, signal_date, args.fill_scored_path)
        if not data_ok and bool(
            config.get("paper_ab_filtered_strategy", {})
            .get("data_quality_requirements", {})
            .get("block_when_not_compatible", True)
        ):
            paths = write_data_quality_block_outputs(output_prefix, signal_date, quality)
            print("A+B+C filtered 每日模拟盘操作台完成：")
            for name, path in paths.items():
                print(f"- {name}: {path}")
            print(pd.read_csv(paths["checklist"]).to_string(index=False))
            return
    all_candidates = base_generator.load_all_candidates()
    signal_date = normalize_date(args.signal_date) if args.signal_date else latest_signal_date(all_candidates)
    data_ok, quality = validate_signal_data_quality(args.runtime_config, config, signal_date, args.fill_scored_path)
    if not data_ok and bool(
        config.get("paper_ab_filtered_strategy", {})
        .get("data_quality_requirements", {})
        .get("block_when_not_compatible", True)
    ):
        paths = write_data_quality_block_outputs(output_prefix, signal_date, quality)
        print("A+B+C filtered 每日模拟盘操作台完成：")
        for name, path in paths.items():
            print(f"- {name}: {path}")
        print(pd.read_csv(paths["checklist"]).to_string(index=False))
        return
    selected_action = config.get("paper_candidate", {}).get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    live_plan_mode = bool(args.input_trades_path)
    audit = PaperDailyFlowRunner(args.strategy_config).load_audit_trades(
        PROJECT_ROOT / config.get("paper_daily_flow", {}).get("input_audit_trades_path", "")
    ) if not live_plan_mode else pd.DataFrame()

    a_config = copy.deepcopy(config)
    a_generator = build_generator(args.strategy_config, a_config)
    a_generator.input_trades_path = base_generator.input_trades_path
    a_generator.market_emotion_features_path = base_generator.market_emotion_features_path
    a_generator.theme_heat_features_path = base_generator.theme_heat_features_path
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
        if live_plan_mode:
            operation_status, account_return, return_source, note = "PLAN_ONLY_PENDING", 0.0, "live_signal_plan", "A 实盘计划模式：只生成开仓计划，不读取历史回测成交回放。"
        else:
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
                b_rejected["reject_reason_desc"] = "B 首选标的命中风险过滤规则。"
                b_rejected["risk_reject_detail"] = risk_reject_detail(b_rejected, config)
                selection_status = "A_NO_SELECTED_B_RISK_FILTERED"
            else:
                if live_plan_mode:
                    execution_reference, operation_status, account_return, return_source, note = (
                        pd.DataFrame(),
                        "PLAN_ONLY_PENDING",
                        0.0,
                        "live_signal_plan",
                        "B 实盘计划模式：只生成开仓计划，不读取历史回测成交回放。",
                    )
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
            not live_plan_mode
            and
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
                        c_rejected["reject_reason_desc"] = "C 首选标的命中风险过滤规则。"
                        c_rejected["risk_reject_detail"] = risk_reject_detail(c_rejected, config)
                        if selected.empty:
                            selection_status = "A_B_NO_FILLED_C_RISK_FILTERED"
                    else:
                        if live_plan_mode:
                            c_reference, c_status, c_return, c_source, c_note = (
                                pd.DataFrame(),
                                "PLAN_ONLY_PENDING",
                                0.0,
                                "live_signal_plan",
                                "C 实盘计划模式：只生成开仓计划，不读取历史回测成交回放。",
                            )
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

    # live 模式下 selected 行没有价格列，从候选源数据按 ts_code+signal_date 反查涨停收盘价做参考价
    reference_price_fallback = 0.0
    if live_plan_mode and not selected.empty:
        _sel_code = str(selected.iloc[0].get("ts_code", ""))
        _match = all_candidates[
            (all_candidates["ts_code"].astype(str) == _sel_code)
            & (all_candidates["trade_date"].map(normalize_date) == signal_date)
        ]
        if not _match.empty:
            reference_price_fallback = to_float(_match.iloc[0].get("limit_close", _match.iloc[0].get("close", 0.0)))
    planned_orders = (
        estimate_planned_order(
            config, selected.iloc[0], signal_date,
            live_plan_mode=live_plan_mode,
            reference_price_fallback=reference_price_fallback,
            runtime_config=load_json_config(args.runtime_config),
        )
        if not selected.empty else pd.DataFrame()
    )
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
        live_plan_mode=live_plan_mode,
    )
    if selection_status and selected.empty:
        checklist["selection_status"] = selection_status
        checklist = enrich_checklist(checklist)

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

    _print_e2_status(signal_date, planned_orders)


def _print_e2_status(signal_date: str, planned_orders: pd.DataFrame) -> None:
    """在每日操作台末尾打印 E2 策略状态预览。"""
    print()
    print("─" * 50)
    print("  策略 E2 状态预览（板块中性小市值）")
    print("─" * 50)

    # 检查 A/B/C 是否生成了计划委托
    if not planned_orders.empty:
        print("  A/B/C 今日已生成计划委托 → E2 不触发（资金被 A/B/C 占用）")
        print("─" * 50)
        return

    # 检查 positions.json 是否有 open 持仓
    positions_path = PROJECT_ROOT / "data" / "processed" / "positions.json"
    open_positions: list[dict[str, object]] = []
    if positions_path.exists():
        try:
            import json as _json
            raw = _json.loads(positions_path.read_text(encoding="utf-8"))
            open_positions = [p for p in (raw if isinstance(raw, list) else []) if str(p.get("status", "")) == "open"]
        except Exception:
            pass

    if open_positions:
        occupied = [(p.get("strategy_leg", "?"), p.get("ts_code", "?"), p.get("planned_exit_date", "?"))
                    for p in open_positions]
        print(f"  账户有未平仓头寸 → E2 不触发。持仓: {occupied}")
        print("─" * 50)
        return

    print("  A/B/C 今日无委托，账户无持仓 → E2 可能触发")
    print(f"  请收盘后运行（15:30+）：")
    print(f"    python scripts/run_strategy_e2_signal.py --signal-date {signal_date}")
    print("  E2 条件：segment_retreat_state_bucket=neutral + 非ST + 成交可靠 → 选流通市值最小1只")
    print("  E2 执行：T+1开盘买入 80%仓位，T+2收盘卖出")
    print("─" * 50)


if __name__ == "__main__":
    main()
