"""
策略D盘中监控：首板回封信号检测 + 14:55自动撤单

【正式发布驱动】
  FACTOR_UNION模式：
    从09:30读取QMT一分钟K线；只用已完成分钟收盘重建封板、炸板和回封，
    每次新完成的分钟回封匹配已发布因子条件；
    旧D的14:00时间、strong情绪和2～3次炸板条件不参与因子版判断。

  LEGACY_FORMAL_D模式：
    09:35～14:00只观察；14:00后真实回封并满足旧D全部条件才允许BUY。

  两种模式都继续强制首板、非ST、当前封板、可靠成交概率、单日唯一候选和
  14:55撤单等公共安全门。

运行方式：
  python scripts/monitor_strategy_d_intraday.py              # 仅提醒，不下单
  python scripts/monitor_strategy_d_intraday.py --live-order # 已退役；实盘只能由daemon统一执行
  python scripts/monitor_strategy_d_intraday.py --dry-run    # 打印配置后退出

日志/输出：
  logs/strategy_d_monitor_YYYYMMDD.log
  reports/strategy_d/intraday_signals_YYYYMMDD.csv

代码位置（2026-11-15 起按职责纯搬移拆分，逻辑未改）：本文件保留 StrategyDMonitor 的主流程（初始化、轮询、
状态更新、成交概率门）和启动入口；参数与辅助函数、断点续跑、市场环境、选股触发、下单撤单在
src/strategy_d_monitor/ 下，StrategyDMonitor 继承那里的四个方法组。原来本文件里的名字仍可从这里导入。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from dotenv import load_dotenv

from src.utils.logger import setup_logger
from src.utils.config import load_json_config
from src.portfolio_ace_m import ACE_FULL_HARD_CAP_PCT, ACE_FULL_TARGET_PCT
from src.utils.time_utils import now_beijing, today_beijing
from src.notify import notify
from src.data_cleaner import DataCleaner
from src.fill_model import FillProbabilityEstimator
from src.strategy_d_checkpoint import (
    D_CHECKPOINT_STATUS_CLOSED,
    D_CHECKPOINT_STATUS_READY,
    D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
    block_strategy_d_checkpoint_recovery,
    clear_strategy_d_checkpoint_recovery_block,
    inspect_strategy_d_checkpoint,
    invalidate_strategy_d_checkpoint,
    strategy_d_checkpoint_path,
    strategy_d_checkpoint_recovery_block_path,
    strategy_d_machine_fingerprint,
    strategy_d_market_context_sha256,
    strategy_d_runtime_fingerprint,
    strategy_d_universe_sha256,
    write_strategy_d_checkpoint,
)
from src.strategy_d_factor_rules import (
    FACTOR_UNION_MODE,
    factor_values_from_raw,
    load_factor_release,
    matching_profile_ids,
    release_signal_clock,
    release_uses_factor_union,
    trading_minutes_between,
)
from src.strategy_d_minute_alignment import (
    StrictMinutePath,
    replay_completed_minute_path,
)
from src.market_rules import market_segment, price_limit_pct
from src.qmt_adapter import MAX_SINGLE_QUOTE_SUBSCRIPTIONS
from src.strategy_d_spec import (
    D_CHECKPOINT_MAX_AGE_SECONDS,
    D_FIRST_TIME_BUCKETS,
    D_LATEST_COMPLETE_HISTORY_START_HHMM,
    D_MAX_OPEN_TIMES,
    D_MIN_FILL_PROBABILITY,
    D_MIN_OPEN_TIMES,
    D_ORDER_CANCEL_HHMM,
    D_PREFERRED_OPEN_TIMES,
    D_SIGNAL_START_HHMM,
    D_TAIL_RESEAL_HHMM,
    D_TRACKING_START_HHMM,
    classify_first_time_bucket_hhmm,
    common_candidate_rejection_reason,
    d_rank_key,
    intraday_history_is_complete,
    live_sentiment_is_historical_strong,
)
from src.strategy_d_monitor.support import (  # noqa: F401  原来定义在本文件，守护进程和测试仍从这里取用
    SENTIMENT_STRONG_MIN,
    SENTIMENT_STRONG_MAX,
    WATCH_START_HHMM,
    SIGNAL_START_HHMM,
    CANCEL_HHMM,
    POLL_BATCH_SIZE,
    POLL_INTERVAL_SEC,
    ORDER_FILL_POLL_INTERVAL_SEC,
    MONITOR_START_HHMM,
    D_POSITION_PCT,
    D_RETRY_TOP_N,
    MIN_D_VALID_LIMIT_PRICE,
    MAX_D_INVALID_PRICE_TICKS,
    STRATEGY_REMARK,
    DEFAULT_ALLOWED_SEGMENTS,
    StockState,
    now_hhmm,
    hhmm_to_str,
    _read_open_dates_before_today,
    _resolve_previous_limit_file,
    load_yesterday_limit_codes,
    load_stock_universe,
    load_latest_circ_mv_map,
    load_previous_daily_amount_map,
    classify_market_segment,
    load_strategy_d_config,
    configured_factor_release_path,
    configured_allowed_segments,
    configured_position_pct,
    configured_max_open_times,
    configured_min_open_times,
    configured_preferred_open_times,
    configured_min_fill_probability,
    configured_first_time_buckets,
    configured_tail_reseal_hhmm,
    configured_sentiment_bounds,
    configured_checkpoint_max_age_sec,
    validate_configured_execution_clock,
    filter_universe_by_segments,
    limit_up_pct,
    round_price,
    estimate_upper_limit,
    check_strategy_position_occupied,
    load_stock_names,
    calc_shares_below_target_amount,
    DOrderCapacity,
    calculate_d_order_capacity,
    is_d_market_context_eligible,
    next_trade_day,
    load_position_records,
    save_position_records,
)
from src.strategy_d_monitor.checkpoint import CheckpointMixin
from src.strategy_d_monitor.market import MarketContextMixin
from src.strategy_d_monitor.signals import SignalMixin
from src.strategy_d_monitor.orders import OrderMixin

load_dotenv(PROJECT_ROOT / ".env")


# ── 核心监控逻辑 ─────────────────────────────────────────────────────────────

class StrategyDMonitor(CheckpointMixin, MarketContextMixin, SignalMixin, OrderMixin):

    def __init__(self, broker, live_order: bool, logger, signal_csv: Path,
                 monitor_start_hhmm: int = MONITOR_START_HHMM,
                 allowed_segments: set[str] | None = None,
                 position_pct: float = D_POSITION_PCT,
                 config: dict[str, Any] | None = None,
                 position_recorder: Callable[[dict[str, Any]], None] | None = None,
                 entry_gate: Callable[[], tuple[bool, str]] | None = None,
                 tracking_gate: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.broker = broker
        self.live_order = live_order
        self.logger = logger
        self.signal_csv = signal_csv
        self.monitor_start_hhmm = monitor_start_hhmm
        self.allowed_segments = allowed_segments or set(DEFAULT_ALLOWED_SEGMENTS)
        self.position_pct = position_pct
        self.config = config or {}
        self.position_recorder = position_recorder
        # D的行情路径采集与实际入场必须解耦：上游候选仍在执行时继续从09:30
        # 维护完整日内路径，但每轮形成BUY候选前动态复核资金是否已经真正释放。
        # 独立研究/回测未传回调时保持原行为；daemon实盘入口必须传入组合门禁。
        self.entry_gate = entry_gate
        self.tracking_gate = tracking_gate
        self._last_entry_gate_reason = ""
        if self.live_order and self.position_recorder is None:
            raise RuntimeError("D实盘必须由daemon统一持仓记录器回写，禁止直写positions.json")
        self.factor_release_path = configured_factor_release_path(self.config)
        self.factor_release = load_factor_release(self.factor_release_path)
        self.factor_union_active = release_uses_factor_union(self.factor_release)
        self.factor_profiles = list(self.factor_release.get("profiles", []))
        self.factor_release_id = str(self.factor_release.get("release_id", ""))
        self.factor_signal_clock = release_signal_clock(
            self.factor_release,
            full_session_last_hhmm=CANCEL_HHMM - 1,
        )
        entry_alignment = self.factor_release.get("entry_alignment", {})
        self.historical_fill_gate_certified = bool(
            isinstance(entry_alignment, dict)
            and entry_alignment.get("historical_signal_time_fill_gate_certified", False)
        )
        self.runtime_new_buy_enabled = bool(
            isinstance(entry_alignment, dict)
            and entry_alignment.get("runtime_new_buy_enabled", False)
        )
        validate_configured_execution_clock(self.config)
        self.min_open_times = configured_min_open_times(self.config)
        self.max_open_times = configured_max_open_times(self.config)
        self.preferred_open_times = configured_preferred_open_times(self.config)
        self.min_fill_probability = configured_min_fill_probability(self.config)
        self.first_time_buckets = configured_first_time_buckets(self.config)
        self.tail_reseal_hhmm = configured_tail_reseal_hhmm(self.config)
        self.checkpoint_max_age_sec = configured_checkpoint_max_age_sec(self.config)
        self.late_start_qmt_1m_recovery_enabled = bool(
            self.config.get("strategy_d", {}).get(
                "late_start_qmt_1m_recovery_enabled", False
            )
        )
        (
            self.sentiment_current_min,
            self.sentiment_current_max,
        ) = configured_sentiment_bounds(self.config)
        self.default_fill_planned_amount = float(
            self.config.get("fill_model", {}).get(
                "default_planned_buy_amount", 100000
            )
        )

        self.yesterday_limit_codes: set[str] = set()
        self.universe: list[str] = []
        self.name_map: dict[str, str] = {}
        self.circ_mv_map: dict[str, float] = {}
        self.previous_day_amount_map: dict[str, float] = {}
        self.segment_stock_counts: dict[str, int] = {}
        self.states: dict[str, StockState] = {}
        self.fill_estimator: FillProbabilityEstimator | None = None
        self.fill_model_ready: bool = False
        self.scan_round = 0
        self.path_integrity_failed = False
        self.path_integrity_reason = ""
        self._path_failure_notified = False
        # 检查点 I/O 降级与行情路径缺失分开管理。前者可在后续完整扫描并成功写入
        # 新 READY 后恢复；后者涉及真实封板/炸板证据缺口，必须全天 fail-closed。
        self.checkpoint_io_degraded = False
        self.checkpoint_io_reason = ""
        self._last_checkpoint_io_error = ""
        self.checkpoint_path = strategy_d_checkpoint_path(
            PROJECT_ROOT, today_beijing().strftime("%Y%m%d")
        )
        self.checkpoint_machine_fingerprint = strategy_d_machine_fingerprint()
        self.checkpoint_runtime_fingerprint = ""
        self.universe_sha256 = ""
        self.market_context_sha256 = ""
        self.original_session_start_hhmm = 0
        self.first_complete_scan_at = ""
        self.last_complete_scan_at = ""
        self.last_scan_updated_count = 0
        self._restored_from_checkpoint = False

        # 本次会话下单记录 {order_id: ts_code}
        self.session_orders: dict[str, str] = {}
        self.session_order_details: dict[str, dict[str, Any]] = {}
        self.signal_records: list[dict] = []
        self.order_placed: bool = False   # 本会话已触发BUY，不再对其他股票下单
        self.order_locked_ts_code: str = ""
        self.factor_signal_consumed: bool = False
        self.factor_signal_locked_ts_code: str = ""
        # 每个已完成分钟只输出一次候选结论，既明确回答“有没有合格候选”，
        # 又避免30秒轮询在同一分钟重复刷屏。
        self.factor_candidate_summary_hhmm: int = 0
        self.factor_candidate_summary_announced: bool = False
        self.position_opened: bool = False
        self.waiting_order_only: bool = False
        self.limit_price_fallback_logged: bool = False
        self._strong_notified: bool = False     # 情绪转强(≥阈值)只推送一次
        self._fill_alignment_block_notified: bool = False
        # 正式因子D的事件时钟必须来自“已完成QMT 1m收盘”，不能再使用30秒快照
        # 的瞬时涨停/炸板转换。缓存只覆盖当前分钟；下一分钟重新拉取完整路径。
        self.strict_minute_paths: dict[str, StrictMinutePath] = {}
        self.strict_minute_refresh_hhmm: int = 0
        self.strict_minute_refresh_error: str = ""
        # daemon晚启动且原子检查点不可用时，正式FACTOR_UNION D可以从QMT补取
        # 09:30至当前的完整1m K线重建路径。该标记只表示“需要回补”，真正完成
        # 必须等首次全市场快照扫描完整、所有触板目标的分钟序列逐根认证通过。
        self.minute_history_recovery_required: bool = False
        self.minute_history_recovery_completed: bool = False
        self.minute_history_recovery_reason: str = ""

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def setup(self) -> None:
        self.logger.info("=== 策略D盘中监控启动 ===")
        self.yesterday_limit_codes = load_yesterday_limit_codes()
        full_universe = load_stock_universe()
        self.universe = filter_universe_by_segments(full_universe, self.allowed_segments)
        self.name_map = load_stock_names()
        self.circ_mv_map = load_latest_circ_mv_map()
        self.previous_day_amount_map = load_previous_daily_amount_map()
        segment_counts: dict[str, int] = {}
        for code in self.universe:
            segment = classify_market_segment(code)
            segment_counts[segment] = segment_counts.get(segment, 0) + 1
        self.segment_stock_counts = segment_counts
        self.universe_sha256 = strategy_d_universe_sha256(self.universe)
        self.market_context_sha256 = strategy_d_market_context_sha256(
            self.universe,
            self.yesterday_limit_codes,
            self.name_map,
            self.circ_mv_map,
            self.previous_day_amount_map,
        )
        if not self.checkpoint_runtime_fingerprint:
            self.checkpoint_runtime_fingerprint = strategy_d_runtime_fingerprint(
                PROJECT_ROOT, self.config
            )
        try:
            self.fill_estimator = FillProbabilityEstimator(
                PROJECT_ROOT / "config" / "config.json"
            )
            self.fill_model_ready = True
        except Exception as exc:
            self.fill_estimator = None
            self.fill_model_ready = False
            self.logger.error(
                "D成交概率模型初始化失败，按严格对齐口径禁止D开仓: %s", exc
            )
        self.logger.info(
            "宇宙: %d只(原始%d只) | 允许分段=%s | 分段数量=%s | 昨日涨停(排除首板): %d只 | 全市场扫描: %d批x%d只/轮，轮间隔%d秒",
            len(self.universe), len(full_universe), ",".join(sorted(self.allowed_segments)),
            segment_counts, len(self.yesterday_limit_codes),
            len(self._batches()), POLL_BATCH_SIZE, POLL_INTERVAL_SEC,
        )
        max_order_amount = float(self.config.get("live_trade", {}).get("max_single_order_amount", 50000))
        if self.factor_union_active:
            self.logger.warning(
                "D启用月度冻结因子并集发布: release_id=%s profiles=%d | 任一if命中后进入候选 | "
                "封板/炸板/回封严格按已完成QMT 1m收盘重建 | "
                "有效回封桶=%s，最后信号分钟=%s | "
                "首板/非ST/14:55前/成交概率>=%.0f%%且可靠仍是公共安全门",
                self.factor_release_id,
                len(self.factor_profiles),
                ",".join(self.factor_signal_clock.reseal_time_buckets) or "全时段",
                hhmm_to_str(self.factor_signal_clock.last_signal_hhmm),
                self.min_fill_probability * 100,
            )
            if not self.runtime_new_buy_enabled:
                self.logger.error(
                    "D发布未取得运行新BUY授权；当前只维护路径并禁止新BUY。"
                )
            elif not self.historical_fill_gate_certified:
                self.logger.warning(
                    "D历史信号时L2队列仍不完整，历史收益继续按D=0披露；"
                    "实盘已按用户授权启用逐单实时门禁，只有V15因子、完整1m路径、"
                    "真实委托金额及可靠成交概率>=%.0f%%全部通过才允许BUY。",
                    self.min_fill_probability * 100,
                )
        else:
            self.logger.info(
                "D最低开仓条件: 市场分段在%s | 首板(排除昨日涨停) | 当前封涨停 | 今日炸板%d~%d次(multi_open) | 首次封板时段=%s | 当前封板数=%d~%d(strong代理，不含very_strong) | 最后真实回封>=%s | 成交概率>=%.0f%%且可靠 | 实盘二次复核通过",
                ",".join(sorted(self.allowed_segments)),
                self.min_open_times,
                self.max_open_times,
                ",".join(sorted(self.first_time_buckets)),
                self.sentiment_current_min,
                self.sentiment_current_max,
                hhmm_to_str(self.tail_reseal_hhmm),
                self.min_fill_probability * 100,
            )
        self.logger.info(
            "D回测对齐时钟: %s开始持续记录完整封板/炸板路径；%s；%s停止并撤销未成交委托。",
            hhmm_to_str(MONITOR_START_HHMM),
            (
                "每次已完成1m收盘形成回封后按发布因子if判断"
                if self.factor_union_active
                else f"{hhmm_to_str(SIGNAL_START_HHMM)}后只有真实回封才允许BUY"
            ),
            hhmm_to_str(CANCEL_HHMM),
        )
        self.logger.info(
            "D重启恢复: 每轮完整扫描原子保存逐票路径；检查点最长%d秒，"
            "跨设备/跨交易日/代码配置变化/扫描缺批一律拒绝检查点恢复；"
            "正式因子版QMT 1m回补=%s。",
            self.checkpoint_max_age_sec,
            "启用" if self.late_start_qmt_1m_recovery_enabled else "关闭",
        )
        self.logger.info(
            "D选票规则: 每天最多买1只，先优先炸板%d次，再按封单金额÷流通市值降序；"
            "只买排第1的那只，买不进当天就放弃、不用第2名递补。",
            self.preferred_open_times,
        )
        fixed_cap_text = f"{max_order_amount:.0f}元" if max_order_amount > 0 else "不设固定金额上限"
        self.logger.info(
            "D开仓参数: 目标仓位=%.1f%% 单票硬顶=85%% 固定单笔上限=%s 买入价=涨停价 14:55处理未成交/部分成交委托",
            self.position_pct * 100,
            fixed_cap_text,
        )

    def _batches(self) -> list[list[str]]:
        return [self.universe[i: i + POLL_BATCH_SIZE]
                for i in range(0, len(self.universe), POLL_BATCH_SIZE)]

    def _signal_scan_is_open(self, hhmm: int) -> bool:
        """当前分钟是否仍可能产生正式D信号。

        因子模式直接服从发布分支推导出的时钟；旧模式保持原14:55撤单边界。
        这是全市场快照与QMT分钟线查询的唯一运行条件，避免决策层和行情层各自
        维护一套时间判断。
        """

        if int(hhmm) >= CANCEL_HHMM:
            return False
        if not self.factor_union_active:
            return True
        return int(hhmm) <= self.factor_signal_clock.last_signal_hhmm

    # ── 盘中路径检查点 ────────────────────────────────────────────────────────

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else 0.0
        if isinstance(value, dict):
            return {
                str(key): StrategyDMonitor._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [StrategyDMonitor._json_safe(item) for item in value]
        return value

    def _resolve_order_capacity(self, st: StockState) -> DOrderCapacity:
        """读取当前账户并生成成交门/下单共用的真实目标金额。"""

        if self.broker is None or not hasattr(self.broker, "query_account"):
            raise RuntimeError("没有可查询资金的券商账户")
        account = self.broker.query_account()
        return calculate_d_order_capacity(
            account,
            price=st.upper_limit,
            position_pct=self.position_pct,
            live_config=self.config.get("live_trade", {}),
        )

    def _refresh_fill_gate(
        self,
        st: StockState,
        *,
        planned_buy_amount: float | None = None,
    ) -> tuple[bool, str]:
        """按历史成交概率模型实时复算；模型或字段缺失时fail-closed。"""

        if not self.fill_model_ready or self.fill_estimator is None:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_sample_count = 0
            st.fill_min_group_samples = 0
            st.fill_reject_reason = "成交概率模型未就绪"
            return False, st.fill_reject_reason
        if st.circ_mv <= 0 or st.upper_limit <= 0 or st.bid_vol <= 0:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_sample_count = 0
            st.fill_min_group_samples = self.fill_estimator.min_group_samples
            st.fill_reject_reason = "流通市值/涨停价/实时封单缺失"
            return False, st.fill_reject_reason

        strict_path = self._strict_path_for(st) if self.factor_union_active else None
        first_bucket = classify_first_time_bucket_hhmm(
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        current_queue_amount = float(st.bid_vol) * float(st.upper_limit)
        if planned_buy_amount is None:
            if self.live_order:
                try:
                    capacity = self._resolve_order_capacity(st)
                except Exception as exc:
                    st.fill_probability = 0.0
                    st.fill_reliable = False
                    st.fill_matched_source = "none"
                    st.fill_sample_count = 0
                    st.fill_min_group_samples = self.fill_estimator.min_group_samples
                    st.fill_reject_reason = f"无法按真实账户计算计划买入金额:{exc}"
                    return False, st.fill_reject_reason
                planned_buy_amount = capacity.actual_amount
            else:
                # 非下单观察模式没有实时账户，只用于展示成交模型，不参与真实BUY。
                planned_buy_amount = self.default_fill_planned_amount
        if float(planned_buy_amount or 0.0) <= 0:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_sample_count = 0
            st.fill_min_group_samples = self.fill_estimator.min_group_samples
            st.fill_reject_reason = "真实计划买入金额或可下单股数为0"
            return False, st.fill_reject_reason
        fd_ratio = self._fd_amount_to_circ_mv(st)
        abnormal_threshold = float(
            self.config.get("fill_model", {}).get(
                "fd_amount_abnormal_ratio_threshold", 1.0
            )
        )
        if fd_ratio > abnormal_threshold:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_sample_count = 0
            st.fill_min_group_samples = self.fill_estimator.min_group_samples
            st.fill_reject_reason = (
                f"封单市值比{fd_ratio:.2%}超过异常阈值{abnormal_threshold:.2%}"
            )
            return False, st.fill_reject_reason

        try:
            result = self.fill_estimator.estimate(
                ts_code=st.ts_code,
                trade_date=today_beijing().strftime("%Y%m%d"),
                limit_times=1,
                board_type="multi_open",
                first_time_bucket=first_bucket,
                market_sentiment_level="strong",
                market_segment=st.market_segment,
                segment_market_sentiment_level=self._segment_sentiment_level(
                    st.market_segment
                ),
                circ_mv=float(st.circ_mv),
                current_queue_amount=current_queue_amount,
                planned_buy_amount=float(planned_buy_amount),
            )
        except Exception as exc:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_sample_count = 0
            st.fill_min_group_samples = self.fill_estimator.min_group_samples
            st.fill_reject_reason = f"成交概率复算失败:{exc}"
            return False, st.fill_reject_reason

        source = str(result.get("matched_source", "none"))
        probability = float(result.get("fill_probability", 0.0) or 0.0)
        sample_count = int(result.get("sample_count", 0) or 0)
        reliable = self.fill_estimator.result_is_reliable(result)
        st.fill_probability = probability
        st.fill_reliable = reliable
        st.fill_matched_source = source
        st.fill_sample_count = sample_count
        st.fill_min_group_samples = self.fill_estimator.min_group_samples
        st.fill_planned_buy_amount = float(planned_buy_amount)
        st.fill_estimated_turnover_amount = float(
            result.get("estimated_turnover_amount", 0.0) or 0.0
        )
        st.fill_current_queue_amount = current_queue_amount
        if not reliable:
            if source == "none":
                st.fill_reject_reason = "成交概率没有历史匹配"
            else:
                st.fill_reject_reason = (
                    f"成交概率历史分组样本{sample_count}个，低于可靠性下限"
                    f"{self.fill_estimator.min_group_samples}个"
                )
            return False, st.fill_reject_reason
        if probability < self.min_fill_probability:
            st.fill_reject_reason = (
                f"成交概率{probability:.1%}低于回测阈值"
                f"{self.min_fill_probability:.1%}"
            )
            return False, st.fill_reject_reason
        st.fill_reject_reason = ""
        return True, "通过回测同口径成交概率门"

    def _update_states(self, quotes: dict) -> None:
        hhmm = now_hhmm()
        fallback_count = 0
        qmt_limit_count = 0
        skipped_no_price = 0
        for ts_code, snap in quotes.items():
            name = self.name_map.get(ts_code, "")
            qmt_limit = float(snap.upper_limit or 0.0)
            est_limit = estimate_upper_limit(ts_code, float(snap.pre_close or 0.0), name)
            if qmt_limit > 0:
                qmt_limit_count += 1
            elif est_limit > 0:
                fallback_count += 1
            # 存储/下单用涨停价：优先 QMT（交易所口径），缺失时用估算
            upper_limit = qmt_limit if qmt_limit > 0 else est_limit
            if upper_limit <= 0 or snap.last_price <= 0:
                skipped_no_price += 1
                continue
            # 封板判定：现价贴近【QMT涨停价】或【昨收估算涨停价（已验证准确）】任一即算。
            # 不再单信 QMT——QMT 涨停价偶有缺失/滞后/不准，会漏掉真封板的票。
            at_limit = (
                (qmt_limit > 0 and abs(snap.last_price - qmt_limit) < 0.015)
                or (est_limit > 0 and abs(snap.last_price - est_limit) < 0.015)
            )

            if ts_code not in self.states:
                self.states[ts_code] = StockState(
                    ts_code=ts_code,
                    name=name,
                    market_segment=classify_market_segment(ts_code),
                    upper_limit=upper_limit,
                    circ_mv=float(self.circ_mv_map.get(ts_code, 0.0) or 0.0),
                    pre_close=float(snap.pre_close or 0.0),
                    open_price=float(getattr(snap, "open_price", 0.0) or 0.0),
                    session_high_price=float(
                        getattr(snap, "high_price", 0.0) or snap.last_price or 0.0
                    ),
                    session_low_price=float(
                        getattr(snap, "low_price", 0.0) or snap.last_price or 0.0
                    ),
                    cumulative_amount=float(getattr(snap, "amount", 0.0) or 0.0),
                    previous_day_amount_yuan=float(
                        self.previous_day_amount_map.get(ts_code, 0.0) or 0.0
                    ),
                )
            st = self.states[ts_code]
            st.upper_limit = upper_limit
            st.last_price = float(snap.last_price or 0.0)
            st.pre_close = float(snap.pre_close or st.pre_close or 0.0)
            st.open_price = float(
                getattr(snap, "open_price", 0.0) or st.open_price or 0.0
            )
            raw_high = float(
                getattr(snap, "high_price", 0.0) or st.last_price or 0.0
            )
            if raw_high > 0:
                st.session_high_price = max(st.session_high_price, raw_high)
            raw_low = float(
                getattr(snap, "low_price", 0.0) or st.last_price or 0.0
            )
            if raw_low > 0:
                st.session_low_price = (
                    raw_low
                    if st.session_low_price <= 0
                    else min(st.session_low_price, raw_low)
                )
            st.cumulative_amount = max(
                float(st.cumulative_amount or 0.0),
                float(getattr(snap, "amount", 0.0) or 0.0),
            )
            st.previous_day_amount_yuan = float(
                self.previous_day_amount_map.get(
                    ts_code, st.previous_day_amount_yuan
                ) or 0.0
            )
            # ST/风险警示识别（对齐回测口径 ~is_st，2026-07-23 春兴精工废单事故）：
            # ①名字带ST/退；②QMT真实涨停幅≈5%（名字缓存可能不带前缀，这次就栽在名字上，
            # 涨停幅是交易所口径、不会骗人）。命中任一即嫌疑。
            pre_c = float(snap.pre_close or 0.0)
            name_st = ("ST" in str(name).upper()) or ("退" in str(name))
            ratio_st = bool(qmt_limit > 0 and pre_c > 0 and 0.04 <= (qmt_limit / pre_c - 1.0) <= 0.06)
            st.st_suspect = name_st or ratio_st
            if upper_limit < MIN_D_VALID_LIMIT_PRICE:
                st.invalid_price_ticks += 1
                if st.invalid_price_ticks <= MAX_D_INVALID_PRICE_TICKS:
                    self.logger.warning(
                        "[PRICE GUARD] %s %s 涨停价异常%.2f，连续%d/%d轮，等待下一轮行情复核",
                        ts_code,
                        name,
                        upper_limit,
                        st.invalid_price_ticks,
                        MAX_D_INVALID_PRICE_TICKS,
                    )
            else:
                st.invalid_price_ticks = 0
            st.circ_mv = float(self.circ_mv_map.get(ts_code, st.circ_mv) or 0.0)

            if at_limit:
                # 当前封在涨停（炸板历史不影响：只看此刻是否封板）
                if not st.ever_sealed:
                    st.ever_sealed = True
                    st.first_seal_hhmm = hhmm
                if not st.was_sealed:
                    # 非涨停 → 涨停：记录重封时间
                    st.last_seal_hhmm = hhmm
                    st.last_reseal_scan_round = self.scan_round + 1
                st.was_sealed = True
                # 更新封单量（涨停买一量，单位：股）
                if snap.bid_volumes:
                    st.bid_vol = snap.bid_volumes[0]
            else:
                if st.was_sealed:
                    # 涨停 → 非涨停：炸板
                    st.previous_seal_to_break_minutes = trading_minutes_between(
                        st.last_seal_hhmm, hhmm
                    )
                    st.last_break_hhmm = hhmm
                    st.last_break_price = float(st.last_price or 0.0)
                    st.open_times_today += 1
                st.was_sealed = False
                st.bid_vol = 0

        if not self.limit_price_fallback_logged and quotes:
            self.limit_price_fallback_logged = True
            self.logger.info(
                "涨停价口径: QMT直接给出=%d，按昨收价估算=%d，缺价格跳过=%d",
                qmt_limit_count,
                fallback_count,
                skipped_no_price,
            )

    # ── 轮询 ─────────────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        batches = self._batches()
        if not batches:
            return
        if strategy_d_checkpoint_recovery_block_path(self.checkpoint_path).exists():
            self.checkpoint_io_degraded = True
            if not self.checkpoint_io_reason:
                self.checkpoint_io_reason = "检测到尚未由新READY解除的恢复阻断标记"
        # 先原子摧毁上一轮READY检查点。若进程在本轮扫描中途退出，新进程只能看到
        # SCAN_IN_PROGRESS，绝不会拿上一轮状态跨过一段未知行情继续计数。
        if not self._invalidate_checkpoint(
            D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
            f"第{self.scan_round + 1}轮全市场扫描进行中",
        ):
            self._enter_checkpoint_io_degraded(
                "无法使上一轮D检查点原子切换为SCAN_IN_PROGRESS"
                + (
                    f":{self._last_checkpoint_io_error}"
                    if self._last_checkpoint_io_error
                    else ""
                )
            )
        updated_count = 0
        failures: list[str] = []
        for batch in batches:
            try:
                quotes = self.broker.get_full_tick(batch)
            except Exception as e:
                self.logger.warning("get_full_tick 异常: %s", e)
                failures.append(str(e))
                continue
            if not quotes:
                failures.append(f"空行情批次({len(batch)}只)")
                continue
            requested_codes = {str(code) for code in batch}
            returned_codes = {str(code) for code in quotes}
            missing_codes = requested_codes.difference(returned_codes)
            unexpected_codes = returned_codes.difference(requested_codes)
            if missing_codes or unexpected_codes:
                failures.append(
                    "行情批次成分不完整"
                    f"(请求{len(requested_codes)}/返回{len(returned_codes)}/"
                    f"缺失{len(missing_codes)}/意外{len(unexpected_codes)})"
                )
                continue
            updated_count += len(quotes)
            self._update_states(quotes)
        if failures:
            self.path_integrity_failed = True
            self.path_integrity_reason = (
                f"第{self.scan_round + 1}轮有{len(failures)}/{len(batches)}个批次未完整返回；"
                "无法证明全市场09:30起封板/炸板路径完整"
            )
            self.logger.error("D完整路径已永久失效：%s", self.path_integrity_reason)
            if not self._path_failure_notified:
                self._path_failure_notified = True
                try:
                    notify(
                        "buy_result",
                        "⛔ D实时路径不完整，今日停止开仓",
                        f"{self.path_integrity_reason}。为与回测严格对齐，"
                        "今日D只保留监控日志，不会买入。",
                        level="critical",
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
        self.scan_round += 1
        self.last_scan_updated_count = updated_count
        self.logger.info("完成全市场扫描: round=%d updated=%d states=%d", self.scan_round, updated_count, len(self.states))
        # 历史D只取strong，不含very_strong；实时代理必须同时满足上下界。
        sealed = self.sealed_ever_count
        if (
            not self.factor_union_active
            and not self._strong_notified
            and self._sentiment_passes()
        ):
            self._strong_notified = True
            self.logger.warning(
                "📈 情绪进入D历史strong代理区间：当前封板%d只(%d~%d)。",
                sealed,
                self.sentiment_current_min,
                self.sentiment_current_max,
            )
            try:
                notify(
                    "buy_result",
                    "📈 D满足开仓条件",
                    f"全市场当前封板{sealed}只，位于D历史strong代理区间"
                    f"{self.sentiment_current_min}~{self.sentiment_current_max}，"
                    "且不属于very_strong；"
                    f"14:00后将扫描首板回封发出买入信号。",
                )
            except Exception as exc:
                self.logger.warning("情绪转强推送失败：%s", exc)
        if self.path_integrity_failed:
            self._invalidate_checkpoint(
                D_CHECKPOINT_STATUS_CLOSED,
                self.path_integrity_reason or "D全市场路径已失效",
            )
            return

        if self.checkpoint_io_degraded:
            # I/O降级期间内存路径仍连续，但必须先把本轮完整状态持久化并解除阻断，
            # 才能重新判断BUY。这样临时锁不会关闭全天D，也不会让陈旧READY被恢复。
            if self._save_ready_checkpoint() and self._clear_checkpoint_io_degraded():
                self._check_and_fire()
                if not self._save_ready_checkpoint():
                    self._enter_checkpoint_io_degraded(
                        "D恢复信号判断后未能保存最新READY"
                        + (
                            f":{self._last_checkpoint_io_error}"
                            if self._last_checkpoint_io_error
                            else ""
                        )
                    )
            else:
                self.logger.warning(
                    "D检查点I/O仍处于降级保护，本轮全市场行情虽完整，但不判断BUY；"
                    "下一轮继续尝试生成新的原子READY。"
                )
            return

        self._check_and_fire()
        if not self._save_ready_checkpoint():
            self._enter_checkpoint_io_degraded(
                "D本轮完整路径未能保存为READY"
                + (
                    f":{self._last_checkpoint_io_error}"
                    if self._last_checkpoint_io_error
                    else ""
                )
            )

    def status_line(self) -> str:
        hhmm = now_hhmm()
        watching = sum(1 for st in self.states.values()
                       if st.watch_alerted and not st.buy_signaled)
        bought = sum(1 for st in self.states.values() if st.buy_signaled)
        order_text = "无"
        if self.session_order_details:
            detail_parts = []
            for order_id, detail in self.session_order_details.items():
                detail_parts.append(
                    f"{detail.get('ts_code', '')}({detail.get('shares', 0)}股 order_id={order_id})"
                )
            order_text = ";".join(detail_parts)
        elif self.order_locked_ts_code:
            order_text = f"{self.order_locked_ts_code}(已锁定)"
        if self._sentiment_passes():
            sentiment = f"strong代理({self.sealed_ever_count}只)"
        elif self.sealed_ever_count < self.sentiment_current_min:
            sentiment = (
                f"不足({self.sealed_ever_count}只，需>="
                f"{self.sentiment_current_min})"
            )
        else:
            sentiment = (
                f"very_strong代理({self.sealed_ever_count}只，D上限="
                f"{self.sentiment_current_max})"
            )
        return (
            f"[{hhmm_to_str(hhmm)}] "
            f"扫过{len(self.states)}只 | {sentiment} | "
            f"观察={watching} 买入={bought} | D委托/持仓={order_text} | 全市场扫描轮次={self.scan_round}"
        )

    def _wait_without_open_scan(self) -> None:
        """已有有效委托/持仓后不再跑开仓扫描，只等待撤单窗口或退出。"""

        if self.position_opened and not self.waiting_order_only:
            self.logger.warning(
                "D已形成持仓，停止开仓扫描；后续由主守护进程账户心跳和平仓检查接管。"
            )
            print("D已形成持仓，停止开仓扫描；后续由主守护进程账户心跳和平仓检查接管。")
            return

        if self.waiting_order_only:
            self.logger.warning(
                "D已有有效委托/部分成交，停止新的开仓扫描；持续确认真实成交，"
                "%s撤未成残单。",
                hhmm_to_str(CANCEL_HHMM),
            )
            print(
                "D已有有效委托/部分成交，停止新的开仓扫描；"
                f"持续确认真实成交，{hhmm_to_str(CANCEL_HHMM)}撤未成残单。"
            )
            while now_hhmm() < CANCEL_HHMM:
                if self._reconcile_active_d_orders_once():
                    self.logger.warning(
                        "D活动委托已全部成交并写入策略持仓，成交确认生命周期完成。"
                    )
                    return
                time.sleep(ORDER_FILL_POLL_INTERVAL_SEC)
            self.cancel_all_d_orders()

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        session_start_hhmm = now_hhmm()
        if self.original_session_start_hhmm <= 0:
            self.original_session_start_hhmm = session_start_hhmm
        if not self._signal_scan_is_open(session_start_hhmm):
            reason = (
                f"当前时间{hhmm_to_str(session_start_hhmm)}已超过正式发布最后信号分钟"
                f"{hhmm_to_str(self.factor_signal_clock.last_signal_hhmm)}"
            )
            self.logger.warning(
                "[D SCAN WINDOW CLOSED] %s；不启动全市场快照、QMT分钟线回补或"
                "新开仓判断，之后不可能形成与发布规则一致的新D信号。",
                reason,
            )
            self._invalidate_checkpoint(
                D_CHECKPOINT_STATUS_CLOSED,
                f"D发布信号窗口结束:{reason}",
            )
            return
        resumable_in_memory_path = (
            self.scan_round > 0
            and bool(self.states)
            and not self.path_integrity_failed
        )
        self.setup()

        # 正式因子D本来就用QMT已完成1m收盘复刻回测事件。晚启动时先尝试原子
        # 检查点；检查点因代码/配置变化或过期不可用时，再从QMT回补09:30至当前
        # 的完整分钟序列。绝不再用单张午后快照伪造早盘封板/炸板路径。
        history_ready, history_reason = self._prepare_intraday_history(
            session_start_hhmm,
            resumable_in_memory_path=resumable_in_memory_path,
        )
        if not history_ready:
            reason = f"D监控于{hhmm_to_str(session_start_hhmm)}启动；{history_reason}，今日禁止D开仓"
            self.logger.error(reason)
            print(f"[D跳过] {reason}")
            try:
                notify(
                    "buy_result",
                    "⛔ D日内路径无法恢复",
                    reason,
                    level="critical",
                )
            except Exception as exc:
                self.logger.warning("D日内路径恢复阻断推送失败: %s", exc)
            return
        if not intraday_history_is_complete(session_start_hhmm):
            if self._restored_from_checkpoint:
                self.logger.warning(
                    "D监控已安全恢复原子检查点：已保留%d轮、%d只逐票状态；%s。"
                    "下一轮将从这些状态继续识别真实封板/炸板转换。",
                    self.scan_round,
                    len(self.states),
                    history_reason,
                )
            elif self.minute_history_recovery_required:
                self.logger.warning(
                    "D监控晚启动：%s。下一轮先完整扫描股票宇宙，再按日内最高价定位"
                    "全部触板目标并逐只认证09:30至当前1m序列；认证完成前不产生BUY。",
                    history_reason,
                )
            else:
                self.logger.warning(
                    "D监控使用同一对象内存续跑：已保留%d轮、%d只状态，"
                    "不使用重启后快照伪造早盘路径。",
                    self.scan_round, len(self.states),
                )

        # ── 串行单仓检测：券商仍有任何旧策略仓就直接退出，不做D ─────────────
        occupied, desc = check_strategy_position_occupied(self.broker if self.live_order else None)
        if occupied:
            msg = (
                f"\n{'='*55}\n"
                f"  [跳过] 今日检测到旧策略仓，D策略不启动\n"
                f"  持仓: {desc}\n"
                f"  原因: 已取消衔接开仓；券商确认实际清仓前禁止D买入\n"
                f"{'='*55}\n"
            )
            print(msg)
            self.logger.info("旧策略仓未实际清空，D监控跳过: %s", desc)
            return

        while now_hhmm() < self.monitor_start_hhmm:
            remaining_min = (self.monitor_start_hhmm - now_hhmm())
            print(f"等待开盘... 距{hhmm_to_str(self.monitor_start_hhmm)}还有约{remaining_min}分钟")
            time.sleep(60)

        batches = self._batches()
        print(
            f"\n开始扫描 — {len(self.universe)}只股票，"
            f"{len(batches)}批x{POLL_BATCH_SIZE}，"
            f"每轮扫完整市场后等待{POLL_INTERVAL_SEC}s\n"
            + (
                f"  真实新回封 → 匹配{len(self.factor_profiles)}条发布if条件 "
                f"| 最后信号分钟={hhmm_to_str(self.factor_signal_clock.last_signal_hhmm)} "
                "| 14:55 → 自动撤单\n"
                if self.factor_union_active
                else "  09:35 → 观察提醒  |  14:00 → 买入信号  |  14:55 → 自动撤单\n"
            )
        )

        try:
            while self._signal_scan_is_open(now_hhmm()):
                tracking_allowed, tracking_reason = self._tracking_gate_allows_monitor()
                if not tracking_allowed:
                    self.logger.info(
                        "[D TRACKING STOPPED] 其他策略候选已成交或路径继续门禁失效，"
                        "停止D只读扫描：%s",
                        tracking_reason,
                    )
                    self._invalidate_checkpoint(
                        D_CHECKPOINT_STATUS_CLOSED,
                        f"D只读扫描终止:{tracking_reason}",
                    )
                    return
                self.poll_once()
                print(self.status_line())
                if self.position_opened or self.waiting_order_only:
                    self._wait_without_open_scan()
                    return
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n用户中断，执行撤单流程...")

        self.cancel_all_d_orders()
        if self.factor_union_active and now_hhmm() < CANCEL_HHMM:
            close_reason = (
                "D发布信号窗口正常结束；最后信号分钟="
                f"{hhmm_to_str(self.factor_signal_clock.last_signal_hhmm)}，"
                "已停止全市场快照和QMT分钟线查询"
            )
            self.logger.warning("[D SCAN WINDOW CLOSED] %s。", close_reason)
        else:
            close_reason = "D当日监控已到撤单边界并正常结束"
        self._invalidate_checkpoint(
            D_CHECKPOINT_STATUS_CLOSED,
            close_reason,
        )
        self._print_summary()

    def _print_summary(self) -> None:
        watched = [st for st in self.states.values() if st.watch_alerted and not st.buy_signaled]
        bought = [st for st in self.states.values() if st.buy_signaled]
        print(f"\n=== 今日策略D监控完毕 ===")
        print(f"观察提醒: {len(watched) + len(bought)} 次 | 买入信号: {len(bought)} 次")
        for st in bought:
            order_str = f"order_id={st.order_id}" if st.order_id else "仅提醒"
            source = "正式因子回封" if self.factor_union_active else "14:00后真实回封"
            strict_path = self._strict_path_for(st) if self.factor_union_active else None
            reseal_hhmm = (
                strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
            )
            open_times = strict_path.open_times if strict_path else st.open_times_today
            print(f"  [BUY/{source}] {st.ts_code} {st.name} "
                  f"重封={hhmm_to_str(reseal_hhmm)} 炸板={open_times}次 {order_str}")
        print(f"信号记录: {self.signal_csv}")
        self.logger.info("监控结束 观察=%d 买入=%d", len(watched) + len(bought), len(bought))

    def _save_signals(self) -> None:
        try:
            pd.DataFrame(self.signal_records).to_csv(self.signal_csv, index=False)
        except Exception as e:
            self.logger.warning("保存信号CSV失败: %s", e)

# ── 入口 ─────────────────────────────────────────────────────────────────────

def build_broker(config: dict, live_order: bool) -> Any:
    try:
        from src.qmt_broker_factory import create_qmt_adapter
        from src.qmt_single_owner import assert_standalone_qmt_allowed

        assert_standalone_qmt_allowed(
            PROJECT_ROOT,
            caller="monitor_strategy_d_intraday.py独立入口",
        )
        adapter = create_qmt_adapter(config.get("broker", {}))
        adapter.connect()
        mode = "实盘" if live_order else "行情"
        print(f"QMT已连接（{mode}模式）。")
        return adapter
    except Exception as e:
        if live_order:
            raise
        print(f"[警告] QMT连接失败（{e}），将无法获取实时行情。")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="策略D盘中监控")
    parser.add_argument("--live-order", action="store_true", help="实盘下单（默认仅提醒）")
    parser.add_argument("--dry-run", action="store_true", help="打印配置后退出")
    parser.add_argument("--start-hhmm", type=int, default=MONITOR_START_HHMM,
                        help=f"开始扫描时间，默认{MONITOR_START_HHMM}")
    args = parser.parse_args()

    if args.live_order:
        raise RuntimeError(
            "策略D独立进程实盘下单入口已退役；"
            "请由trading_daemon内嵌监控器经统一交易意图和唯一串行QMT通道执行。"
        )

    today_str = today_beijing().strftime("%Y%m%d")
    signal_dir = PROJECT_ROOT / "reports" / "strategy_d"
    signal_dir.mkdir(parents=True, exist_ok=True)
    signal_csv = signal_dir / f"intraday_signals_{today_str}.csv"

    logger = setup_logger(
        log_dir=PROJECT_ROOT / "logs",
        log_file=f"strategy_d_monitor_{today_str}.log",
        level="INFO",
    )
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    allowed_segments = configured_allowed_segments(config)
    position_pct = configured_position_pct(config)
    min_open_times = configured_min_open_times(config)
    max_open_times = configured_max_open_times(config)
    preferred_open_times = configured_preferred_open_times(config)
    min_fill_probability = configured_min_fill_probability(config)
    first_time_buckets = configured_first_time_buckets(config)
    tail_reseal_hhmm = configured_tail_reseal_hhmm(config)
    sentiment_min, sentiment_max = configured_sentiment_bounds(config)
    factor_release_path = configured_factor_release_path(config)
    factor_release = load_factor_release(factor_release_path)
    factor_release_active = release_uses_factor_union(factor_release)

    if args.dry_run:
        print("=== 策略D监控配置 ===")
        print(
            f"  D发布版本: {factor_release.get('release_id', '')} | "
            f"模式={factor_release.get('strategy_mode', '')} | "
            f"if条件数={len(factor_release.get('profiles', []))}"
        )
        if factor_release_active:
            print("  正式D条件: 当前月度冻结因子条件（旧D时间/情绪/炸板次数门不参与）")
            for profile in factor_release.get("profiles", []):
                conditions = " AND ".join(
                    f"{name}={value}"
                    for name, value in profile.get("conditions", {}).items()
                )
                print(f"    {profile.get('profile_id', '')}: {conditions}")
            print(
                "  因子排序口径: 当日最早合格回封；同一分钟优先炸板2次，再按代码排序"
            )
            print(
                "  公共安全门: 首板、非ST、当前封板、真实新回封、09:30~14:54、"
                "成交概率可靠"
            )
        else:
            print(
                f"  情绪阈值: 全市场当前封板涨停数 {sentiment_min}~{sentiment_max} "
                "(代理历史strong，不含very_strong)"
            )
            print(
                f"  D排序口径: 优先炸板{preferred_open_times}次，再按实时封单金额 / "
                "流通市值(fd_amount_to_circ_mv)降序"
            )
            print(f"  炸板次数: {min_open_times}~{max_open_times}（multi_open）")
            print(
                f"  首次封板时段: {','.join(sorted(first_time_buckets))} | "
                f"最后真实回封>={hhmm_to_str(tail_reseal_hhmm)}"
            )
        print(
            f"  成交概率: >={min_fill_probability:.0%}且历史匹配可靠，"
            "实时复算失败则禁止开仓"
        )
        print(f"  允许市场分段: {','.join(sorted(allowed_segments))}")
        print(f"  目标开仓仓位: {position_pct:.1%}")
        print("  补偿机制: 已取消，只尝试D排序第1名")
        print(f"  扫描开始: {hhmm_to_str(args.start_hhmm)}")
        if factor_release_active:
            print("  因子信号: 09:30~14:54发生真实新回封时即时匹配正式条件")
        else:
            print(
                f"  观察提醒: {hhmm_to_str(WATCH_START_HHMM)} 起（只提醒，不升级买入）"
            )
            print(
                f"  买入信号: {hhmm_to_str(SIGNAL_START_HHMM)} 起发生真实回封才允许BUY"
            )
        print(
            f"  重启保护: {hhmm_to_str(D_LATEST_COMPLETE_HISTORY_START_HHMM)}后才启动时，"
            "先恢复同版本原子检查点；检查点不可用则严格回补QMT 09:30至当前1m，"
            "逐根认证完成前禁止D开仓"
        )
        print(f"  撤单时间: {hhmm_to_str(CANCEL_HHMM)}")
        print(f"  实盘下单: {'是' if args.live_order else '否（仅提醒）'}")
        print(f"  信号输出: {signal_csv}")
        return

    broker = build_broker(config, args.live_order)
    if broker is None:
        print("[错误] 策略D盘中监控必须连接 QMT 实时行情；当前无法获取行情，退出。")
        import sys; sys.exit(1)

    monitor = StrategyDMonitor(
        broker=broker,
        live_order=args.live_order,
        logger=logger,
        signal_csv=signal_csv,
        monitor_start_hhmm=args.start_hhmm,
        allowed_segments=allowed_segments,
        position_pct=position_pct,
        config=config,
    )
    try:
        monitor.run()
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                pass  # 可以忽略：退出时断开券商连接失败无影响


if __name__ == "__main__":
    main()
