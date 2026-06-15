"""
审计备用策略 B 的日线保守成交结果。

文件作用：
1. 固定审计 B_0018：segment_emotion_state_bucket=warming。
2. 只在 A 策略无候选日里生成 B 候选，避免和 A 抢同一天信号。
3. 用 ConservativeTradeReplay 对 B 候选重新做 T+1 买入、T+2 卖出、涨停买不到、跌停卖不出、滑点和费用审计。
4. 输出 B 单独严格回放、A+B 组合严格回放、汇总和风险缺口。

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

from scripts.search_paper_backup_strategy_b import (
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计备用策略 B 的日线保守成交结果。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--recent-days", type=int, default=120, help="最近交易日数量。")
    parser.add_argument("--end-date", default="20260518", help="截止日期，格式 YYYYMMDD。")
    parser.add_argument(
        "--b-condition",
        default="segment_emotion_state_bucket=warming",
        help="备用策略 B 条件，格式 column=value。默认审计 B_0018。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/backup_strategy_b/a_clean_exclude_star_prev0_3_bj_b0018_audit",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def parse_condition(text: str) -> dict[str, str]:
    if "=" not in text:
        raise ValueError(f"条件格式错误，应为 column=value: {text}")
    column, value = text.split("=", 1)
    return {"column": column.strip(), "value": value.strip()}


def resolve_recent_dates(candidates: pd.DataFrame, recent_days: int, end_date: str | None) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有可用于 B 审计的候选日期。")
    return dates[-recent_days:]


def load_audit(strategy_config_path: str | Path) -> pd.DataFrame:
    runner = PaperDailyFlowRunner(strategy_config_path)
    return runner.load_audit_trades(runner.audit_trades_path)


def find_a_audit_row(audit: pd.DataFrame, signal_date: str, ts_code: str) -> pd.Series | None:
    matched = audit[
        (audit["trade_date"].astype(str) == str(signal_date))
        & (audit["ts_code"].astype(str) == str(ts_code))
    ].copy()
    if matched.empty:
        return None
    return matched.iloc[0]


def replay_selected_b(
    selected: pd.DataFrame,
    runtime_config: str | Path,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    replay_engine = ConservativeTradeReplay(config_path=runtime_config)
    forward = replay_engine.load_forward_prices()
    samples = selected.merge(forward, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
    replayed = replay_engine.replay_rule(samples, replay_rule)
    replayed["strict_account_return"] = pd.to_numeric(replayed["daily_return"], errors="coerce").fillna(0.0)
    replayed["strict_return_source"] = "conservative_daily_replay"
    return replayed.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def selected_b_signals(
    b_generator: PaperCandidateGenerator,
    b_filtered: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    selected_action = b_generator.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    rows = []
    for signal_date in dates:
        daily = b_filtered[b_filtered["trade_date"].map(normalize_date) == signal_date].copy()
        if daily.empty:
            continue
        ranked = b_generator.rank_candidates(daily)
        output = b_generator.build_output(ranked, signal_date, top_n=b_generator.default_top_n)
        selected = output[output["planned_action"].astype(str) == selected_action].copy()
        if selected.empty:
            continue
        row = selected.iloc[0].copy()
        row["trade_date"] = normalize_date(row["signal_date"])
        rows.append(row)
    return pd.DataFrame(rows)


def replay_map(replayed_b: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    result: dict[tuple[str, str], pd.Series] = {}
    for row in replayed_b.itertuples(index=False):
        result[(normalize_date(getattr(row, "trade_date", "")), str(getattr(row, "ts_code", "")))] = pd.Series(
            row._asdict()
        )
    return result


def build_audit_row(
    scenario: str,
    signal_date: str,
    strategy_leg: str,
    status: str,
    equity_before: float,
    equity_after: float,
    row: pd.Series | None = None,
    account_return: float = 0.0,
    return_source: str = "",
    active_label: str = "",
) -> dict[str, Any]:
    row = row if row is not None else pd.Series(dtype=object)
    return {
        "scenario": scenario,
        "signal_date": signal_date,
        "strategy_leg": strategy_leg,
        "operation_status": status,
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "market_segment": str(row.get("market_segment", "")),
        "risk_flags": str(row.get("risk_flags", "")),
        "account_return": float(account_return),
        "equity_before": float(equity_before),
        "equity_after": float(equity_after),
        "return_source": return_source,
        "active_position": active_label,
        "buy_executed": bool(row.get("buy_executed", False)) if row is not None else False,
        "sell_executed": bool(row.get("sell_executed", False)) if row is not None else False,
        "buy_reject_reason": str(row.get("buy_reject_reason", "")),
        "sell_reject_reason": str(row.get("sell_reject_reason", "")),
        "buy_trade_date": normalize_date(row.get("buy_trade_date", "")),
        "exit_trade_date": normalize_date(row.get("exit_trade_date", "")),
        "buy_price": row.get("buy_price", pd.NA),
        "exit_price": row.get("exit_price", pd.NA),
        "path_conflict": bool(row.get("path_conflict", False)) if row is not None else False,
        "limit_down_blocked_days": int(row.get("limit_down_blocked_days", 0) or 0),
        "live_order_enabled": False,
    }


def simulate_b_strict(
    replayed_b: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
) -> pd.DataFrame:
    by_key = replay_map(replayed_b)
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows = []
    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_audit_row(
                    "B_strict",
                    signal_date,
                    "B",
                    "POSITION_OCCUPIED_SKIP",
                    equity,
                    equity,
                    active_label=active_label,
                )
            )
            continue
        daily_keys = [key for key in by_key if key[0] == signal_date]
        if not daily_keys:
            rows.append(build_audit_row("B_strict", signal_date, "B", "NO_CANDIDATE", equity, equity))
            continue
        row = by_key[daily_keys[0]]
        if not bool(row.get("buy_executed", False)):
            rows.append(
                build_audit_row(
                    "B_strict",
                    signal_date,
                    "B",
                    "BUY_REJECTED",
                    equity,
                    equity,
                    row=row,
                    return_source="conservative_daily_replay",
                )
            )
            continue
        if not bool(row.get("sell_executed", False)):
            rows.append(
                build_audit_row(
                    "B_strict",
                    signal_date,
                    "B",
                    "SELL_UNRESOLVED",
                    equity,
                    equity,
                    row=row,
                    return_source="conservative_daily_replay",
                )
            )
            active_exit_date = normalize_date(row.get("exit_trade_date", ""))
            active_label = f"{row.get('ts_code', '')} {row.get('name', '')}"
            continue
        account_return = float(row.get("strict_account_return", 0.0))
        before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(row.get("exit_trade_date", ""))
        active_label = f"{row.get('ts_code', '')} {row.get('name', '')}"
        rows.append(
            build_audit_row(
                "B_strict",
                signal_date,
                "B",
                "HISTORICAL_SIM_FILLED",
                before,
                equity,
                row=row,
                account_return=account_return,
                return_source="conservative_daily_replay",
            )
        )
    return attach_drawdown(pd.DataFrame(rows), initial_equity)


def simulate_a_plus_b_strict(
    a_detail: pd.DataFrame,
    replayed_b: pd.DataFrame,
    audit: pd.DataFrame,
    dates: list[str],
    initial_equity: float,
) -> pd.DataFrame:
    by_key = replay_map(replayed_b)
    equity = initial_equity
    active_exit_date = ""
    active_label = ""
    rows = []
    a_by_date = {normalize_date(row.signal_date): pd.Series(row._asdict()) for row in a_detail.itertuples(index=False)}

    for signal_date in dates:
        if active_exit_date and signal_date < active_exit_date:
            rows.append(
                build_audit_row(
                    "A_plus_B_strict",
                    signal_date,
                    "A_OR_B",
                    "POSITION_OCCUPIED_SKIP",
                    equity,
                    equity,
                    active_label=active_label,
                )
            )
            continue

        a_row = a_by_date.get(signal_date)
        if a_row is not None and str(a_row.get("operation_status", "")) == "HISTORICAL_SIM_FILLED":
            audit_row = find_a_audit_row(audit, signal_date, str(a_row.get("ts_code", "")))
            account_return = float(a_row.get("account_return", 0.0))
            before = equity
            equity = equity * (1.0 + account_return)
            active_exit_date = normalize_date(audit_row.get("exit_trade_date", "")) if audit_row is not None else ""
            active_label = f"{a_row.get('ts_code', '')} {a_row.get('name', '')}"
            rows.append(
                build_audit_row(
                    "A_plus_B_strict",
                    signal_date,
                    "A",
                    "HISTORICAL_SIM_FILLED",
                    before,
                    equity,
                    row=a_row,
                    account_return=account_return,
                    return_source="a_audit_dynamic_account_return",
                )
            )
            continue
        if a_row is not None and str(a_row.get("operation_status", "")) == "REVIEW_REQUIRED_PLAN_ONLY":
            rows.append(
                build_audit_row(
                    "A_plus_B_strict",
                    signal_date,
                    "A",
                    "REVIEW_REQUIRED_PLAN_ONLY",
                    equity,
                    equity,
                    row=a_row,
                    return_source="manual_review_skip",
                )
            )
            continue

        daily_keys = [key for key in by_key if key[0] == signal_date]
        if not daily_keys:
            rows.append(build_audit_row("A_plus_B_strict", signal_date, "NONE", "NO_CANDIDATE", equity, equity))
            continue
        b_row = by_key[daily_keys[0]]
        if not bool(b_row.get("buy_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_strict",
                    signal_date,
                    "B",
                    "BUY_REJECTED",
                    equity,
                    equity,
                    row=b_row,
                    return_source="b_conservative_daily_replay",
                )
            )
            continue
        if not bool(b_row.get("sell_executed", False)):
            rows.append(
                build_audit_row(
                    "A_plus_B_strict",
                    signal_date,
                    "B",
                    "SELL_UNRESOLVED",
                    equity,
                    equity,
                    row=b_row,
                    return_source="b_conservative_daily_replay",
                )
            )
            active_exit_date = normalize_date(b_row.get("exit_trade_date", ""))
            active_label = f"{b_row.get('ts_code', '')} {b_row.get('name', '')}"
            continue
        account_return = float(b_row.get("strict_account_return", 0.0))
        before = equity
        equity = equity * (1.0 + account_return)
        active_exit_date = normalize_date(b_row.get("exit_trade_date", ""))
        active_label = f"{b_row.get('ts_code', '')} {b_row.get('name', '')}"
        rows.append(
            build_audit_row(
                "A_plus_B_strict",
                signal_date,
                "B",
                "HISTORICAL_SIM_FILLED",
                before,
                equity,
                row=b_row,
                account_return=account_return,
                return_source="b_conservative_daily_replay",
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


def summarize(detail: pd.DataFrame, scenario: str, condition: str) -> dict[str, Any]:
    status_counts = detail["operation_status"].value_counts().to_dict() if not detail.empty else {}
    trades = detail[detail["operation_status"] == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    initial_equity = float(detail["initial_equity"].iloc[0]) if "initial_equity" in detail.columns and not detail.empty else 0.0
    final_equity = float(detail["equity_after"].iloc[-1]) if not detail.empty else initial_equity
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    a_trade_count = (
        int(len(trades))
        if scenario == "A_strict"
        else int((trades.get("strategy_leg", pd.Series(dtype=str)) == "A").sum())
    )
    return {
        "scenario": scenario,
        "condition": condition,
        "day_count": int(len(detail)),
        "executed_trade_count": int(len(trades)),
        "b_trade_count": int((trades.get("strategy_leg", pd.Series(dtype=str)) == "B").sum()),
        "a_trade_count": a_trade_count,
        "buy_rejected_count": int(status_counts.get("BUY_REJECTED", 0)),
        "sell_unresolved_count": int(status_counts.get("SELL_UNRESOLVED", 0)),
        "review_required_count": int(status_counts.get("REVIEW_REQUIRED_PLAN_ONLY", 0)),
        "no_candidate_count": int(status_counts.get("NO_CANDIDATE", 0)),
        "position_occupied_skip_count": int(status_counts.get("POSITION_OCCUPIED_SKIP", 0)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
        "max_drawdown": float(detail["drawdown"].min()) if "drawdown" in detail.columns and not detail.empty else 0.0,
        "path_conflict_count": int(detail.get("path_conflict", pd.Series(False, index=detail.index)).astype(bool).sum())
        if not detail.empty
        else 0,
        "limit_down_blocked_trade_count": int(
            (pd.to_numeric(detail.get("limit_down_blocked_days", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum()
        )
        if not detail.empty
        else 0,
        "live_order_enabled": False,
    }


def build_gap_report(replayed_b: pd.DataFrame) -> pd.DataFrame:
    if replayed_b.empty:
        return pd.DataFrame()
    rows = []
    rows.append({"check_item": "b_selected_signal_count", "value": int(len(replayed_b)), "note": "B 条件选出的信号数"})
    rows.append({"check_item": "b_buy_rejected_count", "value": int((replayed_b["buy_executed"] == False).sum()), "note": "日线保守口径下 T+1 涨停开盘等买入失败"})  # noqa: E712
    rows.append({"check_item": "b_sell_unresolved_count", "value": int(((replayed_b["buy_executed"] == True) & (replayed_b["sell_executed"] == False)).sum()), "note": "日线保守口径下卖出未解决"})  # noqa: E712
    rows.append({"check_item": "b_path_conflict_count", "value": int(replayed_b["path_conflict"].fillna(False).astype(bool).sum()), "note": "止盈止损同日路径冲突"})
    rows.append({"check_item": "b_limit_down_blocked_count", "value": int((pd.to_numeric(replayed_b["limit_down_blocked_days"], errors="coerce").fillna(0) > 0).sum()), "note": "跌停阻塞卖出"})
    rows.append({"check_item": "minute_k_required", "value": int(len(replayed_b)), "note": "仍需分钟 K / 集合竞价 / 五档盘口验证"})
    return pd.DataFrame(rows)


def write_markdown(path: Path, summary: pd.DataFrame, gap: pd.DataFrame, combo_detail: pd.DataFrame) -> None:
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
    ]
    detail_columns = [column for column in detail_columns if column in combo_detail.columns]
    content = f"""# 备用策略 B 日线保守成交审计

