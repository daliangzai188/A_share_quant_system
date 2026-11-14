"""D盘中监控 StrategyDMonitor 的断点续跑方法（2026-11-15 项目优化：从 scripts/monitor_strategy_d_intraday.py 原样搬来，逻辑未改）。

检查点保存与恢复、盘中分钟历史补齐。StrategyDMonitor 继承 CheckpointMixin；方法里用到的属性都在 StrategyDMonitor 的 __init__/setup 里建立。
"""
from __future__ import annotations

from dataclasses import asdict, fields

from src.utils.time_utils import now_beijing, today_beijing
from src.strategy_d_checkpoint import (
    D_CHECKPOINT_STATUS_READY,
    block_strategy_d_checkpoint_recovery,
    clear_strategy_d_checkpoint_recovery_block,
    inspect_strategy_d_checkpoint,
    invalidate_strategy_d_checkpoint,
    write_strategy_d_checkpoint,
)
from src.strategy_d_spec import D_TRACKING_START_HHMM, intraday_history_is_complete

from src.strategy_d_monitor.support import StockState, hhmm_to_str, classify_market_segment


class CheckpointMixin:
    """断点续跑：检查点保存与恢复、盘中分钟历史补齐。"""

    def _invalidate_checkpoint(self, status: str, reason: str) -> bool:
        try:
            invalidate_strategy_d_checkpoint(
                self.checkpoint_path,
                trade_date=today_beijing().strftime("%Y%m%d"),
                status=status,
                reason=reason,
                recorded_at=now_beijing(),
                machine_fingerprint=self.checkpoint_machine_fingerprint,
                runtime_fingerprint=self.checkpoint_runtime_fingerprint,
            )
            self._last_checkpoint_io_error = ""
            return True
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D路径检查点失效标记写入失败：%s", exc)
            return False

    def _enter_checkpoint_io_degraded(self, reason: str) -> bool:
        """隔离检查点I/O故障；不把完整的内存行情路径误判为永久缺失。"""

        self.checkpoint_io_degraded = True
        self.checkpoint_io_reason = str(reason)
        try:
            block_strategy_d_checkpoint_recovery(
                self.checkpoint_path,
                trade_date=today_beijing().strftime("%Y%m%d"),
                reason=self.checkpoint_io_reason,
                recorded_at=now_beijing(),
            )
        except Exception as exc:
            # 主检查点没能切换为 SCAN_IN_PROGRESS，恢复阻断标记也无法落盘时，
            # 新进程可能读取陈旧 READY。此时无法证明恢复唯一性，只能永久停开。
            self.path_integrity_failed = True
            self.path_integrity_reason = (
                "D检查点I/O失败且恢复阻断标记无法落盘，不能证明重启恢复路径唯一:"
                f"{exc}"
            )
            self.logger.error("D完整路径已永久失效：%s", self.path_integrity_reason)
            return False
        self.logger.warning(
            "D检查点进入I/O降级保护：%s；当前轮不判断BUY。若后续全市场扫描完整、"
            "新READY原子落盘且恢复阻断标记清除成功，将自动恢复D，不按全天路径缺失处理。",
            self.checkpoint_io_reason,
        )
        return True

    def _clear_checkpoint_io_degraded(self) -> bool:
        try:
            clear_strategy_d_checkpoint_recovery_block(self.checkpoint_path)
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D新READY已落盘，但恢复阻断标记清除失败：%s", exc)
            return False
        old_reason = self.checkpoint_io_reason
        self.checkpoint_io_degraded = False
        self.checkpoint_io_reason = ""
        self._last_checkpoint_io_error = ""
        self.logger.warning(
            "D检查点I/O已恢复：后续完整扫描已成功写入新READY并清除恢复阻断标记；"
            "D继续使用09:30起内存路径判断信号。原故障=%s",
            old_reason,
        )
        return True

    def _save_ready_checkpoint(self) -> bool:
        now = now_beijing()
        if not self.first_complete_scan_at:
            self.first_complete_scan_at = now.isoformat()
        self.last_complete_scan_at = now.isoformat()
        resume_allowed = bool(
            not self.path_integrity_failed
            and not self.order_placed
            and not self.session_orders
            and not self.position_opened
            and not self.waiting_order_only
        )
        # 未曾封板且当前也未封板的股票，其路径状态等价于默认值；股票宇宙摘要和
        # 完整扫描覆盖数已证明它们被观察过，无需每30秒重复写入数千条零状态。
        # 恢复时先按同一宇宙重建默认状态，再覆盖下面的非默认路径状态。
        path_states = {
            code: self._json_safe(asdict(state))
            for code, state in self.states.items()
            if (
                state.ever_sealed
                or state.was_sealed
                or state.open_times_today > 0
                or state.watch_alerted
                or state.buy_signaled
                or bool(state.order_id)
                or state.invalid_price_ticks > 0
            )
        }
        payload = {
            "status": D_CHECKPOINT_STATUS_READY,
            "resume_allowed": resume_allowed,
            "trade_date": today_beijing().strftime("%Y%m%d"),
            "recorded_at": now.isoformat(),
            "tracking_start_hhmm": D_TRACKING_START_HHMM,
            "original_session_start_hhmm": self.original_session_start_hhmm,
            "first_complete_scan_at": self.first_complete_scan_at,
            "last_complete_scan_at": self.last_complete_scan_at,
            "scan_round": self.scan_round,
            "last_scan_updated_count": self.last_scan_updated_count,
            "universe_size": len(self.universe),
            "universe_sha256": self.universe_sha256,
            "market_context_sha256": self.market_context_sha256,
            "state_count": len(path_states),
            "states": path_states,
            "path_integrity_failed": self.path_integrity_failed,
            "path_integrity_reason": self.path_integrity_reason,
            "machine_fingerprint": self.checkpoint_machine_fingerprint,
            "runtime_fingerprint": self.checkpoint_runtime_fingerprint,
            "signal_records": self._json_safe(self.signal_records),
            "strong_notified": self._strong_notified,
            "limit_price_fallback_logged": self.limit_price_fallback_logged,
            "order_placed": self.order_placed,
            "order_locked_ts_code": self.order_locked_ts_code,
            "factor_signal_consumed": self.factor_signal_consumed,
            "factor_signal_locked_ts_code": self.factor_signal_locked_ts_code,
            "session_orders": self._json_safe(self.session_orders),
        }
        try:
            write_strategy_d_checkpoint(self.checkpoint_path, payload)
            self._last_checkpoint_io_error = ""
            return True
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D完整路径检查点保存失败，本轮状态不可用于重启恢复：%s", exc)
            return False

    def _restore_ready_checkpoint(self) -> tuple[bool, str]:
        check = inspect_strategy_d_checkpoint(
            self.checkpoint_path,
            trade_date=today_beijing().strftime("%Y%m%d"),
            now=now_beijing(),
            max_age_seconds=self.checkpoint_max_age_sec,
            expected_tracking_start_hhmm=D_TRACKING_START_HHMM,
            expected_machine_fingerprint=self.checkpoint_machine_fingerprint,
            expected_runtime_fingerprint=self.checkpoint_runtime_fingerprint,
            expected_universe_sha256=self.universe_sha256,
            expected_universe_size=len(self.universe),
            expected_market_context_sha256=self.market_context_sha256,
        )
        if not check.ok:
            return False, check.reason
        payload = check.payload
        valid_field_names = {item.name for item in fields(StockState)}
        restored_states: dict[str, StockState] = {
            code: StockState(
                ts_code=code,
                name=self.name_map.get(code, ""),
                market_segment=classify_market_segment(code),
                circ_mv=float(self.circ_mv_map.get(code, 0.0) or 0.0),
            )
            for code in self.universe
        }
        try:
            for ts_code, raw_state in payload["states"].items():
                if set(raw_state).difference(valid_field_names):
                    raise ValueError(f"{ts_code}包含未知状态字段")
                if str(ts_code) not in restored_states:
                    raise ValueError(f"{ts_code}不在当前股票宇宙")
                restored_states[str(ts_code)] = StockState(**raw_state)
        except Exception as exc:
            return False, f"D逐票状态无法恢复:{exc}"
        self.states = restored_states
        self.scan_round = int(payload["scan_round"])
        self.original_session_start_hhmm = int(payload["original_session_start_hhmm"])
        self.first_complete_scan_at = str(payload["first_complete_scan_at"])
        self.last_complete_scan_at = str(payload["last_complete_scan_at"])
        self.last_scan_updated_count = int(payload["last_scan_updated_count"])
        records = payload.get("signal_records", [])
        self.signal_records = list(records) if isinstance(records, list) else []
        self._strong_notified = bool(payload.get("strong_notified", False))
        self.limit_price_fallback_logged = bool(
            payload.get("limit_price_fallback_logged", False)
        )
        self.factor_signal_consumed = bool(
            payload.get("factor_signal_consumed", False)
        )
        self.factor_signal_locked_ts_code = str(
            payload.get("factor_signal_locked_ts_code", "")
        )
        self._restored_from_checkpoint = True
        return True, check.reason

    def _prepare_intraday_history(
        self,
        session_start_hhmm: int,
        *,
        resumable_in_memory_path: bool,
    ) -> tuple[bool, str]:
        """准备D路径证据：内存/检查点优先，正式因子版可回补QMT 1m历史。

        QMT回补只负责恢复09:30以来的已完成分钟，不能把此前已错过的回封信号
        当成当前信号追买；后续仍由``require_fresh_reseal=True``只接受最新完成
        分钟刚形成的回封。LEGACY模式依赖30秒快照转换，无法用1m数据等价恢复。
        """

        if intraday_history_is_complete(session_start_hhmm):
            return True, "监控从09:30完整路径起点启动"
        if resumable_in_memory_path:
            return True, "同一监控对象保留09:30起连续内存路径"

        restored, checkpoint_reason = self._restore_ready_checkpoint()
        if restored:
            return True, f"原子检查点恢复成功:{checkpoint_reason}"

        if (
            self.factor_union_active
            and self.late_start_qmt_1m_recovery_enabled
            and self.broker is not None
            and hasattr(self.broker, "get_minute_bars")
        ):
            self.minute_history_recovery_required = True
            self.minute_history_recovery_completed = False
            self.minute_history_recovery_reason = (
                "原子检查点不可恢复"
                f"({checkpoint_reason or '无检查点'})，改由QMT回补09:30至当前完整1m路径"
            )
            return True, self.minute_history_recovery_reason

        return False, (
            "缺少09:30起连续内存路径，且原子检查点不可恢复"
            f"({checkpoint_reason or '无检查点'})；当前D模式或行情接口不支持严格QMT 1m回补"
        )

    def _mark_minute_history_recovery_complete(
        self,
        *,
        target_count: int,
        current_hhmm: int,
    ) -> None:
        """只在全市场快照和全部触板目标1m路径均完整后解除回补门禁。"""

        if not self.minute_history_recovery_required:
            return
        self.minute_history_recovery_required = False
        self.minute_history_recovery_completed = True
        self.logger.warning(
            "[D QMT 1M RECOVERED] 已从QMT严格回补09:30~%s路径："
            "全市场快照覆盖%d只，需逐分钟认证的非ST首板触板目标%d只。"
            "恢复后只接受最新完成分钟新形成的回封，不追买恢复前已错过的历史信号。",
            hhmm_to_str(current_hhmm),
            self.last_scan_updated_count,
            target_count,
        )
