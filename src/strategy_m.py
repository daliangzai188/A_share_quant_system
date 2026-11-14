from __future__ import annotations

"""策略 M 的唯一规则源（2026-11-12 用户确认上线的 ACE + M 版本）。

M = 20cm 强势涨停接力，剔除一字板和巨量封单，并要求当天 20cm 涨停少于 10 只。
研究依据：~/A_System_复核/2026-11-11_M大法研究/（第七~十四轮，预注册与报告）。

信号日 T 收盘后，只用 T 日及以前已经存在的数据生成次日候选；不读取次日开盘、
退出价格或收益字段。实盘信号脚本与历史对齐脚本都必须调用本模块。

入选条件（全部满足）：
1. T 日涨停收盘（收盘价 = 按前收盘计算的涨停价），20% 涨跌幅品种（创业板 300/301、
   科创板 688/689）；
2. 股票池：当日有成交、非 ST、非北交所、上市满 60 个交易日、近 20 个交易日日均成交额
   （停牌日按 0 计）≥ 2000 万元；
3. 涨停前 5 个交易日（T-5..T-1）累计涨幅 ≥ RET5_MIN（复权口径）；
4. 市场温度：T-1 日涨停（同一股票池口径）、T 日有成交的股票，T 日平均涨幅在 [0, 2%)；
5. 非一字板：T 日最低价 < 涨停价；
6. 封单金额 / 流通市值 < 5%（封单缺失按 0）；
7. T 日股票池内 20cm 涨停数 < 10。

排序：T 日成交额从大到小。T+1 开盘价已涨停的跳过，取下一只；ACE 当日持有的股票跳过。
退出：买入日的下一个交易日（T+2）收盘卖出，跌停或停牌顺延。
"""

import bisect
import math
from typing import Any, Callable, Iterable

import pandas as pd

from src.portfolio_ace_m import M_PCT_BESIDE_ACE, M_PCT_WHEN_ACE_IDLE, TOTAL_POSITION_CAP_PCT  # 仓位只在这一处定义
from src.market_rules import (
    is_st_name,
    limit_up_price,
    market_segment,
    normalize_trade_date,
    price_limit_pct,
)

M_VERSION = "M_20CM_STRONG_RELAY_NO_CONSENSUS_V1_20261112"
STRATEGY_M_LEG = "M"

# 研究冻结参数（第七轮 F9 规则 + 第十三轮 M-b 过滤，数值取自研究代码的精确值）
RET5_MIN = 0.16706779003143313      # 样本内涨停股“前5日涨幅”第80百分位
MOOD_MIN = 0.0                      # 昨日涨停今日平均涨幅下限（含）
MOOD_MAX = 0.02                     # 上限（不含）
N20_MAX_EXCLUSIVE = 10              # 当日20cm涨停数 < 10
FD_RATIO_MAX_EXCLUSIVE = 0.05       # 封单/流通市值 < 5%
AMT20_MIN_THOUSAND = 2e4            # 近20日日均成交额 ≥ 2000万元（单位：千元）
AMT20_WINDOW = 20
MIN_LISTING_DAYS = 60               # 上市满60个交易日（研究口径：age = 交易日序号差 ≥ 60）
RET5_DAYS = 5
UNIVERSE_SEGMENTS = ("sh_main", "sz_main", "chi_next", "star")
TWENTY_CM_SEGMENTS = ("chi_next", "star")

# 仓位与持有（与研究回测一致；仓位以总资产为基数）
POSITION_PCT_WHEN_ACE_IDLE = M_PCT_WHEN_ACE_IDLE  # ACE 当日无交易且无持仓时
POSITION_PCT_BESIDE_ACE = M_PCT_BESIDE_ACE  # ACE 持仓或当日开仓时，M 只用闲置资金
TOTAL_POSITION_CAP = TOTAL_POSITION_CAP_PCT  # ACE + M 合计上限
EXIT_RULE = "FIXED_T2_CLOSE"
HOLD_OFFSET = 2                     # 信号日后第2个交易日收盘卖出（买入日后1个交易日）

DAILY_COLUMNS = ("ts_code", "open", "high", "low", "close", "pre_close", "amount", "vol")


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out


