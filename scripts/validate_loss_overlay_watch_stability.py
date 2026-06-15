"""
LOSS_OVERLAY_WATCH 稳定性验证脚本。

文件作用：
1. 读取严格版可执行性审计明细。
2. 按全区间、年度区间和训练/测试区间统计 LOSS_OVERLAY_WATCH 的命中表现。
3. 对比保留、跳过、降仓三种处理方式的资金倍数、胜率和最大回撤。
4. 判断该标签是否具备升级为硬过滤或降仓规则的证据。

本脚本只处理本地 CSV，不接实盘，不调用 QMT，不下真实订单。
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

from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LOSS_OVERLAY_WATCH 稳定性验证。")
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
        default="reports/paper_trade/executability/a_clean_exclude_star_prev0_3_bj_loss_overlay_stability",
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


def to_bool_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(False, index=data.index)
    values = data[column]
    if values.dtype == bool:
        return values.fillna(False)
    return values.fillna(False).astype(str).str.lower().isin({"true", "1", "yes"})


def max_drawdown_from_returns(returns: pd.Series, initial_cash: float) -> float:
    equity = initial_cash
    values = [initial_cash]
    for account_return in pd.to_numeric(returns, errors="coerce").fillna(0.0):
        equity *= 1.0 + float(account_return)
        values.append(equity)
    curve = pd.Series(values)
    peak = curve.cummax()
    drawdown = curve / peak - 1.0
    return float(drawdown.min())


def compound_multiple(returns: pd.Series) -> float:
    if returns.empty:
        return 1.0
    return float((1.0 + pd.to_numeric(returns, errors="coerce").fillna(0.0)).prod())


def period_definitions(detail: pd.DataFrame) -> list[dict[str, str]]:
    min_date = str(detail["trade_date"].min())
    max_date = str(detail["trade_date"].max())
    return [
        {"period": "full", "start_date": min_date, "end_date": max_date, "role": "all"},
        {"period": "year_2024", "start_date": "20240101", "end_date": "20241231", "role": "year"},
        {"period": "year_2025", "start_date": "20250101", "end_date": "20251231", "role": "year"},
        {"period": "year_2026", "start_date": "20260101", "end_date": "20261231", "role": "year"},
        {"period": "train_2024", "start_date": "20240101", "end_date": "20241231", "role": "train"},
        {"period": "test_2025_2026", "start_date": "20250101", "end_date": "20261231", "role": "test"},
        {"period": "train_2024_2025", "start_date": "20240101", "end_date": "20251231", "role": "train"},
        {"period": "test_2026", "start_date": "20260101", "end_date": "20261231", "role": "test"},
    ]


def filter_period(detail: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    date = detail["trade_date"].astype(str)
    return detail[(date >= start_date) & (date <= end_date)].copy()


def describe_returns(prefix: str, returns: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    return {
        f"{prefix}_count": int(len(values)),
        f"{prefix}_win_count": int((values > 0).sum()),
        f"{prefix}_loss_count": int((values < 0).sum()),
        f"{prefix}_win_rate": float((values > 0).mean()) if len(values) else 0.0,
        f"{prefix}_avg_return": float(values.mean()) if len(values) else 0.0,
        f"{prefix}_median_return": float(values.median()) if len(values) else 0.0,
        f"{prefix}_max_profit": float(values.max()) if len(values) else 0.0,
        f"{prefix}_max_loss": float(values.min()) if len(values) else 0.0,
        f"{prefix}_compound_multiple": compound_multiple(values),
    }


def simulate_policy(
    period_data: pd.DataFrame,
    initial_cash: float,
    base_position_pct: float,
    policy: str,
    reduced_position_pct: float | None = None,
) -> dict[str, Any]:
    data = period_data.copy()
    returns = pd.to_numeric(data["dynamic_account_return"], errors="coerce").fillna(0.0)
    hit = to_bool_series(data, "issue_loss_overlay_watch")
    adjusted = returns.copy()
    if policy == "hard_exclude":
        adjusted.loc[hit] = 0.0
    elif policy == "reduce_position":
        if reduced_position_pct is None:
            raise ValueError("reduce_position 必须提供 reduced_position_pct。")
        scale = reduced_position_pct / base_position_pct if base_position_pct else 0.0
        adjusted.loc[hit] = adjusted.loc[hit] * scale
    elif policy != "baseline":
        raise ValueError(f"未知 policy: {policy}")

    traded = data[~(hit & (policy == "hard_exclude"))].copy()
    traded_returns = adjusted[~(hit & (policy == "hard_exclude"))].copy()
    return {
        "policy": policy,
        "reduced_position_pct": reduced_position_pct if reduced_position_pct is not None else "",
        "equity_multiple": compound_multiple(adjusted),
        "executed_trade_count": int(len(traded)),
        "skipped_trade_count": int((hit & (policy == "hard_exclude")).sum()),
        "loss_overlay_hit_count": int(hit.sum()),
        "win_rate": float((traded_returns > 0).mean()) if len(traded_returns) else 0.0,
        "avg_account_return": float(traded_returns.mean()) if len(traded_returns) else 0.0,
        "median_account_return": float(traded_returns.median()) if len(traded_returns) else 0.0,
        "max_loss": float(traded_returns.min()) if len(traded_returns) else 0.0,
        "max_drawdown": max_drawdown_from_returns(adjusted, initial_cash),
    }


def build_period_summary(detail: pd.DataFrame, initial_cash: float, base_position_pct: float) -> pd.DataFrame:
    rows = []
    for period in period_definitions(detail):
        period_data = filter_period(detail, period["start_date"], period["end_date"])
        if period_data.empty:
            continue
        returns = pd.to_numeric(period_data["dynamic_account_return"], errors="coerce").fillna(0.0)
        hit = to_bool_series(period_data, "issue_loss_overlay_watch")
        base = {
            **period,
            "actual_start_date": str(period_data["trade_date"].min()),
            "actual_end_date": str(period_data["trade_date"].max()),
            "trade_count": int(len(period_data)),
            "loss_overlay_hit_count": int(hit.sum()),
            "loss_overlay_hit_pct": float(hit.mean()) if len(hit) else 0.0,
        }
        base.update(describe_returns("hit", returns[hit]))
        base.update(describe_returns("non_hit", returns[~hit]))
        baseline = simulate_policy(period_data, initial_cash, base_position_pct, "baseline")
        hard_exclude = simulate_policy(period_data, initial_cash, base_position_pct, "hard_exclude")
        reduce_40 = simulate_policy(period_data, initial_cash, base_position_pct, "reduce_position", 0.4)
        reduce_20 = simulate_policy(period_data, initial_cash, base_position_pct, "reduce_position", 0.2)
        base.update(
            {
                "baseline_multiple": baseline["equity_multiple"],
                "baseline_max_drawdown": baseline["max_drawdown"],
                "hard_exclude_multiple": hard_exclude["equity_multiple"],
                "hard_exclude_improvement": hard_exclude["equity_multiple"] / baseline["equity_multiple"]
                if baseline["equity_multiple"]
                else 0.0,
                "hard_exclude_max_drawdown": hard_exclude["max_drawdown"],
                "reduce_40_multiple": reduce_40["equity_multiple"],
                "reduce_40_improvement": reduce_40["equity_multiple"] / baseline["equity_multiple"]
                if baseline["equity_multiple"]
                else 0.0,
                "reduce_20_multiple": reduce_20["equity_multiple"],
                "reduce_20_improvement": reduce_20["equity_multiple"] / baseline["equity_multiple"]
                if baseline["equity_multiple"]
                else 0.0,
            }
        )
        rows.append(base)
    return pd.DataFrame(rows)


def build_policy_summary(detail: pd.DataFrame, initial_cash: float, base_position_pct: float) -> pd.DataFrame:
    rows = []
    for period in period_definitions(detail):
        period_data = filter_period(detail, period["start_date"], period["end_date"])
        if period_data.empty:
            continue
        for policy, reduced_position_pct in [
            ("baseline", None),
            ("hard_exclude", None),
            ("reduce_position", 0.4),
            ("reduce_position", 0.2),
        ]:
            row = {**period, **simulate_policy(period_data, initial_cash, base_position_pct, policy, reduced_position_pct)}
            row["actual_start_date"] = str(period_data["trade_date"].min())
            row["actual_end_date"] = str(period_data["trade_date"].max())
            rows.append(row)
    return pd.DataFrame(rows)


def build_hit_trades(detail: pd.DataFrame) -> pd.DataFrame:
    hit = to_bool_series(detail, "issue_loss_overlay_watch")
    columns = [
        "trade_date",
        "ts_code",
        "name",
        "buy_trade_date",
        "exit_trade_date",
        "dynamic_account_return",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "issue_labels",
    ]
    columns = [column for column in columns if column in detail.columns]
    return detail.loc[hit, columns].copy().reset_index(drop=True)


def build_decision(summary: pd.DataFrame) -> pd.DataFrame:
    full = summary[summary["period"] == "full"].iloc[0] if not summary[summary["period"] == "full"].empty else None
    test = summary[summary["period"] == "test_2025_2026"].iloc[0] if not summary[summary["period"] == "test_2025_2026"].empty else None
    test_2026 = summary[summary["period"] == "test_2026"].iloc[0] if not summary[summary["period"] == "test_2026"].empty else None
    rows = []
    if full is not None:
        rows.append(
            {
                "check_item": "full_period_effect",
                "result": "PASS" if float(full["hard_exclude_improvement"]) > 1.0 else "FAIL",
                "evidence": (
                    f"全区间跳过 LOSS_OVERLAY_WATCH 后倍数改善 "
                    f"{float(full['hard_exclude_improvement']):.4f}，命中 {int(full['loss_overlay_hit_count'])} 笔。"
                ),
            }
        )
    if test is not None:
        rows.append(
            {
                "check_item": "test_2025_2026_effect",
                "result": "PASS" if float(test["hard_exclude_improvement"]) > 1.0 else "FAIL",
                "evidence": (
                    f"2025-2026 测试区间跳过后倍数改善 "
                    f"{float(test['hard_exclude_improvement']):.4f}，命中 {int(test['loss_overlay_hit_count'])} 笔。"
                ),
            }
        )
    if test_2026 is not None:
        rows.append(
            {
                "check_item": "test_2026_sample_size",
                "result": "WARN" if int(test_2026["loss_overlay_hit_count"]) < 3 else "PASS",
                "evidence": f"2026 测试区间 LOSS_OVERLAY_WATCH 仅命中 {int(test_2026['loss_overlay_hit_count'])} 笔。",
            }
        )
    rows.append(
        {
            "check_item": "final_decision",
            "result": "WATCH_ONLY",
            "evidence": (
                "当前证据支持继续保留为强复核/降仓候选规则；由于命中样本少，暂不建议直接升级为正式硬过滤。"
            ),
        }
    )
    return pd.DataFrame(rows)


def write_markdown(
    path: Path,
    period_summary: pd.DataFrame,
    policy_summary: pd.DataFrame,
    hit_trades: pd.DataFrame,
    decision: pd.DataFrame,
) -> None:
    period_columns = [
        "period",
        "trade_count",
        "loss_overlay_hit_count",
        "hit_win_rate",
        "hit_avg_return",
        "hit_max_loss",
        "non_hit_win_rate",
        "non_hit_avg_return",
        "baseline_multiple",
        "hard_exclude_multiple",
        "hard_exclude_improvement",
        "baseline_max_drawdown",
        "hard_exclude_max_drawdown",
    ]
    period_columns = [column for column in period_columns if column in period_summary.columns]
    policy_columns = [
        "period",
        "policy",
        "reduced_position_pct",
        "equity_multiple",
        "executed_trade_count",
        "skipped_trade_count",
        "win_rate",
        "max_drawdown",
    ]
    policy_columns = [column for column in policy_columns if column in policy_summary.columns]
    content = f"""# LOSS_OVERLAY_WATCH 稳定性验证

