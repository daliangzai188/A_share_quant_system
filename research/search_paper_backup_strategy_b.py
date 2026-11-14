"""
搜索 A 策略无候选日的备用策略 B。

文件作用：
1. 读取当前严格 A 策略配置和本地候选数据。
2. 只在 A 策略无候选日里搜索 B 策略条件组合。
3. 分别输出 A 基准、B 单独、A+B 组合的收益、回撤、样本数和复核数量。
4. 标记 B 新增交易的收益来源，区分审计逐笔收益和候选近似收益。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import itertools
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config


SEARCH_COLUMNS = [
    "market_segment",
    "market_chain_count_bucket",
    "segment_limit_up_count_bucket",
    "fd_ratio_bucket",
    "segment_emotion_state_bucket",
    "market_emotion_state_bucket",
    "first_time_detail_bucket",
    "turnover_rate_bucket",
    "amount_ratio_bucket",
    "prev_pct_chg_bucket",
    "open_times_bucket",
]

DEFAULT_FIXED_B_EXCLUDES = [
    {"column": "amount_ratio_bucket", "value": "0_8_1_2"},
    {"column": "market_segment", "value": "star"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="搜索 A 策略无候选日的备用策略 B。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--end-date", default=None, help="截止日期，格式 YYYYMMDD。")
    parser.add_argument("--top-values", type=int, default=4, help="每个字段最多取出现频率最高的几个取值。")
    parser.add_argument("--max-scenarios", type=int, default=1800, help="最多评估的 B 条件组合数量。")
    parser.add_argument("--min-b-trades", type=int, default=5, help="B 单独至少成交笔数。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/backup_strategy_b/a_clean_exclude_star_prev0_3_bj_backup_b",
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


def to_float(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def has_loss_overlay_watch(value: object) -> bool:
    return "LOSS_OVERLAY_WATCH" in str(value)


def resolve_recent_dates(candidates: pd.DataFrame, recent_days: int, end_date: str | None) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于 B 策略搜索的候选日期。")
    return dates[-recent_days:]


def build_generator(strategy_config_path: str | Path, config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(strategy_config_path)
    generator.config = config
    generator.paper_config = config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    daily_runner = PaperDailyFlowRunner(strategy_config_path)
    return daily_runner.load_audit_trades(daily_runner.audit_trades_path)


def find_audit_return(audit: pd.DataFrame, signal_date: str, ts_code: str) -> float | None:
    matched = audit[
        (audit["trade_date"].astype(str) == str(signal_date))
        & (audit["ts_code"].astype(str) == str(ts_code))
    ].copy()
    if matched.empty:
        return None
    return to_float(matched.iloc[0].get("dynamic_account_return", 0.0))


def strict_config(base_config: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(base_config)


def backup_config(base_config: dict[str, Any], conditions: list[dict[str, str]]) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    config["strategy_name"] = "backup_strategy_b_research_only"
    filters = config.setdefault("candidate_filters", {})
    filters["conditions"] = [dict(condition) for condition in conditions]

    existing_excludes = filters.get("exclude_conditions", [])
    merged_excludes = existing_excludes + [
        condition
        for condition in DEFAULT_FIXED_B_EXCLUDES
        if condition not in existing_excludes
    ]
    filters["exclude_conditions"] = merged_excludes
    return config


def condition_key(conditions: list[dict[str, str]]) -> str:
    return ";".join(f"{condition['column']}={condition['value']}" for condition in conditions)


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
    if selected.empty:
        return None
    return selected.iloc[0]


def selected_trade_return(
    row: pd.Series,
    audit: pd.DataFrame,
    signal_date: str,
    position_pct: float,
) -> tuple[float, str]:
    ts_code = str(row.get("ts_code", ""))
    audit_return = find_audit_return(audit, signal_date, ts_code)
    if audit_return is not None:
        return audit_return, "audit_dynamic_account_return"
    net_return = to_float(row.get("historical_reference_net_return", 0.0))
    return net_return * position_pct, "candidate_net_return_x_position"


def build_day_row(
    signal_date: str,
    strategy_leg: str,
    operation_status: str,
    equity_before: float,
    equity_after: float,
    candidate_count: int = 0,
    row: pd.Series | None = None,
    account_return: float = 0.0,
    return_source: str = "",
    active_label: str = "",
    scenario: str = "",
) -> dict[str, Any]:
    row = row if row is not None else pd.Series(dtype=object)
    return {
        "scenario": scenario,
        "signal_date": signal_date,
        "strategy_leg": strategy_leg,
        "operation_status": operation_status,
        "candidate_count": int(candidate_count),
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "market_segment": str(row.get("market_segment", "")),
        "risk_flags": str(row.get("risk_flags", "")),
        "account_return": float(account_return),
        "equity_before": float(equity_before),
        "equity_after": float(equity_after),
        "return_source": return_source,
        "active_position": active_label,
        "historical_reference_exit_trade_date": normalize_date(row.get("historical_reference_exit_trade_date", "")),
        "live_order_enabled": False,
    }


def simulate_single_strategy(
    scenario: str,
    generator: PaperCandidateGenerator,
    filtered: pd.DataFrame,
    audit: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
    position_pct: float,
) -> pd.DataFrame:
    selected_action = generator.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows = []

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg=scenario,
                    operation_status="POSITION_OCCUPIED_SKIP",
                    equity_before=equity,
                    equity_after=equity,
                    active_label=active_label,
                    scenario=scenario,
                )
            )
            continue

        output = apply_and_rank(generator, filtered, signal_date)
        if output.empty:
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg=scenario,
                    operation_status="NO_CANDIDATE",
                    equity_before=equity,
                    equity_after=equity,
                    scenario=scenario,
                )
            )
            continue

        selected = selected_candidate(output, selected_action)
        if selected is None:
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg=scenario,
                    operation_status="NO_SELECTED",
                    equity_before=equity,
                    equity_after=equity,
                    candidate_count=len(output),
                    scenario=scenario,
                )
            )
            continue

        if has_loss_overlay_watch(selected.get("risk_flags", "")):
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg=scenario,
                    operation_status="REVIEW_REQUIRED_PLAN_ONLY",
                    equity_before=equity,
                    equity_after=equity,
                    candidate_count=len(output),
                    row=selected,
                    return_source="manual_review_skip",
                    scenario=scenario,
                )
            )
            continue

        account_return, return_source = selected_trade_return(selected, audit, signal_date, position_pct)
        equity_before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(selected.get("historical_reference_exit_trade_date", ""))
        active_label = f"{selected.get('ts_code', '')} {selected.get('name', '')}"
        rows.append(
            build_day_row(
                signal_date=signal_date,
                strategy_leg=scenario,
                operation_status="HISTORICAL_SIM_FILLED",
                equity_before=equity_before,
                equity_after=equity,
                candidate_count=len(output),
                row=selected,
                account_return=account_return,
                return_source=return_source,
                scenario=scenario,
            )
        )

    detail = pd.DataFrame(rows)
    return attach_drawdown(detail, initial_equity)


def simulate_a_plus_b(
    scenario: str,
    a_generator: PaperCandidateGenerator,
    a_filtered: pd.DataFrame,
    b_generator: PaperCandidateGenerator,
    b_filtered: pd.DataFrame,
    audit: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
    position_pct: float,
) -> pd.DataFrame:
    selected_action = a_generator.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows = []

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg="A_OR_B",
                    operation_status="POSITION_OCCUPIED_SKIP",
                    equity_before=equity,
                    equity_after=equity,
                    active_label=active_label,
                    scenario=scenario,
                )
            )
            continue

        a_output = apply_and_rank(a_generator, a_filtered, signal_date)
        a_selected = selected_candidate(a_output, selected_action)
        source_leg = "A"
        output = a_output
        selected = a_selected

        if a_selected is None:
            b_output = apply_and_rank(b_generator, b_filtered, signal_date)
            b_selected = selected_candidate(b_output, selected_action)
            source_leg = "B"
            output = b_output
            selected = b_selected

        if selected is None:
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg="NONE",
                    operation_status="NO_CANDIDATE",
                    equity_before=equity,
                    equity_after=equity,
                    candidate_count=0 if output.empty else len(output),
                    scenario=scenario,
                )
            )
            continue

        if has_loss_overlay_watch(selected.get("risk_flags", "")):
            rows.append(
                build_day_row(
                    signal_date=signal_date,
                    strategy_leg=source_leg,
                    operation_status="REVIEW_REQUIRED_PLAN_ONLY",
                    equity_before=equity,
                    equity_after=equity,
                    candidate_count=len(output),
                    row=selected,
                    return_source="manual_review_skip",
                    scenario=scenario,
                )
            )
            continue

        account_return, return_source = selected_trade_return(selected, audit, signal_date, position_pct)
        equity_before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(selected.get("historical_reference_exit_trade_date", ""))
        active_label = f"{selected.get('ts_code', '')} {selected.get('name', '')}"
        rows.append(
            build_day_row(
                signal_date=signal_date,
                strategy_leg=source_leg,
                operation_status="HISTORICAL_SIM_FILLED",
                equity_before=equity_before,
                equity_after=equity,
                candidate_count=len(output),
                row=selected,
                account_return=account_return,
                return_source=return_source,
                scenario=scenario,
            )
        )

    detail = pd.DataFrame(rows)
    return attach_drawdown(detail, initial_equity)


def attach_drawdown(detail: pd.DataFrame, initial_equity: float) -> pd.DataFrame:
    result = detail.copy()
    if result.empty:
        return result
    result["initial_equity"] = initial_equity
    result["peak_equity"] = result["equity_after"].cummax().clip(lower=initial_equity)
    result["drawdown"] = result["equity_after"] / result["peak_equity"] - 1.0
    return result


def summarize_detail(detail: pd.DataFrame, scenario: str, conditions: str, mode: str) -> dict[str, Any]:
    status_counts = detail["operation_status"].value_counts().to_dict() if not detail.empty else {}
    trades = detail[detail["operation_status"] == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    initial_equity = float(detail["initial_equity"].iloc[0]) if "initial_equity" in detail.columns and not detail.empty else 0.0
    final_equity = float(detail["equity_after"].iloc[-1]) if not detail.empty else 0.0
    b_trade_count = (
        int(len(trades))
        if mode == "B_ONLY_ON_A_NO_CANDIDATE_DAYS"
        else int((trades.get("strategy_leg", pd.Series(dtype=str)) == "B").sum())
    )
    return {
        "scenario": scenario,
        "mode": mode,
        "conditions": conditions,
        "day_count": int(len(detail)),
        "executed_trade_count": int(len(trades)),
        "b_trade_count": b_trade_count,
        "review_required_count": int(status_counts.get("REVIEW_REQUIRED_PLAN_ONLY", 0)),
        "no_candidate_count": int(status_counts.get("NO_CANDIDATE", 0)),
        "position_occupied_skip_count": int(status_counts.get("POSITION_OCCUPIED_SKIP", 0)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
        "max_drawdown": float(detail["drawdown"].min()) if "drawdown" in detail.columns and not detail.empty else 0.0,
        "candidate_return_source_count": int(
            (trades.get("return_source", pd.Series(dtype=str)) == "candidate_net_return_x_position").sum()
        ),
        "audit_return_source_count": int(
            (trades.get("return_source", pd.Series(dtype=str)) == "audit_dynamic_account_return").sum()
        ),
        "live_order_enabled": False,
    }


def top_values_by_column(data: pd.DataFrame, columns: list[str], top_values: int) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for column in columns:
        if column not in data.columns:
            continue
        values = (
            data[column]
            .fillna("missing")
            .astype(str)
            .loc[lambda series: series.ne("missing") & series.ne("")]
            .value_counts()
            .head(top_values)
            .index.tolist()
        )
        if values:
            result[column] = values
    return result


def generate_condition_sets(value_map: dict[str, list[str]], max_scenarios: int) -> list[list[dict[str, str]]]:
    condition_sets: list[list[dict[str, str]]] = []
    columns = list(value_map)

    for column in columns:
        for value in value_map[column]:
            condition_sets.append([{"column": column, "value": value}])

    for left, right in itertools.combinations(columns, 2):
        for left_value in value_map[left][:3]:
            for right_value in value_map[right][:3]:
                condition_sets.append(
                    [
                        {"column": left, "value": left_value},
                        {"column": right, "value": right_value},
                    ]
                )

    priority_groups = [
        ("market_segment", "market_chain_count_bucket", "fd_ratio_bucket"),
        ("market_segment", "segment_limit_up_count_bucket", "fd_ratio_bucket"),
        ("market_segment", "segment_emotion_state_bucket", "first_time_detail_bucket"),
        ("market_chain_count_bucket", "fd_ratio_bucket", "turnover_rate_bucket"),
        ("segment_limit_up_count_bucket", "fd_ratio_bucket", "prev_pct_chg_bucket"),
    ]
    for group in priority_groups:
        if not all(column in value_map for column in group):
            continue
        for values in itertools.product(*(value_map[column][:3] for column in group)):
            condition_sets.append(
                [{"column": column, "value": value} for column, value in zip(group, values)]
            )

    unique: dict[str, list[dict[str, str]]] = {}
    for conditions in condition_sets:
        key = condition_key(conditions)
        if key not in unique:
            unique[key] = conditions
        if len(unique) >= max_scenarios:
            break
    return list(unique.values())


def scoped_no_candidate_dates(strict_detail: pd.DataFrame) -> list[str]:
    return (
        strict_detail.loc[strict_detail["operation_status"].astype(str) == "NO_CANDIDATE", "signal_date"]
        .map(normalize_date)
        .tolist()
    )


def write_markdown(path: Path, summary: pd.DataFrame, top_detail: pd.DataFrame, strict_summary: pd.DataFrame) -> None:
    summary_columns = [
        "scenario",
        "mode",
        "conditions",
        "executed_trade_count",
        "b_trade_count",
        "review_required_count",
        "no_candidate_count",
        "win_rate",
        "equity_multiple",
        "max_drawdown",
        "candidate_return_source_count",
        "audit_return_source_count",
    ]
    summary_columns = [column for column in summary_columns if column in summary.columns]
    detail_columns = [
        "scenario",
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
    ]
    detail_columns = [column for column in detail_columns if column in top_detail.columns]
    content = f"""# A 主策略 + B 备用策略搜索

