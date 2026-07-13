#!/usr/bin/env python3
"""东方财富人气榜快照采集器(热榜策略研究的数据基建)。

背景(2026-07-13 立项):研究"热榜前N名 + 时点买入 + 次日卖出"策略。
Tushare 的 ths_hot/dc_hot 只有每日盘后快照,无法支撑"盘中时点买入"的
无前视回测;自建多时点快照是唯一干净路径。

运行:Mac launchd 每 5 分钟触发(com.asystem.hotrank);
     北京时间交易时段(9:15~15:05)且为交易日才采集,其余直接退出。
存储:data/raw/hot_rank/dc_YYYYMMDD.csv,追加模式,
     列 = snap_time,rank,ts_code,rank_chg,his_rank_chg
     (每快照 50 行,每天约 70 快照 ≈ 3500 行/200KB,随 Syncthing 同步)
注意:本脚本独立于交易系统,失败静默,绝不影响任何交易流程。
"""
import csv
import datetime
import json
import os
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/Users/user/Desktop/A_System")
OUT_DIR = ROOT / "data" / "raw" / "hot_rank"
CAL = ROOT / "data" / "raw" / "trade_calendar.csv"
API = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
TOP_N = 50


def beijing_now() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))


def is_trade_day(d: datetime.date) -> bool:
    ds = d.strftime("%Y%m%d")
    try:
        with open(CAL, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("cal_date") == ds:
                    return str(row.get("is_open", "")).strip() == "1"
    except OSError:
        pass
    return d.weekday() < 5  # 日历不可读时周历兜底


def fetch_rank() -> list:
    body = json.dumps({
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "pageNo": 1, "pageSize": TOP_N,
    }).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read())
    return j.get("data") or []


def main() -> None:
    now = beijing_now()
    t = now.time()
    if not (datetime.time(9, 15) <= t <= datetime.time(15, 5)):
        return
    if not is_trade_day(now.date()):
        return
    rows = fetch_rank()
    if not rows:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"dc_{now.strftime('%Y%m%d')}.csv"
    new_file = not out.exists()
    snap = now.strftime("%H:%M:%S")
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["snap_time", "rank", "ts_code", "rank_chg", "his_rank_chg"])
        for item in rows:
            sc = str(item.get("sc", ""))          # 如 SH600118
            code = f"{sc[2:]}.{sc[:2]}" if len(sc) == 8 else sc
            w.writerow([snap, item.get("rk"), code,
                        item.get("rc", 0), item.get("hisRc", 0)])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 研究采集,静默失败,下个周期再试
