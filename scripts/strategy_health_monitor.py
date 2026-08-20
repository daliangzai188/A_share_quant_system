# -*- coding: utf-8 -*-
"""策略健康度监控(收盘流水线末步,2026-07-17 用户拍板落地)。

监控E/ABC的信号口径收益及N的真实完整成交收益:
  <P10 → YELLOW 预警;<P5 → RED 告警(建议降仓/停腿,e_enabled开关现成)。
E/ABC数据源 = 历史审计 + 每日信号增量；N只读取execution_tracking中买卖数量
完整的券商真实成交并按日期化费用计算，候选、计划、未平仓和部分成交不得进入。
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

from src.live_performance import completed_live_trades  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402

OUT_DIR = PROJECT_ROOT / "reports" / "strategy_health"
STATE_FILE = OUT_DIR / "state.json"
HISTORY_FILE = OUT_DIR / "strategy_health_history.csv"
AUDIT = PROJECT_ROOT / "reports" / "current_live_abce_audit" / "current_live_abce_detail.csv"
TRADE_COMPLETION = (
    PROJECT_ROOT / "reports" / "execution_tracking" / "trade_completion_summary.csv"
)
FEE = 0.0015
ROLL = 20
MIN_SAMPLE = 40

# ── N腿专属绝对判据 ────────────────────────────────────────────────────
# N 2026-08-20 才进样本外，实盘笔数长期不够 MIN_SAMPLE=40，套分位判据只会
# 永远 INSUFFICIENT、等于没监控。所以 N 用绝对阈值：基线取自v3因果认证组合里
# N 的32笔（旧35笔含前视成交打分/未前复权口径，已失效）。
# v3历史：连亏3笔 / 单笔-9.0586% / 滚动10笔P10=-0.9164%、最差-1.4566% /
# 自身回撤-20.6996%。RED在历史最差之外，避免上线即误报；这仍只是小样本监控。
N_ROLL = 10
N_RULES = {
    "yellow_consecutive_losses": 3,
    "red_consecutive_losses": 5,
    "yellow_single_loss": -0.0906,
    "red_single_loss": -0.1200,
    "yellow_roll_mean": -0.009164,  # v3基线P10
    "red_roll_mean": -0.014566,     # v3基线最差
    "yellow_drawdown": -0.2070,
    "red_drawdown": -0.2700,
}

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


def completed_n_sequence(
    raw: pd.DataFrame,
    config: dict,
) -> list[tuple[str, str, int, float]]:
    """把真实完成汇总转换为N健康序列；未平仓/部分成交由统一清洗器剔除。"""

    report_config = dict(config.get("live_performance_report", {}))
    analysis = config.get("analysis", {})
    for field in (
        "commission_rate", "stamp_tax_rate", "stamp_tax_schedule",
        "transfer_fee_rate", "minimum_commission",
    ):
        report_config.setdefault(field, analysis.get(field))
    completed, _quality = completed_live_trades(raw, report_config)
    completed = completed[completed["strategy_leg"].astype(str).eq("N")].copy()
    completed = completed.drop_duplicates("trade_key", keep="last")
    result: list[tuple[str, str, int, float]] = []
    for _, trade in completed.iterrows():
        sig = str(trade.get("signal_date", "")).replace(".0", "")
        if not sig.isdigit():
            sig = str(trade.get("entry_date", "")).replace("-", "")[:8]
        result.append((
            sig,
            str(trade.get("ts_code", "")),
            1,
            float(trade["net_return"]),
        ))
    return sorted(result, key=lambda item: item[0])


def collect_signals() -> dict[str, list]:
    """返回 {leg_group: [(sig_date, ts_code, exit_n, net_or_None), ...]},按信号日排序。"""
    seen: set = set()
    seqs: dict[str, list] = {"E": [], "ABC": [], "N": []}
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
    # ── N：只统计已完成且买卖数量完整的券商真实成交。候选、信号、计划单、
    #     未平仓和部分成交一律不得冒充真实收益。 ──
    if TRADE_COMPLETION.exists():
        try:
            raw = pd.read_csv(
                TRADE_COMPLETION,
                dtype={"trade_key": str, "ts_code": str, "signal_date": str},
                low_memory=False,
            )
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            for sig, ts, exit_n, net_return in completed_n_sequence(raw, config):
                key = ("N", sig, ts)
                if key in seen:
                    continue
                seen.add(key)
                seqs["N"].append((sig, ts, exit_n, net_return))
        except Exception as exc:
            print(f"[N真实成交读取失败] {exc}")
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


def evaluate_n(seq: list) -> dict:
    """N腿绝对判据：连亏、单笔、滚动均值、自身回撤，任一触及即升级。

    与 evaluate() 的分位判据并存而不是替换——N 的实盘样本要很久才够 40 笔，
    在那之前分位判据什么都判不出来。四项里任一 RED 即 RED，否则任一 YELLOW 即 YELLOW。
    """
    nets = [x[3] for x in seq if x[3] is not None]
    n = len(nets)
    if n == 0:
        return dict(n=0, level="INSUFFICIENT")
    s = pd.Series(nets)

    streak = 0
    for v in reversed(nets):
        if v < 0:
            streak += 1
        else:
            break
    worst_single = float(s.min())
    roll_mean = float(s.tail(N_ROLL).mean()) if n >= N_ROLL else None
    equity = (1 + s * 0.825).cumprod()
    drawdown = float((equity / equity.cummax() - 1).min())

    hits = []
    level = "GREEN"

    def mark(cond_red, cond_yellow, label):
        nonlocal level
        if cond_red:
            level = "RED"
            hits.append(f"{label}(RED)")
        elif cond_yellow and level != "RED":
            level = "YELLOW"
            hits.append(f"{label}(YELLOW)")

    mark(streak >= N_RULES["red_consecutive_losses"],
         streak >= N_RULES["yellow_consecutive_losses"], f"连亏{streak}笔")
    mark(worst_single < N_RULES["red_single_loss"],
         worst_single < N_RULES["yellow_single_loss"], f"单笔{worst_single * 100:+.1f}%")
    if roll_mean is not None:
        mark(roll_mean < N_RULES["red_roll_mean"],
             roll_mean < N_RULES["yellow_roll_mean"], f"滚动{N_ROLL}笔{roll_mean * 100:+.2f}%")
    mark(drawdown < N_RULES["red_drawdown"],
         drawdown < N_RULES["yellow_drawdown"], f"回撤{drawdown * 100:.1f}%")

    return dict(n=n, roll_mean=roll_mean if roll_mean is not None else float("nan"),
                roll_win=float((s > 0).mean()), p10=N_RULES["yellow_roll_mean"],
                p5=N_RULES["red_roll_mean"], level=level, last_sig=seq[-1][0],
                all_mean=float(s.mean()), streak=streak, drawdown=drawdown,
                hits=";".join(hits) if hits else "")


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
        r = evaluate_n(seq) if group == "N" else evaluate(seq)
        rows.append(dict(date=today, leg=group, **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()}))
        lvl = r["level"]
        if rank.get(lvl, 0) > rank.get(worst, 0):
            worst = lvl
        prev = str(state.get(group, "GREEN"))
        if lvl == "INSUFFICIENT":
            floor = 1 if group == "N" else MIN_SAMPLE
            msgs.append(f"{group}:样本{r['n']}笔不足{floor},暂不判级")
        elif group == "N":
            # N 单独走绝对判据，文案也必须单独写：下面 E/ABC 那套"不要降仓"来自
            # E 152笔零前视因果回测支持均值回归；N v3虽修复为32笔因果执行口径，
            # 但冻结TEST_OOS仍低于不含N，且真实oos起点2026-08-20，不能照搬。
            msgs.append(
                f"N:{lvl} 实盘{r['n']}笔 触发[{r['hits']}] "
                f"(连亏{r['streak']}笔/自身回撤{r['drawdown'] * 100:.1f}%/均值{r['all_mean'] * 100:+.2f}%)"
            )
            if lvl != prev:
                if rank[lvl] < rank.get(prev, 0):
                    bark(f"🟢 策略体检:N 回到正常区({prev}→{lvl})",
                         msgs[-1] + "。指标回到阈值内,无需操作。")
                elif lvl == "YELLOW":
                    bark(f"🟡 策略体检:N 触及预警阈值",
                         msgs[-1] + "。N是样本内搜索出的规则、2026-08-20才进样本外,"
                         "没有'均值回归'的回测依据支持继续持有。"
                         "动作=核查规则是否已失效,先不动仓。")
                else:
                    bark(f"🔴 策略体检:N 触及停腿阈值",
                         msgs[-1] + "。已超出N自身历史最差区间。"
                         "建议只暂停N新增开仓(config.strategy_n.entry_pause=true),"
                         "不得关闭已有持仓SELL链路。暂停N不影响其余五腿。",
                         critical=True)
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
            "source": (
                "reports/execution_tracking/trade_completion_summary.csv"
                if group == "N" else "historical_audit_plus_signal_increment"
            ),
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
