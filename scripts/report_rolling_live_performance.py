#!/usr/bin/env python3
"""生成真实成交滚动报告；不连接券商、不修改策略、不下单。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

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
from src.execution_data_quality import analyze_execution_data_quality  # noqa: E402
from src.account_risk_shadow import (  # noqa: E402
    load_json_object as load_risk_json,
    update_account_risk_shadow,
    validate_shadow_policy,
)
from src.utils.config import load_json_config, mkdir_p  # noqa: E402


SOURCE = PROJECT_ROOT / "reports" / "execution_tracking" / "trade_completion_summary.csv"
SELL_EVENTS_SOURCE = PROJECT_ROOT / "reports" / "execution_tracking" / "sell_execution_slices.csv"
POSITIONS_SOURCE = PROJECT_ROOT / "data" / "processed" / "positions.json"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "live_performance"
ACCOUNT_RISK_POLICY_PATH = PROJECT_ROOT / "config" / "account_risk_shadow.json"


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


def _gap_markdown(frame: pd.DataFrame) -> str:
    gaps = frame[~frame["gap_category"].astype(str).eq("COMPLETE")].copy()
    if gaps.empty:
        return "暂无成交数据缺口或未平仓记录。"
    columns = [
        "trade_key",
        "planned_exit_date",
        "gap_category",
        "severity",
        "recoverability",
        "reason",
        "recommended_action",
    ]
    view = gaps[columns].fillna("").astype(str)
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist()
    )
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
    sell_events = (
        pd.read_csv(SELL_EVENTS_SOURCE, dtype=str, low_memory=False)
        if SELL_EVENTS_SOURCE.exists()
        else pd.DataFrame()
    )
    try:
        positions = json.loads(POSITIONS_SOURCE.read_text(encoding="utf-8"))
        if not isinstance(positions, list):
            positions = []
    except (OSError, json.JSONDecodeError):
        positions = []
    now_shanghai = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    data_quality_detail, data_quality_status = analyze_execution_data_quality(
        raw,
        report_config,
        as_of_date=now_shanghai.strftime("%Y%m%d"),
        as_of_time=now_shanghai.strftime("%H%M%S"),
        positions=positions,
        sell_events=sell_events,
    )
    trades, quality = completed_live_trades(raw, report_config)
    capacity_metrics = execution_capacity_metrics(raw, report_config)
    capacity_status = capacity_monitor_status(capacity_metrics, report_config)
    risk_policy = load_risk_json(ACCOUNT_RISK_POLICY_PATH)
    normalized_risk_policy = validate_shadow_policy(risk_policy)
    risk_bootstrap = load_risk_json(
        PROJECT_ROOT / normalized_risk_policy["bootstrap_equity_path"]
    )
    bootstrap_equity = float(risk_bootstrap.get("last_equity", 0.0) or 0.0)
    account_risk_shadow = update_account_risk_shadow(
        state_path=PROJECT_ROOT / normalized_risk_policy["state_path"],
        latest_status_path=PROJECT_ROOT
        / normalized_risk_policy["latest_status_path"],
        policy=risk_policy,
        complete_trades=trades,
        bootstrap_equity=bootstrap_equity,
        as_of_date=now_shanghai.strftime("%Y%m%d"),
    )
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
    data_quality_detail.to_csv(
        OUTPUT_DIR / "execution_data_quality_detail.csv", index=False, encoding="utf-8-sig"
    )
    (OUTPUT_DIR / "execution_data_quality_status.json").write_text(
        json.dumps(data_quality_status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    payload = {
        "status": status,
        "reason": reason,
        "source": str(SOURCE.relative_to(PROJECT_ROOT)),
        **quality,
        "minimum_samples_for_decision": minimum,
        "valid_sample_count": int(len(trades)),
        "latest_exit_date": str(trades["exit_date"].max()) if len(trades) else "",
        "execution_data_quality": data_quality_status,
        "capacity_monitor": capacity_status,
        "account_risk_shadow": account_risk_shadow,
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
        f"- 已结算口径完整率：{data_quality_status['settled_data_complete_rate']:.2%}（正常未到期/今日到期持仓{data_quality_status['normal_open_trade_rows']}笔不计入分母）。",
        "- 收益已按真实买卖金额并估算佣金、印花税和过户费；不完整交易不进入收益统计。",
        "- 全仓串行倍数只用于比较交易分布，不能当作账户实际收益。",
        "",
        "## 真实成交数据缺口",
        "",
        f"- 状态：**{data_quality_status['status']}**",
        f"- 判定：{data_quality_status['reason']}",
        "- 本节只分类、提示如何取证，不会猜卖出价，也不会自动写回持仓或成交账本。",
        "",
        _gap_markdown(data_quality_detail),
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
        "",
        "## 账户级风险总闸（影子模式）",
        "",
        f"- 状态：**{account_risk_shadow['status']}**",
        f"- 判定：{account_risk_shadow['reason']}",
        f"- 假设动作：`{account_risk_shadow['suggested_action']}`",
        "- enforce_live_gate=false：本节只观测，不拦截当前候选、开仓、卖出或资金调度。",
        "- 将来只有历史与样本外证据充分、且总复利不低于当前硬底线70%时，才允许另行评审是否接入。",
    ]
    (OUTPUT_DIR / "rolling_live_report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
