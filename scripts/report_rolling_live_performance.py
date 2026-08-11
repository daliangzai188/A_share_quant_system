#!/usr/bin/env python3
"""生成真实成交滚动报告；不连接券商、不修改策略、不下单。"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live_performance import (  # noqa: E402
    capacity_monitor_status,
    completed_live_trades,
    execution_capacity_metrics,
    rolling_metrics,
)
from src.utils.config import load_json_config, mkdir_p  # noqa: E402


SOURCE = PROJECT_ROOT / "reports" / "execution_tracking" / "trade_completion_summary.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "live_performance"


def _markdown(frame: pd.DataFrame) -> str:
    view = frame.copy()
    for column in ("win_rate", "avg_return", "median_return", "return_on_invested_capital", "hypothetical_max_drawdown", "max_profit", "max_loss"):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(lambda value: f"{value:.2%}")
    for column in ("total_net_pnl",):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(lambda value: f"{value:,.2f}")
    for column in ("hypothetical_full_notional_multiple", "profit_loss_ratio", "avg_total_slippage_bps"):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(lambda value: f"{value:.4f}")
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist())
    return "\n".join(lines)


def _capacity_markdown(frame: pd.DataFrame) -> str:
    view = frame.copy()
    for column in (
        "entry_full_fill_rate",
        "avg_entry_qty_completion",
        "p10_entry_qty_completion",
        "entry_notional_completion",
        "exit_full_completion_rate",
        "buy_benchmark_coverage",
        "sell_benchmark_coverage",
    ):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(
            lambda value: f"{value:.2%}"
        )
    for column in ("planned_entry_amount", "filled_entry_amount"):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(
            lambda value: f"{value:,.2f}"
        )
    for column in (
        "avg_buy_slippage_bps",
        "p90_buy_slippage_bps",
        "avg_sell_slippage_bps",
        "p90_sell_slippage_bps",
        "avg_total_slippage_bps",
    ):
        view[column] = pd.to_numeric(view[column], errors="coerce").map(
            lambda value: f"{value:.2f}"
        )
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist())
    return "\n".join(lines)


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(f"找不到真实成交完成汇总：{SOURCE}")
    config = load_json_config("config/config.json")
    report_config = dict(config.get("live_performance_report", {}))
    analysis = config.get("analysis", {})
    for key in ("commission_rate", "stamp_tax_rate", "transfer_fee_rate"):
        report_config.setdefault(key, analysis.get(key))
    raw = pd.read_csv(SOURCE, dtype={"trade_key": str, "ts_code": str}, low_memory=False)
    trades, quality = completed_live_trades(raw, report_config)
    capacity_metrics = execution_capacity_metrics(raw, report_config)
    capacity_status = capacity_monitor_status(capacity_metrics, report_config)
    windows = [int(value) for value in report_config.get("windows", [20, 60, 120])]
    metrics = rolling_metrics(trades, windows)
    minimum = int(report_config.get("minimum_samples_for_decision", 20))
    overall = metrics.iloc[0]
    if len(trades) < minimum:
        status = "INSUFFICIENT_SAMPLE"
        reason = f"完整真实成交仅{len(trades)}笔，少于决策门槛{minimum}笔；只监控，不改策略。"
    elif float(overall["avg_return"]) <= 0:
        status = "WATCH"
        reason = "滚动真实成交平均净收益不为正，进入观察；仍需人工复核行情和各腿归因。"
    else:
        status = "PASS"
        reason = "滚动真实成交样本达到门槛且平均净收益为正。"

    mkdir_p(OUTPUT_DIR)
    trades.to_csv(OUTPUT_DIR / "rolling_live_trades.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUTPUT_DIR / "rolling_live_metrics.csv", index=False, encoding="utf-8-sig")
    capacity_metrics.to_csv(
        OUTPUT_DIR / "execution_capacity_metrics.csv", index=False, encoding="utf-8-sig"
    )
    payload = {
        "status": status,
        "reason": reason,
        "source": str(SOURCE.relative_to(PROJECT_ROOT)),
        **quality,
        "minimum_samples_for_decision": minimum,
        "valid_sample_count": int(len(trades)),
        "latest_exit_date": str(trades["exit_date"].max()) if len(trades) else "",
        "capacity_monitor": capacity_status,
        "note": "hypothetical_full_notional_multiple仅是每笔全仓串行假设，不是账户实际收益。",
    }
    (OUTPUT_DIR / "rolling_live_status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = [
        "# 真实成交滚动绩效",
        "",
        f"- 状态：**{status}**",
        f"- 判定：{reason}",
        f"- 数据完整率：{quality['data_complete_rate']:.2%}（{quality['complete_trade_rows']}/{quality['active_trade_rows']}）",
        "- 收益已按真实买卖金额并估算佣金、印花税和过户费；不完整交易不进入收益统计。",
        "- 全仓串行倍数只用于比较交易分布，不能当作账户实际收益。",
        "",
        _markdown(metrics),
        "",
        "## 真实执行容量与TCA",
        "",
        f"- 容量状态：**{capacity_status['status']}**",
        f"- 判定：{capacity_status['reason']}",
        "- 只有开仓前真实冻结的计划参与容量判定；历史按成交反推的目标只披露，不参与认证。",
        "- 该状态当前只监控、不改变下单；capacity_certified=false时继续小资金验证。",
        "",
        _capacity_markdown(capacity_metrics),
    ]
    (OUTPUT_DIR / "rolling_live_report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
