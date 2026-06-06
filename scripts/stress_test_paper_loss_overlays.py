"""
模拟盘大亏交易风控压力测试。

文件作用：
1. 读取当前策略审计逐笔交易文件。
2. 从大亏交易中提取可观察因子桶，自动生成单因子、双因子风控规则。
3. 分别测试硬过滤、降仓到 40%、降仓到 20% 对复利、回撤、胜率和样本数的影响。
4. 输出规则压力测试报告，供后续决定是否把规则写入策略配置。

本脚本只读取本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


FACTOR_COLUMNS = [
    "market_segment",
    "fd_ratio_bucket",
    "volume_ratio_bucket",
    "amount_ratio_bucket",
    "first_time_detail_bucket",
    "prev_pct_chg_bucket",
    "market_limit_down_count_bucket",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
    "turnover_rate_bucket",
    "open_times_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模拟盘大亏交易风控压力测试。")
    parser.add_argument(
        "--input",
        default="reports/a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv",
        help="当前策略审计逐笔交易文件。",
    )
    parser.add_argument("--initial-cash", type=float, default=500000.0, help="初始资金。")
    parser.add_argument("--base-position-pct", type=float, default=0.8, help="基准仓位。")
    parser.add_argument("--loss-threshold", type=float, default=-0.08, help="大亏账户收益阈值。")
    parser.add_argument("--max-rule-size", type=int, default=2, help="自动生成规则最多组合几个因子。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_loss_overlay_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.astype(float):
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def load_trades(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"逐笔交易文件不存在: {path}")
    trades = pd.read_csv(path, low_memory=False)
    if "scenario_executed" in trades.columns:
        trades = trades[trades["scenario_executed"].astype(str).str.lower().isin({"true", "1"})].copy()
    required = {"trade_order", "ts_code", "name", "dynamic_net_return", "dynamic_account_return"}
    missing = sorted(required - set(trades.columns))
    if missing:
        raise RuntimeError(f"逐笔交易文件缺少字段: {missing}")
    trades["trade_order"] = pd.to_numeric(trades["trade_order"], errors="coerce").fillna(0).astype(int)
    trades["dynamic_net_return"] = pd.to_numeric(trades["dynamic_net_return"], errors="coerce").fillna(0.0)
    trades["dynamic_account_return"] = pd.to_numeric(trades["dynamic_account_return"], errors="coerce").fillna(0.0)
    if "exit_trade_date" in trades.columns:
        trades["exit_trade_date"] = trades["exit_trade_date"].map(normalize_date)
    for column in FACTOR_COLUMNS:
        if column in trades.columns:
            trades[column] = trades[column].fillna("missing").astype(str)
    return trades.sort_values(["trade_order", "trade_date", "ts_code"]).reset_index(drop=True)


def rule_name(rule: tuple[tuple[str, str], ...]) -> str:
    return ";".join(f"{column}={value}" for column, value in rule)


def rule_set_name(rule_set: tuple[tuple[tuple[str, str], ...], ...] | None) -> str:
    if not rule_set:
        return "baseline"
    return " OR ".join(rule_name(rule) for rule in rule_set)


def rule_mask(data: pd.DataFrame, rule: tuple[tuple[str, str], ...]) -> pd.Series:
    mask = pd.Series(True, index=data.index)
    for column, value in rule:
        if column not in data.columns:
            return pd.Series(False, index=data.index)
        mask &= data[column].fillna("missing").astype(str) == str(value)
    return mask


def rule_set_mask(data: pd.DataFrame, rule_set: tuple[tuple[tuple[str, str], ...], ...] | None) -> pd.Series:
    if not rule_set:
        return pd.Series(False, index=data.index)
    mask = pd.Series(False, index=data.index)
    for rule in rule_set:
        mask |= rule_mask(data, rule)
    return mask


def build_loss_derived_rules(trades: pd.DataFrame, loss_threshold: float, max_rule_size: int) -> list[tuple[tuple[str, str], ...]]:
    losses = trades[trades["dynamic_account_return"].astype(float) <= loss_threshold].copy()
    rules: set[tuple[tuple[str, str], ...]] = set()
    factor_columns = [column for column in FACTOR_COLUMNS if column in trades.columns]
    for _, row in losses.iterrows():
        values = [(column, str(row[column])) for column in factor_columns if str(row[column]) not in {"missing", "nan"}]
        for size in range(1, max_rule_size + 1):
            for combo in combinations(values, size):
                rules.add(tuple(sorted(combo)))
    return sorted(rules, key=lambda item: (len(item), rule_name(item)))


def simulate(
    trades: pd.DataFrame,
    initial_cash: float,
    base_position_pct: float,
    rule_set: tuple[tuple[tuple[str, str], ...], ...] | None,
    action: str,
    reduced_position_pct: float | None = None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = initial_cash
    rows: list[dict[str, Any]] = []
    hit_mask = rule_set_mask(trades, rule_set)
    for index, row in trades.iterrows():
        is_hit = bool(hit_mask.loc[index])
        position_pct = base_position_pct
        executed = True
        skip_reason = ""
        if is_hit and action == "hard_exclude":
            executed = False
            position_pct = 0.0
            skip_reason = "risk_overlay_hard_exclude"
        elif is_hit and action == "reduce_position":
            position_pct = min(base_position_pct, float(reduced_position_pct or base_position_pct))

        account_return = float(row["dynamic_net_return"]) * position_pct if executed else 0.0
        equity_before = equity
        equity = equity * (1.0 + account_return)
        item = row.to_dict()
        item.update(
            {
                "overlay_rule": rule_set_name(rule_set),
                "overlay_action": action,
                "overlay_reduced_position_pct": reduced_position_pct if reduced_position_pct is not None else "",
                "overlay_rule_hit": is_hit,
                "overlay_executed": executed,
                "overlay_skip_reason": skip_reason,
                "overlay_position_pct": position_pct,
                "overlay_account_return": account_return,
                "overlay_equity_before": equity_before,
                "overlay_equity_after": equity,
            }
        )
        rows.append(item)

    detail = pd.DataFrame(rows)
    executed_detail = detail[detail["overlay_executed"] == True].copy()  # noqa: E712
    returns = executed_detail["overlay_account_return"].astype(float)
    hit_trades = detail[detail["overlay_rule_hit"]].copy()
    hit_returns = hit_trades["dynamic_account_return"].astype(float) if not hit_trades.empty else pd.Series(dtype=float)
    summary = {
        "overlay_rule": rule_set_name(rule_set),
        "overlay_action": action,
        "overlay_reduced_position_pct": reduced_position_pct if reduced_position_pct is not None else "",
        "rule_factor_count": sum(len(rule) for rule in rule_set) if rule_set else 0,
        "rule_set_count": len(rule_set) if rule_set else 0,
        "initial_cash": initial_cash,
        "final_equity": equity,
        "equity_multiple": equity / initial_cash if initial_cash else 0.0,
        "executed_trade_count": int(len(executed_detail)),
        "skipped_trade_count": int((detail["overlay_executed"] == False).sum()),  # noqa: E712
        "rule_hit_count": int(len(hit_trades)),
        "rule_hit_loss_count": int((hit_returns <= -0.08).sum()) if len(hit_returns) else 0,
        "rule_hit_win_rate_original": float((hit_returns > 0).mean()) if len(hit_returns) else 0.0,
        "rule_hit_avg_original_return": float(hit_returns.mean()) if len(hit_returns) else 0.0,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(detail["overlay_equity_after"]),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }
    return summary, detail


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    data = detail[detail["overlay_executed"] == True].copy()  # noqa: E712
    if data.empty:
        return pd.DataFrame()
    data["year"] = data.get("exit_trade_date", pd.Series("", index=data.index)).astype(str).str[:4]
    rows = []
    for year, group in data.groupby("year"):
        returns = group["overlay_account_return"].astype(float)
        first_equity = float(group["overlay_equity_before"].iloc[0])
        last_equity = float(group["overlay_equity_after"].iloc[-1])
        rows.append(
            {
                "overlay_rule": str(group["overlay_rule"].iloc[0]),
                "overlay_action": str(group["overlay_action"].iloc[0]),
                "overlay_reduced_position_pct": group["overlay_reduced_position_pct"].iloc[0],
                "year": year,
                "trade_count": int(len(group)),
                "period_return": last_equity / first_equity - 1.0 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["overlay_equity_after"]),
            }
        )
    return pd.DataFrame(rows)


def run_all(trades: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rules = build_loss_derived_rules(
        trades=trades,
        loss_threshold=args.loss_threshold,
        max_rule_size=args.max_rule_size,
    )
    summary_rows = []
    detail_frames = []
    yearly_frames = []

    baseline_summary, baseline_detail = simulate(
        trades=trades,
        initial_cash=args.initial_cash,
        base_position_pct=args.base_position_pct,
        rule_set=None,
        action="baseline",
    )
    summary_rows.append(baseline_summary)
    detail_frames.append(baseline_detail)
    yearly_frames.append(build_yearly(baseline_detail))

    for rule in rules:
        hard_summary, hard_detail = simulate(
            trades=trades,
            initial_cash=args.initial_cash,
            base_position_pct=args.base_position_pct,
            rule_set=(rule,),
            action="hard_exclude",
        )
        summary_rows.append(hard_summary)
        detail_frames.append(hard_detail)
        yearly_frames.append(build_yearly(hard_detail))
        for reduced_position_pct in [0.4, 0.2]:
            reduce_summary, reduce_detail = simulate(
                trades=trades,
                initial_cash=args.initial_cash,
                base_position_pct=args.base_position_pct,
                rule_set=(rule,),
                action="reduce_position",
                reduced_position_pct=reduced_position_pct,
            )
            summary_rows.append(reduce_summary)
            detail_frames.append(reduce_detail)
            yearly_frames.append(build_yearly(reduce_detail))

    individual_summary = pd.DataFrame(summary_rows)
    candidate_rules = []
    for rule in rules:
        mask = rule_mask(trades, rule)
        hit = trades[mask].copy()
        if hit.empty:
            continue
        loss_hit_count = int((hit["dynamic_account_return"].astype(float) <= args.loss_threshold).sum())
        if loss_hit_count >= 1 and len(hit) <= 8:
            candidate_rules.append(rule)

    combined_seen: set[str] = set()
    for first, second in combinations(candidate_rules, 2):
        rule_set = tuple(sorted((first, second), key=rule_name))
        name = rule_set_name(rule_set)
        if name in combined_seen:
            continue
        combined_seen.add(name)
        mask = rule_set_mask(trades, rule_set)
        hit = trades[mask].copy()
        loss_hit_count = int((hit["dynamic_account_return"].astype(float) <= args.loss_threshold).sum()) if not hit.empty else 0
        if loss_hit_count < 2 or len(hit) > 12:
            continue
        for action, reduced_position_pct in [
            ("hard_exclude", None),
            ("reduce_position", 0.4),
            ("reduce_position", 0.2),
        ]:
            summary_item, detail_item = simulate(
                trades=trades,
                initial_cash=args.initial_cash,
                base_position_pct=args.base_position_pct,
                rule_set=rule_set,
                action=action,
                reduced_position_pct=reduced_position_pct,
            )
            summary_rows.append(summary_item)
            detail_frames.append(detail_item)
            yearly_frames.append(build_yearly(detail_item))

    summary = pd.DataFrame(summary_rows).sort_values(
        ["equity_multiple", "max_drawdown", "executed_trade_count"],
        ascending=[False, False, False],
    )
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    yearly = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()
    return summary, detail, yearly


def write_markdown(path: Path, summary: pd.DataFrame) -> None:
    columns = [
        "overlay_rule",
        "overlay_action",
        "overlay_reduced_position_pct",
        "equity_multiple",
        "executed_trade_count",
        "skipped_trade_count",
        "rule_hit_count",
        "rule_hit_loss_count",
        "win_rate",
        "max_drawdown",
        "max_loss",
    ]
    columns = [column for column in columns if column in summary.columns]
    baseline = summary[summary["overlay_rule"] == "baseline"]
    top = summary[summary["overlay_rule"] != "baseline"].head(30)
    content = f"""# 模拟盘大亏风控压力测试

本报告只读取本地审计逐笔交易，测试大亏来源因子对应的硬过滤和降仓影响。不接实盘，不调用 QMT，不下真实订单。

## 基准

{baseline[columns].to_markdown(index=False) if not baseline.empty else "无基准。"}

## 排名前 30 风控覆盖场景

{top[columns].to_markdown(index=False) if not top.empty else "无风控场景。"}

## 口径限制

硬过滤场景只是跳过命中交易，不会自动补买当天因持仓释放后可能出现的其他候选；降仓场景按原单笔收益率线性缩放账户收益。该报告用于压力测试，不代表可以直接实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    trades = load_trades(PROJECT_ROOT / args.input)
    summary, detail, yearly = run_all(trades, args)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary)

    print("模拟盘大亏风控压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- markdown: {markdown_path}")
    show_columns = [
        "overlay_rule",
        "overlay_action",
        "overlay_reduced_position_pct",
        "equity_multiple",
        "executed_trade_count",
        "skipped_trade_count",
        "rule_hit_count",
        "rule_hit_loss_count",
        "win_rate",
        "max_drawdown",
        "max_loss",
    ]
    print(summary[show_columns].head(20).to_string(index=False))


if __name__ == "__main__":
    main()
