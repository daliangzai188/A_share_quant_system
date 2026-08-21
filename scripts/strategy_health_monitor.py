# -*- coding: utf-8 -*-
"""策略健康度监控(收盘流水线末步,2026-07-17 用户拍板落地)。

监控E/ABC的信号口径收益:
  <P10 → YELLOW 预警;<P5 → RED 告警(建议降仓/停腿,e_enabled开关现成)。
E/ABC数据源 = 历史审计 + 每日信号增量。
级别变化即推送;每周一推例行摘要。
不判死策略,只报警——降仓/停腿由用户决策。
输出: reports/strategy_health/strategy_health_history.csv + state.json
"""
from __future__ import annotations

import csv
import glob
import json
import bisect
import datetime
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUT_DIR = PROJECT_ROOT / "reports" / "strategy_health"
STATE_FILE = OUT_DIR / "state.json"
HISTORY_FILE = OUT_DIR / "strategy_health_history.csv"
AUDIT = PROJECT_ROOT / "reports" / "current_live_abce_audit" / "current_live_abce_detail.csv"
FEE = 0.0015
ROLL = 20
MIN_SAMPLE = 40

cal = sorted(set(
    r["cal_date"] for r in csv.DictReader(
        open(PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv", encoding="utf-8-sig"))
    if r["is_open"] == "1"
))


def next_trade(d: str, n: int = 1):
    i = bisect.bisect_right(cal, str(d))
    return cal[i + n - 1] if i + n - 1 < len(cal) else None


_daily_cache: dict = {}


def daily(dd: str):
    if dd not in _daily_cache:
        p = PROJECT_ROOT / "data" / "raw" / "daily" / f"{dd}.csv"
        _daily_cache[dd] = (
            pd.read_csv(p).drop_duplicates("ts_code").set_index("ts_code") if p.exists() else None
        )
    return _daily_cache[dd]


def signal_return(ts_code: str, sig: str, exit_n: int = 1) -> float | None:
    """信号口径收益:T+1开盘买,T+1+exit_n收盘卖,减费用近似。数据未齐(未到期)返回None。"""
    b, e = next_trade(sig, 1), next_trade(sig, 1 + exit_n)
    if not b or not e:
        return None
    db, de = daily(b), daily(e)
    if db is None or de is None or ts_code not in db.index or ts_code not in de.index:
        return None
    o = float(db.loc[ts_code]["open"])
    c = float(de.loc[ts_code]["close"])
    if o <= 0 or c <= 0:
        return None
    return c / o - 1 - FEE


def collect_signals() -> dict[str, list]:
    """返回 {leg_group: [(sig_date, ts_code, exit_n, net_or_None), ...]},按信号日排序。"""
    seen: set = set()
    seqs: dict[str, list] = {"E": [], "ABC": []}
    # ── 历史段:审计明细(net直接用审计值,与实盘配置口径完全一致) ──
    if AUDIT.exists():
        a = pd.read_csv(AUDIT)
        f = a[a.operation_status == "HISTORICAL_SIM_FILLED"]
        for _, r in f.iterrows():
            sig = str(int(r.signal_date))
            leg = str(r.strategy_leg).upper()
            key = (leg, sig, r.ts_code)
            if key in seen:
                continue
            seen.add(key)
            net = float(r.e_stock_net_return if leg == "E" else r.account_return / 0.8)
            group = "E" if leg == "E" else "ABC"
            seqs[group].append((sig, str(r.ts_code), 1, net))
    # ── 增量段:每日信号文件(审计截止日之后) ──
    audit_max = max((s for grp in seqs.values() for s, *_ in grp), default="00000000")
    for p in sorted(glob.glob(str(PROJECT_ROOT / "reports" / "strategy_e" / "e_signal_*_candidates.csv"))):
        m = re.search(r"e_signal_(\d{8})_candidates", p)
        if not m or m.group(1) <= audit_max:
            continue
        sig = m.group(1)
        try:
            d = pd.read_csv(p, encoding="utf-8-sig")
        except Exception:
            continue
        if d.empty:
            continue
        ts = str(d.iloc[0]["ts_code"])  # 实盘口径=第一候选
        key = ("E", sig, ts)
        if key in seen:
            continue
        seen.add(key)
        seqs["E"].append((sig, ts, 1, signal_return(ts, sig, 1)))
    for p in sorted(glob.glob(str(PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops" / "*_planned_orders.csv"))):
        try:
            d = pd.read_csv(p, encoding="utf-8-sig")
        except Exception:
            continue
        if d.empty or "side" not in d.columns:
            continue
        for _, r in d[d.side.astype(str).str.upper() == "BUY"].iterrows():
            sig = str(r.get("signal_date", "")).strip().split(".")[0]
            if not sig.isdigit() or sig <= audit_max:
                continue
            ts = str(r.get("ts_code", ""))
            leg = str(r.get("strategy_leg", "")).upper()
            key = (leg, sig, ts)
            if key in seen:
                continue
            seen.add(key)
            try:
                exit_n = int(float(r.get("exit_n_days", 1) or 1))
            except Exception:
                exit_n = 1
            seqs["ABC"].append((sig, ts, exit_n, signal_return(ts, sig, exit_n)))
    for g in seqs:
        seqs[g].sort(key=lambda x: x[0])
    return seqs


def evaluate(seq: list) -> dict:
    nets = [x[3] for x in seq if x[3] is not None]
    n = len(nets)
    if n < MIN_SAMPLE:
        return dict(n=n, level="INSUFFICIENT")
    s = pd.Series(nets)
    roll_mean = s.rolling(ROLL).mean().dropna()
    roll_win = s.rolling(ROLL).apply(lambda x: (x > 0).mean()).dropna()
    cur_mean = float(roll_mean.iloc[-1])
    cur_win = float(roll_win.iloc[-1])
    # 基线分布排除最近ROLL笔(避免当前值自证)
    base = roll_mean.iloc[:-ROLL] if len(roll_mean) > ROLL * 2 else roll_mean
    p10, p5 = float(base.quantile(0.10)), float(base.quantile(0.05))
    level = "RED" if cur_mean < p5 else ("YELLOW" if cur_mean < p10 else "GREEN")
    return dict(n=n, roll_mean=cur_mean, roll_win=cur_win, p10=p10, p5=p5,
                level=level, last_sig=seq[-1][0], all_mean=float(s.mean()))


def bark(title: str, body: str, critical: bool = False) -> None:
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.notify import notify
        notify("system_error", title, body,
               level="critical" if critical else "timeSensitive")
    except Exception as e:
        print(f"[bark失败] {e}")


def main() -> None:
    today = datetime.date.today().strftime("%Y%m%d")
    seqs = collect_signals()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    rows, msgs, worst = [], [], "GREEN"
    rank = {"GREEN": 0, "INSUFFICIENT": 0, "YELLOW": 1, "RED": 2}
    for group, seq in seqs.items():
        r = evaluate(seq)
        rows.append(dict(date=today, leg=group, **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()}))
        lvl = r["level"]
        if rank.get(lvl, 0) > rank.get(worst, 0):
            worst = lvl
        prev = str(state.get(group, "GREEN"))
        if lvl == "INSUFFICIENT":
            msgs.append(f"{group}:样本{r['n']}笔不足{MIN_SAMPLE},暂不判级")
        else:
            msgs.append(
                f"{group}:{lvl} 滚动{ROLL}笔期望{r['roll_mean'] * 100:+.2f}%/胜率{r['roll_win'] * 100:.0f}%"
                f"(基线P10={r['p10'] * 100:+.2f}%,P5={r['p5'] * 100:+.2f}%,全样本{r['all_mean'] * 100:+.2f}%,n={r['n']})"
            )
            if lvl != prev:
                # 文案口径(2026-07-18 因果回测定稿):预警≠降仓信号≠坏消息。
                # E 152笔零前视回测:YELLOW/RED状态下一笔期望+7.11%/+5.79%
                # (GREEN仅+0.74%,深回撤后均值回归=右尾利润藏身处);预警时降仓
                # 两年复利2.2x→1.5x,全停→1.2x。预警的唯一用途=触发结构性核查
                # (制度变化/玩法拥挤/规则失效——机器无法从20笔数据识别,需人工
                # 结合场外信息),无结构性变化则按纪律满仓拿住。
                improving = rank[lvl] < rank.get(prev, 0)
                if improving:
                    title = f"🟢 策略体检:{group} 回到正常区({prev}→{lvl})"
                    advice = "。指标已回到历史正常范围,无需任何操作。"
                elif lvl == "YELLOW":
                    title = f"🟡 策略体检:{group} 近20笔弱于自身历史(不是亏损警报)"
                    advice = ("。这是相对自身历史标准的体检指标,不代表在亏钱。"
                              "历史数据:此状态后下一笔期望反而更高(均值回归),"
                              "✋不要降仓、不要停腿;唯一动作=找Claude做一次结构性核查"
                              "(确认是正常回撤,还是市场制度/玩法环境变了)。")
                else:  # RED
                    title = f"🔴 策略体检:{group} 触及历史极弱位(需要核查,不需要恐慌)"
                    advice = ("。历史上此状态后下一笔期望+5.79%(正常时仅+0.74%),"
                              "深回撤后的反弹正是本策略利润所在,✋不要降仓。"
                              "但极弱位必须排除'策略环境已变'的可能:请尽快找Claude"
                              "做结构性核查(制度变化/玩法拥挤/规则失效),核查通过则满仓拿住。")
                bark(title, msgs[-1] + advice, critical=(lvl == "RED"))
        state[group] = lvl
        state.setdefault("_details", {})[group] = {
            "level": lvl,
            "sample_count": int(r.get("n", 0)),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "historical_audit_plus_signal_increment",
        }
    # 每周一例行摘要(不论级别)
    if datetime.date.today().weekday() == 0 and state.get("_last_weekly") != today:
        bark("📊 策略体检周报", "；".join(msgs)
             + "。(口径:滚动20笔对照自身历史分位;黄/红=弱于自身标准≠亏损,"
             "历史上弱位后下一笔期望反而更高,任何级别都不是降仓信号)")
        state["_last_weekly"] = today
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    new_file = not HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "leg", "n", "roll_mean", "roll_win", "p10", "p5", "level", "last_sig", "all_mean"])
        if new_file:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in w.fieldnames})
    print("策略健康度:", "；".join(msgs))


if __name__ == "__main__":
    sys.exit(main())
