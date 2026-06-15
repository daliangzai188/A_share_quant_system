"""
严格版可执行性复核项压力测试。

文件作用：
1. 读取严格版可执行性审计明细。
2. 分别测试跳过 LOSS_OVERLAY_WATCH、高滑点、成交额占比异常、所有复核项后的资金影响。
3. 输出不同复核过滤方案的资金倍数、胜率、最大回撤和被跳过交易清单。
4. 全程只处理本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config


RejectRule = Callable[[pd.DataFrame], pd.Series]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="严格版可执行性复核项压力测试。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument(
        "--executability-detail",
        default=(
            "reports/paper_trade/executability/"
            "a_clean_exclude_star_prev0_3_bj_executability_20240520_20260417_detail.csv"
        ),
        help="严格版可执行性审计明细。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/executability/a_clean_exclude_star_prev0_3_bj_executability_filter_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def to_bool_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(False, index=data.index)
    values = data[column]
    if values.dtype == bool:
        return values.fillna(False)
    return values.fillna(False).astype(str).str.lower().isin({"true", "1", "yes"})


def max_drawdown(equity: pd.Series, initial_cash: float) -> float:
    if equity.empty:
        return 0.0
    curve = pd.concat([pd.Series([initial_cash]), pd.to_numeric(equity, errors="coerce")], ignore_index=True)
    curve = curve.ffill().fillna(initial_cash)
    peak = curve.cummax()
    drawdown = curve / peak - 1.0
    return float(drawdown.min())


def max_consecutive_losses(returns: pd.Series) -> int:
    max_losses = 0
    current = 0
    for value in returns:
        if value < 0:
            current += 1
            max_losses = max(max_losses, current)
        else:
            current = 0
    return max_losses


def scenario_rules() -> list[tuple[str, str, RejectRule]]:
    return [
        (
            "baseline_keep_all",
            "保留全部已成交交易。",
            lambda data: pd.Series(False, index=data.index),
        ),
        (
            "reject_loss_overlay_watch",
            "跳过命中 LOSS_OVERLAY_WATCH 的交易。",
            lambda data: to_bool_series(data, "issue_loss_overlay_watch"),
        ),
        (
            "reject_slippage_warnings",
            "跳过买入或卖出滑点超过阈值的交易。",
            lambda data: to_bool_series(data, "issue_high_buy_slippage")
            | to_bool_series(data, "issue_high_sell_slippage"),
        ),
        (
            "reject_amount_ratio_warnings",
            "跳过买入或卖出成交额占比超过阈值的交易。",
            lambda data: to_bool_series(data, "issue_high_buy_amount_ratio")
            | to_bool_series(data, "issue_high_sell_amount_ratio"),
        ),
        (
            "reject_loss_overlay_or_slippage",
            "跳过 LOSS_OVERLAY_WATCH 或高滑点交易。",
            lambda data: to_bool_series(data, "issue_loss_overlay_watch")
            | to_bool_series(data, "issue_high_buy_slippage")
            | to_bool_series(data, "issue_high_sell_slippage"),
        ),
        (
            "reject_any_executability_review",
            "跳过所有可执行性审计 REVIEW 交易。",
            lambda data: data.get("executability_status", pd.Series("", index=data.index)).astype(str) == "REVIEW",
        ),
        (
            "diagnostic_reject_negative_review_only",
            "诊断项：只跳过复核项中的亏损交易。该规则使用事后收益，只能诊断，不能直接实盘。",
            lambda data: (
                (data.get("executability_status", pd.Series("", index=data.index)).astype(str) == "REVIEW")
                & (pd.to_numeric(data.get("dynamic_account_return", pd.Series(0.0, index=data.index)), errors="coerce") < 0)
            ),
        ),
    ]


def simulate_scenario(
    name: str,
    description: str,
    detail: pd.DataFrame,
    initial_cash: float,
    reject_mask: pd.Series,
) -> tuple[dict[str, object], pd.DataFrame]:
    data = detail.copy()
    data["scenario"] = name
    data["scenario_description"] = description
    data["rejected_by_scenario"] = reject_mask.reindex(data.index).fillna(False).astype(bool)
    data["scenario_account_return"] = pd.to_numeric(data["dynamic_account_return"], errors="coerce").fillna(0.0)
    data.loc[data["rejected_by_scenario"], "scenario_account_return"] = 0.0
    data["scenario_trade_status"] = data["rejected_by_scenario"].map(
        lambda rejected: "SCENARIO_REJECTED_SKIP" if rejected else "SCENARIO_KEPT"
    )

    equity = initial_cash
    equity_values = []
    for account_return in data["scenario_account_return"]:
        equity *= 1.0 + float(account_return)
        equity_values.append(equity)
    data["scenario_equity_after"] = equity_values
    data["scenario_peak_equity"] = data["scenario_equity_after"].cummax().clip(lower=initial_cash)
    data["scenario_drawdown"] = data["scenario_equity_after"] / data["scenario_peak_equity"] - 1.0

    kept = data[~data["rejected_by_scenario"]].copy()
    rejected = data[data["rejected_by_scenario"]].copy()
    returns = pd.to_numeric(kept["dynamic_account_return"], errors="coerce").fillna(0.0)
    rejected_returns = pd.to_numeric(rejected["dynamic_account_return"], errors="coerce").fillna(0.0)
    gross_profit = float(returns[returns > 0].sum()) if len(returns) else 0.0
    gross_loss = abs(float(returns[returns < 0].sum())) if len(returns) else 0.0
    final_equity = float(data["scenario_equity_after"].iloc[-1]) if not data.empty else initial_cash
    summary = {
        "scenario": name,
        "description": description,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_cash if initial_cash else 0.0,
        "total_trade_count": int(len(data)),
        "kept_trade_count": int(len(kept)),
        "rejected_trade_count": int(len(rejected)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": gross_profit / gross_loss if gross_loss else 0.0,
        "max_drawdown": max_drawdown(data["scenario_equity_after"], initial_cash),
        "max_consecutive_losses": max_consecutive_losses(returns),
        "rejected_avg_account_return": float(rejected_returns.mean()) if len(rejected_returns) else 0.0,
        "rejected_total_account_return": float(rejected_returns.sum()) if len(rejected_returns) else 0.0,
        "live_order_enabled": False,
    }
    return summary, data


def build_minute_validation_targets(review_detail: pd.DataFrame) -> pd.DataFrame:
    if review_detail.empty:
        return pd.DataFrame(
            columns=[
                "trade_date",
                "ts_code",
                "name",
                "buy_trade_date",
                "exit_trade_date",
                "issue_labels",
                "validation_focus",
            ]
        )
    result = review_detail[
        [
            column
            for column in [
                "trade_date",
                "ts_code",
                "name",
                "buy_trade_date",
                "exit_trade_date",
                "issue_labels",
                "dynamic_account_return",
                "buy_amount_ratio",
                "sell_amount_ratio",
                "dynamic_buy_slippage_rate",
                "dynamic_sell_slippage_rate",
            ]
            if column in review_detail.columns
        ]
    ].copy()
    result["validation_focus"] = result["issue_labels"].fillna("").astype(str).map(resolve_validation_focus)
    return result


def resolve_validation_focus(issue_labels: str) -> str:
    labels = set(filter(None, str(issue_labels).split(";")))
    focus = []
    if "loss_overlay_watch" in labels:
        focus.append("复核是否应升级为硬过滤")
    if "high_buy_slippage" in labels:
        focus.append("验证T+1开盘买入盘口冲击")
    if "high_sell_slippage" in labels:
        focus.append("验证T+2卖出盘口冲击")
    if "high_sell_amount_ratio" in labels or "high_buy_amount_ratio" in labels:
        focus.append("验证计划金额相对成交额是否过大")
    return "；".join(focus) if focus else "常规复核"


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    rejected_detail: pd.DataFrame,
    minute_targets: pd.DataFrame,
) -> None:
    summary_columns = [
        "scenario",
        "equity_multiple",
        "kept_trade_count",
        "rejected_trade_count",
        "win_rate",
        "avg_account_return",
        "max_loss",
        "max_drawdown",
        "rejected_avg_account_return",
    ]
    summary_columns = [column for column in summary_columns if column in summary.columns]
    rejected_columns = [
        "scenario",
        "trade_date",
        "ts_code",
        "name",
        "dynamic_account_return",
        "issue_labels",
    ]
    rejected_columns = [column for column in rejected_columns if column in rejected_detail.columns]
    target_columns = [
        "trade_date",
        "ts_code",
        "name",
        "buy_trade_date",
        "exit_trade_date",
        "issue_labels",
        "validation_focus",
    ]
    target_columns = [column for column in target_columns if column in minute_targets.columns]
    content = f"""# 严格版可执行性复核项压力测试

