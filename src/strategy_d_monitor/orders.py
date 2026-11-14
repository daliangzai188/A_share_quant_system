"""D盘中监控 StrategyDMonitor 的下单与撤单方法（2026-11-15 项目优化：从 scripts/monitor_strategy_d_intraday.py 原样搬来，逻辑未改）。

D委托提交、成交确认、撤单、成交记账和活动委托对账。StrategyDMonitor 继承 OrderMixin；方法里用到的属性都在 StrategyDMonitor 的 __init__/setup 里建立。
"""
from __future__ import annotations

import time
from typing import Any

from src.utils.time_utils import now_beijing, today_beijing
from src.notify import notify

from src.strategy_d_monitor.support import (
    MIN_D_VALID_LIMIT_PRICE,
    MAX_D_INVALID_PRICE_TICKS,
    STRATEGY_REMARK,
    StockState,
    classify_market_segment,
    check_strategy_position_occupied,
    next_trade_day,
    load_position_records,
    save_position_records,
)


class OrderMixin:
    """下单与撤单：D委托提交、成交确认、撤单、成交记账和活动委托对账。"""

    # ── 实盘下单 ──────────────────────────────────────────────────────────────

    def _place_d_order_handle_result(
        self, st, record, result, shares, target_amount, actual_amount, actual_position_pct, max_order_amount,
    ):
        """处理D委托提交结果：受理后等待成交确认、记账和通知，被拒或失败时记录原因（从 _place_d_order 拆出，语句原样）。"""
        if result.accepted:
            self.session_orders[result.order_id] = st.ts_code
            self.session_order_details[result.order_id] = {
                "order_id": result.order_id,
                "ts_code": st.ts_code,
                "name": st.name,
                "shares": shares,
                "buy_price": st.upper_limit,
                "target_amount": target_amount,
                "actual_amount": actual_amount,
                "target_position_pct": self.position_pct,
                "actual_position_pct": actual_position_pct,
                "buy_date": today_beijing().strftime("%Y%m%d"),
                "strategy_leg": "D",
            }
            st.order_id = result.order_id
            record["order_id"] = result.order_id
            fill_check = self._confirm_submitted_order(result.order_id, st.ts_code)
            self.session_order_details[result.order_id]["submit_status_code"] = fill_check.status_code
            self.session_order_details[result.order_id]["submit_status_text"] = fill_check.status_text
            checked_qty = int(getattr(fill_check, "filled_qty", 0) or 0)
            checked_price = float(getattr(fill_check, "avg_price", 0.0) or 0.0)
            checked_price = checked_price if checked_price > 0 else st.upper_limit
            checked_amount = checked_qty * checked_price
            unchecked_amount = max(actual_amount - checked_amount, 0.0)
            if self._is_terminal_no_fill(fill_check):
                fail_reason = self._describe_order_failure(fill_check, st, result.message)
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_TERMINAL"
                record["order_status_text"] = fill_check.status_text
                record["failure_reason"] = fail_reason
                record["filled_qty"] = 0
                record["filled_amount"] = 0.0
                self.logger.error(
                    "D下单失败: 策略=D 股票=%s %s 委托%d股 %.2f 总委托金额=%.2f 已成交金额=0.00 未成交金额=%.2f order_id=%s 状态=%s(%s) 失败原因=%s",
                    st.ts_code,
                    st.name,
                    shares,
                    st.upper_limit,
                    actual_amount,
                    actual_amount,
                    result.order_id,
                    fill_check.status_text,
                    fill_check.status_code,
                    fail_reason,
                )
                print(
                    f"  → 下单失败: 策略=D {st.ts_code} {st.name} "
                    f"委托{shares}股 总委托金额{actual_amount / 10000:.2f}万 "
                    f"已成交金额0.00万 未成交金额{actual_amount / 10000:.2f}万 "
                    f"order_id={result.order_id} 状态={fill_check.status_text}({fill_check.status_code}) "
                    f"失败原因={fail_reason}"
                )
                try:
                    notify(
                        "buy_result",
                        "❌ D开仓下单失败",
                        (
                            f"策略=D {st.ts_code} {st.name} 委托{shares}股 @{st.upper_limit:.2f}，"
                            f"总委托金额{actual_amount / 10000:.2f}万，已成交金额0.00万，"
                            f"未成交金额{actual_amount / 10000:.2f}万，"
                            f"order_id={result.order_id}，状态={fill_check.status_text}({fill_check.status_code})。"
                            f"失败原因：{fail_reason}"
                        ),
                        level="critical",
                        call=True,
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
                self.session_orders.pop(result.order_id, None)
                self.session_order_details.pop(result.order_id, None)
                st.order_id = ""
                self.order_placed = False
                self.order_locked_ts_code = ""
                return True, (False)
            if checked_qty >= shares:
                self.position_opened = True
                self.waiting_order_only = False
                record["order_status"] = "FILLED"
                record["order_status_text"] = fill_check.status_text
                record["filled_qty"] = checked_qty
                record["filled_amount"] = checked_amount
                self.logger.info(
                    "D持仓信息: 策略=D 股票=%s %s 持仓%d股 成本%.2f 市值=%.2f order_id=%s",
                    st.ts_code,
                    st.name,
                    checked_qty,
                    checked_price,
                    checked_amount,
                    result.order_id,
                )
                print(
                    f"  → 持仓信息: 策略=D {st.ts_code} {st.name} "
                    f"持仓{checked_qty}股 成本{checked_price:.2f} "
                    f"市值{checked_amount / 10000:.2f}万 order_id={result.order_id}"
                )
                self._record_filled_d_position(result.order_id, checked_qty, checked_price)
            else:
                self.waiting_order_only = True
                self.position_opened = checked_qty > 0
                record["order_status"] = "PENDING_OR_PARTIAL"
                record["order_status_text"] = fill_check.status_text
                record["filled_qty"] = checked_qty
                record["filled_amount"] = checked_amount
                self.logger.info(
                    "D委托信息: 策略=D 股票=%s %s 委托%d股 %.2f 总委托金额=%.2f 已成交%d股 已成交金额=%.2f 未成交金额=%.2f 目标仓位=%.1f%% 实际仓位=%.2f%% 固定单笔上限=%s order_id=%s 提交后状态=%s(%s)",
                    st.ts_code,
                    st.name,
                    shares,
                    st.upper_limit,
                    actual_amount,
                    checked_qty,
                    checked_amount,
                    unchecked_amount,
                    self.position_pct * 100,
                    actual_position_pct * 100,
                    f"{max_order_amount:.0f}元" if max_order_amount > 0 else "无",
                    result.order_id,
                    fill_check.status_text,
                    fill_check.status_code,
                )
                print(
                    f"  → 委托信息: 策略=D {st.ts_code} {st.name} 委托{shares}股 "
                    f"总委托金额{actual_amount / 10000:.2f}万 "
                    f"已成交金额{checked_amount / 10000:.2f}万 "
                    f"未成交金额{unchecked_amount / 10000:.2f}万 "
                    f"order_id={result.order_id} 提交后状态={fill_check.status_text}({fill_check.status_code})"
                )
                if checked_qty > 0:
                    self._record_filled_d_position(result.order_id, checked_qty, checked_price)
                try:
                    notify(
                        "buy_result",
                        "⏳ D开仓委托未全成",
                        (
                            f"策略=D {st.ts_code} {st.name} 委托{shares}股 @{st.upper_limit:.2f}，"
                            f"总委托金额{actual_amount / 10000:.2f}万，"
                            f"已成交金额{checked_amount / 10000:.2f}万，"
                            f"未成交金额{unchecked_amount / 10000:.2f}万，"
                            f"order_id={result.order_id}，提交后状态={fill_check.status_text}({fill_check.status_code})。"
                        ),
                        level="timeSensitive",
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
            if fill_check.status_code < 0 and fill_check.filled_qty <= 0:
                self.logger.error(
                    "D委托提交后未在QMT当日委托中确认: %s order_id=%s %d股 %.2f 金额=%.2f",
                    st.ts_code,
                    result.order_id,
                    shares,
                    st.upper_limit,
                    actual_amount,
                )
                try:
                    notify(
                        "buy_result",
                        "❌ D开仓委托未确认",
                        (
                            f"{st.ts_code} {st.name} QMT返回order_id={result.order_id}，"
                            f"但提交后未在当日委托中查到。程序计划委托{shares}股，"
                            f"总委托金额{actual_amount / 10000:.2f}万；请立即查看QMT是否实际挂单。"
                        ),
                        level="critical",
                        call=True,
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
            return True, (True)
        else:
            self.logger.error("D委托被拒: %s %s", st.ts_code, result.message)
            fail_reason = result.message or "QMT返回拒单，未生成有效委托号"
            st.last_order_fail_reason = fail_reason
            record["order_status"] = "REJECTED_BY_QMT"
            record["order_status_text"] = result.message
            record["failure_reason"] = fail_reason
            try:
                notify("buy_result", "❌ D开仓委托被拒",
                       f"{st.ts_code} {st.name} 委托被拒：{fail_reason}",
                       level="critical", call=True)
            except Exception as exc:
                self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
            print(f"  → 委托被拒: {result.message}")
            self.order_placed = False
            self.order_locked_ts_code = ""
            return True, (False)
        return False, None

    def _place_d_order(self, st: StockState, record: dict) -> bool:
        from src.broker_adapter import OrderRequest
        from src.qmt_adapter import tushare_to_qmt_code
        try:
            # 最终券商边界再做一次fail-closed校验，防止任何内部调用绕过信号层。
            if not self._entry_alignment_allows_new_buy():
                fail_reason = "D发布未通过开仓逻辑与成交概率对齐认证，禁止新开仓"
                self.logger.error("D下单拦截: %s %s", st.ts_code, fail_reason)
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_ENTRY_ALIGNMENT"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            if self.session_orders:
                self.logger.warning("本会话已有D委托，拒绝再次下单: %s", st.ts_code)
                return True
            if st.upper_limit < MIN_D_VALID_LIMIT_PRICE:
                level = "确认拦截" if st.invalid_price_ticks >= MAX_D_INVALID_PRICE_TICKS else "等待复核"
                fail_reason = (
                    f"本地风控{level}：D涨停价异常{st.upper_limit:.2f}元，"
                    f"已连续{st.invalid_price_ticks}/{MAX_D_INVALID_PRICE_TICKS}轮低于"
                    f"{MIN_D_VALID_LIMIT_PRICE:.2f}元，疑似QMT行情字段异常"
                )
                self.logger.error(
                    "D下单拦截: 策略=D 股票=%s %s %s",
                    st.ts_code,
                    st.name,
                    fail_reason,
                )
                print(f"  → 下单拦截: 策略=D {st.ts_code} {st.name} {fail_reason}")
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_LOCAL_PRICE_GUARD"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            if st.last_price > 0 and st.upper_limit > st.last_price * 1.005:
                # 价格自洽校验（2026-07-23 春兴精工废单事故）：封板状态下现价=真实涨停价；
                # 下单价明显高于现价，说明涨停幅度判断有误（如ST股按10%误算2.08/真涨停1.98），
                # 挂出必成废单。按"买不进当天放弃"口径放弃该票，绝不猜价格。
                fail_reason = (
                    f"本地风控拦截：D下单价{st.upper_limit:.2f}高于现价{st.last_price:.2f}超0.5%，"
                    f"涨停幅度判断可能有误（疑似ST/风险警示按普通幅度误算），挂出必成废单，放弃该票"
                )
                self.logger.error("D下单拦截: 策略=D 股票=%s %s %s", st.ts_code, st.name, fail_reason)
                print(f"  → 下单拦截: 策略=D {st.ts_code} {st.name} {fail_reason}")
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_LOCAL_PRICE_GUARD"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            occupied, occupied_desc = check_strategy_position_occupied(
                self.broker if self.live_order else None
            )
            if occupied:
                fail_reason = f"旧策略仓未实际清空，取消D开仓：{occupied_desc}"
                self.logger.warning("D下单拦截: %s", fail_reason)
                record["order_status"] = "REJECTED_EXISTING_STRATEGY_POSITION"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                return True
            # 成交概率门和真实委托必须共用同一账户快照、同一股数和同一
            # 委托金额。禁止再以固定41.25万元估算成交概率后，另按82.5%仓位下单。
            capacity = self._resolve_order_capacity(st)
            shares = capacity.shares
            if shares <= 0:
                fail_reason = "真实账户可用资金不足，可下单股数为0"
                self.logger.warning("可用资金不足，跳过下单: %s", st.ts_code)
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_INSUFFICIENT_CASH"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            fill_passed, fill_reason = self._refresh_fill_gate(
                st,
                planned_buy_amount=capacity.actual_amount,
            )
            record.update(
                {
                    "fill_probability": st.fill_probability,
                    "fill_reliable": st.fill_reliable,
                    "fill_matched_source": st.fill_matched_source,
                    "fill_sample_count": st.fill_sample_count,
                    "fill_min_group_samples": st.fill_min_group_samples,
                    "fill_planned_buy_amount": st.fill_planned_buy_amount,
                    "fill_estimated_turnover_amount": st.fill_estimated_turnover_amount,
                    "fill_current_queue_amount": st.fill_current_queue_amount,
                }
            )
            if not fill_passed:
                fail_reason = f"按真实委托金额复核成交概率未通过:{fill_reason}"
                self.logger.warning("D下单拦截: %s %s", st.ts_code, fail_reason)
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_FILL_PROBABILITY"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            target_amount = capacity.target_amount
            actual_amount = capacity.actual_amount
            total_asset = capacity.total_asset
            max_order_amount = float(
                self.config.get("live_trade", {}).get("max_single_order_amount", 0)
                or 0
            )
            actual_position_pct = actual_amount / total_asset if total_asset > 0 else 0.0
            req = OrderRequest(
                ts_code=st.ts_code,
                broker_code=tushare_to_qmt_code(st.ts_code),
                side="BUY",
                quantity=shares,
                price_type="FIX_PRICE",
                price=st.upper_limit,
                strategy_name="STRATEGY_D",
                remark=STRATEGY_REMARK,
                strategy_leg="D",
                business_date=today_beijing().strftime("%Y%m%d"),
                signal_date=today_beijing().strftime("%Y%m%d"),
                purpose="OPEN",
                source_key=(
                    f"D_FIRST_BOARD|{today_beijing().strftime('%Y%m%d')}|{st.ts_code}"
                ),
                metadata={
                    "name": st.name,
                    "entry_clock": now_beijing().strftime("%H:%M:%S"),
                    "fill_probability": st.fill_probability,
                    "fill_planned_buy_amount": st.fill_planned_buy_amount,
                    "fill_estimated_turnover_amount": st.fill_estimated_turnover_amount,
                    "fill_current_queue_amount": st.fill_current_queue_amount,
                },
            )
            result = self.broker.place_order(req)
            _done, _value = self._place_d_order_handle_result(
                st, record, result, shares, target_amount, actual_amount, actual_position_pct,
                max_order_amount,
            )
            if _done:
                return _value
        except Exception as e:
            self.logger.error("下单异常: %s: %s", st.ts_code, e)
            st.last_order_fail_reason = str(e)
            record["order_status"] = "ORDER_EXCEPTION"
            record["order_status_text"] = str(e)
            record["failure_reason"] = str(e)
            try:
                notify("buy_result", "❌ D开仓下单异常",
                       f"{st.ts_code} {st.name} 下单异常：{e}",
                       level="critical", call=True)
            except Exception as exc:
                self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
            print(f"  → 下单异常: {e}")
            self.order_placed = False
            self.order_locked_ts_code = ""
            return False

    @staticmethod
    def _is_terminal_no_fill(fill: Any) -> bool:
        status_code = int(getattr(fill, "status_code", -1) or -1)
        filled_qty = int(getattr(fill, "filled_qty", 0) or 0)
        return filled_qty <= 0 and status_code in {53, 54, 57}

    @staticmethod
    def _describe_order_failure(fill: Any, st: StockState, fallback: str = "") -> str:
        status_code = int(getattr(fill, "status_code", -1) or -1)
        status_text = str(getattr(fill, "status_text", "") or "UNKNOWN")
        raw = getattr(fill, "raw", None) or {}
        if not isinstance(raw, dict):
            raw = {}

        reason_keys = [
            "status_msg", "error_msg", "error_info", "fail_reason", "cancel_reason",
            "order_remark", "remark", "m_strErrorMsg", "m_strStatusMsg",
            "m_strRemark", "m_strOrderRemark", "entrust_status_msg",
        ]
        reasons: list[str] = []
        for key in reason_keys:
            value = raw.get(key)
            if value is None or str(value).strip() == "":
                continue
            text = str(value).strip()
            if text not in reasons:
                reasons.append(f"{key}={text}")

        if fallback:
            reasons.append(f"返回消息={fallback}")

        inference = ""
        segment = classify_market_segment(st.ts_code)
        if status_code == 57:
            inference = "柜台返回废单，委托已终止且0成交"
            if segment == "bj":
                inference += (
                    "；账户已确认开通北交所权限，如仍废单应检查柜台返回原因、"
                    "证券代码格式、价格和申报数量"
                )
        elif status_code == 54:
            inference = "委托已撤且0成交"
        elif status_code == 53:
            inference = "委托部撤但本次查询0成交"
        elif status_code < 0:
            inference = "提交后未在QMT当日委托中确认"

        raw_summary = ""
        if raw:
            useful = []
            for key in sorted(raw.keys()):
                value = raw.get(key)
                if value is None or str(value).strip() == "":
                    continue
                key_l = key.lower()
                if any(token in key_l for token in ["status", "error", "remark", "msg", "reason", "order"]):
                    useful.append(f"{key}={value}")
                if len(useful) >= 8:
                    break
            if useful:
                raw_summary = "；原始字段：" + "，".join(useful)

        parts = [f"状态={status_text}({status_code})"]
        if inference:
            parts.append(inference)
        if reasons:
            parts.append("；".join(reasons))
        if raw_summary:
            parts.append(raw_summary)
        return "；".join(parts)

    def _confirm_submitted_order(self, order_id: str, ts_code: str):
        """下单后短暂等待，再反查当日委托/成交，避免把返回号误当成真实挂单。"""

        from src.broker_adapter import OrderFill

        last_error: Exception | None = None
        last_visible_fill: OrderFill | None = None
        for attempt in range(1, 4):
            time.sleep(1.0)
            try:
                fill = self.broker.get_order_fill(order_id)
                # “已报/待报”只证明券商受理，不代表成交确认结束。旧代码在
                # status=50、filled=0时直接返回，随后到达的真实成交无人回写。
                if fill.filled_qty > 0 or fill.is_terminal:
                    return fill
                if fill.status_code >= 0:
                    last_visible_fill = fill
                    self.logger.info(
                        "D委托已在券商可见但尚未成交，第%d次继续确认: %s "
                        "order_id=%s 状态=%s(%s)",
                        attempt,
                        ts_code,
                        order_id,
                        fill.status_text,
                        fill.status_code,
                    )
                    continue
                self.logger.warning(
                    "D委托提交后第%d次反查未确认: %s order_id=%s",
                    attempt,
                    ts_code,
                    order_id,
                )
            except Exception as e:
                last_error = e
                self.logger.error("D委托提交后第%d次反查异常: %s order_id=%s: %s",
                                  attempt, ts_code, order_id, e)
        if last_visible_fill is not None:
            return last_visible_fill
        status_text = f"QUERY_ERROR:{last_error}" if last_error else "NOT_FOUND_IN_QMT_ORDERS"
        return OrderFill(order_id=str(order_id), status_code=-1, status_text=status_text)

    def _update_signal_order_result(
        self,
        order_id: str,
        *,
        order_status: str,
        order_status_text: str,
        filled_qty: int | None = None,
        filled_amount: float | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        """把QMT后续成交/撤单事实回写到当天D信号记录。"""

        changed = False
        for record in reversed(getattr(self, "signal_records", [])):
            if str(record.get("order_id", "")) != str(order_id):
                continue
            updates: dict[str, Any] = {
                "order_status": str(order_status),
                "order_status_text": str(order_status_text),
            }
            if filled_qty is not None:
                updates["filled_qty"] = max(int(filled_qty), 0)
            if filled_amount is not None:
                updates["filled_amount"] = max(float(filled_amount), 0.0)
            if failure_reason:
                updates["failure_reason"] = str(failure_reason)
            for key, value in updates.items():
                if record.get(key) != value:
                    record[key] = value
                    changed = True
            if changed:
                record["order_status_updated_at"] = now_beijing().isoformat()
            break
        if changed and hasattr(self, "signal_csv"):
            self._save_signals()
        return changed

    # ── 14:55 撤单 ────────────────────────────────────────────────────────────

    def cancel_all_d_orders(self) -> None:
        if not self.live_order or self.broker is None:
            self.logger.info("非实盘模式，跳过撤单")
            return
        if not self.session_orders:
            self.logger.info("本会话无D委托")
            return

        print(f"\n{'='*55}")
        print(f"  14:55 撤单 — 共 {len(self.session_orders)} 笔D委托")
        self.logger.warning("14:55撤单，共 %d 笔", len(self.session_orders))

        # 逐笔查询真实成交（get_order_fill 以成交回报为准，避免状态码字符串误判）
        cancelled = failed = 0
        for order_id, ts_code in self.session_orders.items():
            try:
                fill = self.broker.get_order_fill(order_id)
            except Exception as e:
                self.logger.error("查询成交失败: %s order_id=%s: %s", ts_code, order_id, e)
                fill = None

            filled_qty = int(getattr(fill, "filled_qty", 0)) if fill else 0
            fill_price = float(getattr(fill, "avg_price", 0.0)) if fill else 0.0

            if filled_qty > 0:
                status_text = getattr(fill, "status_text", "") if fill else ""
                detail = self.session_order_details.get(order_id, {})
                planned_qty = int(detail.get("shares", 0) or 0)
                planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
                if planned_amount <= 0 and planned_qty > 0:
                    planned_amount = planned_qty * float(detail.get("buy_price", 0.0) or 0.0)
                filled_amount = filled_qty * fill_price
                unfilled_amount = max(planned_amount - filled_amount, 0.0)
                print(
                    f"  {ts_code}  order_id={order_id} → 已成交{filled_qty}/{planned_qty}股，"
                    f"总委托{planned_amount / 10000:.2f}万 已成交{filled_amount / 10000:.2f}万 "
                    f"未成交{unfilled_amount / 10000:.2f}万，状态={status_text}"
                )
                self.logger.warning(
                    "D 14:55成交确认: %s order_id=%s 状态=%s 成交%d/%d股 总委托=%.2f 已成交=%.2f 未成交=%.2f",
                    ts_code,
                    order_id,
                    status_text,
                    filled_qty,
                    planned_qty,
                    planned_amount,
                    filled_amount,
                    unfilled_amount,
                )
                self._record_filled_d_position(order_id, filled_qty, fill_price)
                # 部分成交：撤掉未成残单
                if not getattr(fill, "is_filled", False):
                    ok = self.broker.cancel_order(order_id)
                    final_status = (
                        "PARTIAL_FILLED_CANCEL_REQUESTED"
                        if ok
                        else "PARTIAL_FILLED_CANCEL_FAILED"
                    )
                    self._update_signal_order_result(
                        order_id,
                        order_status=final_status,
                        order_status_text=status_text,
                        filled_qty=filled_qty,
                        filled_amount=filled_amount,
                        failure_reason=(None if ok else "部分成交后的未成残单撤单失败"),
                    )
                    self.logger.warning("D部分成交 %s 已成%d股(%s)，撤残单%s",
                                        ts_code, filled_qty, status_text,
                                        "已发" if ok else "失败")
                else:
                    self._update_signal_order_result(
                        order_id,
                        order_status="FILLED",
                        order_status_text=status_text,
                        filled_qty=filled_qty,
                        filled_amount=filled_amount,
                    )
                    self.logger.info(
                        "D委托已全部成交，无需撤单: %s order_id=%s 持仓%d股 @%.2f",
                        ts_code,
                        order_id,
                        filled_qty,
                        fill_price,
                    )
                continue

            ok = self.broker.cancel_order(order_id)
            fill_was_confirmed = fill is not None
            if ok:
                cancelled += 1
                detail = self.session_order_details.get(order_id, {})
                planned_qty = int(detail.get("shares", 0) or 0)
                planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
                if fill_was_confirmed:
                    print(f"  {ts_code}  order_id={order_id} → 未成交，撤单已发")
                    status = "CANCEL_REQUESTED_NO_FILL"
                    status_text = str(getattr(fill, "status_text", ""))
                    notice_text = "已成交0.00万，14:55已撤单。"
                else:
                    print(f"  {ts_code}  order_id={order_id} → 成交状态未知，撤单已发")
                    status = "CANCEL_REQUESTED_FILL_UNKNOWN"
                    status_text = "QMT成交反查失败，撤单请求已发"
                    notice_text = "成交数量尚未确认，14:55撤单请求已发，请以QMT成交回报为准。"
                self._update_signal_order_result(
                    order_id,
                    order_status=status,
                    order_status_text=status_text,
                    filled_qty=(0 if fill_was_confirmed else None),
                    filled_amount=(0.0 if fill_was_confirmed else None),
                )
                self.logger.warning(
                    "D委托撤单请求已发: %s order_id=%s 委托%d股 金额=%.2f 成交状态=%s",
                    ts_code, order_id, planned_qty, planned_amount, status,
                )
                try:
                    notify(
                        "buy_result",
                        "⚠️ D开仓未成交已撤单",
                        (
                            f"{ts_code} order_id={order_id} 委托{planned_qty}股，"
                            f"总委托金额{planned_amount / 10000:.2f}万，"
                            f"{notice_text}"
                        ),
                        level="timeSensitive",
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)
            else:
                failed += 1
                print(f"  {ts_code}  order_id={order_id} → 撤单失败！请手动检查")
                self.logger.error("撤单失败: %s order_id=%s", ts_code, order_id)
                self._update_signal_order_result(
                    order_id,
                    order_status="CANCEL_FAILED",
                    order_status_text="14:55撤单请求失败",
                    filled_qty=(0 if fill is not None else None),
                    filled_amount=(0.0 if fill is not None else None),
                    failure_reason="D未成委托撤单失败，请人工核对QMT",
                )
                try:
                    notify(
                        "buy_result",
                        "❌ D未成交撤单失败",
                        f"{ts_code} order_id={order_id} 未成交且撤单失败，请立即检查QMT。",
                        level="critical",
                        call=True,
                    )
                except Exception as exc:
                    self.logger.warning("D推送内容生成或发送失败（不影响下单流程）：%s", exc)

        print(f"  结果: 撤单={cancelled}笔  失败={failed}笔")
        print(f"{'='*55}\n")

    def _record_filled_d_position(self, order_id: str, filled_qty: int | None = None,
                                  fill_price: float | None = None) -> None:
        detail = self.session_order_details.get(order_id)
        if not detail:
            self.logger.warning("找不到D成交委托明细，无法写入持仓: order_id=%s", order_id)
            return
        buy_date = str(detail.get("buy_date", today_beijing().strftime("%Y%m%d")))
        shares = int(filled_qty) if filled_qty and filled_qty > 0 else int(detail.get("shares", 0))
        buy_price = float(fill_price) if fill_price and fill_price > 0 else float(detail.get("buy_price", 0.0))
        recorded_filled_qty = max(
            int(detail.get("recorded_filled_qty", 0) or 0),
            0,
        )
        if shares <= recorded_filled_qty:
            return
        planned_exit_date = next_trade_day(buy_date, 2)
        payload = {
            "order_id": str(order_id),
            "ts_code": str(detail.get("ts_code", "")),
            "name": str(detail.get("name", "")),
            "signal_date": buy_date,
            "buy_date": buy_date,
            "planned_exit_date": planned_exit_date,
            "shares": shares,
            "buy_price": buy_price,
            "strategy_leg": "D",
            "planned_order_qty": int(detail.get("shares", shares) or shares),
        }
        if self.position_recorder is not None:
            # daemon端使用同order_id累计成交幂等更新，并同步事务意图。
            self.position_recorder(payload)
        else:
            # 仅保留给非实盘离线演练；实盘构造器已强制必须提供callback。
            positions = load_position_records()
            existing = next(
                (pos for pos in positions if str(pos.get("order_id", "")) == str(order_id)),
                None,
            )
            if existing is None:
                positions.append({
                    **payload,
                    "status": "open",
                    "sell_date": None,
                    "sell_price": None,
                })
            elif shares > int(existing.get("shares", 0) or 0):
                existing["shares"] = shares
                existing["buy_price"] = buy_price
            save_position_records(positions)
        # 同一委托的成交回报是累计数量；只在持仓投影成功后推进确认水位。
        detail["recorded_filled_qty"] = shares
        self.position_opened = True
        self.logger.warning(
            "D持仓信息已写入持仓账本: 策略=D order_id=%s ts_code=%s %d股 @%.2f 买入日=%s 默认计划平仓日=%s；若次日有A/B/C接力则T+1开盘先卖D",
            order_id,
            detail.get("ts_code", ""),
            shares,
            buy_price,
            buy_date,
            planned_exit_date,
        )
        try:
            planned_shares = int(detail.get("shares", 0) or 0)
            planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
            if planned_amount <= 0 and planned_shares > 0:
                planned_amount = planned_shares * float(detail.get("buy_price", 0.0) or 0.0)
            filled_amount = shares * buy_price
            unfilled_amount = max(planned_amount - filled_amount, 0.0)
            partial = planned_shares > 0 and shares < planned_shares
            if partial:
                body = (
                    f"策略=D {detail.get('ts_code', '')} {detail.get('name', '')} "
                    f"成交{shares}/{planned_shares or shares}股 @{buy_price:.2f}。"
                    f"总委托金额{planned_amount / 10000:.2f}万，"
                    f"已成交金额{filled_amount / 10000:.2f}万，"
                    f"未成交金额{unfilled_amount / 10000:.2f}万。"
                )
            else:
                body = (
                    f"策略=D {detail.get('ts_code', '')} {detail.get('name', '')} "
                    f"持仓{shares}股 成本{buy_price:.2f} 市值{filled_amount / 10000:.2f}万。"
                )
            notify(
                "buy_result",
                "⚠️ D开仓部分成交" if partial else "✅ D持仓信息",
                body,
                level="timeSensitive" if partial else "active",
            )
            # D竞价卖容量提醒(2026-07-24 用户要求):历史36笔标的实算,次日开盘
            # 竞价的单笔安全卖单上限中位约300万(竞价参与≤10%口径)。买入金额≈
            # 次日9:23要卖的金额,达到300万说明D已进入竞价冲击灰色区——提醒用户
            # 验证"竞价实际成交价 vs 开盘价"偏差,决定是否给D卖出加方案。
            if filled_amount >= 3_000_000:
                notify(
                    "buy_result",
                    "📏 D单仓已达300万:该验证竞价冲击了",
                    f"本次D买入成交{filled_amount / 10000:.0f}万,明日9:23集合竞价卖出规模已达"
                    f"历史标的安全线(中位约300万,小票更低)。请对比明日实际竞价成交价与开盘价的偏差:"
                    f"偏差明显(如>0.5%)则需考虑D卖出方案(竞价部分卖+盘中卖余量),"
                    f"并把D纳入执行对账(开盘价基准)持续跟踪。",
                    level="timeSensitive",
                )
        except Exception as exc:
            self.logger.warning("D成交后的容量提示生成或发送失败（不影响持仓记录）：%s", exc)

    def _reconcile_active_d_orders_once(self) -> bool:
        """推进D活动委托的成交状态；成交事实只能来自同一QMT委托。"""

        if not self.session_orders or self.broker is None:
            return False

        all_filled = True
        for order_id, ts_code in self.session_orders.items():
            detail = self.session_order_details.get(order_id, {})
            planned_qty = max(int(detail.get("shares", 0) or 0), 0)
            try:
                fill = self.broker.get_order_fill(order_id)
            except Exception as exc:
                all_filled = False
                self.logger.error(
                    "D活动委托成交确认异常: %s order_id=%s: %s",
                    ts_code,
                    order_id,
                    exc,
                )
                continue

            filled_qty = max(int(getattr(fill, "filled_qty", 0) or 0), 0)
            fill_price = float(getattr(fill, "avg_price", 0.0) or 0.0)
            if fill_price <= 0:
                fill_price = float(detail.get("buy_price", 0.0) or 0.0)
            if filled_qty > int(detail.get("recorded_filled_qty", 0) or 0):
                self._record_filled_d_position(order_id, filled_qty, fill_price)
                self.logger.warning(
                    "D活动委托成交已回写: %s order_id=%s 累计成交%d/%d股 @%.2f",
                    ts_code,
                    order_id,
                    filled_qty,
                    planned_qty,
                    fill_price,
                )

            filled_amount = filled_qty * fill_price
            status_text = str(getattr(fill, "status_text", ""))
            if planned_qty > 0 and filled_qty >= planned_qty:
                order_status = "FILLED"
            elif filled_qty > 0:
                order_status = "PENDING_OR_PARTIAL"
            elif bool(getattr(fill, "is_terminal", False)):
                order_status = "TERMINAL_NO_FILL"
            else:
                order_status = "PENDING_OR_PARTIAL"
            self._update_signal_order_result(
                order_id,
                order_status=order_status,
                order_status_text=status_text,
                filled_qty=filled_qty,
                filled_amount=filled_amount,
            )

            if planned_qty <= 0 or filled_qty < planned_qty:
                all_filled = False

        if all_filled:
            self.position_opened = True
            self.waiting_order_only = False
        return all_filled
