"""
搜索 A严格策略 + B0018过滤版之后的备用策略 C。

文件作用：
1. 固定当前 A 主策略和 B0018 过滤版，不改变既有 A/B 逻辑。
2. 只在 A+B 都没有候选且没有持仓占用的交易日里搜索 C 策略。
3. C 使用涨停池、连板、封单、情绪、换手等龙头战法相关因子组合。
4. 对 C 候选使用日线保守成交回放，处理 T+1、涨停买不到、跌停卖不出、滑点和费用。
5. 输出 A+B 基准、C 单独、A+B+C 组合的收益、回撤、样本数和风险缺口。

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

from scripts.audit_backup_strategy_b import (
    build_audit_row,
    replay_map,
    replay_selected_b,
    selected_b_signals,
    simulate_a_plus_b_strict,
    simulate_b_strict,
    summarize,
)
from scripts.run_paper_ab_filtered_observation_window import (
    configured_b_conditions,
    reject_b_risk_mask,
)
from scripts.search_paper_backup_strategy_b import (
    DEFAULT_FIXED_B_EXCLUDES,
    backup_config,
    build_generator,
    normalize_date,
    scoped_no_candidate_dates,
    simulate_single_strategy,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config


SEARCH_COLUMNS = [
    "market_segment",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "fd_ratio_bucket",
    "segment_emotion_state_bucket",
    "market_emotion_state_bucket",
    "first_time_detail_bucket",
    "turnover_rate_bucket",
    "amount_ratio_bucket",
    "prev_pct_chg_bucket",
    "open_times_bucket",
    "limit_times_detail_bucket",
    "limit_height_rank_bucket",
    "segment_limit_height_rank_bucket",
]

PRIORITY_GROUPS = [
    ("market_chain_count_bucket", "segment_limit_up_count_bucket", "fd_ratio_bucket"),
    ("segment_chain_count_bucket", "segment_emotion_state_bucket", "fd_ratio_bucket"),
    ("market_segment", "segment_emotion_state_bucket", "first_time_detail_bucket"),
    ("segment_limit_up_count_bucket", "open_times_bucket", "turnover_rate_bucket"),
    ("market_chain_count_bucket", "fd_ratio_bucket", "prev_pct_chg_bucket"),
    ("market_emotion_state_bucket", "segment_emotion_state_bucket", "amount_ratio_bucket"),
]

DEEP_PRIORITY_GROUPS = [
    (
        "market_chain_count_bucket",
        "segment_limit_up_count_bucket",
        "segment_emotion_state_bucket",
        "turnover_rate_bucket",
    ),
    (
        "market_chain_count_bucket",
        "segment_limit_up_count_bucket",
        "fd_ratio_bucket",
        "first_time_detail_bucket",
    ),
    (
        "market_segment",
        "segment_emotion_state_bucket",
        "turnover_rate_bucket",
        "first_time_detail_bucket",
    ),
    (
        "segment_chain_count_bucket",
        "segment_limit_up_count_bucket",
        "open_times_bucket",
        "turnover_rate_bucket",
    ),
    (
        "market_chain_count_bucket",
        "segment_limit_up_count_bucket",
        "fd_ratio_bucket",
        "prev_pct_chg_bucket",
        "first_time_detail_bucket",
    ),
]

AB_NO_FILL_STATUSES = {
    "NO_CANDIDATE",
    "NO_SELECTED",
    "BUY_REJECTED",
    "REVIEW_REQUIRED_PLAN_ONLY",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="搜索 A+B 后的备用策略 C。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--start-date", default=None, help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", default=None, help="截止日期，格式 YYYYMMDD。")
    parser.add_argument("--recent-days", type=int, default=481, help="最近交易日数量。")
    parser.add_argument("--top-values", type=int, default=5, help="每个字段最多取出现频率最高的几个取值。")
    parser.add_argument("--max-scenarios", type=int, default=4500, help="最多评估的 C 条件组合数量。")
    parser.add_argument("--strict-top-n", type=int, default=80, help="轻量筛选后进入严格成交回放的 C 组合数量。")
    parser.add_argument("--min-c-trades", type=int, default=8, help="C 单独至少成交笔数。")
    parser.add_argument("--max-c-drawdown", type=float, default=0.25, help="C 单独最大回撤绝对值上限。")
    parser.add_argument("--max-combo-drawdown", type=float, default=0.18, help="A+B+C 组合最大回撤绝对值上限。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/backup_strategy_c/a_strict_plus_b0018_filtered_backup_c",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def resolve_dates(
    candidates: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
    recent_days: int,
) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if start_date:
        dates = [date for date in dates if date >= str(start_date)]
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于 C 策略搜索的候选日期。")
    return dates[-recent_days:]


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    runner = PaperDailyFlowRunner(strategy_config_path)
    return runner.load_audit_trades(runner.audit_trades_path)


def condition_key(conditions: list[dict[str, str]]) -> str:
    return ";".join(f"{condition['column']}={condition['value']}" for condition in conditions)


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
        for left_value in value_map[left][:4]:
            for right_value in value_map[right][:4]:
                condition_sets.append(
                    [
                        {"column": left, "value": left_value},
                        {"column": right, "value": right_value},
                    ]
                )

    for group in PRIORITY_GROUPS:
        if not all(column in value_map for column in group):
            continue
        for values in itertools.product(*(value_map[column][:4] for column in group)):
            condition_sets.append(
                [{"column": column, "value": value} for column, value in zip(group, values)]
            )

    for group in DEEP_PRIORITY_GROUPS:
        if not all(column in value_map for column in group):
            continue
        value_limit = 3 if len(group) <= 4 else 2
        for values in itertools.product(*(value_map[column][:value_limit] for column in group)):
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


def ab_available_dates(combo_detail: pd.DataFrame) -> list[str]:
    """返回 A/B 没有成交且没有持仓占用的日期，供 C 继续尝试。"""
    return (
        combo_detail.loc[combo_detail["operation_status"].astype(str).isin(AB_NO_FILL_STATUSES), "signal_date"]
        .map(normalize_date)
        .tolist()
    )


def configured_c_config(base_config: dict[str, Any], conditions: list[dict[str, str]]) -> dict[str, Any]:
    config = backup_config(base_config, conditions)
    config["strategy_name"] = "backup_strategy_c_research_only"
    return config


def prepare_ranked_scope(
    generator: PaperCandidateGenerator,
    scoped: pd.DataFrame,
    base_config: dict[str, Any],
) -> pd.DataFrame:
    result = generator.apply_universe_filters(scoped)
    for condition in DEFAULT_FIXED_B_EXCLUDES:
        column = str(condition["column"])
        value = str(condition["value"])
        if column in result.columns:
            result = result[result[column].fillna("missing").astype(str) != value].copy()
    result["profit_source_score"] = generator.calculate_profit_source_score(result)
    ranking = base_config.get("ranking", {})
    columns = [column for column in ranking.get("columns", []) if column in result.columns]
    if not columns:
        columns = ["fill_probability"]
    ascending = list(ranking.get("ascending", []))[: len(columns)]
    if len(ascending) != len(columns):
        ascending = [False] * len(columns)
    sort_columns = columns + ["amount", "turnover_rate", "ts_code"]
    sort_ascending = ascending + [False, False, True]
    return result.sort_values(sort_columns, ascending=sort_ascending).reset_index(drop=True)


def audit_return_map(audit: pd.DataFrame) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for row in audit.itertuples(index=False):
        trade_date = normalize_date(getattr(row, "trade_date", ""))
        ts_code = str(getattr(row, "ts_code", ""))
        value = pd.to_numeric(getattr(row, "dynamic_account_return", 0.0), errors="coerce")
        result[(trade_date, ts_code)] = 0.0 if pd.isna(value) else float(value)
    return result


def filter_by_conditions(data: pd.DataFrame, conditions: list[dict[str, str]]) -> pd.DataFrame:
    result = data
    for condition in conditions:
        column = str(condition["column"])
        value = str(condition["value"])
        if column not in result.columns:
            return result.iloc[0:0].copy()
        result = result[result[column].fillna("missing").astype(str) == value]
        if result.empty:
            return result.copy()
    return result.copy()


def quick_selected_by_conditions(
    ranked_scope: pd.DataFrame,
    conditions: list[dict[str, str]],
) -> pd.DataFrame:
    matched = filter_by_conditions(ranked_scope, conditions)
    if matched.empty:
        return matched
    return matched.drop_duplicates("trade_date", keep="first").copy()


def quick_simulate_c(
    selected: pd.DataFrame,
    dates: list[str],
    audit_returns: dict[tuple[str, str], float],
    initial_equity: float,
    position_pct: float,
) -> dict[str, Any]:
    selected_by_date = {
        normalize_date(row.trade_date): pd.Series(row._asdict())
        for row in selected.itertuples(index=False)
    }
    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    active_exit_date = ""
    returns: list[float] = []
    no_candidate = 0
    occupied = 0

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            occupied += 1
            continue
        row = selected_by_date.get(signal_date)
        if row is None:
            no_candidate += 1
            continue
        ts_code = str(row.get("ts_code", ""))
        account_return = audit_returns.get((signal_date, ts_code))
        if account_return is None:
            net_return = pd.to_numeric(row.get("net_return", 0.0), errors="coerce")
            account_return = 0.0 if pd.isna(net_return) else float(net_return) * position_pct
        returns.append(float(account_return))
        equity = equity * (1.0 + float(account_return))
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
        active_exit_date = normalize_date(row.get("exit_trade_date", ""))

    returns_series = pd.Series(returns, dtype=float)
    return {
        "executed_trade_count": int(len(returns)),
        "no_candidate_count": int(no_candidate),
        "position_occupied_skip_count": int(occupied),
        "win_rate": float((returns_series > 0).mean()) if len(returns_series) else 0.0,
        "avg_account_return": float(returns_series.mean()) if len(returns_series) else 0.0,
        "median_account_return": float(returns_series.median()) if len(returns_series) else 0.0,
        "max_profit": float(returns_series.max()) if len(returns_series) else 0.0,
        "max_loss": float(returns_series.min()) if len(returns_series) else 0.0,
        "initial_equity": float(initial_equity),
        "final_equity": float(equity),
        "equity_multiple": float(equity / initial_equity) if initial_equity else 0.0,
        "max_drawdown": float(max_drawdown),
    }


def selected_c_signals(
    c_generator: PaperCandidateGenerator,
    c_filtered: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    return selected_b_signals(c_generator, c_filtered, dates)


def replay_selected_c(selected: pd.DataFrame, runtime_config: str | Path) -> pd.DataFrame:
    return replay_selected_b(selected, runtime_config)


def replay_selected_with_cache(
    selected: pd.DataFrame,
    replay_engine: ConservativeTradeReplay,
    forward: pd.DataFrame,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    samples = selected.merge(forward, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
    replayed = replay_engine.replay_rule(samples, replay_rule)
    replayed["strict_account_return"] = pd.to_numeric(replayed["daily_return"], errors="coerce").fillna(0.0)
    replayed["strict_return_source"] = "conservative_daily_replay"
    return replayed.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def reject_c_risk_mask(replayed_c: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    return reject_b_risk_mask(replayed_c, config)


def simulate_a_plus_b_plus_c_strict(
    ab_detail: pd.DataFrame,
    replayed_c: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
) -> pd.DataFrame:
    c_by_key = replay_map(replayed_c)
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows: list[dict[str, Any]] = []
    ab_by_date = {
        normalize_date(row.signal_date): pd.Series(row._asdict())
        for row in ab_detail.itertuples(index=False)
    }

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    "A_B_OR_C",
                    "POSITION_OCCUPIED_SKIP",
                    equity,
                    equity,
                    active_label=active_label,
                )
            )
            continue

        ab_row = ab_by_date.get(signal_date)
        if ab_row is not None and str(ab_row.get("operation_status", "")) == "HISTORICAL_SIM_FILLED":
            account_return = float(ab_row.get("account_return", 0.0))
            before = equity
            equity = equity * (1.0 + account_return)
            active_exit_date = normalize_date(ab_row.get("exit_trade_date", ""))
            active_label = f"{ab_row.get('ts_code', '')} {ab_row.get('name', '')}"
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    str(ab_row.get("strategy_leg", "A_OR_B")),
                    "HISTORICAL_SIM_FILLED",
                    before,
                    equity,
                    row=ab_row,
                    account_return=account_return,
                    return_source=str(ab_row.get("return_source", "")),
                )
            )
            continue

        if ab_row is not None and str(ab_row.get("operation_status", "")) not in AB_NO_FILL_STATUSES:
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    str(ab_row.get("strategy_leg", "A_OR_B")),
                    str(ab_row.get("operation_status", "NO_CANDIDATE")),
                    equity,
                    equity,
                    row=ab_row,
                    return_source=str(ab_row.get("return_source", "")),
                )
            )
            continue

        daily_keys = [key for key in c_by_key if key[0] == signal_date]
        if not daily_keys:
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    "NONE",
                    "NO_CANDIDATE",
                    equity,
                    equity,
                )
            )
            continue

        c_row = c_by_key[daily_keys[0]]
        if not bool(c_row.get("buy_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    "C",
                    "BUY_REJECTED",
                    equity,
                    equity,
                    row=c_row,
                    return_source="c_conservative_daily_replay",
                )
            )
            continue
        if not bool(c_row.get("sell_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_strict",
                    signal_date,
                    "C",
                    "SELL_UNRESOLVED",
                    equity,
                    equity,
                    row=c_row,
                    return_source="c_conservative_daily_replay",
                )
            )
            active_exit_date = normalize_date(c_row.get("exit_trade_date", ""))
            active_label = f"{c_row.get('ts_code', '')} {c_row.get('name', '')}"
            continue

        account_return = float(c_row.get("strict_account_return", 0.0))
        before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(c_row.get("exit_trade_date", ""))
        active_label = f"{c_row.get('ts_code', '')} {c_row.get('name', '')}"
        rows.append(
            build_audit_row(
                "A_plus_B_plus_C_strict",
                signal_date,
                "C",
                "HISTORICAL_SIM_FILLED",
                before,
                equity,
                row=c_row,
                account_return=account_return,
                return_source="c_conservative_daily_replay",
            )
        )
    return attach_drawdown(pd.DataFrame(rows), initial_equity)


def attach_drawdown(detail: pd.DataFrame, initial_equity: float) -> pd.DataFrame:
    result = detail.copy()
    if result.empty:
        return result
    result["initial_equity"] = initial_equity
    result["peak_equity"] = result["equity_after"].cummax().clip(lower=initial_equity)
    result["drawdown"] = result["equity_after"] / result["peak_equity"] - 1.0
    return result


def c_summary(detail: pd.DataFrame, scenario: str, condition: str) -> dict[str, Any]:
    row = summarize(detail, scenario, condition)
    trades = detail[detail["operation_status"].astype(str) == "HISTORICAL_SIM_FILLED"].copy()
    row["c_trade_count"] = int((trades.get("strategy_leg", pd.Series(dtype=str)) == "C").sum())
    row["candidate_return_source_count"] = int(
        trades.get("return_source", pd.Series(dtype=str)).astype(str).str.contains("candidate").sum()
    )
    row["audit_or_replay_return_source_count"] = int(len(trades) - row["candidate_return_source_count"])
    return row


def write_markdown(
    path: Path,
    strict_summary: pd.DataFrame,
    summary: pd.DataFrame,
    best_detail: pd.DataFrame,
    rejected_c: pd.DataFrame,
) -> None:
    summary_columns = [
        "scenario",
        "condition",
        "executed_trade_count",
        "a_trade_count",
        "b_trade_count",
        "c_trade_count",
        "win_rate",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "buy_rejected_count",
        "sell_unresolved_count",
        "limit_down_blocked_trade_count",
    ]
    summary_columns = [column for column in summary_columns if column in summary.columns]
    detail_columns = [
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
        "buy_reject_reason",
        "sell_reject_reason",
    ]
    detail_columns = [column for column in detail_columns if column in best_detail.columns]
    rejected_columns = [
        "trade_date",
        "ts_code",
        "name",
        "strict_account_return",
        "risk_flags",
        "fd_ratio_bucket",
        "market_chain_count_bucket",
        "open_times",
    ]
    rejected_columns = [column for column in rejected_columns if column in rejected_c.columns]
    content = f"""# A+B 后备用策略 C 搜索

