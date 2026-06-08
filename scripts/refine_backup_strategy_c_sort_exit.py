"""
精修 A+B 之后的备用策略 C：单独搜索 C 的排序规则和卖出规则。

文件作用：
1. 固定当前 A 严格策略和 B0018 过滤版，不改变既有 A/B 逻辑。
2. 只在 A/B 没有真实成交的交易日里尝试 C 补位。
3. 读取上一轮 C 搜索报告中的候选条件，对每个 C 条件枚举排序规则和卖出规则。
4. 使用日线保守成交回放，处理 T+1、涨停开盘买不到、跌停日卖不出、滑点和费用。
5. 输出 C 单独结果、A+B+C 组合结果和最佳方案逐日明细。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_backup_strategy_b import (
    attach_drawdown,
    build_audit_row,
    replay_selected_b,
    selected_b_signals,
    simulate_a_plus_b_strict,
    summarize,
)
from scripts.optimize_recent_2y_full_strategy import Recent2YFullStrategyOptimizer
from scripts.run_paper_ab_filtered_observation_window import (
    configured_b_conditions,
    reject_b_risk_mask,
)
from scripts.search_paper_backup_strategy_b import (
    backup_config,
    build_generator,
    normalize_date,
    scoped_no_candidate_dates,
    simulate_single_strategy,
)
from scripts.search_paper_backup_strategy_c import (
    AB_NO_FILL_STATUSES,
    c_summary,
    condition_key,
    reject_c_risk_mask,
    resolve_dates,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config


DEFAULT_SUMMARY_PATH = (
    "reports/paper_trade/backup_strategy_c/"
    "a_strict_plus_b0018_filtered_backup_c_2y_deep_nofill_target4_"
    "20240520_20260514_481d_summary.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="精修 A+B 后备用策略 C 的排序和卖出规则。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--source-summary", default=DEFAULT_SUMMARY_PATH, help="上一轮 C 搜索 summary 文件。")
    parser.add_argument(
        "--condition",
        action="append",
        default=[],
        help="直接指定 C 条件，格式 column=value;column2=value2。传入后优先使用该条件，可重复传入。",
    )
    parser.add_argument("--start-date", default="20240520", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", default="20260518", help="截止日期，格式 YYYYMMDD。")
    parser.add_argument("--recent-days", type=int, default=481, help="最近交易日数量。")
    parser.add_argument("--top-condition-count", type=int, default=12, help="从上一轮报告中取前 N 个 C 条件。")
    parser.add_argument("--limit-sort-rules", type=int, default=20, help="最多测试的排序规则数量。")
    parser.add_argument("--limit-exit-rules", type=int, default=9, help="最多测试的卖出规则数量。")
    parser.add_argument("--min-c-trades", type=int, default=3, help="C 单独至少成交笔数。")
    parser.add_argument("--max-combo-drawdown", type=float, default=0.4, help="A+B+C 组合最大回撤绝对值上限。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/backup_strategy_c/a_strict_plus_b0018_filtered_backup_c_sort_exit_refine",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    runner = PaperDailyFlowRunner(strategy_config_path)
    return runner.load_audit_trades(runner.audit_trades_path)


def parse_condition_text(text: str) -> list[dict[str, str]]:
    conditions: list[dict[str, str]] = []
    for part in str(text).split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"条件格式错误，应为 column=value: {part}")
        column, value = part.split("=", 1)
        conditions.append({"column": column.strip(), "value": value.strip()})
    if not conditions:
        raise ValueError("C 条件为空，无法精修。")
    return conditions


def load_top_conditions(
    summary_path: Path,
    top_condition_count: int,
    direct_conditions: list[str] | None = None,
) -> list[list[dict[str, str]]]:
    if direct_conditions:
        unique_direct: dict[str, list[dict[str, str]]] = {}
        for condition_text in direct_conditions:
            conditions = parse_condition_text(condition_text)
            unique_direct.setdefault(condition_key(conditions), conditions)
        return list(unique_direct.values())

    summary = pd.read_csv(summary_path, dtype=str)
    for column in ["equity_multiple", "executed_trade_count", "c_trade_count", "max_drawdown"]:
        if column in summary.columns:
            summary[column] = pd.to_numeric(summary[column], errors="coerce").fillna(0.0)

    combo = summary[summary["scenario"].astype(str).str.startswith("A_plus_B_plus_C")].copy()
    c_only = summary[summary["scenario"].astype(str).str.startswith("C_")].copy()
    combo = combo.sort_values(["equity_multiple", "c_trade_count"], ascending=[False, False])
    c_only = c_only.sort_values(["equity_multiple", "executed_trade_count"], ascending=[False, False])

    ordered_conditions = pd.concat(
        [
            combo["condition"].head(top_condition_count),
            c_only["condition"].head(top_condition_count),
        ],
        ignore_index=True,
    )
    unique: dict[str, list[dict[str, str]]] = {}
    for condition_text in ordered_conditions.dropna().astype(str):
        conditions = parse_condition_text(condition_text)
        unique.setdefault(condition_key(conditions), conditions)
        if len(unique) >= top_condition_count:
            break
    return list(unique.values())


def build_sort_rules(base_config: dict[str, Any], runtime_config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    full_config = runtime_config.get("recent_2y_full_strategy_optimization", {})
    current_ranking = copy.deepcopy(base_config.get("ranking", {}))
    if current_ranking:
        current_ranking.setdefault("name", current_ranking.get("sort_rule", "current_strategy_ranking"))
    rules = [current_ranking] if current_ranking else []
    rules.extend(full_config.get("sort_rules", []))
    rules.extend(Recent2YFullStrategyOptimizer.build_profit_source_sort_rules())
    extra_rules = [
        {"name": "fill_probability_desc", "columns": ["fill_probability"], "ascending": [False]},
        {"name": "amount_desc", "columns": ["amount"], "ascending": [False]},
        {"name": "turnover_desc", "columns": ["turnover_rate"], "ascending": [False]},
        {"name": "turnover_asc", "columns": ["turnover_rate"], "ascending": [True]},
        {"name": "volume_ratio_desc", "columns": ["volume_ratio"], "ascending": [False]},
        {"name": "volume_ratio_asc", "columns": ["volume_ratio"], "ascending": [True]},
        {"name": "circ_mv_desc", "columns": ["circ_mv"], "ascending": [False]},
        {"name": "circ_mv_asc", "columns": ["circ_mv"], "ascending": [True]},
        {"name": "market_leader_rank_desc", "columns": ["market_leader_rank"], "ascending": [False]},
        {"name": "limit_height_rank_desc", "columns": ["limit_height_rank"], "ascending": [False]},
        {"name": "fd_ratio_asc", "columns": ["fd_amount_to_circ_mv"], "ascending": [True]},
    ]

    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], tuple[bool, ...]]] = set()
    for rule in rules + extra_rules:
        key = (tuple(rule.get("columns", [])), tuple(bool(value) for value in rule.get("ascending", [])))
        if key in seen:
            continue
        seen.add(key)
        result.append(rule)
        if len(result) >= limit:
            break
    return result


def build_exit_rules(runtime_config: dict[str, Any], limit: int) -> list[ReplayRule]:
    full_config = runtime_config.get("recent_2y_full_strategy_optimization", {})
    rules = []
    for item in full_config.get("exit_rules", []):
        rules.append(
            ReplayRule(
                rule_name=str(item["rule_name"]),
                max_hold_days=int(item["max_hold_days"]),
                exit_price_field=str(item.get("exit_price_field", "close")),
                stop_loss=float(item["stop_loss"]) if "stop_loss" in item else None,
                take_profit=float(item["take_profit"]) if "take_profit" in item else None,
            )
        )
        if len(rules) >= limit:
            break
    return rules


def select_by_sort_rule(candidates: pd.DataFrame, sort_rule: dict[str, Any]) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    ranked = Recent2YFullStrategyOptimizer.attach_profit_source_score(candidates, sort_rule)
    columns = [column for column in sort_rule.get("columns", []) if column in ranked.columns]
    if not columns:
        columns = ["fill_probability"] if "fill_probability" in ranked.columns else ["amount"]
    ascending = list(sort_rule.get("ascending", []))[: len(columns)]
    if len(ascending) != len(columns):
        ascending = [False] * len(columns)
    selected = ranked.sort_values(
        ["trade_date"] + columns + ["amount", "turnover_rate", "ts_code"],
        ascending=[True] + ascending + [False, False, True],
        na_position="last",
    )
    return selected.groupby("trade_date", as_index=False).head(1).sort_values(["trade_date", "ts_code"])


def replay_selected_with_rule(
    selected: pd.DataFrame,
    replay_engine: ConservativeTradeReplay,
    forward: pd.DataFrame,
    rule: ReplayRule,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    samples = selected.merge(forward, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replayed = replay_engine.replay_rule(samples, rule)
    replayed["strict_account_return"] = pd.to_numeric(replayed["daily_return"], errors="coerce").fillna(0.0)
    replayed["strict_return_source"] = f"c_conservative_daily_replay_{rule.rule_name}"
    return replayed.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def replay_by_date(replayed: pd.DataFrame) -> dict[str, pd.Series]:
    result: dict[str, pd.Series] = {}
    for row in replayed.itertuples(index=False):
        trade_date = normalize_date(getattr(row, "trade_date", ""))
        if trade_date and trade_date not in result:
            result[trade_date] = pd.Series(row._asdict())
    return result


def simulate_c_strict_fast(
    replayed_c: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
    scenario: str,
) -> pd.DataFrame:
    c_by_date = replay_by_date(replayed_c)
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows: list[dict[str, Any]] = []
    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_audit_row(
                    scenario,
                    signal_date,
                    "C",
                    "POSITION_OCCUPIED_SKIP",
                    equity,
                    equity,
                    active_label=active_label,
                )
            )
            continue

        c_row = c_by_date.get(signal_date)
        if c_row is None:
            rows.append(build_audit_row(scenario, signal_date, "C", "NO_CANDIDATE", equity, equity))
            continue
        if not bool(c_row.get("buy_executed", False)):
            rows.append(
                build_audit_row(
                    scenario,
                    signal_date,
                    "C",
                    "BUY_REJECTED",
                    equity,
                    equity,
                    row=c_row,
                    return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
                )
            )
            continue
        if not bool(c_row.get("sell_executed", False)):
            rows.append(
                build_audit_row(
                    scenario,
                    signal_date,
                    "C",
                    "SELL_UNRESOLVED",
                    equity,
                    equity,
                    row=c_row,
                    return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
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
                scenario,
                signal_date,
                "C",
                "HISTORICAL_SIM_FILLED",
                before,
                equity,
                row=c_row,
                account_return=account_return,
                return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
            )
        )
    return attach_drawdown(pd.DataFrame(rows), initial_equity)


def simulate_a_plus_b_plus_c_fast(
    ab_detail: pd.DataFrame,
    replayed_c: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
) -> pd.DataFrame:
    c_by_date = replay_by_date(replayed_c)
    ab_by_date = {
        normalize_date(row.signal_date): pd.Series(row._asdict())
        for row in ab_detail.itertuples(index=False)
    }
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows: list[dict[str, Any]] = []

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_refined",
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
                    "A_plus_B_plus_C_refined",
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
                    "A_plus_B_plus_C_refined",
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

        c_row = c_by_date.get(signal_date)
        if c_row is None:
            rows.append(build_audit_row("A_plus_B_plus_C_refined", signal_date, "NONE", "NO_CANDIDATE", equity, equity))
            continue
        if not bool(c_row.get("buy_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_refined",
                    signal_date,
                    "C",
                    "BUY_REJECTED",
                    equity,
                    equity,
                    row=c_row,
                    return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
                )
            )
            continue
        if not bool(c_row.get("sell_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_plus_C_refined",
                    signal_date,
                    "C",
                    "SELL_UNRESOLVED",
                    equity,
                    equity,
                    row=c_row,
                    return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
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
                "A_plus_B_plus_C_refined",
                signal_date,
                "C",
                "HISTORICAL_SIM_FILLED",
                before,
                equity,
                row=c_row,
                account_return=account_return,
                return_source=str(c_row.get("strict_return_source", "c_conservative_daily_replay")),
            )
        )
    return attach_drawdown(pd.DataFrame(rows), initial_equity)


def prepare_ab_detail(
    strategy_config_path: str | Path,
    runtime_config_path: str | Path,
    base_config: dict[str, Any],
    all_candidates: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    audit = load_audit(strategy_config_path)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(base_config.get("position", {}).get("target_position_pct", 0.8))

    a_generator = build_generator(strategy_config_path, copy.deepcopy(base_config))
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

    b_conditions = configured_b_conditions(base_config)
    b_config = backup_config(base_config, b_conditions)
    b_generator = build_generator(strategy_config_path, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, a_no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, runtime_config_path)
    b_rejected_mask = reject_b_risk_mask(replayed_b, base_config)
    replayed_b_filtered = replayed_b[~b_rejected_mask].copy()
    return simulate_a_plus_b_strict(a_detail, replayed_b_filtered, audit, dates, initial_equity)


def summarize_best_rows(summary: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "scenario",
        "condition",
        "sort_rule",
        "exit_rule",
        "executed_trade_count",
        "a_trade_count",
        "b_trade_count",
        "c_trade_count",
        "win_rate",
        "avg_account_return",
        "median_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_profit",
        "max_loss",
        "buy_rejected_count",
        "sell_unresolved_count",
        "limit_down_blocked_trade_count",
    ]
    return summary[[column for column in columns if column in summary.columns]].head(30)


def write_markdown(path: Path, ab_summary: pd.DataFrame, summary: pd.DataFrame, best_detail: pd.DataFrame) -> None:
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
        "buy_reject_reason",
        "sell_reject_reason",
        "limit_down_blocked_days",
    ]
    detail_columns = [column for column in detail_columns if column in best_detail.columns]
    content = f"""# 备用策略 C 排序与卖出规则精修

