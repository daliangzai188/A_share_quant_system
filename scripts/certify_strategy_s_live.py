"""
策略S实盘口径模拟认证。

用途：
  - 不提交任何实盘委托。
  - 读取策略S研究明细，按实盘资金、整百股、单笔金额上限、滑点、费用复放。
  - 给出 PASS/FAIL 认证结论，作为是否允许接入组合实盘链路的硬闸门。

认证口径：
  - S-safe: 创业板 + 近涨停动量，3.5 <= volume_ratio5 <= 8。
  - 连续3笔亏损后暂停5个交易日。
  - T日收盘信号，T+1开盘买，T+2收盘卖。
  - 单笔计划金额必须严格小于 live_trade.max_single_order_amount。
  - 买入/卖出均按指定滑点网格复放，并扣佣金、过户费、卖出印花税。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config, mkdir_p

INPUT_TRADES = PROJECT_ROOT / "reports" / "strategy_s" / "s_best_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_s" / "live_certification"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"

DEFAULT_INITIAL_CASH = 100_500.0
DEFAULT_CASH_BUFFER = 500.0
DEFAULT_MAX_ORDER_AMOUNT = 50_000.0
DEFAULT_SLIPPAGE_GRID = [0.001, 0.003, 0.005, 0.01]


@dataclass(frozen=True)
class CertificationGate:
    min_2026_return: float = 0.0
    max_drawdown_floor: float = -0.20
    min_trade_count: int = 100
    min_2026_trade_count: int = 20
    max_consecutive_losses: int = 6
    min_stress_vs_base_ratio: float = 0.85
    max_zero_qty_rate: float = 0.05


@dataclass
class ReplayConfig:
    scenario: str
    initial_cash: float
    max_single_order_amount: float
    cash_buffer: float
    buy_slippage_rate: float
    sell_slippage_rate: float
    commission_rate: float
    stamp_tax_rate: float
    transfer_fee_rate: float
    min_commission: float
    lot_size: int = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="策略S实盘口径模拟认证")
    parser.add_argument("--input", default=str(INPUT_TRADES), help="策略S研究交易明细CSV")
    parser.add_argument("--config", default="config/config.json", help="项目配置文件")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH, help="认证初始资金")
    parser.add_argument("--cash-buffer", type=float, default=DEFAULT_CASH_BUFFER, help="现金缓冲，避免满额下单")
    parser.add_argument("--slippage-grid", default="0.001,0.003,0.005,0.01", help="滑点网格，逗号分隔")
    parser.add_argument("--max-order-amount", type=float, default=None, help="单笔订单金额上限，默认读取配置")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    return parser.parse_args()


def load_calendar() -> list[str]:
    cal = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    if "is_open" in cal.columns:
        cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "true", "True"})]
    return sorted(cal["cal_date"].astype(str).tolist())


def add_trade_days(date_str: str, n: int, calendar: list[str]) -> str:
    future = [d for d in calendar if d > date_str]
    if len(future) < n:
        return "99991231"
    return future[n - 1]


def load_source_trades(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"找不到策略S交易明细：{path}")
    data = pd.read_csv(path, dtype={"signal_date": str, "buy_date": str, "sell_date": str, "ts_code": str})
    required = {
        "signal_date",
        "buy_date",
        "sell_date",
        "ts_code",
        "segment",
        "volume_ratio5",
        "buy_price",
        "sell_price",
        "net_return",
        "year",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"策略S交易明细缺少字段：{missing}")
    data["signal_date"] = data["signal_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["buy_date"] = data["buy_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["sell_date"] = data["sell_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["year"] = data["year"].astype(str).str.replace(r"\.0$", "", regex=True)
    return data.sort_values(["signal_date", "ts_code"]).reset_index(drop=True)


def apply_s_safe_filter(data: pd.DataFrame) -> pd.DataFrame:
    filtered = data.copy()
    filtered["volume_ratio5"] = pd.to_numeric(filtered["volume_ratio5"], errors="coerce")
    filtered = filtered[
        (filtered["segment"].astype(str) == "chi_next")
        & (filtered["volume_ratio5"] >= 3.5)
        & (filtered["volume_ratio5"] <= 8.0)
    ].copy()
    return filtered.reset_index(drop=True)


def round_lot_shares_below_amount(amount: float, price: float, lot_size: int) -> int:
    if amount <= 0 or price <= 0:
        return 0
    shares = int((amount - 0.01) / price)
    if lot_size > 0:
        shares -= shares % lot_size
    return max(shares, 0)


def calc_fee(amount: float, rate: float, min_commission: float) -> float:
    if amount <= 0:
        return 0.0
    return max(amount * rate, min_commission)


def compute_max_drawdown(equity_values: list[float]) -> float:
    if not equity_values:
        return 0.0
    peak = equity_values[0]
    max_dd = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, (value - peak) / peak)
    return max_dd


def max_consecutive_losses(returns: list[float]) -> int:
    longest = 0
    current = 0
    for ret in returns:
        if ret <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def replay_live_cap(
    source: pd.DataFrame,
    calendar: list[str],
    config: ReplayConfig,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    equity = config.initial_cash
    rows: list[dict[str, Any]] = []
    loss_streak = 0
    pause_until = ""

    for _, row in source.iterrows():
        signal_date = str(row["signal_date"])
        if pause_until and signal_date < pause_until:
            rows.append({
                "scenario": config.scenario,
                "signal_date": signal_date,
                "buy_date": row["buy_date"],
                "sell_date": row["sell_date"],
                "ts_code": row["ts_code"],
                "status": "SKIP_LOSS_PAUSE",
                "equity_before": equity,
                "equity_after": equity,
                "skip_reason": f"连续亏损暂停至{pause_until}",
            })
            continue

        raw_buy_price = float(row["buy_price"])
        raw_sell_price = float(row["sell_price"])
        buy_price = raw_buy_price * (1.0 + config.buy_slippage_rate)
        sell_price = raw_sell_price * (1.0 - config.sell_slippage_rate)
        allowed_amount = min(config.max_single_order_amount, max(equity - config.cash_buffer, 0.0))
        shares = round_lot_shares_below_amount(allowed_amount, buy_price, config.lot_size)

        if shares <= 0:
            rows.append({
                "scenario": config.scenario,
                "signal_date": signal_date,
                "buy_date": row["buy_date"],
                "sell_date": row["sell_date"],
                "ts_code": row["ts_code"],
                "status": "SKIP_ZERO_QTY",
                "equity_before": equity,
                "equity_after": equity,
                "buy_price": buy_price,
                "allowed_amount": allowed_amount,
                "skip_reason": "整百股后数量为0",
            })
            continue

        buy_amount = shares * buy_price
        sell_amount = shares * sell_price
        buy_fee = calc_fee(buy_amount, config.commission_rate + config.transfer_fee_rate, config.min_commission)
        sell_fee = calc_fee(sell_amount, config.commission_rate + config.transfer_fee_rate, config.min_commission)
        stamp_tax = sell_amount * config.stamp_tax_rate
        pnl = sell_amount - buy_amount - buy_fee - sell_fee - stamp_tax
        equity_before = equity
        equity += pnl
        account_return = pnl / equity_before if equity_before > 0 else 0.0
        trade_return = pnl / buy_amount if buy_amount > 0 else 0.0

        if pnl <= 0:
            loss_streak += 1
        else:
            loss_streak = 0
        if loss_streak >= 3:
            pause_until = add_trade_days(str(row["sell_date"]), 5, calendar)
            loss_streak = 0

        rows.append({
            "scenario": config.scenario,
            "signal_date": signal_date,
            "buy_date": row["buy_date"],
            "sell_date": row["sell_date"],
            "ts_code": row["ts_code"],
            "segment": row.get("segment", ""),
            "volume_ratio5": row.get("volume_ratio5", 0),
            "turnover_rate": row.get("turnover_rate", 0),
            "market_limit_count": row.get("market_limit_count", 0),
            "status": "EXECUTED",
            "shares": shares,
            "raw_buy_price": raw_buy_price,
            "raw_sell_price": raw_sell_price,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "buy_slippage_rate": config.buy_slippage_rate,
            "sell_slippage_rate": config.sell_slippage_rate,
            "allowed_amount": allowed_amount,
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "buy_fee": buy_fee,
            "sell_fee": sell_fee,
            "stamp_tax": stamp_tax,
            "pnl": pnl,
            "trade_return": trade_return,
            "account_return": account_return,
            "equity_before": equity_before,
            "equity_after": equity,
            "is_win": pnl > 0,
            "loss_streak_after": loss_streak,
            "pause_until_after": pause_until,
            "year": str(row["year"]),
            "skip_reason": "",
        })

    detail = pd.DataFrame(rows)
    executed = detail[detail["status"] == "EXECUTED"].copy()
    zero_qty = detail[detail["status"] == "SKIP_ZERO_QTY"].copy()
    equity_values = [config.initial_cash] + executed["equity_after"].astype(float).tolist()
    returns = executed["account_return"].astype(float).tolist()

    by_year = {}
    if not executed.empty:
        for year, group in executed.groupby("year"):
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            by_year[str(year)] = {
                "trade_count": int(len(group)),
                "period_return": (last_equity - first_equity) / first_equity if first_equity > 0 else 0.0,
                "win_rate": float(group["is_win"].mean()),
            }

    summary = {
        "scenario": config.scenario,
        "initial_cash": config.initial_cash,
        "final_equity": float(equity),
        "equity_multiple": float(equity / config.initial_cash) if config.initial_cash > 0 else 0.0,
        "trade_count": int(len(executed)),
        "source_signal_count": int(len(source)),
        "skip_loss_pause_count": int((detail["status"] == "SKIP_LOSS_PAUSE").sum()) if not detail.empty else 0,
        "zero_qty_count": int(len(zero_qty)),
        "zero_qty_rate": float(len(zero_qty) / len(source)) if len(source) else 0.0,
        "win_rate": float(executed["is_win"].mean()) if not executed.empty else 0.0,
        "avg_trade_return": float(executed["trade_return"].mean()) if not executed.empty else 0.0,
        "avg_account_return": float(executed["account_return"].mean()) if not executed.empty else 0.0,
        "max_profit": float(executed["trade_return"].max()) if not executed.empty else 0.0,
        "max_loss": float(executed["trade_return"].min()) if not executed.empty else 0.0,
        "max_drawdown": float(compute_max_drawdown(equity_values)),
        "max_consecutive_losses": int(max_consecutive_losses(returns)),
        "return_2026": float(by_year.get("2026", {}).get("period_return", 0.0)),
        "trade_count_2026": int(by_year.get("2026", {}).get("trade_count", 0)),
        "win_rate_2026": float(by_year.get("2026", {}).get("win_rate", 0.0)),
        "max_order_amount_seen": float(executed["buy_amount"].max()) if not executed.empty else 0.0,
        "all_orders_below_cap": bool((executed["buy_amount"] < config.max_single_order_amount).all()) if not executed.empty else True,
        "buy_slippage_rate": config.buy_slippage_rate,
        "sell_slippage_rate": config.sell_slippage_rate,
        "max_single_order_amount": config.max_single_order_amount,
        "cash_buffer": config.cash_buffer,
    }
    return detail, summary


def evaluate_gate(summary: dict[str, Any], base_summary: dict[str, Any], gate: CertificationGate) -> tuple[str, list[str]]:
    failures: list[str] = []
    if summary["trade_count"] < gate.min_trade_count:
        failures.append(f"全区间交易笔数不足：{summary['trade_count']} < {gate.min_trade_count}")
    if summary["trade_count_2026"] < gate.min_2026_trade_count:
        failures.append(f"2026交易笔数不足：{summary['trade_count_2026']} < {gate.min_2026_trade_count}")
    if summary["return_2026"] <= gate.min_2026_return:
        failures.append(f"2026收益未转正：{summary['return_2026']:.2%}")
    if summary["max_drawdown"] < gate.max_drawdown_floor:
        failures.append(f"最大回撤超限：{summary['max_drawdown']:.2%} < {gate.max_drawdown_floor:.2%}")
    if summary["max_consecutive_losses"] > gate.max_consecutive_losses:
        failures.append(f"最大连续亏损超限：{summary['max_consecutive_losses']} > {gate.max_consecutive_losses}")
    if summary["zero_qty_rate"] > gate.max_zero_qty_rate:
        failures.append(f"零股跳过比例超限：{summary['zero_qty_rate']:.2%} > {gate.max_zero_qty_rate:.2%}")
    if not summary["all_orders_below_cap"]:
        failures.append("存在买入金额不小于单笔上限的订单")

    base_multiple = float(base_summary.get("equity_multiple", 0.0))
    ratio = float(summary.get("equity_multiple", 0.0)) / base_multiple if base_multiple > 0 else 0.0
    if ratio < gate.min_stress_vs_base_ratio:
        failures.append(f"压力滑点相对基准衰减过大：{ratio:.2%} < {gate.min_stress_vs_base_ratio:.2%}")

    return ("PASS" if not failures else "FAIL"), failures


def write_markdown(
    path: Path,
    summary_df: pd.DataFrame,
    gate: CertificationGate,
    base_scenario: str,
    final_status: str,
    final_failures: list[str],
) -> None:
    lines = [
        "# 策略S实盘口径模拟认证报告",
        "",
        "## 认证结论",
        "",
        f"- 最终结论：**{final_status}**",
        f"- 基准场景：{base_scenario}",
        "- 认证规则：S-safe（3.5 <= volume_ratio5 <= 8）+ 连亏3笔暂停5个交易日",
        "- 执行口径：T日收盘信号，T+1开盘买，T+2收盘卖",
        "- 实盘约束：整百股，单笔买入金额严格小于配置上限，扣佣金/过户费/印花税/滑点",
        "",
        "## 硬门槛",
        "",
    ]
    for key, value in asdict(gate).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## 未通过项", ""])
    if final_failures:
        lines.extend([f"- {item}" for item in final_failures])
    else:
        lines.append("- 无")

    display_cols = [
        "scenario",
        "certification_status",
        "trade_count",
        "trade_count_2026",
        "equity_multiple",
        "return_2026",
        "win_rate",
        "max_drawdown",
        "max_consecutive_losses",
        "zero_qty_rate",
        "max_order_amount_seen",
        "stress_vs_base_ratio",
    ]
    lines.extend(["", "## 场景摘要", ""])
    table = summary_df[display_cols].copy()
    lines.append(table.to_markdown(index=False))
    lines.extend([
        "",
        "## 接入判断",
        "",
        "只有最终结论为 PASS，才允许继续把 S 接入组合状态机；接入后仍必须走 LiveOrderGateway 二次风控。",
        "如果结论为 FAIL，只允许继续研究或模拟，不允许打开 S 的真实下单。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    mkdir_p(out_dir)

    project_config = load_json_config(args.config)
    risk = project_config.get("analysis", {})
    live_trade = project_config.get("live_trade", {})
    max_order_amount = float(args.max_order_amount or live_trade.get("max_single_order_amount", DEFAULT_MAX_ORDER_AMOUNT))
    slippage_grid = [float(x.strip()) for x in str(args.slippage_grid).split(",") if x.strip()]

    source_all = load_source_trades(Path(args.input))
    source = apply_s_safe_filter(source_all)
    calendar = load_calendar()

    gate = CertificationGate()
    summaries: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []

    for slip in slippage_grid:
        scenario = f"live_cap_{max_order_amount:.0f}_slippage_{slip:.3%}"
        replay_config = ReplayConfig(
            scenario=scenario,
            initial_cash=float(args.initial_cash),
            max_single_order_amount=max_order_amount,
            cash_buffer=float(args.cash_buffer),
            buy_slippage_rate=slip,
            sell_slippage_rate=slip,
            commission_rate=float(risk.get("commission_rate", 0.0003)),
            stamp_tax_rate=float(risk.get("stamp_tax_rate", 0.001)),
            transfer_fee_rate=float(risk.get("transfer_fee_rate", 0.00001)),
            min_commission=float(live_trade.get("min_commission", 0.0)),
            lot_size=int(live_trade.get("round_lot_size", 100)),
        )
        detail, summary = replay_live_cap(source, calendar, replay_config)
        detail_frames.append(detail)
        summaries.append(summary)

    summary_df = pd.DataFrame(summaries)
    base_summary = summaries[0] if summaries else {}
    base_multiple = float(base_summary.get("equity_multiple", 0.0))
    statuses = []
    failure_texts = []
    ratios = []
    for summary in summaries:
        status, failures = evaluate_gate(summary, base_summary, gate)
        statuses.append(status)
        failure_texts.append("; ".join(failures))
        ratio = float(summary.get("equity_multiple", 0.0)) / base_multiple if base_multiple > 0 else 0.0
        ratios.append(ratio)
    summary_df["certification_status"] = statuses
    summary_df["certification_failures"] = failure_texts
    summary_df["stress_vs_base_ratio"] = ratios

    final_status = "PASS" if bool((summary_df["certification_status"] == "PASS").all()) else "FAIL"
    final_failures = sorted({item for text in failure_texts for item in text.split("; ") if item})
    summary_df["final_status"] = final_status

    detail_df = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    summary_path = out_dir / "s_live_certification_summary.csv"
    detail_path = out_dir / "s_live_certification_detail.csv"
    md_path = out_dir / "s_live_certification_report.md"
    json_path = out_dir / "s_live_certification_gate.json"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")
    write_markdown(
        md_path,
        summary_df,
        gate,
        str(summary_df["scenario"].iloc[0]) if not summary_df.empty else "",
        final_status,
        final_failures,
    )
    json_path.write_text(
        json.dumps(
            {
                "final_status": final_status,
                "summary_path": str(summary_path.relative_to(PROJECT_ROOT)),
                "detail_path": str(detail_path.relative_to(PROJECT_ROOT)),
                "report_path": str(md_path.relative_to(PROJECT_ROOT)),
                "gate": asdict(gate),
                "failures": final_failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"策略S实盘口径认证：{final_status}")
    print(f"摘要：{summary_path}")
    print(f"明细：{detail_path}")
    print(f"报告：{md_path}")
    print(summary_df[[
        "scenario",
        "certification_status",
        "trade_count",
        "trade_count_2026",
        "equity_multiple",
        "return_2026",
        "max_drawdown",
        "stress_vs_base_ratio",
    ]].to_string(index=False))


if __name__ == "__main__":
    main()