def listing_age(list_date: str, trade_date: str, trade_dates: list[str]) -> int | None:
    """研究口径的上市天数：交易日序号(T) - 交易日序号(上市日或其后第一个交易日)。

    trade_dates 必须升序；上市日早于日历起点时按日历起点计（研究面板同口径）。
    """

    listed = normalize_trade_date(list_date)
    if len(listed) != 8 or trade_date not in trade_dates or listed > trade_date:
        return None
    return trade_dates.index(trade_date) - bisect.bisect_left(trade_dates, listed)


def _limit_up(code: str, pre_close: float, trade_date: str, age: int | None, st: bool) -> float | None:
    if not (pre_close > 0):
        return None
    name = "ST" if st else ""
    pct = price_limit_pct(code, name=name, trade_date=trade_date,
                          listing_day_number=(age + 1) if age is not None else None)
    return limit_up_price(pre_close, pct)


def _daily_index(frame: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    if frame is None or frame.empty:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in frame.to_dict("records"):
        code = str(row.get("ts_code") or "")
        if not code:
            continue
        rec = {k: _num(row.get(k)) for k in DAILY_COLUMNS if k != "ts_code"}
        if not (rec["vol"] > 0):
            continue
        out[code] = rec
    return out


def build_m_candidates(
    signal_date: str,
    trade_dates: Iterable[str],
    daily_by_date: dict[str, pd.DataFrame],
    limit_today: pd.DataFrame | None,
    basic_today: pd.DataFrame | None,
    list_dates: dict[str, str],
    is_st: Callable[[str, str], bool],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """返回 (按成交额降序的候选表, 诊断信息)。

    daily_by_date: 至少包含 T-19..T 共20个交易日的日线（列见 DAILY_COLUMNS，amount 单位千元）。
    limit_today:   T 日涨停明细，至少含 ts_code、fd_amount；缺失按封单 0 处理。
    basic_today:   T 日每日指标，至少含 ts_code、circ_mv（万元）。
    list_dates:    ts_code -> 上市日期 YYYYMMDD；缺失视为上市不足（排除）。
    is_st:         (ts_code, trade_date) -> 是否 ST。
    任何必需数据缺失都抛出 ValueError（fail-closed，由调用方决定不生成计划）。
    """

    dates = [str(d) for d in trade_dates]
    if signal_date not in dates:
        raise ValueError("signal_date %s not in trade calendar" % signal_date)
    t = dates.index(signal_date)
    if t < AMT20_WINDOW:
        raise ValueError("not enough trade days before %s" % signal_date)
    window = dates[t - AMT20_WINDOW + 1:t + 1]
    missing = [d for d in window if d not in daily_by_date or daily_by_date[d] is None or daily_by_date[d].empty]
    if missing:
        raise ValueError("daily data missing for %s" % ",".join(missing))
    idx = {d: _daily_index(daily_by_date[d]) for d in window}
    today, yday = idx[signal_date], idx[dates[t - 1]]
    if len(today) < 1000 or len(yday) < 1000:
        raise ValueError("daily data incomplete for %s/%s" % (signal_date, dates[t - 1]))

    ages: dict[str, int | None] = {}
    pos = {d: i for i, d in enumerate(dates)}

    def age_of(code: str, day: str) -> int | None:
        key = code + "|" + day
        if key not in ages:
            ld = normalize_trade_date(list_dates.get(code) or "")
            ages[key] = (pos[day] - bisect.bisect_left(dates, ld)) if len(ld) == 8 and ld <= day else None
        return ages[key]

    def in_universe(code: str, day: str) -> bool:
        if market_segment(code) not in UNIVERSE_SEGMENTS:
            return False
        if is_st(code, day):
            return False
        age = age_of(code, day)
        return age is not None and age >= MIN_LISTING_DAYS

    def is_limit_up(code: str, bar: dict[str, float], day: str) -> bool:
        up = _limit_up(code, bar["pre_close"], day, age_of(code, day), False)
        return up is not None and round(bar["close"], 2) >= up - 1e-6

    # 4. 市场温度：昨日涨停（股票池口径）今日平均涨幅
    yday_lu = [c for c, b in yday.items() if in_universe(c, dates[t - 1]) and is_limit_up(c, b, dates[t - 1])]
    mood_values = [today[c]["close"] / today[c]["pre_close"] - 1 for c in yday_lu
                   if c in today and today[c]["pre_close"] > 0]
    mood = sum(mood_values) / len(mood_values) if mood_values else float("nan")

    # 7. 当日股票池内20cm涨停数
    today_lu = [c for c, b in today.items() if in_universe(c, signal_date) and is_limit_up(c, b, signal_date)]
    lu20 = [c for c in today_lu if market_segment(c) in TWENTY_CM_SEGMENTS]
    n20 = len(lu20)

    diag: dict[str, Any] = {
        "signal_date": signal_date, "version": M_VERSION, "mood": mood, "mood_samples": len(mood_values),
        "n20_limit_up": n20, "limit_up_universe": len(today_lu),
        "mood_ok": bool(mood == mood and MOOD_MIN <= mood < MOOD_MAX), "n20_ok": n20 < N20_MAX_EXCLUSIVE,
        "rejects": {},
    }
    cols = ["signal_date", "ts_code", "amount", "close", "limit_up_price", "ret5", "amt20", "fd_amount",
            "circ_mv", "fd_ratio", "low", "mood", "n20_limit_up", "rank", "strategy_leg", "m_version"]
    if not diag["mood_ok"] or not diag["n20_ok"]:
        return pd.DataFrame(columns=cols), diag

    fd = {}
    if limit_today is not None and not limit_today.empty:
        for row in limit_today.to_dict("records"):
            fd[str(row.get("ts_code"))] = _num(row.get("fd_amount"))
    circ = {}
    if basic_today is not None and not basic_today.empty:
        for row in basic_today.to_dict("records"):
            circ[str(row.get("ts_code"))] = _num(row.get("circ_mv"))

    rejects: dict[str, int] = {}

    def reject(key: str) -> None:
        rejects[key] = rejects.get(key, 0) + 1

    rows = []
    for code in lu20:
        bar = today[code]
        amt20 = sum(idx[d][code]["amount"] if code in idx[d] else 0.0 for d in window) / float(AMT20_WINDOW)
        if not (amt20 >= AMT20_MIN_THOUSAND):
            reject("amt20")
            continue
        growth = 1.0
        for d in dates[t - RET5_DAYS:t]:
            b = idx[d].get(code)
            if b is not None and b["pre_close"] > 0:
                growth *= b["close"] / b["pre_close"]
        ret5 = growth - 1
        if not (ret5 >= RET5_MIN):
            reject("ret5")
            continue
        up = _limit_up(code, bar["pre_close"], signal_date, age_of(code, signal_date), False)
        if round(bar["low"], 2) >= up - 1e-6:
            reject("one_word")
            continue
        fd_amount = fd.get(code, float("nan"))
        cm = circ.get(code, float("nan"))
        ratio = fd_amount / (cm * 1e4) if (fd_amount == fd_amount and cm == cm and cm > 0) else 0.0
        if not math.isfinite(ratio):
            ratio = 0.0
        if ratio >= FD_RATIO_MAX_EXCLUSIVE:
            reject("fd_ratio")
            continue
        rows.append({"signal_date": signal_date, "ts_code": code, "amount": bar["amount"], "close": bar["close"],
                     "limit_up_price": up, "ret5": ret5, "amt20": amt20, "fd_amount": fd_amount, "circ_mv": cm,
                     "fd_ratio": ratio, "low": bar["low"], "mood": mood, "n20_limit_up": n20,
                     "strategy_leg": STRATEGY_M_LEG, "m_version": M_VERSION})
    diag["rejects"] = rejects
    out = pd.DataFrame(rows, columns=[c for c in cols if c != "rank"])
    if not out.empty:
        out = out.sort_values(["amount", "ts_code"], ascending=[False, True]).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    return out[cols], diag


def name_based_st(names: dict[str, str]) -> Callable[[str, str], bool]:
    """实盘用：按当前名称判断 ST（QMT 原生涨停池与 stock_basic 同口径）。"""

    def check(code: str, _day: str) -> bool:
        return is_st_name(names.get(code, ""))

    return check
