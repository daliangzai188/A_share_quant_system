"""M候选单的只读重算，给“实盘逐笔对账”当参照。

实盘的M信号文件只存在于QMT内部，QMT外的对账程序读不到，所以按同一套规则
（``src.strategy_m.build_m_candidates``）用原始数据把买入日的候选单重算一遍。读数口径与
``scripts/run_strategy_m_signal.py`` 逐项一致，由测试保证两者候选相同。本模块只读不写，
不参与实盘信号生成。

与QMT内信号的已知差别（只影响对账，不影响交易）：
- QMT用当日原生名称表取名称和上市日；这里用stock_basic快照，快照之后新上市的股票
  用它第一次出现在日线里的那天当上市日；
- 封单金额、成交额来自Tushare，与QMT行情可能有细微出入。
所以对账结论里要写明“候选单是重算的”。
"""
from __future__ import annotations

from collections import OrderedDict
import csv
from pathlib import Path
from typing import Any

import pandas as pd

from src.market_rules import is_st_name, limit_up_price, market_segment, price_limit_pct
from src.strategy_m import (
    AMT20_WINDOW,
    DAILY_COLUMNS,
    MIN_LISTING_DAYS,
    UNIVERSE_SEGMENTS,
    build_m_candidates,
    listing_age,
    name_based_st,
)


def _read_table(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str, "list_date": str}, encoding="utf-8-sig")
    if columns:
        missing = [c for c in columns if c not in frame.columns]
        if missing:
            raise ValueError("%s 缺少列 %s" % (path, ",".join(missing)))
        frame = frame[columns]
    return frame


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