本报告只使用本地可执行性审计明细，不接实盘，不调用 QMT，不下真实订单。

## 方案汇总

{summary[summary_columns].to_markdown(index=False)}

## 被跳过交易明细

{rejected_detail[rejected_columns].to_markdown(index=False) if not rejected_detail.empty else "无被跳过交易。"}

## 分钟 K / 盘口验证目标

{minute_targets[target_columns].to_markdown(index=False) if not minute_targets.empty else "无验证目标。"}

## 解释限制

- `diagnostic_reject_negative_review_only` 使用了事后盈亏，只能用于理解风险来源，不能直接写入策略。
- 其他方案也只是基于日线审计风险标签的压力测试，是否升级为正式硬过滤还要做样本外和模拟盘验证。
- 当前结果不代表可以实盘，后续仍需分钟 K、集合竞价和盘口五档验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_json_config(args.strategy_config)
    detail_path = resolve_path(args.executability_detail)
    detail = pd.read_csv(detail_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    detail = detail.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    initial_cash = float(config.get("position", {}).get("initial_cash", 500000))

    summaries = []
    scenario_details = []
    for scenario_name, description, rule in scenario_rules():
        reject_mask = rule(detail)
        summary, scenario_detail = simulate_scenario(
            name=scenario_name,
            description=description,
            detail=detail,
            initial_cash=initial_cash,
            reject_mask=reject_mask,
        )
        summaries.append(summary)
        scenario_details.append(scenario_detail)

    summary_df = pd.DataFrame(summaries).sort_values("equity_multiple", ascending=False).reset_index(drop=True)
    detail_df = pd.concat(scenario_details, ignore_index=True) if scenario_details else pd.DataFrame()
    rejected_detail = detail_df[detail_df["rejected_by_scenario"]].copy() if not detail_df.empty else pd.DataFrame()
    review_detail = detail[detail.get("executability_status", pd.Series("", index=detail.index)).astype(str) == "REVIEW"].copy()
    minute_targets = build_minute_validation_targets(review_detail)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    detail_path_out = output_prefix.with_name(output_prefix.name + "_detail.csv")
    rejected_path = output_prefix.with_name(output_prefix.name + "_rejected_trades.csv")
    minute_targets_path = output_prefix.with_name(output_prefix.name + "_minute_targets.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail_df.to_csv(detail_path_out, index=False, encoding="utf-8-sig")
    rejected_detail.to_csv(rejected_path, index=False, encoding="utf-8-sig")
    minute_targets.to_csv(minute_targets_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary_df, rejected_detail, minute_targets)

    print("严格版可执行性复核项压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path_out}")
    print(f"- rejected_trades: {rejected_path}")
    print(f"- minute_targets: {minute_targets_path}")
    print(f"- markdown: {markdown_path}")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
