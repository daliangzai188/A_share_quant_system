"""策略 L 晚间信号（QMT 外运行；ACE+M+L 新框架，2026-11-26）。

龙虎榜与席位只能从 Tushare 取（QMT 里没有，且 QMT 内置 Python 不能联网读写文件），所以 L 的选股在 QMT 外算：
虚拟机上的推送转发程序 16:30 起每 20 分钟调本脚本，直到“取全”；次日 08:20 再兜底一次。规则见 docs/strategy_l.md。

步骤：①补齐 T 日日线/每日指标（同生产第①步采集脚本）；②取 T 日龙虎榜与席位，每条公告都能对上席位才算取全；
③更新名称变更记录（判断历史时点 ST）；④按 src/strategy_l.py 算 T 日候选，首选即 T+1 计划；
⑤按历史首选与买卖结果更新 L 影子净值，得出 T+1 是否处于 L 自身停手；⑥写 reports/strategy_l/ 留档，
带 --write-registry 时把计划写进注册表 HKCU\\Software\\A_System\\QmtNative 的 LPlan（QMT 宿主开盘前读入）。

退出码：0 完成（有首选或无候选）；3 数据还没出全（稍后再试）；1 出错（L 次日不开仓）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.ace_m_replay import RawPrices, make_l_outcome
from src.equity_curve_stop import allowed_flags
from src.portfolio_ace_m import L_SELF_STOP_SCALE
from src.rolling_signal_store import ERROR, NO_CANDIDATE, SIGNAL_READY, save_recent_signal, save_recent_signal_run
from src.strategy_l import (
    A_SHARE_CODE,
    L_SHADOW_START,
    L_STOP_LAG,
    L_STOP_MA_WINDOW,
    L_STOP_MIN_DAYS,
    L_VERSION,
    STRATEGY_L_LEG,
    LInputs,
    NameHistory,
    canonical_board,
    l_shadow_nav,
    select_l_candidates,
)

RAW = PROJECT_ROOT / "data" / "raw"
CALENDAR_PATH = RAW / "trade_calendar.csv"
STOCK_BASIC_PATH = RAW / "stock_basic" / "stock_basic_all.csv"
TOP_LIST_DIR = RAW / "top_list"
TOP_INST_DIR = RAW / "top_inst"
NAMECHANGE_PATH = RAW / "namechange" / "namechange_all.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "l_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "l_signal_runs_recent.json"
PICKS_CACHE_PATH = OUTPUT_DIR / "l_picks_cache.json"
SHADOW_NAV_PATH = OUTPUT_DIR / "l_shadow_nav_latest.csv"
DAILY_COLUMNS = ["ts_code", "open", "high", "low", "close", "pre_close", "pct_chg", "amount", "vol"]
REGISTRY_KEY = r"Software\A_System\QmtNative"
REGISTRY_VALUE = "LPlan"
EXIT_DONE, EXIT_ERROR, EXIT_NOT_READY = 0, 1, 3
MIN_SHADOW_DAYS = L_STOP_MIN_DAYS   # L 影子净值至少要这么多交易日才给出停手判定（60日均线+滞后6日+余量）
NAMECHANGE_MAX_AGE_HOURS = 20
SIGNAL_READY_HOUR = 16         # 16:00 以前跑，信号日取上一个交易日（08:20 兜底即属此类）


class NotReady(Exception):
    """数据还没出全，稍后再试。"""


def open_days() -> list[str]:
    cal = pd.read_csv(CALENDAR_PATH, dtype=str, encoding="utf-8-sig")
    if "exchange" in cal.columns:
        cal = cal[cal["exchange"].astype(str).str.upper().isin(["SSE", ""])]
    return sorted(set(cal.loc[pd.to_numeric(cal["is_open"], errors="coerce").eq(1), "cal_date"].astype(str)))


def read_csv(path: Path, **kw) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", **kw)


def default_signal_date(days: list[str], now: datetime) -> str:
    today = now.strftime("%Y%m%d")
    past = [d for d in days if d < today]
    if today in days and now.hour >= SIGNAL_READY_HOUR:
        return today
    if not past:
        raise ValueError("交易日历里没有早于今天的交易日")
    return past[-1]


class BarCache:
    """按交易日读日线（只读、带缓存），给 L 选股和影子净值用。"""

    def __init__(self, keep: int = 120) -> None:
        self._data: OrderedDict = OrderedDict()
        self._keep = keep

    def day(self, day: str) -> dict:
        if day in self._data:
            self._data.move_to_end(day)
            return self._data[day]
        frame = read_csv(RAW / "daily" / ("%s.csv" % day), dtype={"ts_code": str})
        rows = {}
        if not frame.empty:
            for rec in frame[[c for c in DAILY_COLUMNS if c in frame.columns]].to_dict("records"):
                rows[str(rec["ts_code"])] = rec
        self._data[day] = rows
        while len(self._data) > self._keep:
            self._data.popitem(last=False)
        return rows


class BarsView(dict):
    """LInputs.bars 需要 {交易日: {代码: 行情}}；按需从缓存读。"""

    def __init__(self, cache: BarCache) -> None:
        super().__init__()
        self._cache = cache

    def get(self, day, default=None):  # noqa: D401 - 与 dict 同名
        return self._cache.day(str(day))

    def __getitem__(self, day):
        return self._cache.day(str(day))


def market_mean_pct(bars_today: dict) -> float | None:
    values = [float(r["pct_chg"]) for code, r in bars_today.items()
              if A_SHARE_CODE.match(code) and r.get("pct_chg") is not None and math.isfinite(float(r["pct_chg"]))]
    return sum(values) / len(values) if values else None


def lhb_complete(top_list: pd.DataFrame, top_inst: pd.DataFrame) -> tuple[bool, str]:
    """取全：龙虎榜非空，且每条按规则入选的公告（股票+原因）都能对上席位明细。"""

    if top_list is None or top_list.empty:
        return False, "龙虎榜为空"
    board = canonical_board(top_list)
    if board.empty:
        return False, "龙虎榜没有可用的单日/三日公告"
    if top_inst is None or top_inst.empty:
        return False, "席位明细为空"
    seats = set(zip(top_inst["ts_code"].astype(str), top_inst["reason"].astype(str)))
    missing = [(c, r) for c, r in zip(board["ts_code"].astype(str), board["reason"].astype(str)) if (c, r) not in seats]
    if missing:
        return False, "%d 条公告还没有席位明细（如 %s）" % (len(missing), missing[0][0])
    return True, ""


def ensure_market_data(day: str) -> None:
    """T 日日线与每日指标不全就调生产第①步采集脚本补；仍不全则 NotReady。"""

    need = [RAW / "daily" / ("%s.csv" % day), RAW / "daily_basic" / ("%s.csv" % day)]
    if all(p.exists() and p.stat().st_size > 0 for p in need):
        return
    subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "collect_all_data.py"), "--start-date", day,
                    "--end-date", day, "--skip-limit"], cwd=str(PROJECT_ROOT), check=False,
                   timeout=900)
    if not all(p.exists() and p.stat().st_size > 0 for p in need):
        raise NotReady("T 日日线或每日指标还没有")


def ensure_lhb(day: str, source) -> None:
    tl_path, ti_path = TOP_LIST_DIR / ("%s.csv" % day), TOP_INST_DIR / ("%s.csv" % day)
    if tl_path.exists() and ti_path.exists():
        ok, _ = lhb_complete(read_csv(tl_path, dtype={"trade_date": str}), read_csv(ti_path, dtype={"trade_date": str, "side": str}))
        if ok:
            return
    if source is None:
        raise NotReady("本地没有完整的龙虎榜，且未允许联网采集")
    top_list = source.get_top_list(day)
    top_inst = source.get_top_inst(day)
    ok, why = lhb_complete(top_list, top_inst)
    if not ok:
        raise NotReady("龙虎榜还没出全：%s" % why)
    TOP_LIST_DIR.mkdir(parents=True, exist_ok=True)
    TOP_INST_DIR.mkdir(parents=True, exist_ok=True)
    top_list.to_csv(tl_path, index=False, encoding="utf-8-sig")
    top_inst.to_csv(ti_path, index=False, encoding="utf-8-sig")


def refresh_namechange(source) -> None:
    if NAMECHANGE_PATH.exists():
        age = datetime.now() - datetime.fromtimestamp(NAMECHANGE_PATH.stat().st_mtime)
        if age < timedelta(hours=NAMECHANGE_MAX_AGE_HOURS) or source is None:
            return
    if source is None:
        raise RuntimeError("缺少名称变更记录 %s" % NAMECHANGE_PATH)
    frame = source.get_namechange_all()
    if frame is None or frame.empty or len(frame) < 1000:
        if NAMECHANGE_PATH.exists():
            return            # 取失败就沿用旧的（判断 ST 只依赖已生效的历史名称）
        raise RuntimeError("名称变更记录取不到")
    NAMECHANGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = NAMECHANGE_PATH.with_suffix(".tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(NAMECHANGE_PATH)


def load_names() -> tuple[NameHistory, dict[str, str]]:
    from src.strategy_l_reference import load_name_history

    return load_name_history(STOCK_BASIC_PATH, NAMECHANGE_PATH)


def board_history(days: list[str]) -> dict[str, set]:
    out = {}
    for d in days:
        tl = read_csv(TOP_LIST_DIR / ("%s.csv" % d), dtype={"trade_date": str})
        out[d] = set(canonical_board(tl)["ts_code"]) if not tl.empty else set()
    return out


def candidates_for(day: str, days: list[str], cache: BarCache, names: NameHistory, list_dates: dict,
                   hist: dict) -> tuple[pd.DataFrame, dict]:
    i = days.index(day)
    window = days[max(0, i - 60):i + 1]
    tl = read_csv(TOP_LIST_DIR / ("%s.csv" % day), dtype={"trade_date": str})
    ti = read_csv(TOP_INST_DIR / ("%s.csv" % day), dtype={"trade_date": str, "side": str})
    basic = read_csv(RAW / "daily_basic" / ("%s.csv" % day), dtype={"ts_code": str})
    total_share = {}
    if not basic.empty and "total_share" in basic.columns:
        for code, v in zip(basic["ts_code"], basic["total_share"]):
            if v == v:
                total_share[str(code)] = float(v)
    inp = LInputs(signal_date=day, days=window, bars=BarsView(cache), pct_chg_mean=market_mean_pct(cache.day(day)),
                  top_list=tl, top_inst=ti, board_history=hist, total_share=total_share, list_dates=list_dates,
                  name_on=names.name_on)
    return select_l_candidates(inp)


def update_picks_cache(days: list[str], signal_date: str, cache: BarCache, names: NameHistory, list_dates: dict) -> dict:
    """历史首选缓存：{买入日: [按规则排好序的前3个代码]}；只补缺的日子（龙虎榜文件齐全的信号日）。"""

    picks = {}
    if PICKS_CACHE_PATH.exists():
        payload = json.loads(PICKS_CACHE_PATH.read_text(encoding="utf-8"))
        if payload.get("l_version") == L_VERSION:
            picks = dict(payload.get("picks", {}))
    start = max(L_SHADOW_START, days[0])
    todo = [d for d in days if start <= d <= signal_date and days.index(d) >= 60
            and (TOP_LIST_DIR / ("%s.csv" % d)).exists() and (TOP_INST_DIR / ("%s.csv" % d)).exists()]
    hist = board_history([d for d in days if d <= signal_date and d >= days[max(0, days.index(todo[0]) - 60)]]) if todo else {}
    for d in todo:
        nxt = days[days.index(d) + 1] if days.index(d) + 1 < len(days) else ""
        if not nxt or nxt in picks:
            continue
        out, _ = candidates_for(d, days, cache, names, list_dates, hist)
        picks[nxt] = list(out["ts_code"][:3]) if not out.empty else []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PICKS_CACHE_PATH.write_text(json.dumps({"l_version": L_VERSION, "picks": dict(sorted(picks.items()))},
                                           ensure_ascii=False), encoding="utf-8")
    return picks


def l_allowed_for(buy_date: str, days: list[str], picks: dict, names: NameHistory, list_dates: dict) -> tuple[bool | None, int]:
    """T+1 的 L 自身停手判定：影子净值滞后 6 日不低于 60 日均线 → 允许（满仓位）；历史不够返回 None。"""

    upto = [d for d in days if d < buy_date]
    outcome = make_l_outcome(RawPrices(RAW), days, name_on=names.name_on, list_dates=list_dates)
    nav = l_shadow_nav(upto, picks, outcome)
    if len(nav) < MIN_SHADOW_DAYS:
        return None, len(nav)
    nav.to_frame("l_shadow_nav").to_csv(SHADOW_NAV_PATH, encoding="utf-8-sig")
    extended = list(nav.to_numpy()) + [float(nav.iloc[-1])]     # 买入日本身的净值不影响它自己的判定（滞后 6 日）
    flags = allowed_flags(extended, ma_window=L_STOP_MA_WINDOW, lag=L_STOP_LAG)
    return bool(flags[-1]), len(nav)


def registry_value(plan: dict) -> str:
    body = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return "v1|%s|%s|%s|%s" % (plan["signal_date"], plan["planned_buy_date"], body, digest)


def write_registry(value: str) -> None:
    import winreg  # 只在虚拟机（Windows）上可用

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, REGISTRY_VALUE + "At", 0, winreg.REG_SZ, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def run(signal_date: str | None, *, fetch: bool = True, registry: bool = False) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    days = open_days()
    if signal_date is None:
        from src.utils.time_utils import now_beijing

        signal_date = default_signal_date(days, now_beijing())
    if signal_date not in days or days.index(signal_date) + 1 >= len(days):
        print("L 信号：%s 不是交易日或日历不足" % signal_date)
        return EXIT_ERROR
    buy_date = days[days.index(signal_date) + 1]
    try:
        source = None
        if fetch:
            from src.data_source import TushareDataSource

            source = TushareDataSource(PROJECT_ROOT / "config" / "config.json")
            ensure_market_data(signal_date)
        ensure_lhb(signal_date, source)
        refresh_namechange(source)
        names, list_dates = load_names()
        cache = BarCache()
        picks = update_picks_cache(days, signal_date, cache, names, list_dates)
        hist = board_history(days[max(0, days.index(signal_date) - 60):days.index(signal_date)])
        candidates, diag = candidates_for(signal_date, days, cache, names, list_dates, hist)
        allowed, shadow_days = l_allowed_for(buy_date, days, picks, names, list_dates)
        if allowed is None:
            raise RuntimeError("L 影子净值只有 %d 个交易日，不足 %d 个，无法给出停手判定" % (shadow_days, MIN_SHADOW_DAYS))
    except NotReady as exc:
        print("L 信号 %s：数据还没出全，稍后再试（%s）" % (signal_date, exc))
        return EXIT_NOT_READY
    except Exception as exc:  # fail-closed：L 次日不开仓
        save_recent_signal_run(RUN_STATUS_PATH, {"signal_date": signal_date, "status": ERROR, "error": str(exc),
                                                 "l_version": L_VERSION, "started_at": started}, strategy_leg=STRATEGY_L_LEG)
        print("L 信号生成失败（L 次日不开仓）：%s" % exc)
        return EXIT_ERROR
    rows = []
    for rec in candidates.head(3).to_dict("records"):
        rows.append({k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in rec.items()
                     if k in ("ts_code", "name", "branch", "rank", "amplitude", "limit_up_count_60", "board_count_60",
                              "turnover_multiple_20")})
    status = SIGNAL_READY if rows else NO_CANDIDATE
    plan = {"signal_date": signal_date, "planned_buy_date": buy_date, "strategy_leg": STRATEGY_L_LEG, "l_version": L_VERSION,
            "status": status, "ts_code": rows[0]["ts_code"] if rows else "", "name": rows[0]["name"] if rows else "",
            "branch": diag.get("branch"), "l_allowed": bool(allowed), "l_scale": 1.0 if allowed else L_SELF_STOP_SCALE,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    signal = {**plan, "candidates": rows, "diagnostics": {k: v for k, v in diag.items() if k != "signal_date"}}
    save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg=STRATEGY_L_LEG)
    save_recent_signal_run(RUN_STATUS_PATH, {"signal_date": signal_date, "status": status, "candidates": len(rows),
                                             "l_version": L_VERSION, "started_at": started, "branch": diag.get("branch"),
                                             "l_allowed": bool(allowed)}, strategy_leg=STRATEGY_L_LEG)
    if registry:
        write_registry(registry_value(plan))
    print("L 信号 %s（%s 买入）：分支%s，%s；L%s" % (
        signal_date, buy_date, diag.get("branch"),
        ("首选 %s(%s)" % (plan["name"], plan["ts_code"])) if rows else "无合格股票",
        "正常仓位" if allowed else "自身停手（仓位×20/82.5）"))
    return EXIT_DONE


def main() -> int:
    parser = argparse.ArgumentParser(description="策略 L 晚间信号（QMT 外）")
    parser.add_argument("--signal-date", default=None, help="信号日 YYYYMMDD；缺省按当前北京时间推算")
    parser.add_argument("--no-fetch", action="store_true", help="不联网，只用本地已有数据")
    parser.add_argument("--write-registry", action="store_true", help="把计划写进注册表（只在虚拟机上用）")
    args = parser.parse_args()
    return run(args.signal_date, fetch=not args.no_fetch, registry=args.write_registry)


if __name__ == "__main__":
    sys.exit(main())
