"""
bj_cap_2pct 多因子风险评分模型 walk-forward 验证。

文件作用：
1. 只用 2024-2025 基准成交交易学习风险分桶，不使用 2026 结果训练。
2. 将多个前视字段组合成风险分数，而不是继续寻找单个过滤分桶。
3. 测试跳过高风险信号、降低高风险信号仓位两类风控方式。
4. 分别输出训练期和 2026 测试期结果，检查样本外是否改善。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_bj_cap2_full_replay_with_forward_filters import (  # noqa: E402
    build_skipped_result,
    build_yearly,
    estimate_slippage,
    load_config,
    load_rows,
    max_consecutive_losses,
    max_drawdown,
    normalize_date,
    replay_rule,
    summarize_detail,
)
from scripts.walk_forward_bj_cap2_filter_rules import (  # noqa: E402
    prepare_execution_config,
    slice_by_trade_date,
)

BASE_RULE_NAME = "bj_cap_2pct_full_replay"
RISK_FEATURES = [
    "market_segment",
    "fd_ratio_bucket",
    "market_limit_down_count_bucket",
    "market_chain_count_bucket",
    "segment_retreat_state_bucket",
    "market_emotion_state_bucket",
    "amount_bucket",
    "turnover_rate_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bj_cap_2pct 风险评分模型 walk-forward 验证。")
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
        default="reports/bj_cap2_risk_score_model",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def baseline_rule() -> dict[str, Any]:
    return {
        "rule_name": BASE_RULE_NAME,
        "description": "完整回放基准：BJ容量2%，不额外过滤",
        "conditions": tuple(),
    }


def risk_model_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_name": "risk_score_skip_2",
            "description": "风险分数>=2则跳过",
            "skip_score": 2,
        },
        {
            "rule_name": "risk_score_skip_3",
            "description": "风险分数>=3则跳过",
            "skip_score": 3,
        },
        {
            "rule_name": "risk_score_skip_4",
            "description": "风险分数>=4则跳过",
            "skip_score": 4,
        },
        {
            "rule_name": "risk_score_half_2",
            "description": "风险分数>=2则仓位降到40%",
            "half_score": 2,
            "reduced_position_pct": 0.4,
        },
        {
            "rule_name": "risk_score_half_3",
            "description": "风险分数>=3则仓位降到40%",
            "half_score": 3,
            "reduced_position_pct": 0.4,
        },
        {
            "rule_name": "risk_score_half_4",
            "description": "风险分数>=4则仓位降到40%",
            "half_score": 4,
            "reduced_position_pct": 0.4,
        },
        {
            "rule_name": "risk_score_half_2_skip_4",
            "description": "风险分数>=2降到40%，>=4跳过",
            "half_score": 2,
            "skip_score": 4,
            "reduced_position_pct": 0.4,
        },
        {
            "rule_name": "risk_score_half_3_skip_5",
            "description": "风险分数>=3降到40%，>=5跳过",
            "half_score": 3,
            "skip_score": 5,
            "reduced_position_pct": 0.4,
        },
        {
            "rule_name": "risk_score_half_2_skip_5",
            "description": "风险分数>=2降到40%，>=5跳过",
            "half_score": 2,
            "skip_score": 5,
            "reduced_position_pct": 0.4,
        },
    ]


def learn_risk_table(train_baseline_detail: pd.DataFrame) -> pd.DataFrame:
    executed = train_baseline_detail[train_baseline_detail["replay_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    baseline_avg = float(executed["replay_account_return"].mean())
    baseline_win = float((executed["replay_account_return"] > 0).mean())
    rows = []
    for feature in RISK_FEATURES:
        if feature not in executed.columns:
            continue
        for value, group in executed.groupby(executed[feature].fillna("missing").astype(str)):
            sample_count = int(len(group))
            avg_return = float(group["replay_account_return"].mean())
            win_rate = float((group["replay_account_return"] > 0).mean())
            max_loss = float(group["replay_account_return"].min())
            risk_points = 0
            reasons = []
            if sample_count >= 5 and avg_return < baseline_avg and win_rate < baseline_win:
                risk_points += 1
                reasons.append("avg_and_win_below_baseline")
            if sample_count >= 5 and avg_return <= 0:
                risk_points += 1
                reasons.append("avg_non_positive")
            large_loss_bucket = sample_count >= 5 and max_loss <= -0.10
            rows.append(
                {
                    "feature": feature,
                    "value": value,
                    "sample_count": sample_count,
                    "avg_return": avg_return,
                    "win_rate": win_rate,
                    "max_loss": max_loss,
                    "baseline_avg_return": baseline_avg,
                    "baseline_win_rate": baseline_win,
                    "risk_points": risk_points,
                    "risk_reasons": ";".join(reasons),
                    "large_loss_bucket": large_loss_bucket,
                }
            )
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    return table.sort_values(["risk_points", "avg_return"], ascending=[False, True]).reset_index(drop=True)


def risk_lookup_from_table(risk_table: pd.DataFrame) -> dict[tuple[str, str], int]:
    lookup: dict[tuple[str, str], int] = {}
    for _, row in risk_table.iterrows():
        points = int(row["risk_points"])
        if points <= 0:
            continue
        lookup[(str(row["feature"]), str(row["value"]))] = points
    return lookup


def calculate_risk_score(row: pd.Series, risk_lookup: dict[tuple[str, str], int]) -> tuple[int, str]:
    score = 0
    hits = []
    for feature in RISK_FEATURES:
        value = str(row.get(feature, "missing"))
        points = risk_lookup.get((feature, value), 0)
        if points:
            score += points
            hits.append(f"{feature}={value}:{points}")
    return score, ";".join(hits)


def build_trade_result_with_position(
    row: pd.Series,
    rule: dict[str, Any],
    equity: float,
    trade_order: int,
    position_pct: float,
    execution_config: dict[str, Any],
    risk_score: int,
    risk_hits: str,
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
        result = build_skipped_result(row, rule, equity, "missing_liquidity_or_price")
        result["risk_score"] = risk_score
        result["risk_hits"] = risk_hits
        return result

    capacity = (
        float(execution_config["bj_capacity"])
        if str(row.get("market_segment", "")) == "bj"
        else float(execution_config["default_capacity"])
    )
    target_buy_amount = equity * position_pct
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
            "replay_rule_name": rule["rule_name"],
            "replay_rule_description": rule["description"],
            "replay_executed": True,
            "replay_skip_reason": "",
            "replay_trade_order": trade_order,
            "replay_capacity": capacity,
            "replay_equity_before": equity,
            "replay_target_buy_amount": target_buy_amount,
            "replay_actual_buy_amount": actual_buy_amount,
            "replay_actual_position_pct": actual_position_pct,
            "replay_buy_amount_ratio": buy_amount_ratio,
            "replay_sell_amount_ratio": sell_amount_ratio,
            "replay_buy_slippage": buy_slippage,
            "replay_sell_slippage": sell_slippage,
            "replay_buy_price": buy_price,
            "replay_sell_price": sell_price,
            "replay_net_return": net_return,
            "replay_account_return": account_return,
            "replay_equity_after": equity_after,
            "risk_score": risk_score,
            "risk_hits": risk_hits,
            "risk_position_pct": position_pct,
        }
    )
    return result


def replay_risk_model_rule(
    rows: pd.DataFrame,
    rule: dict[str, Any],
    execution_config: dict[str, Any],
    risk_lookup: dict[tuple[str, str], int],
) -> pd.DataFrame:
    equity = float(execution_config["initial_cash"])
    occupied_until = ""
    trade_order = 0
    details = []
    base_position_pct = float(execution_config["position_pct"])

    for _, row in rows.iterrows():
        buy_trade_date = normalize_date(row.get("buy_trade_date", ""))
        risk_score, risk_hits = calculate_risk_score(row, risk_lookup)
        if occupied_until and buy_trade_date <= occupied_until:
            result = build_skipped_result(row, rule, equity, "position_occupied")
            result["risk_score"] = risk_score
            result["risk_hits"] = risk_hits
            details.append(result)
            continue

        skip_score = rule.get("skip_score")
        if skip_score is not None and risk_score >= int(skip_score):
            result = build_skipped_result(row, rule, equity, "risk_score_skip")
            result["risk_score"] = risk_score
            result["risk_hits"] = risk_hits
            details.append(result)
            continue

        if not bool(row.get("buy_executed", False)):
            reason = str(row.get("buy_reject_reason", "buy_not_executed"))
            result = build_skipped_result(row, rule, equity, reason)
            result["risk_score"] = risk_score
            result["risk_hits"] = risk_hits
            details.append(result)
            continue
        if not bool(row.get("sell_executed", False)) or pd.isna(row.get("exit_price_before_slippage")):
            reason = str(row.get("sell_reject_reason", "sell_not_executed"))
            result = build_skipped_result(row, rule, equity, reason)
            result["risk_score"] = risk_score
            result["risk_hits"] = risk_hits
            details.append(result)
            continue

        position_pct = base_position_pct
        half_score = rule.get("half_score")
        if half_score is not None and risk_score >= int(half_score):
            position_pct = float(rule.get("reduced_position_pct", 0.4))

        trade_order += 1
        result = build_trade_result_with_position(
            row=row,
            rule=rule,
            equity=equity,
            trade_order=trade_order,
            position_pct=position_pct,
            execution_config=execution_config,
            risk_score=risk_score,
            risk_hits=risk_hits,
        )
        if bool(result["replay_executed"]):
            equity = float(result["replay_equity_after"])
            occupied_until = normalize_date(row.get("exit_trade_date", ""))
        details.append(result)
    return pd.DataFrame(details)


def replay_baseline(rows: pd.DataFrame, execution_config: dict[str, Any]) -> pd.DataFrame:
    return replay_rule(
        rows=rows,
        rule=baseline_rule(),
        initial_cash=float(execution_config["initial_cash"]),
        position_pct=float(execution_config["position_pct"]),
        default_capacity=float(execution_config["default_capacity"]),
        bj_capacity=float(execution_config["bj_capacity"]),
        slippage_tiers=list(execution_config["slippage_tiers"]),
        fee_rate_without_slippage=float(execution_config["fee_rate_without_slippage"]),
    )


def summarize_model_detail(detail: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
    summary = summarize_detail(detail, initial_cash)
    executed = detail[detail["replay_executed"] == True].copy()  # noqa: E712
    summary["avg_risk_score"] = float(executed["risk_score"].mean()) if len(executed) and "risk_score" in executed else 0.0
    summary["risk_skip_count"] = int((detail["replay_skip_reason"].astype(str) == "risk_score_skip").sum())
    summary["reduced_position_count"] = (
        int((executed["risk_position_pct"].astype(float) < 0.799).sum())
        if len(executed) and "risk_position_pct" in executed
        else 0
    )
    return summary


def replay_all_models(
    rows: pd.DataFrame,
    execution_config: dict[str, Any],
    risk_lookup: dict[tuple[str, str], int],
    period_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    baseline = replay_baseline(rows, execution_config)
    baseline["wf_period"] = period_name
    baseline["risk_score"] = 0
    baseline["risk_hits"] = ""
    baseline["risk_position_pct"] = float(execution_config["position_pct"])
    frames.append(baseline)
    for rule in risk_model_rules():
        detail = replay_risk_model_rule(rows, rule, execution_config, risk_lookup)
        detail["wf_period"] = period_name
        frames.append(detail)
    detail_report = pd.concat(frames, ignore_index=True)
    summary_rows = [summarize_model_detail(frame, float(execution_config["initial_cash"])) for frame in frames]
    summary = pd.DataFrame(summary_rows)
    summary["wf_period"] = period_name
    return summary, detail_report


def add_period_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    result = summary.copy()
    for period_name, group in result.groupby("wf_period"):
        baseline = group[group["rule_name"] == BASE_RULE_NAME].iloc[0]
        mask = result["wf_period"] == period_name
        result.loc[mask, "equity_multiple_delta_vs_baseline"] = (
            result.loc[mask, "equity_multiple"] - float(baseline["equity_multiple"])
        )
        result.loc[mask, "max_drawdown_delta_vs_baseline"] = (
            result.loc[mask, "max_drawdown"] - float(baseline["max_drawdown"])
        )
        result.loc[mask, "win_rate_delta_vs_baseline"] = result.loc[mask, "win_rate"] - float(baseline["win_rate"])
        result.loc[mask, "beats_baseline_multiple"] = (
            result.loc[mask, "equity_multiple"] > float(baseline["equity_multiple"])
        )
        result.loc[mask, "improves_baseline_drawdown"] = (
            result.loc[mask, "max_drawdown"] > float(baseline["max_drawdown"])
        )
    return result


def build_walk_forward_report(train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    train = add_period_deltas(train)
    test = add_period_deltas(test)
    train = train.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, False]).reset_index(drop=True)
    train["train_rank"] = train.index + 1
    train_view = train.rename(
        columns={
            "equity_multiple": "train_equity_multiple",
            "executed_trade_count": "train_trade_count",
            "win_rate": "train_win_rate",
            "max_drawdown": "train_max_drawdown",
            "max_loss": "train_max_loss",
            "risk_skip_count": "train_risk_skip_count",
            "reduced_position_count": "train_reduced_position_count",
            "equity_multiple_delta_vs_baseline": "train_multiple_delta_vs_baseline",
            "max_drawdown_delta_vs_baseline": "train_drawdown_delta_vs_baseline",
            "beats_baseline_multiple": "train_beats_baseline_multiple",
            "improves_baseline_drawdown": "train_improves_baseline_drawdown",
        }
    )
    test_view = test.rename(
        columns={
            "equity_multiple": "test_equity_multiple",
            "executed_trade_count": "test_trade_count",
            "win_rate": "test_win_rate",
            "max_drawdown": "test_max_drawdown",
            "max_loss": "test_max_loss",
            "risk_skip_count": "test_risk_skip_count",
            "reduced_position_count": "test_reduced_position_count",
            "equity_multiple_delta_vs_baseline": "test_multiple_delta_vs_baseline",
            "max_drawdown_delta_vs_baseline": "test_drawdown_delta_vs_baseline",
            "beats_baseline_multiple": "test_beats_baseline_multiple",
            "improves_baseline_drawdown": "test_improves_baseline_drawdown",
        }
    )
    keep_train = [
        "rule_name",
        "description",
        "train_rank",
        "train_equity_multiple",
        "train_trade_count",
        "train_win_rate",
        "train_max_drawdown",
        "train_max_loss",
        "train_risk_skip_count",
        "train_reduced_position_count",
        "train_multiple_delta_vs_baseline",
        "train_drawdown_delta_vs_baseline",
        "train_beats_baseline_multiple",
        "train_improves_baseline_drawdown",
    ]
    keep_test = [
        "rule_name",
        "test_equity_multiple",
        "test_trade_count",
        "test_win_rate",
        "test_max_drawdown",
        "test_max_loss",
        "test_risk_skip_count",
        "test_reduced_position_count",
        "test_multiple_delta_vs_baseline",
        "test_drawdown_delta_vs_baseline",
        "test_beats_baseline_multiple",
        "test_improves_baseline_drawdown",
    ]
    report = train_view[keep_train].merge(test_view[keep_test], on="rule_name", how="left", validate="one_to_one")
    report["passes_oos_both"] = report["test_beats_baseline_multiple"] & report["test_improves_baseline_drawdown"]
    return report


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(PROJECT_ROOT / args.config)
    execution_config = prepare_execution_config(config)
    all_rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    train_rows = slice_by_trade_date(all_rows, args.train_start, args.train_end)
    test_rows = slice_by_trade_date(all_rows, args.test_start, args.test_end)

    train_baseline = replay_baseline(train_rows, execution_config)
    risk_table = learn_risk_table(train_baseline)
    risk_lookup = risk_lookup_from_table(risk_table)
    train_summary, train_detail = replay_all_models(train_rows, execution_config, risk_lookup, "train")
    test_summary, test_detail = replay_all_models(test_rows, execution_config, risk_lookup, "test")
    wf_report = build_walk_forward_report(train_summary, test_summary)
    detail = pd.concat([train_detail, test_detail], ignore_index=True)
    yearly = build_yearly(detail)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    train_path = output_prefix.with_name(output_prefix.name + "_train_summary.csv")
    test_path = output_prefix.with_name(output_prefix.name + "_test_summary.csv")
    risk_table_path = output_prefix.with_name(output_prefix.name + "_risk_table.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    wf_report.to_csv(summary_path, index=False, encoding="utf-8-sig")
    train_summary.to_csv(train_path, index=False, encoding="utf-8-sig")
    test_summary.to_csv(test_path, index=False, encoding="utf-8-sig")
    risk_table.to_csv(risk_table_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")

    display_columns = [
        "train_rank",
        "rule_name",
        "train_equity_multiple",
        "train_max_drawdown",
        "train_trade_count",
        "test_equity_multiple",
        "test_max_drawdown",
        "test_trade_count",
        "test_risk_skip_count",
        "test_reduced_position_count",
        "passes_oos_both",
    ]
    print("bj_cap_2pct 风险评分模型 walk-forward 验证完成")
    print(wf_report[display_columns].to_string(index=False))
    print("训练期风险分桶：")
    print(risk_table[risk_table["risk_points"] > 0].head(30).to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- train_summary: {train_path}")
    print(f"- test_summary: {test_path}")
    print(f"- risk_table: {risk_table_path}")
    print(f"- detail: {detail_path}")
    print(f"- yearly: {yearly_path}")


if __name__ == "__main__":
    main()
