"""
低频策略单条件放宽压力测试。

文件作用：
1. 读取当前策略配置和本地候选数据。
2. 在不修改正式配置文件的前提下，分别移除一个入选条件做历史观察回放。
3. 比较严格基准、放宽分段涨停数、放宽市场连板数、放宽封单比例后的交易频率、收益和回撤。
4. 输出每个方案的汇总和逐日明细。

本脚本只使用本地数据，不接实盘，不调用 QMT，不下真实订单。
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

from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config


RELAX_TARGETS = [
    ("strict_baseline", ""),
    ("relax_segment_limit_up_count", "segment_limit_up_count_bucket"),
    ("relax_market_chain_count", "market_chain_count_bucket"),
    ("relax_fd_ratio", "fd_ratio_bucket"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低频策略单条件放宽压力测试。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--end-date", default=None, help="截止日期，格式 YYYYMMDD。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/low_frequency/a_clean_exclude_star_prev0_3_bj_relaxation",
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


def resolve_recent_dates(candidates: pd.DataFrame, recent_days: int, end_date: str | None) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于压力测试的候选日期。")
    return dates[-recent_days:]


def scenario_config(base_config: dict[str, Any], relaxed_column: str) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    if not relaxed_column:
        return config
    conditions = config.get("candidate_filters", {}).get("conditions", [])
    config["candidate_filters"]["conditions"] = [
        condition for condition in conditions if str(condition.get("column", "")) != relaxed_column
    ]
    return config


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    daily_runner = PaperDailyFlowRunner(strategy_config_path)
    return daily_runner.load_audit_trades(daily_runner.audit_trades_path)


def find_audit_return(audit: pd.DataFrame, signal_date: str, ts_code: str) -> float | None:
    if audit.empty:
        return None
    matched = audit[
        (audit["trade_date"].astype(str) == str(signal_date))
        & (audit["ts_code"].astype(str) == str(ts_code))
    ].copy()
    if matched.empty:
        return None
    return to_float(matched.iloc[0].get("dynamic_account_return", 0.0))


def has_loss_overlay_watch(value: object) -> bool:
    return "LOSS_OVERLAY_WATCH" in str(value)


def build_generator(strategy_config_path: str | Path, config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(strategy_config_path)
    generator.config = config
    generator.paper_config = config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


def simulate_scenario(
    scenario_name: str,
    relaxed_column: str,
    base_config: dict[str, Any],
    strategy_config_path: str | Path,
    candidates: pd.DataFrame,
    audit: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    config = scenario_config(base_config, relaxed_column)
    generator = build_generator(strategy_config_path, config)
    filtered = generator.apply_strategy_filters(candidates)
    selected_action = config.get("paper_candidate", {}).get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    position_pct = float(config.get("position", {}).get("target_position_pct", 0.8))
    equity = float(config.get("position", {}).get("initial_cash", 500000))
    initial_equity = equity
    active_exit_date = ""
    active_label = ""
    rows = []

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_day_row(
                    scenario_name=scenario_name,
                    relaxed_column=relaxed_column,
                    signal_date=signal_date,
                    operation_status="POSITION_OCCUPIED_SKIP",
                    equity_before=equity,
                    equity_after=equity,
                    active_label=active_label,
                )
            )
            continue

        daily = filtered[filtered["trade_date"].map(normalize_date) == signal_date].copy()
        if daily.empty:
            rows.append(
                build_day_row(
                    scenario_name=scenario_name,
                    relaxed_column=relaxed_column,
                    signal_date=signal_date,
                    operation_status="NO_CANDIDATE",
                    equity_before=equity,
                    equity_after=equity,
                )
            )
            continue

        ranked = generator.rank_candidates(daily)
        output = generator.build_output(ranked, signal_date, top_n=generator.default_top_n)
        selected = output[output["planned_action"].astype(str) == selected_action].copy()
        if selected.empty:
            rows.append(
                build_day_row(
                    scenario_name=scenario_name,
                    relaxed_column=relaxed_column,
                    signal_date=signal_date,
                    operation_status="NO_SELECTED",
                    candidate_count=len(output),
                    equity_before=equity,
                    equity_after=equity,
                )
            )
            continue

        selected_row = selected.iloc[0]
        ts_code = str(selected_row.get("ts_code", ""))
        name = str(selected_row.get("name", ""))
        risk_flags = str(selected_row.get("risk_flags", ""))
        if has_loss_overlay_watch(risk_flags):
            rows.append(
                build_day_row(
                    scenario_name=scenario_name,
                    relaxed_column=relaxed_column,
                    signal_date=signal_date,
                    operation_status="REVIEW_REQUIRED_PLAN_ONLY",
                    candidate_count=len(output),
                    ts_code=ts_code,
                    name=name,
                    risk_flags=risk_flags,
                    equity_before=equity,
                    equity_after=equity,
                    return_source="manual_review_skip",
                )
            )
            continue

        audit_return = find_audit_return(audit, signal_date, ts_code)
        if audit_return is None:
            net_return = to_float(selected_row.get("historical_reference_net_return", 0.0))
            account_return = net_return * position_pct
            return_source = "candidate_net_return_x_position"
        else:
            account_return = audit_return
            return_source = "audit_dynamic_account_return"

        equity_before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(selected_row.get("historical_reference_exit_trade_date", ""))
        active_label = f"{ts_code} {name}"
        rows.append(
            build_day_row(
                scenario_name=scenario_name,
                relaxed_column=relaxed_column,
                signal_date=signal_date,
                operation_status="HISTORICAL_SIM_FILLED",
                candidate_count=len(output),
                ts_code=ts_code,
                name=name,
                risk_flags=risk_flags,
                account_return=account_return,
                equity_before=equity_before,
                equity_after=equity,
                return_source=return_source,
            )
        )

    detail = pd.DataFrame(rows)
    detail["initial_equity"] = initial_equity
    detail["peak_equity"] = detail["equity_after"].cummax().clip(lower=initial_equity)
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    return detail


def build_day_row(
    scenario_name: str,
    relaxed_column: str,
    signal_date: str,
    operation_status: str,
    equity_before: float,
    equity_after: float,
    candidate_count: int = 0,
    ts_code: str = "",
    name: str = "",
    risk_flags: str = "",
    account_return: float = 0.0,
    return_source: str = "",
    active_label: str = "",
) -> dict[str, Any]:
    return {
        "scenario": scenario_name,
        "relaxed_column": relaxed_column or "none",
        "signal_date": signal_date,
        "operation_status": operation_status,
        "candidate_count": int(candidate_count),
        "ts_code": ts_code,
        "name": name,
        "risk_flags": risk_flags,
        "account_return": float(account_return),
        "equity_before": float(equity_before),
        "equity_after": float(equity_after),
        "return_source": return_source,
        "active_position": active_label,
        "live_order_enabled": False,
    }


def summarize_scenario(detail: pd.DataFrame) -> dict[str, Any]:
    scenario = str(detail["scenario"].iloc[0]) if not detail.empty else ""
    relaxed_column = str(detail["relaxed_column"].iloc[0]) if not detail.empty else ""
    status_counts = detail["operation_status"].value_counts().to_dict() if not detail.empty else {}
    trades = detail[detail["operation_status"] == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    initial_equity = float(detail["initial_equity"].iloc[0]) if not detail.empty else 0.0
    final_equity = float(detail["equity_after"].iloc[-1]) if not detail.empty else 0.0
    return {
        "scenario": scenario,
        "relaxed_column": relaxed_column,
        "day_count": int(len(detail)),
        "executed_trade_count": int(len(trades)),
        "review_required_count": int(status_counts.get("REVIEW_REQUIRED_PLAN_ONLY", 0)),
        "no_candidate_count": int(status_counts.get("NO_CANDIDATE", 0)),
        "position_occupied_skip_count": int(status_counts.get("POSITION_OCCUPIED_SKIP", 0)),
        "candidate_day_count": int((detail["candidate_count"] > 0).sum()) if "candidate_count" in detail.columns else 0,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
        "max_drawdown": float(detail["drawdown"].min()) if not detail.empty else 0.0,
        "candidate_return_source_count": int((detail.get("return_source", pd.Series(dtype=str)) == "candidate_net_return_x_position").sum()),
        "audit_return_source_count": int((detail.get("return_source", pd.Series(dtype=str)) == "audit_dynamic_account_return").sum()),
        "live_order_enabled": False,
    }


def write_markdown(path: Path, summary: pd.DataFrame, detail: pd.DataFrame) -> None:
    detail_columns = [
        "scenario",
        "signal_date",
        "operation_status",
        "ts_code",
        "name",
        "account_return",
        "equity_after",
        "drawdown",
        "return_source",
        "risk_flags",
    ]
    detail_columns = [column for column in detail_columns if column in detail.columns]
    content = f"""# 低频策略单条件放宽压力测试

