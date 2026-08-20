"""策略N双分支每日收盘信号生成；不直接提交委托。

N是正式组合最低优先级腿。脚本会先用与E相同的80日bucket特征链计算N第一名，
第一分支无候选时才检查3_8连板/mixed情绪补充分支，再检查账户持仓以及
A/M/E/C上游计划；被占用时只保存候选审计，不生成正式信号。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_strategy_e_signal import (  # noqa: E402
    has_ac_planned_order,
    has_existing_open_position,
    load_open_positions,
    next_trade_day,
    resolve_signal_date,
)
from src.rolling_signal_store import (  # noqa: E402
    NO_CANDIDATE,
    NO_SIGNAL_OCCUPIED,
    SIGNAL_READY,
    save_recent_signal,
    save_recent_signal_run,
    signal_by_signal_date,
)
from src.strategy_e import load_bucketed_signal_pool  # noqa: E402
from src.strategy_n import (  # noqa: E402
    N_VERSION,
    load_n_spec,
    n_live_entry_block_reason,
    resolve_exit_offset,
    select_n_daily_picks,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_n"
SIGNAL_PATH = OUTPUT_DIR / "n_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "n_signal_runs_recent.json"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _higher_signal_exists(signal_date: str, leg: str) -> bool:
    path = PROJECT_ROOT / "reports" / f"strategy_{leg.lower()}" / f"{leg.lower()}_signals_recent.json"
    return signal_by_signal_date(path, signal_date) is not None


def higher_priority_blocker(signal_date: str) -> str:
    """返回D/A/M/E/C中已经占用当日候选权的首个原因。"""

    positions = load_open_positions()
    if has_existing_open_position(positions):
        held = [(str(p.get("strategy_leg", "")), str(p.get("ts_code", ""))) for p in positions]
        return f"账户有未平仓头寸{held}"
    if has_ac_planned_order(signal_date, legs=("A",)):
        return "A当日已有计划"
    if _higher_signal_exists(signal_date, "M"):
        return "M当日已有正式信号"
    if _higher_signal_exists(signal_date, "E"):
        return "E当日已有正式信号"
    if has_ac_planned_order(signal_date, legs=("C",)):
        return "C当日已有计划"
    return ""


def save_candidates(signal_date: str, candidates: pd.DataFrame, dry_run: bool) -> Path:
    path = OUTPUT_DIR / f"n_signal_{signal_date}_candidates.csv"
    columns = [
        value for value in (
            "trade_date", "ts_code", "name", "market_segment",
            "segment_limit_max_height_bucket", "segment_retreat_state_bucket",
            "market_chain_count_bucket", "market_emotion_state_bucket",
            "n_branch", "n_rule_id",
            "first_time", "first_time_minutes", "circ_mv", "limit_close",
            "fill_probability", "allow_buy_reliable", "is_fill_score_reliable",
            "is_fd_amount_abnormal", "strategy_compatible",
        ) if value in candidates.columns
    ]
    if not dry_run:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        candidates[columns].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def record_run(
    signal_date: str,
    status: str,
    reason: str,
    *,
    dry_run: bool,
    candidate_count: int | None = None,
    signal: dict[str, Any] | None = None,
) -> None:
    if dry_run:
        return
    row: dict[str, Any] = {
        "signal_date": signal_date,
        "status": status,
        "reason": reason,
        "strategy_version": N_VERSION,
    }
    if candidate_count is not None:
        row["candidate_count"] = int(candidate_count)
    if signal:
        row.update({"ts_code": signal["ts_code"], "name": signal["name"]})
    save_recent_signal_run(RUN_STATUS_PATH, row, strategy_leg="N", max_trade_days=20)


def build_signal(signal_date: str, row: pd.Series, spec: dict[str, Any]) -> dict[str, Any]:
    offset = resolve_exit_offset(spec)
    return {
        "strategy_leg": "N",
        "strategy_version": N_VERSION,
        "signal_date": signal_date,
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "market_segment": str(row.get("market_segment", "")),
        "segment_limit_max_height_bucket": str(row.get("segment_limit_max_height_bucket", "")),
        "segment_retreat_state_bucket": str(row.get("segment_retreat_state_bucket", "")),
        "market_chain_count_bucket": str(row.get("market_chain_count_bucket", "")),
        "market_emotion_state_bucket": str(row.get("market_emotion_state_bucket", "")),
        "n_branch": str(row.get("n_branch", "")),
        "n_rule_id": str(row.get("n_rule_id", "")),
        "first_time": str(row.get("first_time", "")),
        "first_time_minutes": float(pd.to_numeric(row.get("first_time_minutes"), errors="coerce")),
        "circ_mv": float(pd.to_numeric(row.get("circ_mv"), errors="coerce")),
        "limit_close": float(pd.to_numeric(row.get("limit_close"), errors="coerce")),
        "fill_probability": float(pd.to_numeric(row.get("fill_probability"), errors="coerce")),
        "allow_buy_reliable": bool(str(row.get("allow_buy_reliable", "")).lower() in {"1", "true", "yes"}),
        "is_fill_score_reliable": bool(str(row.get("is_fill_score_reliable", "")).lower() in {"1", "true", "yes"}),
        "planned_buy_date": next_trade_day(signal_date, 1),
        "planned_buy_price": "T+1_open",
        "planned_exit_date": next_trade_day(signal_date, offset),
        "planned_exit_rule": f"T+{offset}_close",
        "position_pct": float(spec.get("position_pct", 0.825)),
        "status": "pending",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="策略N最低优先级补位信号")
    parser.add_argument("--signal-date", help="信号日YYYYMMDD，不填自动推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    args = parser.parse_args()

    config = load_config()
    spec = load_n_spec(config)
    signal_date = args.signal_date or resolve_signal_date()
    print(f"[N信号] 信号日期: {signal_date}")
    if not bool(spec.get("enabled", False)):
        reason = "strategy_n.enabled=false"
        print(f"[N信号] {reason}")
        record_run(signal_date, NO_SIGNAL_OCCUPIED, reason, dry_run=args.dry_run)
        return

    try:
        pool = load_bucketed_signal_pool(PROJECT_ROOT, signal_date)
        candidates = select_n_daily_picks(pool, spec, signal_date=signal_date)
        save_candidates(signal_date, candidates, args.dry_run)
    except Exception as exc:
        reason = f"N特征或规则计算失败：{exc}"
        print(f"[N信号] ⚠️ {reason}")
        record_run(signal_date, "ERROR", reason, dry_run=args.dry_run)
        return

    if candidates.empty:
        reason = "N第一分支与补充分支均无候选"
        print(f"[N信号] 不触发：{reason}")
        record_run(signal_date, NO_CANDIDATE, reason, dry_run=args.dry_run, candidate_count=0)
        return

    risk_blocker = n_live_entry_block_reason(PROJECT_ROOT, config)
    if risk_blocker:
        row = candidates.iloc[0]
        reason = f"N新增开仓门禁阻断：{risk_blocker}"
        print(f"[N信号] {reason}；候选={row.get('ts_code','')} {row.get('name','')}")
        record_run(
            signal_date,
            NO_SIGNAL_OCCUPIED,
            reason,
            dry_run=args.dry_run,
            candidate_count=1,
        )
        return

    blocker = higher_priority_blocker(signal_date)
    if blocker:
        reason = f"{blocker}，N仅保留只读候选，不生成正式信号"
        row = candidates.iloc[0]
        print(f"[N信号] {reason}；候选={row.get('ts_code','')} {row.get('name','')}")
        record_run(signal_date, NO_SIGNAL_OCCUPIED, reason, dry_run=args.dry_run, candidate_count=1)
        return

    signal = build_signal(signal_date, candidates.iloc[0], spec)
    print(
        f"[N信号] ✅ 命中 {signal['ts_code']} {signal['name']}；"
        f"分支={signal['n_branch']}；"
        f"{signal['planned_buy_date']}开盘买，{signal['planned_exit_date']}收盘卖"
    )
    if not bool(spec.get("live_order_enabled", False)):
        print("[N信号] live_order_enabled=false：仅生成信号，不会进入实盘计划。")
    if args.dry_run:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    save_recent_signal(SIGNAL_PATH, signal, strategy_leg="N", max_trade_days=10)
    record_run(
        signal_date, SIGNAL_READY, f"N分支{signal['n_branch']}第一名通过全部条件",
        dry_run=False, candidate_count=1, signal=signal,
    )
    print(f"[N信号] 已写入 {SIGNAL_PATH}")


if __name__ == "__main__":
    main()
