"""
策略E每日收盘后信号生成脚本。

策略条件（无前视、单账户R1、入场门禁对齐版）：
  - 40条R1规则各选信号日第一名，合并成可执行候选宇宙
  - 在候选宇宙里保留板块neutral，按信号日换手率降序取一只
  - 每日第一名若在13:30~14:30首次涨停，当日E空仓，不回补第二名
  - T+1开盘买入；按命中规则在T+2或T+3收盘卖出
  - 仅在 A/C/D 均未占用资金时触发；B已删除
  - 关键字段、成交可靠性或完整数据任一不满足时拒绝生成信号

触发时机：
  每日 15:30 后运行（A/C daily ops 和 D 盘中监控均已完成后）

输出：
  reports/strategy_e/e_signals_recent.json    最近10个交易日E信号（滚动覆盖）
  reports/strategy_e/e_signal_YYYYMMDD_candidates.csv  所有符合条件的候选

segment_retreat_state_bucket 计算逻辑（来自 src/strategy_optimizer.py）：
  current <= 3                    → weak_below_3
  current < prev1 < prev2         → retreat_2day
  current < prev1 and current<=5  → retreat_weak
  current > prev1 > prev2         → warming_2day
  其他                             → neutral  ← E 的目标状态

用法：
  python scripts/run_strategy_e_signal.py
  python scripts/run_strategy_e_signal.py --signal-date 20260616
  python scripts/run_strategy_e_signal.py --signal-date 20260616 --dry-run
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
    ERROR,
    NO_CANDIDATE,
    NO_SIGNAL_OCCUPIED,
    SIGNAL_READY,
    cleanup_legacy_daily_signal_files,
    migrate_legacy_daily_signal_files,
    save_recent_signal,
    save_recent_signal_run,
)
from src.strategy_e import (
    E_VERSION,
    build_live_e_candidates,
    load_e_spec,
    resolve_exit_offset,
)
from src.strategy_identity import ACTIVE_E_VARIANT, STRATEGY_E_LEG, normalize_strategy_record

LIMIT_DIR = PROJECT_ROOT / "data" / "raw" / "limit_list"
DAILY_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
LIVE_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv"
HIST_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
SCORED_PATH = LIVE_SCORED_PATH if LIVE_SCORED_PATH.exists() else HIST_SCORED_PATH
POSITIONS_PATH = PROJECT_ROOT / "data" / "processed" / "positions.json"
DAILY_OPS_DIR = PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_e"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "e_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "e_signal_runs_recent.json"
STRATEGY_D_SIGNAL_DIR = PROJECT_ROOT / "reports" / "strategy_d"

POSITION_PCT = 0.825
E_MIN_CIRC_MV = 0       # 不设下限
E_MAX_CIRC_MV = float("inf")
E_RESEARCH_AUDIT = {
    "window": "20240630~20260630",
    "rule": "R1_no_lookahead_single_account_entry_gate_v5_turnover_rank",
    # 候选池计数只说明有多少个历史信号可评估；独立策略指标必须再执行
    # 单账户占仓约束，上一笔退出前不得复用同一笔资金。
    "candidate_pool_before_gate_trade_count": 112,
    "candidate_pool_after_gate_trade_count": 91,
    "standalone_trade_count": 76,
    "standalone_avg_account_return": 0.03603595325965961,
    "standalone_median_account_return": 0.0172393380151361,
    "standalone_win_rate": 0.6447368421052632,
    "standalone_equity_multiple": 10.83416173854884,
    "standalone_max_drawdown": -0.24746103236951644,
    "aligned_max_profit": 0.5231929254879739,
    "aligned_max_loss": -0.17629142067722955,
    "standalone_profit_loss_ratio": 1.7756009375180655,
    "standalone_max_consecutive_losses": 3,
    "candidate_pool_equity_multiple": 15.490272044579283,
    "position_pct": POSITION_PCT,
    "source_report": "reports/current_portfolio_alignment/strict_asof_audit.json",
    "old_62_trade_reference_is_live_realisable": False,
    "entry_gate": "排除每日第一名first_time_detail_bucket=1330_1430，且不回补第二名。",
    "research_protocol": "STRICT_DISCOVERY",
    "release_eligible": False,
    "overfit_warning": "换手率最终排序来自同窗口多候选搜索，存在多重比较和过拟合风险；严格as-of只证明未使用决策时点后数据，历史结果不代表未来收益。",
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
        return [
            normalize_strategy_record(p)
            for p in (data if isinstance(data, list) else [])
            if str(p.get("status", "")) == "open"
        ]
    except Exception:
        return []


def has_ac_planned_order(signal_date: str, legs: tuple[str, ...]) -> bool:
    """A/C daily ops 是否为 signal_date 生成了 `legs` 中某条腿的计划委托。

    `legs` 必须由调用方按腿序显式声明——**只有排在自己前面的腿才有资格挡住
    本腿的信号**。2026-08-07 之前这里不分 A 和 C，谁调用都是"A 或 C 有计划
    就挡"，于是 C 事实上挡住了排在它前面的 E。M已退役，当前固定腿序为
    D>A>E>C，上游门与下游排序必须保持同一口径。

    旧 B 计划只能人工退出、不占用新开仓资金，一律排除。
    """
    if not DAILY_OPS_DIR.exists():
        return False
    wanted = {leg.strip().upper() for leg in legs}
    if not wanted:
        return False
    pattern = f"*{signal_date}*planned_orders*.csv"
    files = list(DAILY_OPS_DIR.glob(pattern))
    for f in files:
        try:
            df = pd.read_csv(f)
            if "strategy_leg" not in df.columns:
                # 没有腿标记的历史文件无法判断归属，按最保守口径当作占用。
                if len(df) > 0:
                    return True
                continue
            legs_in_file = df["strategy_leg"].astype(str).str.strip().str.upper()
            if bool((legs_in_file.isin(wanted) & legs_in_file.ne("B")).any()):
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
    """读取D盘中信号结果；D失败不占用资金，允许E继续判断。"""
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


# ── E 候选筛选 ───────────────────────────────────────────────────────────────

def load_e_candidates(signal_date: str) -> pd.DataFrame:
    """调用E唯一规则源，构造无前视、单账户R1候选。

    这里不保留旧版完整涨停池兜底。规则数据不完整时必须抛错并停腿，否则实盘
    会在不知情的情况下退化为历史负期望的另一套策略。
    """

    return build_live_e_candidates(PROJECT_ROOT, signal_date)


# ── 信号输出 ──────────────────────────────────────────────────────────────────

def build_signal(signal_date: str, candidate: pd.Series, segment_states: dict[str, str]) -> dict[str, Any]:
    seg = str(candidate.get("market_segment", ""))
    spec = load_e_spec(PROJECT_ROOT)
    exit_rule = str(candidate.get("exit_rule", ""))
    exit_offset = resolve_exit_offset(spec, exit_rule)
    return {
        "strategy_leg": STRATEGY_E_LEG,
        "strategy_family": STRATEGY_E_LEG,
        "strategy_variant": ACTIVE_E_VARIANT,
        "strategy_version": E_VERSION,
        "signal_date": signal_date,
        "ts_code": str(candidate.get("ts_code", "")),
        "name": str(candidate.get("name", candidate.get("ts_code", ""))),
        "market_segment": seg,
        "segment_retreat_state_bucket": str(candidate.get("segment_retreat_state_bucket", "neutral")),
        "segment_retreat_state_legacy_preview": segment_states.get(seg, "unknown"),
        "r1_scenario": str(candidate.get("scenario", "")),
        "r1_scenario_rank": int(candidate.get("scenario_rank", 0) or 0),
        "exit_rule": exit_rule,
        "exit_offset": exit_offset,
        "limit_data_quality": str(candidate.get("limit_data_quality", "")),
        "limit_data_source": str(candidate.get("limit_data_source", "")),
        "strategy_compatible": bool(str(candidate.get("strategy_compatible", "")).lower() in ("true", "1")),
        "circ_mv": float(candidate.get("circ_mv", 0)),
        "turnover_rate": float(candidate.get("turnover_rate", 0)),
        "final_ranking_rule": "turnover_rate_desc_then_scenario_rank_ts_code_asc",
        "limit_close": float(candidate.get("limit_close", 0)),
        "fill_probability": float(candidate.get("fill_probability", 0)),
        "allow_buy_reliable": bool(str(candidate.get("allow_buy_reliable", "")).lower() in ("true", "1")),
        "is_fill_score_reliable": bool(str(candidate.get("is_fill_score_reliable", "")).lower() in ("true", "1")),
        "planned_buy_date": next_trade_day(signal_date, 1),
        "planned_buy_price": "T+1_open",
        "planned_exit_date": next_trade_day(signal_date, exit_offset),
        "planned_exit_rule": f"T+{exit_offset}_close",
        "position_pct": POSITION_PCT,
        "research_audit": E_RESEARCH_AUDIT,
        "status": "pending",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_signal(signal: dict[str, Any], dry_run: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        migrate_existing_signals()
        save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg="E", max_trade_days=10)
    return ROLLING_SIGNAL_PATH


def migrate_existing_signals() -> None:
    """迁移并清理旧的每日E信号JSON；无新信号的交易日也会执行。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_daily_signal_files(
        OUTPUT_DIR,
        "e_signal_????????.json",
        ROLLING_SIGNAL_PATH,
        strategy_leg="E",
        max_trade_days=10,
    )
    cleanup_legacy_daily_signal_files(OUTPUT_DIR, "e_signal_????????.json")