本报告只使用本地日线数据和保守成交模型，不接实盘，不调用 QMT，不下真实订单。

## A+B 基准

{strict_summary.to_markdown(index=False)}

## Top C / A+B+C 方案

{summary[summary_columns].head(30).to_markdown(index=False) if not summary.empty else "无可用 C 方案。"}

## 最优 A+B+C 逐日明细

{best_detail[detail_columns].to_markdown(index=False) if not best_detail.empty else "无逐日明细。"}

## C 风险过滤跳过清单

{rejected_c[rejected_columns].to_markdown(index=False) if not rejected_c.empty else "无 C 风险过滤跳过项。"}

## 口径限制

- C 只在 A/B 都没有成交且没有持仓占用时启用。
- C 使用和 B 相同的事前风险过滤：封单/流通市值偏高、LOSS_OVERLAY_WATCH、open_times >= 4。
- 当前是日线保守成交口径，不等于盘口五档真实撮合。
- 搜索结果只能作为后续分钟 K / 盘口验证候选，不能直接实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    if bool(base_config.get("live_trading_enabled", False)) or bool(base_config.get("qmt_enabled", False)):
        raise RuntimeError("拒绝运行 C 搜索：配置存在实盘或 QMT 开关。")

    print("阶段1/7：加载本地候选数据...", flush=True)
    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_dates(all_candidates, args.start_date, args.end_date, args.recent_days)
    print(f"阶段1/7完成：候选行数={len(all_candidates)}, 窗口交易日={len(dates)}", flush=True)

    print("阶段2/7：加载 A 审计交易明细...", flush=True)
    audit = load_audit(args.strategy_config)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(base_config.get("position", {}).get("target_position_pct", 0.8))
    print(f"阶段2/7完成：审计行数={len(audit)}", flush=True)

    print("阶段3/7：生成 A 严格策略基准...", flush=True)
    a_generator = build_generator(args.strategy_config, copy.deepcopy(base_config))
    a_filtered = a_generator.apply_strategy_filters(all_candidates)
    a_detail = simulate_single_strategy(
        scenario="A_strict",
        generator=a_generator,
        filtered=a_filtered,
        audit=audit,
        dates=dates,
        initial_equity=initial_equity,
        position_pct=position_pct,
    )
    a_no_candidate_dates = scoped_no_candidate_dates(a_detail)
    print(f"阶段3/7完成：A过滤后行数={len(a_filtered)}, A无候选日={len(a_no_candidate_dates)}", flush=True)

    print("阶段4/7：生成 B0018 过滤版基准...", flush=True)
    b_conditions = configured_b_conditions(base_config)
    b_config = backup_config(base_config, b_conditions)
    b_generator = build_generator(args.strategy_config, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, a_no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, args.runtime_config)
    b_rejected_mask = reject_b_risk_mask(replayed_b, base_config)
    replayed_b_filtered = replayed_b[~b_rejected_mask].copy()

    ab_detail = simulate_a_plus_b_strict(a_detail, replayed_b_filtered, audit, dates, initial_equity)
    ab_summary = pd.DataFrame([summarize(ab_detail, "A_plus_B0018_filtered", condition_key(b_conditions))])
    c_dates = ab_available_dates(ab_detail)
    if not c_dates:
        raise RuntimeError("A/B 在当前窗口没有可补 C 的未成交日期。")
    print(
        "阶段4/7完成："
        f"B过滤前信号={len(selected_b)}, B风险过滤后={len(replayed_b_filtered)}, A/B未成交可补C日={len(c_dates)}",
        flush=True,
    )

    print("阶段5/7：准备 C 向量化粗筛数据...", flush=True)
    scoped = all_candidates[all_candidates["trade_date"].map(normalize_date).isin(c_dates)].copy()
    ranked_scope = prepare_ranked_scope(a_generator, scoped, base_config)
    audit_returns = audit_return_map(audit)
    value_map = top_values_by_column(ranked_scope, SEARCH_COLUMNS, args.top_values)
    condition_sets = generate_condition_sets(value_map, args.max_scenarios)
    print(
        f"阶段5/7完成：C可补日期候选行数={len(ranked_scope)}, 条件组合数={len(condition_sets)}",
        flush=True,
    )

    print("阶段6/7：C 条件组合轻量筛选...", flush=True)
    approx_candidates: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    best_detail = pd.DataFrame()
    best_replayed_c = pd.DataFrame()
    best_rejected_c = pd.DataFrame()
    best_score = -1.0

    for idx, conditions in enumerate(condition_sets, start=1):
        if idx % 250 == 0:
            print(f"C 策略轻量筛选进度: {idx}/{len(condition_sets)}", flush=True)

        selected_approx = quick_selected_by_conditions(ranked_scope, conditions)
        if selected_approx.empty:
            continue

        approx_row = quick_simulate_c(
            selected=selected_approx,
            dates=c_dates,
            audit_returns=audit_returns,
            initial_equity=initial_equity,
            position_pct=position_pct,
        )
        if approx_row["executed_trade_count"] < args.min_c_trades:
            continue
        if abs(float(approx_row["max_drawdown"])) > args.max_c_drawdown:
            continue
        approx_candidates.append(
            {
                "idx": idx,
                "conditions": conditions,
                "condition": condition_key(conditions),
                "approx_equity_multiple": float(approx_row["equity_multiple"]),
                "approx_executed_trade_count": int(approx_row["executed_trade_count"]),
                "approx_win_rate": float(approx_row["win_rate"]),
                "approx_max_drawdown": float(approx_row["max_drawdown"]),
                "approx_max_loss": float(approx_row["max_loss"]),
            }
        )

    approx_candidates = sorted(
        approx_candidates,
        key=lambda item: (
            item["approx_equity_multiple"],
            item["approx_executed_trade_count"],
            item["approx_max_drawdown"],
        ),
        reverse=True,
    )

    strict_candidates = approx_candidates[: max(1, int(args.strict_top_n))]
    print(f"阶段6/7完成：进入严格回放 {len(strict_candidates)}/{len(approx_candidates)}", flush=True)

    print("阶段7/7：Top C 条件严格成交回放...", flush=True)
    replay_engine = ConservativeTradeReplay(config_path=args.runtime_config)
    forward = replay_engine.load_forward_prices()
    for strict_rank, item in enumerate(strict_candidates, start=1):
        idx = int(item["idx"])
        conditions = list(item["conditions"])
        if strict_rank % 10 == 0:
            print(f"C 策略严格回放进度: {strict_rank}/{len(strict_candidates)}", flush=True)

        c_config = configured_c_config(base_config, conditions)
        c_generator = build_generator(args.strategy_config, c_config)
        c_filtered = c_generator.apply_strategy_filters(all_candidates)
        selected_c = selected_c_signals(c_generator, c_filtered, c_dates)
        if selected_c.empty:
            continue

        replayed_c = replay_selected_with_cache(selected_c, replay_engine, forward)
        if replayed_c.empty:
            continue

        rejected_mask = reject_c_risk_mask(replayed_c, base_config)
        rejected_c = replayed_c[rejected_mask].copy()
        replayed_c_filtered = replayed_c[~rejected_mask].copy()
        c_detail = simulate_b_strict(replayed_c_filtered, c_dates, initial_equity)
        c_detail["scenario"] = f"C_{idx:04d}_strict"
        c_detail["strategy_leg"] = c_detail["strategy_leg"].replace({"B": "C"})
        c_row = c_summary(c_detail, f"C_{idx:04d}_strict", condition_key(conditions))

        if c_row["executed_trade_count"] < args.min_c_trades:
            continue
        if abs(float(c_row["max_drawdown"])) > args.max_c_drawdown:
            continue

        combo_detail = simulate_a_plus_b_plus_c_strict(ab_detail, replayed_c_filtered, dates, initial_equity)
        combo_row = c_summary(combo_detail, f"A_plus_B_plus_C_{idx:04d}", condition_key(conditions))
        if abs(float(combo_row["max_drawdown"])) > args.max_combo_drawdown:
            continue

        for row in (c_row, combo_row):
            row["strict_rank"] = strict_rank
            row["approx_equity_multiple"] = item["approx_equity_multiple"]
            row["approx_executed_trade_count"] = item["approx_executed_trade_count"]
            row["approx_win_rate"] = item["approx_win_rate"]
            row["approx_max_drawdown"] = item["approx_max_drawdown"]

        summary_rows.append(c_row)
        summary_rows.append(combo_row)

        score = float(combo_row["equity_multiple"]) + float(combo_row["executed_trade_count"]) / 1000.0
        if score > best_score:
            best_score = score
            best_detail = combo_detail
            best_replayed_c = replayed_c_filtered
            best_rejected_c = rejected_c

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary = summary.sort_values(
            ["scenario", "equity_multiple", "executed_trade_count"],
            ascending=[True, False, False],
        ).reset_index(drop=True)
        combo_rank = summary["scenario"].astype(str).str.startswith("A_plus_B_plus_C")
        summary = pd.concat(
            [
                summary[combo_rank].sort_values(
                    ["equity_multiple", "executed_trade_count", "max_drawdown"],
                    ascending=[False, False, False],
                ),
                summary[~combo_rank].sort_values(
                    ["equity_multiple", "executed_trade_count", "max_drawdown"],
                    ascending=[False, False, False],
                ),
            ],
            ignore_index=True,
        )

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{len(dates)}d"
    ab_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_ab_detail.csv")
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    best_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_best_abc_detail.csv")
    best_c_path = output_prefix.with_name(output_prefix.name + suffix + "_best_c_replayed.csv")
    rejected_c_path = output_prefix.with_name(output_prefix.name + suffix + "_best_c_rejected.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    ab_detail.to_csv(ab_detail_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    best_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")
    best_replayed_c.to_csv(best_c_path, index=False, encoding="utf-8-sig")
    best_rejected_c.to_csv(rejected_c_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, ab_summary, summary, best_detail, best_rejected_c)

    print("A+B 后备用策略 C 搜索完成：")
    print(f"- ab_detail: {ab_detail_path}")
    print(f"- summary: {summary_path}")
    print(f"- best_abc_detail: {best_detail_path}")
    print(f"- best_c_replayed: {best_c_path}")
    print(f"- best_c_rejected: {rejected_c_path}")
    print(f"- markdown: {markdown_path}")
    print(ab_summary.to_string(index=False))
    print(summary.head(30).to_string(index=False) if not summary.empty else "没有找到满足约束的 C 方案。")


if __name__ == "__main__":
    main()
