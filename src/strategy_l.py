"""策略 L：龙虎榜席位 + 高振幅 + 市场强弱分支（2026-11-26 用户确认落地的优化版）。

规则唯一来源（实盘信号、影子净值、警告回放都调这里）；规则全文与研究依据见 docs/strategy_l.md。
T 为龙虎榜公告日（T 日收盘后可得全部输入），T+1 开盘买入、T+2 收盘卖出（执行与资金见 src/portfolio_ace_m.py）。

本模块只做选股（纯函数，不读文件、不联网）：输入当天的龙虎榜、席位、行情和历史，输出按规则排好序的候选。
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from src.market_rules import (
    is_st_name,
    is_unreformed_name,
    limit_up_price,
    price_limit_pct,
)

L_VERSION = "L_LHB_SEAT_AMPLITUDE_REGIME_OPT_V1_20261126"
STRATEGY_L_LEG = "L"

# ── 研究冻结参数（研究原值；两处为优化版：龙虎榜成交额上限 8.55亿→10亿、卖方普通营业部 2→3 家）──────────
AMPLITUDE_MIN = 0.20117019704281433          # T 日振幅下限（两个分支共用）
REGIME_MEAN_PCT_MIN = -0.1953682431184186     # 市场等权平均涨跌幅（%）≥ 此值用分支一，否则分支二
B1_L_AMOUNT_MAX = 1_000_000_000.0             # 分支一：龙虎榜成交额（元）上限（优化版）
B1_BUY2_PCT_MAX = 0.9104477611940298          # 分支一：买入前两名占买入前五合计的当日分位上限
B2_NAMED_SELL_MIN = 3                         # 分支二：卖出前五普通营业部（去重）下限（优化版）
B2_TOTAL_SHARE_MAX = 83240.3985               # 分支二：总股本（万股）上限
B2_HHI_PCT_MIN = 0.2798564240991567           # 分支二：买入前五集中度的当日分位下限
LOOKBACK_LIMIT_UP = 60                        # 分支一排序：含 T 日的最近 60 个交易日收盘涨停次数
LOOKBACK_BOARD = 60                           # 分支二排序：T 日之前 60 个交易日上榜天数
TURNOVER_WINDOW = 20                          # 分支二次排序：T 日成交额 ÷ 此前 20 日日均成交额
MIN_LISTING_DAY = 6                           # 上市第 1~5 个交易日不选
EXCLUDED_SEAT_NAMES = frozenset({"机构专用", "沪股通专用", "深股通专用"})
A_SHARE_CODE = re.compile(r"^(?:6\d{5}\.SH|(?:000|001|002|003|300|301)\d{3}\.SZ|[489]\d{5}\.BJ)$")


def board_period(reason: Any) -> int:
    """公告期间：单日=1，三日=3；10/30 日、严重异常期间、融资异常等=0（不用）。"""

    r = str(reason or "")
    if "严重异常" in r or "10个交易日" in r or "30个交易日" in r or "融资买入" in r:
        return 0
    if "三个交易日" in r or "3个交易日" in r or "3个有成交的交易日" in r:
        return 3
    return 1


def canonical_board(top_list: pd.DataFrame) -> pd.DataFrame:
    """同一股票同一天只取一条公告：先单日榜、再三日榜；同期间取成交额最大，再按原因文本升序。"""

    if top_list is None or top_list.empty:
        return pd.DataFrame(columns=["ts_code", "name", "amount", "l_amount", "l_sell", "reason", "period"])
    x = top_list[top_list["ts_code"].astype(str).str.match(A_SHARE_CODE, na=False)].copy()
    x["period"] = x["reason"].map(board_period)
    x = x[x["period"].isin((1, 3)) & x["l_sell"].notna()]
    x = x.sort_values(["ts_code", "period", "amount", "reason"], ascending=[True, True, False, True], kind="stable")
    return x.drop_duplicates(["ts_code"], keep="first").reset_index(drop=True)


def seat_metrics(top_inst: pd.DataFrame) -> pd.DataFrame:
    """每个（股票, 上榜原因）的席位指标：买入前两名占比、买入集中度、卖出普通营业部数。"""

    cols = ["ts_code", "reason", "buy2_share", "buy_hhi", "named_sell"]
    if top_inst is None or top_inst.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for (code, reason), g in top_inst.groupby(["ts_code", "reason"], sort=False):
        side = g["side"].astype(str)
        buys = sorted((float(v) for v in g.loc[side == "0", "buy"].fillna(0.0)), reverse=True)
        total = sum(buys)
        sell_rows = g.loc[side == "1"]
        names = {str(n).strip() for n in sell_rows["exalter"] if str(n).strip() and str(n).strip() not in EXCLUDED_SEAT_NAMES}
        rows.append({
            "ts_code": code, "reason": reason,
            "buy2_share": sum(buys[:2]) / total if total > 0 else math.nan,
            "buy_hhi": sum((v / total) ** 2 for v in buys) if total > 0 else math.nan,
            "named_sell": float(len(names)) if len(sell_rows) else math.nan,
        })
    return pd.DataFrame(rows, columns=cols)


class NameHistory:
    """按历史时点给出证券简称（判断当时是否 ST）：当日 QMT 名称快照优先，其次 Tushare 名称变更记录，最后股票基本表的当前名。

    龙虎榜/基本表里的名称是下载时的最新名称，不能拿来判断历史上是否 ST（很多股票是后来才变 ST 的）。
    """

    def __init__(self, namechange: pd.DataFrame | None = None, *, current: Mapping[str, str] | None = None,
                 snapshots: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._current = dict(current or {})
        self._snapshots = {str(d): dict(v) for d, v in (snapshots or {}).items()}
        self._hist: dict[str, tuple[list[str], list[str]]] = {}
        if namechange is not None and not namechange.empty:
            nc = namechange.copy()
            nc["start_date"] = nc["start_date"].astype(str)
            sort_cols = ["ts_code", "start_date"] + (["ann_date"] if "ann_date" in nc.columns else [])
            nc = nc.drop_duplicates(["ts_code", "name", "start_date"]).sort_values(sort_cols)
            for code, g in nc.groupby("ts_code"):
                self._hist[str(code)] = (list(g["start_date"]), [str(n) for n in g["name"]])

    def name_on(self, code: str, day: str) -> str:
        snap = self._snapshots.get(str(day))
        if snap and snap.get(code):
            return str(snap[code])
        rec = self._hist.get(code)
        if rec:
            k = bisect.bisect_right(rec[0], str(day)) - 1
            if k >= 0:
                return rec[1][k]
        return self._current.get(code, "")


@dataclass
class LInputs:
    """一个信号日 T 的全部输入（都是 T 日收盘后已知的信息）。

    days：交易日序列（升序，至少包含 T 及之前 60 个交易日）；
    bars：{交易日: {代码: {open, high, low, close, pre_close, amount, vol}}}，至少覆盖 days 里 T 前 60 天到 T；
    pct_chg_mean：T 日沪深北 A 股等权平均涨跌幅（%），缺失给 None；
    top_list / top_inst：T 日龙虎榜与席位原始表（Tushare 字段）；
    board_history：{交易日: 当天按 canonical_board 规则上榜的代码集合}（T 之前 60 个交易日）；
    total_share：{代码: T 日总股本（万股）}；list_dates：{代码: 上市日 YYYYMMDD}；
    name_on：(代码, 交易日) -> 当时的证券简称（判断 ST 和涨跌停幅度用）。
    """

    signal_date: str
    days: Sequence[str]
    bars: Mapping[str, Mapping[str, Mapping[str, float]]]
    pct_chg_mean: float | None
    top_list: pd.DataFrame
    top_inst: pd.DataFrame
    board_history: Mapping[str, set]
    total_share: Mapping[str, float]
    list_dates: Mapping[str, str]
    name_on: Callable[[str, str], str]
    diagnostics: dict = field(default_factory=dict)


def _p2(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return math.nan
    return round(v, 2) if math.isfinite(v) else math.nan


def _valid(bar: Mapping[str, float] | None) -> bool:
    if not bar:
        return False
    vol, op, cl = (float(bar.get(k) or 0.0) for k in ("vol", "open", "close"))
    return vol > 0 and op > 0 and cl > 0


def listing_day_number(list_date: str, day: str, days: Sequence[str]) -> int | None:
    """上市后的第几个交易日（上市日=1）；上市日早于 days 起点时返回一个足够大的数。"""

    ld = str(list_date or "")
    if len(ld) != 8 or day < ld:
        return None
    idx = {d: i for i, d in enumerate(days)}
    if day not in idx:
        return None
    first = next((i for i, d in enumerate(days) if d >= ld), None)
    if first is None:
        return None
    if ld < days[0]:
        return 10 ** 6
    return idx[day] - first + 1


def _limit_pct(code: str, name: str, day: str, list_date: str, days: Sequence[str]) -> float | None:
    return price_limit_pct(code, name=name, trade_date=day, listing_day_number=listing_day_number(list_date, day, days))


def _closed_limit_up(code: str, day: str, inp: LInputs) -> bool:
    bar = inp.bars.get(day, {}).get(code)
    if not _valid(bar):
        return False
    up = limit_up_price(_p2(bar["pre_close"]), _limit_pct(code, inp.name_on(code, day), day, inp.list_dates.get(code, ""), inp.days))
    return up is not None and _p2(bar["close"]) >= up - 1e-6


def select_l_candidates(inp: LInputs) -> tuple[pd.DataFrame, dict[str, Any]]:
    """按 L 规则返回 T 日候选（第一行是当日首选）与诊断信息。"""

    days = list(inp.days)
    t = inp.signal_date
    if t not in days:
        raise ValueError("signal_date %s not in days" % t)
    i = days.index(t)
    mean = inp.pct_chg_mean
    branch = 1 if (mean is not None and math.isfinite(float(mean)) and float(mean) >= REGIME_MEAN_PCT_MIN) else 2
    board = canonical_board(inp.top_list)
    seats = seat_metrics(inp.top_inst)
    board = board.merge(seats, on=["ts_code", "reason"], how="left")
    board["buy2_pct"] = board["buy2_share"].rank(pct=True, method="average")
    board["hhi_pct"] = board["buy_hhi"].rank(pct=True, method="average")
    diag: dict[str, Any] = {"signal_date": t, "version": L_VERSION, "branch": branch, "market_mean_pct": mean,
                            "board_rows": int(len(board)), "rejects": {}}

    def reject(key: str) -> None:
        diag["rejects"][key] = diag["rejects"].get(key, 0) + 1

    today = inp.bars.get(t, {})
    rows = []
    for r in board.itertuples(index=False):
        code = str(r.ts_code)
        bar = today.get(code)
        if not _valid(bar):
            reject("no_bar")
            continue
        name = inp.name_on(code, t) or str(r.name)
        if is_st_name(name) or is_unreformed_name(name) or "风险警示" in name:
            reject("st")
            continue
        ld = listing_day_number(inp.list_dates.get(code, ""), t, days)
        if ld is None or ld < MIN_LISTING_DAY:
            reject("new_listing")
            continue
        pre = _p2(bar["pre_close"])
        amp = (_p2(bar["high"]) - _p2(bar["low"])) / pre if pre > 0 else math.nan
        if not (amp >= AMPLITUDE_MIN):
            reject("amplitude")
            continue
        if branch == 1:
            if not (float(r.l_amount) <= B1_L_AMOUNT_MAX):
                reject("l_amount")
                continue
            if not (r.buy2_pct <= B1_BUY2_PCT_MAX):
                reject("buy2_pct")
                continue
            if i < LOOKBACK_LIMIT_UP - 1:
                reject("history")
                continue
            cnt = sum(_closed_limit_up(code, days[j], inp) for j in range(i - LOOKBACK_LIMIT_UP + 1, i + 1))
            rows.append({"ts_code": code, "name": name, "amplitude": amp, "branch": 1, "rank_key1": -cnt,
                         "rank_key2": 0.0, "limit_up_count_60": cnt})
        else:
            if not (r.named_sell >= B2_NAMED_SELL_MIN):
                reject("named_sell")
                continue
            ts_val = inp.total_share.get(code)
            if not (ts_val is not None and float(ts_val) <= B2_TOTAL_SHARE_MAX):
                reject("total_share")
                continue
            if not (r.hhi_pct >= B2_HHI_PCT_MIN):
                reject("hhi_pct")
                continue
            if i < LOOKBACK_BOARD:
                reject("history")
                continue
            bc = sum(1 for d in days[i - LOOKBACK_BOARD:i] if code in inp.board_history.get(d, ()))
            prev = [inp.bars.get(d, {}).get(code) for d in days[i - TURNOVER_WINDOW:i]]
            amts = [float(b["amount"]) for b in prev if b is not None and b.get("amount") is not None and math.isfinite(float(b["amount"]))]
            mean_amt = sum(amts) / len(amts) if len(amts) == TURNOVER_WINDOW else math.nan
            tm = float(bar["amount"]) / mean_amt if (math.isfinite(mean_amt) and mean_amt > 0) else math.inf
            rows.append({"ts_code": code, "name": name, "amplitude": amp, "branch": 2, "rank_key1": -bc,
                         "rank_key2": tm, "board_count_60": bc, "turnover_multiple_20": tm})
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["rank_key1", "rank_key2", "ts_code"], kind="stable").reset_index(drop=True)
        out["rank"] = range(1, len(out) + 1)
        out["signal_date"] = t
        out["strategy_leg"] = STRATEGY_L_LEG
        out["l_version"] = L_VERSION
    diag["candidates"] = int(len(out))
    return out, diag


# ── L 自身停手：L 原样单仓 90% 的影子净值（与研究最终推荐用的算法逐位一致）──────────────────
L_SHADOW_POSITION = 0.9      # 影子账每笔用现金的 90%
L_SHADOW_FEE = 0.0007        # 每笔按成交额扣 0.07%（佣金、过户费、印花税的近似；滑点已含在买卖价里）
L_SHADOW_START = "20190402"  # 影子账起点（净值按比例滚动，起点只影响整体缩放，不影响和均线的比较）


def l_shadow_nav(days: Sequence[str], picks_by_buy_day: Mapping[str, Sequence[str]],
                 outcome: Callable[[str, str], Any], *, start: str = L_SHADOW_START) -> pd.Series:
    """逐日 L 影子净值（按已实现记账）：空仓且当日有首选、首选买得进就按现金 90% 开仓，卖出日收盘入账；卖出当天不开新仓。"""

    day_list = [str(d) for d in days]
    position = {d: i for i, d in enumerate(day_list)}
    cash, pos, nav, index = 1.0, None, [], []
    for d in day_list:
        if d < start:
            continue
        i = position[d]
        picks = list(picks_by_buy_day.get(d, []) or [])
        if pos is None and picks:
            res = outcome(str(picks[0]), d)
            if res is not None and str(res[0]) in position:
                pos = (position[str(res[0])], float(res[2]), L_SHADOW_POSITION * cash)
        if pos is not None and i >= pos[0]:
            cash += pos[2] * pos[1] - pos[2] * L_SHADOW_FEE
            pos = None
        nav.append(cash)
        index.append(d)
    return pd.Series(nav, index=index, dtype=float)