def save_candidates(signal_date: str, candidates: pd.DataFrame, dry_run: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"e_signal_{signal_date}_candidates.csv"
    cols = [c for c in ["ts_code", "name", "market_segment", "segment_retreat_state_bucket",
                         "scenario_rank", "exit_rule", "scenario",
                         "limit_data_quality", "limit_data_source", "strategy_compatible",
                         "circ_mv", "fill_probability", "allow_buy_reliable", "is_fill_score_reliable",
                         "limit_close", "fd_amount_to_circ_mv"] if c in candidates.columns]
    # 即使候选为0也覆盖当天文件，防止同一天重跑后仍读到先前非空候选的陈旧结果。
    if not dry_run:
        candidates[cols].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def save_run_status(
    signal_date: str,
    status: str,
    reason: str,
    *,
    dry_run: bool,
    candidate_count: int | None = None,
    signal: dict[str, Any] | None = None,
    candidate: pd.Series | None = None,
) -> None:
    """记录E当日是否正常完成；不向正式信号文件写入空信号。"""

    if dry_run:
        return
    run: dict[str, Any] = {
        "signal_date": signal_date,
        "status": status,
        "reason": reason,
    }
    if candidate_count is not None:
        run["candidate_count"] = int(candidate_count)
    if signal is not None:
        run["ts_code"] = str(signal.get("ts_code", ""))
        run["name"] = str(signal.get("name", ""))
    if candidate is not None:
        run["candidate_ts_code"] = str(candidate.get("ts_code", ""))
        run["candidate_name"] = str(candidate.get("name", ""))
    save_recent_signal_run(
        RUN_STATUS_PATH,
        run,
        strategy_leg="E",
        max_trade_days=20,
    )


def inspect_blocked_e_candidates(
    signal_date: str,
    *,
    dry_run: bool,
) -> tuple[int | None, str, pd.Series | None]:
    """持仓已阻断开仓时，继续做一次只读候选检查。

    这里只回答“如果账户空仓，今天E有没有候选”，不会调用``build_signal``，
    不会写正式信号JSON，更不会提交委托。这样既保留单仓阻断，又消除“候选池未知”
    的审计盲区。
    """

    try:
        candidates = load_e_candidates(signal_date)
        save_candidates(signal_date, candidates, dry_run)
    except Exception as exc:
        return None, f"E只读候选检查失败，暂时无法判断是否有候选：{exc}", None

    candidate_count = int(len(candidates))
    if candidate_count == 0:
        return 0, "E只读候选检查为0只，即使账户空仓也不会触发", None

    selected = candidates.iloc[0]
    code = str(selected.get("ts_code", ""))
    name = str(selected.get("name", code))
    return (
        candidate_count,
        f"E只读候选检查有{candidate_count}只，第一名={code} {name}；"
        "但因当前持仓阻断，不生成正式信号",
        selected,
    )


def finish_occupied_without_e_signal(
    signal_date: str,
    blocker_reason: str,
    *,
    dry_run: bool,
) -> None:
    """保存“资金被占用”终态，同时把候选是否存在写清楚。"""

    candidate_count, candidate_reason, candidate = inspect_blocked_e_candidates(
        signal_date,
        dry_run=dry_run,
    )
    reason = f"{blocker_reason}；{candidate_reason}"
    print(f"[E信号] {reason}")
    save_run_status(
        signal_date,
        NO_SIGNAL_OCCUPIED,
        reason,
        dry_run=dry_run,
        candidate_count=candidate_count,
        candidate=candidate,
    )


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


def run_signal_generation(signal_date: str, *, dry_run: bool) -> None:
    """执行一次E信号生成，并为每个正常退出分支写明终态。"""

    print(f"[E信号] 信号日期: {signal_date}")
    if not dry_run:
        migrate_existing_signals()

    # ── 1. 检查 ABCD 是否空闲 ────────────────────────────────────────────────
    open_positions = load_open_positions()

    if has_existing_open_position(open_positions):
        occupied = [(p.get("strategy_leg","?"), p.get("ts_code","?"), p.get("planned_exit_date","?"))
                    for p in open_positions]
        finish_occupied_without_e_signal(
            signal_date,
            f"账户有未平仓头寸，E不触发；持仓={occupied}",
            dry_run=dry_run,
        )
        return

    # 腿序 D>A>E>C：排在 E 前面的是 D、A，后面是 C。
    # C 有计划时 E 必须照常出信号，由 combined_live_engine 按腿序在两者间挑；
    # 2026-08-07 之前这里连 C 一起挡，等于把 C 顶到了 E 前面。
    #
    if has_ac_planned_order(signal_date, legs=("A",)):
        finish_occupied_without_e_signal(
            signal_date,
            "A今日已生成计划委托（腿序A>E），E不触发",
            dry_run=dry_run,
        )
        return

    if has_d_position_today(signal_date, open_positions):
        finish_occupied_without_e_signal(
            signal_date,
            "D策略今日已建仓，E不触发",
            dry_run=dry_run,
        )
        return

    d_status = load_d_intraday_status(signal_date)
    if d_status["has_filled"]:
        finish_occupied_without_e_signal(
            signal_date,
            f"D盘中信号已成交或部分成交，E不触发；{d_status['summary']}",
            dry_run=dry_run,
        )
        return
    if d_status["has_failed"]:
        print(f"[E信号] D盘中第1名开仓失败，未占用资金，释放给E继续判断。{d_status['summary']}")

    print("[E信号] ABCD 今日均空闲，开始筛选 E 候选。")

    # ── 2. 旧算法只保留作日志对照；正式neutral取值来自统一R1特征链 ───────────
    segment_states = compute_segment_retreat_states(signal_date)
    print(f"[E信号] 今日各板块状态（旧算法，仅对照）: {segment_states}")

    # ── 3. 正式R1候选；任一关键数据失败都fail-closed ─────────────────────────
    try:
        candidates = load_e_candidates(signal_date)
    except Exception as exc:
        reason = f"R1候选构造失败：{exc}"
        print(f"[E信号] {reason}")
        print("[E信号] 禁止退回旧口径，今日E不生成实盘信号。")
        save_run_status(signal_date, ERROR, reason, dry_run=dry_run)
        return
    cand_path = save_candidates(signal_date, candidates, dry_run)

    if candidates.empty:
        reason = "R1每日第一名未通过neutral/成交可靠性/13:30~14:30入场门禁，E不触发且不回补第二名"
        print(f"[E信号] {reason}。")
        save_run_status(
            signal_date,
            NO_CANDIDATE,
            reason,
            dry_run=dry_run,
            candidate_count=0,
        )
        return

    print(f"[E信号] 符合条件候选: {len(candidates)} 只")

    # ── 4. final_ranking 已按换手率降序、scenario_rank/ts_code升序稳定选首位 ──
    selected = candidates.iloc[0]
    signal = build_signal(signal_date, selected, segment_states)

    # ── 5. 打印操作提示 ───────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  策略E 信号")
    print("=" * 60)
    print(f"  股票:       {signal['ts_code']}  {signal['name']}")
    print(f"  板块:       {signal['market_segment']}  ({signal['segment_retreat_state_bucket']})")
    print(f"  流通市值:   {signal['circ_mv']/10000:.1f} 亿")
    print(f"  信号日换手: {signal['turnover_rate']:.2f}%（最终排序主键：降序）")
    print(f"  成交概率:   {signal['fill_probability']:.1%}")
    print(f"  R1规则:     rank={signal['r1_scenario_rank']}  退出={signal['exit_rule']}")
    print(f"  买入计划:   {signal['planned_buy_date']} 开盘价买入  目标仓位{signal['position_pct']:.1%}")
    print(f"  卖出计划:   {signal['planned_exit_date']} 收盘前卖出")
    print("=" * 60)
    print()
    print(f"  候选完整列表: {cand_path}")

    # ── 6. 写信号文件 ─────────────────────────────────────────────────────────
    sig_path = save_signal(signal, dry_run)
    if dry_run:
        print(f"  [dry-run] 信号未写入文件（路径将为: {sig_path}）")
        print(f"\n  信号内容:")
        print(json.dumps(signal, ensure_ascii=False, indent=2))
    else:
        print(f"  信号已保存: {sig_path}")
    save_run_status(
        signal_date,
        SIGNAL_READY,
        f"E已生成唯一入选信号：{signal['ts_code']} {signal['name']}",
        dry_run=dry_run,
        candidate_count=len(candidates),
        signal=signal,
    )

    print("\n[E信号] 完成。")


def main() -> None:
    parser = argparse.ArgumentParser(description="策略E每日收盘后信号生成")
    parser.add_argument("--signal-date", default=None, help="信号日期 YYYYMMDD，不传则自动推断")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写文件")
    args = parser.parse_args()
    signal_date = args.signal_date or resolve_signal_date()
    try:
        run_signal_generation(signal_date, dry_run=args.dry_run)
    except Exception as exc:
        # 未预期异常也要在当天留下ERROR，审计不能把崩溃误认为正常空信号。
        save_run_status(
            signal_date,
            ERROR,
            f"E信号脚本异常退出：{type(exc).__name__}: {exc}",
            dry_run=args.dry_run,
        )
        raise


if __name__ == "__main__":
    main()