本报告只使用本地严格版可执行性审计明细，不接实盘，不调用 QMT，不下真实订单。

## 决策检查

{decision.to_markdown(index=False)}

## 分区间统计

{period_summary[period_columns].to_markdown(index=False) if not period_summary.empty else "无分区间统计。"}

## 处理方式对比

{policy_summary[policy_columns].to_markdown(index=False) if not policy_summary.empty else "无处理方式统计。"}

## 命中交易

{hit_trades.to_markdown(index=False) if not hit_trades.empty else "无命中交易。"}

## 解释限制

- 命中样本只有少数几笔，不能因为全区间收益改善就直接写成正式硬过滤。
- 当前建议是继续作为强复核标签；若模拟盘后续继续验证稳定，再考虑升级为硬过滤或降仓。
- 本报告不代表可以实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_json_config(args.strategy_config)
    initial_cash = float(config.get("position", {}).get("initial_cash", 500000))
    base_position_pct = float(config.get("position", {}).get("target_position_pct", 0.8))
    detail_path = resolve_path(args.executability_detail)
    detail = pd.read_csv(detail_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    detail["trade_date"] = detail["trade_date"].map(normalize_date)
    detail = detail.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    period_summary = build_period_summary(detail, initial_cash, base_position_pct)
    policy_summary = build_policy_summary(detail, initial_cash, base_position_pct)
    hit_trades = build_hit_trades(detail)
    decision = build_decision(period_summary)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    period_path = output_prefix.with_name(output_prefix.name + "_period_summary.csv")
    policy_path = output_prefix.with_name(output_prefix.name + "_policy_summary.csv")
    hit_path = output_prefix.with_name(output_prefix.name + "_hit_trades.csv")
    decision_path = output_prefix.with_name(output_prefix.name + "_decision.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    period_summary.to_csv(period_path, index=False, encoding="utf-8-sig")
    policy_summary.to_csv(policy_path, index=False, encoding="utf-8-sig")
    hit_trades.to_csv(hit_path, index=False, encoding="utf-8-sig")
    decision.to_csv(decision_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, period_summary, policy_summary, hit_trades, decision)

    print("LOSS_OVERLAY_WATCH 稳定性验证完成：")
    print(f"- period_summary: {period_path}")
    print(f"- policy_summary: {policy_path}")
    print(f"- hit_trades: {hit_path}")
    print(f"- decision: {decision_path}")
    print(f"- markdown: {markdown_path}")
    print(decision.to_string(index=False))


if __name__ == "__main__":
    main()
