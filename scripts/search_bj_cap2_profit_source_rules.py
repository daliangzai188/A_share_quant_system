"""
搜索 bj_cap_2pct 当前候选池的收益来源规则。

文件作用：
1. 基于当前最近 2 年方案2的 180 条逐日候选信号做完整回放。
2. 测试收益来源分桶的保留/排除规则，而不是继续做风险评分。
3. 分别输出全区间、2024-2025训练期、2026测试期表现。
4. 为下一轮扩大候选池搜索提供方向。

注意：
本脚本只使用已有小型候选报告，不读取 1.8GB 日线大文件，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="搜索 bj_cap_2pct 当前候选池收益来源规则。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="最近2年方案逐信号交易明细。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认回放方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--train-start", default="20240101", help="训练开始日期。")
    parser.add_argument("--train-end", default="20251231", help="训练结束日期。")
    parser.add_argument("--test-start", default="20260101", help="测试开始日期。")
    parser.add_argument("--test-end", default="20260518", help="测试结束日期。")
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_profit_source_rules",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def estimate_slippage(amount_ratio: float, tiers: list[dict[str, Any]]) -> float:
    if pd.isna(amount_ratio) or amount_ratio <= 0:
        return 0.0
    for tier in tiers:
        threshold = tier.get("max_amount_ratio")
        if threshold is None or amount_ratio <= float(threshold) + 1e-12:
            return float(tier.get("slippage_rate", 0.0))
    return 0.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns:
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def load_rows(path: Path, scenario_rank: int) -> pd.DataFrame:
    rows = pd.read_csv(path, low_memory=False)
    rows = rows[rows["scenario_rank"] == scenario_rank].copy()
    if rows.empty:
        raise RuntimeError(f"没有找到 scenario_rank={scenario_rank}: {path}")
    rows["selected_order"] = pd.to_numeric(rows["selected_order"], errors="coerce")
    rows["trade_date"] = rows["trade_date"].map(normalize_date)
    rows["buy_trade_date"] = rows["buy_trade_date"].map(normalize_date)
    rows["exit_trade_date"] = rows["exit_trade_date"].map(normalize_date)
    rows["market_segment"] = rows["market_segment"].fillna("unknown").astype(str)
    return rows.sort_values("selected_order").reset_index(drop=True)


def prepare_execution_config(config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("risk", {})
    opt = config.get("realistic_condition_strategy_search", {})
    return {
        "initial_cash": float(opt.get("initial_cash", 500000)),
        "position_pct": float(opt.get("position_pct", 0.8)),
        "default_capacity": float(opt.get("max_buy_amount_ratio", 0.05)),
        "bj_capacity": 0.02,
        "slippage_tiers": list(opt.get("slippage_tiers", [])),
        "fee_rate_without_slippage": (
            float(risk.get("commission_rate", 0.0003))
            + float(risk.get("transfer_fee_rate", 0.00001))
            + float(risk.get("commission_rate", 0.0003))
            + float(risk.get("transfer_fee_rate", 0.00001))
            + float(risk.get("stamp_tax_rate", 0.001))
        ),
    }


def profit_source_rules() -> list[dict[str, Any]]:
    return [
        {"rule_name": "baseline", "description": "不做额外收益来源过滤"},
        {
            "rule_name": "include_market_segment_bj",
            "description": "只保留 BJ 候选",
            "include": {"market_segment": {"bj"}},
        },
        {
            "rule_name": "include_market_segment_non_chi_star",
            "description": "排除创业板和科创板，只保留 BJ / 主板",
            "exclude": {"market_segment": {"chi_next", "star"}},
        },
        {
            "rule_name": "include_first_time_late",
            "description": "只保留 13:30 后涨停",
            "include": {"first_time_detail_bucket": {"1330_1430", "after_1430"}},
        },
        {
            "rule_name": "exclude_first_time_weak",
            "description": "排除 11:00 前和 11:00-13:30 涨停",
            "exclude": {"first_time_detail_bucket": {"before_1000", "1000_1100", "1100_1330"}},
        },
        {
            "rule_name": "exclude_segment_retreat",
            "description": "排除板块退潮 weak/2day",
            "exclude": {"segment_retreat_state_bucket": {"retreat_2day", "retreat_weak"}},
        },
        {
            "rule_name": "exclude_market_chain_3_8",
            "description": "排除全市场连板数 3-8 的低连板环境",
            "exclude": {"market_chain_count_bucket": {"3_8"}},
        },
        {
            "rule_name": "include_market_chain_8_plus",
            "description": "只保留全市场连板数 8 以上环境",
            "include": {"market_chain_count_bucket": {"8_15", "15_30", "gte_30"}},
        },
        {
            "rule_name": "include_down_5_60",
            "description": "只保留跌停数 5-60 的非极端环境",
            "include": {"market_limit_down_count_bucket": {"5_15", "15_30", "30_60"}},
        },
        {
            "rule_name": "include_fd_low_mid",
            "description": "只保留封单/流通市值 0.1%-2%",
            "include": {"fd_ratio_bucket": {"0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct", "1pct_2pct"}},
        },
        {
            "rule_name": "include_amount_under_8e8",
            "description": "只保留成交额 8 亿以下",
            "include": {"amount_bucket": {"lt_1e8", "1e8_3e8", "3e8_8e8"}},
        },
        {
            "rule_name": "include_turnover_6_10_or_gte25",
            "description": "只保留换手 6-10 或 >=25",
            "include": {"turnover_rate_bucket": {"6_10", "gte_25"}},
        },
        {
            "rule_name": "combo_late_exclude_retreat",
            "description": "13:30后涨停 + 排除板块退潮",
            "include": {"first_time_detail_bucket": {"1330_1430", "after_1430"}},
            "exclude": {"segment_retreat_state_bucket": {"retreat_2day", "retreat_weak"}},
        },
        {
            "rule_name": "combo_chain8_down5_60",
            "description": "连板环境8+ + 跌停数5-60",
            "include": {
                "market_chain_count_bucket": {"8_15", "15_30", "gte_30"},
                "market_limit_down_count_bucket": {"5_15", "15_30", "30_60"},
            },
        },
        {
            "rule_name": "combo_fd_low_mid_down5_60",
            "description": "封单低中位 + 跌停数5-60",
            "include": {
                "fd_ratio_bucket": {"0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct", "1pct_2pct"},
                "market_limit_down_count_bucket": {"5_15", "15_30", "30_60"},
            },
        },
        {
            "rule_name": "combo_late_chain8",
            "description": "13:30后涨停 + 连板环境8+",
            "include": {
                "first_time_detail_bucket": {"1330_1430", "after_1430"},
                "market_chain_count_bucket": {"8_15", "15_30", "gte_30"},
            },
        },
        {
            "rule_name": "combo_late_chain8_exclude_retreat",
            "description": "13:30后涨停 + 连板环境8+ + 排除板块退潮",
            "include": {
                "first_time_detail_bucket": {"1330_1430", "after_1430"},
                "market_chain_count_bucket": {"8_15", "15_30", "gte_30"},
            },
            "exclude": {"segment_retreat_state_bucket": {"retreat_2day", "retreat_weak"}},
        },
        {
            "rule_name": "combo_bj_or_main_late",
            "description": "BJ/主板 + 13:30后涨停",
            "include": {
                "market_segment": {"bj", "sh_main", "sz_main"},
                "first_time_detail_bucket": {"1330_1430", "after_1430"},
            },
        },
    ]


def row_passes_rule(row: pd.Series, rule: dict[str, Any]) -> bool:
    includes = rule.get("include", {})
    excludes = rule.get("exclude", {})
    for column, allowed_values in includes.items():
        if str(row.get(column, "missing")) not in allowed_values:
            return False
    for column, blocked_values in excludes.items():
        if str(row.get(column, "missing")) in blocked_values:
            return False
    return True


def build_skipped(row: pd.Series, rule: dict[str, Any], equity: float, reason: str) -> dict[str, Any]:
    result = row.to_dict()
    result.update(
        {
            "profit_rule_name": rule["rule_name"],
            "profit_rule_description": rule["description"],
            "profit_executed": False,
            "profit_skip_reason": reason,
            "profit_trade_order": pd.NA,
            "profit_equity_before": equity,
            "profit_account_return": 0.0,
            "profit_equity_after": equity,
            "profit_actual_position_pct": 0.0,
            "profit_buy_slippage": 0.0,
            "profit_sell_slippage": 0.0,
        }
    )
    return result


def build_trade(
    row: pd.Series,
    rule: dict[str, Any],
    equity: float,
    trade_order: int,
    execution_config: dict[str, Any],
) -> dict[str, Any]:
    buy_day_amount = float(row.get("buy_day_amount_yuan", 0.0)) if pd.notna(row.get("buy_day_amount_yuan")) else 0.0
    sell_day_amount = float(row.get("sell_day_amount_yuan", 0.0)) if pd.notna(row.get("sell_day_amount_yuan")) else 0.0
    buy_price_raw = (
        float(row.get("buy_price_before_slippage", 0.0))
        if pd.notna(row.get("buy_price_before_slippage"))
        else 0.0
    )
    sell_price_raw = (
        float(row.get("exit_price_before_slippage", 0.0))
        if pd.notna(row.get("exit_price_before_slippage"))
        else 0.0
    )
    if buy_day_amount <= 0 or sell_day_amount <= 0 or buy_price_raw <= 0 or sell_price_raw <= 0:
        return build_skipped(row, rule, equity, "missing_liquidity_or_price")

    capacity = (
        float(execution_config["bj_capacity"])
        if str(row.get("market_segment", "")) == "bj"
        else float(execution_config["default_capacity"])
    )
    target_buy_amount = equity * float(execution_config["position_pct"])
    actual_buy_amount = min(target_buy_amount, buy_day_amount * capacity)
    actual_position_pct = actual_buy_amount / equity if equity > 0 else 0.0
    buy_amount_ratio = actual_buy_amount / buy_day_amount if buy_day_amount > 0 else 0.0
    buy_slippage = estimate_slippage(buy_amount_ratio, list(execution_config["slippage_tiers"]))
    buy_price = buy_price_raw * (1.0 + buy_slippage)

    sell_value_before_slippage = actual_buy_amount * sell_price_raw / buy_price
    sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
    sell_slippage = estimate_slippage(sell_amount_ratio, list(execution_config["slippage_tiers"]))
    sell_price = sell_price_raw * (1.0 - sell_slippage)
    net_return = sell_price / buy_price - 1.0 - float(execution_config["fee_rate_without_slippage"])
    account_return = net_return * actual_position_pct
    equity_after = equity * (1.0 + account_return)

    result = row.to_dict()
    result.update(
        {
            "profit_rule_name": rule["rule_name"],
            "profit_rule_description": rule["description"],
            "profit_executed": True,
            "profit_skip_reason": "",
            "profit_trade_order": trade_order,
            "profit_equity_before": equity,
            "profit_account_return": account_return,
            "profit_equity_after": equity_after,
            "profit_actual_position_pct": actual_position_pct,
            "profit_buy_slippage": buy_slippage,
            "profit_sell_slippage": sell_slippage,
        }
    )
    return result


def replay_profit_rule(rows: pd.DataFrame, rule: dict[str, Any], execution_config: dict[str, Any]) -> pd.DataFrame:
    equity = float(execution_config["initial_cash"])
    occupied_until = ""
    trade_order = 0
    details = []
    for _, row in rows.iterrows():
        buy_trade_date = normalize_date(row.get("buy_trade_date", ""))
        if occupied_until and buy_trade_date <= occupied_until:
            details.append(build_skipped(row, rule, equity, "position_occupied"))
            continue
        if not row_passes_rule(row, rule):
            details.append(build_skipped(row, rule, equity, "profit_source_filter"))
            continue
        if not bool(row.get("buy_executed", False)):
            details.append(build_skipped(row, rule, equity, str(row.get("buy_reject_reason", "buy_not_executed"))))
            continue
        if not bool(row.get("sell_executed", False)) or pd.isna(row.get("exit_price_before_slippage")):
            details.append(build_skipped(row, rule, equity, str(row.get("sell_reject_reason", "sell_not_executed"))))
            continue
        trade_order += 1
        result = build_trade(row, rule, equity, trade_order, execution_config)
        if bool(result["profit_executed"]):
            equity = float(result["profit_equity_after"])
            occupied_until = normalize_date(row.get("exit_trade_date", ""))
        details.append(result)
    return pd.DataFrame(details)


def summarize_profit_detail(detail: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
    executed = detail[detail["profit_executed"] == True].copy()  # noqa: E712
    returns = executed["profit_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    final_equity = float(executed["profit_equity_after"].iloc[-1]) if not executed.empty else initial_cash
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "rule_name": str(detail["profit_rule_name"].iloc[0]) if not detail.empty else "",
        "description": str(detail["profit_rule_description"].iloc[0]) if not detail.empty else "",
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_cash if initial_cash else 0.0,
        "signal_count": int(len(detail)),
        "executed_trade_count": int(len(executed)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_drawdown": max_drawdown(executed["profit_equity_after"].astype(float)) if len(executed) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["profit_actual_position_pct"].mean()) if len(executed) else 0.0,
        "avg_buy_slippage": float(executed["profit_buy_slippage"].mean()) if len(executed) else 0.0,
        "avg_sell_slippage": float(executed["profit_sell_slippage"].mean()) if len(executed) else 0.0,
        "filter_skip_count": int((detail["profit_skip_reason"].astype(str) == "profit_source_filter").sum()),
        "position_occupied_skip_count": int((detail["profit_skip_reason"].astype(str) == "position_occupied").sum()),
        "buy_rejected_count": int((detail["profit_skip_reason"].astype(str) == "open_limit_up_unbuyable").sum()),
        "sell_unresolved_count": int(
            detail["profit_skip_reason"].astype(str).str.contains("limit_down|sell", regex=True).sum()
        ),
    }


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["profit_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["exit_trade_date"].map(normalize_date).str[:4]
    rows = []
    for (rule_name, year), group in executed.groupby(["profit_rule_name", "year"]):
        first_equity = float(group["profit_equity_before"].iloc[0])
        last_equity = float(group["profit_equity_after"].iloc[-1])
        returns = group["profit_account_return"].astype(float)
        rows.append(
            {
                "rule_name": rule_name,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["profit_equity_after"].astype(float)),
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def slice_by_trade_date(rows: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    trade_dates = rows["trade_date"].map(normalize_date)
    return rows[(trade_dates >= start_date) & (trade_dates <= end_date)].copy().reset_index(drop=True)


def replay_rules(rows: pd.DataFrame, execution_config: dict[str, Any], period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    summaries = []
    for rule in profit_source_rules():
        detail = replay_profit_rule(rows, rule, execution_config)
        detail["period"] = period
        summary = summarize_profit_detail(detail, float(execution_config["initial_cash"]))
        summary["period"] = period
        frames.append(detail)
        summaries.append(summary)
    return pd.DataFrame(summaries), pd.concat(frames, ignore_index=True)


def add_baseline_delta(summary: pd.DataFrame) -> pd.DataFrame:
    result = summary.copy()
    for period, group in result.groupby("period"):
        baseline = group[group["rule_name"] == "baseline"].iloc[0]
        mask = result["period"] == period
        result.loc[mask, "multiple_delta_vs_baseline"] = (
            result.loc[mask, "equity_multiple"] - float(baseline["equity_multiple"])
        )
        result.loc[mask, "drawdown_delta_vs_baseline"] = (
            result.loc[mask, "max_drawdown"] - float(baseline["max_drawdown"])
        )
        result.loc[mask, "beats_baseline_multiple"] = (
            result.loc[mask, "equity_multiple"] > float(baseline["equity_multiple"])
        )
        result.loc[mask, "improves_baseline_drawdown"] = (
            result.loc[mask, "max_drawdown"] > float(baseline["max_drawdown"])
        )
    return result


def build_walk_forward_report(full: pd.DataFrame, train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    full = add_baseline_delta(full)
    train = add_baseline_delta(train)
    test = add_baseline_delta(test)
    train = train.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, False]).reset_index(drop=True)
    train["train_rank"] = train.index + 1
    keys = ["rule_name", "description"]
    report = train[keys + [
        "train_rank",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "multiple_delta_vs_baseline",
        "drawdown_delta_vs_baseline",
        "beats_baseline_multiple",
        "improves_baseline_drawdown",
    ]].rename(
        columns={
            "equity_multiple": "train_equity_multiple",
            "executed_trade_count": "train_trade_count",
            "win_rate": "train_win_rate",
            "max_drawdown": "train_max_drawdown",
            "multiple_delta_vs_baseline": "train_multiple_delta_vs_baseline",
            "drawdown_delta_vs_baseline": "train_drawdown_delta_vs_baseline",
            "beats_baseline_multiple": "train_beats_baseline_multiple",
            "improves_baseline_drawdown": "train_improves_baseline_drawdown",
        }
    )
    test_view = test[keys + [
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "multiple_delta_vs_baseline",
        "drawdown_delta_vs_baseline",
        "beats_baseline_multiple",
        "improves_baseline_drawdown",
    ]].rename(
        columns={
            "equity_multiple": "test_equity_multiple",
            "executed_trade_count": "test_trade_count",
            "win_rate": "test_win_rate",
            "max_drawdown": "test_max_drawdown",
            "multiple_delta_vs_baseline": "test_multiple_delta_vs_baseline",
            "drawdown_delta_vs_baseline": "test_drawdown_delta_vs_baseline",
            "beats_baseline_multiple": "test_beats_baseline_multiple",
            "improves_baseline_drawdown": "test_improves_baseline_drawdown",
        }
    )
    full_view = full[keys + [
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "multiple_delta_vs_baseline",
        "drawdown_delta_vs_baseline",
    ]].rename(
        columns={
            "equity_multiple": "full_equity_multiple",
            "executed_trade_count": "full_trade_count",
            "win_rate": "full_win_rate",
            "max_drawdown": "full_max_drawdown",
            "multiple_delta_vs_baseline": "full_multiple_delta_vs_baseline",
            "drawdown_delta_vs_baseline": "full_drawdown_delta_vs_baseline",
        }
    )
    report = report.merge(test_view, on=keys, how="left", validate="one_to_one")
    report = report.merge(full_view, on=keys, how="left", validate="one_to_one")
    report["passes_oos_both"] = report["test_beats_baseline_multiple"] & report["test_improves_baseline_drawdown"]
    return report


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(PROJECT_ROOT / args.config)
    execution_config = prepare_execution_config(config)
    rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    train_rows = slice_by_trade_date(rows, args.train_start, args.train_end)
    test_rows = slice_by_trade_date(rows, args.test_start, args.test_end)

    full_summary, full_detail = replay_rules(rows, execution_config, "full")
    train_summary, train_detail = replay_rules(train_rows, execution_config, "train")
    test_summary, test_detail = replay_rules(test_rows, execution_config, "test")
    wf = build_walk_forward_report(full_summary, train_summary, test_summary)
    detail = pd.concat([full_detail, train_detail, test_detail], ignore_index=True)
    yearly = build_yearly(full_detail)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    full_path = output_prefix.with_name(output_prefix.name + "_full_summary.csv")
    train_path = output_prefix.with_name(output_prefix.name + "_train_summary.csv")
    test_path = output_prefix.with_name(output_prefix.name + "_test_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    wf.to_csv(summary_path, index=False, encoding="utf-8-sig")
    add_baseline_delta(full_summary).to_csv(full_path, index=False, encoding="utf-8-sig")
    add_baseline_delta(train_summary).to_csv(train_path, index=False, encoding="utf-8-sig")
    add_baseline_delta(test_summary).to_csv(test_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")

    display_cols = [
        "train_rank",
        "rule_name",
        "train_equity_multiple",
        "train_max_drawdown",
        "train_trade_count",
        "test_equity_multiple",
        "test_max_drawdown",
        "test_trade_count",
        "full_equity_multiple",
        "full_max_drawdown",
        "passes_oos_both",
    ]
    print("bj_cap_2pct 收益来源规则搜索完成")
    print(wf[display_cols].head(20).to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- full_summary: {full_path}")
    print(f"- train_summary: {train_path}")
    print(f"- test_summary: {test_path}")
    print(f"- detail: {detail_path}")
    print(f"- yearly: {yearly_path}")


if __name__ == "__main__":
    main()
