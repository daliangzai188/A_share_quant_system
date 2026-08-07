"""生成策略M历史候选账本，供组合认证脚本复现。

M 的触发依赖"五腿是否全空"，而这要在组合回放中才知道，因此本脚本不预判触发，
只把**所有满足 M 选股条件的交易日**及其候选与收益固化下来；是否真正开仓由
certify_current_executable_portfolio.py 在回放时按空档日和回撤保护决定。

选股口径与实盘 src/strategy_m.py 完全一致：
    情绪门禁 sz_main_market_sentiment_level=weak
    → 数据质量/可买性/非ST 过滤
    → 流通市值最小的一只

收益口径（2026-08-07 起与 A/C 逐字相同，见 build_ac_daily_candidates.trade_return）：
    T+1 开盘买入，买价 open*1.001；T+2 收盘卖出，卖价 close*0.999（双边各0.1%
    滑点+费用）
    T+1 一字涨停（open 与 low 均触及涨停价）视为排队买不到，该日不产生交易，
      **不递补第二名**（与 A/C、D 一致）
    卖出日跌停（当日最高价未超过跌停价）视为卖不出，顺延到 4 个交易日内的第一
      个可卖日

  ⚠️ 2026-08-07 之前只扣买入侧 0.1%、且不判一字板和跌停，池子里因此含有实盘
     根本买不到的交易（20240809 603065.SH、以及另一笔）。修正后 M 池 61→59 天。

运行：
    python3 scripts/build_strategy_m_backtest_pool.py
输出：
    reports/strategy_m/m_backtest_trades.csv
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy_m import DEFAULT_SPEC, build_m_candidate  # noqa: E402

SCORED_PATHS = [
    PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv",
    PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv",
]
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
DAILY_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
OUTPUT_PATH = PROJECT_ROOT / "reports" / "strategy_m" / "m_backtest_trades.csv"

WINDOW_START = "20240520"
WINDOW_END = "20260514"
BUY_COST = 0.001   # 买入滑点+费用
SELL_COST = 0.001  # 卖出滑点+费用（印花税/过户费/佣金/滑点），与 A/C 口径一致
SELL_DELAY_MAX = 4  # 卖出日跌停时最多顺延的交易日数


def load_pool() -> pd.DataFrame:
    frames = []
    for path in SCORED_PATHS:
        if not path.exists():
            continue
        frame = pd.read_csv(path, low_memory=False)
        frame["trade_date"] = (
            frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        )
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("涨停打分池不存在，先跑收盘流水线的采集/清洗/打分步骤。")
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged = merged.drop_duplicates(["trade_date", "ts_code"], keep="last")
    return merged[
        (merged["trade_date"] >= WINDOW_START) & (merged["trade_date"] <= WINDOW_END)
    ].copy()


def trade_days() -> list[str]:
    calendar = pd.read_csv(CALENDAR_PATH, dtype=str)
    return sorted(
        calendar.loc[calendar["is_open"] == "1", "cal_date"].astype(str).tolist()
    )


_daily_cache: dict[str, pd.DataFrame | None] = {}


def daily(date: str) -> pd.DataFrame | None:
    if date not in _daily_cache:
        path = DAILY_DIR / f"{date}.csv"
        if not path.exists():
            _daily_cache[date] = None
        else:
            frame = pd.read_csv(
                path,
                dtype={"ts_code": str},
                usecols=["ts_code", "open", "high", "low", "close", "pre_close"],
                low_memory=False,
            )
            frame["ts_code"] = frame["ts_code"].str.upper()
            _daily_cache[date] = frame.set_index("ts_code")
    return _daily_cache[date]


def price(date: str, code: str, column: str) -> float:
    frame = daily(date)
    if frame is None:
        return float("nan")
    key = str(code).upper()
    if key not in frame.index:
        return float("nan")
    return float(frame.loc[key, column])


def limit_pct(code: str) -> float:
    """创业板/科创板 20%，其余 10%。与 build_ac_daily_candidates.limit_cap 一致。"""

    return 0.20 if str(code)[:3] in {"300", "301", "688"} else 0.10


def is_limit_up_unbuyable(date: str, code: str) -> bool:
    """T+1 一字涨停：开盘即涨停且全天最低价也在涨停价，排队买不到。"""

    pre = price(date, code, "pre_close")
    op = price(date, code, "open")
    low = price(date, code, "low")
    if not np.isfinite(pre) or pre <= 0 or not np.isfinite(op) or not np.isfinite(low):
        return False
    cap = round(pre * (1 + limit_pct(code)), 2)
    return op >= cap - 1e-6 and low >= cap - 1e-6


def is_limit_down_unsellable(date: str, code: str) -> bool:
    """卖出日跌停：当日最高价都没超过跌停价，挂不出去。"""

    pre = price(date, code, "pre_close")
    high = price(date, code, "high")
    if not np.isfinite(pre) or pre <= 0 or not np.isfinite(high):
        return False
    floor = round(pre * (1 - limit_pct(code)), 2)
    return high <= floor + 1e-6


def main() -> None:
    pool = load_pool()
    days = trade_days()
    index = {day: i for i, day in enumerate(days)}
    spec = dict(DEFAULT_SPEC)
    hold = int(spec["exit_hold_offset"])

    rows: list[dict[str, object]] = []
    for signal_date, day_rows in pool.groupby("trade_date"):
        picked, reason = build_m_candidate(day_rows, spec)
        if picked.empty:
            continue
        position = index.get(str(signal_date))
        if position is None or position + hold >= len(days):
            continue
        row = picked.iloc[0]
        code = str(row["ts_code"])
        buy_date = days[position + 1]
        buy_price = price(buy_date, code, "open")
        if not np.isfinite(buy_price) or buy_price <= 0:
            continue
        # T+1 一字涨停排队买不到 → 当日无交易，且不递补第二名（与 A/C、D 一致）
        if is_limit_up_unbuyable(buy_date, code):
            continue
        # 卖出日跌停顺延，最多 SELL_DELAY_MAX 天
        exit_date = None
        for k in range(hold, hold + SELL_DELAY_MAX):
            if position + k >= len(days):
                break
            candidate_exit = days[position + k]
            if not np.isfinite(price(candidate_exit, code, "close")):
                continue
            if is_limit_down_unsellable(candidate_exit, code):
                continue
            exit_date = candidate_exit
            break
        if exit_date is None:
            continue
        exit_price = price(exit_date, code, "close")
        if not np.isfinite(exit_price):
            continue
        net_buy = buy_price * (1 + BUY_COST)
        net_sell = exit_price * (1 - SELL_COST)
        rows.append(
            {
                "trade_date": signal_date,
                "ts_code": code,
                "name": str(row.get("name", "")),
                "market_segment": str(row.get("market_segment", "")),
                "circ_mv": float(pd.to_numeric(row.get("circ_mv"), errors="coerce")),
                "sentiment": str(row.get(spec["sentiment_column"], "")),
                "buy_date": buy_date,
                "buy_price": net_buy,
                "exit_date": exit_date,
                "exit_price": net_sell,
                "net_return": net_sell / net_buy - 1.0,
                "select_reason": reason,
            }
        )

    detail = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    returns = detail["net_return"].values
    print(f"M历史候选账本已生成：{OUTPUT_PATH}")
    print(f"  窗口 {WINDOW_START}~{WINDOW_END}，满足M选股条件的交易日 {len(detail)} 天")
    print(f"  单笔均值 {returns.mean():+.4f}  中位 {np.median(returns):+.4f}  "
          f"胜率 {(returns > 0).mean():.3f}")
    print("  注：这是候选池，实际开仓天数由组合回放按空档日与回撤保护决定。")


if __name__ == "__main__":
    main()
