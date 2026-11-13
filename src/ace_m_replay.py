"""ACE+M 按现行规则和仓位的单账户回放（只读，给三级警告的“整体组合”“M”判定用）。

逻辑与2026-11-12上线依据的研究回放（``~/A_System_复核/2026-11-11_M大法研究/scripts/m14_verify.py``
的 ``simulate``）逐项一致；用研究的输入重放能逐位复现研究结果（全期295,345.59倍）：

- 每个行动日先把卖出日已过的持仓按卖出所得入账；当日可用基数=现金+在持仓位成本；
- ACE：当天A>C>E的第一条计划；方案甲允许时目标82.5%，停手日按 ``ace_halted_weight``
  （现行20%；“只用ACE”即甲v1为0，停手日不开），且不超过99.5%−M占用；账户里已有ACE仓位
  （含计划成立但买不进、仍按规则占位的）时不开新的；
- M：账户里没有M时，按候选单排名买第一只开盘没涨停、之后有收盘能卖出的，跳过ACE持有的股票；
  ACE当天有计划或已有ACE持仓时最多17.5%（且不超过99.5%−ACE占用），否则82.5%；
- 买卖：数量按交易所整手规则向下取整，佣金万三最低5元、过户费十万分之一、印花税按卖出日。
权益按成本记账（卖出日入账），不按每日市值估值。
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from src.market_rules import (
    fixed_close_sell_executable,
    fixed_open_buy_executable,
    market_segment,
    orderable_buy_quantity,
    price_limit_pct,
)
from src.trading_fees import stamp_tax_rate_for_date

ACE_FULL_WEIGHT = 0.825
ACE_HALTED_WEIGHT = 0.20
M_IDLE_WEIGHT = 0.825
M_BESIDE_ACE_WEIGHT = 0.175
TOTAL_WEIGHT_CAP = 0.995
M_SLIPPAGE = 0.001
ACE_LEGS = ("A", "C", "E")
M_EXIT_SCAN_DAYS = 60


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "1.0")


def ace_plans_by_day(legs: Mapping[str, pd.DataFrame], order: Sequence[str] = ACE_LEGS) -> dict[str, list[dict]]:
    """影子账各腿计划按行动日归并，同一天按A>C>E排列（与研究回放同序）。"""

    by_day: dict[str, list[dict]] = {}
    for leg in order:
        frame = legs.get(leg)
        if frame is None or frame.empty or "action_date" not in frame.columns:
            continue
        for record in frame.to_dict("records"):
            by_day.setdefault(str(record["action_date"]), []).append(record)
    return by_day


def replay_ace_m(
    days: Sequence[str],
    ace_plans: Mapping[str, Sequence[Mapping[str, Any]]],
    ace_allowed: Mapping[str, bool],
    m_candidates: Mapping[str, Sequence[str]],
    m_outcome: Callable[[str, str], Any],
    *,
    use_m: bool = True,
    ace_halted_weight: float = ACE_HALTED_WEIGHT,
    cash0: float = 500000.0,
    commission_rate: float = 0.0003,
    minimum_commission: float = 5.0,
    transfer_rate: float = 0.00001,
    stamp_schedule: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    """逐个行动日回放，返回（按成本记账的权益序列，逐笔交易）。

    m_outcome(代码, 买入日) 返回 (卖出日, 买入价, 含滑点复权后的个股收益) 或 None
    （开盘涨停买不进、停牌、或之后还没有能卖出的收盘）。
    """

    def commission(gross: float) -> float:
        return max(minimum_commission, gross * commission_rate)

    cash = float(cash0)
    positions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity: list[float] = []
    last_day = str(days[-1]) if len(days) else ""
    for day in days:
        day = str(day)
        still = []
        for position in positions:
            if day > position["until"]:
                cash += position["proceeds"]
            else:
                still.append(position)
        positions = still
        base = cash + sum(p["cost"] for p in positions)

        def used(family: str) -> float:
            return sum(p["cost"] for p in positions if p["family"] == family) / base if base > 0 else 0.0

        def holds(family: str) -> bool:
            return any(p["family"] == family for p in positions)

        def buy(family: str, leg: str, code: str, entry: float, stock_return: float, exit_date: str,
                weight: float) -> bool:
            nonlocal cash
            quantity = orderable_buy_quantity(ts_code=code, available_amount=min(base * weight, cash),
                                              execution_price=entry)
            step = 1 if market_segment(code) in ("star", "bj") else 100
            while quantity > 0 and (quantity * entry + commission(quantity * entry)
                                    + quantity * entry * transfer_rate > cash + 1e-9):
                quantity -= step
            if quantity <= 0:
                return False
            gross = quantity * entry
            cost = gross + commission(gross) + gross * transfer_rate
            sell = gross * (1 + stock_return)
            proceeds = (sell - commission(sell) - sell * transfer_rate
                        - sell * stamp_tax_rate_for_date(exit_date, stamp_schedule))
            cash -= cost
            positions.append({"family": family, "code": code, "until": exit_date, "cost": cost,
                              "proceeds": proceeds})
            trades.append({"action_date": day, "exit_date": exit_date, "family": family, "leg": leg,
                           "ts_code": code, "ret": proceeds / cost - 1, "weight": cost / base})
            return True

        ace_today = False
        plans = list(ace_plans.get(day, []) or [])
        if plans:
            weight = ACE_FULL_WEIGHT if ace_allowed.get(day, True) else float(ace_halted_weight)
            if weight > 0:
                ace_today = True
                first = plans[0]
                if not holds("ACE") and _truthy(first.get("position_opened")):
                    if _truthy(first.get("outcome_observable")) and str(first.get("status")) == "OK":
                        buy("ACE", str(first.get("strategy_leg", "")), str(first["ts_code"]),
                            float(first["entry_price"]), float(first["stock_return_before_fees"]),
                            str(first["exit_date"]), min(weight, TOTAL_WEIGHT_CAP - used("M")))
                    else:
                        until = str(first.get("position_open_until") or "") or last_day
                        positions.append({"family": "ACE", "code": "", "until": until, "cost": 0.0,
                                          "proceeds": 0.0})
        if use_m and not holds("M") and m_candidates.get(day):
            held = {p["code"] for p in positions if p["family"] == "ACE"}
            if not ace_today and not holds("ACE"):
                weight = M_IDLE_WEIGHT
            else:
                weight = min(M_BESIDE_ACE_WEIGHT, TOTAL_WEIGHT_CAP - used("ACE"))
            if weight > 0.001:
                for code in m_candidates[day]:
                    if code in held:
                        continue
                    outcome = m_outcome(code, day)
                    if outcome is None:
                        continue
                    exit_date, entry, stock_return = outcome
                    if buy("M", "M", code, float(entry), float(stock_return), str(exit_date), weight):
                        break
        equity.append(cash + sum(p["cost"] for p in positions))
    return pd.Series(equity, index=[str(d) for d in days], dtype=float), pd.DataFrame(
        trades, columns=["action_date", "exit_date", "family", "leg", "ts_code", "ret", "weight"])


class RawPrices:
    """按交易日读原始日线和复权因子（只读，带缓存），给M的买卖结果用。"""

    def __init__(self, raw_dir: Path | str, *, cached_days: int = 32) -> None:
        self.raw = Path(raw_dir)
        self._cached_days = int(cached_days)
        self._bars = OrderedDict()
        self._adj = OrderedDict()

    def _load(self, cache: OrderedDict, path: Path, columns: list[str]) -> dict[str, dict]:
        key = str(path)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        rows: dict[str, dict] = {}
        if path.exists():
            frame = pd.read_csv(path, dtype={"ts_code": str}, encoding="utf-8-sig")
            missing = [c for c in columns if c not in frame.columns]
            if missing:
                raise ValueError("%s 缺少列 %s" % (path, ",".join(missing)))
            for record in frame[columns].to_dict("records"):
                rows[str(record["ts_code"])] = record
        cache[key] = rows
        while len(cache) > self._cached_days:
            cache.popitem(last=False)
        return rows

    def bar(self, day: str, code: str) -> dict | None:
        rows = self._load(self._bars, self.raw / "daily" / ("%s.csv" % day),
                          ["ts_code", "open", "close", "pre_close", "vol"])
        return rows.get(code)

    def adj(self, day: str, code: str) -> float | None:
        rows = self._load(self._adj, self.raw / "adj_factor" / ("%s.csv" % day), ["ts_code", "adj_factor"])
        record = rows.get(code)
        if not record:
            return None
        value = float(record["adj_factor"])
        return value if value == value and value > 0 else None


def _traded(bar: Mapping[str, Any] | None) -> bool:
    if not bar:
        return False
    close, volume = bar.get("close"), bar.get("vol")
    return close == close and close is not None and volume == volume and volume is not None and float(volume) > 0


def make_m_outcome(prices: RawPrices, days: Sequence[str], *, slippage: float = M_SLIPPAGE) -> Callable[[str, str], Any]:
    """M买入日开盘买（开盘已涨停或停牌则买不进），之后第一个收盘没跌停的交易日收盘卖。"""

    position = {str(day): i for i, day in enumerate(days)}

    def outcome(code: str, buy_date: str):
        bar = prices.bar(buy_date, code)
        if not _traded(bar):
            return None
        pct = price_limit_pct(code, trade_date=buy_date)
        if not fixed_open_buy_executable(pre_close=round(float(bar["pre_close"]), 2),
                                         open_price=round(float(bar["open"]), 2), limit_pct=pct):
            return None
        adj_buy = prices.adj(buy_date, code)
        if adj_buy is None or position.get(str(buy_date)) is None:
            return None
        start = position[str(buy_date)] + 1
        for day in list(days)[start:start + M_EXIT_SCAN_DAYS]:
            later = prices.bar(day, code)
            if not _traded(later):
                continue
            if fixed_close_sell_executable(pre_close=round(float(later["pre_close"]), 2),
                                           close_price=round(float(later["close"]), 2),
                                           limit_pct=price_limit_pct(code, trade_date=day)):
                adj_exit = prices.adj(day, code)
                if adj_exit is None:
                    return None
                entry = float(bar["open"])
                stock_return = (float(later["close"]) * adj_exit * (1 - slippage)
                                / (entry * adj_buy * (1 + slippage)) - 1)
                return str(day), entry * (1 + slippage), stock_return
        return None

    return outcome


def m_candidate_codes(reference: Callable[[str], Mapping[str, Any]], days: Iterable[str]) -> tuple[dict, dict]:
    """每个买入日的M候选代码（按排名）；返回（候选，算不出来的日子及原因）。"""

    candidates: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for day in days:
        result = reference(str(day))
        if str(result.get("status", "")) != "OK":
            errors[str(day)] = str(result.get("error", ""))
            continue
        codes = [str(c["ts_code"]) for c in result.get("candidates") or []]
        if codes:
            candidates[str(day)] = codes
    return candidates, errors
