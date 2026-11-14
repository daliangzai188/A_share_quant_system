"""D盘中监控 StrategyDMonitor 的市场环境方法（2026-11-15 项目优化：从 scripts/monitor_strategy_d_intraday.py 原样搬来，逻辑未改）。

情绪、封板/炸板统计、严格分钟路径与因子取值。StrategyDMonitor 继承 MarketContextMixin；方法里用到的属性都在 StrategyDMonitor 的 __init__/setup 里建立。
"""
from __future__ import annotations

import json
from typing import Any

from src.utils.time_utils import now_beijing, today_beijing
from src.data_cleaner import DataCleaner
from src.strategy_d_factor_rules import factor_values_from_raw, matching_profile_ids, trading_minutes_between
from src.strategy_d_minute_alignment import StrictMinutePath, replay_completed_minute_path
from src.qmt_adapter import MAX_SINGLE_QUOTE_SUBSCRIPTIONS
from src.strategy_d_spec import live_sentiment_is_historical_strong

from src.strategy_d_monitor.support import (
    CANCEL_HHMM,
    MONITOR_START_HHMM,
    StockState,
    now_hhmm,
    hhmm_to_str,
    is_d_market_context_eligible,
)


class MarketContextMixin:
    """市场环境：情绪、封板/炸板统计、严格分钟路径与因子取值。"""

    # ── 状态更新 ──────────────────────────────────────────────────────────────

    def _eligible_market_context_state(self, state: StockState) -> bool:
        """D市场广度只统计与历史母池一致的非ST首板。"""

        return is_d_market_context_eligible(
            state,
            yesterday_limit_codes=self.yesterday_limit_codes,
            allowed_segments=self.allowed_segments,
        )

    @property
    def sealed_ever_count(self) -> int:
        """全市场【当前正封在涨停】的家数（瞬时快照，每轮刷新）。

        与回测口径一致：回测数的是收盘封住的涨停(limit==U)，临近收盘时"当前封板数"≈"收盘涨停数"。
        只看每只票最近一次轮询的封板状态(was_sealed)，炸板打开的不计、回封的计入。
        """
        return sum(
            1
            for st in self.states.values()
            if self._eligible_market_context_state(st) and st.was_sealed
        )

    def _sentiment_passes(self) -> bool:
        return live_sentiment_is_historical_strong(
            self.sealed_ever_count,
            minimum=self.sentiment_current_min,
            maximum=self.sentiment_current_max,
        )

    def _segment_sentiment_level(self, segment: str) -> str:
        stock_count = int(self.segment_stock_counts.get(segment, 0))
        limit_count = sum(
            1
            for state in self.states.values()
            if (
                self._eligible_market_context_state(state)
                and state.was_sealed
                and state.market_segment == segment
            )
        )
        return DataCleaner.classify_segment_sentiment(
            limit_count, stock_count
        )

    @property
    def market_ever_sealed_count(self) -> int:
        return sum(
            1
            for state in self.states.values()
            if self._eligible_market_context_state(state) and state.ever_sealed
        )

    @property
    def market_break_event_count(self) -> int:
        return sum(
            int(state.open_times_today)
            for state in self.states.values()
            if self._eligible_market_context_state(state)
        )

    def _same_segment_reseal_context(self, segment: str) -> tuple[int, int, float]:
        same = [
            state for state in self.states.values()
            if (
                self._eligible_market_context_state(state)
                and state.market_segment == segment
            )
        ]
        active = sum(1 for state in same if state.was_sealed)
        ever = sum(1 for state in same if state.ever_sealed)
        return active, ever, active / ever if ever > 0 else 0.0

    def _strict_path_for(self, st: StockState) -> StrictMinutePath | None:
        return self.strict_minute_paths.get(st.ts_code)

    def _refresh_strict_minute_paths(self) -> bool:
        """为当前可能入选的股票批量重建回测同口径分钟路径；失败即不产生BUY。"""

        current_hhmm = now_hhmm()
        if self.strict_minute_refresh_hhmm == current_hhmm:
            return not self.strict_minute_refresh_error

        targets = [
            state
            for state in self.states.values()
            if (
                self._eligible_market_context_state(state)
                and (
                    state.ever_sealed
                    or state.session_high_price >= state.upper_limit - 0.015
                )
            )
        ]
        self.strict_minute_paths = {}
        self.strict_minute_refresh_error = ""
        if not targets:
            self.strict_minute_refresh_hhmm = current_hhmm
            # 首轮全市场快照完整，且所有股票日内最高价都未触及涨停时，目标集为空
            # 本身就是可验证结论，不需要为五千余只股票下载无关1m序列。
            self._mark_minute_history_recovery_complete(
                target_count=0,
                current_hhmm=current_hhmm,
            )
            return True
        if self.broker is None or not hasattr(self.broker, "get_minute_bars"):
            self.strict_minute_refresh_error = "券商行情接口不支持QMT一分钟K线"
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        now = now_beijing()
        trade_date = today_beijing().strftime("%Y%m%d")
        codes = [state.ts_code for state in targets]
        try:
            # QMT单批实时分钟订阅安全上限是50只。强市非ST首板触板数
            # 可能超过50，不能因为修正了市场广度口径就整轮fail-closed。
            # 按50只分批查询；适配器会先释放上批订阅，已返回的K线保留在本轮内存。
            raw_paths: dict[str, list[dict[str, Any]]] = {}
            for offset in range(0, len(codes), MAX_SINGLE_QUOTE_SUBSCRIPTIONS):
                batch = codes[offset : offset + MAX_SINGLE_QUOTE_SUBSCRIPTIONS]
                batch_paths = self.broker.get_minute_bars(
                    batch,
                    start_time=trade_date + "093000",
                    end_time=now.strftime("%Y%m%d%H%M%S"),
                )
                if not isinstance(batch_paths, dict):
                    raise RuntimeError(
                        f"一分钟K线返回非法类型{type(batch_paths).__name__}"
                    )
                raw_paths.update(batch_paths)
            missing_codes = set(codes).difference(str(code) for code in raw_paths)
            if missing_codes:
                raise RuntimeError(
                    f"一分钟K线缺少股票:{sorted(missing_codes)[:5]}"
                )
            for state in targets:
                self.strict_minute_paths[state.ts_code] = replay_completed_minute_path(
                    raw_paths.get(state.ts_code, []),
                    limit_price=state.upper_limit,
                    current_hhmm=current_hhmm,
                )
        except Exception as exc:
            self.strict_minute_paths = {}
            self.strict_minute_refresh_error = f"QMT一分钟路径查询/重建失败:{exc}"
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        uncertified = [
            f"{code}:{path.reason}"
            for code, path in self.strict_minute_paths.items()
            if not path.certifiable
        ]
        if uncertified:
            self.strict_minute_refresh_error = (
                f"{len(uncertified)}只候选的一分钟路径不完整，"
                f"示例={uncertified[:3]}"
            )
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        self.strict_minute_refresh_hhmm = current_hhmm
        self._mark_minute_history_recovery_complete(
            target_count=len(self.strict_minute_paths),
            current_hhmm=current_hhmm,
        )
        self.logger.info(
            "[D STRICT 1M] 已按回测分钟收盘口径认证%d只非ST首板触板路径，完成分钟=%s",
            len(self.strict_minute_paths),
            hhmm_to_str(current_hhmm),
        )
        return True

    def _strict_market_context(self, segment: str) -> tuple[int, int, int, int, int]:
        """从同一完成分钟路径计算历史/实盘共用的D市场广度。"""

        eligible = [
            (self.states[code], path)
            for code, path in self.strict_minute_paths.items()
            if (
                code in self.states
                and path.certifiable
                and self._eligible_market_context_state(self.states[code])
            )
        ]
        market_active = sum(int(path.was_sealed) for _, path in eligible)
        market_ever = sum(int(path.ever_sealed) for _, path in eligible)
        market_breaks = sum(int(path.open_times) for _, path in eligible)
        segment_active = sum(
            int(path.was_sealed)
            for state, path in eligible
            if state.market_segment == segment
        )
        segment_ever = sum(
            int(path.ever_sealed)
            for state, path in eligible
            if state.market_segment == segment
        )
        return (
            market_active,
            market_ever,
            market_breaks,
            segment_active,
            segment_ever,
        )

    def _factor_raw_values(
        self,
        st: StockState,
        strict_path: StrictMinutePath | None = None,
    ) -> dict[str, Any]:
        if strict_path is not None:
            (
                market_active,
                market_ever,
                market_breaks,
                segment_active,
                segment_ever,
            ) = self._strict_market_context(st.market_segment)
            segment_rate = segment_active / segment_ever if segment_ever > 0 else 0.0
        else:
            market_active = self.sealed_ever_count
            market_ever = self.market_ever_sealed_count
            market_breaks = self.market_break_event_count
            segment_active, segment_ever, segment_rate = self._same_segment_reseal_context(
                st.market_segment
            )
        event_first_seal_hhmm = (
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        event_last_seal_hhmm = (
            strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
        )
        event_open_times = strict_path.open_times if strict_path else st.open_times_today
        event_last_break_hhmm = (
            strict_path.last_break_hhmm if strict_path else st.last_break_hhmm
        )
        event_last_break_price = (
            strict_path.last_break_close if strict_path else st.last_break_price
        )
        event_previous_hold = (
            strict_path.previous_seal_to_break_minutes
            if strict_path
            else st.previous_seal_to_break_minutes
        )
        event_low = (
            strict_path.pre_signal_low_price
            if strict_path and strict_path.pre_signal_low_price > 0
            else st.session_low_price
        )
        event_amount = (
            strict_path.signal_cumulative_amount
            if strict_path and strict_path.signal_cumulative_amount > 0
            else st.cumulative_amount
        )
        amount_ratio = (
            event_amount / st.previous_day_amount_yuan
            if st.previous_day_amount_yuan > 0
            else float("nan")
        )
        return {
            "signal_hhmm": event_last_seal_hhmm,
            "first_seal_hhmm": event_first_seal_hhmm,
            "open_times_at_signal": event_open_times,
            "first_to_signal_minutes": trading_minutes_between(
                event_first_seal_hhmm, event_last_seal_hhmm
            ),
            "last_break_to_signal_minutes": trading_minutes_between(
                event_last_break_hhmm, event_last_seal_hhmm
            ),
            "previous_seal_to_break_minutes": event_previous_hold,
            "last_break_close_depth_pct": (
                max(st.upper_limit / event_last_break_price - 1.0, 0.0)
                if st.upper_limit > 0 and event_last_break_price > 0
                else float("nan")
            ),
            "open_gap_pct": (
                st.open_price / st.pre_close - 1.0
                if st.open_price > 0 and st.pre_close > 0
                else float("nan")
            ),
            "pre_signal_min_return": (
                event_low / st.pre_close - 1.0
                if event_low > 0 and st.pre_close > 0
                else float("nan")
            ),
            "signal_cumulative_amount_vs_prev_day": amount_ratio,
            "market_ever_sealed_count": market_ever,
            "market_active_sealed_count": market_active,
            "market_seal_rate": market_active / market_ever if market_ever > 0 else 0.0,
            "market_break_event_rate": (
                market_breaks / market_ever if market_ever > 0 else 0.0
            ),
            "market_segment": st.market_segment,
            "same_segment_seal_rate": segment_rate,
            "same_segment_active_sealed_count": segment_active,
            "same_segment_ever_sealed_count": segment_ever,
        }

    def _factor_release_match(
        self, st: StockState, *, require_fresh_reseal: bool
    ) -> tuple[bool, str]:
        if not self.factor_union_active:
            return False, "当前D发布不是因子并集模式"
        if st.st_suspect:
            return False, "ST/风险警示"
        if st.market_segment not in self.allowed_segments:
            return False, f"市场分段{st.market_segment}不在允许范围"
        if st.ts_code in self.yesterday_limit_codes:
            return False, "昨日已涨停，非首板"
        if not st.was_sealed:
            return False, "当前不在涨停封板状态"
        strict_path = self._strict_path_for(st)
        if strict_path is None:
            return False, "尚未取得已完成QMT一分钟路径认证"
        if not strict_path.certifiable:
            return False, f"QMT一分钟路径不可认证:{strict_path.reason}"
        if not strict_path.has_reseal:
            return False, "回测分钟收盘口径尚未形成封板→炸板→回封"
        if not (
            MONITOR_START_HHMM <= strict_path.last_reseal_hhmm < CANCEL_HHMM
        ):
            return False, "回封时间不在可委托窗口"
        if require_fresh_reseal and not strict_path.has_fresh_reseal:
            return False, "不是最新完成分钟刚形成的回测口径回封事件"
        raw = self._factor_raw_values(st, strict_path)
        values = factor_values_from_raw(raw)
        matched = matching_profile_ids(values, self.factor_profiles)
        st.factor_values_json = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        st.matched_factor_profile_ids = ";".join(matched)
        if not matched:
            return False, "未命中任何已发布D因子if条件"
        return True, f"命中D因子条件:{','.join(matched)}"
