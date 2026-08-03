"""
策略 L 龙头策略每日收盘后信号生成脚本。

重要说明：
  - 本脚本只生成 L 独立策略信号文件，不提交任何实盘委托。
  - L 是否真正进入实盘计划，由 config/config.json 的总策略模式控制：
      active_strategy_profile.mode = 2        L 独立策略模式
      active_strategy_profile.mode = 3        model=3 自动切换模式，L 可按规则补位/替换
  - mode=2 还需要 strategy_l.enabled=true 且 strategy_l.live_order_enabled=true。
  - mode=3 还需要 strategy_model3.enabled=true 且 strategy_model3.live_order_enabled=true。

当前接入的 L 版本：
  L2 = L_theme_mainline_leader
       + 排除 segment_retreat_state_bucket=retreat_2day
       + 排除 segment_limit_down_count_bucket=3_8
       + 排除 theme_limit_count=30

L_theme_mainline_leader 本体条件：
  - theme_data_available = true
  - theme_is_mainline = true，也就是行业/题材热度排名 <= 3
  - theme_leader_rank = 1，也就是该行业/题材内龙头排序第 1

输出：
  reports/strategy_l/l_signals_recent.json
  reports/strategy_l/l_signal_YYYYMMDD_candidates.csv
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

from src.utils.config import load_json_config
from src.rolling_signal_store import (
    ERROR,
    NO_CANDIDATE,
    SIGNAL_READY,
    cleanup_legacy_daily_signal_files,
    migrate_legacy_daily_signal_files,
    save_recent_signal,
    save_recent_signal_run,
)


LIVE_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv"
LIVE_THEME_FEATURE_PATH = PROJECT_ROOT / "data" / "processed" / "live_theme_heat_features.csv"
LIVE_MARKET_EMOTION_FEATURE_PATH = PROJECT_ROOT / "data" / "processed" / "live_market_emotion_features.csv"
SCORED_PATH = LIVE_SCORED_PATH if LIVE_SCORED_PATH.exists() else PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
THEME_FEATURE_PATH = LIVE_THEME_FEATURE_PATH if LIVE_THEME_FEATURE_PATH.exists() else PROJECT_ROOT / "data" / "processed" / "theme_heat_features.csv"
MARKET_EMOTION_FEATURE_PATH = (
    LIVE_MARKET_EMOTION_FEATURE_PATH
    if LIVE_MARKET_EMOTION_FEATURE_PATH.exists()
    else PROJECT_ROOT / "data" / "processed" / "market_emotion_features.csv"
)
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "l_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "l_signal_runs_recent.json"


def load_open_dates() -> list[str]:
    if not CALENDAR_PATH.exists():
        return []
    cal = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    if "is_open" in cal.columns:
        cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    return sorted(cal["cal_date"].astype(str).tolist())


def next_trade_day(date_str: str, n: int = 1) -> str:
    dates = load_open_dates()
    future = [d for d in dates if d > date_str]
    if len(future) >= n:
        return future[n - 1]
    cur = datetime.strptime(date_str, "%Y%m%d").date()
    count = 0
    while count < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            count += 1
    return cur.strftime("%Y%m%d")


def resolve_signal_date() -> str:
    if SCORED_PATH.exists():
        try:
            dates = pd.read_csv(SCORED_PATH, usecols=["trade_date"], low_memory=False)["trade_date"]
            dates = dates.astype(str).str.replace(r"\.0$", "", regex=True)
            latest = dates.max()
            if latest and latest != "nan":
                return latest
        except Exception:
            pass
    return datetime.now().strftime("%Y%m%d")


def bucket_segment_limit_down_count(value: Any) -> str:
    """segment_limit_down_count_bucket 的实盘分桶口径。

    必须和 src/factors.py 中历史回测使用的分桶保持一致：
      [-inf,1)  -> lt_1
      [1,3)     -> 1_3
      [3,8)     -> 3_8
      [8,15)    -> 8_15
      [15,inf)  -> gte_15
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if number < 1:
        return "lt_1"
    if number < 3:
        return "1_3"
    if number < 8:
        return "3_8"
    if number < 15:
        return "8_15"
    return "gte_15"


def classify_segment_retreat_state(current: Any, prev1: Any, prev2: Any) -> str:
    """segment_retreat_state_bucket 的实盘计算口径。

    必须和 E2/历史特征口径一致：
      current <= 3                    -> weak_below_3
      current < prev1 < prev2         -> retreat_2day
      current < prev1 and current<=5  -> retreat_weak
      current > prev1 > prev2         -> warming_2day
      其他                            -> neutral
    """
    try:
        c = float(current)
        p1 = float(prev1)
        p2 = float(prev2)
    except (TypeError, ValueError):
        return "unknown"
    if pd.isna(c) or pd.isna(p1) or pd.isna(p2):
        return "unknown"
    if c <= 3:
        return "weak_below_3"
    if c < p1 < p2:
        return "retreat_2day"
    if c < p1 and c <= 5:
        return "retreat_weak"
    if c > p1 > p2:
        return "warming_2day"
    return "neutral"


