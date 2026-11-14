"""D盘中监控 StrategyDMonitor 的选股与触发方法（2026-11-15 项目优化：从 scripts/monitor_strategy_d_intraday.py 原样搬来，逻辑未改）。

基础过滤、排序、开仓门禁、候选校验、观察提醒与买入信号。StrategyDMonitor 继承 SignalMixin；方法里用到的属性都在 StrategyDMonitor 的 __init__/setup 里建立。
"""
from __future__ import annotations

from src.utils.time_utils import now_beijing
from src.strategy_d_spec import (
    D_PREFERRED_OPEN_TIMES,
    classify_first_time_bucket_hhmm,
    common_candidate_rejection_reason,
    d_rank_key,
)

from src.strategy_d_monitor.support import (
    WATCH_START_HHMM,
    SIGNAL_START_HHMM,
    CANCEL_HHMM,
    MONITOR_START_HHMM,
    D_RETRY_TOP_N,
    MIN_D_VALID_LIMIT_PRICE,
    MAX_D_INVALID_PRICE_TICKS,
    StockState,
    now_hhmm,
    hhmm_to_str,
    classify_market_segment,
)


class SignalMixin:
    """选股与触发：基础过滤、排序、开仓门禁、候选校验、观察提醒与买入信号。"""

    # ── 信号检测与分级触发 ────────────────────────────────────────────────────

    def _passes_base_filters(self, ts_code: str, st: StockState) -> bool:
        """通用过滤：首板 + 开板回封 + 当前涨停 + strong情绪 + 非ST。"""
        if st.st_suspect:  # 回测口径 ~is_st：ST/风险警示股从不入D候选池
            if not st.st_suspect_logged:
                st.st_suspect_logged = True
                self.logger.warning(
                    "[D过滤] %s %s 判定为ST/风险警示（名字带ST/退 或 QMT涨停幅≈5%%），"
                    "回测口径(~is_st)从不买这类票，排除出D候选。",
                    ts_code, st.name,
                )
            return False
        if ts_code in self.yesterday_limit_codes:  # 排除2板+
            return False
        if not st.was_sealed:                      # 当前不在涨停
            return False
        common_reason = common_candidate_rejection_reason(
            open_times=st.open_times_today,
            first_seal_hhmm=st.first_seal_hhmm,
        )
        if common_reason:
            return False
        if not self._sentiment_passes():           # 只允许历史strong桶
            return False
        return True

    def _fd_amount_to_circ_mv(self, st: StockState) -> float:
        """实时封单金额/流通市值，复刻D回测 pick_d_candidate 的排序字段。"""
        if st.circ_mv <= 0 or st.upper_limit <= 0:
            return 0.0
        fd_amount = float(st.bid_vol) * float(st.upper_limit)
        return fd_amount / (float(st.circ_mv) * 10000.0)

    def _rank_explain(self, st: StockState) -> str:
        return (
            f"fd_ratio={self._fd_amount_to_circ_mv(st):.4%}/"
            f"成交概率={st.fill_probability:.1%}/"
            f"封单{st.bid_vol / 10000:.1f}万股/"
            f"涨停价{st.upper_limit:.2f}/"
            f"流通市值{st.circ_mv / 10000:.2f}亿/"
            f"炸{st.open_times_today}/"
            f"重封{hhmm_to_str(st.last_seal_hhmm)}"
        )

    def _rank_key(self, st: StockState) -> tuple[int, float, str]:
        """调用D共享排序：先优先炸板2次，再比较实时封单比。"""

        return d_rank_key(
            open_times=st.open_times_today,
            fd_amount_to_circ_mv=self._fd_amount_to_circ_mv(st),
            ts_code=st.ts_code,
        )

    def _entry_gate_allows_buy(self) -> tuple[bool, str]:
        """动态复核D能否入场；门禁异常一律fail-closed但不停止路径采集。"""

        if self.entry_gate is None:
            return True, "未配置外部候选资金门禁"
        try:
            allowed, reason = self.entry_gate()
            normalized_reason = str(reason or ("动态门禁允许" if allowed else "动态门禁阻断"))
        except Exception as exc:
            allowed = False
            normalized_reason = f"动态门禁检查异常:{exc}"
        if not allowed and normalized_reason != self._last_entry_gate_reason:
            self.logger.info(
                "[D TRACKING ONLY] 继续维护09:30起完整路径，暂不产生BUY：%s",
                normalized_reason,
            )
        elif allowed and self._last_entry_gate_reason and normalized_reason != self._last_entry_gate_reason:
            self.logger.warning(
                "[D ENTRY UNLOCKED] 上游候选已不再占用资金；D从本轮起允许按正式条件形成BUY：%s",
                normalized_reason,
            )
        self._last_entry_gate_reason = normalized_reason
        return bool(allowed), normalized_reason

    def _tracking_gate_allows_monitor(self) -> tuple[bool, str]:
        """候选一旦真实成交便结束D只读扫描；异常时停止扫描并fail-closed。"""

        if self.tracking_gate is None:
            return True, "未配置外部路径继续门禁"
        try:
            allowed, reason = self.tracking_gate()
            return bool(allowed), str(reason or "路径继续门禁未说明原因")
        except Exception as exc:
            return False, f"路径继续门禁检查异常:{exc}"

    def _entry_alignment_allows_new_buy(self) -> bool:
        """按运行授权控制D新开仓；历史缺失只影响历史收益认证。

        实盘每一笔仍由因子匹配、完整一分钟路径、真实账户定仓和实时成交概率
        三层门禁复核。历史信号时L2缺失不能被伪造成历史成交，但也不代表今天
        可观测的实时封单和成交空间永远不能形成合格订单。
        """

        allowed = bool(self.runtime_new_buy_enabled)
        if not allowed and not self._fill_alignment_block_notified:
            self._fill_alignment_block_notified = True
            self.logger.error(
                "[D ENTRY ALIGNMENT BLOCK] 当前发布未取得运行新BUY授权；"
                "D只记录路径，不产生BUY。"
            )
        return allowed

    def _check_and_fire_factor_union(self) -> None:
        """用已完成1m路径执行月度发布因子，并明确记录候选筛选结论。"""

        if self.factor_signal_consumed:
            return
        if not self._entry_alignment_allows_new_buy():
            return
        if not self._refresh_strict_minute_paths():
            return
        candidates: list[StockState] = []
        for st in self.states.values():
            if st.buy_signaled:
                continue
            matched, reason = self._factor_release_match(
                st, require_fresh_reseal=True
            )
            if not matched:
                continue
            self.logger.info("[D因子命中] %s %s", st.ts_code, reason)
            candidates.append(st)

        if not candidates:
            completed_hhmm = self.strict_minute_refresh_hhmm
            if completed_hhmm != self.factor_candidate_summary_hhmm:
                self.factor_candidate_summary_hhmm = completed_hhmm
                reseal_buckets = ",".join(
                    sorted(
                        {
                            str(profile.get("conditions", {}).get("reseal_time_bucket", "ANY"))
                            for profile in self.factor_profiles
                            if isinstance(profile, dict)
                            and isinstance(profile.get("conditions", {}), dict)
                        }
                    )
                ) or "ANY"
                # 外层Windows启动器主要展示WARNING级D事件，因此每次进程启动后
                # 第一条候选结论提升为WARNING；后续零候选分钟保留INFO，避免刷屏。
                result_logger = (
                    self.logger.info
                    if self.factor_candidate_summary_announced
                    else self.logger.warning
                )
                self.factor_candidate_summary_announced = True
                result_logger(
                    "[D候选结果] 完成分钟=%s 本分钟新增因子合格=0，"
                    "最终可开仓=0；release=%s 发布回封桶=%s，"
                    "没有股票同时满足发布因子、当前封板和最新完成分钟新回封条件。",
                    hhmm_to_str(completed_hhmm),
                    self.factor_release_id,
                    reseal_buckets,
                )
            return
        # 与历史因子并集一致：先取最早回封；同一轮/分钟优先炸板2次，再按代码稳定排序。
        ranked = sorted(
            candidates,
            key=lambda state: (
                -int(
                    bool(
                        self._strict_path_for(state)
                        and self._strict_path_for(state).open_times
                        == D_PREFERRED_OPEN_TIMES
                    )
                ),
                state.ts_code,
            ),
        )
        candidate = ranked[0]
        candidate_summary = ",".join(
            f"{state.ts_code}({state.matched_factor_profile_ids})"
            for state in ranked
        )
        self.factor_candidate_summary_hhmm = self.strict_minute_refresh_hhmm
        self.logger.warning(
            "[D候选结果] 完成分钟=%s 本分钟新增因子合格=%d：%s；"
            "按冻结排序选择%s，继续复核真实委托金额和成交概率门。",
            hhmm_to_str(self.strict_minute_refresh_hhmm),
            len(ranked),
            candidate_summary,
            candidate.ts_code,
        )
        # 历史账本先选当日最早信号，再判断该挂单能否成交；即使成交门失败，
        # 也不能用事后结果补选更晚回封。因此选中即永久消耗今日D机会。
        self.factor_signal_consumed = True
        self.factor_signal_locked_ts_code = candidate.ts_code
        self.logger.warning(
            "[D FACTOR BUY] release=%s profiles=%s 本轮候选=%d，选择=%s",
            self.factor_release_id,
            candidate.matched_factor_profile_ids,
            len(ranked),
            candidate.ts_code,
        )
        fill_passed, fill_reason = self._refresh_fill_gate(candidate)
        if not fill_passed:
            self.logger.warning(
                "[D FACTOR NO FILL] %s 当日最早if信号未通过成交概率门，"
                "按无补选口径锁定今日D: %s",
                candidate.ts_code,
                fill_reason,
            )
            self.logger.warning(
                "[D候选结果] %s 因子合格但最终可开仓=0；成交概率门失败：%s",
                candidate.ts_code,
                fill_reason,
            )
            return
        valid, invalid_reason = self._validate_buy_candidate(candidate)
        if not valid:
            self.logger.warning(
                "[D FACTOR BUY SKIP] %s 二次复核失败: %s",
                candidate.ts_code,
                invalid_reason,
            )
            self.logger.warning(
                "[D候选结果] %s 因子合格但最终可开仓=0；二次复核失败：%s",
                candidate.ts_code,
                invalid_reason,
            )
            return
        self.logger.warning(
            "[D候选结果] %s 最终可开仓=1；发布因子、完整1m路径、"
            "真实委托金额及可靠成交概率门全部通过，开始提交委托。",
            candidate.ts_code,
        )
        self._fire_buy_signal(candidate)

    def _check_and_fire(self) -> None:
        if self.order_placed:
            return

        # 实盘新BUY必须在总入口再次校验发布认证。即使发布文件被误切回旧模式，
        # 也不能绕过正式因子分支里的成交概率对齐门。
        if self.live_order and not self._entry_alignment_allows_new_buy():
            return

        # 只拦截信号选择/下单，不拦截poll_once中的全市场行情更新和检查点保存。
        # 因此上游任意策略候选失败并过了补仓窗口后，D可以使用同一进程从
        # 09:30连续积累的真实路径继续判断，而不是从午后快照伪造路径。
        entry_allowed, _entry_reason = self._entry_gate_allows_buy()
        if not entry_allowed:
            return

        if self.factor_union_active:
            self._check_and_fire_factor_union()
            return

        hhmm = now_hhmm()
        buy_candidates: list[StockState] = []

        for ts_code, st in self.states.items():
            if st.buy_signaled:
                continue
            if not self._passes_base_filters(ts_code, st):
                continue

            # 14:00后必须发生真实回封。WATCH只提醒，不能绕过回测last_time>=14:00。
            if hhmm >= SIGNAL_START_HHMM:
                common_reason = common_candidate_rejection_reason(
                    open_times=st.open_times_today,
                    first_seal_hhmm=st.first_seal_hhmm,
                    last_seal_hhmm=st.last_seal_hhmm,
                    require_tail_reseal=True,
                )
                if common_reason:
                    continue
                fill_passed, fill_reason = self._refresh_fill_gate(st)
                if not fill_passed:
                    self.logger.info(
                        "[D过滤] %s %s 未通过成交概率门: %s",
                        st.ts_code,
                        st.name,
                        fill_reason,
                    )
                    continue
                buy_candidates.append(st)
                continue

            # ── 场景二：WATCH窗口 → 逐个发提醒 ──────────────────────────────
            if hhmm >= WATCH_START_HHMM and not st.watch_alerted:
                self._fire_watch_alert(st)

        # 有BUY候选：按原始D回测口径排序，只尝试第1名，失败不补偿。
        if not buy_candidates:
            if hhmm >= SIGNAL_START_HHMM:
                self._log_buy_empty_funnel(hhmm)
            return

        scored_all = sorted(buy_candidates, key=self._rank_key, reverse=True)
        scored = scored_all[:D_RETRY_TOP_N]
        rank_info = "  ".join(
            f"{idx + 1}.{s.ts_code}({self._rank_explain(s)})" for idx, s in enumerate(scored)
        )
        self.logger.info(
            "[BUY RANK] 本轮%d只候选，严格对齐D原始回测，只尝试第1名: %s",
            len(scored_all),
            rank_info,
        )
        if len(scored_all) > D_RETRY_TOP_N:
            self.logger.info(
                "[BUY RANK] 第%d名以后不尝试，原因=已取消D补偿机制，仅保留原始回测第1名口径；未尝试数量=%d",
                D_RETRY_TOP_N + 1,
                len(scored_all) - D_RETRY_TOP_N,
            )
        if len(scored) > 1:
            print(f"  [多候选排序] {rank_info}")

        for idx, candidate in enumerate(scored, start=1):
            fd_ratio = self._fd_amount_to_circ_mv(candidate)
            still_valid, invalid_reason = self._validate_buy_candidate(candidate)
            if not still_valid:
                self.logger.warning(
                    "[BUY SKIP] 第%d名 %s 不再符合D策略要求：%s",
                    idx,
                    candidate.ts_code,
                    invalid_reason,
                )
                print(f"  [跳过第{idx}名] {candidate.ts_code} 不再符合D策略要求：{invalid_reason}")
                continue
            self.logger.info(
                "[BUY TRY] 第%d名 fd_ratio=%.4f%%: %s %s  炸板%d次 重封%s 封单%.1f万股 流通市值%.2f亿",
                idx, fd_ratio * 100, candidate.ts_code, candidate.name, candidate.open_times_today,
                hhmm_to_str(candidate.last_seal_hhmm), candidate.bid_vol / 10000, candidate.circ_mv / 10000,
            )
            print(f"  [尝试第{idx}名] {candidate.ts_code} {candidate.name} fd_ratio={fd_ratio:.4%}")
            locked = self._fire_buy_signal(candidate)
            if locked:
                return
            fail_reason = str(getattr(candidate, "last_order_fail_reason", "") or "未形成有效委托")
            self.logger.warning(
                "[BUY FAIL] 第%d名失败：%s；已取消D补偿机制，本轮不再尝试其他候选。", idx, fail_reason
            )
            print(f"  [尝试第{idx}名失败] 原因={fail_reason}，已取消补偿，本轮结束")

        self.logger.warning("[BUY FAIL] 本轮第1名候选未形成有效委托，严格回测口径下不补偿。")
        print("  [本轮结束] 第1名未形成有效委托，严格回测口径下不补偿")

    def _log_buy_empty_funnel(self, hhmm: int) -> None:
        """14:00后无买入候选时打印D实时漏斗，说明卡在哪一层。"""

        current_sealed = [st for st in self.states.values() if st.was_sealed]
        first_board = [st for st in current_sealed if st.ts_code not in self.yesterday_limit_codes]
        opened_once = [st for st in first_board if st.open_times_today >= 1]
        open_times_ok = [
            st for st in opened_once
            if st.open_times_today <= self.max_open_times
        ]
        buy_time_ok = [
            st for st in open_times_ok
            if st.last_seal_hhmm >= self.tail_reseal_hhmm
        ]
        sample = "  ".join(
            f"{st.ts_code}(炸{st.open_times_today},重封{hhmm_to_str(st.last_seal_hhmm)})"
            for st in buy_time_ok[:5]
        ) or "无"
        self.logger.info(
            "[BUY FUNNEL] %s 无D买入候选：当前封板=%d 首板封板=%d 曾炸板回封=%d 炸板次数合规=%d 14:00后真实回封=%d 情绪=%d(允许%d~%d) 样例=%s",
            hhmm_to_str(hhmm),
            len(current_sealed),
            len(first_board),
            len(opened_once),
            len(open_times_ok),
            len(buy_time_ok),
            self.sealed_ever_count,
            self.sentiment_current_min,
            self.sentiment_current_max,
            sample,
        )

    def _validate_buy_candidate(self, st: StockState) -> tuple[bool, str]:
        """每次尝试下单前复核，确保重试候选仍符合D策略实时要求。"""

        segment = classify_market_segment(st.ts_code)
        if segment not in self.allowed_segments:
            return False, f"市场分段{segment}不在允许范围{','.join(sorted(self.allowed_segments))}"
        if st.ts_code in self.yesterday_limit_codes:
            return False, "昨日已涨停，非首板"
        if st.upper_limit < MIN_D_VALID_LIMIT_PRICE:
            if st.invalid_price_ticks < MAX_D_INVALID_PRICE_TICKS:
                return False, (
                    f"涨停价异常{st.upper_limit:.2f}元，连续{st.invalid_price_ticks}/{MAX_D_INVALID_PRICE_TICKS}轮，"
                    "等待下一轮行情复核"
                )
            return False, (
                f"涨停价异常{st.upper_limit:.2f}元，已连续{st.invalid_price_ticks}轮低于"
                f"{MIN_D_VALID_LIMIT_PRICE:.2f}元，本地风控确认拦截"
            )
        if not st.was_sealed:
            return False, "当前不在涨停封板状态"
        if st.st_suspect:
            return False, "ST/风险警示股不允许进入D"
        if self.factor_union_active:
            matched, match_reason = self._factor_release_match(
                st, require_fresh_reseal=False
            )
            if not matched:
                return False, match_reason
            hhmm = now_hhmm()
            if not (MONITOR_START_HHMM <= hhmm < CANCEL_HHMM):
                return False, f"当前{hhmm_to_str(hhmm)}不在D可委托窗口"
            fill_passed, fill_reason = self._refresh_fill_gate(st)
            if not fill_passed:
                return False, fill_reason
            return True, match_reason
        common_reason = common_candidate_rejection_reason(
            open_times=st.open_times_today,
            first_seal_hhmm=st.first_seal_hhmm,
            last_seal_hhmm=st.last_seal_hhmm,
            require_tail_reseal=True,
        )
        if common_reason:
            return False, common_reason
        if not self._sentiment_passes():
            return False, (
                f"当前封板{self.sealed_ever_count}只不在回测strong代理区间"
                f"{self.sentiment_current_min}~{self.sentiment_current_max}"
            )
        if st.circ_mv <= 0:
            return False, "缺少流通市值，无法按D原始fd_amount_to_circ_mv口径排序"
        hhmm = now_hhmm()
        if hhmm < SIGNAL_START_HHMM:
            return False, f"当前{hhmm_to_str(hhmm)}未到D买入时间{hhmm_to_str(SIGNAL_START_HHMM)}"
        fill_passed, fill_reason = self._refresh_fill_gate(st)
        if not fill_passed:
            return False, fill_reason
        return True, "通过D策略实时复核"

    # ── 观察提醒 ──────────────────────────────────────────────────────────────

    def _fire_watch_alert(self, st: StockState) -> None:
        hhmm = now_hhmm()
        st.watch_alerted = True
        msg = (
            f"\n{'─'*55}\n"
            f"  [WATCH] 观察提醒 {hhmm_to_str(hhmm)}\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(st.last_seal_hhmm)}  "
            f"炸板 {st.open_times_today} 次\n"
            f"  → 仅观察：14:00后必须再次真实回封并通过成交概率门才允许买入\n"
            f"{'─'*55}"
        )
        print(msg)
        self.logger.info(
            "[WATCH] %s %s 重封=%s 炸板=%d次",
            st.ts_code, st.name, hhmm_to_str(st.last_seal_hhmm), st.open_times_today,
        )
        self.signal_records.append({
            "signal_time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_type": "WATCH",
            "ts_code": st.ts_code,
            "name": st.name,
            "upper_limit": st.upper_limit,
            "reseal_hhmm": st.last_seal_hhmm,
            "open_times_today": st.open_times_today,
            "sentiment_est": self.sealed_ever_count,
            "order_id": "",
        })
        self._save_signals()

    # ── 买入信号 ──────────────────────────────────────────────────────────────

    def _fire_buy_signal(self, st: StockState) -> bool:
        if self.order_placed:
            self.logger.info("[BUY SKIP] 已锁定本轮D委托: %s，跳过 %s", self.order_locked_ts_code, st.ts_code)
            return True
        # 防御直接调用或未来分支遗漏总入口门禁：实盘未认证时不形成BUY，
        # 不写BUY记录，也不提前占用本会话委托锁。
        if self.live_order and not self._entry_alignment_allows_new_buy():
            st.last_order_fail_reason = "D发布未通过开仓逻辑与成交概率对齐认证"
            return False
        hhmm = now_hhmm()
        strict_path = self._strict_path_for(st) if self.factor_union_active else None
        event_reseal_hhmm = (
            strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
        )
        event_open_times = strict_path.open_times if strict_path else st.open_times_today
        event_first_seal_hhmm = (
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        st.buy_signaled = True
        self.order_placed = True   # 先加锁再下单，防止QMT资金冻结延迟导致重复委托
        self.order_locked_ts_code = st.ts_code
        source = (
            f"月度冻结D正式因子:{self.factor_release_id}:"
            f"{st.matched_factor_profile_ids}"
            if self.factor_union_active
            else "14:00后真实回封"
        )

        msg = (
            f"\n{'='*55}\n"
            f"  ★ [BUY] 买入信号 {hhmm_to_str(hhmm)}  [{source}]\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(event_reseal_hhmm)}  "
            f"炸板 {event_open_times} 次\n"
            f"  情绪估算：当前封板涨停 {self.sealed_ever_count} 只\n"
            f"  操作：{'实盘挂单' if self.live_order else '仅提醒（--live-order 开启下单）'}\n"
            f"{'='*55}"
        )
        print(msg)
        self.logger.warning(
            "[BUY] %s %s 重封=%s 炸板=%d次 来源=%s",
            st.ts_code, st.name, hhmm_to_str(event_reseal_hhmm),
            event_open_times, source,
        )
        record = {
            "signal_time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_type": "BUY",
            "ts_code": st.ts_code,
            "name": st.name,
            "upper_limit": st.upper_limit,
            "reseal_hhmm": event_reseal_hhmm,
            "open_times_today": event_open_times,
            "sentiment_est": self.sealed_ever_count,
            "first_time_bucket": classify_first_time_bucket_hhmm(
                event_first_seal_hhmm
            ),
            "fill_probability": st.fill_probability,
            "fill_reliable": st.fill_reliable,
            "fill_matched_source": st.fill_matched_source,
            "fill_sample_count": st.fill_sample_count,
            "fill_min_group_samples": st.fill_min_group_samples,
            "source": source,
            "factor_release_id": self.factor_release_id,
            "matched_factor_profile_ids": st.matched_factor_profile_ids,
            "factor_values_json": st.factor_values_json,
            "event_clock_source": (
                "QMT_COMPLETED_1M_CLOSE"
                if strict_path
                else "REALTIME_SNAPSHOT"
            ),
            "strict_break_close": (
                strict_path.last_break_close if strict_path else 0.0
            ),
            "order_id": "",
        }
        if self.live_order and self.broker is not None:
            locked = self._place_d_order(st, record)
        else:
            locked = True
        self.signal_records.append(record)
        self._save_signals()
        return locked
