"""收盘流水线：后台运行各步骤、重试到23:00、净值快照与告警、停手判定新鲜度、离线数据维护。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import glob
import json
import threading
import time
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import now_beijing, today_beijing
from src.strategy_equity_ledger import equity_ledger_requires_bootstrap, update_strategy_equity_ledger
from src.daemon.common import (
    PROJECT_ROOT,
    TIMEOUT_DATA_STEP,
    TIMEOUT_EQUITY_CURVE_STOP_STEP,
    TIMEOUT_SIGNAL_STEP,
    _fmt_wan,
    _mask_account,
    _notify,
    _notify_once_per,
    logger,
    prev_n_trade_days,
    run_script,
)
from src.daemon.state import load_positions
from src.daemon.plans import (
    _checklist_data_quality_blocked,
    _date_in_scored,
    _expected_signal_date,
    _has_signal_for_date,
)
from src.daemon.qmt import _qmt_get, _qmt_lock
from src.daemon.reports import (
    _log_abc_strategy_brief,
    _log_cleaning_brief,
    _log_close_pipeline_candidate_summary,
    _log_collection_brief,
    _log_decision_chain_summary,
    _log_dynamic_feature_brief,
    _log_e_signal_status,
    _log_enhanced_feature_brief,
    _log_fill_score_brief,
    _processed_data_ready_for_date,
    push_open_plan_notification,
    report_next_day_candidates,
    report_next_trade_factor_readiness,
    report_signal_readiness_summary,
)


def post_market_marker_path(date: datetime.date) -> Path:
    return PROJECT_ROOT / "logs" / f"post_market_done_{date.strftime('%Y%m%d')}.marker"


def mark_post_market_done(date: datetime.date) -> None:
    path = post_market_marker_path(date)
    mkdir_p(path.parent)
    path.write_text(now_beijing().isoformat(), encoding="utf-8")


def has_post_market_run_today(date: datetime.date) -> bool:
    """判断今日收盘流水线是否成功产出信号文件。
    以 planned_orders 文件名中的日期为唯一判据——marker 文件在流水线失败时也会写入，不可信。
    """
    import re as _re
    today_str = date.strftime("%Y%m%d")
    pattern = str(PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops" / "*_planned_orders.csv")
    for f in glob.glob(pattern):
        m = _re.search(r"\d{8}", Path(f).stem)
        if m and m.group() == today_str:
            return True
    return False


# ── subprocess 执行（带超时）──────────────────────────────────────────────────

_pipeline_thread: threading.Thread | None = None
_pipeline_thread_lock = threading.Lock()


def _start_post_market_pipeline(end_date: str | None = None, *, reason: str = "",
                                offline: bool = False, stop_event: threading.Event | None = None) -> bool:
    """后台启动收盘/补采流水线。主调度继续运行；流水线遇到交易暂停门禁会等待。"""
    global _pipeline_thread
    with _pipeline_thread_lock:
        if _pipeline_thread is not None and _pipeline_thread.is_alive():
            logger().warning("收盘/采集流水线已在运行，本次不重复启动：%s", reason or "未指定原因")
            return False

        def _target() -> None:
            try:
                if offline:
                    _run_post_market_with_retry(end_date, offline=True, stop_event=stop_event)
                else:
                    _run_post_market_with_retry(end_date)
            finally:
                logger().info("收盘/采集流水线线程结束：%s", reason or "正常结束")

        _pipeline_thread = threading.Thread(target=_target, daemon=True, name="post-market-pipeline")
        _pipeline_thread.start()
        logger().info("收盘/采集流水线已后台启动：%s", reason or "未指定原因")
        return True


def _log_post_market_step_brief(script: str, signal_date: str) -> None:
    if script == "collect_all_data.py":
        _log_collection_brief(signal_date)
    elif script == "clean_collected_data.py":
        _log_cleaning_brief(signal_date)
    elif script == "build_dynamic_features.py":
        _log_dynamic_feature_brief(signal_date)
    elif script == "score_limit_up_fill_probability.py":
        _log_fill_score_brief(signal_date)
    elif script == "build_live_enhanced_features.py":
        _log_enhanced_feature_brief(signal_date)
    elif script == "run_paper_ab_filtered_daily_ops.py":
        _log_abc_strategy_brief(signal_date)
    elif script == "run_strategy_e_signal.py":
        _log_e_signal_status(signal_date)


def snapshot_realized_strategy_equity(signal_date: str, *, allow_broker_bootstrap: bool = True) -> None:
    """空仓时更新全策略已实现净值；详细账务逻辑位于独立模块。"""
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        open_positions = [
            p for p in load_positions()
            if str(p.get("status", "")).lower() in {"open", "sell_pending"}
        ]
        if open_positions:
            logger().info(
                "全策略净值快照：账户有 %d 个未平仓头寸，跳过（有持仓时总资产含浮动盈亏，"
                "不符合已实现净值口径）。", len(open_positions)
            )
            return

        path = PROJECT_ROOT / "reports" / "account_risk_shadow" / "equity_ledger.json"
        bootstrap_equity: float | None = None
        if equity_ledger_requires_bootstrap(path):
            if not allow_broker_bootstrap:
                logger().warning("离线净值检查：原账本未初始化，保持未就绪；不查询券商、不新建资金基线。")
                return
            if not (cfg.get("broker_adapter_enabled") and cfg.get("qmt_enabled")):
                logger().warning("全策略净值账本尚未初始化且券商适配未启用，保持未就绪状态。")
                return
            with _qmt_lock:
                adapter = _qmt_get(cfg.get("broker", {}))
                account = adapter.query_account()
            bootstrap_equity = float(getattr(account, "total_asset", 0.0) or 0.0)
            if bootstrap_equity <= 0:
                logger().warning("全策略净值账本初始化失败：券商总资产=%s。", bootstrap_equity)
                return

        snapshot = update_strategy_equity_ledger(
            state_path=path,
            completion_summary_path=(
                PROJECT_ROOT / "reports" / "execution_tracking" / "trade_completion_summary.csv"
            ),
            signal_date=signal_date,
            config=cfg,
            bootstrap_equity=bootstrap_equity,
        )
        drawdown = (
            snapshot.equity / snapshot.peak_equity - 1.0
            if snapshot.peak_equity > 0
            else 0.0
        )
        if not getattr(snapshot, "historical_coverage_ready", True):
            logger().warning("全策略净值账本：历史覆盖未恢复，净值、峰值和回撤不可认证；保留待恢复快照。")
        else:
            logger().info(
                "全策略净值账本：净值%.2f元，峰值%.2f元，已实现盈亏%.2f元，"
                "新增完整交易%d笔，待补全%d笔，回撤%.2f%%。",
                snapshot.equity,
                snapshot.peak_equity,
                snapshot.realized_pnl,
                snapshot.new_trade_count,
                snapshot.pending_incomplete_trade_count,
                drawdown * 100,
            )
        if snapshot.initialized_now:
            logger().warning(
                "全策略净值账本已用一次券商总资产建立基线；后续只累计本系统完整平仓盈亏，"
                "入金、出金和系统外持仓不再改变策略峰值。"
            )
        if not snapshot.ledger_ready:
            logger().warning(
                "全策略净值账本未就绪，账户级风险状态保持fail-closed：%s；历史覆盖原因=%s",
                "、".join(snapshot.pending_trade_keys) or snapshot.source,
                "、".join(getattr(snapshot, "historical_coverage_reasons", ()) or ()) or "无",
            )
        _maybe_alert_ledger_incomplete(snapshot, cfg)
        _maybe_alert_strategy_drawdown(snapshot, drawdown, cfg)
    except Exception as exc:
        logger().warning("全策略净值账本更新失败：%s（账户级风险状态保持fail-closed）", exc)


def _describe_ledger_trade_key(trade_key: str) -> str:
    parts = str(trade_key).split("|")
    if len(parts) >= 3 and len(parts[0]) == 8:
        return f"{parts[0][4:6]}/{parts[0][6:8]}买入 {parts[1]}（{parts[2]}腿）"
    return str(trade_key)


def _maybe_alert_ledger_incomplete(snapshot: Any, cfg: dict[str, Any]) -> None:
    """净值账本缺成交时推送一次缺哪几笔；同一组缺口只推一次，补齐后清除。

    2026-09-30复核发现账本漏记4笔、回撤报警因此从未触发，用户毫不知情。
    缺成交时回撤只按已确认交易计算，必须让用户知道并补录券商成交记录。
    """
    section = cfg.get("drawdown_alert", {}) or {}
    if not bool(section.get("enabled", False)):
        return
    state_path = PROJECT_ROOT / str(
        section.get("ledger_incomplete_state_path", "data/state/equity_ledger_pending_alert.json")
    )
    pending = sorted(str(key) for key in getattr(snapshot, "pending_trade_keys", ()) or ())
    history_missing = not bool(getattr(snapshot, "historical_coverage_ready", True))
    history_reasons = sorted(getattr(snapshot, "historical_coverage_reasons", ()) or ())
    missing_previous = sorted(getattr(snapshot, "missing_previously_included_trade_keys", ()) or ())
    missing_observed = sorted(getattr(snapshot, "missing_previously_observed_trade_keys", ()) or ())
    if not pending and not history_missing:
        if state_path.exists():
            try:
                state_path.unlink()
            except OSError:
                pass  # 可以忽略：状态文件删不掉下次再删，不影响告警判断
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    if (sorted(state.get("alerted_pending_trade_keys", [])) == pending
            and bool(state.get("alerted_history_missing", False)) == history_missing
            and sorted(state.get("alerted_history_coverage_reasons", [])) == history_reasons
            and sorted(state.get("alerted_missing_previously_included_trade_keys", [])) == missing_previous
            and sorted(state.get("alerted_missing_previously_observed_trade_keys", [])) == missing_observed):
        return
    if history_missing:
        reason_text = {
            "BASELINE_NEWER_THAN_CONFIGURED_LEGACY_CUTOFF": "当前基线晚于旧系统基线，旧净值与成交未迁回",
            "PREVIOUSLY_INCLUDED_TRADES_NOT_COMPLETE": "以前已计入的真实成交现在缺失或不完整",
            "PREVIOUSLY_OBSERVED_TRADES_MISSING": "以前已发现的待补成交现在整行丢失",
        }
        body = "历史成交覆盖未恢复：" + "；".join(reason_text.get(x, x) for x in history_reasons)
        if missing_previous:
            body += "。缺失：" + "；".join(_describe_ledger_trade_key(x) for x in missing_previous)
        if missing_observed:
            body += "。丢失待补成交：" + "；".join(_describe_ledger_trade_key(x) for x in missing_observed)
        body += "。待补全0笔或当前回撤0%都不能证明历史完整；需要恢复原账本与完整券商成交后再认证回撤。"
        title = "⚠️ 历史净值账本未恢复，回撤暂不可认证"
    else:
        body = (
            f"基线日后有{len(pending)}笔交易缺真实卖出成交："
            + "；".join(_describe_ledger_trade_key(key) for key in pending)
            + "。回撤报警目前只按已确认的交易计算，实际回撤可能更大。"
            "请把这些股票的券商成交记录截图发给Claude补录。"
        )
        title = "⚠️ 净值账本缺成交，回撤报警暂时不准"
    if _notify("risk_drawdown", title, body, level="timeSensitive"):
        mkdir_p(state_path.parent)
        state_path.write_text(
            json.dumps(
                {
                    "alerted_pending_trade_keys": pending,
                    "alerted_history_missing": history_missing,
                    "alerted_history_coverage_reasons": history_reasons,
                    "alerted_missing_previously_included_trade_keys": missing_previous,
                    "alerted_missing_previously_observed_trade_keys": missing_observed,
                    "alerted_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _maybe_alert_strategy_drawdown(snapshot: Any, drawdown: float, cfg: dict[str, Any]) -> None:
    """全策略净值从高点回撤达到阈值时推送一次，提醒用户发起"甲·临时复核"。

    净值来自全策略净值账本（券商总资产一次基线+本系统完整平仓盈亏），入金、出金
    不影响回撤。同一个峰值只推送一次；创出新高后重新计数。只提醒，不改变任何下单。
    """

    section = cfg.get("drawdown_alert", {}) or {}
    if not bool(section.get("enabled", False)):
        return
    # 缺少历史基线时数值不可认证；仅有待补成交时保留原有带缺口说明的报警。
    if not bool(getattr(snapshot, "historical_coverage_ready", True)):
        return
    threshold = float(section.get("threshold", -0.30))
    if drawdown > threshold:
        return
    state_path = PROJECT_ROOT / str(section.get("state_path", "data/state/drawdown_alert.json"))
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    peak = float(snapshot.peak_equity)
    if abs(float(state.get("alerted_peak_equity", -1.0)) - peak) < 0.01:
        return
    pending = int(getattr(snapshot, "pending_incomplete_trade_count", 0) or 0)
    body = (
        f"全策略净值{snapshot.equity:.0f}元，较峰值{peak:.0f}元回撤{drawdown:.1%}，"
        f"超过{abs(threshold):.0%}报警线。请在Claude发送：甲·临时复核，原因：实盘回撤超过{abs(threshold):.0%}。"
    )
    if pending:
        body += f"（账本另有{pending}笔成交待补全，实际回撤可能更大）"
    sent = _notify("risk_drawdown", f"⚠️ 实盘回撤{drawdown:.0%}：请发起甲·临时复核", body, level="timeSensitive")
    if sent:
        mkdir_p(state_path.parent)
        state_path.write_text(
            json.dumps(
                {
                    "alerted_peak_equity": peak,
                    "alerted_equity": float(snapshot.equity),
                    "alerted_drawdown": float(drawdown),
                    "alerted_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def job_post_market(end_date: str | None = None, *, offline: bool = False) -> bool:
    target_str = end_date or today_beijing().strftime("%Y%m%d")
    target_date = datetime.datetime.strptime(target_str, "%Y%m%d").date()
    logger().info("===== 收盘流水线（目标日期 %s）=====", target_str)

    snapshot_realized_strategy_equity(target_str, allow_broker_bootstrap=not offline)

    cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
    live_window_days = max(1, int(cfg.get("cleaning", {}).get("live_signal_window_trade_days", 3)))
    # 实盘只维护策略判断所需的最近N个交易日缓存。
    # 1天策略从窗口里读目标日；市场状态需要前两日上下文，所以默认N=3。
    # 已有 raw 缓存由 collect_all_data.py 自动跳过，缺哪天才补哪天；回测研究大表不在这里更新。
    recent_start_date = prev_n_trade_days(target_date, live_window_days - 1) if live_window_days > 1 else target_date
    recent_start = recent_start_date.strftime("%Y%m%d")
    live_limit_up_path = "data/processed/live_limit_up_merged.csv"
    live_market_sentiment_path = "data/processed/live_market_sentiment.csv"
    live_market_emotion_path = "data/processed/live_market_emotion_features.csv"
    live_theme_heat_path = "data/processed/live_theme_heat_features.csv"
    live_fill_scored_path = "data/processed/live_limit_up_fill_scored.csv"

    steps = [
        ("collect_all_data.py",               "① 采集日线 + 涨停池",             TIMEOUT_DATA_STEP,  "约1分钟"),
        ("clean_collected_data.py",            "② 清洗合并数据",                   TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_dynamic_features.py",          "③ 市场情绪 / 题材热度",            TIMEOUT_DATA_STEP,  "约1分钟"),
        ("score_limit_up_fill_probability.py", "④ 涨停成交概率打分",               TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_live_enhanced_features.py",    "⑤ 增强因子生成（资金流/龙虎榜/竞价审计）", TIMEOUT_DATA_STEP, "约1分钟"),
        ("run_paper_ab_filtered_daily_ops.py", "⑥ A/C 信号生成（B已删除）",       TIMEOUT_SIGNAL_STEP,"约1分钟"),
        ("run_strategy_e_signal.py",          "⑦ E 信号生成（板块neutral+换手率降序）", TIMEOUT_SIGNAL_STEP,"约30秒"),
        ("run_strategy_m_signal.py",          "⑦M M 信号生成（20cm强势涨停接力）", TIMEOUT_SIGNAL_STEP,"约30秒"),
        ("strategy_health_monitor.py",         "⑧ 策略健康度监控（滚动20笔期望分位）", TIMEOUT_DATA_STEP, "约10秒"),
        ("live_execution_audit.py",            "⑨ 实盘执行对账（逐笔损耗vs回测基准）", TIMEOUT_DATA_STEP, "约5秒"),
        ("update_execution_completion.py",     "⑩ 真实成交完成率汇总（逐片+一笔一行）", TIMEOUT_DATA_STEP, "约5秒"),
        ("report_rolling_live_performance.py", "⑪ 真实收益/容量/TCA滚动报告（只监控）", TIMEOUT_DATA_STEP, "约5秒"),
        ("update_release_oos_observation.py", "⑫ 全策略影子候选/反事实收益（只观察）", TIMEOUT_DATA_STEP, "约5秒"),
        # 必须在最终开仓计划播报之前：播报与次日组合状态机读取同一份停手判定。
        # 失败不停止流水线；次日判定缺失时组合状态机按fail-closed不开新仓。
        ("update_equity_curve_stop.py", "⑬ 方案甲停手门禁（影子净值→次日判定）", TIMEOUT_EQUITY_CURVE_STOP_STEP, "约5分钟"),
    ]
    extra_args: dict[str, list[str]] = {
        "collect_all_data.py": ["--start-date", recent_start, "--end-date", target_str, "--require-end-date-limit"],
        "clean_collected_data.py": ["--start-date", recent_start, "--end-date", target_str, "--incremental-replace"],
        "build_dynamic_features.py": [
            "--start-date", target_str,
            "--end-date", target_str,
            "--limit-up-path", live_limit_up_path,
            "--market-emotion-output", live_market_emotion_path,
            "--theme-heat-output", live_theme_heat_path,
        ],
        "score_limit_up_fill_probability.py": [
            "--input-path", live_limit_up_path,
            "--output-path", live_fill_scored_path,
            "--market-sentiment-path", live_market_sentiment_path,
        ],
        "build_live_enhanced_features.py": [
            "--trade-date", target_str,
            "--input-path", live_fill_scored_path,
            "--max-trade-days", "10",
        ],
        "run_paper_ab_filtered_daily_ops.py": [
            "--signal-date", target_str,
            "--top-n", "10",
            "--input-trades-path", live_fill_scored_path,
            "--fill-scored-path", live_fill_scored_path,
            "--market-emotion-features-path", live_market_emotion_path,
            "--theme-heat-features-path", live_theme_heat_path,
        ],
        "run_strategy_e_signal.py": ["--signal-date", target_str],
        "run_strategy_m_signal.py": ["--signal-date", target_str],
        "update_release_oos_observation.py": ["--signal-date", target_str],
        "update_equity_curve_stop.py": ["--signal-date", target_str],
    }
    critical_scripts = {
        "collect_all_data.py",
        "clean_collected_data.py",
        "score_limit_up_fill_probability.py",
    }
    # 信号生成步骤：失败时本趟内立即用已缓存数据就地重试一次（不阻断后续、不停止流水线）。
    # A/C 在慢 IO 夜晚易被单步超时误杀；此时 raw/清洗数据已缓存，重试仅剩加载+选股，命中率高。
    signal_retry_scripts = {
        "run_paper_ab_filtered_daily_ops.py",
        "run_strategy_e_signal.py",
        "run_strategy_m_signal.py",
        # ⑬既写次日停手判定，也做每日临时复核触发检查；超时/失败就地重试一次。
        "update_equity_curve_stop.py",
    }

    failed_steps: list[str] = []
    total_steps = len(steps)
    for step_index, (script, desc, timeout, eta) in enumerate(steps, 1):
        try:
            logger().info(
                "收盘流水线进度：%d/%d %s（预计%s，单步超时上限%d秒）",
                step_index,
                total_steps,
                desc,
                eta,
                timeout,
            )
            args = extra_args.get(script, [])
            ok = run_script(script, *args, timeout=timeout)
            if not ok:
                if script in critical_scripts:
                    logger().warning("收盘流水线进度：%d/%d %s 第一次失败，等待10秒后自动重试一次", step_index, total_steps, desc)
                    time.sleep(10)
                    ok = run_script(script, *args, timeout=timeout)
                elif script in signal_retry_scripts:
                    logger().warning("收盘流水线进度：%d/%d %s 第一次失败（不阻断后续步骤），等待10秒后就地重试一次", step_index, total_steps, desc)
                    time.sleep(10)
                    ok = run_script(script, *args, timeout=timeout)
                if not ok and script in critical_scripts:
                    logger().error("❌ %s 仍然失败，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc)
                    return False
                if not ok:
                    logger().error("%s 失败，继续后续步骤", desc)
                    failed_steps.append(desc)
                    _log_post_market_step_brief(script, target_str)
                    continue
            logger().info("收盘流水线进度：%d/%d %s 完成", step_index, total_steps, desc)
            _log_post_market_step_brief(script, target_str)
        except Exception as e:
            if script in critical_scripts:
                logger().error("❌ %s 异常：%s，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc, e)
                return False
            logger().error("%s 异常：%s，继续后续步骤", desc, e)
            failed_steps.append(desc)

    _alert_if_stop_decision_stale(target_str, failed_steps)

    # 所有策略步骤结束后只读当日产物，明确区分“候选0只”和“产物未生成/未知”。
    # D是盘中策略，这里只统计其已有BUY/WATCH记录，不在收盘时补跑D。
    _log_close_pipeline_candidate_summary(target_str)

    # 关键检查：今日数据是否真的从 Tushare 入库了
    if not _date_in_scored(target_date):
        logger().warning(
            "⚠️ Tushare %s 数据尚未就绪（live_limit_up_fill_scored.csv 无该日记录），流水线步骤已完成但需等数据",
            target_str,
        )
        if not offline:
            report_next_day_candidates()
        return False  # 通知调用方稍后重试

    if not _has_signal_for_date(target_date):
        logger().warning(
            "A/C 未生成 %s 计划单，自动使用当日涨停池生成安全模拟观察计划...",
            target_str,
        )
        def _run_limit_pool_fallback() -> bool:
            return run_script(
                "generate_live_limit_pool_daily_ops.py",
                "--input-path",
                live_fill_scored_path,
                "--signal-date",
                target_str,
                "--top-n",
                "10",
                timeout=TIMEOUT_SIGNAL_STEP,
            )

        fallback_ok = _run_limit_pool_fallback()
        if not (fallback_ok and _has_signal_for_date(target_date)):
            logger().warning("兜底涨停池观察计划第一次失败，等待10秒后就地重试一次：signal_date=%s", target_str)
            time.sleep(10)
            fallback_ok = _run_limit_pool_fallback()
        if fallback_ok and _has_signal_for_date(target_date):
            logger().info("✅ 当日涨停池模拟观察计划已生成：signal_date=%s，live_order_enabled=False", target_str)
        else:
            logger().error("❌ 当日涨停池模拟观察计划生成失败：signal_date=%s", target_str)

    if _checklist_data_quality_blocked(target_date):
        logger().warning(
            "⚠️ %s 只有基础涨停口径，A/C 已阻断开仓；继续等待完整 limit_list_d，稍后自动重试",
            target_str,
        )
        if not offline:
            report_next_day_candidates()
        report_signal_readiness_summary(target_str)
        return False

    if offline:
        fresh, _ = _equity_curve_stop_decision_fresh(target_str)
        report_signal_readiness_summary(target_str)
        if failed_steps or not fresh or not report_next_trade_factor_readiness(target_str):
            logger().warning("离线数据维护未完成：失败步骤=%s，最新停手判定=%s；交易门禁保持关闭。", failed_steps, fresh)
            return False
        mark_post_market_done(target_date)
        logger().info("离线数据维护完成：%s 数据/影子停手判定已更新；账户未验证，不生成真实定仓播报，不启动交易。", target_str)
        return True

    report_next_day_candidates()
    # 决策链 summary 必须先于收盘推送：它设置 _last_final_plan（推送的唯一数据源）。
    # 原顺序 push 在前、summary 在后，push 会读到上一次旧计划；必须先生成本次
    # 串行单仓结论，再推送“82.5%开仓”或“旧仓未清空不买”的准确结果。
    _log_decision_chain_summary(target_str)
    push_open_plan_notification("收盘")   # 收盘流水线完成→推明日开仓计划(有则详细/无则明示)
    report_signal_readiness_summary(target_str)
    if not report_next_trade_factor_readiness(target_str):
        logger().warning("⚠️ 明日开盘因子尚未完全就绪，本次不标记收盘完成；5分钟后自动重试")
        return False
    logger().info("===== 收盘流水线完成 =====")
    mark_post_market_done(target_date)
    # 决策链已在推送前播报并设好 _last_final_plan；
    # 之后由 _decision_chain_broadcast_loop 每30分钟重播一次。
    try:
        open_cnt = sum(1 for p in load_positions() if p.get("status") == "open")
        acct_part = ""
        try:
            cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
            if cfg.get("broker_adapter_enabled") and cfg.get("qmt_enabled"):
                with _qmt_lock:
                    adapter = _qmt_get(cfg.get("broker", {}))
                    account = adapter.query_account()
                acct_part = (f"账户{_mask_account(account.account_id)} "
                             f"总资产{_fmt_wan(getattr(account, 'total_asset', 0.0))} ")
        except Exception as acct_exc:
            logger().warning("收盘汇总查询账户失败：%s", acct_exc)
        if failed_steps:
            _notify("daily_summary", "⚠️ 今日收盘汇总：有步骤失败",
                    f"{acct_part}持仓{open_cnt}笔，收盘流水线跑完但有{len(failed_steps)}步失败："
                    f"{'、'.join(failed_steps)}。明日计划已生成；失败步骤的产物可能是旧的，请让Claude排查。",
                    level="timeSensitive")
        else:
            _notify("daily_summary", "📊 今日收盘汇总",
                    f"{acct_part}持仓{open_cnt}笔，收盘流水线已完成，明日计划已生成。")
    except Exception as exc:
        logger().warning("收盘汇总推送异常：%s", exc)
    return True


def _equity_curve_stop_decision_fresh(target_str: str) -> tuple[bool, str]:
    """⑬是否已写出紧接着的下一个行动日的判定；未启用门禁时视为正常。"""
    from src.equity_curve_stop import load_decision, load_settings, next_open_date

    from src.post_market_freshness import decision_producer_fresh

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    settings = load_settings(config, PROJECT_ROOT)
    if not settings.enabled:
        return True, ""
    expected = next_open_date(PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv", target_str)
    payload = load_decision(settings)
    fresh = (bool(payload) and str(payload.get("signal_date", "")) == target_str
             and str(payload.get("action_date", "")) == expected
             and decision_producer_fresh(PROJECT_ROOT, target_str, config, payload))
    return fresh, expected


def _alert_if_stop_decision_stale(target_str: str, failed_steps: list[str]) -> None:
    """⑬被超时杀掉或没跑到时脚本自己发不出告警，由daemon按判定文件补报。

    判定没更新=次日按fail-closed停手，且当天的临时复核触发检查也没有执行，
    两件事都必须让用户知道，不能只写日志。
    """
    try:
        fresh, expected = _equity_curve_stop_decision_fresh(target_str)
    except Exception as exc:
        fresh, expected = False, f"未知（{exc}）"
    if fresh:
        return
    failed = "、".join(failed_steps) or "无（判定文件仍是旧的）"
    logger().error("⛔ 方案甲停手判定没有更新到%s；失败步骤：%s", expected, failed)
    # 因子未就绪时流水线会每5分钟整趟重跑；同一收盘日只推一次，避免重复轰炸。
    _notify_once_per(
        f"equity_curve_stop_decision_stale:{target_str}",
        6 * 3600,
        "⛔ 方案甲停手判定没有更新",
        f"收盘流水线没有写出{expected}的停手判定（失败步骤：{failed}）。"
        f"{expected}将按fail-closed不开新仓；今天的临时复核触发检查也没有执行。"
        "请在Claude发送：甲·临时复核（先修收盘流水线）。",
        level="timeSensitive",
    )


def _alert_post_market_not_completed(date_str: str, cutoff_hour: int) -> None:
    """收盘流水线到截止时间仍未完成：停手判定和临时复核检查可能都没跑。"""
    try:
        fresh, expected = _equity_curve_stop_decision_fresh(date_str)
    except Exception as exc:
        fresh, expected = False, f"未知（{exc}）"
    state = "已更新" if fresh else f"没有更新，{expected}将按fail-closed不开新仓"
    _notify(
        "system_error",
        f"⛔ {date_str}收盘流水线到{cutoff_hour}:00仍未完成",
        f"今天的收盘流水线没有完成（数据或因子未就绪，或中途失败）。停手判定{state}；"
        "今天的临时复核触发检查可能没有执行。请在Claude发送：甲·临时复核（先修收盘流水线）。",
        level="timeSensitive",
    )


def _run_post_market_with_retry(end_date: str | None = None, *, offline: bool = False,
                                stop_event: threading.Event | None = None) -> None:
    """运行收盘流水线，若收盘数据或明日开盘因子未就绪则每5分钟重试，直到23:00。"""
    cutoff_hour = 23
    retry_seconds = 300
    date_str = end_date or today_beijing().strftime("%Y%m%d")
    while stop_event is None or not stop_event.is_set():
        try:
            data_ready = (job_post_market(end_date=end_date, offline=True) if offline
                          else job_post_market(end_date=end_date))
        except Exception as _e:
            logger().error("收盘流水线异常：%s", _e)
            data_ready = False
        if data_ready:
            break
        now = now_beijing()
        if now.hour >= cutoff_hour:
            logger().warning("已过 %d:00，停止等待 Tushare %s 数据，今日收盘流水线结束", cutoff_hour, date_str)
            _alert_post_market_not_completed(date_str, cutoff_hour)
            break
        next_retry = now + datetime.timedelta(seconds=retry_seconds)
        logger().warning(
            "⚠️ %s 收盘数据/因子尚未就绪，%d分钟后（%s）自动重试",
            date_str,
            retry_seconds // 60,
            next_retry.strftime("%H:%M"),
        )
        if stop_event is not None:
            stop_event.wait(retry_seconds)
        else:
            time.sleep(retry_seconds)


def _offline_data_fresh(date_str: str) -> bool:
    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    fresh, _ = _equity_curve_stop_decision_fresh(date_str)
    return _has_signal_for_date(date) and _processed_data_ready_for_date(date) and fresh


def _start_offline_data_maintenance(stop: threading.Event) -> threading.Thread | None:
    from src.offline_data_maintenance import MaintenanceSettings, run_maintenance

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    settings = MaintenanceSettings.from_config(config)
    if not settings.enabled:
        return None
    worker = threading.Thread(
        target=run_maintenance,
        kwargs={
            "stop": stop, "settings": settings,
            "expected_date": lambda: _expected_signal_date().strftime("%Y%m%d"),
            "refresh_ledger": lambda date: snapshot_realized_strategy_equity(date, allow_broker_bootstrap=False),
            "data_fresh": _offline_data_fresh,
            "start_pipeline": lambda date: _start_post_market_pipeline(
                date, reason="账户门禁等待期间补齐后台数据", offline=True, stop_event=stop),
            "on_error": lambda exc: logger().error("离线数据维护异常（不绕过交易门禁）：%s", exc),
        },
        daemon=True, name="offline-data-maintenance",
    )
    worker.start()
    logger().info("账户门禁等待期间已启动仅数据/本地账本维护；交易线程仍等待账户认证及成交恢复。")
    return worker