def normalize_bool(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(str).str.lower().isin({"true", "1", "yes"})


def merge_theme_features(data: pd.DataFrame) -> pd.DataFrame:
    if not THEME_FEATURE_PATH.exists():
        return data
    theme = pd.read_csv(THEME_FEATURE_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    columns = [
        "trade_date",
        "ts_code",
        "theme_data_available",
        "theme_source_column",
        "theme_name",
        "theme_limit_count",
        "theme_limit_height",
        "theme_chain_count",
        "theme_heat_score",
        "theme_heat_rank",
        "theme_leader_rank",
        "theme_height_rank",
        "theme_is_mainline",
        "same_theme_limit_count",
    ]
    columns = [column for column in columns if column in theme.columns]
    return data.merge(theme[columns], on=["trade_date", "ts_code"], how="left", validate="one_to_one")


def merge_market_emotion_features(data: pd.DataFrame) -> pd.DataFrame:
    if not MARKET_EMOTION_FEATURE_PATH.exists():
        return data
    emotion = pd.read_csv(
        MARKET_EMOTION_FEATURE_PATH,
        dtype={"trade_date": str, "market_segment": str},
        low_memory=False,
    )
    columns = [
        "trade_date",
        "market_segment",
        "segment_limit_up_count_emotion",
        "segment_limit_up_count_emotion_prev1",
        "segment_limit_up_count_emotion_prev2",
        "segment_limit_down_count",
        "market_chain_count",
        "segment_emotion_state",
        "market_emotion_state",
    ]
    columns = [column for column in columns if column in emotion.columns]
    emotion = emotion[columns].drop_duplicates(["trade_date", "market_segment"])
    merged = data.merge(emotion, on=["trade_date", "market_segment"], how="left", validate="many_to_one")
    merged["segment_retreat_state_bucket"] = merged.apply(
        lambda row: classify_segment_retreat_state(
            row.get("segment_limit_up_count_emotion"),
            row.get("segment_limit_up_count_emotion_prev1"),
            row.get("segment_limit_up_count_emotion_prev2"),
        ),
        axis=1,
    )
    merged["segment_limit_down_count_bucket"] = merged["segment_limit_down_count"].map(bucket_segment_limit_down_count)
    merged["market_chain_count_bucket"] = pd.cut(
        pd.to_numeric(merged.get("market_chain_count"), errors="coerce"),
        bins=[-float("inf"), 3, 8, 15, 30, float("inf")],
        labels=["lt_3", "3_8", "8_15", "15_30", "gte_30"],
    ).astype(str)
    return merged


def load_l_candidates(signal_date: str) -> tuple[pd.DataFrame, list[str]]:
    reasons: list[str] = []
    if not SCORED_PATH.exists():
        return pd.DataFrame(), [f"缺少成交评分文件：{SCORED_PATH}"]

    data = pd.read_csv(SCORED_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    data["trade_date"] = data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data = data[data["trade_date"].eq(signal_date)].copy()
    if data.empty:
        return pd.DataFrame(), [f"limit_up_fill_scored.csv 中没有 {signal_date} 数据"]

    data = merge_theme_features(data)
    data = merge_market_emotion_features(data)

    required = [
        "market_segment",
        "limit_data_quality",
        "strategy_compatible",
        "is_st",
        "allow_buy_reliable",
        "is_fill_score_reliable",
        "theme_data_available",
        "theme_is_mainline",
        "theme_leader_rank",
        "theme_heat_rank",
        "theme_height_rank",
        "theme_limit_count",
        "segment_retreat_state_bucket",
        "segment_limit_down_count_bucket",
        "limit_times",
        "first_time",
        "fd_amount_to_circ_mv",
        "limit_close",
    ]
    missing = [column for column in required if column not in data.columns]
    if missing:
        return pd.DataFrame(), [f"L信号字段不完整，缺少：{missing}"]

    data["theme_data_available"] = normalize_bool(data["theme_data_available"])
    data["theme_is_mainline"] = normalize_bool(data["theme_is_mainline"])
    data["strategy_compatible"] = normalize_bool(data["strategy_compatible"])
    data["allow_buy_reliable"] = normalize_bool(data["allow_buy_reliable"])
    data["is_fill_score_reliable"] = normalize_bool(data["is_fill_score_reliable"])
    data["is_st"] = normalize_bool(data["is_st"])
    for column in ["theme_leader_rank", "theme_heat_rank", "theme_height_rank", "theme_limit_count", "limit_times", "fd_amount_to_circ_mv", "limit_close"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["first_time_minutes"] = pd.to_numeric(data["first_time"], errors="coerce").fillna(999999)
    data["first_time_detail_bucket"] = pd.cut(
        data["first_time_minutes"],
        bins=[-float("inf"), 570, 600, 660, 810, 870, float("inf")],
        labels=["open_auction", "before_1000", "1000_1100", "1100_1330", "1330_1430", "after_1430"],
    ).astype(str)

    # L_theme_mainline_leader 本体条件。
    candidates = data[
        data["theme_data_available"]
        & data["theme_is_mainline"]
        & data["theme_leader_rank"].eq(1)
    ].copy()
    reasons.append(f"L主线龙头候选={len(candidates)}")

    # L2 收益优先三条件过滤。
    candidates = candidates[candidates["segment_retreat_state_bucket"].astype(str).ne("retreat_2day")].copy()
    candidates = candidates[candidates["segment_limit_down_count_bucket"].astype(str).ne("3_8")].copy()
    candidates = candidates[pd.to_numeric(candidates["theme_limit_count"], errors="coerce").ne(30.0)].copy()
    reasons.append(f"L2过滤后候选={len(candidates)}")

    # 实盘基础安全过滤：必须是完整涨停数据、非ST、成交评分可靠、策略兼容。
    candidates = candidates[candidates["limit_data_quality"].astype(str).eq("full")].copy()
    candidates = candidates[candidates["strategy_compatible"]].copy()
    candidates = candidates[~candidates["is_st"]].copy()
    candidates = candidates[candidates["allow_buy_reliable"] & candidates["is_fill_score_reliable"]].copy()
    reasons.append(f"实盘基础过滤后候选={len(candidates)}")

    sort_columns = ["theme_heat_rank", "theme_leader_rank", "theme_height_rank", "limit_times", "first_time_minutes", "fd_amount_to_circ_mv"]
    ascending = [True, True, True, False, True, False]
    candidates = candidates.sort_values(sort_columns, ascending=ascending).reset_index(drop=True)
    return candidates, reasons


def build_signal(signal_date: str, candidate: pd.Series, config: dict[str, Any]) -> dict[str, Any]:
    l_config = config.get("strategy_l", {})
    return {
        "strategy_leg": "L",
        "strategy_version": str(l_config.get("variant", "L2")),
        "strategy_description": str(l_config.get("variant_description", "L2 龙头策略")),
        "signal_date": signal_date,
        "ts_code": str(candidate.get("ts_code", "")),
        "name": str(candidate.get("name", candidate.get("ts_code", ""))),
        "market_segment": str(candidate.get("market_segment", "")),
        "theme_name": str(candidate.get("theme_name", "")),
        "theme_heat_rank": float(candidate.get("theme_heat_rank", 0) or 0),
        "theme_leader_rank": float(candidate.get("theme_leader_rank", 0) or 0),
        "theme_height_rank": float(candidate.get("theme_height_rank", 0) or 0),
        "theme_limit_count": float(candidate.get("theme_limit_count", 0) or 0),
        "segment_retreat_state_bucket": str(candidate.get("segment_retreat_state_bucket", "")),
        "segment_limit_down_count_bucket": str(candidate.get("segment_limit_down_count_bucket", "")),
        "market_chain_count_bucket": str(candidate.get("market_chain_count_bucket", "")),
        "first_time_detail_bucket": str(candidate.get("first_time_detail_bucket", "")),
        "limit_close": float(candidate.get("limit_close", 0) or 0),
        "fill_probability": float(candidate.get("fill_probability", 0) or 0),
        "planned_buy_date": next_trade_day(signal_date, 1),
        "planned_buy_price": "T+1_open_or_limit_order",
        "planned_exit_date": next_trade_day(signal_date, 2),
        "planned_exit_rule": "T+2_close",
        "position_pct": float(l_config.get("position_pct", 0.825)),
        "live_order_enabled": bool(l_config.get("live_order_enabled", False)),
        "status": "pending",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "research_audit": {
            "window": "recent_2y",
            "live_certification": "L2模拟实盘：执行138笔，复利142.35倍，最大回撤-30.35%",
            "source_report": "reports/strategy_l/live_certification/l_live_cert_summary.csv",
        },
    }


def save_outputs(signal_date: str, candidates: pd.DataFrame, signal: dict[str, Any] | None, dry_run: bool) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path = OUTPUT_DIR / f"l_signal_{signal_date}_candidates.csv"
    if not dry_run and not candidates.empty:
        keep_cols = [
            "trade_date",
            "ts_code",
            "name",
            "market_segment",
            "theme_name",
            "theme_heat_rank",
            "theme_leader_rank",
            "theme_height_rank",
            "theme_limit_count",
            "segment_retreat_state_bucket",
            "segment_limit_down_count_bucket",
            "market_chain_count_bucket",
            "first_time_detail_bucket",
            "limit_close",
            "fill_probability",
        ]
        candidates[[c for c in keep_cols if c in candidates.columns]].to_csv(candidate_path, index=False, encoding="utf-8-sig")
    if signal and not dry_run:
        migrate_existing_signals()
        save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg="L", max_trade_days=10)


def save_run_status(
    signal_date: str,
    status: str,
    reason: str,
    *,
    dry_run: bool,
    candidate_count: int | None = None,
    signal: dict[str, Any] | None = None,
) -> None:
    """记录L脚本当日终态；正常无候选不再伪装成信号文件缺失。"""

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
    save_recent_signal_run(
        RUN_STATUS_PATH,
        run,
        strategy_leg="L",
        max_trade_days=20,
    )


def migrate_existing_signals() -> None:
    """迁移并清理旧的每日L信号JSON；无新信号的交易日也会执行。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_daily_signal_files(
        OUTPUT_DIR,
        "l_signal_????????.json",
        ROLLING_SIGNAL_PATH,
        strategy_leg="L",
        max_trade_days=10,
    )
    cleanup_legacy_daily_signal_files(OUTPUT_DIR, "l_signal_????????.json")


def run_signal_generation(signal_date: str, *, dry_run: bool) -> None:
    """执行一次L信号生成，并把有信号或正常无候选都写成明确终态。"""

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    print(f"[L信号] 信号日期: {signal_date}")
    print("[L信号] 当前只生成信号文件，不提交实盘委托。")
    if not dry_run:
        migrate_existing_signals()

    candidates, reasons = load_l_candidates(signal_date)
    for reason in reasons:
        print(f"[L信号] {reason}")

    if candidates.empty:
        reason = "今日无符合L2的候选，L不触发"
        print(f"[L信号] {reason}。")
        save_outputs(signal_date, candidates, None, dry_run)
        save_run_status(
            signal_date,
            NO_CANDIDATE,
            reason,
            dry_run=dry_run,
            candidate_count=0,
        )
        return

    selected = candidates.iloc[0]
    signal = build_signal(signal_date, selected, config)
    save_outputs(signal_date, candidates, signal, dry_run)
    save_run_status(
        signal_date,
        SIGNAL_READY,
        f"L已生成唯一入选信号：{signal['ts_code']} {signal['name']}",
        dry_run=dry_run,
        candidate_count=len(candidates),
        signal=signal,
    )

    print("=" * 60)
    print("  策略 L 信号（独立龙头策略）")
    print("=" * 60)
    print(f"  股票: {signal['ts_code']} {signal['name']}  题材/行业={signal['theme_name']}")
    print(f"  排名: theme_heat_rank={signal['theme_heat_rank']} theme_leader_rank={signal['theme_leader_rank']} theme_height_rank={signal['theme_height_rank']}")
    print(f"  过滤: segment_retreat_state_bucket={signal['segment_retreat_state_bucket']} segment_limit_down_count_bucket={signal['segment_limit_down_count_bucket']} theme_limit_count={signal['theme_limit_count']}")
    print(f"  计划: {signal['planned_buy_date']} 买入，{signal['planned_exit_date']} 收盘平仓，目标仓位{signal['position_pct']:.1%}")
    print(f"  策略开关: strategy_l.enabled={bool(config.get('strategy_l', {}).get('enabled', False))}")
    print(f"  实盘开关: strategy_l.live_order_enabled={signal['live_order_enabled']}")
    print("=" * 60)
    print(f"[L信号] 已{'模拟' if dry_run else '保存'}信号：reports/strategy_l/l_signals_recent.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="策略L每日收盘后信号生成")
    parser.add_argument("--signal-date", default=None, help="信号日期 YYYYMMDD，不传则自动推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()
    signal_date = args.signal_date or resolve_signal_date()
    try:
        run_signal_generation(signal_date, dry_run=args.dry_run)
    except Exception as exc:
        # 任意未预期异常都留下ERROR，收盘审计据此告警，而不是误报成普通无候选。
        save_run_status(
            signal_date,
            ERROR,
            f"L信号脚本异常退出：{type(exc).__name__}: {exc}",
            dry_run=args.dry_run,
        )
        raise


if __name__ == "__main__":
    main()
