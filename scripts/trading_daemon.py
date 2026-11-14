"""
A_System 量化策略常驻守护进程。

安全设计原则：
1. 平仓逻辑完全独立于数据流水线 —— 数据步骤出错不影响平仓。
2. 每个操作单独 try/except，不因局部错误崩溃。
3. subprocess 设超时上限，防止某步骤挂死导致平仓被跳过。
4. 进程本身崩溃由 start.sh 的外部 watchdog 自动重启。
5. 心跳文件每分钟更新，外部可监控守护进程存活状态。

调度时间表（A 股交易日）：
    09:00  盘前计划 —— 生成/刷新组合状态机买入计划
    09:20  盘前复核 —— 平仓检查（优先） + 组合状态机复核 + 按涨停价预挂买入 + D监控
    09:23  D接力竞价 —— 仅卖虚拟匹配量可安全承接的部分，不再整仓砸开盘
    09:26  竞价确认 —— 按D真实成交额建立接力资金账
    09:30  开盘确认 —— 确认09:20预挂成交，未成交再补买
    09:30-10:30 D接力 —— 卖D确认一片后，才用该片释放资金买候选一片
    13:00  卖出容量检查 —— 普通D等收盘退出腿仅在仓位超过实时容量门槛时启动POV
    14:30/14:45 —— 按实际成交流速复核容量，小仓位继续等待14:55
    14:55  收盘平仓 —— 连续竞价合法价格笼子下限；14:56:20撤余单，14:57:05仅按安全账本真实余量进收盘竞价
    14:40  撤买单 —— 提前撤销所有未成交买单，不占用POV交接后的平仓QMT通道
    15:10  收盘  —— 数据流水线 + 信号生成

持仓状态：data/processed/positions.json
心跳文件：logs/daemon_heartbeat.txt

代码位置：2026-11-14 起按职责拆到 src/daemon/（各模块职责见 src/daemon/__init__.py），本文件是启动入口。
"""
from __future__ import annotations

import datetime
import csv
import glob
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any

# resolve() 在 Windows WebDAV 盘符（Z:\）上会展开为 UNC 路径导致文件 I/O 失败，
# 用 absolute() 保留盘符路径。Mac/Linux 上行为相同。
PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import get_logger, setup_logger
from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.execution_completion_tracker import ExecutionCompletionTracker
from src.broker_execution_service import IntentBrokerExecutionService
from src.trade_intent_store import TradeIntentStore
from src.trade_recovery import (
    RecoveryOutcome,
    TradeRecoveryCoordinator,
    normalize_broker_positions,
)
from src.strategy_equity_ledger import (
    equity_ledger_requires_bootstrap,
    update_strategy_equity_ledger,
)
from src.strategy_identity import normalize_strategy_leg, normalize_strategy_record
from src.portfolio_ace_m import (
    ACE_FULL_HARD_CAP_PCT,
    ACE_FULL_TARGET_PCT,
    GATE_BLOCKED as ACE_GATE_BLOCKED,
    GATE_REDUCED as ACE_GATE_REDUCED,
    M_LEG,
    ace_gate_mode,
    ace_limits,
    m_budget,
    position_in_family,
)
from src.strategy_d_checkpoint import (
    StrategyDCheckpointCheck,
    clear_strategy_d_intraday_checkpoints,
    inspect_strategy_d_checkpoint,
    strategy_d_checkpoint_path,
    strategy_d_machine_fingerprint,
    strategy_d_runtime_fingerprint,
)
from src.strategy_d_factor_rules import (
    ReleaseSignalClock,
    load_factor_release,
    release_signal_clock,
    release_uses_factor_union,
)
from src.strategy_d_spec import (
    D_CHECKPOINT_MAX_AGE_SECONDS,
    D_ORDER_CANCEL_HHMM,
    D_TRACKING_START_HHMM,
    intraday_history_is_complete,
)
from src.strategy_d_relay_execution import (
    calculate_auction_safe_sell_quantity,
    calculate_relay_buy_quantity,
    floor_lot as relay_floor_lot,
    relay_buy_limit_price,
)
from src.rolling_signal_store import (
    ERROR as SIGNAL_RUN_ERROR,
    NO_CANDIDATE,
    NO_SIGNAL_OCCUPIED,
    SIGNAL_READY,
    signal_run_by_signal_date,
)
import sys as _sys
import platform as _platform