本报告只使用本地日线数据和保守成交模型，不接实盘，不调用 QMT，不下真实订单。

## A+B 基准

{ab_summary.to_markdown(index=False)}

## Top 精修方案

{summarize_best_rows(summary).to_markdown(index=False) if not summary.empty else "没有找到满足约束的 C 精修方案。"}

## 最优 A+B+C 逐日明细

{best_detail[detail_columns].to_markdown(index=False) if not best_detail.empty else "无逐日明细。"}

## 口径限制

- C 只在 A/B 没有真实成交时启用，包括无候选、买入被拒、只需人工复核等状态。
- C 精修只改变 C 自己的排序规则和卖出规则，不改变 A/B。
- 本报告仍是日线保守成交口径，不等于盘口五档真实撮合。
- 涨停开盘默认买不到，跌停日默认无法卖出，滑点和交易费用已按现有配置扣除。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    runtime_config = load_json_config(args.runtime_config)
    if bool(base_config.get("live_trading_enabled", False)) or bool(base_config.get("qmt_enabled", False)):
        raise RuntimeError("拒绝运行 C 精修：配置存在实盘或 QMT 开关。")

    source_summary_path = resolve_path(args.source_summary)
    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    print("阶段1/6：加载候选条件和本地候选池...", flush=True)
    conditions_list = load_top_conditions(source_summary_path, args.top_condition_count, args.condition)
    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_dates(all_candidates, args.start_date, args.end_date, args.recent_days)
    print(f"阶段1/6完成：C条件={len(conditions_list)}, 窗口交易日={len(dates)}", flush=True)

    print("阶段2/6：重建 A+B 严格基准...", flush=True)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    ab_detail = prepare_ab_detail(args.strategy_config, args.runtime_config, base_config, all_candidates, dates)
    ab_summary = pd.DataFrame([summarize(ab_detail, "A_plus_B0018_filtered", "current_a_b")])
    c_dates = (
        ab_detail.loc[ab_detail["operation_status"].astype(str).isin(AB_NO_FILL_STATUSES), "signal_date"]
        .map(normalize_date)
        .tolist()
    )
    print(f"阶段2/6完成：A/B未成交可补C日={len(c_dates)}", flush=True)

    print("阶段3/6：准备 C 排序规则和卖出规则...", flush=True)
    sort_rules = build_sort_rules(base_config, runtime_config, args.limit_sort_rules)
    exit_rules = build_exit_rules(runtime_config, args.limit_exit_rules)
    replay_engine = ConservativeTradeReplay(config_path=args.runtime_config)
    forward = replay_engine.load_forward_prices()
    print(f"阶段3/6完成：排序规则={len(sort_rules)}, 卖出规则={len(exit_rules)}", flush=True)

    print("阶段4/6：缓存每个 C 条件的候选池...", flush=True)
    condition_candidates: list[tuple[list[dict[str, str]], pd.DataFrame]] = []
    for conditions in conditions_list:
        c_config = backup_config(base_config, conditions)
        c_generator = build_generator(args.strategy_config, c_config)
        filtered = c_generator.apply_strategy_filters(all_candidates)
        filtered = filtered[filtered["trade_date"].map(normalize_date).isin(c_dates)].copy()
        if not filtered.empty:
            condition_candidates.append((conditions, filtered))
    print(f"阶段4/6完成：有效C条件={len(condition_candidates)}", flush=True)

    print("阶段5/6：枚举 C 排序 x 卖出规则...", flush=True)
    summary_rows: list[dict[str, Any]] = []
    best_detail = pd.DataFrame()
    best_c_replayed = pd.DataFrame()
    best_score = -1.0
    total = len(condition_candidates) * len(sort_rules) * len(exit_rules)
    done = 0
    for conditions, filtered in condition_candidates:
        condition_text = condition_key(conditions)
        for sort_rule in sort_rules:
            selected = select_by_sort_rule(filtered, sort_rule)
            if selected.empty:
                done += len(exit_rules)
                continue
            for exit_rule in exit_rules:
                done += 1
                if done % 100 == 0:
                    print(f"C 精修进度: {done}/{total}", flush=True)
                replayed_c = replay_selected_with_rule(selected, replay_engine, forward, exit_rule)
                if replayed_c.empty:
                    continue
                rejected_mask = reject_c_risk_mask(replayed_c, base_config)
                replayed_c_filtered = replayed_c[~rejected_mask].copy()
                c_detail = simulate_c_strict_fast(
                    replayed_c_filtered,
                    c_dates,
                    initial_equity,
                    "C_refined_strict",
                )
                c_row = c_summary(c_detail, "C_refined_strict", condition_text)
                if int(c_row["executed_trade_count"]) < args.min_c_trades:
                    continue

                combo_detail = simulate_a_plus_b_plus_c_fast(ab_detail, replayed_c_filtered, dates, initial_equity)
                combo_row = c_summary(combo_detail, "A_plus_B_plus_C_refined", condition_text)
                if abs(float(combo_row["max_drawdown"])) > args.max_combo_drawdown:
                    continue

                for row in (c_row, combo_row):
                    row["sort_rule"] = str(sort_rule.get("name", "custom"))
                    row["exit_rule"] = exit_rule.rule_name
                    row["source_condition_count"] = len(conditions)
                summary_rows.append(c_row)
                summary_rows.append(combo_row)

                score = float(combo_row["equity_multiple"]) + int(combo_row["c_trade_count"]) / 1000.0
                if score > best_score:
                    best_score = score
                    best_detail = combo_detail
                    best_c_replayed = replayed_c_filtered

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        combo_mask = summary["scenario"].astype(str).eq("A_plus_B_plus_C_refined")
        summary = pd.concat(
            [
                summary[combo_mask].sort_values(
                    ["equity_multiple", "executed_trade_count", "max_drawdown"],
                    ascending=[False, False, False],
                ),
                summary[~combo_mask].sort_values(
                    ["equity_multiple", "executed_trade_count", "max_drawdown"],
                    ascending=[False, False, False],
                ),
            ],
            ignore_index=True,
        )

    print("阶段6/6：写入报告...", flush=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{len(dates)}d"
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    best_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_best_abc_detail.csv")
    best_c_path = output_prefix.with_name(output_prefix.name + suffix + "_best_c_replayed.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    best_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")
    best_c_replayed.to_csv(best_c_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, ab_summary, summary, best_detail)

    print("备用策略 C 排序与卖出规则精修完成：")
    print(f"- summary: {summary_path}")
    print(f"- best_abc_detail: {best_detail_path}")
    print(f"- best_c_replayed: {best_c_path}")
    print(f"- markdown: {markdown_path}")
    print(ab_summary.to_string(index=False))
    print(summarize_best_rows(summary).to_string(index=False) if not summary.empty else "没有找到满足约束的 C 精修方案。")


if __name__ == "__main__":
    main()
