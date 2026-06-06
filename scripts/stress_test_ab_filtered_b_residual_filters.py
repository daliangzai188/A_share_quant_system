"""
A+B filtered 中 B 剩余风险过滤压力测试。

文件作用：
1. 复用当前 A 严格策略和 B0018 filtered 观察口径。
2. 只对 B 风险过滤后仍保留的交易，测试额外的事前可见过滤条件。
3. 每个过滤条件都重新计算 A+B 组合资金曲线和持仓占用，避免简单事后删除导致失真。
4. 输出各过滤方案的收益、回撤、交易笔数、过滤清单和最优候选明细。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_backup_strategy_b import (
    replay_selected_b,
    selected_b_signals,
    simulate_a_plus_b_strict,
    simulate_b_strict,
    summarize,
)
from scripts.run_paper_ab_filtered_observation_window import (
    configured_b_conditions,
    condition_text,
    reject_b_risk_mask,
    resolve_window_dates,
)
from scripts.search_paper_backup_strategy_b import (
    backup_config,
    build_generator,
    normalize_date,
    scoped_no_candidate_dates,
    simulate_single_strategy,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config


Predicate = Callable[[pd.DataFrame], pd.Series]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试 A+B filtered 中 B 剩余风险过滤条件。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--start-date", default=None, help="观察窗口开始日期，格式YYYYMMDD。")
    parser.add_argument("--end-date", default="20260518", help="观察窗口截止日期，格式YYYYMMDD。")
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--limit", type=int, default=None, help="观察交易日数量。设置后覆盖--recent-days。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/ab_filtered/a_strict_plus_b0018_filtered_residual_filter_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    runner = PaperDailyFlowRunner(strategy_config_path)
    return runner.load_audit_trades(runner.audit_trades_path)


def col(data: pd.DataFrame, name: str) -> pd.Series:
    return data.get(name, pd.Series("", index=data.index)).fillna("").astype(str)


def num_col(data: pd.DataFrame, name: str) -> pd.Series:
    return pd.to_numeric(data.get(name, pd.Series(0.0, index=data.index)), errors="coerce").fillna(0.0)


def filter_definitions() -> list[dict[str, Any]]:
    return [
        {
            "scenario": "baseline_ab_filtered",
            "description": "当前 A+B filtered，不增加额外B过滤。",
            "predicate": lambda data: pd.Series(False, index=data.index),
        },
        {
            "scenario": "reject_b_market_segment_sh_main",
            "description": "B额外过滤沪市主板。",
            "predicate": lambda data: col(data, "market_segment").eq("sh_main"),
        },
        {
            "scenario": "reject_b_market_chain_gte_30",
            "description": "B额外过滤全市场连板数量gte_30。",
            "predicate": lambda data: col(data, "market_chain_count_bucket").eq("gte_30"),
        },
        {
            "scenario": "reject_b_open_times_gte_4",
            "description": "B额外过滤炸板次数>=4。",
            "predicate": lambda data: num_col(data, "open_times") >= 4,
        },
        {
            "scenario": "reject_b_open_times_gte_6",
            "description": "B额外过滤炸板次数>=6。",
            "predicate": lambda data: num_col(data, "open_times") >= 6,
        },
        {
            "scenario": "reject_b_turnover_rate_gte_20",
            "description": "B额外过滤换手率>=20%。",
            "predicate": lambda data: num_col(data, "turnover_rate") >= 20,
        },
        {
            "scenario": "reject_b_turnover_bucket_15_25",
            "description": "B额外过滤换手率分桶15_25。",
            "predicate": lambda data: col(data, "turnover_rate_bucket").eq("15_25"),
        },
        {
            "scenario": "reject_b_amount_lt_800k",
            "description": "B额外过滤成交额字段amount<80万口径单位。",
            "predicate": lambda data: num_col(data, "amount") < 800000,
        },
        {
            "scenario": "reject_b_retreat_warming_2day",
            "description": "B额外过滤retreat_state_bucket=warming_2day。",
            "predicate": lambda data: col(data, "retreat_state_bucket").eq("warming_2day"),
        },
        {
            "scenario": "reject_b_market_emotion_warming",
            "description": "B额外过滤market_emotion_state_bucket=warming。",
            "predicate": lambda data: col(data, "market_emotion_state_bucket").eq("warming"),
        },
        {
            "scenario": "reject_b_sh_main_or_open_times_gte_4",
            "description": "B额外过滤沪市主板或炸板次数>=4。",
            "predicate": lambda data: col(data, "market_segment").eq("sh_main") | (num_col(data, "open_times") >= 4),
        },
        {
            "scenario": "reject_b_sh_main_or_chain_gte_30",
            "description": "B额外过滤沪市主板或全市场连板数量gte_30。",
            "predicate": lambda data: col(data, "market_segment").eq("sh_main")
            | col(data, "market_chain_count_bucket").eq("gte_30"),
        },
        {
            "scenario": "reject_b_open_times_gte_4_or_turnover_gte_20",
            "description": "B额外过滤炸板次数>=4或换手率>=20%。",
            "predicate": lambda data: (num_col(data, "open_times") >= 4) | (num_col(data, "turnover_rate") >= 20),
        },
    ]


def rejected_key_set(data: pd.DataFrame, predicate: Predicate) -> set[tuple[str, str]]:
    if data.empty:
        return set()
    mask = predicate(data).fillna(False).astype(bool)
    rejected = data[mask].copy()
    return {(normalize_date(row.trade_date), str(row.ts_code)) for row in rejected.itertuples(index=False)}


def apply_rejected_keys(data: pd.DataFrame, rejected_keys: set[tuple[str, str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if data.empty:
        return data.copy(), data.copy()
    keys = pd.Series(
        [(normalize_date(row.trade_date), str(row.ts_code)) for row in data.itertuples(index=False)],
        index=data.index,
    )
    mask = keys.isin(rejected_keys)
    return data[~mask].copy(), data[mask].copy()


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    base_config = load_json_config(args.strategy_config)
    ab_config = base_config.get("paper_ab_filtered_strategy", {})
    if bool(ab_config.get("allow_live_order", False)) or bool(ab_config.get("live_order_enabled", False)):
        raise RuntimeError("拒绝运行B剩余风险测试：配置中存在实盘开关。")

    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_window_dates(all_candidates, args.start_date, args.end_date, args.recent_days, args.limit)
    audit = load_audit(args.strategy_config)
    initial_equity = float(base_config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(base_config.get("position", {}).get("target_position_pct", 0.8))

    a_config = copy.deepcopy(base_config)
    a_generator = build_generator(args.strategy_config, a_config)
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
    no_candidate_dates = scoped_no_candidate_dates(a_detail)

    b_conditions = configured_b_conditions(base_config)
    b_config = backup_config(base_config, b_conditions)
    b_generator = build_generator(args.strategy_config, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, args.runtime_config)
    base_risk_mask = reject_b_risk_mask(replayed_b, base_config)
    replayed_b_after_base_filter = replayed_b[~base_risk_mask].copy()
    base_rejected_b = replayed_b[base_risk_mask].copy()

    return {
        "base_config": base_config,
        "dates": dates,
        "audit": audit,
        "initial_equity": initial_equity,
        "a_detail": a_detail,
        "no_candidate_dates": no_candidate_dates,
        "b_conditions": b_conditions,
        "selected_b": selected_b,
        "replayed_b_before_base_filter": replayed_b,
        "base_rejected_b": base_rejected_b,
        "replayed_b_after_base_filter": replayed_b_after_base_filter,
    }


def summarize_rejected(rejected: pd.DataFrame) -> str:
    if rejected.empty:
        return ""
    values = []
    for row in rejected.itertuples(index=False):
        ret = float(pd.to_numeric(getattr(row, "strict_account_return", 0.0), errors="coerce") or 0.0)
        values.append(f"{normalize_date(getattr(row, 'trade_date', ''))}:{getattr(row, 'ts_code', '')}:{ret:.4f}")
    return ";".join(values)


def run_scenarios(context: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows = []
    detail_frames = []
    rejected_frames = []
    replayed_b = context["replayed_b_after_base_filter"]
    condition = condition_text(context["b_conditions"])

    for definition in filter_definitions():
        rejected_keys = rejected_key_set(replayed_b, definition["predicate"])
        filtered_b, rejected_b = apply_rejected_keys(replayed_b, rejected_keys)
        b_detail = simulate_b_strict(filtered_b, context["no_candidate_dates"], context["initial_equity"])
        combo_detail = simulate_a_plus_b_strict(
            context["a_detail"],
            filtered_b,
            context["audit"],
            context["dates"],
            context["initial_equity"],
        )
        combo_summary = summarize(combo_detail, definition["scenario"], condition)
        b_summary = summarize(b_detail, definition["scenario"] + "_b_only", condition)
        combo_summary.update(
            {
                "description": definition["description"],
                "base_b_selected_count": int(len(context["selected_b"])),
                "base_b_rejected_count": int(len(context["base_rejected_b"])),
                "residual_b_before_filter_count": int(len(replayed_b)),
                "residual_b_rejected_count": int(len(rejected_b)),
                "residual_b_after_filter_count": int(len(filtered_b)),
                "b_only_equity_multiple": float(b_summary["equity_multiple"]),
                "rejected_trade_keys": summarize_rejected(rejected_b),
            }
        )
        summary_rows.append(combo_summary)
        combo_detail["residual_filter_scenario"] = definition["scenario"]
        combo_detail["residual_filter_description"] = definition["description"]
        detail_frames.append(combo_detail)
        rejected_b = rejected_b.copy()
        rejected_b["residual_filter_scenario"] = definition["scenario"]
        rejected_b["residual_filter_description"] = definition["description"]
        rejected_frames.append(rejected_b)

    summary = pd.DataFrame(summary_rows).sort_values(
        ["max_drawdown", "equity_multiple", "executed_trade_count"],
        ascending=[False, False, False],
    )
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    rejected = pd.concat(rejected_frames, ignore_index=True) if rejected_frames else pd.DataFrame()
    return summary, detail, rejected


def write_markdown(path: Path, summary: pd.DataFrame, best_detail: pd.DataFrame, rejected: pd.DataFrame) -> None:
    summary_columns = [
        "scenario",
        "description",
        "executed_trade_count",
        "b_trade_count",
        "a_trade_count",
        "win_rate",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "residual_b_rejected_count",
        "residual_b_after_filter_count",
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
        "risk_flags",
    ]
    detail_columns = [column for column in detail_columns if column in best_detail.columns]
    rejected_columns = [
        "residual_filter_scenario",
        "trade_date",
        "ts_code",
        "name",
        "strict_account_return",
        "market_segment",
        "market_chain_count_bucket",
        "open_times",
        "turnover_rate",
    ]
    rejected_columns = [column for column in rejected_columns if column in rejected.columns]
    content = f"""# A+B filtered 中 B 剩余风险过滤压力测试

