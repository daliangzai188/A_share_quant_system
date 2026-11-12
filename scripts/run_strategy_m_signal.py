"""
策略 M 每日收盘后信号生成脚本（2026-11-12 起，ACE + M）。

规则唯一来源：src/strategy_m.py（20cm 强势涨停接力，剔除一字板和巨量封单，当天 20cm 涨停 < 10 只）。
本脚本只负责读取信号日 T 及以前的数据、调用规则、落盘候选；不读取次日任何数据。

输入：
  data/raw/daily/YYYYMMDD.csv        T-19..T 共 20 个交易日日线（amount 千元）
  data/raw/daily_basic/T.csv         流通市值 circ_mv（万元）
  data/raw/limit_list/T.csv          封单金额 fd_amount（缺失按 0）
  data/raw/stock_names/T.csv         QMT 原生名称与上市日（有则优先；无则用 stock_basic）
  data/raw/stock_basic/stock_basic_all.csv
  data/raw/trade_calendar.csv

输出：
  reports/strategy_m/m_signals_recent.json      最近 10 个信号日（候选按成交额降序）
  reports/strategy_m/m_signal_runs_recent.json  每日运行终态（SIGNAL_READY / NO_CANDIDATE / ERROR）
  reports/strategy_m/m_signal_YYYYMMDD_candidates.csv

任何必需数据缺失都写 ERROR 并以非零退出（fail-closed：M 次日不开仓，不影响 ACE）。

用法：
  python scripts/run_strategy_m_signal.py --signal-date 20261112
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rolling_signal_store import ERROR, NO_CANDIDATE, SIGNAL_READY, save_recent_signal, save_recent_signal_run
from src.market_rules import is_st_name, market_segment
from src.strategy_m import (
    AMT20_WINDOW,
    EXIT_RULE,
    HOLD_OFFSET,
    MIN_LISTING_DAYS,
    M_VERSION,
    STRATEGY_M_LEG,
    UNIVERSE_SEGMENTS,
    build_m_candidates,
    listing_age,
    name_based_st,
)

RAW = PROJECT_ROOT / "data" / "raw"
CALENDAR_PATH = RAW / "trade_calendar.csv"
STOCK_BASIC_PATH = RAW / "stock_basic" / "stock_basic_all.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_m"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "m_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "m_signal_runs_recent.json"
DAILY_COLUMNS = ["ts_code", "open", "high", "low", "close", "pre_close", "amount", "vol"]


def load_open_days() -> list[str]:
    days = []
    with CALENDAR_PATH.open(encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if str(row.get("is_open", "")).strip() in ("1", "1.0", "True", "true"):
                days.append(str(row.get("cal_date", "")).strip())
    return sorted(set(d for d in days if len(d) == 8))


def read_table(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str, "list_date": str}, encoding="utf-8-sig")
    if columns:
        missing = [c for c in columns if c not in frame.columns]
        if missing:
            raise ValueError("%s 缺少列 %s" % (path, ",".join(missing)))
        frame = frame[columns]
    return frame


def load_names_and_list_dates(signal_date: str) -> tuple[dict[str, str], dict[str, str], str]:
    names: dict[str, str] = {}
    list_dates: dict[str, str] = {}
    source = "stock_basic"
    basic = read_table(STOCK_BASIC_PATH)
    if not basic.empty:
        for row in basic.to_dict("records"):
            code = str(row.get("ts_code") or "")
            names[code] = str(row.get("name") or "")
            list_dates[code] = str(row.get("list_date") or "")
    native = RAW / "stock_names" / ("%s.csv" % signal_date)
    if native.exists():
        source = "qmt_native+stock_basic"
        for row in read_table(native).to_dict("records"):
            code = str(row.get("ts_code") or "")
            if row.get("name") == row.get("name") and str(row.get("name") or ""):
                names[code] = str(row.get("name"))
            ld = str(row.get("list_date") or "")[:8]
            if len(ld) == 8 and ld.isdigit():
                list_dates[code] = ld
    return names, list_dates, source


def next_open_day(days: list[str], signal_date: str) -> str:
    later = [d for d in days if d > signal_date]
    return later[0] if later else ""


def run(signal_date: str) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        days = load_open_days()
        if signal_date not in days:
            raise ValueError("信号日 %s 不是交易日" % signal_date)
        t = days.index(signal_date)
        if t < AMT20_WINDOW:
            raise ValueError("交易日历不足 %d 天" % AMT20_WINDOW)
        window = days[t - AMT20_WINDOW + 1:t + 1]
        daily = {d: read_table(RAW / "daily" / ("%s.csv" % d), DAILY_COLUMNS) for d in window}
        missing = [d for d, f in daily.items() if f.empty]
        if missing:
            raise ValueError("日线缺失：%s" % ",".join(missing))
        limit_today = read_table(RAW / "limit_list" / ("%s.csv" % signal_date))
        if limit_today.empty:
            raise ValueError("涨停池缺失：%s" % signal_date)
        if "fd_amount" not in limit_today.columns:
            raise ValueError("涨停池缺少 fd_amount")
        basic_today = read_table(RAW / "daily_basic" / ("%s.csv" % signal_date))
        if basic_today.empty or "circ_mv" not in basic_today.columns:
            raise ValueError("每日指标缺失或缺少 circ_mv：%s" % signal_date)
        limit_yday = read_table(RAW / "limit_list" / ("%s.csv" % days[t - 1]))
        names, list_dates, name_source = load_names_and_list_dates(signal_date)
        for frame in (limit_today, limit_yday):
            if not frame.empty and "name" in frame.columns:
                for row in frame[["ts_code", "name"]].to_dict("records"):
                    if str(row.get("name") or ""):
                        names[str(row["ts_code"])] = str(row["name"])
        # 数据完整性（fail-closed，不改变选股规则）：M股票池内（非ST、非北交所、上市满60个交易日）的
        # 涨停池股票当天一定有成交，必须都在当日日线里。市场温度用“昨日涨停股今日涨幅”，任何一只缺行情
        # 都会使均值失真（如QMT重启后从本地缓存重建的历史日线漏票），宁可当天M不开仓。
        for day, lim in ((days[t - 1], limit_yday), (signal_date, limit_today)):
            if lim.empty:
                if day != signal_date:
                    raise ValueError("昨日涨停池缺失：%s" % day)
                continue
            rows = lim
            if "limit" in rows.columns:
                rows = rows[rows["limit"].astype(str).str.upper().eq("U")]
            eligible = set()
            for code in rows["ts_code"].dropna().astype(str):
                if market_segment(code) not in UNIVERSE_SEGMENTS or is_st_name(names.get(code, "")):
                    continue
                age = listing_age(list_dates.get(code, ""), day, days)
                if age is not None and age >= MIN_LISTING_DAYS:
                    eligible.add(code)
            missing = sorted(eligible - set(daily[day]["ts_code"].astype(str)))
            if missing:
                raise ValueError("%s 日线缺少涨停池股票%d只（%s），数据不完整" % (day, len(missing), "、".join(missing[:5])))
        candidates, diag = build_m_candidates(signal_date, days, daily, limit_today, basic_today, list_dates,
                                              name_based_st(names))
    except Exception as exc:  # fail-closed：记录ERROR，M次日不开仓
        save_recent_signal_run(RUN_STATUS_PATH, {"signal_date": signal_date, "status": ERROR, "error": str(exc),
                                                 "m_version": M_VERSION, "started_at": started},
                               strategy_leg=STRATEGY_M_LEG)
        print("M 信号生成失败（M 次日不开仓）：%s" % exc)
        return 1

    buy_date = next_open_day(days, signal_date)
    candidates.to_csv(OUTPUT_DIR / ("m_signal_%s_candidates.csv" % signal_date), index=False, encoding="utf-8-sig")
    rows: list[dict[str, Any]] = []
    for row in candidates.to_dict("records"):
        rows.append({
            "rank": int(row["rank"]),
            "ts_code": str(row["ts_code"]),
            "name": names.get(str(row["ts_code"]), ""),
            "amount_thousand": float(row["amount"]),
            "signal_close": float(row["close"]),
            "ret5": float(row["ret5"]),
            "fd_ratio": float(row["fd_ratio"]),
        })
    status = SIGNAL_READY if rows else NO_CANDIDATE
    diag_out = {k: v for k, v in diag.items() if k != "signal_date"}
    signal = {
        "signal_date": signal_date,
        "planned_buy_date": buy_date,
        "strategy_leg": STRATEGY_M_LEG,
        "m_version": M_VERSION,
        "status": status,
        "candidates": rows,
        "exit_rule": EXIT_RULE,
        "hold_offset": HOLD_OFFSET,
        "name_source": name_source,
        "diagnostics": diag_out,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg=STRATEGY_M_LEG)
    save_recent_signal_run(RUN_STATUS_PATH, {"signal_date": signal_date, "status": status, "candidates": len(rows),
                                             "m_version": M_VERSION, "started_at": started,
                                             "mood": diag.get("mood"), "n20_limit_up": diag.get("n20_limit_up")},
                           strategy_leg=STRATEGY_M_LEG)
    mood = diag.get("mood")
    print("M 信号 %s：昨日涨停今日平均 %s，20cm 涨停 %d 只，候选 %d 只%s" % (
        signal_date, ("%.2f%%" % (mood * 100)) if mood == mood else "无", int(diag.get("n20_limit_up") or 0), len(rows),
        ("：" + "、".join("%s(%s)" % (r["name"] or r["ts_code"], r["ts_code"]) for r in rows)) if rows else ""))
    print(json.dumps({"rejects": diag.get("rejects", {}), "mood_ok": diag.get("mood_ok"), "n20_ok": diag.get("n20_ok")},
                     ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="策略 M 收盘信号")
    parser.add_argument("--signal-date", required=True)
    args = parser.parse_args()
    return run(str(args.signal_date))


if __name__ == "__main__":
    sys.exit(main())