本报告只使用本地数据做历史观察回放，不接实盘，不调用 QMT，不下真实订单。

## 方案汇总

{summary.to_markdown(index=False)}

## 交易明细

{detail[detail_columns].to_markdown(index=False) if not detail.empty else "无交易明细。"}

## 解释限制

- 放宽条件后新增标的若不在审计逐笔交易中，收益使用候选行 `net_return * 仓位` 近似，必须后续再做严格成交审计。
- 人工复核命中的交易不计收益。
- 这一步只用于判断放宽方向，不代表可以实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    base_generator = PaperCandidateGenerator(args.strategy_config)
    candidates = base_generator.load_all_candidates()
    dates = resolve_recent_dates(candidates, args.recent_days, args.end_date)
    audit = load_audit(args.strategy_config)

    detail_frames = []
    summaries = []
    for scenario_name, relaxed_column in RELAX_TARGETS:
        detail = simulate_scenario(
            scenario_name=scenario_name,
            relaxed_column=relaxed_column,
            base_config=base_config,
            strategy_config_path=args.strategy_config,
            candidates=candidates,
            audit=audit,
            dates=dates,
        )
        detail_frames.append(detail)
        summaries.append(summarize_scenario(detail))

    summary = pd.DataFrame(summaries).sort_values("equity_multiple", ascending=False).reset_index(drop=True)
    detail_all = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{args.recent_days}d"
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + suffix + "_detail.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail_all.to_csv(detail_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, detail_all[detail_all["operation_status"] != "NO_CANDIDATE"].copy())

    print("低频策略单条件放宽压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
