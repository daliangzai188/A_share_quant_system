from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ac_rules import (
    condition_strategy_config,
    condition_text,
    configured_c_condition_profiles,
    configured_c_conditions,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.ac_filter_trace import filter_trace, stop_point_text  # 2026-11-14 搬到 src，守护进程也用
from src.utils.config import load_json_config

# 与历史回测 limit_list_d/full 口径一致的涨停池来源（同 trading_daemon.LIMIT_SOURCES_MATCHING_BACKTEST）：
# qmt_native 为券商禁止 QMT 内置 Python 联网后按 limit_list_d 规则从 QMT 行情重建、逐日核对一致的涨停池。
LIMIT_SOURCES_MATCHING_BACKTEST = ("limit_list_d", "qmt_native")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计指定信号日的数据口径、必需字段和 A/C 候选筛选结果。")
    parser.add_argument("--signal-date", required=True, help="信号日期，格式 YYYYMMDD。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_c_hold3",
        help="A/C 每日操作台输出前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def prefer_live_processed_path(live_name: str, fallback_name: str | Path) -> Path:
    live_path = PROJECT_ROOT / "data" / "processed" / live_name
    if live_path.exists():
        return live_path
    fallback_path = Path(fallback_name)
    if fallback_path.is_absolute():
        return fallback_path
    if len(fallback_path.parts) > 1:
        return PROJECT_ROOT / fallback_path
    return PROJECT_ROOT / "data" / "processed" / fallback_path


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def yes_no(ok: bool) -> str:
    return "是" if ok else "否"


def value_counts_text(data: pd.DataFrame, column: str) -> str:
    if data.empty or column not in data.columns:
        return "无"
    counts = data[column].fillna("").astype(str).value_counts().head(10).to_dict()
    return "；".join(f"{key or '空'}={value}" for key, value in counts.items())


def audit_required_columns(data: pd.DataFrame, required_columns: list[str]) -> tuple[list[str], list[str]]:
    missing = [column for column in required_columns if column not in data.columns]
    empty = [
        column
        for column in required_columns
        if column in data.columns and data[column].isna().all()
    ]
    return missing, empty


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def summarize_candidate_file(label: str, path: Path) -> None:
    data = read_csv(path)
    print(f"{label}: exists={path.exists()} rows={len(data)} path={path}")
    if data.empty:
        return
    for column in ["reject_reason", "reject_reason_desc", "risk_reject_detail", "risk_flags"]:
        if column in data.columns:
            print(f"  {column}: {value_counts_text(data, column)}")
    display_columns = [
        column
        for column in [
            "candidate_rank",
            "ts_code",
            "name",
            "fill_probability",
            "allow_buy_reliable",
            "is_fill_score_reliable",
            "risk_flags",
            "reject_reason_desc",
            "risk_reject_detail",
        ]
        if column in data.columns
    ]
    if display_columns:
        print(data[display_columns].head(5).to_string(index=False))


def main() -> None:
    args = parse_args()
    signal_date = str(args.signal_date)
    runtime_config = load_json_config(args.runtime_config)
    strategy_config = load_json_config(args.strategy_config)
    data_config = runtime_config.get("data", {})
    fill_config = runtime_config.get("fill_model", {})
    requirements = strategy_config.get("paper_ab_filtered_strategy", {}).get("data_quality_requirements", {})
    required_columns = [str(column) for column in requirements.get("required_columns", [])]

    raw_limit_path = PROJECT_ROOT / data_config.get("limit_list_dir", "data/raw/limit_list") / f"{signal_date}.csv"
    fill_path = prefer_live_processed_path(
        "live_limit_up_fill_scored.csv",
        fill_config.get("output_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"),
    )
    market_emotion_path = prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
    theme_heat_path = prefer_live_processed_path("live_theme_heat_features.csv", "theme_heat_features.csv")
    output_prefix = resolve_path(args.output_prefix)
    checklist_path = output_prefix.with_name(output_prefix.name + f"_{signal_date}_checklist.csv")

    print_section("1. 原始涨停池口径")
    raw_limit = read_csv(raw_limit_path)
    print(f"raw_limit: exists={raw_limit_path.exists()} rows={len(raw_limit)} path={raw_limit_path}")
    print(f"limit_data_source: {value_counts_text(raw_limit, 'limit_data_source')}")
    print(f"limit_data_quality: {value_counts_text(raw_limit, 'limit_data_quality')}")
    print(f"strategy_compatible: {value_counts_text(raw_limit, 'strategy_compatible')}")

    print_section("2. 成交概率打分表")
    scored = read_csv(fill_path)
    scored_daily = scored[scored["trade_date"].astype(str) == signal_date].copy() if "trade_date" in scored.columns else pd.DataFrame()
    missing, empty = audit_required_columns(scored_daily, required_columns)
    full_quality = (
        not scored_daily.empty
        and "limit_data_quality" in scored_daily.columns
        and scored_daily["limit_data_quality"].fillna("").astype(str).eq("full").all()
    )
    source_ok = (
        not scored_daily.empty
        and "limit_data_source" in scored_daily.columns
        and scored_daily["limit_data_source"].fillna("").astype(str).isin(LIMIT_SOURCES_MATCHING_BACKTEST).all()
    )
    compatible_ok = (
        not scored_daily.empty
        and "strategy_compatible" in scored_daily.columns
        and scored_daily["strategy_compatible"].fillna("").astype(str).str.lower().isin({"true", "1"}).all()
    )
    print(f"fill_scored: exists={fill_path.exists()} signal_rows={len(scored_daily)} path={fill_path}")
    print(f"完整历史口径: {yes_no(full_quality)}")
    print(f"来源为 limit_list_d（或按其规则重建并核对一致的 QMT 原生涨停池）: {yes_no(source_ok)}")
    print(f"策略兼容: {yes_no(compatible_ok)}")
    print(f"缺少必需字段: {missing or '无'}")
    print(f"必需字段全为空: {empty or '无'}")
    print(f"allow_buy_reliable: {value_counts_text(scored_daily, 'allow_buy_reliable')}")
    print(f"is_fill_score_reliable: {value_counts_text(scored_daily, 'is_fill_score_reliable')}")

    print_section("3. A/C 操作台结果")
    checklist = read_csv(checklist_path)
    print(f"checklist: exists={checklist_path.exists()} rows={len(checklist)} path={checklist_path}")
    if not checklist.empty:
        display_columns = [
            column
            for column in [
                "signal_date",
                "operation_status",
                "operation_status_desc",
                "selection_status",
                "selection_status_desc",
                "next_action",
                "a_candidate_count",
                "c_candidate_count",
                "selected_count",
                "planned_order_count",
                "live_order_enabled_desc",
            ]
            if column in checklist.columns
        ]
        print(checklist[display_columns].to_string(index=False))

    print_section("4. 分层筛选漏斗")
    try:
        generator_kwargs = {
            "input_trades_path": fill_path,
            "market_emotion_features_path": market_emotion_path,
            "theme_heat_features_path": theme_heat_path,
        }
        base_generator = PaperCandidateGenerator(args.strategy_config, **generator_kwargs)
        all_candidates = base_generator.load_all_candidates()
        traces = [filter_trace("A主策略", base_generator, all_candidates, signal_date)]

        c_conditions = configured_c_conditions(strategy_config)
        c_profiles = configured_c_condition_profiles(strategy_config)
        if c_conditions or c_profiles:
            c_config = condition_strategy_config(
                strategy_config,
                c_conditions,
                "backup_strategy_c_current",
                condition_profiles=c_profiles,
            )
            c_generator = PaperCandidateGenerator(args.strategy_config, **generator_kwargs)
            c_generator.config = c_config
            c_generator.paper_config = c_config.get("paper_candidate", {})
            c_generator.risk_thresholds = c_generator.paper_config.get("risk_thresholds", {})
            c_rule_text = condition_text(c_profiles or c_conditions)
            traces.append(filter_trace(f"C补位策略（{c_rule_text}）", c_generator, all_candidates, signal_date))

        trace = pd.concat(traces, ignore_index=True)
        print(trace.to_string(index=False))
        print()
        print("停止点判定：")
        for layer in trace["strategy_layer"].dropna().astype(str).drop_duplicates().tolist():
            print(f"- {stop_point_text(trace, layer)}")
    except Exception as exc:
        print(f"分层筛选漏斗生成失败: {exc}")

    print_section("5. 候选与过滤明细")
    for name in ["a_candidates", "c_candidates", "c_rejected_by_filter"]:
        suffix = name
        path = output_prefix.with_name(output_prefix.name + f"_{signal_date}_{suffix}.csv")
        summarize_candidate_file(name, path)

    print_section("6. 审计结论")
    if full_quality and source_ok and compatible_ok and not missing and not empty:
        print("数据口径结论：通过。当前信号日与历史回测使用的 limit_list_d 完整口径一致。")
    else:
        print("数据口径结论：不通过。不要生成开仓计划，请先补齐完整 limit_list_d 数据。")
    if not checklist.empty and "planned_order_count" in checklist.columns:
        planned_count = int(pd.to_numeric(checklist["planned_order_count"].iloc[0], errors="coerce") or 0)
        print(f"交易计划结论：计划单数量={planned_count}。0 表示策略筛选后不交易，不代表数据失败。")


if __name__ == "__main__":
    main()
