from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_paper_ab_filtered_daily_ops import configured_c_conditions
from scripts.run_paper_ab_filtered_observation_window import configured_b_conditions, condition_text
from scripts.search_paper_backup_strategy_b import backup_config
from src.paper_candidate_generator import PaperCandidateGenerator
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计指定信号日的数据口径、必需字段和 A/B/C 候选筛选结果。")
    parser.add_argument("--signal-date", required=True, help="信号日期，格式 YYYYMMDD。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_plus_c_hold3",
        help="A/B/C 每日操作台输出前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


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


def condition_desc(conditions: list[dict[str, Any]]) -> str:
    if not conditions:
        return "无"
    parts = []
    for condition in conditions:
        column = condition.get("column", "")
        operator = condition.get("operator", "==")
        value = condition.get("value", "")
        description = condition.get("description", "")
        parts.append(f"{column} {operator} {value}" + (f"（{description}）" if description else ""))
    return "；".join(parts)


def exclude_rules_desc(rules: list[dict[str, Any]]) -> str:
    if not rules:
        return "无"
    parts = []
    for rule in rules:
        rule_name = rule.get("name", "")
        description = rule.get("description", "")
        conditions = " 且 ".join(
            f"{item.get('column', '')} {item.get('operator', '==')} {item.get('value', '')}"
            for item in rule.get("conditions", [])
        )
        parts.append(f"{rule_name}: {conditions}" + (f"（{description}）" if description else ""))
    return "；".join(parts)


def daily_count(data: pd.DataFrame, signal_date: str) -> int:
    if data.empty or "trade_date" not in data.columns:
        return 0
    return int((data["trade_date"].astype(str) == signal_date).sum())


def signal_slice(data: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    if data.empty or "trade_date" not in data.columns:
        return pd.DataFrame()
    return data[data["trade_date"].astype(str) == signal_date].copy()


def top_counts(series: pd.Series, limit: int = 6) -> str:
    if series.empty:
        return "无"
    counts = series.fillna("missing").astype(str).value_counts().head(limit).to_dict()
    return "，".join(f"{key}={value}" for key, value in counts.items())


def universe_reason_detail(before: pd.DataFrame, after: pd.DataFrame, config: dict[str, Any], signal_date: str) -> str:
    daily_before = signal_slice(before, signal_date)
    if daily_before.empty:
        return "当日进入股票池过滤前已经没有候选。"
    daily_after_keys = set(zip(signal_slice(after, signal_date).get("trade_date", []), signal_slice(after, signal_date).get("ts_code", [])))
    removed = daily_before[
        ~daily_before.apply(lambda row: (row.get("trade_date"), row.get("ts_code")) in daily_after_keys, axis=1)
    ].copy()
    if removed.empty:
        return "当日没有候选被股票池过滤剔除。"

    universe = config.get("universe", {})
    parts: list[str] = []
    name = removed.get("name", pd.Series("", index=removed.index)).fillna("").astype(str).str.upper()
    is_st = removed.get("is_st", pd.Series(False, index=removed.index)).astype(str).str.lower().isin({"true", "1"})
    if bool(universe.get("exclude_st", False)) or bool(universe.get("exclude_delisting_risk", False)):
        st_count = int((is_st | name.str.contains("ST", na=False) | name.str.contains("退", na=False)).sum())
        if st_count:
            parts.append(f"ST/退市风险={st_count}")

    segment = removed.get("market_segment", pd.Series("missing", index=removed.index)).fillna("missing").astype(str)
    excluded_segments = {str(value) for value in universe.get("exclude_market_segments", [])}
    segment_rules = {
        "bj": bool(universe.get("exclude_bj", False)),
        "chi_next": bool(universe.get("exclude_chi_next", False)),
        "sh_main": bool(universe.get("exclude_sh_main", False)),
        "sz_main": bool(universe.get("exclude_sz_main", False)),
    }
    blocked_segments = set(excluded_segments) | {key for key, enabled in segment_rules.items() if enabled}
    if blocked_segments:
        blocked = segment[segment.isin(blocked_segments)]
        if not blocked.empty:
            parts.append(f"被排除市场分段={top_counts(blocked)}")
    if not parts:
        parts.append(f"剔除候选市场分段分布={top_counts(segment)}")
    return "；".join(parts)


def include_reason_detail(before: pd.DataFrame, conditions: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入入选条件前已经没有候选。"
    if not conditions:
        return "没有配置入选条件。"
    parts: list[str] = []
    for condition in conditions:
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in current.columns:
            parts.append(f"{column}字段不存在")
            current = pd.DataFrame()
            continue
        values = current[column].fillna("missing").astype(str)
        passed = values == expected
        failed = current[~passed].copy()
        parts.append(
            f"{column}要求={expected}，通过={int(passed.sum())}，未通过={len(failed)}，未通过实际值：{top_counts(failed[column]) if not failed.empty else '无'}"
        )
        current = current[passed].copy()
        if current.empty:
            break
    return "；".join(parts)


def exclude_reason_detail(before: pd.DataFrame, conditions: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入排除条件前已经没有候选。"
    if not conditions:
        return "没有配置单字段排除条件。"
    parts: list[str] = []
    for condition in conditions:
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in current.columns:
            parts.append(f"{column}字段不存在")
            continue
        values = current[column].fillna("missing").astype(str)
        removed = current[values == expected].copy()
        parts.append(f"排除 {column}={expected}，剔除={len(removed)}")
        current = current[values != expected].copy()
    return "；".join(parts)


def compound_exclude_reason_detail(before: pd.DataFrame, rules: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入复合排除规则前已经没有候选。"
    if not rules:
        return "没有配置复合排除规则。"
    parts: list[str] = []
    for rule in rules:
        mask = pd.Series(True, index=current.index)
        texts: list[str] = []
        for condition in rule.get("conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            texts.append(f"{column}={expected}")
            if column not in current.columns:
                mask &= False
            else:
                mask &= current[column].fillna("missing").astype(str) == expected
        removed = current[mask].copy()
        parts.append(f"{rule.get('name', 'unnamed_rule')}（{' 且 '.join(texts)}）剔除={len(removed)}")
        current = current[~mask].copy()
    return "；".join(parts)


def rank_reason_detail(daily: pd.DataFrame, selected: pd.DataFrame, config: dict[str, Any]) -> str:
    if daily.empty:
        return "当日没有候选进入排序。"
    if selected.empty:
        return "当日有候选进入排序，但没有生成选中计划。"
    row = selected.iloc[0]
    ranking = config.get("ranking", {})
    return (
        f"排序后首选={row.get('ts_code', '')} {row.get('name', '')}，"
        f"profit_source_score={row.get('profit_source_score', '')}，"
        f"排序字段={ranking.get('columns', [])}"
    )


def stop_point_text(trace: pd.DataFrame, label: str) -> str:
    scoped = trace[trace["strategy_layer"].astype(str) == label].copy()
    if scoped.empty:
        return f"{label}: 无漏斗记录"
    for _, row in scoped.iterrows():
        if int(row.get("signal_date_after", 0)) == 0:
            return (
                f"{label}: 停在 {row.get('step', '')}；"
                f"原因={row.get('reason_detail', row.get('description', ''))}"
            )
    last = scoped.iloc[-1]
    if int(last.get("signal_date_after", 0)) > 0:
        return f"{label}: 已通过策略筛选并进入排序/首选；后续是否交易取决于风险过滤和成交回放。"
    return f"{label}: 未识别停止点"


def filter_trace(label: str, generator: PaperCandidateGenerator, all_candidates: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    config = generator.config
    filters = config.get("candidate_filters", {})
    rows: list[dict[str, Any]] = []

    def add(step: str, description: str, before: pd.DataFrame, after: pd.DataFrame, reason_detail: str = "") -> None:
        rows.append(
            {
                "strategy_layer": label,
                "step": step,
                "description": description,
                "reason_detail": reason_detail,
                "all_dates_before": len(before),
                "all_dates_after": len(after),
                "signal_date_before": daily_count(before, signal_date),
                "signal_date_after": daily_count(after, signal_date),
                "removed_on_signal_date": daily_count(before, signal_date) - daily_count(after, signal_date),
            }
        )

    current = all_candidates.copy()
    rows.append(
        {
            "strategy_layer": label,
            "step": "0_load_base_candidates",
            "description": "加载 next_day_premium_trades，并复用 StrategyConditionOptimizer.load_trades 的基础可交易过滤：成交概率可靠、封单异常过滤、股票池过滤等。",
            "reason_detail": "此处已经过滤掉成交概率不可靠、封单异常、不可执行样本；当日剩余样本是后续 A/B/C 的共同基础池。",
            "all_dates_before": len(current),
            "all_dates_after": len(current),
            "signal_date_before": daily_count(current, signal_date),
            "signal_date_after": daily_count(current, signal_date),
            "removed_on_signal_date": 0,
        }
    )

    before = current
    current = generator.apply_universe_filters(current)
    universe = config.get("universe", {})
    universe_desc = (
        f"exclude_st={universe.get('exclude_st')}, exclude_delisting_risk={universe.get('exclude_delisting_risk')}, "
        f"exclude_market_segments={universe.get('exclude_market_segments')}, exclude_bj={universe.get('exclude_bj')}, "
        f"exclude_sh_main={universe.get('exclude_sh_main')}, exclude_chi_next={universe.get('exclude_chi_next')}, "
        f"exclude_sz_main={universe.get('exclude_sz_main')}"
    )
    add("1_universe_filters", universe_desc, before, current, universe_reason_detail(before, current, config, signal_date))

    before = current
    current = generator.apply_include_conditions(current)
    add(
        "2_include_conditions",
        condition_desc(filters.get("conditions", [])),
        before,
        current,
        include_reason_detail(before, filters.get("conditions", []), signal_date),
    )

    before = current
    current = generator.apply_exclude_conditions(current)
    add(
        "3_exclude_conditions",
        condition_desc(filters.get("exclude_conditions", [])),
        before,
        current,
        exclude_reason_detail(before, filters.get("exclude_conditions", []), signal_date),
    )

    before = current
    current = generator.apply_exclude_rules(current)
    add(
        "4_compound_exclude_rules",
        exclude_rules_desc(filters.get("exclude_rules", [])),
        before,
        current,
        compound_exclude_reason_detail(before, filters.get("exclude_rules", []), signal_date),
    )

    daily = current[current["trade_date"].astype(str) == signal_date].copy() if "trade_date" in current.columns else pd.DataFrame()
    ranked = generator.rank_candidates(daily) if not daily.empty else pd.DataFrame()
    top_n = generator.default_top_n
    output = generator.build_output(ranked, signal_date, top_n) if not ranked.empty else pd.DataFrame()
    selected_action = generator.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    selected = output[output["planned_action"].astype(str) == selected_action].copy() if not output.empty else pd.DataFrame()
    rows.append(
        {
            "strategy_layer": label,
            "step": "5_rank_and_select_first",
            "description": f"排序字段={config.get('ranking', {}).get('columns', [])}，ascending={config.get('ranking', {}).get('ascending', [])}；top_n={top_n}；第1名 planned_action={selected_action}",
            "reason_detail": rank_reason_detail(daily, selected, config),
            "all_dates_before": len(current),
            "all_dates_after": len(current),
            "signal_date_before": len(daily),
            "signal_date_after": len(selected),
            "removed_on_signal_date": len(daily) - len(selected),
        }
    )
    return pd.DataFrame(rows)


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
    fill_path = PROJECT_ROOT / fill_config.get("output_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv")
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
        and scored_daily["limit_data_source"].fillna("").astype(str).eq("limit_list_d").all()
    )
    compatible_ok = (
        not scored_daily.empty
        and "strategy_compatible" in scored_daily.columns
        and scored_daily["strategy_compatible"].fillna("").astype(str).str.lower().isin({"true", "1"}).all()
    )
    print(f"fill_scored: exists={fill_path.exists()} signal_rows={len(scored_daily)} path={fill_path}")
    print(f"完整历史口径: {yes_no(full_quality)}")
    print(f"来源为 limit_list_d: {yes_no(source_ok)}")
    print(f"策略兼容: {yes_no(compatible_ok)}")
    print(f"缺少必需字段: {missing or '无'}")
    print(f"必需字段全为空: {empty or '无'}")
    print(f"allow_buy_reliable: {value_counts_text(scored_daily, 'allow_buy_reliable')}")
    print(f"is_fill_score_reliable: {value_counts_text(scored_daily, 'is_fill_score_reliable')}")

    print_section("3. A/B/C 操作台结果")
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
                "b_candidate_count",
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
        base_generator = PaperCandidateGenerator(args.strategy_config)
        all_candidates = base_generator.load_all_candidates()
        traces = [filter_trace("A主策略", base_generator, all_candidates, signal_date)]

        b_conditions = configured_b_conditions(strategy_config)
        b_config = backup_config(strategy_config, b_conditions)
        b_generator = PaperCandidateGenerator(args.strategy_config)
        b_generator.config = b_config
        b_generator.paper_config = b_config.get("paper_candidate", {})
        traces.append(filter_trace(f"B备用策略（{condition_text(b_conditions)}）", b_generator, all_candidates, signal_date))

        c_conditions = configured_c_conditions(strategy_config)
        if c_conditions:
            c_config = backup_config(strategy_config, c_conditions)
            c_generator = PaperCandidateGenerator(args.strategy_config)
            c_generator.config = c_config
            c_generator.paper_config = c_config.get("paper_candidate", {})
            traces.append(filter_trace(f"C补位策略（{condition_text(c_conditions)}）", c_generator, all_candidates, signal_date))

        trace = pd.concat(traces, ignore_index=True)
        print(trace.to_string(index=False))
        print()
        print("停止点判定：")
        for layer in trace["strategy_layer"].dropna().astype(str).drop_duplicates().tolist():
            print(f"- {stop_point_text(trace, layer)}")
    except Exception as exc:
        print(f"分层筛选漏斗生成失败: {exc}")

    print_section("5. 候选与过滤明细")
    for name in ["a_candidates", "b_candidates", "b_rejected_by_filter", "c_candidates", "c_rejected_by_filter"]:
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
