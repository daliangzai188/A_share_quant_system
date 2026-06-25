"""
策略 L 龙头策略实盘可执行性模拟认证。

只做离线认证，不接实盘，不修改 ABC/E2/D。

认证重点：
  - T 日收盘信号，T+1 买入，按原 L 研究明细的退出规则卖出。
  - 同一资金不能重叠持仓，上一笔未卖出前跳过新信号。
  - 买入日涨停开盘按保守口径判定买不到。
  - 卖出日跌停按保守口径判定卖不出，顺延到 D5 内第一个可卖日。
  - 重新扣买卖费用，不直接沿用理论收益。

输出：
  reports/strategy_l/live_certification/l_live_cert_summary.csv
  reports/strategy_l/live_certification/l_live_cert_detail.csv
  reports/strategy_l/live_certification/l_live_cert_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
INPUT_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_strategy_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l" / "live_certification"
INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.8
BUY_SLIPPAGE_RATE = 0.001
SELL_SLIPPAGE_RATE = 0.001
LIMIT_PRICE_TOLERANCE = 0.002
COMMISSION_RATE = 0.0003
TRANSFER_FEE_RATE = 0.00001
STAMP_TAX_RATE = 0.001
MAX_FORWARD_DAYS = 5


@dataclass(frozen=True)
class LVariant:
    name: str
    description: str
    filter_expr: str


VARIANTS = [
    LVariant(
        name="L1",
        description="排除沪主板+retreat_2day+theme_limit_count=30",
        filter_expr="market_segment!=sh_main AND segment_retreat_state_bucket!=retreat_2day AND theme_limit_count!=30.0",
    ),
    LVariant(
        name="L2",
        description="排除retreat_2day+跌停3_8+theme_limit_count=30",
        filter_expr="segment_retreat_state_bucket!=retreat_2day AND segment_limit_down_count_bucket!=3_8 AND theme_limit_count!=30.0",
    ),
    LVariant(
        name="L3",
        description="排除半导体+沪主板+retreat_2day",
        filter_expr="theme_name!=半导体 AND market_segment!=sh_main AND segment_retreat_state_bucket!=retreat_2day",
    ),
]


def to_numeric(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.fillna(0.0):
        if float(value) < 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def limit_pct(ts_code: str, name: object | None = None) -> float:
    stock_name = "" if name is None or pd.isna(name) else str(name).upper()
    if "ST" in stock_name or "退" in stock_name:
        return 0.05
    if ts_code.endswith(".BJ") or ts_code.startswith(("4", "8", "9")):
        return 0.30
    if ts_code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def fee_rate_without_slippage() -> float:
    return COMMISSION_RATE + TRANSFER_FEE_RATE + COMMISSION_RATE + TRANSFER_FEE_RATE + STAMP_TAX_RATE


def is_limit_up_open(row: pd.Series) -> bool:
    open_price = float(row["d1_open"])
    pre_close = float(row["d1_pre_close"])
    up_price = pre_close * (1.0 + limit_pct(str(row["ts_code"]), row.get("name")) - LIMIT_PRICE_TOLERANCE)
    return open_price >= up_price


def is_limit_up_one_word(row: pd.Series) -> bool:
    pre_close = float(row["d1_pre_close"])
    up_price = pre_close * (1.0 + limit_pct(str(row["ts_code"]), row.get("name")) - LIMIT_PRICE_TOLERANCE)
    return float(row["d1_low"]) >= up_price


def is_limit_down_day(row: pd.Series, offset: int) -> bool:
    open_price = float(row[f"d{offset}_open"])
    close_price = float(row[f"d{offset}_close"])
    pre_close = float(row[f"d{offset}_pre_close"])
    down_price = pre_close * (1.0 - limit_pct(str(row["ts_code"]), row.get("name")) + LIMIT_PRICE_TOLERANCE)
    return open_price <= down_price or close_price <= down_price


def has_day(row: pd.Series, offset: int) -> bool:
    required = [f"d{offset}_{field}" for field in ["trade_date", "open", "close", "pre_close"]]
    return all(field in row.index and not pd.isna(row[field]) for field in required)


def planned_exit_offset(row: pd.Series) -> int:
    exit_date = str(row.get("exit_trade_date", "")).replace(".0", "")
    for offset in range(1, MAX_FORWARD_DAYS + 1):
        value = str(row.get(f"d{offset}_trade_date", "")).replace(".0", "")
        if value and value == exit_date:
            return offset
    replay_rule = str(row.get("replay_rule", ""))
    if "hold3" in replay_rule:
        return 3
    return 2


def apply_filter(data: pd.DataFrame, expr: str) -> pd.DataFrame:
    result = data.copy()
    for raw_part in expr.split(" AND "):
        part = raw_part.strip()
        column, value = [item.strip() for item in part.split("!=", 1)]
        result = result[result[column].fillna("missing").astype(str).ne(value)].copy()
    return result


def load_l_source() -> pd.DataFrame:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"找不到 L 策略交易明细: {INPUT_PATH}")
    data = pd.read_csv(INPUT_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    data = data[data["l_rule"].astype(str).eq("L_theme_mainline_leader")].copy()
    data["l_account_return"] = to_numeric(data["l_account_return"])
    return data.sort_values(["trade_date", "l_account_return"], ascending=[True, False]).reset_index(drop=True)


def theoretical_stats(trades: pd.DataFrame) -> dict[str, Any]:
    returns = to_numeric(trades["l_account_return"])
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    return {
        "theory_signal_count": int(len(trades)),
        "theory_win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "theory_avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "theory_equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "theory_max_drawdown": max_drawdown(equity),
    }


def replay_variant(variant: LVariant, trades: pd.DataFrame, buy_block_mode: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    selected = apply_filter(trades, variant.filter_expr)
    selected = selected.drop_duplicates("trade_date", keep="first").reset_index(drop=True)
    theory = theoretical_stats(selected)

    equity = INITIAL_EQUITY
    position_until = ""
    rows: list[dict[str, Any]] = []
    executed_returns: list[float] = []

    for _, row in selected.iterrows():
        signal_date = str(row["trade_date"])
        base = {
            "variant": variant.name,
            "description": variant.description,
            "buy_block_mode": buy_block_mode,
            "signal_date": signal_date,
            "ts_code": row["ts_code"],
            "name": row.get("name", ""),
            "theory_account_return": float(row["l_account_return"]),
            "equity_before": equity,
        }

        if position_until and signal_date < position_until:
            rows.append({
                **base,
                "status": "SKIP_POSITION_OCCUPIED",
                "buy_executed": False,
                "sell_executed": False,
                "account_return": 0.0,
                "equity_after": equity,
                "skip_reason": f"上一笔持仓到{position_until}卖出",
            })
            continue

        if not has_day(row, 1):
            rows.append({
                **base,
                "status": "SKIP_MISSING_BUY_DAY",
                "buy_executed": False,
                "sell_executed": False,
                "account_return": 0.0,
                "equity_after": equity,
                "skip_reason": "缺少T+1买入日行情",
            })
            continue

        open_limit = is_limit_up_open(row)
        one_word = is_limit_up_one_word(row)
        buy_blocked = open_limit if buy_block_mode == "open_limit_unbuyable" else one_word
        if buy_blocked:
            rows.append({
                **base,
                "status": "SKIP_LIMIT_UP_UNBUYABLE",
                "buy_executed": False,
                "sell_executed": False,
                "account_return": 0.0,
                "equity_after": equity,
                "buy_trade_date": row["d1_trade_date"],
                "buy_price_before_slippage": row["d1_open"],
                "buy_open_limit": open_limit,
                "buy_one_word_limit": one_word,
                "skip_reason": "买入日涨停排队不可成交",
            })
            continue

        buy_price = float(row["d1_open"]) * (1.0 + BUY_SLIPPAGE_RATE)
        start_offset = planned_exit_offset(row)
        sell_info: dict[str, Any] | None = None
        blocked_days = 0
        for offset in range(start_offset, MAX_FORWARD_DAYS + 1):
            if not has_day(row, offset):
                break
            if is_limit_down_day(row, offset):
                blocked_days += 1
                continue
            raw_exit_price = float(row[f"d{offset}_close"])
            sell_info = {
                "sell_executed": True,
                "exit_trade_date": str(row[f"d{offset}_trade_date"]).replace(".0", ""),
                "exit_offset": offset,
                "exit_price_before_slippage": raw_exit_price,
                "exit_price": raw_exit_price * (1.0 - SELL_SLIPPAGE_RATE),
            }
            break

        if not sell_info:
            position_until = "99991231"
            rows.append({
                **base,
                "status": "SELL_UNRESOLVED_LIMIT_DOWN",
                "buy_executed": True,
                "sell_executed": False,
                "buy_trade_date": row["d1_trade_date"],
                "buy_price": buy_price,
                "limit_down_blocked_days": blocked_days,
                "account_return": 0.0,
                "equity_after": equity,
                "skip_reason": "D5内仍无可卖日",
            })
            continue

        net_return = sell_info["exit_price"] / buy_price - 1.0 - fee_rate_without_slippage()
        account_return = net_return * POSITION_PCT
        equity = equity * (1.0 + account_return)
        position_until = sell_info["exit_trade_date"]
        executed_returns.append(account_return)
        rows.append({
            **base,
            **sell_info,
            "status": "EXECUTED",
            "buy_executed": True,
            "sell_executed": True,
            "buy_trade_date": str(row["d1_trade_date"]).replace(".0", ""),
            "buy_price_before_slippage": float(row["d1_open"]),
            "buy_price": buy_price,
            "buy_open_limit": open_limit,
            "buy_one_word_limit": one_word,
            "planned_exit_offset": start_offset,
            "limit_down_blocked_days": blocked_days,
            "net_return": net_return,
            "account_return": account_return,
            "equity_after": equity,
        })

    detail = pd.DataFrame(rows)
    executed = detail[detail["status"].eq("EXECUTED")].copy()
    returns = to_numeric(executed.get("account_return", pd.Series(dtype=float)))
    equity_curve = INITIAL_EQUITY * (1.0 + returns).cumprod()
    summary = {
        "variant": variant.name,
        "description": variant.description,
        "buy_block_mode": buy_block_mode,
        **theory,
        "signal_count": int(len(selected)),
        "executed_count": int(len(executed)),
        "position_skip_count": int(detail["status"].eq("SKIP_POSITION_OCCUPIED").sum()),
        "buy_rejected_count": int(detail["status"].eq("SKIP_LIMIT_UP_UNBUYABLE").sum()),
        "sell_unresolved_count": int(detail["status"].eq("SELL_UNRESOLVED_LIMIT_DOWN").sum()),
        "limit_down_blocked_trades": int((to_numeric(executed.get("limit_down_blocked_days", pd.Series(dtype=float))) > 0).sum()),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "equity_multiple": float(equity_curve.iloc[-1] / INITIAL_EQUITY) if len(equity_curve) else 1.0,
        "max_drawdown": max_drawdown(equity_curve),
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }
    return detail, summary


def write_report(summary: pd.DataFrame) -> None:
    report_path = OUTPUT_DIR / "l_live_cert_report.md"
    main = summary[summary["buy_block_mode"].eq("open_limit_unbuyable")].copy()
    best = main.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, False]).iloc[0]
    lines = [
        "# L 策略实盘可执行性模拟认证",
        "",
        "说明：本报告只研究 L 单策略，不接实盘，不修改 ABC/E2/D。",
        "",
        f"- 严格口径最优：{best['variant']}，执行{int(best['executed_count'])}笔，买入拒绝{int(best['buy_rejected_count'])}笔，持仓冲突跳过{int(best['position_skip_count'])}笔，复利{best['equity_multiple']:.2f}倍，最大回撤{best['max_drawdown']:.2%}",
        "",
        "## 汇总",
        "",
        summary.to_markdown(index=False),
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = load_l_source()
    details = []
    summaries = []
    for variant in VARIANTS:
        for mode in ["open_limit_unbuyable", "one_word_limit_unbuyable"]:
            detail, summary = replay_variant(variant, source, mode)
            details.append(detail)
            summaries.append(summary)
    detail_df = pd.concat(details, ignore_index=True)
    summary_df = pd.DataFrame(summaries).sort_values(
        ["buy_block_mode", "equity_multiple", "max_drawdown"],
        ascending=[True, False, False],
    )
    detail_df.to_csv(OUTPUT_DIR / "l_live_cert_detail.csv", index=False)
    summary_df.to_csv(OUTPUT_DIR / "l_live_cert_summary.csv", index=False)
    write_report(summary_df)

    print("L实盘可执行性模拟认证完成")
    print(summary_df.to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
