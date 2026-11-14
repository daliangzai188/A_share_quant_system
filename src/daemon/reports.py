"""播报（只读，不参与下单）：收盘流水线各步摘要、候选与开仓计划推送、决策链、09:35开仓成交核查、
因子与信号就绪检查。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import (
    ACE_FULL_TARGET_PCT,
    GATE_REDUCED as ACE_GATE_REDUCED,
    M_LEG,
    position_in_family,
)
from src.rolling_signal_store import (
    ERROR as SIGNAL_RUN_ERROR,
    NO_CANDIDATE,
    NO_SIGNAL_OCCUPIED,
    SIGNAL_READY,
    signal_run_by_signal_date,
)
from src.daemon.common import (
    PROJECT_ROOT,
    _floor_buy_quantity_by_amount,
    _notify,
    _ts_code_aliases,
    is_trade_day,
    logger,
    next_n_trade_days,
    to_float,
)
from src.daemon.state import (
    _d_relay_pair_active_today,
    _is_recent_auto_ghost_clear,
    _position_manual_exit_only,
    _pov_active_today,
    _pov_load_state,
    load_pending_buys,
    load_positions,
)
from src.daemon.plans import (
    _ace_gate_mode_for,
    _date_in_csv,
    _equity_curve_stop_block_reason,
    _has_signal_for_date,
    _latest_planned_orders_signal_date,
    _limit_up_pct_for_stock,
    _load_e_signal_for_signal_date,
    _prefer_live_processed_path,
    _round_stock_price,
    has_open_local_position,
    has_position_bought_today,
)
from src.daemon.qmt import (
    _broker_has_preexisting_strategy_position,
    _qmt_get,
    _qmt_lock,
    _qmt_query_account_positions,
    _strategy_only_market_value,
)
from src.daemon.buy import (
    _e_retry_resolved_today,
    _e_retry_running,
    _new_buy_execution_gate,
    resize_buy_orders_for_live_account,
)
from src.daemon.buy_m import M_SIGNAL_RECENT_PATH
from src.daemon.strategy_d import _strategy_d_monitor_running


# 普通开盘计划只允许在09:20竞价预挂、09:30确认以及09:30~09:35恢复宽限内
# 新建执行；E有独立的13:30延迟截止。该时间只决定原候选是否还能执行，
# 不再释放D：只要当日存在正式候选/开仓计划，D全日不启动。
OPEN_PLAN_DEFAULT_EXECUTION_CUTOFF = datetime.time(9, 35)
OPEN_PLAN_EXECUTION_CUTOFF_BY_LEG: dict[str, datetime.time] = {
    "E": datetime.time(13, 30),
}


def _csv_readiness(path: Path, signal_date: str, required_columns: list[str]) -> dict[str, Any]:
    """轻量检查某个因子文件是否包含信号日数据和必需字段。"""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "missing_columns": [],
        "ok": False,
        "data_available_false": None,
    }
    if not path.exists():
        result["missing_columns"] = required_columns
        return result
    try:
        import pandas as pd

        header = pd.read_csv(path, nrows=0).columns.tolist()
        missing = [c for c in required_columns if c not in header]
        result["missing_columns"] = missing
        if "trade_date" not in header:
            return result
        usecols = sorted(set(["trade_date", "data_available", *required_columns]) & set(header))
        data = pd.read_csv(path, usecols=usecols, dtype={"trade_date": str}, low_memory=False)
        day = data[data["trade_date"].astype(str).eq(signal_date)].copy()
        result["rows"] = int(len(day))
        if "data_available" in day.columns and len(day) > 0:
            available = day["data_available"].astype(str).str.lower().isin({"true", "1", "1.0"})
            result["data_available_false"] = int((~available).sum())
        result["ok"] = len(day) > 0 and not missing
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


def _json_signal_has_date(path: Path, signal_date: str) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return any(str(item.get("signal_date", "")) == signal_date for item in data.get("signals", []))
    except Exception:
        return False


def _strategy_signal_run_readiness(
    *,
    strategy_leg: str,
    run_status_path: Path,
    signal_path: Path,
    signal_date: str,
) -> dict[str, Any]:
    """区分“正常无信号”和“脚本未运行/失败”。

    正式信号JSON只保存真正入选的股票，不能用“当日没有信号行”判断脚本失败；
    因此优先读取独立运行状态账本。兼容升级前数据：当日已有正式信号但尚无状态
    记录时，仍视为旧版正常完成。
    """

    has_signal = _json_signal_has_date(signal_path, signal_date)
    run = signal_run_by_signal_date(run_status_path, signal_date)
    if run is None:
        if has_signal:
            return {
                "ok": True,
                "icon": "✅",
                "status": "SIGNAL_READY_LEGACY",
                "reason": "升级前正式信号已存在",
                "has_signal": True,
            }
        return {
            "ok": False,
            "icon": "⚠️",
            "status": "NOT_RUN",
            "reason": "未找到当日运行状态，无法确认脚本是否执行",
            "has_signal": False,
        }

    status = str(run.get("status", "")).strip().upper()
    reason = str(run.get("reason", "")).strip() or "未记录原因"
    if status == SIGNAL_RUN_ERROR:
        return {
            "ok": False,
            "icon": "⚠️",
            "status": status,
            "reason": reason,
            "has_signal": has_signal,
        }
    if status == SIGNAL_READY:
        if not has_signal:
            return {
                "ok": False,
                "icon": "⚠️",
                "status": SIGNAL_RUN_ERROR,
                "reason": "状态为SIGNAL_READY，但正式信号文件缺少当日记录",
                "has_signal": False,
            }
        return {
            "ok": True,
            "icon": "✅",
            "status": status,
            "reason": reason,
            "has_signal": True,
        }
    if status in {NO_SIGNAL_OCCUPIED, NO_CANDIDATE}:
        if has_signal:
            return {
                "ok": False,
                "icon": "⚠️",
                "status": SIGNAL_RUN_ERROR,
                "reason": f"状态为{status}，但正式信号文件仍含当日记录",
                "has_signal": True,
            }
        return {
            "ok": True,
            "icon": "ℹ️",
            "status": status,
            "reason": reason,
            "has_signal": False,
        }
    return {
        "ok": False,
        "icon": "⚠️",
        "status": SIGNAL_RUN_ERROR,
        "reason": f"{strategy_leg}运行状态非法：{status or '空值'}",
        "has_signal": has_signal,
    }


def report_next_trade_factor_readiness(signal_date: str) -> bool:
    """收盘后确认下个交易日开盘计算所需因子是否准备齐全。

    口径说明：
    - T日收盘能准备 T日涨停/日线/情绪/题材/资金/龙虎榜，用于 T+1 09:00 计划。
    - T+1集合竞价和开盘5分钟是真实未来盘中数据，不可能在T日晚提前获取；这里只检查审计文件。
    """
    try:
        signal_dt = datetime.datetime.strptime(signal_date, "%Y%m%d").date()
    except Exception:
        logger().warning("因子就绪检查跳过：signal_date非法=%s", signal_date)
        return False
    next_date = next_n_trade_days(signal_dt, 1).strftime("%Y%m%d")
    processed = PROJECT_ROOT / "data" / "processed"
    raw = PROJECT_ROOT / "data" / "raw"
    checks = [
        ("raw日线", raw / "daily" / f"{signal_date}.csv", ["trade_date", "ts_code", "open", "close", "amount"], True, False),
        ("raw每日基本面", raw / "daily_basic" / f"{signal_date}.csv", ["trade_date", "ts_code", "turnover_rate", "volume_ratio", "circ_mv"], True, False),
        ("raw涨停池", raw / "limit_list" / f"{signal_date}.csv", ["trade_date", "ts_code", "first_time", "last_time", "open_times", "limit_times"], True, False),
        ("实盘涨停合并", processed / "live_limit_up_merged.csv", ["trade_date", "ts_code", "fd_amount", "fd_amount_to_circ_mv", "market_segment"], True, False),
        ("成交概率打分", processed / "live_limit_up_fill_scored.csv", ["trade_date", "ts_code", "fill_probability", "allow_buy_reliable", "is_fill_score_reliable"], True, False),
        ("市场情绪", processed / "live_market_emotion_features.csv", ["trade_date", "market_segment", "market_chain_count", "segment_emotion_state"], True, False),
        ("题材热度", processed / "live_theme_heat_features.csv", ["trade_date", "ts_code", "theme_name", "theme_heat_rank", "theme_limit_count"], True, False),
        # 资金流/龙虎榜是纯记录性增强因子，A/C/E/D选股都不以它们为条件（龙虎榜策略已判死）。
        # 龙虎榜是交易所晚发布数据，tushare 常整日无数据/无权限（2026-07-23 卡住收盘流水线到18点+）；
        # 故 require_available=False：当日不可用只记录、不阻塞收盘完成与明日计划推送（2026-07-23 修）。
        ("资金流增强", processed / "sector_moneyflow_features.csv", ["trade_date", "ts_code", "sector_moneyflow_score"], False, False),
        ("龙虎榜增强", processed / "top_list_features.csv", ["trade_date", "ts_code", "top_list_net_buy_score"], False, False),
        # 集合竞价/开盘5分钟属于T+1盘中数据，T日晚只能生成审计占位，不能用未来数据伪造分数。
        ("集合竞价审计", processed / "auction_features.csv", ["trade_date", "ts_code", "auction_strength_score"], False, False),
        ("开盘5分钟审计", processed / "open_5m_features.csv", ["trade_date", "ts_code", "open_5m_strength_score"], False, False),
    ]

    critical_missing: list[str] = []
    retry_missing: list[str] = []
    enhanced_missing: list[str] = []
    logger().info("----- 明日开盘因子就绪检查：signal_date=%s next_trade_date=%s -----", signal_date, next_date)
    for name, path, cols, critical, require_available in checks:
        status = _csv_readiness(path, signal_date, cols)
        detail = f"{name}: rows={status['rows']} file={path.name}"
        if status.get("data_available_false") not in (None, 0):
            detail += f" data_available_false={status['data_available_false']}"
        all_unavailable = (
            require_available
            and int(status.get("rows") or 0) > 0
            and status.get("data_available_false") == status.get("rows")
        )
        if status["ok"]:
            if all_unavailable:
                logger().warning("  ⚠️ %s 当日数据全不可用，将等待接口补齐", detail)
                retry_missing.append(name)
            else:
                logger().info("  ✅ %s", detail)
        else:
            missing_text = ",".join(status.get("missing_columns") or [])
            err = status.get("error", "")
            logger().warning("  ⚠️ %s 缺失字段=%s 错误=%s", detail, missing_text or "无", err or "无")
            if critical:
                critical_missing.append(name)
            elif require_available:
                retry_missing.append(name)
            else:
                enhanced_missing.append(name)

    abc_ready = _has_signal_for_date(signal_dt)
    e_status = _strategy_signal_run_readiness(
        strategy_leg="E",
        run_status_path=PROJECT_ROOT / "reports" / "strategy_e" / "e_signal_runs_recent.json",
        signal_path=PROJECT_ROOT / "reports" / "strategy_e" / "e_signals_recent.json",
        signal_date=signal_date,
    )
    logger().info("  %s A/C计划或空计划文件已生成", "✅" if abc_ready else "⚠️")
    e_log_method = logger().info if e_status["ok"] else logger().warning
    e_log_method(
        "  %s E信号脚本：%s；%s",
        e_status["icon"],
        e_status["status"],
        e_status["reason"],
    )
    if not abc_ready:
        critical_missing.append("A/C planned_orders")
    if not e_status["ok"]:
        enhanced_missing.append(f"E signal run={e_status['status']}")
    if critical_missing:
        body = f"signal_date={signal_date} next={next_date} 缺关键因子/计划：{', '.join(critical_missing)}。明日09:00计划可能不可用。"
        logger().error("❌ 明日开盘关键因子未齐：%s", body)
        _notify("system_error", "❌ 明日开盘关键因子未齐", body, level="critical", call=True)
        logger().info("----- 明日开盘因子就绪检查结束 -----")
        return False
    if retry_missing:
        body = f"signal_date={signal_date} next={next_date} 待补齐增强因子：{', '.join(retry_missing)}。收盘流水线将5分钟后重试。"
        logger().warning("⚠️ 明日开盘增强因子未齐：%s", body)
        logger().info("----- 明日开盘因子就绪检查结束 -----")
        return False
    elif enhanced_missing:
        logger().warning("⚠️ 明日开盘关键因子已齐，增强/备用项缺失：%s", ", ".join(enhanced_missing))
    else:
        logger().info("✅ 明日开盘因子已齐：A/C/E/D所需关键文件均已准备")
    logger().info("----- 明日开盘因子就绪检查结束 -----")
    return True


def _processed_data_ready_for_date(target_date: datetime.date) -> bool:
    """启动自检：实盘审计依赖的 processed 表必须包含目标信号日。"""
    required_paths = [
        _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv"),
        _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv"),
        _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv"),
    ]
    return all(_date_in_csv(path, target_date, "trade_date") for path in required_paths)


def _log_collection_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        daily_path = PROJECT_ROOT / "data" / "raw" / "daily" / f"{signal_date}.csv"
        basic_path = PROJECT_ROOT / "data" / "raw" / "daily_basic" / f"{signal_date}.csv"
        limit_path = PROJECT_ROOT / "data" / "raw" / "limit_list" / f"{signal_date}.csv"
        daily_rows = len(pd.read_csv(daily_path, low_memory=False)) if daily_path.exists() else 0
        basic_rows = len(pd.read_csv(basic_path, low_memory=False)) if basic_path.exists() else 0
        if not limit_path.exists():
            logger().warning("涨停数据状态：❌ %s 涨停池文件不存在：%s", signal_date, limit_path)
            return
        limit_df = pd.read_csv(limit_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        open_times = pd.to_numeric(limit_df.get("open_times"), errors="coerce").fillna(0)
        one_word = int((open_times == 0).sum()) if "open_times" in limit_df.columns else 0
        opened = int((open_times > 0).sum()) if "open_times" in limit_df.columns else 0
        logger().info(
            "采集结果：✅ %s raw日线=%s行，每日基本面=%s行，涨停池=%s行",
            signal_date,
            daily_rows,
            basic_rows,
            len(limit_df),
        )
        logger().info(
            "涨停数据状态：✅ %s 涨停池已获取，数量=%d，一字板=%d，开板/炸板=%d，source={%s} quality={%s} compatible={%s}",
            signal_date,
            len(limit_df),
            one_word,
            opened,
            _value_counts_text(limit_df, "limit_data_source"),
            _value_counts_text(limit_df, "limit_data_quality"),
            _value_counts_text(limit_df, "strategy_compatible"),
        )
    except Exception as exc:
        logger().warning("采集结果摘要失败：%s", exc)


def _log_cleaning_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        daily_path = PROJECT_ROOT / "data" / "processed" / "daily_merged_by_date" / f"{signal_date}.csv"
        limit_path = _prefer_live_processed_path("live_limit_up_merged.csv", "limit_up_merged.csv")
        daily_rows = len(pd.read_csv(daily_path, low_memory=False)) if daily_path.exists() else 0
        limit_df = pd.read_csv(limit_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False) if limit_path.exists() else pd.DataFrame()
        if not limit_df.empty and "trade_date" in limit_df.columns:
            limit_df = limit_df[limit_df["trade_date"].astype(str).eq(str(signal_date))].copy()
        logger().info(
            "清洗结果：%s 日线合并 rows=%s；涨停合并 rows=%s source={%s} quality={%s}",
            signal_date,
            daily_rows,
            len(limit_df),
            _value_counts_text(limit_df, "limit_data_source"),
            _value_counts_text(limit_df, "limit_data_quality"),
        )
    except Exception as exc:
        logger().warning("清洗结果摘要失败：%s", exc)


def _log_dynamic_feature_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        sentiment_path = _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv")
        emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        theme_path = _prefer_live_processed_path("live_theme_heat_features.csv", "theme_heat_features.csv")
        sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False) if sentiment_path.exists() else pd.DataFrame()
        emotion = pd.read_csv(emotion_path, dtype={"trade_date": str}, low_memory=False) if emotion_path.exists() else pd.DataFrame()
        theme = pd.read_csv(theme_path, dtype={"trade_date": str}, low_memory=False) if theme_path.exists() else pd.DataFrame()
        srow = sentiment[sentiment["trade_date"].astype(str).eq(str(signal_date))].iloc[0] if not sentiment.empty and "trade_date" in sentiment.columns and not sentiment[sentiment["trade_date"].astype(str).eq(str(signal_date))].empty else pd.Series(dtype=object)
        erow = emotion[emotion["trade_date"].astype(str).eq(str(signal_date))].iloc[0] if not emotion.empty and "trade_date" in emotion.columns and not emotion[emotion["trade_date"].astype(str).eq(str(signal_date))].empty else pd.Series(dtype=object)
        theme_daily = theme[theme["trade_date"].astype(str).eq(str(signal_date))].copy() if not theme.empty and "trade_date" in theme.columns else pd.DataFrame()
        lead_count = int(pd.to_numeric(theme_daily.get("theme_limit_count"), errors="coerce").fillna(0).ge(2).sum()) if not theme_daily.empty else 0
        top_theme = str(theme_daily.iloc[0].get("theme_name", theme_daily.iloc[0].get("theme", ""))) if not theme_daily.empty else "NA"
        logger().info(
            "动态特征：✅ 市场情绪已计算，全市场涨停=%s，跌停=%s，连板数=%s，最高板=%s",
            srow.get("limit_up_count", "NA"),
            erow.get("market_limit_down_count", "NA"),
            erow.get("market_chain_count", srow.get("limit_up_max_height", "NA")),
            srow.get("limit_up_max_height", "NA"),
        )
        logger().info("动态特征：✅ 题材热度已计算，rows=%d，主线样本=%d，首位题材=%s", len(theme_daily), lead_count, top_theme)
    except Exception as exc:
        logger().warning("动态特征摘要失败：%s", exc)


def _log_fill_score_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        path = _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv")
        data = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False) if path.exists() else pd.DataFrame()
        daily = data[data["trade_date"].astype(str).eq(str(signal_date))].copy() if not data.empty and "trade_date" in data.columns else pd.DataFrame()
        prob_source = daily["fill_probability"] if "fill_probability" in daily.columns else pd.Series(dtype=float)
        prob = pd.to_numeric(prob_source, errors="coerce")
        if prob.empty or prob.isna().all():
            fallback_source = daily["estimated_fill_probability"] if "estimated_fill_probability" in daily.columns else pd.Series(dtype=float)
            prob = pd.to_numeric(fallback_source, errors="coerce")
        avg_text = "NA" if prob.isna().all() else f"{prob.mean() * 100:.1f}%"
        logger().info(
            "成交概率：✅ %s 打分完成，rows=%d，平均成交概率=%s，allow_buy_reliable={%s}，score_reliable={%s}",
            signal_date,
            len(daily),
            avg_text,
            _value_counts_text(daily, "allow_buy_reliable"),
            _value_counts_text(daily, "is_fill_score_reliable"),
        )
    except Exception as exc:
        logger().warning("成交概率摘要失败：%s", exc)


def _log_enhanced_feature_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        items = [
            ("资金流", PROJECT_ROOT / "data" / "processed" / "sector_moneyflow_features.csv"),
            ("龙虎榜", PROJECT_ROOT / "data" / "processed" / "top_list_features.csv"),
            ("集合竞价", PROJECT_ROOT / "data" / "processed" / "auction_features.csv"),
            ("开盘5分钟", PROJECT_ROOT / "data" / "processed" / "open_5m_features.csv"),
        ]
        parts = []
        for label, path in items:
            data = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False) if path.exists() else pd.DataFrame()
            daily = data[data["trade_date"].astype(str).eq(str(signal_date))].copy() if not data.empty and "trade_date" in data.columns else pd.DataFrame()
            available = len(daily)
            if "data_available" in daily.columns:
                available = int(daily["data_available"].fillna(False).astype(bool).sum())
            parts.append(f"{label}=rows{len(daily)}/available{available}")
        logger().info("增强因子：✅ %s %s", signal_date, "；".join(parts))
    except Exception as exc:
        logger().warning("增强因子摘要失败：%s", exc)


def _log_abc_strategy_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        checklist = _load_ab_checklist(signal_date)
        if checklist.empty:
            logger().warning("A/C策略状态：⚠️ 未找到 %s checklist，A/C 本步可能未完成", signal_date)
            return
        row = checklist.iloc[0]
        planned_count = int(float(row.get("planned_order_count", 0) or 0))
        if planned_count > 0:
            logger().info(
                "A/C策略状态：✅ 符合开仓条件，计划单=%d，选中=%s %s，operation=%s，selection=%s",
                planned_count,
                row.get("top_ts_code", ""),
                row.get("top_name", ""),
                row.get("operation_status", ""),
                row.get("selection_status", ""),
            )
        else:
            logger().info(
                "A/C策略状态：ℹ️ 今日暂无可执行计划单；原因：selection=%s（%s） operation=%s（%s）",
                row.get("selection_status", ""),
                row.get("selection_status_desc", ""),
                row.get("operation_status", ""),
                row.get("operation_status_desc", ""),
            )
            logger().info(
                "A/C候选分布：A=%s，C=%s，C过滤=%s，最终选中=%s；B已删除",
                row.get("a_candidate_count", 0),
                row.get("c_candidate_count", 0),
                row.get("c_rejected_by_filter_count", 0),
                row.get("selected_count", 0),
            )
    except Exception as exc:
        logger().warning("A/C策略摘要失败：%s", exc)


def _read_close_candidate_artifact(strategy: str, signal_date: str) -> dict[str, Any]:
    """只读当天已有候选产物，不触发重算，也不参与组合排序或下单。"""

    import pandas as pd

    leg = str(strategy).strip().upper()
    if leg in {"A", "C"}:
        paths = sorted(
            (
                PROJECT_ROOT
                / "reports"
                / "paper_trade"
                / "ab_filtered_daily_ops"
            ).glob(f"*_{signal_date}_{leg.lower()}_candidates.csv")
        )
        path = paths[-1] if paths else None
    elif leg == "E":
        path = (
            PROJECT_ROOT
            / "reports"
            / "strategy_e"
            / f"e_signal_{signal_date}_candidates.csv"
        )
    elif leg == "D":
        path = (
            PROJECT_ROOT
            / "reports"
            / "strategy_d"
            / f"intraday_signals_{signal_date}.csv"
        )
    else:
        raise ValueError(f"不支持的策略腿：{strategy}")

    result: dict[str, Any] = {
        "strategy": leg,
        "artifact_exists": bool(path and path.exists()),
        "calculated": False,
        "candidate_count": None,
        "first_candidate": None,
        "source": str(path) if path else "",
        "detail": "",
    }
    if path is None or not path.exists():
        result["detail"] = (
            "未发现当日盘中信号文件，不能据此断言D无候选"
            if leg == "D"
            else "未发现当日候选产物，候选状态未知"
        )
        return result

    try:
        frame = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    except Exception as exc:
        result["detail"] = f"候选产物读取失败：{type(exc).__name__}: {exc}"
        return result

    result["calculated"] = True
    if leg == "D":
        if "signal_type" not in frame.columns:
            candidates = pd.DataFrame()
        else:
            candidates = frame[
                frame["signal_type"].astype(str).str.upper().eq("BUY")
            ].copy()
        watch_count = (
            int(frame["signal_type"].astype(str).str.upper().eq("WATCH").sum())
            if "signal_type" in frame.columns
            else 0
        )
        result["detail"] = f"盘中BUY记录={len(candidates)}，WATCH记录={watch_count}"
    else:
        candidates = frame.copy()

    if not candidates.empty and "candidate_rank" in candidates.columns:
        candidates = candidates.assign(
            _candidate_rank=pd.to_numeric(
                candidates["candidate_rank"], errors="coerce"
            ).fillna(10**9)
        ).sort_values("_candidate_rank", kind="stable")

    result["candidate_count"] = int(len(candidates))
    if not candidates.empty:
        row = candidates.iloc[0]
        code = str(row.get("ts_code", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        result["first_candidate"] = " ".join(
            part for part in (code, name) if part
        ) or "候选信息缺失"
    return result


_CLOSE_CANDIDATE_PUSHED_FILE = "close_candidate_summary_pushed.json"


def _push_close_candidate_summary_once(signal_date: str, body: str) -> None:
    """每个信号日把候选统计推送一次（用户 2026-10-30 要求）。

    已推送的信号日记在 data/state/ 下；推送成功才记下，失败的下次收盘流水线
    再推。QMT 重启后补跑同一信号日不会重复推送。只推送，不影响任何交易。
    """
    path = PROJECT_ROOT / "data" / "state" / _CLOSE_CANDIDATE_PUSHED_FILE
    try:
        pushed = list(json.loads(path.read_text(encoding="utf-8")).get("signal_dates", []))
    except (OSError, ValueError, AttributeError):
        pushed = []
    if signal_date in pushed:
        return
    if not _notify("daily_summary", f"📋 收盘候选统计 {signal_date}", body):
        return
    pushed = (pushed + [signal_date])[-60:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"signal_dates": pushed}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger().warning("候选统计推送记录写入失败（下次可能重复推送）：%s", exc)


def _m_close_summary(signal_date: str) -> str:
    """M当日信号产物摘要（只读）。"""
    try:
        from src.rolling_signal_store import signal_by_signal_date, signal_run_by_signal_date

        run = signal_run_by_signal_date(PROJECT_ROOT / "reports" / "strategy_m" / "m_signal_runs_recent.json", signal_date)
        if not run:
            return "未完成统计｜无运行记录"
        if str(run.get("status")) == "ERROR":
            return f"计算失败（次日M不开仓）｜{run.get('error', '')}"
        signal = signal_by_signal_date(M_SIGNAL_RECENT_PATH, signal_date) or {}
        diag = signal.get("diagnostics") or {}
        mood = diag.get("mood")
        mood_text = ("%.2f%%" % (float(mood) * 100)) if mood is not None and mood == mood else "无"
        cands = signal.get("candidates") or []
        head = f"已计算｜昨日涨停今日平均{mood_text}｜20cm涨停{int(diag.get('n20_limit_up') or 0)}只｜候选{len(cands)}只"
        if cands:
            first = sorted(cands, key=lambda c: int(c.get("rank", 999)))[0]
            head += f"｜第一名 {first.get('name') or ''}({first.get('ts_code')})"
        return head
    except Exception as exc:
        return f"统计失败：{exc}"


def _log_close_pipeline_candidate_summary(signal_date: str) -> None:
    """汇总ACDE已有产物；本函数严格只读，不改变任何策略执行结果。"""

    try:
        summaries = {
            leg: _read_close_candidate_artifact(leg, signal_date)
            for leg in ("A", "C", "E", "D")
        }
        labels = {
            "A": "① A主策略",
            "C": "② C强势龙头",
            "E": "③ E策略",
            "D": "④ D盘中兜底",
        }
        lines = [
            "┃━━━━━━━━━━ 收盘流水线候选产物统计 ━━━━━━━━━━",
            f"┃ 信号日：{signal_date}",
            "┃ 口径：只读取当日产物；不重算、不参与下单；执行层按A>C>E>D真实开仓日互斥",
        ]
        for leg in ("A", "C", "E", "D"):
            item = summaries[leg]
            count = item["candidate_count"]
            if not item["calculated"]:
                content = f"未完成统计｜{item['detail']}"
            elif count == 0:
                content = "已计算｜候选0只"
                if item["detail"]:
                    content += f"｜{item['detail']}"
            else:
                content = (
                    f"已计算｜候选{count}只｜第一名 {item['first_candidate']}"
                )
                if item["detail"]:
                    content += f"｜{item['detail']}"
            lines.append(f"┃ {labels[leg]}：{content}")
        lines.append(f"┃ ⑤ M 20cm强势接力（与ACE资金分开）：{_m_close_summary(signal_date)}")
        lines.append("┃━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger().info("\n".join(lines))
        _push_close_candidate_summary_once(
            signal_date,
            "\n".join(line[2:] for line in lines[3:-1])
            + "\n只读统计，不参与下单；ACE按A>C>E>D互斥，M与ACE并行（资金分开）。",
        )
    except Exception as exc:
        logger().warning("收盘流水线候选产物统计失败（不影响信号和下单）：%s", exc)


def _segment_label(ts_code: str, market_segment: str = "") -> str:
    seg_map = {
        "sh_main": "沪市主板",
        "sz_main": "深市主板",
        "chi_next": "创业板",
        "star": "科创板",
        "bj": "北交所",
    }
    if market_segment in seg_map:
        return seg_map[market_segment]
    code = ts_code.split(".")[0] if "." in ts_code else ts_code
    suffix = ts_code.split(".")[-1].upper() if "." in ts_code else ""
    if suffix == "BJ":
        return "北交所"
    if suffix == "SH":
        return "科创板" if code.startswith("688") else "沪市主板"
    if suffix == "SZ":
        return "创业板" if code.startswith("3") else "深市主板"
    return "未知"


def _load_limit_for_codes(ts_codes: list[str]) -> tuple[str, dict[str, dict]]:
    """从实盘涨停表读最新交易日的涨停状态和封单金额。
    返回 (最新交易日, {ts_code: {limit, open_times, fd_amount_wan, last_time}})。"""
    try:
        import pandas as pd
        path = _prefer_live_processed_path("live_limit_up_merged.csv", "limit_up_merged.csv")
        if not path.exists():
            return "", {}
        need = ["ts_code", "trade_date", "limit", "open_times", "fd_amount", "last_time"]
        avail = list(pd.read_csv(path, nrows=0).columns)
        use_cols = [c for c in need if c in avail]
        if "ts_code" not in use_cols or "trade_date" not in use_cols:
            return "", {}
        df = pd.read_csv(path, usecols=use_cols, dtype={"trade_date": str}, low_memory=False)
        latest = str(df["trade_date"].max())
        sub = df[(df["trade_date"] == latest) & (df["ts_code"].isin(ts_codes))]
        result: dict[str, dict] = {}
        for _, r in sub.iterrows():
            t = r.get("last_time", 0)
            try:
                t_int = int(float(t))
                time_str = f"{t_int // 10000:02d}:{(t_int % 10000) // 100:02d}"
            except Exception:
                time_str = str(t)
            result[str(r["ts_code"])] = {
                "limit":          str(r.get("limit", "")),
                "open_times":     int(r.get("open_times", 0) or 0),
                "fd_amount_wan":  float(r.get("fd_amount", 0) or 0) / 10000,  # 元→万元
                "last_time":      time_str,
            }
        return latest, result
    except Exception as e:
        logger().debug("读取涨停数据失败：%s", e)
        return "", {}


def _load_daily_for_codes(ts_codes: list[str]) -> tuple[str, dict[str, dict]]:
    """从日线分片读最新交易日的 close/pct_chg/circ_mv。

    实盘播报只需要最新信号日当天候选股行情，不应读取 250 万行级别
    daily_merged.csv。优先使用 data/processed/daily_merged_by_date/YYYYMMDD.csv；
    旧 daily_merged.csv 仅作为离线研究遗留文件，不再进入实盘启动路径。
    返回 (最新交易日字符串, {ts_code: {...}})。"""
    try:
        import pandas as pd
        part_dir = PROJECT_ROOT / "data" / "processed" / "daily_merged_by_date"
        part_files = sorted(part_dir.glob("*.csv"))
        if not part_files:
            return "", {}
        path = part_files[-1]
        need = ["ts_code", "trade_date", "close", "pct_chg", "circ_mv"]
        avail = list(pd.read_csv(path, nrows=0).columns)
        use_cols = [c for c in need if c in avail]
        if "ts_code" not in use_cols or "trade_date" not in use_cols:
            return "", {}
        daily = pd.read_csv(path, usecols=use_cols, dtype={"trade_date": str}, low_memory=False)
        latest = str(daily["trade_date"].max())
        sub = daily[(daily["trade_date"] == latest) & (daily["ts_code"].isin(ts_codes))]
        result: dict[str, dict] = {}
        for _, r in sub.iterrows():
            result[str(r["ts_code"])] = {
                "close":   float(r.get("close",   0) or 0),
                "pct_chg": float(r.get("pct_chg", 0) or 0),
                "circ_mv": float(r.get("circ_mv", 0) or 0),  # 万元
            }
        return latest, result
    except Exception as e:
        logger().debug("读取日线数据失败：%s", e)
        return "", {}


def report_next_day_candidates() -> None:
    """读取最新 planned_orders，播报下一交易日开仓候选，附当日行情。"""
    try:
        import pandas as pd
        import re as _re

        now_bj = now_beijing()
        today = today_beijing()
        today_str = today.strftime("%Y%m%d")

        # 收盘后（>=15:10 且是交易日）才要求信号必须是今天的；收盘前用最新缓存即可
        require_today = (
            is_trade_day(now_bj.date())
            and now_bj.time() >= datetime.time(15, 10)
        )

        # 交易日且未收盘：信号来自昨天，操作日=今天 → "今日候选"
        # 已收盘或非交易日：操作日=下一交易日 → "明日候选"
        if is_trade_day(now_bj.date()) and not require_today:
            header_label = "今日候选"
            action_date_str = today.strftime("%Y-%m-%d")
            no_candidate_msg = "今日暂不开仓"
        else:
            header_label = "明日候选"
            action_date_str = next_n_trade_days(today, 1).strftime("%Y-%m-%d")
            no_candidate_msg = "明日暂不开仓"

        pattern = str(PROJECT_ROOT / "reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")
        files = sorted(glob.glob(pattern))

        logger().info("=" * 60)

        if not files:
            logger().warning("【%s】%s  ⚠️  未找到 A/C planned_orders 文件", header_label, action_date_str)
            if require_today:
                signal_date_str = today_str
                logger().info("  A/C：今日未生成计划单，继续检查 E 是否已有 %s 信号", signal_date_str)
                _log_e_signal_status(signal_date_str)
                _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), pd.DataFrame())
            logger().info("=" * 60)
            return

        today_files = [Path(file) for file in files if today_str in Path(file).stem]
        latest_file = today_files[-1] if (require_today and today_files) else (Path(files[-1]) if not require_today else None)
        signal_date_str = today_str if require_today and latest_file is None else "未知"
        if latest_file is not None:
            _m = _re.search(r"\d{8}", latest_file.stem)
            signal_date_str = _m.group() if _m else "未知"
        # 收盘后必须是今天的信号；如果 A/C 今天无文件，也按今天检查 E，不能回退到旧 planned_orders。
        data_fresh = (signal_date_str == today_str) or (not require_today)

        try:
            orders = pd.read_csv(latest_file) if latest_file is not None else pd.DataFrame()
        except pd.errors.EmptyDataError:
            logger().info("【%s】%s  信号日期：%s", header_label, action_date_str, signal_date_str)
            if data_fresh:
                logger().info("  A/C 均无符合条件标的，%s", no_candidate_msg)
            else:
                logger().warning("  ⚠️  数据未更新！信号来自 %s，今日（%s）收盘流水线未成功运行", signal_date_str, today_str)
            _log_e_signal_status(signal_date_str)
            _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), None)
            logger().info("=" * 60)
            return
        except Exception as e:
            file_name = latest_file.name if latest_file is not None else "无今日A/C planned_orders"
            logger().error("  读取 planned_orders 失败（%s）：%s", file_name, e)
            logger().info("=" * 60)
            return

        if not orders.empty and "signal_date" in orders.columns and not orders["signal_date"].dropna().empty:
            signal_date_str = str(orders["signal_date"].dropna().iloc[0])

        buy_orders = (
            orders[orders["side"].astype(str).str.upper() == "BUY"].copy()
            if not orders.empty and "side" in orders.columns else pd.DataFrame()
        )

        logger().info("【%s】%s  信号日期：%s", header_label, action_date_str, signal_date_str)
        if not data_fresh:
            logger().warning("  ⚠️  数据未更新！信号来自 %s，今日（%s）收盘流水线未成功运行，以下仅供参考", signal_date_str, today_str)

        if buy_orders.empty:
            checklist = _load_ab_checklist(signal_date_str)
            if not checklist.empty:
                row = checklist.iloc[0]
                logger().info("  A/C 未生成计划单，%s", no_candidate_msg)
                logger().info(
                    "  状态：%s（%s）",
                    row.get("operation_status", ""),
                    row.get("operation_status_desc", "无中文解释，请重跑 run_paper_ab_filtered_daily_ops.py"),
                )
                logger().info(
                    "  筛选：%s（%s）",
                    row.get("selection_status", ""),
                    row.get("selection_status_desc", "无中文解释，请查看 checklist"),
                )
                logger().info(
                    "  漏斗：A候选%s只，C候选%s只，C过滤%s只，最终选中%s只，计划单%s条",
                    row.get("a_candidate_count", 0),
                    row.get("c_candidate_count", 0),
                    row.get("c_rejected_by_filter_count", 0),
                    row.get("selected_count", 0),
                    row.get("planned_order_count", 0),
                )
                if str(row.get("next_action", "")):
                    logger().info("  下一步：%s", row.get("next_action", ""))
            else:
                logger().info("  A/C 均无符合条件标的，%s", no_candidate_msg)
            _log_e_signal_status(signal_date_str)
        else:
            ts_codes = buy_orders["ts_code"].astype(str).tolist()
            daily_date, daily_map = _load_daily_for_codes(ts_codes)
            _, limit_map = _load_limit_for_codes(ts_codes)
            if daily_date:
                logger().info("  行情基准日：%s", daily_date)
            logger().info("  共 %d 只候选：", len(buy_orders))
            for i, (_, r) in enumerate(buy_orders.iterrows(), 1):
                code     = str(r.get("ts_code", ""))
                name     = str(r.get("name", ""))
                leg      = str(r.get("strategy_leg", ""))
                ref_px   = float(r.get("reference_price", 0.0))
                shares   = int(r.get("round_lot_shares", r.get("estimated_shares", 0)))
                amount   = ref_px * shares
                seg      = _segment_label(code, str(r.get("market_segment", "")))
                d        = daily_map.get(code, {})
                close    = d.get("close", 0.0)
                pct      = d.get("pct_chg", 0.0)
                circ_yi  = d.get("circ_mv", 0.0) / 10000  # 万元→亿元
                pct_sign = "+" if pct >= 0 else ""
                lm       = limit_map.get(code, {})
                is_limit = lm.get("limit", "") == "U"
                open_t   = lm.get("open_times", 0)
                fd_wan   = lm.get("fd_amount_wan", 0.0)
                last_t   = lm.get("last_time", "")

                logger().info("  %d. [%s] %s %s", i, seg, code, name)
                if close > 0:
                    logger().info(
                        "     行情：收盘 %.2f元  涨跌 %s%.2f%%  流通市值 %.1f亿",
                        close, pct_sign, pct, circ_yi,
                    )
                else:
                    logger().info("     行情：暂无（日线数据未采集到该日）")
                if is_limit:
                    open_desc = "一字板" if open_t == 0 else f"炸板{open_t}次"
                    logger().info(
                        "     涨停：✅ 封板中  %s  封单 %.0f万元  最后封板 %s",
                        open_desc, fd_wan, last_t,
                    )
                elif lm:
                    logger().info("     涨停：❌ 当日涨停已炸板（炸板 %d 次），收盘未封住", open_t)
                else:
                    logger().info("     涨停：— 当日未涨停")
                logger().info(
                    "     计划：策略 %s  参考价 %.2f元  %d股  预估 %.0f元",
                    leg, ref_px, shares, amount,
                )
                if leg.upper() == "C":
                    logger().info(
                        "     C分支：%s  profile=%s",
                        str(r.get("matched_strategy_branch_ids", "")),
                        str(r.get("matched_condition_profile_ids", "")),
                    )
                    logger().info(
                        "     平仓：%s（信号日T+%s；买入后%s个交易日） 计划平仓日=%s",
                        str(r.get("exit_rule", "")),
                        str(r.get("exit_signal_offset", "")),
                        str(r.get("exit_n_days", "")),
                        str(r.get("planned_exit_date", "")),
                    )
            _log_e_signal_status(signal_date_str)

        _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), buy_orders)
        logger().info("=" * 60)
    except Exception as e:
        logger().error("播报候选异常：%s", e)


def _load_e_candidate_count(signal_date: str) -> int | None:
    """读取E候选池规模；最终首位由换手率降序及稳定并列键确定。"""
    try:
        import pandas as pd

        # 同一天可能重跑，运行状态账本记录最后一次结果，优先级高于候选CSV。
        run = signal_run_by_signal_date(
            PROJECT_ROOT / "reports" / "strategy_e" / "e_signal_runs_recent.json",
            signal_date,
        )
        if run is not None:
            # 当天已有运行状态但candidate_count缺失，表示候选检查失败或未完成。
            # 此时禁止回退读取可能来自更早一次运行的CSV，否则会把陈旧候选写进日志。
            if run.get("candidate_count") is None:
                return None
            return int(run["candidate_count"])
        path = PROJECT_ROOT / "reports" / "strategy_e" / f"e_signal_{signal_date}_candidates.csv"
        if not path.exists():
            return None
        return int(len(pd.read_csv(path, low_memory=False)))
    except Exception as exc:
        logger().debug("读取E候选数失败：%s", exc)
        return None


def _log_e_signal_status(signal_date: str) -> None:
    """播报 E 候选/选中标的/allow_buy_reliable/计划买卖日。

    E 信号存于 reports/strategy_e/e_signals_recent.json，与 A/C 的
    planned_orders.csv 不同。启动播报若不单独
    读取，会出现"有 E 信号却完全看不到"的盲区，故在此独立播报。
    """
    try:
        candidate_count = _load_e_candidate_count(signal_date)
        count_text = "未知" if candidate_count is None else str(candidate_count)
        signal = _load_e_signal_for_signal_date(signal_date)
        if signal is None:
            run = signal_run_by_signal_date(
                PROJECT_ROOT / "reports" / "strategy_e" / "e_signal_runs_recent.json",
                signal_date,
            )
            run_status = _strategy_signal_run_readiness(
                strategy_leg="E",
                run_status_path=PROJECT_ROOT / "reports" / "strategy_e" / "e_signal_runs_recent.json",
                signal_path=PROJECT_ROOT / "reports" / "strategy_e" / "e_signals_recent.json",
                signal_date=signal_date,
            )
            if run is not None and run_status["status"] == NO_SIGNAL_OCCUPIED:
                blocker = str(
                    run.get("priority_blocker")
                    or str(run_status["reason"]).split("；", 1)[0]
                    or "上游优先级或持仓阻断"
                )
                check_status = str(run.get("candidate_check_status", "")).upper()
                # 兼容升级前已完成只读检查的记录：有candidate_count即视为已计算。
                if not check_status and run.get("candidate_count") is not None:
                    check_status = "CALCULATED"
                check_reason = str(run.get("candidate_check_reason", "") or "")
                if check_status == "CALCULATED" and candidate_count == 0:
                    candidate_result = (
                        "已独立计算，结果=无候选（0只）；"
                        "结论=即使没有上游策略占用，E也不会触发"
                    )
                elif check_status == "CALCULATED" and candidate_count is not None:
                    code = str(run.get("candidate_ts_code", "") or "").strip()
                    name = str(run.get("candidate_name", "") or "").strip()
                    first = f"，第一名={code} {name}" if code else ""
                    candidate_result = (
                        f"已独立计算，结果=有候选（{candidate_count}只{first}）；"
                        "结论=本日仅因上游优先级/持仓阻断而未生成E正式信号"
                    )
                elif check_status == "FAILED":
                    candidate_result = (
                        "计算失败，结果=未知；不能判断账户空闲时E是否会触发"
                        + (f"；错误={check_reason}" if check_reason else "")
                    )
                else:
                    candidate_result = "未计算，结果=未知"
                logger().info(
                    "  E策略：信号日期 %s，正式信号=未生成；优先级结论=%s；E候选检查=%s",
                    signal_date,
                    blocker,
                    candidate_result,
                )
                return
            logger().info(
                "  E策略：信号日期 %s 未生成E正式信号，运行状态=%s，候选池=%s，原因=%s",
                signal_date,
                run_status["status"],
                count_text,
                run_status["reason"],
            )
            return
        logger().info(
            "  E策略：信号日期 %s 候选池=%s，选中 %s %s，板块=%s，"
            "allow_buy_reliable=%s，仓位=%s，计划买入=%s(%s)，计划卖出=%s(%s)，状态=%s",
            signal_date,
            count_text,
            signal.get("ts_code", ""),
            signal.get("name", ""),
            signal.get("market_segment", ""),
            bool(signal.get("allow_buy_reliable", False)),
            signal.get("position_pct", ""),
            signal.get("planned_buy_date", ""),
            signal.get("planned_buy_price", ""),
            signal.get("planned_exit_date", ""),
            signal.get("planned_exit_rule", ""),
            signal.get("status", ""),
        )
    except Exception as exc:
        logger().error("播报E状态异常：%s", exc)


def _planned_equity_base() -> float:
    """播报用的名义资金基数（回测初始资金模型），不参与任何实盘定仓。

    config.position.initial_cash 缺省时为50万——真实账户小于它时，播报数字会明显
    高于实际可开仓量，故播报文案必须标注为名义上限（2026-07-31）。
    """
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        return float(cfg.get("position", {}).get("initial_cash", 500_000.0) or 500_000.0)
    except Exception:
        return 500_000.0


def _planned_shares_by_equity(position_pct: Any, price: float) -> int:
    """按初始资金模型估算计划买入股数（floor 到 100 股）。

    实盘 09:20/09:30 下单前会由 resize_buy_orders_for_live_account 按真实可用资金
    再缩放、并在挂单时按涨停价核一次冻结资金，这里只用于收盘/启动播报展示一个
    "计划数量"名义参考值。
    """
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        initial_cash = _planned_equity_base()
        amount = initial_cash * float(position_pct or 0.0)
        if str(cfg.get("trade_mode", "")).lower() == "live":
            cap = float(cfg.get("live_trade", {}).get("max_single_order_amount", amount) or amount)
            amount = min(amount, cap)
        if price and price > 0:
            shares = int(amount // price)
            shares -= shares % 100
            return max(shares, 0)
    except Exception as exc:
        logger().debug("估算计划股数失败：%s", exc)
    return 0


_LIVE_SIZING_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_LIVE_SIZING_TTL_SEC = 120.0
# 整个"查缓存→查询→写缓存"必须串行(2026-07-31)：【最终结果】与【最终开仓计划】
# 由不同线程几乎同时播报，只加缓存不加锁两边都会判定未命中，各查一次QMT账户。
_LIVE_SIZING_LOCK = threading.Lock()


def _live_plan_sizing(
    final_buy: dict[str, Any], action_date: str, signal_date: str
) -> dict[str, Any] | None:
    """用真实账户预演下一交易日09:20定仓，供播报/推送显示真实股数(2026-07-31)。

    口径必须与实盘同源，因此直接复用 resize_buy_orders_for_live_account 本身
    （现金/单票/总仓位/单笔/退出容量五道上限），再按明日委托价=涨停价核一次
    冻结资金——和 job_premarket_buy 完全一致。若券商仍有旧策略仓则返回0股，
    不再按“明日到期”提前豁免；quote_map 传空时 reference_price 作为折算价。
    只读不下单；QMT不可用或任何异常都返回 None，由调用方回退名义值播报。

    同一次收盘流水线里【最终结果】和【最终开仓计划】两处播报都要用，故按
    标的+操作日缓存120秒，避免重复占用 QMT 连接；失败结果同样缓存，防止每处
    播报都各等一次20秒超时。
    """
    cache_key = f"{final_buy.get('ts_code', '')}|{action_date}"
    with _LIVE_SIZING_LOCK:
        hit = _LIVE_SIZING_CACHE.get(cache_key)
        if hit is not None and (time.time() - hit[0]) < _LIVE_SIZING_TTL_SEC:
            return hit[1]
        result = _live_plan_sizing_uncached(final_buy, action_date, signal_date)
        _LIVE_SIZING_CACHE[cache_key] = (time.time(), result)
        return result


def _live_plan_sizing_uncached(
    final_buy: dict[str, Any], action_date: str, signal_date: str
) -> dict[str, Any] | None:
    """_live_plan_sizing 的实际查询体，见其文档字符串。"""
    try:
        import pandas as pd

        ts_code = str(final_buy.get("ts_code", ""))
        name_s = str(final_buy.get("name", ""))
        ref_price = float(final_buy.get("price", 0.0) or 0.0)
        seed_qty = int(final_buy.get("shares", 0) or 0)
        if not ts_code or ref_price <= 0 or seed_qty <= 0:
            return None

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        with _qmt_lock:
            adapter = _qmt_get(config.get("broker", {}))
            account, positions_live = _qmt_query_account_positions(adapter, timeout_sec=20.0)

        if _broker_has_preexisting_strategy_position(positions_live, action_date):
            return {
                "shares": 0,
                "order_price": _round_stock_price(
                    ref_price * (1 + _limit_up_pct_for_stock(ts_code, name_s))
                ),
                "reference_price": ref_price,
                "cash_cap": 0.0,
                "cash_bound": False,
                "blocked_by_existing_position": True,
                "available_cash": float(getattr(account, "available_cash", 0.0) or 0.0),
                "total_asset": float(getattr(account, "total_asset", 0.0) or 0.0),
                "transition": False,
                "risk_flags": "EXISTING_STRATEGY_POSITION_BLOCK",
            }
        market_value = _strategy_only_market_value(
            positions_live, exclude_due_today=False, as_of_date=action_date, family="ACE"
        )
        m_market_value = _strategy_only_market_value(positions_live, family=M_LEG)
        sized = resize_buy_orders_for_live_account(
            planned_orders=pd.DataFrame([{
                "ts_code": ts_code,
                "name": name_s,
                "side": "BUY",
                "strategy_leg": str(final_buy.get("strategy", "")),
                "signal_date": signal_date,
                "reference_price": ref_price,
                "round_lot_shares": seed_qty,
                "estimated_shares": seed_qty,
            }]),
            account=account,
            quote_map={},
            current_market_value=market_value,
            transition_full_cash=False,
            log_adjust=False,   # 播报预演，不打下单路径的"实盘定仓…调整为N股"
            m_market_value=m_market_value,
            action_date=action_date,
        )
        row = sized.iloc[0]
        qty = int(row.get("round_lot_shares", 0) or 0)
        cash_cap = to_float(row.get("live_cash_cap", 0.0))
        target_amount = to_float(row.get("live_target_amount", 0.0))
        min_acceptable_amount = to_float(row.get("live_min_acceptable_amount", 0.0))
        hard_cap_amount = to_float(row.get("live_hard_cap_amount", 0.0))
        order_price = _round_stock_price(
            ref_price * (1 + _limit_up_pct_for_stock(ts_code, name_s))
        )
        if hard_cap_amount > 0 and order_price > 0:
            qty = min(
                qty,
                _floor_buy_quantity_by_amount(hard_cap_amount, order_price, 100),
            )
        # 绑定约束判据：冻结后剩下的钱不够再买一手 = 现金已用满；
        # 还够买好几手却没买 = 被仓位/单笔/退出容量上限截住。不能用固定金额阈值，
        # 整百股取整本身最多就会剩下99股的钱。
        cash_bound = bool(
            cash_cap > 0 and order_price > 0
            and (cash_cap - qty * order_price) < 100 * order_price
        )
        return {
            "shares": max(qty, 0),
            "order_price": order_price,
            "reference_price": ref_price,
            "cash_cap": cash_cap,
            "target_amount": target_amount,
            "min_acceptable_amount": min_acceptable_amount,
            "hard_cap_amount": hard_cap_amount,
            "cash_bound": cash_bound,
            "available_cash": float(getattr(account, "available_cash", 0.0) or 0.0),
            "total_asset": float(getattr(account, "total_asset", 0.0) or 0.0),
            "transition": False,
            "risk_flags": str(row.get("risk_flags", "") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger().warning("播报预演实盘定仓失败（回退名义值播报，不影响交易）：%s", exc)
        return None


def _format_live_plan_line(
    final_buy: dict[str, Any],
    live: dict[str, Any] | None,
    *,
    fallback_reason: str = "QMT账户不可读",
) -> str:
    """开仓计划正文：显示82.5%实际成交目标及85%最坏委托硬顶。

    集合竞价实际成交价事前不可知，因此不把涨停预挂冻结额冒充最终成交额；
    明确展示固定资金目标，并说明开盘确认后由现有POV按真实金额缺口校准。

    ⚠️ 改这里的措辞必须同步核对 start_windows.py 的 should_print_line 白名单：
    终端默认按关键词逐行过滤，【最终结果】里这行没有 ┃ 前缀兜底，措辞一旦不含
    白名单词就会被终端整行吞掉（日志文件仍完整），表现为"打印消失"。当前依赖的
    命中词：可用资金 / 名义计划 / 计划开仓 / 开不出仓。
    """
    head = f"策略{final_buy['strategy']} {final_buy['ts_code']} {final_buy['name']}"
    if live is None:
        return (f"{head} 名义计划{final_buy['shares']}股"
                f"【{fallback_reason}，此为按{_planned_equity_base() / 10000:.0f}万资金模型的"
                f"名义值，不是实际下单量；09:20仍会按真实可用资金÷涨停价定仓】")
    if live.get("blocked_by_existing_position"):
        return f"{head} 暂不开仓（券商仍有本系统旧策略仓，确认清仓前禁止新买入）"
    if live["shares"] <= 0:
        return (f"{head} 明日资金不足，开不出仓（可用资金{live['available_cash'] / 10000:.2f}万，"
                f"按涨停价{live['order_price']:.2f}元不足一手）"
                + (f"；风控标记：{live['risk_flags']}" if live["risk_flags"] else ""))
    froze = live["shares"] * live["order_price"]
    # 说清"钱为什么没花完"：cash_bound=剩余不够再买一手，现金已用满；
    # 否则是被目标仓位/单笔/退出容量上限截住，账上还有钱是故意不用。
    binding = ("（已按可用资金满额）" if live.get("cash_bound")
               else "（受目标仓位/容量上限约束，账上余钱是刻意不用）")
    target_amount = float(live.get("target_amount", 0.0) or 0.0)
    min_amount = float(live.get("min_acceptable_amount", 0.0) or 0.0)
    hard_amount = float(live.get("hard_cap_amount", 0.0) or 0.0)
    return (f"{head}｜计划实际开仓{target_amount / 10000:.2f}万（目标82.5%，"
            f"允许下限{min_amount / 10000:.2f}万/80%，绝对硬顶{hard_amount / 10000:.2f}万/85%）"
            f"；可用资金{live['available_cash'] / 10000:.2f}万"
            f"（总资产{live['total_asset'] / 10000:.2f}万）"
            f"→ 09:20按涨停价{live['order_price']:.2f}元集合竞价预挂"
            f"→ 竞价种子单{live['shares']}股，最坏冻结{froze / 10000:.2f}万{binding}"
            + "。09:30确认实际成交额，低于82.5%的缺口由现有POV在+2%追价和85%硬顶内校准")


def _exit_method_desc(strategy: str, exit_rule: str) -> str:
    """按对应策略的平仓逻辑给出平仓时点/方式描述。

    - 普通D：T+2小仓位14:55卖；实时容量不足的大仓位先POV、14:55卖余仓。
    - D接力：T+1的09:23只卖竞价安全部分，其余在09:30后资金中性成对POV。
    - T+1开盘卖（含 *_open）：09:30 开盘平仓，买10/买5挂限价。
    - T+N收盘卖（默认 A/C/D/E *_close）：14:55卖出实际余仓。
    口径与 check_and_close_positions / job_premarket_sell 一致。
    """
    s = str(strategy).upper()
    rule = str(exit_rule).lower()
    if s == "D":
        if "open" in rule:
            return "D接力：09:23竞价安全部分＋09:30后卖D/买候选成对POV"
        return "普通D按T+2收盘退出：小仓位14:55卖；容量不足的大仓位先POV，14:55卖实际余仓"
    if "open" in rule:
        return "09:30开盘平仓（买10/买5挂限价）"
    return (
        "14:55收盘平仓（按可成交下限价提交；跌停封死、停牌或无买盘时仍可能无法成交，"
        "系统会继续核查余仓并告警）"
    )


def _ordinary_open_plan_expired(
    final_buy: dict[str, Any] | None,
    action_date: str,
) -> bool:
    """判断任意静态候选的执行/补仓窗口是否已结束且没有任何执行活动。

    普通候选默认09:35停止追买，E使用13:30专用截止；D是盘中实时策略，
    不走本函数。候选过期只影响原计划执行与播报，不释放D的全日互斥门禁。
    """

    if not final_buy:
        return False
    leg = str(final_buy.get("strategy", "")).strip().upper().replace("龙头", "")
    if not leg or leg == "D":
        return False
    execution_cutoff = OPEN_PLAN_EXECUTION_CUTOFF_BY_LEG.get(
        leg, OPEN_PLAN_DEFAULT_EXECUTION_CUTOFF
    )
    now = now_beijing()
    today = now.strftime("%Y%m%d")
    compact_action = str(action_date or "").replace("-", "")
    e_resolved = leg == "E" and _e_retry_resolved_today()[0]
    if compact_action > today:
        return False
    if compact_action == today and now.time() < execution_cutoff and not e_resolved:
        return False
    if (
        has_position_bought_today("ACE")
        or has_open_local_position("ACE")
        or bool(load_pending_buys())
        or _pov_active_today()
        or (leg == "E" and _e_retry_running())
    ):
        return False
    return True


def _local_position_blocks_open_plan_broadcast(
    positions: list[dict[str, Any]],
    action_date: str,
) -> bool:
    """播报侧串行单仓门禁：任何策略旧仓都必须把可执行开仓计划归零。

    候选仍可单独展示供审计，但不能再把被旧仓阻断的候选计入“开仓计划”。
    近期被自动误清的持仓也继续按策略仓处理，直至券商恢复对账或三次完整
    快照确认其确实不存在。已经写入 broker_confirmed_absent=True 的记录正是
    三次完整快照确认后的关闭结果，不能再被摘要层重复判成“尚未清空”。
    """

    return any(
        (
            str(position.get("status", "")).lower() in {"open", "sell_pending"}
            or (
                not bool(position.get("broker_confirmed_absent", False))
                and _is_recent_auto_ghost_clear(position, as_of_date=action_date)
            )
        )
        # 2026-11-12 ACE+M：M持仓不阻断ACE开仓计划。
        and position_in_family(position, "ACE")
        for position in positions
    )


def _log_final_decision_summary(signal_date: str, action_date_compact: str, buy_orders: Any) -> None:
    """打印【最终结果】：先判A>C>E静态计划；三腿均无候选时才允许D盘中执行。"""
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        mode_name = "A_C_E_D"
        readable = f"{action_date_compact[:4]}-{action_date_compact[4:6]}-{action_date_compact[6:]}" \
            if len(action_date_compact) == 8 else action_date_compact

        # 最终汇总必须与正式组合共用停手门禁；候选不等于可执行计划。
        # 在预演定仓之前短路，避免停手日仍查券商并显示名义股数/准备下单时间。
        stop_reason = _equity_curve_stop_block_reason(action_date_compact)
        if stop_reason:
            logger().info("\n".join([
                "======================== 最终结果 ========================",
                f"操作日({readable})  正式组合：{mode_name}",
                f"判定：{stop_reason}",
                "开仓计划：⏸ 无 —— 方案甲停手门禁阻断新增买入；A/C/E候选仅供审计，D不启动",
                "已有持仓的退出仍按原规则执行，须通过账户及成交核验。",
                "=" * 60,
            ]))
            return

        # ── mode1 候选：A/C 计划单优先，否则 E；内部abc变量名为兼容旧接口保留 ──
        abc_rows: list[dict[str, Any]] = []
        if buy_orders is not None and not buy_orders.empty:
            for _, r in buy_orders.iterrows():
                shares = int(r.get("round_lot_shares", r.get("estimated_shares", 0)) or 0)
                price = float(r.get("reference_price", 0.0) or 0.0)
                if shares <= 0 or price <= 0:
                    # PLAN_ONLY 占位计划（如C策略0股@0.00仅模拟观察）不算实盘买入。
                    # 必须与 combined_live_engine.build_abc_buy_decisions 的
                    # qty>0 & ref_price>0 过滤口径一致，否则【最终结果】会把
                    # 模拟观察计划当成mode1买入展示，与实际执行（E顶上）不符。
                    continue
                exit_n = int(r.get("exit_n_days", 2) or 2)
                try:
                    bd = datetime.datetime.strptime(action_date_compact, "%Y%m%d").date()
                    exit_date = next_n_trade_days(bd, exit_n).strftime("%Y%m%d")
                except Exception:
                    exit_date = ""
                abc_rows.append({
                    "strategy": str(r.get("strategy_leg", "")) or "ABC",
                    "ts_code": str(r.get("ts_code", "")),
                    "name": str(r.get("name", "")),
                    "shares": shares,
                    "price": price,
                    "exit_date": exit_date,
                    "exit_rule": "T+2收盘",
                })
        e_sig = _load_e_signal_for_signal_date(signal_date)
        e_buy: dict[str, Any] | None = None
        _has_ac_plan = bool(abc_rows)
        if (not _has_ac_plan and e_sig
                and str(e_sig.get("planned_buy_date", "")) == action_date_compact
                and bool(e_sig.get("allow_buy_reliable", False))):
            price = float(e_sig.get("limit_close", 0.0) or 0.0)
            e_buy = {
                "strategy": "E",
                "ts_code": str(e_sig.get("ts_code", "")),
                "name": str(e_sig.get("name", "")),
                "shares": _planned_shares_by_equity(e_sig.get("position_pct", ACE_FULL_TARGET_PCT), price),
                "price": price,
                "exit_date": str(e_sig.get("planned_exit_date", "")),
                "exit_rule": str(e_sig.get("planned_exit_rule", "T+2_close")),
            }
        # 腿序 A > C > E：A/C计划均先于E。
        _a_rows = [r for r in abc_rows if str(r.get("strategy", "")).strip().upper() != "C"]
        _c_rows = [r for r in abc_rows if str(r.get("strategy", "")).strip().upper() == "C"]
        if _a_rows:
            mode1_buys = _a_rows
        elif _c_rows:
            mode1_buys = _c_rows
        elif e_buy:
            mode1_buys = [e_buy]
        else:
            mode1_buys = []

        # 串行单仓守卫：任何策略旧仓都必须阻断新仓。
        # 候选可以继续展示供审计，但【最终结果】只能统计真正可执行的开仓计划。
        # 所有本地策略旧仓都参与阻断，候选展示不能绕过真实下单门禁。
        _blocked_by_holding = _local_position_blocks_open_plan_broadcast(
            load_positions(), action_date_compact
        )

        # ── 按正式腿序决策 ──
        final_buys: list[dict[str, Any]] = []
        note = ""
        if _blocked_by_holding:
            final_buys = []
            note = "有旧策略仓尚未实际清空，取消衔接开仓"
        elif not mode1_buys:
            final_buys = []
            note = "账户无旧策略仓，A/C/E均无开仓计划；后续仅按组合状态机判断D盘中路径"
        else:
            final_buys = mode1_buys
            note = "按腿序A>C>E取第一个收盘后候选；存在任一候选/计划时D全日关闭"

        expired_buys = (
            list(final_buys)
            if len(final_buys) == 1
            and _ordinary_open_plan_expired(final_buys[0], action_date_compact)
            else []
        )
        if expired_buys:
            expired = expired_buys[0]
            final_buys = []
            note += (
                f"；原{expired.get('strategy','')}计划 {expired.get('ts_code','')} "
                f"{expired.get('name','')} 的09:20/09:30窗口已过且未成交，当前不追补"
            )

        # ── 打印 ──
        # 必须"先算完、再单次原子写入"(2026-07-31)：daemon 多线程并发打日志
        # （账户心跳/候选播报/决策链播报），逐行 logger 输出之间只要有耗时动作
        # （这里是预演定仓要查一次 QMT 账户），别的线程就会把整块日志插进来，
        # 用户看到的就是"开仓计划：共1笔"后面紧跟一大段无关内容、明细行不见了。
        # 与【决策优先级树状图】同一写法：拼 lines，最后一条消息落盘。
        _day_lab = "今日" if readable == today_beijing().strftime("%Y-%m-%d") else "明日"
        out: list[str] = [
            "=" * 60,
            "======================== 最终结果 ========================",
            f"{_day_lab}({readable})  正式组合：{mode_name}",
            f"判定：{note}",
        ]
        if expired_buys:
            expired = expired_buys[0]
            out.append(
                "开仓计划：❌ 当前无 —— 原计划"
                f"{expired.get('strategy','')} {expired.get('ts_code','')} {expired.get('name','')}"
                "已过执行窗口且未成交；不追买。该候选仍按全日互斥规则阻断D，"
                "D今日不扫描也不开仓"
            )
        elif not final_buys:
            out.append("开仓计划：❌ 无 —— A/C/E均无开仓计划")
        else:
            out.append(f"开仓计划：✅ 共 {len(final_buys)} 笔")
            for b in final_buys:
                # 与【最终开仓计划】框同一套口径：单笔时按真实账户预演09:20定仓，
                # 显示可用资金/挂单价/计划股数；多笔（串行单仓下不应出现）时无法
                # 逐笔分配同一笔现金，回退名义值并由 _format_live_plan_line 标注。
                live_b = (_live_plan_sizing(b, action_date_compact, signal_date)
                          if len(final_buys) == 1 else None)
                out.append("  • " + _format_live_plan_line(
                    b, live_b,
                    fallback_reason=("多笔计划无法逐笔预演同一笔现金"
                                     if len(final_buys) > 1 else "QMT账户不可读"),
                ))
            out.append(f"准备下单时间：{readable} 09:00生成计划 → 09:20复核并集合竞价预挂 → 09:30确认/补单")
            seen_exits: set[tuple[str, str]] = set()
            for b in final_buys:
                ed = str(b.get("exit_date", ""))
                ed_readable = f"{ed[:4]}-{ed[4:6]}-{ed[6:]}" if len(ed) == 8 else (ed or "未知")
                method = _exit_method_desc(b.get("strategy", ""), b.get("exit_rule", ""))
                key = (ed_readable, method)
                if key in seen_exits:
                    continue
                seen_exits.add(key)
                out.append(f"准备平仓时间：{ed_readable} {method}")
        out.append("==========================================================")
        logger().info("\n".join(out))
    except Exception as exc:
        logger().error("最终结果汇总异常：%s", exc)


def _describe_holdings(positions: list[dict[str, Any]], *, with_quote: bool = True) -> str:
    """把持仓按(策略腿,股票)汇总成一行描述。

    同一只票可能有多条记录：09:20竞价种子单 + 09:30起的POV补单分片。逐条打印
    会让同一笔持仓被列多次，看起来像重复建仓；成本也必须按加权算，不能取某一
    片的买价（2026-08-05华之杰：200股@46.38 vs 4800股@45.47，差1.8个百分点）。

    现价取不到时降级只显示成本，不阻塞播报。
    """

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for position in positions:
        key = (
            str(position.get("strategy_leg", "") or "?").upper(),
            str(position.get("ts_code", "")),
        )
        item = grouped.setdefault(
            key,
            {
                "name": str(position.get("name", "")),
                "shares": 0,
                "cost": 0.0,
                "slices": 0,
                "exit": str(position.get("planned_exit_date", "")),
                "manual": False,
            },
        )
        try:
            shares = int(float(position.get("shares", 0) or 0))
        except (TypeError, ValueError):
            shares = 0
        try:
            buy_price = float(position.get("buy_price", 0) or 0)
        except (TypeError, ValueError):
            buy_price = 0.0
        item["shares"] += shares
        item["cost"] += shares * buy_price
        item["slices"] += 1
        if _position_manual_exit_only(position):
            item["manual"] = True

    last_prices: dict[str, float] = {}
    if with_quote:
        try:
            codes = sorted({code for _, code in grouped})
            if codes:
                config = load_json_config(PROJECT_ROOT / "config" / "config.json")
                if config.get("broker_adapter_enabled") and config.get("qmt_enabled"):
                    with _qmt_lock:
                        adapter = _qmt_get(config.get("broker", {}))
                        quote_map = adapter.get_full_tick(codes)
                    for code in codes:
                        quote = quote_map.get(code)
                        price = float(getattr(quote, "last_price", 0.0) or 0.0) if quote else 0.0
                        if price > 0:
                            last_prices[code] = price
        except Exception as exc:
            logger().debug("持仓播报取现价失败（不影响播报）：%s", exc)

    parts: list[str] = []
    total_cost = total_value = 0.0
    for (leg, code), item in grouped.items():
        avg_cost = item["cost"] / item["shares"] if item["shares"] else 0.0
        text = (
            f"{leg}策略 {code} {item['name']} {item['shares']}股"
            + (f"（{item['slices']}片）" if item["slices"] > 1 else "")
            + (f" 成本{avg_cost:.2f}" if avg_cost > 0 else "")
        )
        price = last_prices.get(code, 0.0)
        if price > 0 and avg_cost > 0:
            value = price * item["shares"]
            total_cost += item["cost"]
            total_value += value
            text += (
                f" 现价{price:.2f} 收益{price / avg_cost - 1:+.2%}"
                f" 市值{value / 10000:.2f}万"
            )
        text += (
            "（仅人工退出，系统禁卖）"
            if item["manual"]
            else f"（计划{item['exit']}平仓）"
        )
        parts.append(text)
    desc = "、".join(parts)
    if len(grouped) > 1 and total_cost > 0:
        desc += (
            f"；合计成本{total_cost / 10000:.2f}万 市值{total_value / 10000:.2f}万 "
            f"收益{total_value / total_cost - 1:+.2%}"
        )
    return desc


def _format_candidate_stock(candidate: dict[str, Any]) -> str:
    """把单腿第一名格式化为统一的“代码+名称”，只用于播报。"""

    code = str(candidate.get("ts_code", "") or "").strip()
    name = str(candidate.get("name", "") or "").strip()
    return " ".join(part for part in (code, name) if part) or "候选信息缺失"


def _format_strategy_candidate(strategy: str, candidate: dict[str, Any]) -> str:
    """把单腿第一名格式化为统一的“策略+代码+名称”，只用于播报。"""

    return f"{strategy}策略 {_format_candidate_stock(candidate)}"


def _load_candidate_csv_for_broadcast(
    strategy: str,
    signal_date: str,
) -> dict[str, Any] | None:
    """从单腿审计 CSV 读取当日第一名，只用于展示。"""

    import pandas as pd

    leg = str(strategy).strip().upper()
    if leg in {"A", "C"}:
        paths = sorted(
            (
                PROJECT_ROOT
                / "reports"
                / "paper_trade"
                / "ab_filtered_daily_ops"
            ).glob(f"*_{signal_date}_{leg.lower()}_candidates.csv")
        )
    elif leg == "E":
        paths = [
            PROJECT_ROOT
            / "reports"
            / f"strategy_{leg.lower()}"
            / f"{leg.lower()}_signal_{signal_date}_candidates.csv"
        ]
    else:
        return None

    for path in reversed(paths):
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (pd.errors.EmptyDataError, OSError):
            continue
        if frame.empty:
            continue
        if "candidate_rank" in frame.columns:
            frame = frame.assign(
                _candidate_rank=pd.to_numeric(
                    frame["candidate_rank"], errors="coerce"
                ).fillna(10**9)
            ).sort_values("_candidate_rank", kind="stable")
        row = frame.iloc[0]
        code = str(row.get("ts_code", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        if code:
            return {"strategy": leg, "ts_code": code, "name": name}
    return None


def _load_readonly_candidate_for_broadcast(
    strategy: str,
    signal_date: str,
) -> tuple[dict[str, Any] | None, str]:
    """读取未转成正式开仓计划的单腿第一名及阻断原因。

    A/C 候选来自独立候选 CSV；E 必须先确认运行状态为
    ``NO_SIGNAL_OCCUPIED`` 且候选数大于0，再使用状态账本或候选 CSV。
    所有返回值只参与播报，禁止进入正式候选排序或下单。
    """

    leg = str(strategy).strip().upper()
    if leg in {"A", "C"}:
        candidate = _load_candidate_csv_for_broadcast(leg, signal_date)
        return candidate, "未生成正式计划" if candidate is not None else ""
    if leg != "E":
        return None, ""

    run = signal_run_by_signal_date(
        PROJECT_ROOT
        / "reports"
        / f"strategy_{leg.lower()}"
        / f"{leg.lower()}_signal_runs_recent.json",
        signal_date,
    )
    if run is None or str(run.get("status", "")).strip().upper() != NO_SIGNAL_OCCUPIED:
        return None, ""
    try:
        candidate_count = int(run.get("candidate_count", 0) or 0)
    except (TypeError, ValueError):
        return None, ""
    if candidate_count <= 0:
        return None, ""

    code = str(run.get("candidate_ts_code", "") or "").strip()
    name = str(run.get("candidate_name", "") or "").strip()
    candidate = (
        {"strategy": leg, "ts_code": code, "name": name}
        if code
        else _load_candidate_csv_for_broadcast(leg, signal_date)
    )
    if candidate is None:
        return None, ""

    reason = str(run.get("reason", "") or "")
    if "账户有未平仓头寸" in reason or "当前持仓阻断" in reason:
        reason_label = "信号生成时有持仓"
    elif "已有计划" in reason or "占用" in reason:
        reason_label = "上游策略占用"
    else:
        reason_label = "未生成正式信号"
    return candidate, reason_label


def _build_candidate_choice_lines(
    candidate_rows: list[tuple[str, dict[str, Any] | None, str]],
    ranking_legs: list[str],
    final_buy: dict[str, Any] | None,
    blocking_holding_legs: list[str],
    execution_override: str = "",
) -> list[str]:
    """生成“各腿候选→让路排序→实际选择”的紧凑播报。

    candidate_rows 每腿只放该策略按自身规则选出的第一名；没有第一名时必须同时
    给出原因，避免把“未评估/被持仓阻断”误写成“策略没有候选”。ranking_legs
    是当前模式真正参与资金竞争的腿序，不能让展示层自行重新解释优先级。
    """

    by_leg = {leg: candidate for leg, candidate, _reason in candidate_rows}
    lines = ["各策略入围候选（每腿按自身规则取第一名）："]
    for leg, candidate, reason in candidate_rows:
        if candidate is not None:
            lines.append(f"  {leg}策略候选：{_format_candidate_stock(candidate)}")
        else:
            lines.append(f"  {leg}策略候选：无（{reason}）")

    ranked = [
        _format_strategy_candidate(leg, by_leg[leg])
        for leg in ranking_legs
        if by_leg.get(leg) is not None
    ]
    lines.append(f"账户空仓时让路排序：{' > '.join(ranked) if ranked else '无入围候选'}")
    lines.append(f"按让路规则首选：{ranked[0] if ranked else '无'}")

    holding_legs = list(dict.fromkeys(str(leg).upper() for leg in blocking_holding_legs if str(leg)))
    if execution_override:
        lines.append(f"实际选择：不开仓｜{execution_override}")
    elif holding_legs:
        holding_text = "、".join(f"{leg}策略" for leg in holding_legs)
        lines.append(f"实际选择：不开仓｜由于当前{holding_text}有持仓，以上候选均不会开仓")
    elif final_buy is not None:
        strategy = str(final_buy.get("strategy", "") or "?").upper()
        lines.append(f"实际选择：{_format_strategy_candidate(strategy, final_buy)}")
    else:
        lines.append("实际选择：不开仓｜当前没有可执行候选")
    return lines


def _log_decision_chain_summary(signal_date: str) -> None:
    """按当前正式腿序播报下一交易日开仓计划；只做展示，不参与下单。"""

    try:
        import pandas as pd

        try:
            signal_day = datetime.datetime.strptime(signal_date, "%Y%m%d").date()
            action_date = next_n_trade_days(signal_day, 1).strftime("%Y%m%d")
        except Exception:
            action_date = ""
        day_label = (
            "今日"
            if action_date and action_date == today_beijing().strftime("%Y%m%d")
            else "明日"
        )

        candidates: dict[str, dict[str, Any]] = {}
        order_files = sorted(
            glob.glob(
                str(
                    PROJECT_ROOT
                    / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_planned_orders.csv"
                )
            )
        )
        if order_files:
            try:
                orders = pd.read_csv(order_files[-1], low_memory=False)
            except pd.errors.EmptyDataError:
                # A/C 当日没有候选时，计划文件可能是合法的0字节空文件。
                # 播报层应把它视为“无候选”，不能让非关键展示任务报错。
                orders = pd.DataFrame()
            if not orders.empty and "side" in orders.columns:
                buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
                for _, row in buys.iterrows():
                    leg = str(row.get("strategy_leg", "")).strip().upper()
                    shares = int(float(row.get("round_lot_shares", 0) or 0))
                    price = float(row.get("reference_price", 0.0) or 0.0)
                    if leg in {"A", "C"} and shares > 0 and price > 0:
                        candidates[leg] = {
                            "strategy": leg,
                            "ts_code": str(row.get("ts_code", "")),
                            "name": str(row.get("name", "")),
                            "shares": shares,
                            "price": price,
                            "exit_rule": "T+2_close",
                        }

        e_signal = _load_e_signal_for_signal_date(signal_date)
        if (
            e_signal
            and str(e_signal.get("planned_buy_date", "")) == action_date
            and bool(e_signal.get("allow_buy_reliable", False))
        ):
            price = float(e_signal.get("limit_close", 0.0) or 0.0)
            shares = _planned_shares_by_equity(e_signal.get("position_pct", ACE_FULL_TARGET_PCT), price)
            if shares > 0 and price > 0:
                candidates["E"] = {
                    "strategy": "E",
                    "ts_code": str(e_signal.get("ts_code", "")),
                    "name": str(e_signal.get("name", "")),
                    "shares": shares,
                    "price": price,
                    "exit_date": str(e_signal.get("planned_exit_date", "")),
                    "exit_rule": str(e_signal.get("planned_exit_rule", "T+2_close")),
                }

        readonly_candidates: dict[str, dict[str, Any]] = {}
        readonly_reasons: dict[str, str] = {}
        for leg in ("A", "C", "E"):
            if leg in candidates:
                continue
            readonly_candidate, readonly_reason = (
                _load_readonly_candidate_for_broadcast(leg, signal_date)
            )
            if readonly_candidate is not None:
                readonly_candidates[leg] = readonly_candidate
                readonly_reasons[leg] = readonly_reason

        broadcast_candidates = dict(readonly_candidates)
        broadcast_candidates.update(candidates)
        broadcast_selected = next(
            (
                broadcast_candidates[leg]
                for leg in ("A", "C", "E")
                if leg in broadcast_candidates
            ),
            None,
        )

        selected = next(
            (candidates[leg] for leg in ("A", "C", "E") if leg in candidates),
            None,
        )
        positions = [
            position
            for position in load_positions()
            if str(position.get("status", "")).lower() in {"open", "sell_pending"}
        ]
        blocked = _local_position_blocks_open_plan_broadcast(positions, action_date)
        final_buy = None if blocked else selected
        original_final_buy = final_buy
        # 停手日候选本来就不应成交，必须先判停手再判“错过窗口”。
        # 否则上午故障恢复后会把正常空仓误报为漏单，并丢失停手原因。
        equity_curve_stop_reason = (
            _equity_curve_stop_block_reason(str(action_date)) if final_buy else ""
        )
        execution_expired = bool(
            final_buy and not equity_curve_stop_reason
            and _ordinary_open_plan_expired(final_buy, action_date)
        )
        if execution_expired or equity_curve_stop_reason:
            final_buy = None
        execution_block_reason = ""
        if final_buy:
            buy_gate_ok, buy_gate_reason = _new_buy_execution_gate()
            if not buy_gate_ok:
                execution_block_reason = buy_gate_reason
                final_buy = None
        hold_line = _describe_holdings(positions, with_quote=False) if positions else ""
        live_sizing = (
            _live_plan_sizing(final_buy, action_date, signal_date) if final_buy else None
        )
        selected_leg = str(selected.get("strategy", "")) if selected else ""
        actual_leg = str(final_buy.get("strategy", "")) if final_buy else ""
        path_tag = " ◄―本次路径"
        d_holding = any(
            str(position.get("strategy_leg", "")).strip().upper() == "D"
            for position in positions
        )
        candidate_text: dict[str, str] = {}
        for leg in ("A", "C", "E"):
            candidate = broadcast_candidates.get(leg)
            if candidate is None:
                candidate_text[leg] = "不触发｜无候选"
                continue
            if blocked:
                status_text = "不触发（当前有持仓）"
            elif execution_expired and leg == selected_leg:
                status_text = "不触发（已过执行窗口）"
            elif execution_block_reason and leg == selected_leg:
                status_text = "不触发（新增BUY安全门禁）"
            elif equity_curve_stop_reason and leg == selected_leg:
                status_text = "不触发（方案甲停手）"
            elif leg == actual_leg:
                status_text = "成立"
            elif leg in readonly_candidates:
                status_text = (
                    f"不触发（{readonly_reasons.get(leg) or '未生成正式计划'}）"
                )
            else:
                status_text = "不触发（上游策略占用）"
            candidate_text[leg] = (
                f"{status_text}｜候选 {_format_candidate_stock(candidate)}"
            )

        # 流程图、逐腿决策链、最终计划必须作为同一条多行日志原子输出；
        # 周期播报时三块一起出现，不能只剩最终摘要。
        bottom = "┃━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        lines = [
            "┃━━━━━━━━━━━━ 决策优先级流程图 ━━━━━━━━━━━━",
            f"┃ 信号日：{signal_date}；操作日：{action_date or '未知'}",
            "┃ 当前互斥顺序：①持仓 > ②非D候选/计划(A>C>E) > ③D盘中",
            "┃",
            "┃ 【0】券商仍有本系统策略仓？",
            f"┃   ├─ 是 → 不开新仓，确认实际清仓后再等待下一候选{path_tag if blocked else ''}",
            "┃   └─ 否",
            "┃        ↓",
            "┃ 【1】今日存在A/C/E正式候选或开仓计划？",
            "┃   ├─ 是 → 按A>C>E取首个计划执行；D全日不扫描、不记录、不下单",
            "┃   └─ 否",
            "┃        ↓",
            "┃ 【2】D从09:30开始盘中扫描/记录，满足正式条件才允许开仓",
            f"┃   ├─ D已成交 → 写入持仓并回到【0】{path_tag if d_holding else ''}",
            f"┃   └─ D未成交/未触发 → 空仓{path_tag if not blocked and not selected_leg else ''}",
            bottom,
            "┃",
            "┃━━━━━━━━━━━━━━ 开仓决策链 ━━━━━━━━━━━━━━",
            f"┃ ① A主策略：{candidate_text['A']}",
            f"┃ ② C强势龙头：{candidate_text['C']}",
            f"┃ ③ E策略：{candidate_text['E']}",
            f"┃ ④ D盘中兜底：{'已有D策略仓，串行资金门禁生效' if d_holding else '不触发｜无候选'}",
            "┃ 账户空仓时首选："
            + (
                _format_strategy_candidate(
                    str(broadcast_selected.get("strategy", "")),
                    broadcast_selected,
                )
                if broadcast_selected
                else "无"
            ),
            bottom,
            "┃",
            "┃━━━━━━━━━━━━━━ 最终开仓计划 ━━━━━━━━━━━━━━",
        ]
        if blocked:
            lines.append(f"┃ ★ 不开新仓：券商仍有策略持仓｜{hold_line}")
        elif execution_expired and original_final_buy:
            lines.append(
                "┃ ★ 当前不开仓：原计划"
                f"{_format_strategy_candidate(str(original_final_buy.get('strategy', '?')), original_final_buy)}"
                "已过执行窗口且未成交，不追补；该候选仍按全日互斥规则阻断D，"
                "D今日不扫描也不开仓"
            )
        elif execution_block_reason and original_final_buy:
            lines.append(
                "┃ ★ 不开新仓：候选"
                f"{_format_strategy_candidate(str(original_final_buy.get('strategy', '?')), original_final_buy)}"
                f"已被新增BUY安全门禁阻断｜{execution_block_reason}"
            )
        elif equity_curve_stop_reason and original_final_buy:
            lines.append(
                "┃ ★ 不开新仓：方案甲停手，候选"
                f"{_format_strategy_candidate(str(original_final_buy.get('strategy', '?')), original_final_buy)}"
                f"不执行，D也不启动｜{equity_curve_stop_reason}"
            )
        elif final_buy:
            lines.append(f"┃ ★ 开仓计划：{_format_live_plan_line(final_buy, live_sizing)}")
        else:
            lines.append(f"┃ ★ {day_label}A/C/E均无开仓计划")
        lines.append(bottom)
        logger().info("\n".join(lines))

        global _last_final_plan
        _last_final_plan = {
            "final_buy": original_final_buy,
            "live_sizing": live_sizing,
            "day_label": day_label,
            "hold_line": hold_line,
            "action_date": str(action_date),
            "signal_date": str(signal_date),
            "execution_expired": execution_expired,
            "execution_block_reason": execution_block_reason,
            "equity_curve_stop_reason": equity_curve_stop_reason,
            "computed_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        logger().warning("开仓决策链播报失败（不影响流水线）：%s", exc)


_last_final_plan: dict[str, Any] | None = None
_PLAN_PUSH_STATE = PROJECT_ROOT / "data" / "state" / "open_plan_push.json"
_MISSED_OPEN_ALERT_STATE = PROJECT_ROOT / "data" / "state" / "missed_open_alert.json"


def _d_candidate_gate_line(
    *,
    day_label: str,
    mode1_buy: dict[str, Any] | None,
) -> str:
    """决策链里的D候选门；只描述候选占用，持仓/过期状态由调用方覆盖。"""

    if mode1_buy:
        leg = str(mode1_buy.get("strategy", "A/C/E") or "A/C/E")
        return (
            f"阻断｜{day_label}已有{leg}正式开仓计划 "
            f"{mode1_buy.get('ts_code', '')} {mode1_buy.get('name', '')}；"
            "D全日不扫描、不记录、不下单"
        )
    return "允许｜全策略均无正式开仓计划时启动盘中监控（仍需实时行情+风控校验）"


def _plan_push_already_sent(action_date: str, occasion: str) -> bool:
    key = f"{action_date}-{occasion}"
    try:
        state = json.loads(_PLAN_PUSH_STATE.read_text(encoding="utf-8"))
        return state.get("last_pushed") == key
    except Exception:
        return False


def _retry_morning_plan_notification_if_needed(source: str) -> None:
    """08:50推送失败/daemon稍晚启动时，在09:20前幂等补发一次。"""
    now = now_beijing()
    if not is_trade_day(now.date()) or not (
        datetime.time(8, 50) <= now.time() < datetime.time(9, 20)
    ):
        return
    action_date = now.strftime("%Y%m%d")
    if _plan_push_already_sent(action_date, "早盘"):
        return
    logger().warning("%s：08:50早盘计划通知尚未成功落标，立即幂等补发。", source)
    if not _last_final_plan or str(_last_final_plan.get("action_date", "")) != action_date:
        report_next_day_candidates()
    push_open_plan_notification("早盘")


def _notify_missed_open_window_if_needed(source: str) -> None:
    """候选窗口后计划仍零成交时告警；不追补静态候选，D仍全日关闭。"""
    now = now_beijing()
    if not is_trade_day(now.date()) or not (
        datetime.time(9, 35) <= now.time() < datetime.time(14, 40)
    ):
        return
    plan = _last_final_plan
    today_str = now.strftime("%Y%m%d")
    if not plan or str(plan.get("action_date", "")) != today_str:
        return
    # 即便拿到故障前缓存的候选，也必须重读当日停手判定。合规空仓不属于
    # 错过开仓窗口；此检查只读本地门禁，不查询账户，也不改变真实交易规则。
    if (plan.get("equity_curve_stop_reason") or plan.get("execution_block_reason")
            or _equity_curve_stop_block_reason(today_str)):
        return
    final_buy = plan.get("final_buy")
    if (
        not final_buy
        or not _ordinary_open_plan_expired(final_buy, today_str)
        or has_position_bought_today("ACE")
        or has_open_local_position("ACE")
        or bool(load_pending_buys())
        or _pov_active_today()
        or _d_relay_pair_active_today()
    ):
        return
    key = f"{today_str}-{final_buy.get('strategy','')}-{final_buy.get('ts_code','')}"
    try:
        old = json.loads(_MISSED_OPEN_ALERT_STATE.read_text(encoding="utf-8"))
        if old.get("last_alerted") == key:
            return
    except Exception:
        pass  # 可以忽略：去重记录读不了就当没提醒过，宁可重复提醒也不漏
    title = f"⚠️ 今日开仓窗口已错过:{final_buy.get('name','')}"
    body = (
        f"{source}发现今日原计划为策略{final_buy.get('strategy','')} "
        f"{final_buy.get('ts_code','')} {final_buy.get('name','')}，但09:20/09:30执行窗口内"
        "未完成开仓且当前无策略持仓/在途买单。系统不会在盘中追补已过期的静态候选，避免偏离回测；"
        "该候选仍按全日互斥规则阻断D，D今日不扫描、不记录、不下单。"
    )
    sent = _notify("buy_result", title, body, level="critical")
    if sent:
        mkdir_p(_MISSED_OPEN_ALERT_STATE.parent)
        _MISSED_OPEN_ALERT_STATE.write_text(
            json.dumps({"last_alerted": key, "alerted_at": now.isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    logger().error("[开仓窗口审计] %s", body)


def _m_plan_line(action_date: str, *, ace_active: bool) -> tuple[str, str]:
    """M在action_date的开仓计划摘要（推送用）；返回(文字, 第一候选名称)。"""
    try:
        from src.rolling_signal_store import latest_signal_for_buy_date

        signal = latest_signal_for_buy_date(M_SIGNAL_RECENT_PATH, str(action_date))
        if not signal:
            return "", ""
        if has_open_local_position(M_LEG):
            return "M：仍有M持仓（当天收盘卖出），M同一时间最多1只，不开新M。", ""
        cands = sorted(signal.get("candidates") or [], key=lambda c: int(c.get("rank", 999)))
        diag = signal.get("diagnostics") or {}
        if not cands:
            mood = diag.get("mood")
            mood_text = ("%.2f%%" % (float(mood) * 100)) if mood is not None and mood == mood else "无"
            return (f"M：无候选（昨日涨停今日平均{mood_text}，20cm涨停{int(diag.get('n20_limit_up') or 0)}只）。", "")
        names = "、".join(f"{c.get('name') or c.get('ts_code')}({c.get('ts_code')})" for c in cands[:3])
        pct = "闲置资金≤17.5%" if ace_active else "总资产82.5%"
        first = f"{cands[0].get('name') or ''}"
        return (f"M计划：{names}（按顺序，开盘涨停则换下一只，跳过ACE同票），仓位{pct}，"
                f"09:20按涨停价-0.01预挂、余量POV补足（≤开盘+2%），买入后下一个交易日收盘卖出。", first)
    except Exception as exc:
        logger().warning("M计划摘要生成失败（不影响交易）：%s", exc)
        return "", ""


def push_open_plan_notification(occasion: str) -> None:
    """推送开仓计划(2026-07-23 用户要求:收盘流水线完成推明日计划、早盘推今日计划,
    详细到策略名+股票+金额;无候选则明确提示无新开仓计划)。

    occasion='收盘'/'早盘'。数据源=_last_final_plan(由 _log_final_decision_summary
    刚算出,与开仓决策链日志同源,保证推送=真实开仓口径)。去重:同一 action_date
    +occasion 只推一次(防 report 被多次调用重复推)。
    """
    try:
        plan = _last_final_plan
        if not plan:
            logger().info("[开仓计划推送] 无最终计划数据(report 未生成),跳过 %s 推送。", occasion)
            return
        action = str(plan.get("action_date", ""))
        key = f"{action}-{occasion}"
        state = {}
        if _PLAN_PUSH_STATE.exists():
            try:
                state = json.loads(_PLAN_PUSH_STATE.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        if state.get("last_pushed") == key:
            return   # 已推过,不重复
        label = "明日" if occasion == "收盘" else "今日"
        fb = plan.get("final_buy")
        hold = plan.get("hold_line") or ""
        execution_expired = bool(plan.get("execution_expired", False))
        execution_block_reason = str(plan.get("execution_block_reason", "") or "")
        equity_curve_stop_reason = str(plan.get("equity_curve_stop_reason", "") or "")
        if fb and equity_curve_stop_reason:
            title = f"⏸ {label}方案甲停手:不开新仓"
            body = (
                f"{_format_strategy_candidate(str(fb.get('strategy', '?')), fb)}入选但不执行，"
                f"D也不启动。{equity_curve_stop_reason}。"
            )
        elif fb and execution_expired:
            title = f"⚠️ {label}原候选已错过执行窗口:{fb.get('name','')}"
            body = (
                f"{_format_strategy_candidate(str(fb.get('strategy', '?')), fb)}零成交且补仓窗口已结束；"
                "系统不追买原候选，但该候选仍按全日互斥规则阻断D，"
                "D今日不扫描、不记录、不下单。"
            )
        elif fb and execution_block_reason:
            title = f"⛔ {label}候选不可执行:{fb.get('name','')}"
            body = (
                f"{_format_strategy_candidate(str(fb.get('strategy', '?')), fb)}已入选，"
                f"但新增BUY安全门禁阻断：{execution_block_reason}。"
                "系统将保持空仓，不会自动或建议手动绕过认证门禁。"
            )
        elif fb:
            _ls = plan.get("live_sizing")
            if _ls is not None and int(_ls.get("shares", 0) or 0) <= 0:
                title = f"⚠️ {label}资金不足开不出仓:{fb.get('name','')}"
            else:
                title = f"📋 {label}开仓计划:{fb.get('strategy','')} {fb.get('name','')}"
            # 与决策链日志同源：直接用播报时算好的真实账户定仓结果，不重算
            body = _format_live_plan_line(fb, plan.get("live_sizing")) + "。"
            if _ace_gate_mode_for(action) == ACE_GATE_REDUCED:
                body += "（方案甲停手期：ACE按20%目标仓位执行，D不启动）"
        else:
            title = f"📋 {label}无新开仓计划"
            body = f"{label}所有收盘计划策略(A/C/E)均无符合条件标的,不开新仓。"
        # 2026-11-12 ACE+M：附上M的计划（M与ACE资金分开，独立执行）。
        m_line, m_first = _m_plan_line(action, ace_active=bool(fb) and not equity_curve_stop_reason)
        if m_line:
            if not fb and m_first:
                title = f"📋 {label}开仓计划:M {m_first}"
                body = f"{label}A/C/E无开仓计划。"
            body += f" {m_line}"
        if hold:
            body += f" ⚠{hold}"
        sent = _notify("buy_result", title, body, level="timeSensitive")
        if sent:
            mkdir_p(_PLAN_PUSH_STATE.parent)
            _PLAN_PUSH_STATE.write_text(
                json.dumps({"last_pushed": key}, ensure_ascii=False), encoding="utf-8"
            )
            logger().info("[开仓计划推送] 已推送 %s:%s", occasion, title)
        else:
            # 不写“已推”标记，让09:00或启动补检仍有机会重试；交易本身不受影响。
            logger().error("[开仓计划推送] %s通知未确认送达，保留重试资格:%s", occasion, title)
    except Exception as e:
        logger().warning("开仓计划推送异常(不影响交易):%s", e)


def _open_plan_execution_detail(plan: dict[str, Any]) -> str:
    """09:35核查详情：优先展示真实竞价/POV执行量，不再打印信号名义股数。"""

    fb = plan.get("final_buy") or {}
    ts_code = str(fb.get("ts_code", ""))
    head = f"策略{fb.get('strategy','')} {ts_code} {fb.get('name','')}"
    today_str = today_beijing().strftime("%Y%m%d")
    matched_positions = [
        position
        for position in load_positions()
        if str(position.get("status", "")).lower() in {"open", "sell_pending"}
        and str(position.get("buy_date", "")).replace("-", "") == today_str
        and (_ts_code_aliases(position.get("ts_code", "")) & _ts_code_aliases(ts_code))
    ]
    actual_qty = sum(int(position.get("shares", 0) or 0) for position in matched_positions)
    actual_cost = sum(
        int(position.get("shares", 0) or 0) * float(position.get("buy_price", 0.0) or 0.0)
        for position in matched_positions
    )
    if actual_qty > 0:
        actual_price = actual_cost / actual_qty if actual_cost > 0 else 0.0
        price_text = f"@均价{actual_price:.2f}" if actual_price > 0 else ""
        return f"{head}｜实际已成交并登记{actual_qty}股{price_text}"
    matching_pending = [
        item
        for item in load_pending_buys()
        if _ts_code_aliases(item.get("ts_code", "")) & _ts_code_aliases(ts_code)
    ]
    pending_qty = sum(int(item.get("qty", 0) or 0) for item in matching_pending)
    pending_price = next(
        (float(item.get("ref_price", 0.0) or 0.0) for item in matching_pending
         if float(item.get("ref_price", 0.0) or 0.0) > 0),
        0.0,
    )
    pov_item = next(
        (
            item
            for item in _pov_load_state().get("items", [])
            if _ts_code_aliases(item.get("ts_code", "")) & _ts_code_aliases(ts_code)
        ),
        None,
    )
    if pov_item:
        auction_qty = pending_qty or int(pov_item.get("auction_planned_qty", 0) or 0)
        target = float(
            pov_item.get("target_actual_amount", pov_item.get("target_amt", 0.0)) or 0.0
        )
        hard_cap = float(pov_item.get("hard_cap_amount", 0.0) or 0.0)
        if auction_qty > 0:
            if pending_qty > 0:
                price_text = f"@委托{pending_price:.2f}" if pending_price > 0 else ""
                seed_text = f"实际已挂竞价单{auction_qty}股{price_text}"
            else:
                seed_text = f"实盘竞价种子计划{auction_qty}股"
        else:
            seed_text = "竞价实际计划0股（竞价链未挂出，全部目标转POV）"
        return (
            f"{head}｜{seed_text}；POV按实际成交额校准至目标{target / 1e4:.2f}万/82.5%，"
            f"硬顶{hard_cap / 1e4:.2f}万/85%"
        )
    if pending_qty > 0:
        price_text = f"@{pending_price:.2f}" if pending_price > 0 else ""
        return f"{head}｜实际在途买单{pending_qty}股{price_text}"
    live = plan.get("live_sizing") or {}
    if live:
        return (
            f"{head}｜执行前预估竞价种子单{int(live.get('shares', 0) or 0)}股；"
            f"实际开仓目标{float(live.get('target_amount', 0.0) or 0.0) / 1e4:.2f}万/82.5%，"
            f"硬顶{float(live.get('hard_cap_amount', 0.0) or 0.0) / 1e4:.2f}万/85%"
        )
    return (
        f"{head}｜实际委托状态尚不可得（信号名义值{int(fb.get('shares', 0) or 0)}股"
        "不作为实盘委托数量）"
    )


def job_open_plan_fill_check() -> None:
    """09:35 开仓计划成交核查（2026-07-23 用户要求：计划没买成绝不允许无声漏过）。

    背景：07-23 星辉环材有开仓计划，却被幽灵持仓 BLOCK、全程零提醒，用户盘后才发现
    漏买。此任务在开仓窗口（09:20预挂→09:30确认）结束后核查，四种结果四种动作：
      ① 今日已有买入成交 → 计划已执行，静默；
      ② 无成交但QMT有计划票的在途买单 → 排队中（买涨停排板是常态），推送知会；
      ③ 无成交/在途单但持久化POV仍活跃 → 明示POV执行中，不误报漏单；
      ④ 无成交、无在途买单且无活跃POV → 计划未执行，强告警，提示用户介入。
    纯只读检查+推送：不下单、不撤单、不改任何交易状态，任何异常不影响交易主流程。
    """
    log = logger()
    try:
        global _last_final_plan
        today_compact = today_beijing().strftime("%Y%m%d")
        plan = _last_final_plan
        if not plan or str(plan.get("action_date", "")) != today_compact:
            # daemon 刚重启、广播线程还没来得及播时内存可能为空——现场补算一次
            sd = _latest_planned_orders_signal_date()
            if sd:
                _log_decision_chain_summary(sd)
                plan = _last_final_plan
        if not plan or str(plan.get("action_date", "")) != today_compact:
            log.info("[开仓核查] 没有今日的开仓计划数据（信号文件缺失或执行日不是今天），跳过核查。")
            return
        fb = plan.get("final_buy")
        if not fb:
            log.info("[开仓核查] 今日本来就无开仓计划，无需核查。")
            return
        if str(plan.get("equity_curve_stop_reason", "") or ""):
            log.info("[开仓核查] 今日方案甲停手，候选按规则不执行，无需核查：%s", plan.get("equity_curve_stop_reason"))
            return
        # 实际成交优先于播报缓存：认证可能在08:50播报后、09:20执行前完成更新。
        # 若券商成交已登记，绝不能拿旧的execution_block_reason误报成“未执行”。
        if has_position_bought_today("ACE"):
            detail = _open_plan_execution_detail(plan)
            log.info("[开仓核查] ✅ 今日已有买入成交，开仓计划已执行（%s）。", detail)
            return
        buy_gate_ok, buy_gate_reason = _new_buy_execution_gate()
        execution_block_reason = "" if buy_gate_ok else buy_gate_reason
        if execution_block_reason:
            message = (
                f"策略{fb.get('strategy','')} {fb.get('ts_code','')} {fb.get('name','')}有正式候选，"
                f"但新增BUY安全门禁未通过：{execution_block_reason}。"
                "因此零成交、零在途单是预期的fail-closed结果，不是POV丢单；"
                "请勿手动绕过，先完成并冻结当前组合发布认证。"
            )
            _notify(
                "buy_result",
                "⛔ 开仓按安全门禁未执行",
                message,
                level="critical",
            )
            log.error("[开仓核查] %s", message)
            return
        detail = _open_plan_execution_detail(plan)
        if _pov_active_today():
            _notify(
                "buy_result",
                "⏳ POV开仓执行中",
                f"{detail}。09:35核查：持久化POV任务仍在运行，将继续按真实成交额和"
                "+2%追价保护执行至10:30；当前不判定为漏单。",
                level="active",
            )
            log.info("[开仓核查] ⏳ %s：POV任务仍在执行，不触发漏单告警。", detail)
            return
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))):
            log.info("[开仓核查] 非实盘模式，跳过核查。")
            return
        # 查在途买单：买涨停一字板排队到尾盘是常态，排队中≠未执行，不能误报
        pending_buy = False
        try:
            with _qmt_lock:
                adapter = _qmt_get(config.get("broker", {}))
                orders = adapter.query_orders() or []
            plan_aliases = _ts_code_aliases(fb.get("ts_code", ""))
            TERMINAL_STATUS = {53, 54, 56, 57}   # 部撤/已撤/已成/废单（与14:40撤买单同口径）
            for o in orders:
                if not isinstance(o, dict):
                    o = getattr(o, "__dict__", {}) or {}
                def _pick(keys, default=""):
                    for k in keys:
                        if k in o and o[k] not in (None, ""):
                            return o[k]
                    return default
                try:
                    otype = int(_pick(["order_type", "m_nOrderType", "order_side", "m_nDirection"], -1))
                    status = int(_pick(["order_status", "m_nOrderStatus", "status"], -1))
                except (TypeError, ValueError):
                    continue
                code = str(_pick(["stock_code", "m_strInstrumentID", "instrument_id", "ts_code"], "")).strip()
                if otype == 23 and status not in TERMINAL_STATUS and (_ts_code_aliases(code) & plan_aliases):
                    pending_buy = True
                    break
        except Exception as e:
            log.warning("[开仓核查] 查询在途委托失败（%s），按无在途单继续告警——宁可多提醒一次，不可无声漏过。", e)
        if pending_buy:
            _notify("buy_result", "⏳ 开仓计划排队中",
                    f"{detail}。买单已挂出、尚未成交（买涨停排队是常态），继续排队，14:40仍未成交会自动撤单。",
                    level="active")
            log.info("[开仓核查] ⏳ %s：买单在途排队中，已推送知会。", detail)
        else:
            _notify("buy_result", "⚠️ 开仓计划未执行!",
                    f"{detail}。09:35核查：既无成交、也无在途买单——计划可能被阻断或程序异常，"
                    f"请立即查看daemon日志；如决定手动买入，买入后需告知系统登记持仓（否则不会自动平仓）。",
                    level="timeSensitive")
            log.warning("[开仓核查] ⚠️ %s：既无成交也无在途买单，开仓计划未执行，已强告警。", detail)
    except Exception as e:
        log.warning("开仓核查异常（不影响交易）：%s", e)


def _decision_chain_broadcast_loop() -> None:
    """每30分钟重播一次开仓决策链（后台线程），方便随时查看明日计划。

    只在决策结果已产出（存在 planned_orders 信号文件）且执行日未过期时重播：
    执行日==今天盘中也播（标签自动切换为"今日"），执行日已过则静默等新信号。
    启动后立即播第一次（覆盖"缓存命中不跑流水线"的启动路径），再进入30分钟循环。
    纯展示线程，任何异常不影响交易主流程。
    """
    while True:
        try:
            sd = _latest_planned_orders_signal_date()
            if sd:
                sd_date = datetime.datetime.strptime(sd, "%Y%m%d").date()
                action_date = next_n_trade_days(sd_date, 1).strftime("%Y%m%d")
                if action_date >= today_beijing().strftime("%Y%m%d"):
                    _log_decision_chain_summary(sd)
        except Exception as exc:
            logger().debug("决策链周期播报异常：%s", exc)
        time.sleep(1800)


def _load_config_section(section: str) -> dict[str, Any]:
    """读取 config.json 的某个顶层段；失败返回空字典（播报用，不参与下单）。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        value = config.get(section, {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _load_ab_checklist(signal_date: str):
    try:
        import pandas as pd

        pattern = str(PROJECT_ROOT / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_checklist.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            return pd.DataFrame()
        return pd.read_csv(files[-1], low_memory=False)
    except Exception:
        return pd.DataFrame()


# 与历史回测 limit_list_d/full 口径一致的涨停池来源。qmt_native：2026-10-23 券商禁止 QMT 内置 Python
# 联网后，按 limit_list_d 规则从 QMT 行情重建的涨停池（上线前逐日核对与 limit_list_d 一致）。
LIMIT_SOURCES_MATCHING_BACKTEST = ("limit_list_d", "qmt_native")


def _limit_sources_match_backtest(values) -> bool:
    values = [str(value) for value in values]
    return bool(values) and all(value in LIMIT_SOURCES_MATCHING_BACKTEST for value in values)


def report_signal_readiness_summary(signal_date: str) -> None:
    """启动/收盘后播报数据口径、字段完整性和 A/C 筛选结果。"""
    try:
        import pandas as pd

        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        strategy_cfg = load_json_config(PROJECT_ROOT / "config" / "strategy_config.json")
        fill_path = _prefer_live_processed_path(
            "live_limit_up_fill_scored.csv",
            cfg.get("fill_model", {}).get("output_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"),
        )
        requirements = (
            strategy_cfg.get("paper_ab_filtered_strategy", {})
            .get("data_quality_requirements", {})
        )
        required_columns = [str(column) for column in requirements.get("required_columns", [])]

        logger().info("----- 信号就绪审计：%s -----", signal_date)
        _log_market_environment(signal_date)
        if not fill_path.exists():
            logger().warning("  数据口径：❌ 成交概率打标文件不存在：%s", fill_path)
            return

        scored = pd.read_csv(fill_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        daily = scored[scored["trade_date"].astype(str) == str(signal_date)].copy()
        if daily.empty:
            logger().warning("  数据口径：❌ %s 没有 %s 记录", fill_path.name, signal_date)
            return

        source_counts = _value_counts_text(daily, "limit_data_source")
        quality_counts = _value_counts_text(daily, "limit_data_quality")
        compatible_counts = _value_counts_text(daily, "strategy_compatible")
        missing_columns = [column for column in required_columns if column not in daily.columns]
        empty_columns = [
            column
            for column in required_columns
            if column in daily.columns and daily[column].isna().all()
        ]
        is_full = (
            "limit_data_quality" in daily.columns
            and daily["limit_data_quality"].fillna("").astype(str).eq("full").all()
        )
        is_limit_list_d = (
            "limit_data_source" in daily.columns
            and _limit_sources_match_backtest(daily["limit_data_source"].fillna("").astype(str))
        )
        is_compatible = (
            "strategy_compatible" in daily.columns
            and daily["strategy_compatible"].fillna("").astype(str).str.lower().isin({"true", "1"}).all()
        )
        fields_ok = not missing_columns and not empty_columns

        logger().info(
            "  数据口径：%s  rows=%d  source={%s} quality={%s} compatible={%s}",
            "✅ 通过，和历史回测 limit_list_d/full 口径一致" if (is_full and is_limit_list_d and is_compatible and fields_ok) else "❌ 不通过",
            len(daily),
            source_counts,
            quality_counts,
            compatible_counts,
        )
        logger().info(
            "  必需字段：%s  缺失=%s  全空=%s",
            "✅ 齐全" if fields_ok else "❌ 不齐",
            missing_columns or "无",
            empty_columns or "无",
        )
        logger().info(
            "  成交概率：allow_buy_reliable={%s}  is_fill_score_reliable={%s}",
            _value_counts_text(daily, "allow_buy_reliable"),
            _value_counts_text(daily, "is_fill_score_reliable"),
        )
        _log_abc_filter_funnel(signal_date)

        checklist = _load_ab_checklist(signal_date)
        if checklist.empty:
            logger().warning("  A/C：⚠️ 未找到 %s checklist，可能尚未生成每日操作台", signal_date)
            return
        row = checklist.iloc[0]
        logger().info(
            "  A/C状态：%s（%s）",
            row.get("operation_status", ""),
            row.get("operation_status_desc", "无中文解释，请重跑每日操作台"),
        )
        logger().info(
            "  筛选状态：%s（%s）",
            row.get("selection_status", ""),
            row.get("selection_status_desc", "无中文解释，请重跑每日操作台"),
        )
        logger().info(
            "  漏斗统计：A候选%s C候选%s C过滤%s 最终选中%s 计划单%s；B已删除",
            row.get("a_candidate_count", 0),
            row.get("c_candidate_count", 0),
            row.get("c_rejected_by_filter_count", 0),
            row.get("selected_count", 0),
            row.get("planned_order_count", 0),
        )
        for label, suffix in [("C风险过滤", "c_rejected_by_filter")]:
            detail = _load_reject_detail(signal_date, suffix)
            if detail:
                logger().info("  %s：%s", label, detail)
        _log_d_status_for_signal(signal_date)
        logger().info("----- 信号就绪审计结束 -----")
    except Exception as exc:
        logger().warning("信号就绪审计失败：%s", exc)


def _log_market_environment(signal_date: str) -> None:
    try:
        import pandas as pd

        sentiment_path = _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv")
        emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        if not sentiment_path.exists():
            logger().info("  市场环境：未找到 %s", sentiment_path.name)
            return
        sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
        row_df = sentiment[sentiment["trade_date"].astype(str) == str(signal_date)]
        if row_df.empty:
            logger().info("  市场环境：%s 无 %s 记录", sentiment_path.name, signal_date)
            return
        row = row_df.iloc[0]
        emotion = pd.DataFrame()
        if emotion_path.exists():
            raw_emotion = pd.read_csv(emotion_path, dtype={"trade_date": str}, low_memory=False)
            emotion = raw_emotion[raw_emotion["trade_date"].astype(str) == str(signal_date)]
        market_row = emotion.iloc[0] if not emotion.empty else pd.Series(dtype=object)
        logger().info(
            "  市场环境：market_sentiment=%s  全市场涨停=%s  跌停=%s  连板数=%s  最高板=%s  一字板=%s  炸板/开板=%s",
            row.get("market_sentiment_level", "unknown"),
            row.get("limit_up_count", "NA"),
            market_row.get("market_limit_down_count", "NA"),
            market_row.get("market_chain_count", row.get("limit_up_max_height", "NA")),
            row.get("limit_up_max_height", "NA"),
            row.get("one_word_limit_count", "NA"),
            row.get("opened_limit_count", "NA"),
        )
        logger().info(
            "  分段热度：沪主板%s只/%s，深主板%s只/%s，创业板%s只/%s，科创板%s只/%s，北交所%s只/%s",
            row.get("sh_main_limit_up_count", "NA"),
            row.get("sh_main_market_sentiment_level", "NA"),
            row.get("sz_main_limit_up_count", "NA"),
            row.get("sz_main_market_sentiment_level", "NA"),
            row.get("chi_next_limit_up_count", "NA"),
            row.get("chi_next_market_sentiment_level", "NA"),
            row.get("star_limit_up_count", "NA"),
            row.get("star_market_sentiment_level", "NA"),
            row.get("bj_limit_up_count", "NA"),
            row.get("bj_market_sentiment_level", "NA"),
        )
        logger().info(
            "  环境结论：市场不是数据阻断项；是否开仓由 A/C 条件和风险过滤继续决定，B已删除。D 盘中策略还会在交易时段单独看实时首板/炸板/情绪。"
        )
    except Exception as exc:
        logger().info("  市场环境读取失败：%s", exc)


def _log_abc_filter_funnel(signal_date: str) -> None:
    try:
        import pandas as pd

        from src.ac_filter_trace import filter_trace, stop_point_text
        from src.ac_rules import (
            condition_strategy_config,
            condition_text,
            configured_c_condition_profiles,
            configured_c_conditions,
        )
        from src.paper_candidate_generator import PaperCandidateGenerator

        strategy_path = PROJECT_ROOT / "config" / "strategy_config.json"
        strategy_cfg = load_json_config(strategy_path)
        live_fill_scored_path = _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv")
        live_market_emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        live_theme_heat_path = _prefer_live_processed_path("live_theme_heat_features.csv", "theme_heat_features.csv")
        generator_kwargs = {
            "input_trades_path": live_fill_scored_path,
            "market_emotion_features_path": live_market_emotion_path,
            "theme_heat_features_path": live_theme_heat_path,
        }
        base_generator = PaperCandidateGenerator(strategy_path, **generator_kwargs)
        all_candidates = base_generator.load_all_candidates()

        traces = [filter_trace("A主策略", base_generator, all_candidates, signal_date)]

        c_conditions = configured_c_conditions(strategy_cfg)
        c_profiles = configured_c_condition_profiles(strategy_cfg)
        if c_conditions or c_profiles:
            c_config = condition_strategy_config(
                strategy_cfg,
                c_conditions,
                "backup_strategy_c_current",
                condition_profiles=c_profiles,
            )
            c_generator = PaperCandidateGenerator(strategy_path, **generator_kwargs)
            c_generator.config = c_config
            c_generator.paper_config = c_config.get("paper_candidate", {})
            c_generator.risk_thresholds = c_generator.paper_config.get("risk_thresholds", {})
            c_rule_text = condition_text(c_profiles or c_conditions)
            traces.append(filter_trace(f"C补位策略（{c_rule_text}）", c_generator, all_candidates, signal_date))

        trace = pd.concat(traces, ignore_index=True)
        logger().info("  A/C逐层筛选漏斗（B已删除）：")
        for _, row in trace.iterrows():
            logger().info(
                "    %s | %s | 当日 %s -> %s，剔除 %s | %s",
                row.get("strategy_layer", ""),
                row.get("step", ""),
                row.get("signal_date_before", 0),
                row.get("signal_date_after", 0),
                row.get("removed_on_signal_date", 0),
                row.get("description", ""),
            )
            if str(row.get("reason_detail", "")):
                logger().info("      未进入/停留原因：%s", row.get("reason_detail", ""))
        logger().info("  A/C停止点：")
        for layer in trace["strategy_layer"].dropna().astype(str).drop_duplicates().tolist():
            logger().info("    %s", stop_point_text(trace, layer))
    except Exception as exc:
        logger().info("  A/C逐层筛选漏斗生成失败：%s", exc)


def _log_d_status_for_signal(signal_date: str) -> None:
    try:
        now = now_beijing()
        checklist = _load_ab_checklist(signal_date)
        planned_count = 0
        if not checklist.empty and "planned_order_count" in checklist.columns:
            planned_count = int(float(checklist["planned_order_count"].iloc[0] or 0))
        in_d_start_window = datetime.time(9, 20) <= now.time() <= datetime.time(14, 55)
        d_running = _strategy_d_monitor_running()
        open_positions = [
            p for p in load_positions()
            if str(p.get("status", "")).lower() in {"open", "sell_pending"}
        ]
        current_plan = _last_final_plan or {}
        formal_candidate = None
        if str(current_plan.get("signal_date", "")) == str(signal_date):
            formal_candidate = current_plan.get("final_buy")
        if open_positions:
            held = _describe_holdings(open_positions, with_quote=False)
            logger().info(
                "  D策略停止点：已有策略持仓。当前持仓=%s；串行单仓规则优先于其他策略候选判断，"
                "不启动新的D盘中监控。",
                held,
            )
        elif d_running:
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            allowed_segments = config.get("strategy_d", {}).get("allowed_market_segments", [])
            allowed_text = ",".join(str(x) for x in allowed_segments) if isinstance(allowed_segments, list) else "未配置"
            if formal_candidate or planned_count > 0:
                reason = (
                    "检测到非D正式候选/开仓计划但D线程仍在运行；运行中全活动门禁会在"
                    "下一轮停止线程，不再继续扫描、记录或下单。"
                )
            else:
                reason = "D盘中监控线程正在运行；账户空仓且今日没有非D正式候选/开仓计划。"
            logger().info("  D策略状态：RUNNING（%s）", reason)
            logger().info("  D实盘扫描范围：%s；日志中的扫描数量为D当前配置股票池数量。", allowed_text)
        elif formal_candidate:
            logger().info(
                "  D策略停止点：组合状态机。原因：今日已有%s正式候选 %s %s，"
                "按全日互斥规则D不启动、不扫描、不记录、不下单。",
                formal_candidate.get("strategy", ""),
                formal_candidate.get("ts_code", ""),
                formal_candidate.get("name", ""),
            )
        elif planned_count > 0:
            logger().info(
                "  D策略停止点：今日已有非D正式候选/开仓计划；"
                "按全日互斥规则D不启动、不扫描、不记录、不下单。"
            )
        elif not in_d_start_window:
            logger().info(
                "  D策略停止点：交易时段。原因：当前不是 D 盘中监控时段。D 只在交易日 09:20 组合状态机允许后启动，09:30开始完整跟踪，09:35起WATCH，14:00后真实回封才允许BUY，14:56停止/撤单。"
            )
            logger().info(
                "  D策略后续过滤链：组合状态机允许 -> 09:30起完整实时路径 -> 首板且昨日未涨停 -> 当前封涨停 -> 炸板2~3次(multi_open) -> 首封不早于10:00 -> 当前封板88~132只(strong代理，不含very_strong) -> 14:00后真实回封 -> 成交概率>=80%且可靠 -> 按实时封单金额/流通市值(fd_amount_to_circ_mv)排序 -> LiveOrderGateway二次风控。"
            )
            logger().info("  D策略明日判断：若09:20账户空仓且A/C/E均无正式候选，才允许启动D盘中监控；能否下单取决于盘中实时过滤。")
        else:
            logger().info("  D策略停止点：组合状态机或实时扫描。当前处于 D 可启动/监控时段，实际是否启动以组合状态机决策明细为准；若已启动，还要看盘中实时基础过滤。")
    except Exception as exc:
        logger().info("  D策略状态读取失败：%s", exc)


def _value_counts_text(data, column: str) -> str:
    if data.empty or column not in data.columns:
        return "无"
    counts = data[column].fillna("").astype(str).value_counts().head(8).to_dict()
    return ", ".join(f"{key or '空'}={value}" for key, value in counts.items())


def _load_reject_detail(signal_date: str, suffix: str) -> str:
    try:
        import pandas as pd

        pattern = str(PROJECT_ROOT / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_{suffix}.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            return ""
        data = pd.read_csv(files[-1], dtype=str, low_memory=False)
        if data.empty:
            return ""
        row = data.iloc[0]
        code = str(row.get("ts_code", ""))
        name = str(row.get("name", ""))
        reason = str(row.get("reject_reason_desc", row.get("reject_reason", "")))
        detail = str(row.get("risk_reject_detail", ""))
        return f"{code} {name}；{reason}；{detail}"
    except Exception:
        return ""
