"""
ST 仓位压力测试。

文件作用：
1. 读取最近 2 年最佳策略审计逐笔交易文件。
2. 保持原始选股、买卖价格、动态滑点和非 ST 仓位不变。
3. 仅调整 ST 交易的最大仓位，测试 ST 仓位 0% / 10% / 20% / 30% / 50% / 80%。
4. 输出每个仓位场景下的复利、回撤、胜率、年度收益和逐笔明细。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ST 仓位压力测试。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_keep_st_exclude_amount_ratio_top500_best_audit_trades.csv",
        help="最佳策略审计逐笔交易文件。",
    )
    parser.add_argument("--initial-cash", type=float, default=500000.0, help="初始资金。")
    parser.add_argument(
        "--st-position-pcts",
        default="0,0.1,0.2,0.3,0.5,0.8",
        help="ST 最大仓位列表，逗号分隔。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_st_position_sizing_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


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


def parse_position_pcts(text: str) -> list[float]:
    result = []
    for part in text.split(","):
        value = part.strip()
        if not value:
            continue
        pct = float(value)
        if pct < 0 or pct > 1:
            raise ValueError(f"ST 仓位必须在 0-1 之间: {pct}")
        result.append(pct)
    if not result:
        raise ValueError("--st-position-pcts 不能为空")
    return result


def load_executed_trades(path: Path) -> pd.DataFrame:
    trades = pd.read_csv(path, low_memory=False)
    if "scenario_executed" in trades.columns:
        trades = trades[trades["scenario_executed"] == True].copy()  # noqa: E712
    required = {
        "name",
        "dynamic_net_return",
        "actual_position_pct",
        "exit_trade_date",
    }
    missing = sorted(required - set(trades.columns))
    if missing:
        raise RuntimeError(f"逐笔文件缺少必要字段: {missing}")
    trades["exit_trade_date"] = trades["exit_trade_date"].map(normalize_date)
    trades["is_st_trade"] = detect_st(trades)
    trades["dynamic_net_return"] = pd.to_numeric(trades["dynamic_net_return"], errors="coerce").fillna(0.0)
    trades["actual_position_pct"] = pd.to_numeric(trades["actual_position_pct"], errors="coerce").fillna(0.0)
    return trades.reset_index(drop=True)


def detect_st(trades: pd.DataFrame) -> pd.Series:
    name = trades.get("name", pd.Series("", index=trades.index)).fillna("").astype(str).str.upper()
    is_st_flag = trades.get("is_st", pd.Series(False, index=trades.index)).astype(str).str.lower().isin({"true", "1"})
    return is_st_flag | name.str.contains("ST", na=False) | name.str.contains("退", na=False)


def simulate(trades: pd.DataFrame, initial_cash: float, st_position_pct: float) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = initial_cash
    rows = []
    for index, row in trades.iterrows():
        original_position_pct = float(row["actual_position_pct"])
        position_pct = min(original_position_pct, st_position_pct) if bool(row["is_st_trade"]) else original_position_pct
        account_return = float(row["dynamic_net_return"]) * position_pct
        equity_before = equity
        equity = equity * (1.0 + account_return)
        item = row.to_dict()
        item.update(
            {
                "stress_st_position_pct": st_position_pct,
                "stress_trade_order": index + 1,
                "stress_equity_before": equity_before,
                "stress_position_pct": position_pct,
                "stress_account_return": account_return,
                "stress_equity_after": equity,
            }
        )
        rows.append(item)

    detail = pd.DataFrame(rows)
    returns = detail["stress_account_return"].astype(float)
    st_returns = detail.loc[detail["is_st_trade"], "stress_account_return"].astype(float)
    clean_returns = detail.loc[~detail["is_st_trade"], "stress_account_return"].astype(float)
    summary = {
        "st_position_pct": st_position_pct,
        "initial_cash": initial_cash,
        "final_equity": equity,
        "equity_multiple": equity / initial_cash if initial_cash else 0.0,
        "executed_trade_count": int(len(detail)),
        "st_trade_count": int(detail["is_st_trade"].sum()),
        "non_st_trade_count": int((~detail["is_st_trade"]).sum()),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(detail["stress_equity_after"]),
        "max_consecutive_losses": max_consecutive_losses(returns),
        "st_compound_multiple": float((1.0 + st_returns).prod()) if len(st_returns) else 0.0,
        "non_st_compound_multiple": float((1.0 + clean_returns).prod()) if len(clean_returns) else 0.0,
    }
    return summary, detail


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if detail.empty:
        return pd.DataFrame()
    data = detail.copy()
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    st_position_pct = float(data["stress_st_position_pct"].iloc[0])
    for year, group in data.groupby("year"):
        returns = group["stress_account_return"].astype(float)
        rows.append(
            {
                "st_position_pct": st_position_pct,
                "year": year,
                "trade_count": int(len(group)),
                "st_trade_count": int(group["is_st_trade"].sum()),
                "first_equity": float(group["stress_equity_before"].iloc[0]),
                "last_equity": float(group["stress_equity_after"].iloc[-1]),
                "period_return": float(group["stress_equity_after"].iloc[-1] / group["stress_equity_before"].iloc[0] - 1.0),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["stress_equity_after"]),
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "max_consecutive_losses": max_consecutive_losses(returns),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    input_path = PROJECT_ROOT / args.input
    output_prefix = PROJECT_ROOT / args.output_prefix
    trades = load_executed_trades(input_path)
    position_pcts = parse_position_pcts(args.st_position_pcts)

    summary_rows = []
    detail_frames = []
    yearly_frames = []
    for pct in position_pcts:
        summary, detail = simulate(trades, initial_cash=args.initial_cash, st_position_pct=pct)
        summary_rows.append(summary)
        detail_frames.append(detail)
        yearly_frames.append(build_yearly(detail))

    summary_df = pd.DataFrame(summary_rows).sort_values("st_position_pct")
    detail_df = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    yearly_df = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()

    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_df.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")

    print("ST 仓位压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- detail: {detail_path}")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