本报告只使用事前可见字段测试 B 备用策略的额外过滤条件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary[summary_columns].to_markdown(index=False) if not summary.empty else "无汇总。"}

## 最优回撤方案逐日明细

{best_detail[detail_columns].to_markdown(index=False) if not best_detail.empty else "无逐日明细。"}

## 被额外过滤的B交易

{rejected[rejected_columns].to_markdown(index=False) if not rejected.empty else "无额外过滤交易。"}

## 口径限制

- 所有过滤字段必须是 T 日收盘后已知字段。
- 该测试仍是日线保守成交模型，不是盘口五档真实撮合。
- 如果某个方案收益改善但样本数过少，不能直接升级为正式策略。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    context = build_context(args)
    summary, detail, rejected = run_scenarios(context)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    dates = context["dates"]
    window_size = int(args.limit or args.recent_days)
    suffix = f"_{dates[0]}_{dates[-1]}_{window_size}d"
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + suffix + "_detail.csv")
    rejected_path = output_prefix.with_name(output_prefix.name + suffix + "_rejected.csv")
    best_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_best_detail.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    best_scenario = str(summary.iloc[0]["scenario"]) if not summary.empty else ""
    best_detail = detail[detail["residual_filter_scenario"].astype(str).eq(best_scenario)].copy() if not detail.empty else pd.DataFrame()

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    rejected.to_csv(rejected_path, index=False, encoding="utf-8-sig")
    best_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, best_detail, rejected)

    print("A+B filtered 的 B 剩余风险过滤压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- rejected: {rejected_path}")
    print(f"- best_detail: {best_detail_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
