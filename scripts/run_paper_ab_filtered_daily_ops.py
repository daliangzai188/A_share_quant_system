"""
运行 A严格策略 + C补位策略每日操作台。

文件作用：
1. T 日收盘后先生成 A 严格策略候选。
2. B策略已彻底删除，不参与候选、买入或自动卖出。
3. A无选中标的时直接尝试C，最终由组合状态机按D>A>E>C统一裁决。
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

from scripts.run_strategy_e_signal import next_trade_day
from scripts.run_paper_ab_filtered_observation_window import (
    reject_strategy_risk_mask,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config, mkdir_p
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 A+C filtered 每日模拟盘操作台。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时通用配置文件路径。")
    parser.add_argument("--signal-date", default=None, help="信号日期，格式 YYYYMMDD。不传则使用本地最新日期。")
    parser.add_argument("--top-n", type=int, default=None, help="候选输出数量，不传则读取配置。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_c_hold3",
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


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def build_generator(strategy_config_path: str | Path, config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(strategy_config_path)
    generator.config = config
    generator.paper_config = config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


def condition_strategy_config(
    base_config: dict[str, Any],
    conditions: list[dict[str, str]],
    strategy_name: str,
    *,
    condition_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    config["strategy_name"] = strategy_name
    filters = config.setdefault("candidate_filters", {})
    filters["conditions"] = [dict(condition) for condition in conditions]
    if condition_profiles:
        filters["condition_profiles"] = copy.deepcopy(condition_profiles)
    else:
        filters.pop("condition_profiles", None)
    # 当前所有传入自定义 conditions 的调用方都是 C。A 的排序优化不能被 C
    # 隐式继承；C 未通过本轮双复利门槛时，必须读取 c_strategy.ranking 冻结原排序。
    c_ranking = (
        base_config.get("paper_ab_filtered_strategy", {})
        .get("c_strategy", {})
        .get("ranking")
    )
    if isinstance(c_ranking, dict) and c_ranking:
        ranking = copy.deepcopy(c_ranking)
        if "score_rules" not in ranking:
            ranking["score_rules"] = copy.deepcopy(
                base_config.get("ranking", {}).get("score_rules", [])
            )
        config["ranking"] = ranking
    return config


def condition_text(conditions: list[dict[str, Any]]) -> str:
    if conditions and "conditions" in conditions[0]:
        return condition_profiles_text(conditions)
    return ";".join(f"{condition['column']}={condition['value']}" for condition in conditions)


def condition_profiles_text(profiles: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for position, profile in enumerate(profiles, 1):
        profile_id = str(profile.get("profile_id", f"PROFILE_{position}"))
        conditions = " AND ".join(
            f"{condition['column']}={condition['value']}"
            for condition in profile.get("conditions", [])
        )
        parts.append(f"{profile_id}({conditions})")
    return " OR ".join(parts)


def apply_and_rank(
    generator: PaperCandidateGenerator,
    filtered: pd.DataFrame,
    signal_date: str,
    top_n: int | None = None,
) -> pd.DataFrame:
    daily = filtered[filtered["trade_date"].map(normalize_date) == signal_date].copy()
    if daily.empty:
        return pd.DataFrame()
    ranked = generator.rank_candidates(daily)
    return generator.build_output(ranked, signal_date, top_n=top_n or generator.default_top_n)


def selected_candidate(output: pd.DataFrame, selected_action: str) -> pd.Series | None:
    if output.empty:
        return None
    selected = output[output["planned_action"].astype(str) == selected_action].copy()
    return None if selected.empty else selected.iloc[0]


def find_a_audit_row(audit: pd.DataFrame, signal_date: str, ts_code: str) -> pd.Series | None:
    matched = audit[
        (audit["trade_date"].astype(str) == str(signal_date))
        & (audit["ts_code"].astype(str) == str(ts_code))
    ].copy()
    return None if matched.empty else matched.iloc[0]


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
        "NO_SELECTED": "A/C 策略均未选出可执行标的。",
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
        "A_NO_SELECTED_B_REMOVED": "A 无候选；B已彻底删除，直接检查C及后续组合补位。",
        "A_NO_SELECTED_C_RISK_FILTERED": "A 无候选，C 首选标的命中风险过滤。",
        "A_NO_SELECTED_C_NO_SELECTED": "A 无候选，C 也无候选。",
    }
    if text.startswith("A_NO_SELECTED_C_SELECTED:"):
        return "A 无候选，C 补位策略选中；冒号后为命中条件。"
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
        raise RuntimeError(f"拒绝运行 A/C 每日操作台：trade_mode 不是安全模式: {trade_mode}")
    for key in ["live_trading_enabled", "broker_adapter_enabled", "qmt_enabled"]:
        if bool(config.get(key, False)):
            raise RuntimeError(f"拒绝运行 A/C 每日操作台：{key}=true")
    ab_config = config.get("paper_ab_filtered_strategy", {})
    if bool(ab_config.get("allow_live_order", False)) or bool(ab_config.get("live_order_enabled", False)):
        raise RuntimeError("拒绝运行 A/C 每日操作台：paper_ab_filtered_strategy 存在实盘开关。")


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
    rules = ab_config.get("c_strategy", {}).get("risk_reject_rules", [])
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
        details.append("；".join(hits) if hits else "未命中可解释风险规则，请检查 C risk_reject_rules 配置。")
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


def configured_c_condition_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """读取正式C的少量冻结OR分支；每个分支内部仍是AND。"""

    c_config = config.get("paper_ab_filtered_strategy", {}).get("c_strategy", {})
    if not bool(c_config.get("enabled", False)):
        return []
    mode = str(c_config.get("condition_mode", "ALL_CONDITIONS")).upper()
    profiles = c_config.get("condition_profiles", [])
    if mode != "ANY_PROFILE":
        return []
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("C配置声明ANY_PROFILE但没有condition_profiles")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(profiles, 1):
        profile_id = str(raw.get("profile_id", "")).strip()
        if not profile_id or profile_id in seen:
            raise RuntimeError(f"C条件分支编号缺失或重复: {profile_id}")
        seen.add(profile_id)
        raw_conditions = raw.get("conditions", [])
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise RuntimeError(f"C条件分支不能为空: {profile_id}")
        conditions = [
            {
                "column": str(condition["column"]),
                "operator": str(condition.get("operator", "==")),
                "value": str(condition["value"]),
            }
            for condition in raw_conditions
        ]
        if any(condition["operator"] != "==" for condition in conditions):
            raise RuntimeError(f"C正式分支目前只允许等值条件: {profile_id}")
        normalized.append(
            {
                "profile_id": profile_id,
                "priority": int(raw.get("priority", position)),
                "conditions": conditions,
            }
        )
    return sorted(normalized, key=lambda item: (item["priority"], item["profile_id"]))


def build_c_shadow_candidates(
    strategy_config_path: str | Path,
    config: dict[str, Any],
    all_candidates: pd.DataFrame,
    signal_date: str,
    selected_action: str,
    top_n: int | None = None,
) -> tuple[list[dict[str, Any]], pd.DataFrame, pd.Series | None, pd.DataFrame]:
    """始终计算 C 自身候选，但不改变 A>C 的实盘让路规则。

    过去只有 A 为空时才计算 C，导致 A 命中日无法观察 C 的样本外表现。
    这里把 C 的纯计算提前，输出候选和风险拒绝结果；真正是否进入
    ``selected/planned_orders`` 仍由调用方原有的 ``A优先`` 分支决定。
    """

    profiles = configured_c_condition_profiles(config)
    conditions = configured_c_conditions(config)
    configured_rules: list[dict[str, Any]] = profiles or conditions
    if not configured_rules:
        return configured_rules, pd.DataFrame(), None, pd.DataFrame()
    c_config = condition_strategy_config(
        config,
        conditions,
        "backup_strategy_c_current",
        condition_profiles=profiles,
    )
    c_generator = build_generator(strategy_config_path, c_config)
    c_filtered = c_generator.apply_strategy_filters(all_candidates)
    daily = c_filtered[
        c_filtered["trade_date"].map(normalize_date).eq(signal_date)
    ].copy()
    if daily.empty:
        return configured_rules, pd.DataFrame(), None, pd.DataFrame()

    # 与严格回放一致：先为全部命中OR分支的股票计算C自身风险过滤，再在
    # 剩余股票中按profit_source_score、turnover_rate排序并允许顺位递补。
    ranked = c_generator.rank_candidates(daily)
    ranked["risk_flags"] = [
        c_generator.build_risk_flags(row) for row in ranked.itertuples(index=False)
    ]
    rejected_mask = reject_strategy_risk_mask(ranked, config, "c_strategy")
    rejected = ranked[rejected_mask].copy()
    if not rejected.empty:
        rejected["reject_reason"] = "C_HIT_RISK_REJECT_RULES_BEFORE_FINAL_RANK"
        rejected["reject_reason_desc"] = "C候选命中自身风险规则，最终排序前剔除并允许下一名递补。"
        rejected["risk_reject_detail"] = risk_reject_detail(rejected, config)
    accepted = ranked[~rejected_mask].copy().reset_index(drop=True)
    accepted["candidate_rank"] = range(1, len(accepted) + 1)
    candidates = c_generator.build_output(
        accepted,
        signal_date,
        top_n=top_n or c_generator.default_top_n,
    )
    picked = selected_candidate(candidates, selected_action)
    return configured_rules, candidates, picked, rejected


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
    planned_position_pct = to_float(selected.get("planned_position_pct", position.get("target_position_pct", 0.825)))
    planned_amount = planned_equity * planned_position_pct
    reference_price = to_float(selected.get("historical_reference_next_open", 0.0))
    planned_order_date = str(selected.get("historical_reference_next_trade_date", "") or "")
    live_order_enabled = False
    if live_plan_mode:
        # 实盘计划模式：信号日当晚没有次日开盘价，参考价用信号日涨停收盘价（与E口径一致）。
        # selected 行经过策略过滤后不含价格列，由调用方从 all_candidates 按
        # ts_code+signal_date 反查 limit_close 传入 reference_price_fallback。
        # 实际下单时 resize_buy_orders_for_live_account 会按实时行情和账户资金重算股数，
        # 这里的股数只是种子，必须>0 才能通过组合状态机 qty>0 的过滤进入实盘执行。
        if reference_price <= 0:
            reference_price = to_float(selected.get("limit_close", selected.get("close", 0.0)))
        if reference_price <= 0:
            reference_price = float(reference_price_fallback or 0.0)
        # 单笔限额与 E/执行层同一口径，种子金额不超过 live_trade.max_single_order_amount。
        # 注意 config 参数是策略配置（无 trade_mode/live_trade），限额必须读运行时配置。
        rt_cfg = runtime_config or {}
        if str(rt_cfg.get("trade_mode", "")).lower() == "live":
            # 0=不限额（82.5%目标仓位接管），>0=单笔限额。
            max_single = float(rt_cfg.get("live_trade", {}).get("max_single_order_amount", 0) or 0)
            if max_single > 0:
                planned_amount = min(planned_amount, max_single)
        # 计划执行日=信号日的下一交易日；组合状态机按 planned_order_date==today 校验，
        # 防止收盘流水线失败后第二天误执行陈旧计划（E 的 planned_buy_date 同款保护）。
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
                    "next_action": "A/C均无可用候选，今日不生成该层买入计划；组合状态机继续检查D/E。",
                    "a_candidate_count": int(len(a_candidates)),
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
            "已生成实盘开仓计划；下一交易日09:20复核并集合竞价预挂、09:30确认/补单，"
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
                    "A+C计划单经组合状态机（planned_order_date==today校验）→ "
                    "LiveOrderGateway 校验 → 按账户资金/单笔限额缩放后下单。"
                    if live_plan_mode
                    else "A+C只允许模拟观察；未完成分钟K、盘口和连续模拟验证前，不允许实盘。"
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

    content = f"""# A+C filtered 每日模拟盘操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

