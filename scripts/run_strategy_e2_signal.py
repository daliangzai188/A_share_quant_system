"""
策略E2每日收盘后信号生成脚本。

策略条件：
  - 市场板块处于"neutral"回撤状态（segment_retreat_state_bucket=neutral）
  - 从符合条件的今日涨停股中选 circ_mv（流通市值）最小的1只
  - T+1开盘买入，T+2收盘卖出（与A/C持仓规则相同）
  - 仅在 A/C/D 均未占用资金时触发；B已删除
  - 必须使用 limit_list_d/full 完整口径，且 strategy_compatible=True

触发时机：
  每日 15:30 后运行（A/C daily ops 和 D 盘中监控均已完成后）

输出：
  reports/strategy_e2/e2_signals_recent.json    最近10个交易日E2信号（滚动覆盖）
  reports/strategy_e2/e2_signal_YYYYMMDD_candidates.csv  所有符合条件的候选

segment_retreat_state_bucket 计算逻辑（来自 src/strategy_optimizer.py）：
  current <= 3                    → weak_below_3
  current < prev1 < prev2         → retreat_2day
  current < prev1 and current<=5  → retreat_weak
  current > prev1 > prev2         → warming_2day
  其他                             → neutral  ← E2 的目标状态

用法：
  python scripts/run_strategy_e2_signal.py
  python scripts/run_strategy_e2_signal.py --signal-date 20260616
  python scripts/run_strategy_e2_signal.py --signal-date 20260616 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rolling_signal_store import (
    cleanup_legacy_daily_signal_files,
    migrate_legacy_daily_signal_files,
    save_recent_signal,
)

LIMIT_DIR = PROJECT_ROOT / "data" / "raw" / "limit_list"
DAILY_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
LIVE_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv"
SCORED_PATH = LIVE_SCORED_PATH if LIVE_SCORED_PATH.exists() else PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
POSITIONS_PATH = PROJECT_ROOT / "data" / "processed" / "positions.json"
DAILY_OPS_DIR = PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_e2"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "e2_signals_recent.json"
STRATEGY_D_SIGNAL_DIR = PROJECT_ROOT / "reports" / "strategy_d"

POSITION_PCT = 0.825
E2_MIN_CIRC_MV = 0       # 不设下限
E2_MAX_CIRC_MV = float("inf")
E2_VERSION = "E2_segment_neutral_circ_mv_asc_v1"
E2_RESEARCH_AUDIT = {
    "window": "recent_2y",
    "added_trade_count": 62,
    "added_avg_account_return": 0.044086,
    "added_median_account_return": 0.017896,
    "added_win_rate": 0.645161,
    "added_max_profit": 0.341127,
    "added_max_loss": -0.067907,
    "combo_equity_multiple": 3952.8312,
    "combo_max_drawdown": -0.197489,
    "position_pct": POSITION_PCT,
    "source_report": "reports/strategy_expansion/abcd_expansion_search_summary.csv",
}


# ── 交易日工具 ────────────────────────────────────────────────────────────────

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


# ── 板块分类 ─────────────────────────────────────────────────────────────────

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


# ── segment_retreat_state_bucket 计算 ────────────────────────────────────────

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


# ── ABCD 空闲状态检测 ─────────────────────────────────────────────────────────

def load_open_positions() -> list[dict[str, Any]]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        return [p for p in (data if isinstance(data, list) else []) if str(p.get("status","")) == "open"]
    except Exception:
        return []


def has_abc_planned_order(signal_date: str) -> bool:
    """检查 A/C daily ops 是否为 signal_date 生成了计划委托；旧B计划不占用资金。"""
    if not DAILY_OPS_DIR.exists():
        return False
    pattern = f"*{signal_date}*planned_orders*.csv"
    files = list(DAILY_OPS_DIR.glob(pattern))
    for f in files:
        try:
            df = pd.read_csv(f)
            if "strategy_leg" in df.columns:
                df = df[df["strategy_leg"].astype(str).str.upper() != "B"].copy()
            if len(df) > 0:
                return True
        except Exception:
            pass
    return False


def has_d_position_today(signal_date: str, open_positions: list[dict[str, Any]]) -> bool:
    """D策略是否在 signal_date 已建仓。"""
    for pos in open_positions:
        if str(pos.get("strategy_leg", "")) == "D" and str(pos.get("signal_date", "")) == signal_date:
            return True
    return False


def load_d_intraday_status(signal_date: str) -> dict[str, Any]:
    """读取D盘中信号结果；D失败不占用资金，允许E2继续判断。"""
    path = STRATEGY_D_SIGNAL_DIR / f"intraday_signals_{signal_date}.csv"
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "has_buy_signal": False,
        "has_filled": False,
        "has_failed": False,
        "summary": "",
    }
    if not path.exists():
        return result
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        result["summary"] = f"D盘中信号文件读取失败：{exc}"
        return result
    if df.empty or "signal_type" not in df.columns:
        return result

    buy_df = df[df["signal_type"].astype(str).str.upper().eq("BUY")].copy()
    if buy_df.empty:
        return result
    result["has_buy_signal"] = True

    status = buy_df.get("order_status", pd.Series("", index=buy_df.index)).fillna("").astype(str)
    filled_qty = pd.to_numeric(
        buy_df.get("filled_qty", pd.Series(0, index=buy_df.index)),
        errors="coerce",
    ).fillna(0)
    filled_amount = pd.to_numeric(
        buy_df.get("filled_amount", pd.Series(0.0, index=buy_df.index)),
        errors="coerce",
    ).fillna(0.0)

    has_filled = bool(status.isin({"FILLED", "PENDING_OR_PARTIAL"}).any() and (filled_qty > 0).any())
    has_failed = bool(
        status.isin({"REJECTED_TERMINAL", "REJECTED_BY_QMT", "ORDER_EXCEPTION"}).any()
        or ((status.ne("FILLED")) & (filled_qty <= 0)).all()
    )
    result["has_filled"] = has_filled
    result["has_failed"] = has_failed and not has_filled

    latest = buy_df.iloc[-1]
    result["summary"] = (
        f"{latest.get('ts_code', '')} {latest.get('name', '')} "
        f"状态={latest.get('order_status', '') or 'UNKNOWN'} "
        f"成交股数={int(filled_qty.iloc[-1]) if len(filled_qty) else 0} "
        f"成交金额={float(filled_amount.iloc[-1]) if len(filled_amount) else 0.0:.2f} "
        f"失败原因={latest.get('failure_reason', '')}"
    )
    return result


def has_existing_open_position(open_positions: list[dict[str, Any]]) -> bool:
    """账户是否持有任何未平仓头寸（含前日未卖出的 A/C/D，以及待手动处理的历史B仓）。"""
    return len(open_positions) > 0


# ── E2 候选筛选 ───────────────────────────────────────────────────────────────

def load_e2_candidates(signal_date: str, segment_states: dict[str, str]) -> pd.DataFrame:
    """
    从 limit_up_fill_scored.csv 加载今日涨停股，附加 segment_retreat_state_bucket，
    过滤出 E2 符合条件的候选。
    """
    if not SCORED_PATH.exists():
        return pd.DataFrame()

    df = pd.read_csv(SCORED_PATH, low_memory=False)
    df["trade_date"] = df["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    df = df[df["trade_date"] == signal_date].copy()

    if df.empty:
        return pd.DataFrame()

    required_columns = [
        "market_segment",
        "is_st",
        "allow_buy_reliable",
        "is_fill_score_reliable",
        "circ_mv",
        "limit_data_quality",
        "strategy_compatible",
    ]
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        print(f"[E2信号] 缺少必需字段 {missing}，E2 不触发。")
        return pd.DataFrame()

    # 附加 segment_retreat_state_bucket
    df["segment_retreat_state_bucket"] = df["market_segment"].astype(str).map(
        lambda seg: segment_states.get(seg, "unknown")
    )

    # 基础过滤
    df = df[df["segment_retreat_state_bucket"] == "neutral"].copy()
    df = df[df["limit_data_quality"].fillna("").astype(str).eq("full")].copy()
    df = df[df["strategy_compatible"].fillna("").astype(str).str.lower().isin(["true", "1"])].copy()
    df = df[~df["is_st"].astype(str).str.lower().isin(["true", "1"])].copy()
    df = df[df["allow_buy_reliable"].astype(str).str.lower().isin(["true", "1"])].copy()
    df = df[df["is_fill_score_reliable"].astype(str).str.lower().isin(["true", "1"])].copy()

    df["circ_mv"] = pd.to_numeric(df["circ_mv"], errors="coerce")
    df = df[df["circ_mv"].notna()].copy()
    return df.sort_values("circ_mv", ascending=True).reset_index(drop=True)


# ── 信号输出 ──────────────────────────────────────────────────────────────────

def build_signal(signal_date: str, candidate: pd.Series, segment_states: dict[str, str]) -> dict[str, Any]:
    seg = str(candidate.get("market_segment", ""))
    return {
        "strategy_leg": "E2",
        "strategy_version": E2_VERSION,
        "signal_date": signal_date,
        "ts_code": str(candidate.get("ts_code", "")),
        "name": str(candidate.get("name", candidate.get("ts_code", ""))),
        "market_segment": seg,
        "segment_retreat_state_bucket": segment_states.get(seg, "neutral"),
        "limit_data_quality": str(candidate.get("limit_data_quality", "")),
        "limit_data_source": str(candidate.get("limit_data_source", "")),
        "strategy_compatible": bool(str(candidate.get("strategy_compatible", "")).lower() in ("true", "1")),
        "circ_mv": float(candidate.get("circ_mv", 0)),
        "limit_close": float(candidate.get("limit_close", 0)),
        "fill_probability": float(candidate.get("fill_probability", 0)),
        "allow_buy_reliable": bool(str(candidate.get("allow_buy_reliable", "")).lower() in ("true", "1")),
        "is_fill_score_reliable": bool(str(candidate.get("is_fill_score_reliable", "")).lower() in ("true", "1")),
        "planned_buy_date": next_trade_day(signal_date, 1),
        "planned_buy_price": "T+1_open",
        "planned_exit_date": next_trade_day(signal_date, 2),
        "planned_exit_rule": "T+2_close",
        "position_pct": POSITION_PCT,
        "research_audit": E2_RESEARCH_AUDIT,
        "status": "pending",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_signal(signal: dict[str, Any], dry_run: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        migrate_existing_signals()
        save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg="E2", max_trade_days=10)
    return ROLLING_SIGNAL_PATH


def migrate_existing_signals() -> None:
    """迁移并清理旧的每日E2信号JSON；无新信号的交易日也会执行。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_daily_signal_files(
        OUTPUT_DIR,
        "e2_signal_????????.json",
        ROLLING_SIGNAL_PATH,
        strategy_leg="E2",
        max_trade_days=10,
    )
    cleanup_legacy_daily_signal_files(OUTPUT_DIR, "e2_signal_????????.json")