class MReference:
    """按买入日给出M候选单参照：``reference(buy_date)`` 返回一个字典，从不抛异常。

    status为OK时，candidates是信号日收盘后按排名的候选（可以为空），每只带买入日的
    开盘价、前收盘价和涨停价（当天停牌则为None）；任何必需数据缺失都返回status=ERROR
    和原因，调用方应当把它当作“这一笔没有被检查”。
    """

    def __init__(self, raw_dir: Path | str, *, cached_days: int = 64) -> None:
        self.raw = Path(raw_dir)
        self._cached_days = int(cached_days)
        self._days = None
        self._basic = None
        self._daily = OrderedDict()
        self._first_seen = {}
        self._first_seen_through = ""
        self._results = {}

    def __call__(self, buy_date: str) -> dict[str, Any]:
        key = str(buy_date)
        if key not in self._results:
            try:
                self._results[key] = self._build(key)
            except Exception as exc:
                self._results[key] = {
                    "buy_date": key, "signal_date": "", "status": "ERROR",
                    "error": "%s: %s" % (type(exc).__name__, exc), "candidates": [],
                }
        return self._results[key]

    def open_days(self) -> list[str]:
        if self._days is None:
            days = []
            with (self.raw / "trade_calendar.csv").open(encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    if str(row.get("is_open", "")).strip() in ("1", "1.0", "True", "true"):
                        days.append(str(row.get("cal_date", "")).strip())
            self._days = sorted(set(d for d in days if len(d) == 8))
        return self._days

    def _daily_frame(self, day: str) -> pd.DataFrame:
        if day in self._daily:
            self._daily.move_to_end(day)
            return self._daily[day]
        frame = _read_table(self.raw / "daily" / ("%s.csv" % day), list(DAILY_COLUMNS))
        self._daily[day] = frame
        while len(self._daily) > self._cached_days:
            self._daily.popitem(last=False)
        return frame

    def _basic_names_and_list_dates(self) -> tuple[dict[str, str], dict[str, str]]:
        if self._basic is None:
            names = {}
            list_dates = {}
            basic = _read_table(self.raw / "stock_basic" / "stock_basic_all.csv")
            if not basic.empty:
                for row in basic.to_dict("records"):
                    code = str(row.get("ts_code") or "")
                    names[code] = str(row.get("name") or "")
                    list_dates[code] = str(row.get("list_date") or "")
            self._basic = (names, list_dates)
        return self._basic

    def _listed_after_snapshot(self, through: str, known: dict[str, str]) -> dict[str, str]:
        """stock_basic快照里没有的股票：用它第一次出现在日线里的那天当上市日。

        只需要看快照最大上市日之后的日线（只读代码列）；更早上市的股票快照里都有。
        """

        valid = [d for d in known.values() if len(d) == 8 and d.isdigit()]
        if not valid:
            return self._first_seen
        scanned = max(max(valid), self._first_seen_through)
        for day in self.open_days():
            if day <= scanned or day > through:
                continue
            path = self.raw / "daily" / ("%s.csv" % day)
            if path.exists():
                codes = pd.read_csv(path, usecols=["ts_code"], dtype={"ts_code": str}, encoding="utf-8-sig")
                for code in codes["ts_code"].dropna():
                    if code not in known:
                        self._first_seen.setdefault(code, day)
            self._first_seen_through = day
        return self._first_seen

    def _names_and_list_dates(self, signal_date: str) -> tuple[dict[str, str], dict[str, str]]:
        base_names, base_list_dates = self._basic_names_and_list_dates()
        names = dict(base_names)
        list_dates = dict(base_list_dates)
        native = self.raw / "stock_names" / ("%s.csv" % signal_date)
        if native.exists():
            for row in _read_table(native).to_dict("records"):
                code = str(row.get("ts_code") or "")
                if row.get("name") == row.get("name") and str(row.get("name") or ""):
                    names[code] = str(row.get("name"))
                listed = str(row.get("list_date") or "")[:8]
                if len(listed) == 8 and listed.isdigit():
                    list_dates[code] = listed
        for code, listed in self._listed_after_snapshot(signal_date, base_list_dates).items():
            list_dates.setdefault(code, listed)
        return names, list_dates

    def _signal_candidates(
        self, signal_date: str, days: list[str]
    ) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
        """与实盘信号脚本 run() 同样的读数、同样的完整性检查、同一个规则函数。"""

        t = days.index(signal_date)
        if t < AMT20_WINDOW:
            raise ValueError("交易日历不足 %d 天" % AMT20_WINDOW)
        window = days[t - AMT20_WINDOW + 1:t + 1]
        daily = {d: self._daily_frame(d) for d in window}
        missing = [d for d in window if daily[d].empty]
        if missing:
            raise ValueError("日线缺失：%s" % ",".join(missing))
        limit_today = _read_table(self.raw / "limit_list" / ("%s.csv" % signal_date))
        if limit_today.empty:
            raise ValueError("涨停池缺失：%s" % signal_date)
        if "fd_amount" not in limit_today.columns:
            raise ValueError("涨停池缺少 fd_amount")
        basic_today = _read_table(self.raw / "daily_basic" / ("%s.csv" % signal_date))
        if basic_today.empty or "circ_mv" not in basic_today.columns:
            raise ValueError("每日指标缺失或缺少 circ_mv：%s" % signal_date)
        limit_yday = _read_table(self.raw / "limit_list" / ("%s.csv" % days[t - 1]))

        names, list_dates = self._names_and_list_dates(signal_date)
        for frame in (limit_today, limit_yday):
            if not frame.empty and "name" in frame.columns:
                for row in frame[["ts_code", "name"]].to_dict("records"):
                    if str(row.get("name") or ""):
                        names[str(row["ts_code"])] = str(row["name"])

        for day, pool in ((days[t - 1], limit_yday), (signal_date, limit_today)):
            if pool.empty:
                if day != signal_date:
                    raise ValueError("昨日涨停池缺失：%s" % day)
                continue
            rows = pool
            if "limit" in rows.columns:
                rows = rows[rows["limit"].astype(str).str.upper().eq("U")]
            eligible = set()
            for code in rows["ts_code"].dropna().astype(str):
                if market_segment(code) not in UNIVERSE_SEGMENTS or is_st_name(names.get(code, "")):
                    continue
                age = listing_age(list_dates.get(code, ""), day, days)
                if age is not None and age >= MIN_LISTING_DAYS:
                    eligible.add(code)
            absent = sorted(eligible - set(daily[day]["ts_code"].astype(str)))
            if absent:
                raise ValueError(
                    "%s 日线缺少涨停池股票%d只（%s），数据不完整" % (day, len(absent), "、".join(absent[:5]))
                )

        candidates, _diag = build_m_candidates(
            signal_date, days, daily, limit_today, basic_today, list_dates, name_based_st(names)
        )
        return candidates, names, list_dates

    def _build(self, buy_date: str) -> dict[str, Any]:
        days = self.open_days()
        if buy_date not in days:
            raise ValueError("买入日 %s 不是交易日" % buy_date)
        position = days.index(buy_date)
        if position == 0:
            raise ValueError("买入日 %s 之前没有交易日" % buy_date)
        signal_date = days[position - 1]
        candidates, names, list_dates = self._signal_candidates(signal_date, days)
        result = {"buy_date": buy_date, "signal_date": signal_date, "status": "OK", "error": "", "candidates": []}
        if candidates.empty:
            return result

        buy_frame = self._daily_frame(buy_date)
        if buy_frame.empty:
            raise ValueError("买入日日线缺失：%s" % buy_date)
        bars = {str(row.get("ts_code") or ""): row for row in buy_frame.to_dict("records")}
        for row in candidates.to_dict("records"):
            code = str(row["ts_code"])
            name = names.get(code, "")
            bar = bars.get(code) or {}
            pre_close = _positive(bar.get("pre_close"))
            age = listing_age(list_dates.get(code, ""), buy_date, days)
            pct = price_limit_pct(
                code, name=name, trade_date=buy_date,
                listing_day_number=(age + 1) if age is not None else None,
            )
            result["candidates"].append({
                "rank": int(row["rank"]),
                "ts_code": code,
                "name": name,
                "buy_open": _positive(bar.get("open")),
                "buy_pre_close": pre_close,
                "buy_limit_up": limit_up_price(pre_close, pct) if pre_close is not None else None,
            })
        return result