本报告只使用本地日线数据和保守成交模型，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 审计缺口

{gap.to_markdown(index=False) if not gap.empty else "无审计缺口。"}

## A+B 严格逐日明细

{combo_detail[detail_columns].to_markdown(index=False) if not combo_detail.empty else "无逐日明细。"}

## 口径限制

- B 使用日线保守成交回放，不是盘口五档真实撮合。
- 涨停开盘默认买不到，跌停日默认无法卖出。
- 该结果只能判断 B 是否值得进入分钟 K / 盘口验证，不能直接用于实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_config = load_json_config(args.strategy_config)
    condition = parse_condition(args.b_condition)
    condition_text = f"{condition['column']}={condition['value']}"

    base_generator = PaperCandidateGenerator(args.strategy_config)
    all_candidates = base_generator.load_all_candidates()
    dates = resolve_recent_dates(all_candidates, args.recent_days, args.end_date)
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

    b_config = backup_config(base_config, [condition])
    b_generator = build_generator(args.strategy_config, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, args.runtime_config)

    b_strict_detail = simulate_b_strict(replayed_b, no_candidate_dates, initial_equity)
    combo_detail = simulate_a_plus_b_strict(a_detail, replayed_b, audit, dates, initial_equity)
    summary = pd.DataFrame(
        [
            summarize(a_detail, "A_strict", "current_config"),
            summarize(b_strict_detail, "B_strict_on_A_no_candidate_days", condition_text),
            summarize(combo_detail, "A_plus_B_strict", condition_text),
        ]
    )
    gap = build_gap_report(replayed_b)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{dates[0]}_{dates[-1]}_{args.recent_days}d"
    selected_path = output_prefix.with_name(output_prefix.name + suffix + "_b_selected.csv")
    replayed_path = output_prefix.with_name(output_prefix.name + suffix + "_b_replayed.csv")
    b_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_b_strict_detail.csv")
    combo_detail_path = output_prefix.with_name(output_prefix.name + suffix + "_a_plus_b_detail.csv")
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    gap_path = output_prefix.with_name(output_prefix.name + suffix + "_gap.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    selected_b.to_csv(selected_path, index=False, encoding="utf-8-sig")
    replayed_b.to_csv(replayed_path, index=False, encoding="utf-8-sig")
    b_strict_detail.to_csv(b_detail_path, index=False, encoding="utf-8-sig")
    combo_detail.to_csv(combo_detail_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    gap.to_csv(gap_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, gap, combo_detail)

    print("备用策略 B 日线保守成交审计完成：")
    print(f"- selected_b: {selected_path}")
    print(f"- replayed_b: {replayed_path}")
    print(f"- b_strict_detail: {b_detail_path}")
    print(f"- a_plus_b_detail: {combo_detail_path}")
    print(f"- summary: {summary_path}")
    print(f"- gap: {gap_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))
    print(gap.to_string(index=False))


if __name__ == "__main__":
    main()