def save_candidates(signal_date: str, candidates: pd.DataFrame, dry_run: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"e2_signal_{signal_date}_candidates.csv"
    cols = [c for c in ["ts_code", "name", "market_segment", "segment_retreat_state_bucket",
                         "limit_data_quality", "limit_data_source", "strategy_compatible",
                         "circ_mv", "fill_probability", "allow_buy_reliable", "is_fill_score_reliable",
                         "limit_close", "fd_amount_to_circ_mv"] if c in candidates.columns]
    if not dry_run and not candidates.empty:
        candidates[cols].to_csv(path, index=False, encoding="utf-8-sig")
    return path


# ── 主流程 ────────────────────────────────────────────────────────────────────

def resolve_signal_date() -> str:
    """推断今日信号日期：实盘打分表中的最新 trade_date。"""
    if SCORED_PATH.exists():
        try:
            df = pd.read_csv(SCORED_PATH, usecols=["trade_date"], low_memory=False)
            dates = df["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
            latest = dates.max()
            if latest and latest != "nan":
                return latest
        except Exception:
            pass
    return datetime.now().strftime("%Y%m%d")


def main() -> None:
    parser = argparse.ArgumentParser(description="策略E2每日收盘后信号生成")
    parser.add_argument("--signal-date", default=None, help="信号日期 YYYYMMDD，不传则自动推断")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写文件")
    args = parser.parse_args()

    signal_date = args.signal_date or resolve_signal_date()
    print(f"[E2信号] 信号日期: {signal_date}")
    if not args.dry_run:
        migrate_existing_signals()

    # ── 1. 检查 ABCD 是否空闲 ────────────────────────────────────────────────
    open_positions = load_open_positions()

    if has_existing_open_position(open_positions):
        occupied = [(p.get("strategy_leg","?"), p.get("ts_code","?"), p.get("planned_exit_date","?"))
                    for p in open_positions]
        print(f"[E2信号] 账户有未平仓头寸，E2 不触发。持仓: {occupied}")
        return

    if has_abc_planned_order(signal_date):
        print(f"[E2信号] A/C 今日已生成计划委托，E2 不触发。")
        return

    if has_d_position_today(signal_date, open_positions):
        print(f"[E2信号] D策略今日已建仓，E2 不触发。")
        return

    d_status = load_d_intraday_status(signal_date)
    if d_status["has_filled"]:
        print(f"[E2信号] D盘中信号已成交或部分成交，E2 不触发。{d_status['summary']}")
        return
    if d_status["has_failed"]:
        print(f"[E2信号] D盘中第1名开仓失败，未占用资金，释放给E2继续判断。{d_status['summary']}")

    print("[E2信号] ABCD 今日均空闲，开始筛选 E2 候选。")

    # ── 2. 计算 segment_retreat_state_bucket ─────────────────────────────────
    segment_states = compute_segment_retreat_states(signal_date)
    neutral_segs = [seg for seg, st in segment_states.items() if st == "neutral"]
    print(f"[E2信号] 今日各板块状态: {segment_states}")
    print(f"[E2信号] neutral 板块: {neutral_segs if neutral_segs else '无'}")

    if not neutral_segs:
        print("[E2信号] 今日无 neutral 板块，E2 不触发。")
        return

    # ── 3. 加载并筛选候选 ─────────────────────────────────────────────────────
    candidates = load_e2_candidates(signal_date, segment_states)
    cand_path = save_candidates(signal_date, candidates, args.dry_run)

    if candidates.empty:
        print("[E2信号] 今日无符合条件的 E2 候选股，E2 不触发。")
        return

    print(f"[E2信号] 符合条件候选: {len(candidates)} 只")

    # ── 4. 选最小流通市值 ─────────────────────────────────────────────────────
    selected = candidates.iloc[0]
    signal = build_signal(signal_date, selected, segment_states)

    # ── 5. 打印操作提示 ───────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  策略E2 信号")
    print("=" * 60)
    print(f"  股票:       {signal['ts_code']}  {signal['name']}")
    print(f"  板块:       {signal['market_segment']}  ({signal['segment_retreat_state_bucket']})")
    print(f"  流通市值:   {signal['circ_mv']/10000:.1f} 亿")
    print(f"  成交概率:   {signal['fill_probability']:.1%}")
    print(f"  买入计划:   {signal['planned_buy_date']} 开盘价买入  目标仓位{signal['position_pct']:.1%}")
    print(f"  卖出计划:   {signal['planned_exit_date']} 收盘前卖出")
    print("=" * 60)
    print()
    print(f"  候选完整列表: {cand_path}")

    # ── 6. 写信号文件 ─────────────────────────────────────────────────────────
    sig_path = save_signal(signal, args.dry_run)
    if args.dry_run:
        print(f"  [dry-run] 信号未写入文件（路径将为: {sig_path}）")
        print(f"\n  信号内容:")
        print(json.dumps(signal, ensure_ascii=False, indent=2))
    else:
        print(f"  信号已保存: {sig_path}")

    print("\n[E2信号] 完成。")


if __name__ == "__main__":
    main()
