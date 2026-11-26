"""实盘交易总账和按策略的实盘统计（2026-11-18，用户要求“把开仓平仓记录好，方便以后研究和复盘”）。

QMT 原生版的逐笔成交汇总（trade_completion_summary.csv）存在 QMT 内部状态里，每次导出覆盖一份。这里由虚拟机上
QMT 外检查（qmt_native_phase0/qmt_external_checks.py）每个交易日调用，另存进同步目录 data/live_trades/
（Mac 和虚拟机各一份，数据快照 tools/data_snapshot.py 也备份）：

- live_trade_ledger.csv：只增不删的逐笔总账。按 trade_key 合并，同一笔交易保留更新时间最新的一行（买入、补仓、
  卖出逐步补全）；导出里没有了的旧交易照样保留；
- live_performance_by_leg.csv / .md：已平仓交易按策略（A/C/E/D/M，ACE+M+L新框架再加L，腿清单在配置
  live_performance_report.active_legs）和全部的实盘统计：笔数、胜率、平均/中位
  净收益、总净盈亏、最大回撤、盈亏比、最大单笔盈亏、最长连亏、平均双边滑点（口径同 src/live_performance.py），
  另列“按规则（开盘买、收盘卖，计划卖出日摸到涨停价−0.01按止盈价卖）”和“实盘”的平均毛收益，两者之差就是
  执行成本，策略本身好坏和执行好坏分开看；
- account_daily.csv：每个交易日收盘后的账户（总资产、可用资金、ACE/M/其它持仓市值，新框架另有L市值一列），由守护进程记、随 QMT
  导出，这里只增不删地并进来，用来算实盘账户的收益曲线和回撤（出入金要另外扣）；

只读 QMT 导出、只写 data/live_trades/，不连券商、不改任何交易状态。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.live_performance import completed_live_trades, performance_metrics

LEDGER_NAME = "live_trade_ledger.csv"
PERFORMANCE_NAME = "live_performance_by_leg"
ACCOUNT_NAME = "account_daily.csv"


def _read(path: Path) -> pd.DataFrame:
    if not Path(path).is_file():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _write_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _merge(existing: pd.DataFrame, new: pd.DataFrame, key: str, version: str, order: list[str]) -> pd.DataFrame:
    """按 key 合并：同一行保留 version 最新的（相同时用新的），旧行不删。"""
    frames = [frame for frame in (existing, new) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    merged = merged[merged.get(key, pd.Series("", index=merged.index)).astype(str).str.len() > 0]
    merged["_order"] = range(len(merged))
    stamp = merged.get(version, pd.Series("", index=merged.index)).astype(str)
    merged = merged.assign(_stamp=stamp).sort_values(["_stamp", "_order"], kind="stable")
    merged = merged.drop_duplicates(key, keep="last")
    return merged.drop(columns=["_order", "_stamp"]).sort_values(order).reset_index(drop=True)


def merge_ledger(existing: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    """逐笔总账：同一笔（trade_key）保留 updated_at 最新的一行，旧交易不删。"""
    return _merge(existing, summary, "trade_key", "updated_at", ["entry_date", "trade_key"])


def _update(source: Path, target: Path, key: str, merge) -> dict[str, int]:
    existing = _read(target)
    new = _read(source)
    if new.empty:
        return {"rows": int(len(existing)), "added": 0}
    merged = merge(existing, new)
    before = set(existing.get(key, pd.Series(dtype=str)).astype(str)) if not existing.empty else set()
    _write_atomic(merged, target)
    return {"rows": int(len(merged)), "added": int(len(set(merged[key].astype(str)) - before))}


def update_ledger(summary_path: Path, ledger_path: Path) -> dict[str, int]:
    """把最新导出的逐笔汇总并进总账；返回总账笔数和本次新增笔数。导出读不到时总账原样不动。"""
    return _update(summary_path, ledger_path, "trade_key", merge_ledger)


def update_account_daily(source: Path, target: Path) -> dict[str, int]:
    """把导出的每日账户并进 data/live_trades/account_daily.csv（按日期，只增不删）。"""
    return _update(source, target, "date",
                   lambda old, new: _merge(old, new, "date", "recorded_at", ["date"]))


def leg_performance(ledger: pd.DataFrame, report_config: Mapping[str, Any]) -> pd.DataFrame:
    """已平仓交易的实盘统计：全部一行，再按策略各一行（只用真实买卖金额，数量不闭合的不算）。"""
    if ledger.empty:
        return pd.DataFrame([performance_metrics(pd.DataFrame(), "全部真实成交")])
    trades, _quality = completed_live_trades(ledger, report_config)
    rows = [dict(performance_metrics(trades, "全部真实成交"), **_rule_versus_live(trades))]
    for leg, group in trades.groupby("strategy_leg", sort=True):
        rows.append(dict(performance_metrics(group, "策略%s" % leg), **_rule_versus_live(group)))
    return pd.DataFrame(rows)


def _rule_versus_live(trades: pd.DataFrame) -> dict[str, Any]:
    """按规则和实盘（真实成交均价）的平均毛收益；两边都有基准价的才算。规则=买入日开盘价买、卖出日按成交汇总的
    benchmark_close卖（计划卖出日摸到涨停价−0.01时为止盈价，否则为收盘价，见scripts/live_execution_audit.py）。
    不复权：持有一两天，除权除息日会有偏差。"""
    if trades.empty:
        return {"rule_compared": 0, "avg_rule_gross_return": 0.0, "avg_live_gross_return": 0.0}
    numeric = {c: pd.to_numeric(trades.get(c, pd.Series(0.0, index=trades.index)), errors="coerce").fillna(0.0)
               for c in ("benchmark_open", "benchmark_close", "entry_fill_amount", "exit_fill_amount")}
    usable = (numeric["benchmark_open"] > 0) & (numeric["benchmark_close"] > 0) & (numeric["entry_fill_amount"] > 0)
    if not usable.any():
        return {"rule_compared": 0, "avg_rule_gross_return": 0.0, "avg_live_gross_return": 0.0}
    rule = numeric["benchmark_close"][usable] / numeric["benchmark_open"][usable] - 1.0
    live = numeric["exit_fill_amount"][usable] / numeric["entry_fill_amount"][usable] - 1.0
    return {"rule_compared": int(usable.sum()), "avg_rule_gross_return": float(rule.mean()),
            "avg_live_gross_return": float(live.mean())}


def _markdown(table: pd.DataFrame, rows: int, as_of: str) -> str:
    lines = [
        "# 实盘交易按策略统计（截至%s，总账%d笔，只算已平仓）" % (as_of, rows),
        "",
        "| 分组 | 笔数 | 胜率 | 平均净收益 | 中位净收益 | 总净盈亏(元) | 最大回撤 | 盈亏比 | 最大单笔盈利 | 最大单笔亏损 "
        "| 最长连亏 | 平均双边滑点 | 规则平均毛收益 | 实盘平均毛收益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in table.iterrows():
        compared = int(row.get("rule_compared", 0) or 0)
        rule = ("%+.2f%%（%d笔）" % (row["avg_rule_gross_return"] * 100, compared)) if compared else "无基准"
        live = ("%+.2f%%" % (row["avg_live_gross_return"] * 100)) if compared else "无基准"
        lines.append("| %s | %d | %.1f%% | %+.2f%% | %+.2f%% | %+.0f | %.2f%% | %.2f | %+.2f%% | %+.2f%% | %d | %.0fbp | %s | %s |" % (
            row["segment"], int(row["sample_count"]), row["win_rate"] * 100, row["avg_return"] * 100,
            row["median_return"] * 100, row["total_net_pnl"], row["hypothetical_max_drawdown"] * 100,
            row["profit_loss_ratio"], row["max_profit"] * 100, row["max_loss"] * 100,
            int(row["max_consecutive_losses"]), row["avg_total_slippage_bps"], rule, live))
    lines += ["", "口径：真实买卖金额扣佣金、过户费、印花税；最大回撤按逐笔净收益连乘；“规则”= 买入日开盘价买、卖出日"
              "收盘价卖，计划卖出日最高价摸到涨停价−0.01时按止盈价卖（不扣费），和“实盘”毛收益之差就是执行成本；样本少时只作参考，不能据此改规则。"]
    return "\n".join(lines) + "\n"


def write_leg_performance(ledger_path: Path, out_dir: Path, report_config: Mapping[str, Any], as_of: str) -> Path:
    ledger = _read(ledger_path)
    table = leg_performance(ledger, report_config)
    _write_atomic(table, out_dir / (PERFORMANCE_NAME + ".csv"))
    md = out_dir / (PERFORMANCE_NAME + ".md")
    tmp = md.with_name(md.name + ".tmp")
    tmp.write_text(_markdown(table, int(len(ledger)), as_of), encoding="utf-8")
    os.replace(tmp, md)
    return md