本报告只使用本地历史数据做研究，不接实盘，不调用 QMT，不下真实订单。

## A 基准

{strict_summary.to_markdown(index=False)}

## Top B / A+B 方案

{summary[summary_columns].head(30).to_markdown(index=False) if not summary.empty else "无可用 B 方案。"}

## 最优 A+B 逐日明细

{top_detail[detail_columns].to_markdown(index=False) if not top_detail.empty else "无逐日明细。"}

## 口径限制

- B 只在 A 无候选日启用，和 A 不抢同一天信号。
- `candidate_net_return_x_position` 是候选收益近似，不是严格逐笔成交审计。
- 命中人工复核的交易不计入收益。
- 搜索结果只能作为下一轮严格审计候选，不能直接实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_recent_dates(all_candidates, args.recent_days, args.end_date)
    audit = load_audit(args.strategy_config)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(base_config.get("position", {}).get("target_position_pct", 0.8))

    a_generator = build_generator(args.strategy_config, strict_config(base_config))
    a_filtered = a_generator.apply_strategy_filters(all_candidates)
    strict_detail = simulate_single_strategy(
        scenario="A_strict",
        generator=a_generator,
        filtered=a_filtered,
        audit=audit,
        dates=dates,
        initial_equity=initial_equity,
        position_pct=position_pct,
    )
    strict_summary = pd.DataFrame(
        [summarize_detail(strict_detail, "A_strict", "current_config", "A_ONLY")]
    )
    no_candidate_dates = scoped_no_candidate_dates(strict_detail)
    if not no_candidate_dates:
        raise RuntimeError("A 策略在当前窗口没有无候选日，不需要搜索 B。")

    scoped = all_candidates[all_candidates["trade_date"].map(normalize_date).isin(no_candidate_dates)].copy()
    scoped = a_generator.apply_universe_filters(scoped)
    value_map = top_values_by_column(scoped, SEARCH_COLUMNS, args.top_values)
    condition_sets = generate_condition_sets(value_map, args.max_scenarios)

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    best_combo_detail = pd.DataFrame()
    best_combo_multiple = -1.0

    for idx, conditions in enumerate(condition_sets, start=1):
        scenario = f"B_{idx:04d}"
        conditions_text = condition_key(conditions)
        b_config = backup_config(base_config, conditions)
        b_generator = build_generator(args.strategy_config, b_config)
        b_filtered = b_generator.apply_strategy_filters(all_candidates)
        if b_filtered.empty:
            continue

        b_detail = simulate_single_strategy(
            scenario=scenario,
            generator=b_generator,
            filtered=b_filtered[b_filtered["trade_date"].map(normalize_date).isin(no_candidate_dates)].copy(),
            audit=audit,
            dates=no_candidate_dates,
            initial_equity=initial_equity,
            position_pct=position_pct,
        )
        b_summary = summarize_detail(b_detail, scenario, conditions_text, "B_ONLY_ON_A_NO_CANDIDATE_DAYS")
        if b_summary["executed_trade_count"] < args.min_b_trades:
            continue

        combo_detail = simulate_a_plus_b(
            scenario=f"A_plus_{scenario}",
            a_generator=a_generator,
            a_filtered=a_filtered,
            b_generator=b_generator,
            b_filtered=b_filtered,
            audit=audit,
            dates=dates,
            initial_equity=initial_equity,
            position_pct=position_pct,
        )
        combo_summary = summarize_detail(combo_detail, f"A_plus_{scenario}", conditions_text, "A_PLUS_B")
        summary_rows.append(b_summary)
        summary_rows.append(combo_summary)

        if combo_summary["equity_multiple"] > best_combo_multiple:
            best_combo_multiple = float(combo_summary["equity_multiple"])
            best_combo_detail = combo_detail

        if len(detail_frames) < 10:
            detail_frames.append(combo_detail)

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary = summary.sort_values(
            ["mode", "equity_multiple", "max_drawdown"],
            ascending=[True, False, False],
        ).reset_index(drop=True)
    sampled_detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{args.recent_days}d"
    strict_path = output_prefix.with_name(output_prefix.name + suffix + "_a_strict_detail.csv")
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    sampled_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_sampled_combo_detail.csv")
    best_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_best_combo_detail.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    strict_detail.to_csv(strict_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    sampled_detail.to_csv(sampled_detail_path, index=False, encoding="utf-8-sig")
    best_combo_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, best_combo_detail, strict_summary)

    print("A 主策略 + B 备用策略搜索完成：")
    print(f"- strict_detail: {strict_path}")
    print(f"- summary: {summary_path}")
    print(f"- sampled_combo_detail: {sampled_detail_path}")
    print(f"- best_combo_detail: {best_detail_path}")
    print(f"- markdown: {markdown_path}")
    print(strict_summary.to_string(index=False))
    print(summary.head(30).to_string(index=False) if not summary.empty else "没有找到满足最小成交笔数的 B 方案。")


if __name__ == "__main__":
    main()