# 2026-11-14 起（项目优化第4步）守护进程的代码按职责搬到 src/daemon/ 下的各模块（分层规则见
# src/daemon/__init__.py）。本文件只负责启动，并把原来所有的名字转出，QMT核心包、运维工具和测试
# 照旧用 scripts.trading_daemon.<名字>。例外：被函数改写的全局变量只能到它所在模块读最新值，
# 不在这里转出（转出的是导入那一刻的旧值）：
#   _ASSET_SANITY_ALERT_DAY（qmt）、_LAST_QMT_ERROR_TEXT（qmt）、_d_monitor_thread（strategy_d）
#   _d_relay_pair_thread（strategy_d）、_e_retry_resolution_reason（buy）、_e_retry_resolved_date（buy）
#   _e_retry_thread（buy）、_exit_bypass_day（close）、_last_account_has_position（qmt）
#   _last_final_plan（reports）、_m_pov_thread（buy_m）、_pipeline_pause_reason（common）
#   _pipeline_thread（pipeline）、_pov_thread（buy）、_premarket_buy_monitor_thread（buy）、_qmt_adapter（qmt）
#   _qmt_connect_failure_streak（qmt）、_qmt_connect_last_error（qmt）、_qmt_connect_retry_not_before（qmt）
#   _qmt_execution_service（qmt）、_qmt_last_verified_at（qmt）、_qmt_reconnect_count（qmt）
#   _qmt_transport_busy_count（qmt）、_trade_intent_store_instance（state）。
from src.daemon.common import (  # noqa: F401
    _execution_completion_tracker,
    _track_execution,
    _notify,
    _notify_async,
    _notify_with_retry_async,
    _mask_account,
    _fmt_wan,
    _execution_time_only,
    _fmt_position_time,
    _normalize_ts_code,
    _ts_code_aliases,
    SCHEDULE,
    SCHED_MORNING_PLAN_PUSH,
    SCHED_PREOPEN_PLAN,
    SCHED_MORNING_REVIEW,
    SCHED_PREMARKET_SELL,
    SCHED_PREMARKET_SYNC,
    SCHED_OPENING_BUY,
    SCHED_OPEN_PLAN_FILL_CHECK,
    SCHED_AFTERNOON_CLOSE,
    SCHED_CANCEL_BUY_ORDERS,
    SCHED_EXIT_LARGE_FORCE_START,
    SCHED_CLOSE_AUCTION_HANDOFF,
    SCHED_POST_MARKET,
    EXIT_CLOSE_SUBMIT_CLOCK_GUARD_DEFAULT_SEC,
    PYTHON,
    POSITIONS_FILE,
    PENDING_BUY_FILE,
    E_CAPACITY_FILE,
    LEGACY_E_CAPACITY_FILE,
    EXIT_EXECUTION_STATE_FILE,
    D_RELAY_PAIR_STATE_FILE,
    D_RELAY_PAIR_EXEC_LOG,
    HEARTBEAT_FILE,
    BROKER_HEALTH_FILE,
    D_MONITOR_PID_FILE,
    QMT_LAST_SUCCESS_FILE,
    CALENDAR_STALE_WARNED,
    _TRADE_CALENDAR_CACHE,
    _ACCOUNT_STATUS_DAILY_CACHE,
    _pending_buy_lock,
    _broker_health_write_lock,
    _positions_file_lock,
    _exit_execution_state_lock,
    _exit_sell_lock,
    _d_relay_pair_state_lock,
    _d_relay_pair_thread_lock,
    _EXIT_POV_REMARK_PREFIX,
    _D_RELAY_SELL_REMARK_PREFIX,
    _SYSTEM_EXIT_REMARK_PREFIXES,
    SCHED_EXIT_POV_HANDOFF,
    TIMEOUT_DATA_STEP,
    TIMEOUT_SIGNAL_STEP,
    TIMEOUT_ORDER_STEP,
    TIMEOUT_COMBINED_PLAN_STEP,
    TIMEOUT_EQUITY_CURVE_STOP_STEP,
    logger,
    _replace_runtime_file_with_retry,
    write_heartbeat,
    write_broker_health,
    _load_calendar,
    ensure_trade_calendar_fresh,
    is_trade_day,
    next_n_trade_days,
    next_trade_date_on_or_after,
    prev_n_trade_days,
    market_is_open,
    append_risk_flag,
    _floor_buy_quantity_by_amount,
    to_float,
    to_int,
    _pipeline_resume_event,
    _pipeline_paused,
    _pause_pipeline_for_trade,
    _resume_pipeline_after_trade,
    _wait_if_pipeline_paused,
    run_script,
    _sleep_until_beijing,
    _load_qmt_last_success,
    _save_qmt_last_success,
    _clear_qmt_last_success,
    EXIT_CODE_EXECUTION_STATE_RECOVERY,
    _ONCE_PER_STATE,
    _ONCE_PER_ATTEMPT,
    _PROCESS_RECOVERY_EVENT,
    _QMT_FATAL_RESTART_EVENT,
    _SOCKET_EXHAUSTED_MARKERS,
    _is_socket_resource_exhausted,
    _request_process_recovery,
    _critical_execution_state_failure,
    _notify_once_per,
)
from src.daemon.state import (  # noqa: F401
    load_positions,
    save_positions,
    _ACTIVE_AUTO_STRATEGY_LEGS,
    _position_exit_ledger_quantity,
    _is_recent_auto_ghost_clear,
    _strategy_identity_aliases,
    restore_ghost_cleared_strategy_positions,
    _recover_sell_price_from_fills,
    record_buy,
    _mark_buy_intent_position_projected,
    _mark_component_buy_intents_position_projected,
    _append_exit_fill_ledger,
    _position_exit_filled_qty_on,
    mark_position_closed,
    reduce_position_shares,
    find_open_position_by_code,
    _position_manual_exit_only,
    _empty_exit_execution_state,
    _state_backup_path,
    _write_json_state_copy,
    _load_redundant_json_state,
    _save_redundant_json_state,
    _validate_exit_execution_state,
    _load_exit_execution_state,
    _save_exit_execution_state,
    _exit_account_fingerprint,
    _d_relay_pair_load_state,
    _d_relay_pair_save_state,
    _d_relay_pair_active_today,
    POV_STATE_FILE,
    _pov_state_lock,
    _pov_load_state,
    _pov_save_state,
    _pov_active_today,
    _has_pending_buy_for_code,
    M_EXEC_STATE_PATH,
    _m_validate_state,
    _m_load_state,
    _m_save_state,
    save_pending_buys,
    load_pending_buys,
    clear_pending_buys,
    _append_pending_buys,
    _replace_pending_buy_order,
    _qmt_execution_init_lock,
    _trade_intent_store,
    _record_live_buy,
    _mark_live_position_closed,
    _reduce_live_position_shares,
)
from src.daemon.plans import (  # noqa: F401
    st_open_forbidden,
    active_strategy_mode,
    _expected_signal_date,
    _has_signal_for_date,
    _date_in_scored,
    _prefer_live_processed_path,
    _date_in_csv,
    _checklist_data_quality_blocked,
    _sig_day_amount_cache,
    _signal_day_amount,
    _ace_gate_mode_for,
    _configured_exit_pov_handoff_time,
    _strategy_d_force_sell_codes_today,
    _has_premarket_close_plan,
    _has_due_close_plan_now,
    _maybe_resume_pipeline_after_trade,
    _combined_paths_for_today,
    read_cached_combined_decisions,
    _equity_curve_stop_block_reason,
    load_combined_decisions,
    has_combined_action,
    MODE1_OPEN_ACTION_PRIORITY,
    _combined_open_action_for_current_mode,
    OPEN_ACTION_LEG_PRIORITY,
    _select_unique_buy_order_for_action,
    D_INTRADAY_BLOCKING_BUY_ACTIONS,
    _d_blocking_buy_actions,
    _d_candidate_legs,
    d_intraday_monitor_gate,
    d_intraday_entry_gate,
    has_open_local_position,
    has_position_bought_today,
    _limit_up_pct_for_stock,
    _round_stock_price,
    _ceil_stock_price,
    _find_local_prev_close,
    _estimate_limit_up_price,
    blocks_d_for_opening_plan,
    _load_e_signal_for_signal_date,
    _latest_planned_orders_signal_date,
)
from src.daemon.qmt import (  # noqa: F401
    _DAEMON_BOOT_ID,
    qmt_is_critical_window,
    _broker_missing_streak,
    reconcile_missing_local_positions,
    clear_local_positions_when_broker_empty,
    reconcile_positions_with_broker,
    _qmt_lock,
    QMTReconnectBackoffError,
    _is_qmt_inner_file_transport_busy,
    _qmt_inner_outage,
    _QMT_CLIENT_DIAGNOSIS_INTERVAL_SEC,
    _is_qmt_inner_heartbeat_stale,
    _qmt_client_processes,
    _diagnose_qmt_client,
    _record_qmt_inner_heartbeat_stale,
    _notify_qmt_inner_outage_recovered,
    _qmt_connect_backoff_seconds,
    _broker_config_with_qmt_override,
    _should_clear_qmt_cache_for_error,
    _qmt_connect_once,
    _qmt_connect,
    _get_qmt_execution_service,
    _qmt_get_raw,
    _qmt_get,
    SharedQMTBrokerProxy,
    _TRANSIENT_QMT_READ_ERROR_MARKERS,
    _is_transient_qmt_read_error,
    _qmt_query_account_positions,
    BrokerSnapshotInconsistentError,
    _ACTIVE_QMT_ORDER_STATUSES,
    _active_buy_order_reserved_cash,
    _sanitize_account_snapshot,
    EXIT_CODE_SOCKET_EXHAUSTED,
    EXIT_CODE_QMT_CHANNEL_POISONED,
    EXIT_CODE_BROKER_RESULT_UNKNOWN,
    EXIT_CODE_QMT_RECONNECT_STALLED,
    _on_qmt_execution_timeout,
    _on_broker_result_unknown,
    _record_broker_snapshot_inconsistent,
    _request_qmt_resource_recovery,
    _request_qmt_reconnect_stalled_recovery,
    _qmt_reset,
    _qmt_reset_raw,
    CAPACITY_WALL_STATE,
    _check_capacity_wall_milestone,
    _strategy_only_market_value,
    _broker_has_strategy_position,
    _broker_has_preexisting_strategy_position,
    _account_status_pre_close_map,
    _print_account_status,
    check_qmt_connection,
    wait_for_qmt_startup_gate,
    _recover_trade_execution_state_once,
    _recovered_buy_exit_n_days,
    _project_recovered_buy_intents,
    _validate_local_execution_state_for_startup,
    wait_for_trade_recovery_gate,
)
from src.daemon.orders import (  # noqa: F401
    _confirm_fill,
    _format_order_fill_detail,
    _try_cancel_order,
)
from src.daemon.sell import (  # noqa: F401
    _sell_price_cage_floor,
    _pick_sell_limit_price,
    _cancel_own_takeprofit_orders,
    _max_sell_order_shares,
    _split_sell_order_quantities,
    _normalize_exit_slice_quantity,
    _abc_place_sell_order_direct,
    _abc_place_sell_order_direct_locked,
    _broker_position_quantities,
    _exit_safety_alerted,
    _exit_batch_key,
    _local_exit_quantities,
    _alert_exit_safety_once,
    _ensure_exit_batch,
    _order_is_sell,
    _order_is_system_exit,
    _order_commitment,
    _exit_commitments_by_code,
    _non_system_active_sell_exists,
    _safe_new_exit_order_quantity,
    _prepare_exit_order_intent,
    _update_exit_order_intent,
    _resolve_exit_intent_by_broker_order_id,
    _exit_intent_by_broker_order_id,
    _apply_known_exit_fill,
    _exit_order_submission_block_reason,
    _place_exit_order_with_intent,
    _assert_exit_live_allowed,
    _watchdog_pending,
    _watchdog_pending_lock,
    _queue_watchdog_pending,
    _remove_watchdog_pending,
)
from src.daemon.buy import (  # noqa: F401
    _execute_orders_inprocess,
    resize_buy_orders_for_live_account,
    _effective_single_order_cap,
    _calculate_pov_buy_quantity,
    explain_reject_reasons,
    _record_e_capacity_and_alert,
    _e_auction_buy_worker,
    _pov_auction_share_for,
    _e_auction_seed_quantity,
    _pov_auction_seed_quantity,
    POV_EXEC_LOG,
    _pov_thread_lock,
    _pov_enqueue,
    _enqueue_opening_pov_from_plan,
    _start_pov_worker,
    _pov_log_slice,
    _pov_slice_row,
    _broker_position_quantity_and_cost,
    _today_local_entry_cost,
    _confirmed_entry_actual_amount,
    _pov_execute_slice,
    _pov_finalize_item,
    _finalize_expired_pov_for_startup,
    _pov_worker,
    _new_buy_execution_gate,
    _notify_new_buy_gate_block,
    _premarket_buy_price,
    _record_premarket_buy_fill,
    _resubmit_premarket_buy,
    _premarket_buy_monitor_loop,
    _start_premarket_buy_monitor,
    confirm_pending_premarket_buys,
    handle_combined_order_preview,
    _e_retry_running,
    _mark_e_retry_resolved,
    _e_retry_resolved_today,
    _get_e_buy_ts_codes,
    _e_open_price_ok,
    _e_place_order_direct,
    _e_delayed_buy_loop,
    _start_e_retry_thread,
)
from src.daemon.buy_m import (  # noqa: F401
    M_SIGNAL_RECENT_PATH,
    M_POV_END_TIME,
    M_POV_START_TIME,
    _m_state_lock,
    _m_signal_for_today,
    _m_live_enabled,
    _m_ace_context,
    _m_buy_quantity,
    _m_upper_limit,
    _m_submit_buy,
    _m_exit_date,
    _m_pick_candidate,
    job_m_premarket_buy,
    job_m_after_auction,
    _m_pov_round,
    _m_pov_finalize,
    _m_pov_worker,
    _m_start_pov_worker,
)
from src.daemon.sell_pov import (  # noqa: F401
    _large_exit_force_eligible,
    T2_CLOSE_STRATEGY_LEGS,
    _configured_exit_pov_strategy_legs,
    _fixed_large_runway_allowed,
    _exit_pov_due_positions,
    _exit_pov_capacity_triggered,
    _exit_auction_bypass_decision,
    _pov_depth_limit,
    _cancel_active_exit_pov_orders,
    _cancel_active_exit_pov_orders_locked,
    _record_exit_pov_slice,
    _exit_pov_slice,
    _exit_pov_monitor,
)
from src.daemon.close import (  # noqa: F401
    _cancel_active_close_orders_for_auction,
    _cancel_active_close_orders_for_auction_locked,
    _do_sell,
    _reconcile_watchdog_pending_once,
    _active_sell_outstanding_by_code,
    _exit_uncovered_by_code,
    _strategy_exit_trade_vwap_by_code,
    _strategy_exit_order_vwap_by_code,
    _watchdog_rescue_sell,
    _watchdog_reconcile_after_close,
    _due_positions_for_close_handoff,
    _close_position_watchdog,
    check_and_close_positions,
    _exit_close_submit_clock_guard_seconds,
    _wait_for_exit_close_submit_clock_guard,
    job_afternoon,
)
from src.daemon.takeprofit import (  # noqa: F401
    _takeprofit_submission_label,
    _intraday_takeprofit_monitor,
)
from src.daemon.strategy_d import (  # noqa: F401
    _strategy_d_release_signal_clock,
    _strategy_d_checkpoint_recovery_check,
    reconcile_d_orphan_fills,
    _d_relay_pair_log,
    _load_d_relay_candidate_order,
    _d_relay_prepare_state,
    _handoff_same_stock_d_relay,
    _d_relay_block,
    _d_relay_recover_order_id,
    _d_relay_apply_sell_fill,
    _d_relay_reconcile_pending_sell,
    _d_relay_apply_buy_fill,
    _d_relay_reconcile_pending_buy,
    _d_relay_flow_budget,
    _d_relay_submit_buy,
    _d_relay_submit_sell,
    _d_relay_execute_pair_slice,
    _d_relay_finalize,
    _d_relay_pair_worker,
    _start_d_relay_pair_worker,
    job_premarket_sell,
    job_strategy_d,
    stop_strategy_d_monitor,
    clear_strategy_d_intraday_transient_data,
    _start_d_monitor_log_forwarder,
    _start_d_monitor_health_probe,
    _pid_is_running,
    _strategy_d_monitor_running,
    EXIT_CODE_D_POSITION_RECOVERY,
)
from src.daemon.reports import (  # noqa: F401
    OPEN_PLAN_DEFAULT_EXECUTION_CUTOFF,
    OPEN_PLAN_EXECUTION_CUTOFF_BY_LEG,
    _csv_readiness,
    _json_signal_has_date,
    _strategy_signal_run_readiness,
    report_next_trade_factor_readiness,
    _processed_data_ready_for_date,
    _log_collection_brief,
    _log_cleaning_brief,
    _log_dynamic_feature_brief,
    _log_fill_score_brief,
    _log_enhanced_feature_brief,
    _log_abc_strategy_brief,
    _read_close_candidate_artifact,
    _CLOSE_CANDIDATE_PUSHED_FILE,
    _push_close_candidate_summary_once,
    _m_close_summary,
    _log_close_pipeline_candidate_summary,
    _segment_label,
    _load_limit_for_codes,
    _load_daily_for_codes,
    report_next_day_candidates,
    _load_e_candidate_count,
    _log_e_signal_status,
    _planned_equity_base,
    _planned_shares_by_equity,
    _LIVE_SIZING_CACHE,
    _LIVE_SIZING_TTL_SEC,
    _LIVE_SIZING_LOCK,
    _live_plan_sizing,
    _live_plan_sizing_uncached,
    _format_live_plan_line,
    _exit_method_desc,
    _ordinary_open_plan_expired,
    _local_position_blocks_open_plan_broadcast,
    _log_final_decision_summary,
    _describe_holdings,
    _format_candidate_stock,
    _format_strategy_candidate,
    _load_candidate_csv_for_broadcast,
    _load_readonly_candidate_for_broadcast,
    _build_candidate_choice_lines,
    _log_decision_chain_summary,
    _PLAN_PUSH_STATE,
    _MISSED_OPEN_ALERT_STATE,
    _d_candidate_gate_line,
    _plan_push_already_sent,
    _retry_morning_plan_notification_if_needed,
    _notify_missed_open_window_if_needed,
    _m_plan_line,
    push_open_plan_notification,
    _open_plan_execution_detail,
    job_open_plan_fill_check,
    _decision_chain_broadcast_loop,
    _load_config_section,
    _load_ab_checklist,
    LIMIT_SOURCES_MATCHING_BACKTEST,
    _limit_sources_match_backtest,
    report_signal_readiness_summary,
    _log_market_environment,
    _log_abc_filter_funnel,
    _log_d_status_for_signal,
    _value_counts_text,
    _load_reject_detail,
)
from src.daemon.pipeline import (  # noqa: F401
    post_market_marker_path,
    mark_post_market_done,
    has_post_market_run_today,
    _pipeline_thread_lock,
    _start_post_market_pipeline,
    _log_post_market_step_brief,
    snapshot_realized_strategy_equity,
    _describe_ledger_trade_key,
    _maybe_alert_ledger_incomplete,
    _maybe_alert_strategy_drawdown,
    job_post_market,
    _equity_curve_stop_decision_fresh,
    _alert_if_stop_decision_stale,
    _alert_post_market_not_completed,
    _run_post_market_with_retry,
    _offline_data_fresh,
    _start_offline_data_maintenance,
)
from src.daemon.jobs import (  # noqa: F401
    job_premarket_position_sync,
    job_preopen_plan,
    job_morning_plan_push,
    job_premarket_buy,
    job_morning,
    job_opening_buy,
    startup_catchup_strategy_d,
    job_cancel_unfilled_buy_orders,
)
from src.daemon.scheduler import (  # noqa: F401
    SYSTEM_READY_TITLE,
    SYSTEM_READY_BODY,
    _publish_system_ready,
    setup,
    _periodic_health_snapshot,
    _next_periodic_health_beacon_at,
    _publish_periodic_health_beacon,
    _periodic_health_beacon_loop,
    _daily_calendar_sentinel,
    _running_on_windows,
    _live_qmt_account_id,
    _qmt_install_root,
    _check_qmt_scheduled_restart,
    next_event,
    next_event_after,
    run_job,
    _should_run_startup_signal_audit,
    main,
)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # 信号处理器(_exit)已发"已停止"通知，这里直接退出
    except KeyboardInterrupt:
        # Windows 下 Ctrl+C 常直接抛 KeyboardInterrupt 而绕过信号处理器，这里补发停止通知
        _notify("system_error", "🔌 守护进程已停止",
                "守护进程被手动中断(Ctrl+C)，实盘自动交易已暂停。如需恢复请重新启动。")
        raise
    except Exception as _fatal:
        try:
            get_logger("a_share_quant").exception("守护进程致命错误，即将退出：%s", _fatal)
        except Exception:
            pass  # 可以忽略：致命错误的日志写不了也要继续推送退出通知
        _notify("system_error", "🛑 守护进程异常退出",
                "守护进程发生致命错误即将退出，实盘自动交易已停止，请立即检查并重启。",
                level="critical", call=True)
        raise
