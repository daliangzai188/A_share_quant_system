"""E信号的输入：交易日、各板块涨停数与退潮状态、当日打分文件、E候选（调用E唯一规则源）。

收盘流水线⑦E（scripts/run_strategy_e_signal.py）、实盘组合的E盘中预览（src/combined_live_engine.py）
和流水线⑥算下一交易日都用这里。2026-11-14 从 scripts/run_strategy_e_signal.py 原样搬来，代码未改；
SCORED_PATH 仍在导入时决定用实盘打分文件还是历史打分文件，和原来一样。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.strategy_e import build_live_e_candidates

PROJECT_ROOT = Path(__file__).absolute().parents[1]

LIMIT_DIR = PROJECT_ROOT / "data" / "raw" / "limit_list"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
LIVE_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv"
HIST_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
SCORED_PATH = LIVE_SCORED_PATH if LIVE_SCORED_PATH.exists() else HIST_SCORED_PATH


def load_open_dates() -> list[str]:
    if not CALENDAR_PATH.exists():
        return []
    try:
        cal = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
        if "is_open" in cal.columns:
            cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
        return sorted(cal["cal_date"].astype(str).tolist())
    except Exception:
        return []


def prev_trade_days(date_str: str, n: int) -> list[str]:
    """返回 date_str 之前的 n 个交易日，最近的排在最后。"""
    dates = load_open_dates()
    before = [d for d in dates if d < date_str]
    return before[-n:] if len(before) >= n else before


def next_trade_day(date_str: str, n: int = 1) -> str:
    dates = load_open_dates()
    future = [d for d in dates if d > date_str]
    if len(future) >= n:
        return future[n - 1]
    # 降级：按自然日跳过周末
    cur = datetime.strptime(date_str, "%Y%m%d").date()
    count = 0
    while count < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            count += 1
    return cur.strftime("%Y%m%d")


def classify_segment(ts_code: str) -> str:
    code = str(ts_code).upper().strip()
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix[:1] in ("4", "8", "9"):
        return "bj"
    if prefix.startswith(("688", "689")):
        return "star"
    if prefix.startswith(("300", "301")):
        return "chi_next"
    if code.endswith(".SH") and prefix.startswith("6"):
        return "sh_main"
    if code.endswith(".SZ") and prefix.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "other"


def count_limit_up_by_segment(date: str) -> dict[str, int]:
    """从 raw limit_list 统计当日各板块涨停数量。"""
    f = LIMIT_DIR / f"{date}.csv"
    if not f.exists():
        return {}
    try:
        df = pd.read_csv(f, dtype=str)
        df = df[df.get("limit", pd.Series(dtype=str)) == "U"]
        counts: dict[str, int] = {}
        for code in df["ts_code"].dropna().astype(str):
            seg = classify_segment(code)
            counts[seg] = counts.get(seg, 0) + 1
        return counts
    except Exception:
        return {}


def classify_retreat_state(current: float, prev1: float, prev2: float) -> str:
    if any(pd.isna(v) for v in [current, prev1, prev2]):
        return "unknown"
    if current <= 3:
        return "weak_below_3"
    if current < prev1 < prev2:
        return "retreat_2day"
    if current < prev1 and current <= 5:
        return "retreat_weak"
    if current > prev1 > prev2:
        return "warming_2day"
    return "neutral"


def compute_segment_retreat_states(signal_date: str) -> dict[str, str]:
    """计算 signal_date 各板块的 segment_retreat_state_bucket。"""
    prev_days = prev_trade_days(signal_date, 2)
    if len(prev_days) < 2:
        return {}
    prev1_date, prev2_date = prev_days[-1], prev_days[-2]

    c0 = count_limit_up_by_segment(signal_date)
    c1 = count_limit_up_by_segment(prev1_date)
    c2 = count_limit_up_by_segment(prev2_date)

    all_segs = set(c0) | set(c1) | set(c2)
    result: dict[str, str] = {}
    for seg in all_segs:
        cur = float(c0.get(seg, float("nan")))
        p1  = float(c1.get(seg, float("nan")))
        p2  = float(c2.get(seg, float("nan")))
        result[seg] = classify_retreat_state(cur, p1, p2)
    return result


def load_e_candidates(signal_date: str) -> pd.DataFrame:
    """调用E唯一规则源，构造无前视、单账户R1候选。

    这里不保留旧版完整涨停池兜底。规则数据不完整时必须抛错并停腿，否则实盘
    会在不知情的情况下退化为历史负期望的另一套策略。
    """

    return build_live_e_candidates(PROJECT_ROOT, signal_date)