{table_text(checklist)}

## 选中标的

{table_text(selected) if not selected.empty else "今日无选中标的。"}

## 输出文件

{table_text(outputs)}

## 执行限制

- A 优先；A 无选中标的时直接检查 C，B 已彻底删除。
- C 命中自身 `risk_reject_rules` 时直接跳过，不寻找下一只替代。
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
            print("A+C filtered 每日模拟盘操作台完成：")
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
        print("A+C filtered 每日模拟盘操作台完成：")
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

    # C 作为影子腿每天都独立计算并落盘，便于前向反事实评估；这里只增加
    # 观察数据，不改变 A 命中时 C 必须让位、不得生成实盘计划的原规则。
    c_conditions, c_candidates, c_selected, c_rejected = build_c_shadow_candidates(
        args.strategy_config,
        config,
        all_candidates,
        signal_date,
        selected_action,
        top_n=args.top_n,
    )
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
        selection_status = "A_NO_SELECTED_B_REMOVED"
        if selected.empty:
            if c_conditions:
                if c_selected is not None:
                    c_selected_frame = pd.DataFrame([c_selected])
                    if not c_rejected.empty:
                        if selected.empty:
                            selection_status = "A_NO_SELECTED_C_RISK_FILTERED"
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
                            f"A_NO_SELECTED_C_SELECTED:{condition_text(c_conditions)}",
                            c_return,
                            c_source,
                            c_note,
                        )
                elif selected.empty:
                    selection_status = "A_NO_SELECTED_C_NO_SELECTED"

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
        c_candidates,
        c_rejected,
        live_plan_mode=live_plan_mode,
    )
    if selection_status and selected.empty:
        checklist["selection_status"] = selection_status
        checklist = enrich_checklist(checklist)

    paths = output_paths(output_prefix, signal_date)
    a_candidates.to_csv(paths["a_candidates"], index=False, encoding="utf-8-sig")
    c_candidates.to_csv(paths["c_candidates"], index=False, encoding="utf-8-sig")
    c_rejected.to_csv(paths["c_rejected"], index=False, encoding="utf-8-sig")
    selected.to_csv(paths["selected"], index=False, encoding="utf-8-sig")
    planned_orders.to_csv(paths["planned_orders"], index=False, encoding="utf-8-sig")
    manual_review.to_csv(paths["manual_review"], index=False, encoding="utf-8-sig")
    execution_reference.to_csv(paths["execution_reference"], index=False, encoding="utf-8-sig")
    checklist.to_csv(paths["checklist"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], checklist, selected, paths)

    print("A+C filtered 每日模拟盘操作台完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")
    print(checklist.to_string(index=False))

    _print_e_status(signal_date, planned_orders)


def _print_e_status(signal_date: str, planned_orders: pd.DataFrame) -> None:
    """在每日操作台末尾打印 E 策略状态预览。"""
    print()
    print("─" * 50)
    print("  策略 E 状态预览（板块neutral + 换手率降序）")
    print("─" * 50)

    # 检查 A/C 是否生成了计划委托
    if not planned_orders.empty:
        print("  A/C 今日已生成计划委托 → E 不触发（资金被 A/C 占用）")
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
        print(f"  账户有未平仓头寸 → E 不触发。持仓: {occupied}")
        print("─" * 50)
        return

    print("  A/C 今日无委托，账户无持仓 → E 可能触发")
    print(f"  请收盘后运行（15:30+）：")
    print(f"    python scripts/run_strategy_e_signal.py --signal-date {signal_date}")
    print("  E 条件：segment_retreat_state_bucket=neutral + 非ST + 成交可靠 → 按信号日换手率降序选1只")
    print("  E 执行：T+1开盘买入 82.5%目标仓位，按命中R1规则在T+2或T+3收盘卖出")
    print("─" * 50)


if __name__ == "__main__":
    main()
