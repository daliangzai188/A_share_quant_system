from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from src.live_order_gateway import LiveOrderGateway
from src.rolling_signal_store import latest_signal_for_buy_date, signal_by_signal_date
from src.strategy_model3_policy import (
    model3_l_base_rule_pass,
    model3_l_replace_guard_pass,
)
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.time_utils import today_beijing

_E2_POSITION_PCT = 0.825
_E2_LOT_SIZE = 100


def round_lot_shares_below_amount(amount: float, price: float, lot_size: int = _E2_LOT_SIZE) -> int:
    if amount <= 0 or price <= 0:
        return 0
    max_qty = int((amount - 0.01) / price)
    if lot_size > 0:
        max_qty -= max_qty % lot_size
    return max(max_qty, 0)


@dataclass(frozen=True)
class CombinedLiveDecision:
    action: str
    strategy_leg: str
    ts_code: str = ""
    name: str = ""
    side: str = ""
    quantity: int = 0
    reason: str = ""
    source: str = ""


class CombinedLiveEngine:
    """A+C+D+E2 / L / model=3 总策略实盘状态机（B已删除）。

    这个类只负责组合层面的顺序和阻断，不直接提交真实委托。
    当前总策略开关在 config/config.json 的 active_strategy_profile.mode：
      1 = 现有 ACDE2/D 组合状态机
      2 = 独立 L 龙头策略状态机
      3 = model=3 自动切换实盘状态机

    当前配置为mode=3；L独立mode=2仍关闭，L只按model=3规则补位或替换。
    model=3 由 strategy_model3.enabled/live_order_enabled 控制，所有计划单仍经过 LiveOrderGateway 风控。
    """

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.gateway = LiveOrderGateway(config_path)
        self.positions_path = self.project_root / "data" / "processed" / "positions.json"
        self.output_dir = self.project_root / "reports" / "live_trade" / "combined"
        mkdir_p(self.output_dir)

    def is_b_new_entry_enabled(self) -> bool:
        """读取退役标记；当前配置必须返回False，阻断全部B新增买入。"""
        strategy_path = self.project_root / "config" / "strategy_config.json"
        try:
            strategy_config = load_json_config(strategy_path)
        except Exception:
            return False
        b_config = strategy_config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
        return bool(b_config.get("enabled", False)) and not bool(
            b_config.get("new_entries_disabled", False)
        )

    def is_b_strategy_removed(self) -> bool:
        """B彻底删除后，组合计划不得再接收B的买单或卖单。"""
        strategy_path = self.project_root / "config" / "strategy_config.json"
        try:
            strategy_config = load_json_config(strategy_path)
        except Exception:
            return True
        b_config = strategy_config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
        return bool(b_config.get("removed", False)) or bool(b_config.get("manual_exit_only", False))

    def is_manual_exit_only_position(self, position: dict[str, Any]) -> bool:
        if bool(position.get("manual_exit_only", False)) or bool(position.get("auto_exit_disabled", False)):
            return True
        return str(position.get("strategy_leg", "")).upper() == "B" and self.is_b_strategy_removed()

    def active_strategy_mode(self) -> int:
        profile = self.config.get("active_strategy_profile", {})
        try:
            return int(profile.get("mode", 1))
        except (TypeError, ValueError):
            return 1

    def active_strategy_name(self) -> str:
        profile = self.config.get("active_strategy_profile", {})
        modes = profile.get("available_modes", {})
        return str(modes.get(str(self.active_strategy_mode()), profile.get("mode_name", "ACDE2")))

    @staticmethod
    def is_l_position(position: dict[str, Any]) -> bool:
        return str(position.get("strategy_leg", "")).upper() == "L"

    def load_yesterday_l_signal(self, today: str) -> dict[str, Any] | None:
        """找 today 之前最近的 L 信号，且其 planned_buy_date == today。

        L 的信号在 T 日收盘后由 scripts/run_strategy_l_signal.py 生成；
        实盘计划只能在 T+1 读取到 planned_buy_date == today 的信号时生成。
        这样可以避免用当天盘中未来数据生成当天买单。
        """
        signal_dir = self.project_root / "reports" / "strategy_l"
        if not signal_dir.exists():
            return None
        rolling = latest_signal_for_buy_date(signal_dir / "l_signals_recent.json", today)
        if rolling is not None:
            return rolling
        files = sorted(signal_dir.glob("l_signal_????????.json"))
        for f in reversed(files):
            date_part = f.stem.replace("l_signal_", "")
            if date_part >= today:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if str(data.get("planned_buy_date", "")) == today:
                    return data
            except Exception:
                continue
        return None

    def load_today_l_signal(self, today: str) -> dict[str, Any] | None:
        """加载今日收盘流水线已生成的 L 信号，用于状态展示。

        今日信号只代表“明日可能开仓”，不能用于今日直接买入。
        """
        rolling = signal_by_signal_date(
            self.project_root / "reports" / "strategy_l" / "l_signals_recent.json",
            today,
        )
        if rolling is not None:
            return rolling
        signal_path = self.project_root / "reports" / "strategy_l" / f"l_signal_{today}.json"
        if not signal_path.exists():
            return None
        try:
            return json.loads(signal_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def build_l_buy_order(self, signal: dict[str, Any], today: str) -> dict[str, Any]:
        """把 L 信号转换成组合计划买单。

        这里只生成“计划单”，不直接下单。真实下单仍要经过 trading_daemon
        和 LiveOrderGateway 的交易时间、账户资金、涨跌停、重复委托等校验。
        """
        limit_close = float(signal.get("limit_close", 0.0))
        position_pct = float(self.config.get("strategy_l", {}).get("position_pct", signal.get("position_pct", 0.825)))
        initial_equity = float(self.config.get("position", {}).get("initial_cash", 500_000.0))
        planned_amount = initial_equity * position_pct
        if str(self.config.get("trade_mode", "")).lower() == "live":
            # 0=不限额（82.5%目标仓位接管），>0=单笔限额。
            max_single_order_amount = float(
                self.config.get("live_trade", {}).get("max_single_order_amount", 0) or 0
            )
            if max_single_order_amount > 0:
                planned_amount = min(planned_amount, max_single_order_amount)
        round_lot = round_lot_shares_below_amount(planned_amount, limit_close)
        planned_amount = round_lot * limit_close
        planned_position_pct = planned_amount / initial_equity if initial_equity > 0 else position_pct
        return {
            "paper_order_id": f"L-BUY-{today}-{signal.get('ts_code','')}",
            "signal_date": signal.get("signal_date", ""),
            "strategy_leg": "L",
            "planned_order_date": today,
            "side": "BUY",
            "ts_code": str(signal.get("ts_code", "")),
            "name": str(signal.get("name", "")),
            "planned_action": "PLAN_BUY_L_T1_OPEN",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": planned_position_pct,
            "planned_equity": initial_equity,
            "planned_amount_by_equity": planned_amount,
            "reference_price": limit_close,
            "estimated_shares": round_lot,
            "round_lot_shares": round_lot,
            "risk_flags": "L_STANDALONE",
            "live_order_enabled": True,
            "exit_n_days": 1,  # L 口径：T+1买入，T+2收盘卖出；买入后持有1个交易日
            "strategy_name": "A_SYSTEM_L",
        }

    def build_l_sell_order(self, position: dict[str, Any], today: str) -> dict[str, Any]:
        """生成 L 到期平仓计划。

        卖出计划不受 strategy_l.live_order_enabled 限制：
        如果未来曾经打开 L 并买入成功，后续即便关闭 L 买入开关，也必须允许已有 L
        持仓按计划平仓，避免因为配置回退导致持仓无法退出。
        """
        shares = self.as_int(position.get("shares", 0))
        return {
            "paper_order_id": f"L-SELL-{today}-{position.get('ts_code','')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "L",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": "PLAN_SELL_L_T2_CLOSE",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": 0.0,
            "planned_equity": 0.0,
            "planned_amount_by_equity": 0.0,
            "reference_price": float(position.get("buy_price", 0.0)),
            "estimated_shares": shares,
            "round_lot_shares": shares,
            "risk_flags": "L_SELL_T2_CLOSE",
            "live_order_enabled": True,
            "strategy_name": "A_SYSTEM_L",
        }

    def build_l_mode_plan(self, today: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        l_config = self.config.get("strategy_l", {})
        positions = self.load_positions()
        open_positions = [p for p in positions if self.is_open_position(p)]
        open_l_positions = [p for p in open_positions if self.is_l_position(p)]
        open_non_l_positions = [p for p in open_positions if not self.is_l_position(p)]
        due_l_positions = [
            p for p in open_l_positions
            if str(p.get("planned_exit_date", "99991231")) <= today
            or str(p.get("status", "")).lower() == "sell_pending"
        ]
        state_df = pd.DataFrame([{
            "today": today,
            "active_strategy_mode": self.active_strategy_mode(),
            "active_strategy_name": self.active_strategy_name(),
            "strategy_l_variant": str(l_config.get("variant", "L2")),
            "strategy_l_run_mode": str(l_config.get("run_mode", "standalone")),
            "strategy_l_enabled": bool(l_config.get("enabled", False)),
            "strategy_l_live_order_enabled": bool(l_config.get("live_order_enabled", False)),
            "open_position_count": len(open_positions),
            "open_l_position_count": len(open_l_positions),
            "open_non_l_position_count": len(open_non_l_positions),
            "due_l_count": len(due_l_positions),
        }])
        decisions = [
            CombinedLiveDecision(
                action="ACTIVE_STRATEGY_L_STANDALONE",
                strategy_leg="L",
                reason=(
                    "总策略模式=2，进入独立L龙头策略状态机；"
                    "ACDE2/D组合状态机本轮不生成买入计划。"
                ),
                source="active_strategy_profile",
            ),
            CombinedLiveDecision(
                action="BLOCK_ABCDE2_BY_STRATEGY_MODE",
                strategy_leg="A+C+D+E2",
                reason="active_strategy_profile.mode=2，阻断现有ACDE2/D计划，避免两套策略混跑。",
                source="active_strategy_profile",
            ),
        ]
        planned_orders: list[dict[str, Any]] = []

        # L 到期卖出优先于新开仓。即使关闭 L 买入，也不能阻断已有 L 持仓退出。
        for pos in due_l_positions:
            decisions.append(CombinedLiveDecision(
                action="PLAN_SELL_L",
                strategy_leg="L",
                ts_code=str(pos.get("ts_code", "")),
                name=str(pos.get("name", "")),
                side="SELL",
                quantity=self.as_int(pos.get("shares", 0)),
                reason=(
                    f"L持仓到期平仓，planned_exit_date={pos.get('planned_exit_date','')}，"
                    f"今日={today}，T+2收盘卖出。"
                ),
                source="positions.json",
            ))
            planned_orders.append(self.build_l_sell_order(pos, today))

        if open_non_l_positions:
            decisions.append(CombinedLiveDecision(
                action="BLOCK_L_BUY_BY_EXISTING_POSITION",
                strategy_leg="L",
                reason="当前仍有非L旧持仓，L独立模式不重复占用资金；先处理旧持仓再考虑L。",
                source="positions.json",
            ))
        elif open_l_positions:
            decisions.append(CombinedLiveDecision(
                action="BLOCK_L_BUY_BY_L_POSITION",
                strategy_leg="L",
                reason="仍有L持仓（含今日到期或待卖）；券商确认清仓前，L独立模式禁止新开仓。",
                source="positions.json",
            ))
        elif not bool(l_config.get("enabled", False)):
            decisions.append(CombinedLiveDecision(
                action="BLOCK_L_DISABLED",
                strategy_leg="L",
                reason="strategy_l.enabled=false，L已接入但当前未开启；默认继续使用模式1的ACDE2。",
                source="config.strategy_l",
            ))
        elif not bool(l_config.get("live_order_enabled", False)):
            decisions.append(CombinedLiveDecision(
                action="BLOCK_L_LIVE_ORDER",
                strategy_leg="L",
                reason="strategy_l.live_order_enabled=false，L只做独立研究/模拟，不生成实盘计划单。",
                source="config.strategy_l",
            ))
        else:
            # 只有 mode=2 且 strategy_l.enabled=true、live_order_enabled=true 时，
            # 才允许把昨日L信号转换为今日买单。
            # 这是防止 L 研究脚本一运行就污染当前实盘状态机的最后一道组合层门禁。
            yesterday_signal = self.load_yesterday_l_signal(today)
            if not yesterday_signal:
                decisions.append(CombinedLiveDecision(
                    action="NO_L_BUY",
                    strategy_leg="L",
                    reason="未找到 planned_buy_date 等于今日的昨日L信号，今日L不开仓。",
                    source=str(self.project_root / "reports" / "strategy_l"),
                ))
            else:
                l_order = self.build_l_buy_order(yesterday_signal, today)
                if l_order.get("round_lot_shares", 0) > 0:
                    planned_orders.append(l_order)
                    decisions.append(CombinedLiveDecision(
                        action="ALLOW_L_BUY",
                        strategy_leg="L",
                        ts_code=str(yesterday_signal.get("ts_code", "")),
                        name=str(yesterday_signal.get("name", "")),
                        side="BUY",
                        quantity=int(l_order.get("round_lot_shares", 0)),
                        reason=(
                            f"L昨日信号今日开仓：{yesterday_signal.get('ts_code')} "
                            f"{yesterday_signal.get('name')}，"
                            f"T+1开盘买入{l_order.get('round_lot_shares', 0)}股，"
                            f"计划金额约{float(l_order.get('planned_amount_by_equity', 0.0)):.0f}元，"
                            "T+2收盘卖出。"
                        ),
                        source=str(self.project_root / "reports" / "strategy_l"),
                    ))
                else:
                    decisions.append(CombinedLiveDecision(
                        action="BLOCK_L_BUY_ZERO_SHARES",
                        strategy_leg="L",
                        ts_code=str(yesterday_signal.get("ts_code", "")),
                        name=str(yesterday_signal.get("name", "")),
                        reason="L信号存在，但按资金/涨停价折算不足一手，今日不开仓。",
                        source=str(self.project_root / "reports" / "strategy_l"),
                    ))

        today_signal = self.load_today_l_signal(today)
        if today_signal:
            decisions.append(CombinedLiveDecision(
                action="L_SIGNAL_READY_FOR_NEXT_TRADE_DAY",
                strategy_leg="L_STATUS",
                ts_code=str(today_signal.get("ts_code", "")),
                name=str(today_signal.get("name", "")),
                reason=(
                    f"今日已生成L信号，计划买入日={today_signal.get('planned_buy_date','')}，"
                    f"计划平仓日={today_signal.get('planned_exit_date','')}。"
                ),
                source=str(self.project_root / "reports" / "strategy_l"),
            ))
        else:
            decisions.append(CombinedLiveDecision(
                action="NO_L_SIGNAL_TODAY",
                strategy_leg="L_STATUS",
                reason="今日尚未生成L信号；收盘流水线会运行 run_strategy_l_signal.py 更新。",
                source=str(self.project_root / "reports" / "strategy_l"),
            ))
        decision_df = pd.DataFrame([decision.__dict__ for decision in decisions])
        planned_orders_df = pd.DataFrame(planned_orders)
        return state_df, decision_df, planned_orders_df

    def model3_l_base_rule_pass(self, signal: dict[str, Any]) -> tuple[bool, str]:
        """调用回测与实盘共用的model=3基础规则。"""

        return model3_l_base_rule_pass(signal, self.config)

    def model3_l_replace_guard_pass(self, signal: dict[str, Any]) -> tuple[bool, str]:
        """调用回测与实盘共用的model=3替换保护。"""

        return model3_l_replace_guard_pass(signal, self.config)

    def build_model3_plan(self, today: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """model=3 自动切换实盘计划。

        实盘口径：
          1. 先生成当前 mode=1 计划。
          2. L 通过基础稳健条件时才参与。
          3. mode=1 无买入计划时，允许 L 补位。
          4. mode=1 有买入计划时，L 替换必须额外满足创业板、theme_limit_count>=2、非after_1430。
        """
        model3_config = self.config.get("strategy_model3", {})
        positions = self.load_positions()
        open_l_positions = [p for p in positions if self.is_open_position(p) and self.is_l_position(p)]
        due_l_positions = [
            p for p in open_l_positions
            if str(p.get("planned_exit_date", "99991231")) <= today
            or str(p.get("status", "")).lower() == "sell_pending"
        ]
        holding_l_positions = [p for p in open_l_positions if p not in due_l_positions]

        if due_l_positions or holding_l_positions:
            state_df = pd.DataFrame([{
                "today": today,
                "active_strategy_mode": self.active_strategy_mode(),
                "active_strategy_name": self.active_strategy_name(),
                "strategy_model3_enabled": bool(model3_config.get("enabled", False)),
                "strategy_model3_live_order_enabled": bool(model3_config.get("live_order_enabled", False)),
                "open_l_position_count": len(open_l_positions),
                "due_l_count": len(due_l_positions),
            }])
            decisions: list[CombinedLiveDecision] = []
            planned_orders: list[dict[str, Any]] = []
            for pos in due_l_positions:
                decisions.append(CombinedLiveDecision(
                    action="PLAN_SELL_L",
                    strategy_leg="L",
                    ts_code=str(pos.get("ts_code", "")),
                    name=str(pos.get("name", "")),
                    side="SELL",
                    quantity=self.as_int(pos.get("shares", 0)),
                    reason=f"model=3下L持仓到期，planned_exit_date={pos.get('planned_exit_date','')}，今日收盘平仓。",
                    source="positions.json",
                ))
                planned_orders.append(self.build_l_sell_order(pos, today))
            if holding_l_positions:
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_MODEL3_BUY_BY_L_POSITION",
                    strategy_leg="MODEL3",
                    reason="已有未到期L持仓，model=3不重复开仓。",
                    source="positions.json",
                ))
            return state_df, pd.DataFrame([d.__dict__ for d in decisions]), pd.DataFrame(planned_orders)

        mode1_state, mode1_decisions, mode1_orders = self.build_mode1_plan(today)
        manual_exit_positions = [
            p for p in positions
            if self.is_open_position(p) and self.is_manual_exit_only_position(p)
        ]
        if manual_exit_positions:
            extra = CombinedLiveDecision(
                action="BLOCK_MODEL3_BUY_BY_MANUAL_EXIT_POSITION",
                strategy_leg="MODEL3",
                reason="存在仅人工退出的旧持仓；用户手动卖出并完成持仓同步前，禁止L补位或替换。",
                source="positions.json",
            )
            decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
            return mode1_state, decisions, mode1_orders
        if not bool(model3_config.get("enabled", False)) or not bool(model3_config.get("live_order_enabled", False)):
            extra = CombinedLiveDecision(
                action="BLOCK_MODEL3_DISABLED",
                strategy_leg="MODEL3",
                reason="strategy_model3.enabled/live_order_enabled未同时开启，沿用mode=1计划。",
                source="config.strategy_model3",
            )
            decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
            return mode1_state, decisions, mode1_orders

        signal = self.load_yesterday_l_signal(today)
        if not signal:
            extra = CombinedLiveDecision(
                action="NO_MODEL3_L_SIGNAL",
                strategy_leg="MODEL3",
                reason="未找到planned_buy_date等于今日的昨日L信号，model=3沿用mode=1计划。",
                source=str(self.project_root / "reports" / "strategy_l"),
            )
            decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
            return mode1_state, decisions, mode1_orders

        base_ok, base_reason = self.model3_l_base_rule_pass(signal)
        if not base_ok:
            extra = CombinedLiveDecision(
                action="BLOCK_MODEL3_L_BASE_RULE",
                strategy_leg="MODEL3",
                ts_code=str(signal.get("ts_code", "")),
                name=str(signal.get("name", "")),
                reason=f"L信号未通过model=3基础稳健条件：{base_reason}；沿用mode=1计划。",
                source=str(self.project_root / "reports" / "strategy_l"),
            )
            decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
            return mode1_state, decisions, mode1_orders

        raw_buy_mask = (
            mode1_orders.get("side", pd.Series(dtype=str)).astype(str).str.upper().eq("BUY")
            if not mode1_orders.empty and "side" in mode1_orders.columns
            else pd.Series(False, index=mode1_orders.index)
        )
        relay_shadow_mask = (
            mode1_orders.get("planned_action", pd.Series(dtype=str)).astype(str).eq(
                "PLAN_D_RELAY_PAIRED_BUY"
            )
            if not mode1_orders.empty and "planned_action" in mode1_orders.columns
            else pd.Series(False, index=mode1_orders.index)
        )
        # D接力影子计划只是给09:30成对POV保存候选身份，不是普通mode=1买单，
        # 更不能让model=3误以为当前资金可用并切换成L替换单。
        buy_mask = raw_buy_mask & ~relay_shadow_mask
        mode1_buy_orders = mode1_orders[buy_mask].copy() if len(buy_mask) else pd.DataFrame()
        mode1_sell_orders = mode1_orders[~raw_buy_mask].copy() if len(raw_buy_mask) else mode1_orders.copy()
        l_order = self.build_l_buy_order(signal, today)
        if l_order.get("round_lot_shares", 0) <= 0:
            extra = CombinedLiveDecision(
                action="BLOCK_MODEL3_L_ZERO_SHARES",
                strategy_leg="MODEL3",
                ts_code=str(signal.get("ts_code", "")),
                name=str(signal.get("name", "")),
                reason="L信号通过条件，但按资金/价格折算不足一手；沿用mode=1计划。",
                source=str(self.project_root / "reports" / "strategy_l"),
            )
            decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
            return mode1_state, decisions, mode1_orders

        if mode1_buy_orders.empty:
            # 串行单仓守卫（2026-07-23 北方长龙 D+L 并存 bug）：mode1 无买入若是因未到期
            # 非L持仓（D/E2/A/C）占用资金所致（build_mode1_plan 已 BLOCK_ABC_BUY），L 补位
            # 同样不得开仓，否则与旧仓并存，违反 8302x daily_cash_constraint 串行单仓口径。
            # 今日到期、逾期和 sell_pending 仍属于实际旧仓；只要未确认清仓，L补位也禁止开仓。
            holding_non_l_positions = [
                p for p in positions
                if self.is_open_position(p) and not self.is_l_position(p)
            ]
            if holding_non_l_positions:
                blockers = "、".join(
                    f"{str(p.get('strategy_leg', '?')).upper()} {p.get('ts_code', '')} {p.get('name', '')}"
                    for p in holding_non_l_positions
                )
                extra = CombinedLiveDecision(
                    action="BLOCK_MODEL3_L_BY_HOLDING_POSITION",
                    strategy_leg="MODEL3",
                    ts_code=str(signal.get("ts_code", "")),
                    name=str(signal.get("name", "")),
                    reason=(
                        f"已有非L策略持仓（{blockers}）尚未实际清空，L补位不得开新仓"
                        f"（串行单仓口径，与A/C/E2/D一致）；券商确认清仓后再择机开L。"
                    ),
                    source="positions.json",
                )
                decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
                return mode1_state, decisions, mode1_orders
            filtered_mode1_decisions = mode1_decisions[
                ~mode1_decisions["action"].astype(str).isin({"ALLOW_D_INTRADAY_MONITOR"})
            ].copy()
            extra_decisions = [
                CombinedLiveDecision(
                    action="ALLOW_MODEL3_L_SUPPLEMENT",
                    strategy_leg="L",
                    ts_code=str(signal.get("ts_code", "")),
                    name=str(signal.get("name", "")),
                    side="BUY",
                    quantity=int(l_order.get("round_lot_shares", 0)),
                    reason=f"mode=1今日无买入计划，L通过基础条件补位：{base_reason}。",
                    source=str(self.project_root / "reports" / "strategy_l"),
                ),
                CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR",
                    strategy_leg="D",
                    reason="model=3今日使用L补位开仓，同一资金不启动D盘中监控。",
                    source="combined_state_machine",
                ),
            ]
            planned = pd.concat([mode1_sell_orders, pd.DataFrame([l_order])], ignore_index=True)
            decisions = pd.concat([filtered_mode1_decisions, pd.DataFrame([d.__dict__ for d in extra_decisions])], ignore_index=True)
        else:
            guard_ok, guard_reason = self.model3_l_replace_guard_pass(signal)
            if not guard_ok:
                extra = CombinedLiveDecision(
                    action="BLOCK_MODEL3_L_REPLACE_GUARD",
                    strategy_leg="MODEL3",
                    ts_code=str(signal.get("ts_code", "")),
                    name=str(signal.get("name", "")),
                    reason=f"mode=1已有买入计划，L未通过替换保护：{guard_reason}；沿用mode=1计划。",
                    source=str(self.project_root / "reports" / "strategy_l"),
                )
                decisions = pd.concat([mode1_decisions, pd.DataFrame([extra.__dict__])], ignore_index=True)
                return mode1_state, decisions, mode1_orders
            filtered_mode1_decisions = mode1_decisions[
                ~mode1_decisions["action"].astype(str).isin({
                    "ALLOW_ABC_BUY_PREVIEW",
                    "ALLOW_E2_BUY",
                    "ALLOW_D_INTRADAY_MONITOR",
                    "BLOCK_D_INTRADAY_MONITOR",
                })
            ].copy()
            extra_decisions = [
                CombinedLiveDecision(
                    action="ALLOW_MODEL3_L_REPLACE",
                    strategy_leg="L",
                    ts_code=str(signal.get("ts_code", "")),
                    name=str(signal.get("name", "")),
                    side="BUY",
                    quantity=int(l_order.get("round_lot_shares", 0)),
                    reason=f"mode=1有买入计划，但L通过替换保护：{guard_reason}；按model=3使用L替换mode=1买入。",
                    source=str(self.project_root / "reports" / "strategy_l"),
                ),
                CombinedLiveDecision(
                    action="BLOCK_MODE1_BUY_BY_MODEL3_L",
                    strategy_leg="A+C+D+E2",
                    reason="model=3选择L替换今日mode=1买入计划，避免同一资金重复占用。",
                    source="combined_state_machine",
                ),
            ]
            planned = pd.concat([mode1_sell_orders, pd.DataFrame([l_order])], ignore_index=True)
            decisions = pd.concat([filtered_mode1_decisions, pd.DataFrame([d.__dict__ for d in extra_decisions])], ignore_index=True)

        mode1_state = mode1_state.copy()
        mode1_state["active_strategy_mode"] = 3
        mode1_state["active_strategy_name"] = "MODEL3"
        mode1_state["strategy_model3_enabled"] = True
        mode1_state["strategy_model3_selected_rule"] = str(model3_config.get("selected_rule_name", ""))
        return mode1_state, decisions, planned

    def load_positions(self) -> list[dict[str, Any]]:
        if not self.positions_path.exists():
            return []
        try:
            data = json.loads(self.positions_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def load_latest_abc_orders(self) -> tuple[Path | None, pd.DataFrame]:
        try:
            path, orders = self.gateway.load_planned_orders("latest")
        except RuntimeError:
            return None, pd.DataFrame()
        except EmptyDataError:
            return None, pd.DataFrame()
        if not orders.empty and {"strategy_leg", "side"}.issubset(orders.columns):
            is_b = orders["strategy_leg"].astype(str).str.upper().eq("B")
            if self.is_b_strategy_removed():
                orders = orders[~is_b].copy()
            elif not self.is_b_new_entry_enabled():
                disabled_b_buy = is_b & orders["side"].astype(str).str.upper().eq("BUY")
                orders = orders[~disabled_b_buy].copy()
        return path, orders

    @staticmethod
    def is_open_position(position: dict[str, Any]) -> bool:
        return str(position.get("status", "open")).lower() in {"open", "sell_pending"}

    @staticmethod
    def is_d_position(position: dict[str, Any]) -> bool:
        return str(position.get("strategy_leg", "")).upper() == "D"

    @staticmethod
    def is_e2_position(position: dict[str, Any]) -> bool:
        return str(position.get("strategy_leg", "")).upper() == "E2"

    @staticmethod
    def as_int(value: Any, default: int = 0) -> int:
        try:
            if value in {None, ""}:
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def load_yesterday_e2_signal(self, today: str) -> dict[str, Any] | None:
        """找 today 之前最近的 E2 信号，且其 planned_buy_date == today。"""
        signal_dir = self.project_root / "reports" / "strategy_e2"
        if not signal_dir.exists():
            return None
        rolling = latest_signal_for_buy_date(signal_dir / "e2_signals_recent.json", today)
        if rolling is not None:
            return rolling
        files = sorted(signal_dir.glob("e2_signal_????????.json"))
        for f in reversed(files):
            date_part = f.stem.replace("e2_signal_", "")
            if date_part >= today:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if str(data.get("planned_buy_date", "")) == today:
                    return data
            except Exception:
                continue
        return None

    def load_today_e2_signal(self, today: str) -> dict[str, Any] | None:
        """加载今日收盘流水线已生成的 E2 信号（signal_date == today），用于盘中状态展示。"""
        rolling = signal_by_signal_date(
            self.project_root / "reports" / "strategy_e2" / "e2_signals_recent.json",
            today,
        )
        if rolling is not None:
            return rolling
        signal_path = self.project_root / "reports" / "strategy_e2" / f"e2_signal_{today}.json"
        if not signal_path.exists():
            return None
        try:
            return json.loads(signal_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def compute_e2_preview(self, today: str) -> dict[str, Any]:
        """盘中实时预判 E2 信号，用当前可用数据尽量多给信息。

        返回 dict 含 keys:
          data_date, segment_states, neutral_segs,
          has_scored_data, has_candidate,
          ts_code, name, circ_mv, fill_probability, limit_close (有候选时)
          reason (无候选时说明原因)
        """
        result: dict[str, Any] = {"data_date": today}
        try:
            from scripts.run_strategy_e2_signal import (
                compute_segment_retreat_states,
                load_e2_candidates,
                SCORED_PATH,
            )
            # ── 1. 板块状态（只需 raw/limit_list/*.csv，盘中即可用）─────────────
            segment_states = compute_segment_retreat_states(today)
            neutral_segs = [s for s, st in segment_states.items() if st == "neutral"]
            result["segment_states"] = segment_states
            result["neutral_segs"] = neutral_segs

            if not segment_states:
                result["reason"] = f"raw/limit_list/{today}.csv 尚未采集，板块状态未知"
                return result
            # 此处的板块状态只用于盘中预览。正式E2 neutral必须由统一R1特征链计算，
            # 不能再用旧“连续自然交易日”算法提前阻断，否则实盘会与回测漂移。

            # ── 2. 候选（需要 limit_up_fill_scored.csv 含今日记录）────────────
            has_scored = False
            if SCORED_PATH.exists():
                try:
                    tmp = pd.read_csv(SCORED_PATH, usecols=["trade_date"], nrows=500)
                    has_scored = tmp["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).eq(today).any()
                except Exception:
                    pass
            result["has_scored_data"] = has_scored

            if not has_scored:
                result["reason"] = (
                    f"板块状态已判定：neutral板块={neutral_segs}，E2前提满足。"
                    f"候选股待15:10收盘流水线⑦步采集scored数据后确定。"
                )
                return result

            candidates = load_e2_candidates(today)
            if candidates.empty:
                result["has_candidate"] = False
                result["reason"] = (
                    "统一R1每日第一名未通过neutral/成交可靠性/"
                    "13:30~14:30入场门禁；当日不回补第二名。"
                )
                return result

            top = candidates.iloc[0]
            result["has_candidate"] = True
            result["candidate_count"] = len(candidates)
            result["ts_code"] = str(top.get("ts_code", ""))
            result["name"] = str(top.get("name", ""))
            result["circ_mv"] = float(top.get("circ_mv", 0) or 0)
            result["fill_probability"] = float(top.get("fill_probability", 0) or 0)
            result["limit_close"] = float(top.get("limit_close", 0) or 0)
            result["r1_scenario_rank"] = int(top.get("scenario_rank", 0) or 0)
            result["exit_rule"] = str(top.get("exit_rule", ""))
        except Exception as exc:
            result["reason"] = f"预判异常：{exc}"
        return result

    def build_e2_buy_order(self, signal: dict[str, Any], today: str) -> dict[str, Any]:
        limit_close = float(signal.get("limit_close", 0.0))
        initial_equity = float(self.config.get("position", {}).get("initial_cash", 500_000.0))
        planned_amount = initial_equity * _E2_POSITION_PCT
        if str(self.config.get("trade_mode", "")).lower() == "live":
            # 0=不限额（82.5%目标仓位接管），>0=单笔限额。
            max_single_order_amount = float(
                self.config.get("live_trade", {}).get("max_single_order_amount", 0) or 0
            )
            if max_single_order_amount > 0:
                planned_amount = min(planned_amount, max_single_order_amount)
        round_lot = round_lot_shares_below_amount(planned_amount, limit_close)
        estimated_shares = round_lot
        planned_amount = round_lot * limit_close
        planned_position_pct = planned_amount / initial_equity if initial_equity > 0 else _E2_POSITION_PCT
        return {
            "paper_order_id": f"E2-BUY-{today}-{signal.get('ts_code','')}",
            "signal_date": signal.get("signal_date", ""),
            "strategy_leg": "E2",
            "planned_order_date": today,
            "side": "BUY",
            "ts_code": str(signal.get("ts_code", "")),
            "name": str(signal.get("name", "")),
            "planned_action": "PLAN_BUY_T1_OPEN",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": planned_position_pct,
            "planned_equity": initial_equity,
            "planned_amount_by_equity": planned_amount,
            "reference_price": limit_close,
            "estimated_shares": estimated_shares,
            "round_lot_shares": round_lot,
            "risk_flags": "",
            "live_order_enabled": True,
            # R1规则允许T+2或T+3退出。exit_offset相对信号日，买入发生在T+1，
            # 因此持仓登记使用exit_offset-1；信号缺字段时沿用T+2旧安全默认值。
            "exit_n_days": max(int(signal.get("exit_offset", 2) or 2) - 1, 1),
        }

    def build_e2_sell_order(self, position: dict[str, Any], today: str) -> dict[str, Any]:
        shares = self.as_int(position.get("shares", 0))
        return {
            "paper_order_id": f"E2-SELL-{today}-{position.get('ts_code','')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "E2",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": "PLAN_SELL_T2_CLOSE",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": 0.0,
            "planned_equity": 0.0,
            "planned_amount_by_equity": 0.0,
            "reference_price": float(position.get("buy_price", 0.0)),
            "estimated_shares": shares,
            "round_lot_shares": shares,
            "risk_flags": "E2_SELL_T2_CLOSE",
            "live_order_enabled": True,
        }

    def build_d_sell_decision(
        self,
        position: dict[str, Any],
        today: str,
        reason: str | None = None,
        action: str = "PLAN_SELL_D_FIRST",
    ) -> CombinedLiveDecision:
        return CombinedLiveDecision(
            action=action,
            strategy_leg="D",
            ts_code=str(position.get("ts_code", "")),
            name=str(position.get("name", "")),
            side="SELL",
            quantity=self.as_int(position.get("shares", 0)),
            reason=reason or (
                f"D持仓计划平仓日={position.get('planned_exit_date', '')}，今日={today}；"
                "只有确认卖出释放的资金才允许用于A/C/E2接力。"
            ),
            source="positions.json",
        )

    def build_d_sell_order(
        self,
        position: dict[str, Any],
        today: str,
        planned_action: str = "PLAN_SELL_D_FIRST",
    ) -> dict[str, Any]:
        return {
            "paper_order_id": f"D-SELL-{today}-{position.get('ts_code', '')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "D",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": planned_action,
            "order_status": "PLAN_ONLY",
            "planned_position_pct": 0.0,
            "planned_equity": 0.0,
            "planned_amount_by_equity": 0.0,
            "reference_price": 0.0,
            "estimated_shares": self.as_int(position.get("shares", 0)),
            "round_lot_shares": self.as_int(position.get("shares", 0)),
            "risk_flags": "D_SELL_FIRST",
            "live_order_enabled": False,
        }

    def build_abc_buy_decisions(self, orders: pd.DataFrame, source: str) -> list[CombinedLiveDecision]:
        if orders.empty or "side" not in orders.columns:
            return []
        rows = orders[orders["side"].astype(str).str.upper() == "BUY"].copy()
        if rows.empty:
            return []
        qty = rows.apply(
            lambda row: self.as_int(row.get("round_lot_shares", row.get("estimated_shares", 0))),
            axis=1,
        )
        ref_price = pd.to_numeric(rows.get("reference_price", 0.0), errors="coerce").fillna(0.0)
        rows = rows[(qty > 0) & (ref_price > 0)].copy()
        decisions = []
        for _, row in rows.iterrows():
            decisions.append(
                CombinedLiveDecision(
                    action="ALLOW_ABC_BUY_PREVIEW",
                    strategy_leg=str(row.get("strategy_leg", "")),
                    ts_code=str(row.get("ts_code", "")),
                    name=str(row.get("name", "")),
                    side="BUY",
                    quantity=self.as_int(row.get("round_lot_shares", row.get("estimated_shares", 0))),
                    reason="无D未平仓或待卖持仓，允许进入A/C买入预览；真实下单仍需LiveOrderGateway二次校验。",
                    source=source,
                )
            )
        return decisions

    def build_mode1_plan(self, today: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        positions = self.load_positions()
        open_positions = [p for p in positions if self.is_open_position(p)]
        open_d_positions = [p for p in open_positions if self.is_d_position(p)]
        open_non_d_positions = [p for p in open_positions if not self.is_d_position(p)]
        open_e2_positions = [p for p in open_non_d_positions if self.is_e2_position(p)]
        due_e2_positions = [
            p for p in open_e2_positions
            if str(p.get("planned_exit_date", "99991231")) <= today
            or str(p.get("status", "")).lower() == "sell_pending"
        ]
        # 今日到期集合只用于生成退出计划和T+0同票保护，绝不能再从开仓占用中排除。
        # 新口径（2026-08-03）：旧仓实际清空前一律不买新仓，取消尾盘旧仓与早盘新仓并存。
        # ABC 到期卖出仍由 daemon check_and_close_positions 执行，不需要额外计划单行。
        due_non_d_positions = [
            p for p in open_non_d_positions
            if not self.is_manual_exit_only_position(p)
            and (
                str(p.get("planned_exit_date", "99991231")) <= today
                or str(p.get("status", "")).lower() == "sell_pending"
            )
        ]
        # 今日到期卖出的标的代码（T+0限制：当日不可再买入同一标的）
        due_selling_codes: set[str] = {str(p.get("ts_code", "")) for p in due_non_d_positions}
        abc_path, abc_orders = self.load_latest_abc_orders()
        # ABC 计划单日期校验（E2 planned_buy_date==today 的同款保护）：
        # load_latest_abc_orders 只按文件时间取最新，若某晚收盘流水线失败，
        # 第二天会读到前一天的计划——只执行 planned_order_date==today 的行，陈旧计划一律丢弃。
        if not abc_orders.empty and "planned_order_date" in abc_orders.columns:
            date_ok = abc_orders["planned_order_date"].astype(str).str.strip() == str(today)
            stale = abc_orders[~date_ok]
            if not stale.empty:
                import logging as _logging
                for _, srow in stale.iterrows():
                    _logging.getLogger(__name__).warning(
                        "ABC计划单 %s %s planned_order_date=%s ≠ today=%s，视为陈旧计划跳过。",
                        srow.get("ts_code", ""), srow.get("name", ""),
                        srow.get("planned_order_date", ""), today,
                    )
            abc_orders = abc_orders[date_ok].copy()

        decisions: list[CombinedLiveDecision] = []
        planned_orders: list[dict[str, Any]] = []
        state_rows: list[dict[str, Any]] = [
            {
                "today": today,
                "active_strategy_mode": self.active_strategy_mode(),
                "active_strategy_name": self.active_strategy_name(),
                "open_position_count": len(open_positions),
                "open_d_position_count": len(open_d_positions),
                "open_non_d_position_count": len(open_non_d_positions),
                "open_e2_position_count": len(open_e2_positions),
                "due_e2_count": len(due_e2_positions),
                "abc_planned_orders_path": str(abc_path or ""),
                "abc_planned_order_count": int(len(abc_orders)) if not abc_orders.empty else 0,
            }
        ]

        # ── E2 到期卖出（R1可能T+2/T+3，以planned_exit_date为唯一依据） ───────
        for pos in due_e2_positions:
            decisions.append(
                CombinedLiveDecision(
                    action="PLAN_SELL_E2",
                    strategy_leg="E2",
                    ts_code=str(pos.get("ts_code", "")),
                    name=str(pos.get("name", "")),
                    side="SELL",
                    quantity=self.as_int(pos.get("shares", 0)),
                    reason=(
                        f"E2持仓到期平仓，planned_exit_date={pos.get('planned_exit_date','')}，"
                        f"今日={today}，按R1信号锁定的到期日收盘卖出。"
                    ),
                    source="positions.json",
                )
            )
            planned_orders.append(self.build_e2_sell_order(pos, today))

        # ── D / ABC / 空仓 主流程 ──────────────────────────────────────────────
        if open_d_positions:
            due_d = [
                p for p in open_d_positions
                if str(p.get("planned_exit_date", "99991231")) <= today
                or str(p.get("status", "")).lower() == "sell_pending"
            ]
            abc_decisions_for_d = self.build_abc_buy_decisions(abc_orders, str(abc_path or ""))
            yesterday_e2_signal_for_d = self.load_yesterday_e2_signal(today)
            e2_order_for_d: dict[str, Any] | None = None
            if yesterday_e2_signal_for_d:
                e2_buy_code = str(yesterday_e2_signal_for_d.get("ts_code", ""))
                if e2_buy_code not in due_selling_codes:
                    e2_order_for_d = self.build_e2_buy_order(yesterday_e2_signal_for_d, today)
            relay_d_for_abc = bool(abc_decisions_for_d)
            relay_d_for_e2 = bool(e2_order_for_d and e2_order_for_d.get("round_lot_shares", 0) > 0)
            relay_d_for_abce2 = relay_d_for_abc or relay_d_for_e2
            relay_candidate_order: dict[str, Any] | None = None
            relay_candidate_decision: CombinedLiveDecision | None = None
            # D已经到默认T+2平仓日时仍按收盘退出，不做开盘接力；只有未到期D
            # 为A/C/E2让路时才生成成对POV影子候选。
            paired_relay_needed = relay_d_for_abce2 and not due_d
            if relay_d_for_abc and paired_relay_needed:
                # 组合优先级始终是A/C > E2。D还没卖时，原代码只留下BLOCK动作，
                # 09:23执行层拿不到“卖完以后要买谁”，只能整仓卖完再重跑状态机。
                # 现在把第一顺位A/C计划复制成只供成对POV读取的影子计划；它不会被
                # 普通09:20买入路径执行，只有D确认卖出资金后才能逐片买入。
                first_abc = abc_decisions_for_d[0]
                abc_buy_rows = abc_orders[
                    abc_orders.get("side", pd.Series(dtype=str)).astype(str).str.upper().eq("BUY")
                ].copy() if not abc_orders.empty else pd.DataFrame()
                if not abc_buy_rows.empty:
                    exact = abc_buy_rows[
                        abc_buy_rows.get("ts_code", pd.Series(dtype=str)).astype(str).eq(first_abc.ts_code)
                    ]
                    source_row = (exact.iloc[0] if not exact.empty else abc_buy_rows.iloc[0]).to_dict()
                    relay_candidate_order = dict(source_row)
                    relay_candidate_decision = first_abc
            elif relay_d_for_e2 and paired_relay_needed and e2_order_for_d is not None:
                relay_candidate_order = dict(e2_order_for_d)
                relay_candidate_decision = CombinedLiveDecision(
                    action="PLAN_D_RELAY_PAIRED_BUY",
                    strategy_leg="E2",
                    ts_code=str(e2_order_for_d.get("ts_code", "")),
                    name=str(e2_order_for_d.get("name", "")),
                    side="BUY",
                    quantity=int(e2_order_for_d.get("round_lot_shares", 0) or 0),
                    reason="D确认卖出一片后，才允许用该片实际释放资金买入E2。",
                    source=str(self.project_root / "reports" / "strategy_e2"),
                )

            if relay_candidate_order is not None and relay_candidate_decision is not None:
                relay_candidate_order["planned_action"] = "PLAN_D_RELAY_PAIRED_BUY"
                relay_candidate_order["order_status"] = "WAIT_D_CONFIRMED_SELL"
                relay_candidate_order["live_order_enabled"] = False
                relay_candidate_order["risk_flags"] = "D_RELAY_PAIRED_POV_ONLY"
                relay_candidate_order["relay_priority"] = "A/C" if relay_d_for_abc else "E2"
                planned_orders.append(relay_candidate_order)
                decisions.append(CombinedLiveDecision(
                    action="PLAN_D_RELAY_PAIRED_BUY",
                    strategy_leg=str(relay_candidate_order.get("strategy_leg", relay_candidate_decision.strategy_leg)),
                    ts_code=str(relay_candidate_order.get("ts_code", relay_candidate_decision.ts_code)),
                    name=str(relay_candidate_order.get("name", relay_candidate_decision.name)),
                    side="BUY",
                    quantity=self.as_int(relay_candidate_order.get(
                        "round_lot_shares", relay_candidate_order.get("estimated_shares", relay_candidate_decision.quantity)
                    )),
                    reason=(
                        "D接力候选已锁定，但普通买入路径不得执行；09:23只卖竞价安全部分，"
                        "09:30后按D实际成交一片、候选买入一片的资金中性POV执行。"
                    ),
                    source=str(relay_candidate_decision.source),
                ))
            relay_legs = []
            if relay_d_for_abc:
                relay_legs.append("A/C")
            if relay_d_for_e2:
                relay_legs.append("E2")
            relay_reason = "/".join(relay_legs)
            if due_d or relay_d_for_abce2:
                sell_d_positions = due_d if due_d else open_d_positions
                for position in due_d:
                    decisions.append(self.build_d_sell_decision(
                        position,
                        today,
                        action="PLAN_SELL_D_T2_CLOSE",
                        reason=(
                            f"D持仓到默认T+2平仓日，planned_exit_date={position.get('planned_exit_date', '')}，"
                            f"今日={today}，按D回测口径等待14:53收盘平仓，不在09:23集合竞价卖出。"
                        ),
                    ))
                    planned_orders.append(self.build_d_sell_order(
                        position,
                        today,
                        planned_action="PLAN_SELL_D_T2_CLOSE",
                    ))
                for position in sell_d_positions:
                    if position in due_d:
                        continue
                    decisions.append(self.build_d_sell_decision(
                        position,
                        today,
                        reason=(
                            f"D持仓未到默认T+2平仓日，但今日存在{relay_reason}接力买入计划；"
                            f"09:23只卖竞价安全部分，09:30后按确认卖出资金与{relay_reason}成对POV接力。"
                        ),
                    ))
                    planned_orders.append(self.build_d_sell_order(position, today))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_ABC_BUY", strategy_leg="A+C",
                    reason="D持仓尚未确认卖出，普通A/C买入路径阻断；接力候选仅由资金中性成对POV执行。",
                    source="combined_state_machine",
                ))
                if relay_d_for_e2:
                    decisions.append(CombinedLiveDecision(
                        action="BLOCK_E2_BUY_UNTIL_D_SOLD",
                        strategy_leg="E2",
                        ts_code=str(yesterday_e2_signal_for_d.get("ts_code", "")) if yesterday_e2_signal_for_d else "",
                        name=str(yesterday_e2_signal_for_d.get("name", "")) if yesterday_e2_signal_for_d else "",
                        side="BUY",
                        quantity=int(e2_order_for_d.get("round_lot_shares", 0)) if e2_order_for_d else 0,
                        reason="今日存在E2开仓计划，但D尚未确认卖出；只允许成对POV用D实际释放资金逐片买入。",
                        source=str(self.project_root / "reports" / "strategy_e2"),
                    ))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="已有D持仓处于待卖状态，今日不启动新的D盘中买入监控。",
                    source="combined_state_machine",
                ))
            else:
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_ABC_BUY", strategy_leg="A+C",
                    reason="D持仓未到平仓日，阻断A/C买入。",
                    source="combined_state_machine",
                ))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="已有D持仓占用资金，今日不启动新的D盘中买入监控。",
                    source="combined_state_machine",
                ))

        elif open_non_d_positions:
            # 只要非D旧仓尚未实际清空（含今日到期、逾期、sell_pending），就阻断所有新开仓。
            decisions.append(CombinedLiveDecision(
                action="BLOCK_ABC_BUY", strategy_leg="A+C",
                reason="存在尚未实际清空的旧策略仓（A/C、E2/L或仅人工退出的历史B仓），取消衔接开仓；券商确认清仓前不允许新买入。",
                source="positions.json",
            ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                reason="存在尚未实际清空的旧策略仓，D盘中策略跳过；确认清仓后才允许下一次正常开仓。",
                source="positions.json",
            ))

        else:
            # 只有账户无旧策略仓时，才按优先级 ABC > E2 > D 决定今日行动。
            # D策略接力是独立流程：09:23卖出D并确认券商空仓后，状态机会重新生成计划；
            # 未确认卖出前仍不会进入本分支。
            # 按优先级 ABC > E2 > D 决定今日行动
            abc_decisions = self.build_abc_buy_decisions(abc_orders, str(abc_path or ""))
            # T+0限制：过滤今日集合竞价已卖出的标的（同日不可再买入）
            if due_selling_codes:
                abc_decisions = [d for d in abc_decisions if d.ts_code not in due_selling_codes]
                abc_orders_buy = abc_orders[
                    ~abc_orders["ts_code"].astype(str).isin(due_selling_codes)
                ] if not abc_orders.empty else abc_orders
            else:
                abc_orders_buy = abc_orders
            if abc_decisions:
                decisions.extend(abc_decisions)
                planned_orders.extend(abc_orders_buy.to_dict("records"))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="今日存在A/C买入计划，D盘中策略不再使用同一资金。",
                    source="combined_state_machine",
                ))
            else:
                # 无 ABC，检查 E2 昨日信号
                yesterday_signal = self.load_yesterday_e2_signal(today)
                e2_order: dict[str, Any] | None = None
                if yesterday_signal:
                    buy_code = str(yesterday_signal.get("ts_code", ""))
                    if buy_code in due_selling_codes:
                        # 今日集合竞价已卖出同一标的，T+0限制不可当日回买
                        import logging as _logging
                        _logging.getLogger(__name__).warning(
                            "E2新买入标的 %s 与今日集合竞价卖出标的相同，T+0限制，跳过买入。", buy_code
                        )
                    else:
                        e2_order = self.build_e2_buy_order(yesterday_signal, today)

                if e2_order and e2_order.get("round_lot_shares", 0) > 0:
                    planned_orders.append(e2_order)
                    decisions.append(CombinedLiveDecision(
                        action="ALLOW_E2_BUY",
                        strategy_leg="E2",
                        ts_code=str(yesterday_signal.get("ts_code", "")),  # type: ignore[union-attr]
                        name=str(yesterday_signal.get("name", "")),  # type: ignore[union-attr]
                        side="BUY",
                        quantity=e2_order.get("round_lot_shares", 0),
                        reason=(
                            f"E2昨日信号今日开仓：{yesterday_signal.get('ts_code')} "  # type: ignore[union-attr]
                            f"{yesterday_signal.get('name')}，"  # type: ignore[union-attr]
                            f"T+1开盘买入{e2_order.get('round_lot_shares', 0)}股，"
                            f"计划金额约{float(e2_order.get('planned_amount_by_equity', 0.0)):.0f}元，"
                            f"按R1信号在T+{int(yesterday_signal.get('exit_offset', 2) or 2)}收盘卖出。"  # type: ignore[union-attr]
                        ),
                        source=str(self.project_root / "reports" / "strategy_e2"),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="NO_ABC_BUY", strategy_leg="A+C",
                        reason="今日无A/C买入计划，E2代替开仓。",
                        source=str(abc_path or ""),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                        reason="E2今日开仓使用同一资金，D盘中监控跳过。",
                        source="combined_state_machine",
                    ))
                else:
                    decisions.append(CombinedLiveDecision(
                        action="NO_ABC_BUY", strategy_leg="A+C",
                        reason="今日没有A/C买入计划。",
                        source=str(abc_path or ""),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="ALLOW_D_INTRADAY_MONITOR", strategy_leg="D",
                        reason="无持仓且无A/C买入计划，允许启动D盘中监控；D本身仍需实时行情、成交概率和风控校验。",
                        source="combined_state_machine",
                    ))

        # ── E2 盘中状态显示（摘要用） ─────────────────────────────────────────
        has_e2_buy = any(d.action == "ALLOW_E2_BUY" for d in decisions)
        has_e2_sell = any(d.action == "PLAN_SELL_E2" for d in decisions)
        has_abc_buy = any(d.action == "ALLOW_ABC_BUY_PREVIEW" for d in decisions)
        has_e2_waiting_for_d_sell = any(d.action == "BLOCK_E2_BUY_UNTIL_D_SOLD" for d in decisions)

        if has_e2_sell:
            e2_status_action = "PLAN_SELL_E2_TODAY"
            e2_status_reason = f"E2持仓今日到期，收盘前平仓（14:53 job_afternoon 执行）。"
        elif has_e2_buy:
            e2_status_action = "ALLOW_E2_BUY_TODAY"
            e2_status_reason = "E2今日T+1开仓，已加入组合计划单。"
        elif has_e2_waiting_for_d_sell:
            e2_status_action = "WAIT_D_SELL_THEN_ALLOW_E2_BUY"
            e2_status_reason = (
                "今日存在E2接力候选；09:23只卖D竞价安全部分，09:30后按D确认释放资金"
                "与E2成对POV，不要求先整仓清空D。"
            )
        elif open_positions:
            e2_status_action = "BLOCK_E2"
            e2_status_reason = f"账户有 {len(open_positions)} 个未平仓头寸，E2 不触发。"
        elif has_abc_buy:
            e2_status_action = "BLOCK_E2"
            e2_status_reason = "今日 A/C 已生成买入计划，E2 不触发（资金冲突）。"
        else:
            e2_status_action = "ALLOW_E2_SIGNAL"
            today_signal = self.load_today_e2_signal(today)
            if today_signal:
                ts = today_signal.get("ts_code", "")
                nm = today_signal.get("name", "")
                buy_dt = today_signal.get("planned_buy_date", "")
                lc = float(today_signal.get("limit_close", 0) or 0)
                fp = float(today_signal.get("fill_probability", 0) or 0)
                circ = float(today_signal.get("circ_mv", 0) or 0)
                e2_status_reason = (
                    f"今日已扫描到E2候选：{ts} {nm}，"
                    f"计划买入日={buy_dt}，"
                    f"涨停参考价={lc:.2f}元，"
                    f"流通市值={circ/10000:.1f}亿，"
                    f"成交概率={fp:.1%}。"
                    f"（明日09:20组合状态机将生成ALLOW_E2_BUY开仓计划）"
                )
            else:
                preview = self.compute_e2_preview(today)
                neutral_segs = preview.get("neutral_segs", [])
                if preview.get("has_candidate"):
                    ts = preview["ts_code"]
                    nm = preview["name"]
                    circ = preview["circ_mv"]
                    fp = preview["fill_probability"]
                    lc = preview["limit_close"]
                    e2_status_reason = (
                        f"E2预判可触发：候选 {ts} {nm}，"
                        f"流通市值={circ/10000:.1f}亿，成交概率={fp:.1%}，参考价={lc:.2f}元，"
                        f"neutral板块={neutral_segs}。"
                        f"（15:10流水线⑦步生成正式信号文件和明日买入计划）"
                    )
                elif neutral_segs and not preview.get("has_scored_data", True):
                    e2_status_reason = (
                        f"板块状态已判定：neutral板块={neutral_segs}，E2前提满足。"
                        f"候选股待15:10流水线⑦步采集scored数据后确定。"
                    )
                elif preview.get("has_candidate") is False:
                    e2_status_reason = preview.get("reason", "E2今日不触发。")
                else:
                    e2_status_reason = preview.get(
                        "reason",
                        "今日raw数据尚未采集，等待15:10收盘流水线⑦步自动扫描。"
                    )
        decisions.append(CombinedLiveDecision(
            action=e2_status_action,
            strategy_leg="E2_STATUS",
            reason=e2_status_reason,
            source="combined_state_machine",
        ))

        decision_df = pd.DataFrame([decision.__dict__ for decision in decisions])
        state_df = pd.DataFrame(state_rows)
        planned_orders_df = pd.DataFrame(planned_orders)
        return state_df, decision_df, planned_orders_df

    def build_plan(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        today = today_beijing().strftime("%Y%m%d")
        if self.active_strategy_mode() == 2:
            return self.build_l_mode_plan(today)
        if self.active_strategy_mode() == 3:
            return self.build_model3_plan(today)
        return self.build_mode1_plan(today)

    def write_plan(self) -> dict[str, Path]:
        today = today_beijing().strftime("%Y%m%d")
        state, decisions, planned_orders = self.build_plan()
        state_path = self.output_dir / f"combined_state_{today}.csv"
        decisions_path = self.output_dir / f"combined_decisions_{today}.csv"
        orders_path = self.output_dir / f"combined_planned_orders_{today}.csv"
        md_path = self.output_dir / f"combined_plan_{today}.md"
        state.to_csv(state_path, index=False, encoding="utf-8-sig")
        decisions.to_csv(decisions_path, index=False, encoding="utf-8-sig")
        planned_orders.to_csv(orders_path, index=False, encoding="utf-8-sig")
        self.write_markdown(md_path, state, decisions, planned_orders)

        # L 模式下打印 L 状态后直接返回，避免在模式2里继续展示 E2 造成误解。
        if self.active_strategy_mode() == 2:
            print()
            print("─" * 60)
            print("  策略 L 状态（独立龙头策略，默认不开启实盘买入）")
            print("─" * 60)
            if not decisions.empty and "strategy_leg" in decisions.columns:
                l_rows = decisions[decisions["strategy_leg"].astype(str).isin({"L", "L_STATUS"})]
                if l_rows.empty:
                    print("  — L 无决策")
                for _, row in l_rows.iterrows():
                    action = str(row.get("action", ""))
                    code = str(row.get("ts_code", ""))
                    nm = str(row.get("name", ""))
                    reason = str(row.get("reason", ""))
                    if action == "ALLOW_L_BUY":
                        print(f"  ✅ 今日 L 开仓计划 → {code} {nm}  {row.get('quantity', 0)}股")
                    elif action == "PLAN_SELL_L":
                        print(f"  ⏳ 今日 L 到期平仓 → {code} {nm}  {row.get('quantity', 0)}股  14:53收盘前卖出")
                    elif action == "BLOCK_L_LIVE_ORDER":
                        print(f"  ✘ L实盘买入未开启：{reason}")
                    else:
                        print(f"  {action}：{reason}")
            print("─" * 60)
            return {"state": state_path, "decisions": decisions_path, "planned_orders": orders_path, "markdown": md_path}

        if self.active_strategy_mode() == 3:
            print()
            print("─" * 60)
            print("  model=3 状态（mode=1优先，L按规则补位/替换）")
            print("─" * 60)
            if not decisions.empty and "strategy_leg" in decisions.columns:
                model3_rows = decisions[decisions["strategy_leg"].astype(str).isin({"MODEL3", "L"})]
                if model3_rows.empty:
                    print("  — model=3 无额外切换决策")
                for _, row in model3_rows.iterrows():
                    action = str(row.get("action", ""))
                    code = str(row.get("ts_code", ""))
                    nm = str(row.get("name", ""))
                    reason = str(row.get("reason", ""))
                    qty = row.get("quantity", 0)
                    if action in {"ALLOW_MODEL3_L_SUPPLEMENT", "ALLOW_MODEL3_L_REPLACE"}:
                        print(f"  ✅ {action} → {code} {nm}  {qty}股")
                        print(f"     {reason}")
                    elif action == "PLAN_SELL_L":
                        print(f"  ⏳ L到期平仓 → {code} {nm}  {qty}股")
                    else:
                        print(f"  {action}：{reason}")
            print("─" * 60)
            return {"state": state_path, "decisions": decisions_path, "planned_orders": orders_path, "markdown": md_path}

        # 打印 E2 状态
        print()
        print("─" * 60)
        print("  策略 E2 状态（无前视单账户R1 · T+1买/T+2或T+3卖）")
        print("─" * 60)
        if not decisions.empty and "strategy_leg" in decisions.columns:
            # 当日买卖动作行（PLAN_SELL_E2 / ALLOW_E2_BUY）
            e2_act_rows = decisions[
                decisions["strategy_leg"].astype(str).eq("E2")
                & decisions["action"].astype(str).isin({"PLAN_SELL_E2", "ALLOW_E2_BUY"})
            ]
            for _, row in e2_act_rows.iterrows():
                action = str(row.get("action", ""))
                code = str(row.get("ts_code", ""))
                nm = str(row.get("name", ""))
                qty = row.get("quantity", 0)
                try:
                    qty_str = f"{int(float(qty))}股" if qty and str(qty) not in {"", "nan", "0"} else ""
                except Exception:
                    qty_str = ""
                if action == "PLAN_SELL_E2":
                    print(f"  ⏳ 今日 T+2 平仓 → {code} {nm}  {qty_str}  14:53收盘前卖出")
                elif action == "ALLOW_E2_BUY":
                    amount_text = ""
                    if not planned_orders.empty and "ts_code" in planned_orders.columns:
                        order_rows = planned_orders[
                            planned_orders["ts_code"].astype(str).eq(code)
                            & planned_orders["side"].astype(str).str.upper().eq("BUY")
                        ]
                        if not order_rows.empty:
                            amount = float(order_rows.iloc[0].get("planned_amount_by_equity", 0.0) or 0.0)
                            amount_text = f"  计划金额约{amount:.0f}元"
                    print(f"  ✅ 今日 T+1 开仓 → {code} {nm}  {qty_str}{amount_text}  开盘买入")

            # 汇总状态行
            e2_status_rows = decisions[decisions["strategy_leg"].astype(str).eq("E2_STATUS")]
            if not e2_status_rows.empty:
                e2 = e2_status_rows.iloc[0]
                action = str(e2.get("action", ""))
                reason = str(e2.get("reason", ""))
                if action == "ALLOW_E2_SIGNAL":
                    today_sig = self.load_today_e2_signal(today)
                    if today_sig:
                        code = today_sig.get("ts_code", "")
                        nm = today_sig.get("name", "")
                        buy_dt = today_sig.get("planned_buy_date", "")
                        lc = float(today_sig.get("limit_close", 0) or 0)
                        fp = float(today_sig.get("fill_probability", 0) or 0)
                        circ = float(today_sig.get("circ_mv", 0) or 0)
                        print(f"  ✔ 今日已扫描到E2候选：{code} {nm}")
                        print(f"     计划买入日：{buy_dt}  涨停参考价：{lc:.2f}元")
                        print(f"     流通市值：{circ/10000:.1f}亿  成交概率：{fp:.1%}")
                        print(f"     → 明日09:20组合状态机自动生成开仓计划")
                    else:
                        preview = self.compute_e2_preview(today)
                        neutral_segs = preview.get("neutral_segs", [])
                        if preview.get("has_candidate"):
                            print(f"  ✔ E2预判可触发：{preview['ts_code']} {preview['name']}")
                            print(f"     neutral板块={neutral_segs}")
                            print(f"     流通市值={preview['circ_mv']/10000:.1f}亿  成交概率={preview['fill_probability']:.1%}  参考价={preview['limit_close']:.2f}元")
                            print(f"     → 15:10流水线⑦步生成正式信号文件和明日买入计划")
                        elif neutral_segs and not preview.get("has_scored_data", True):
                            print(f"  ✔ 板块状态已判定：neutral板块={neutral_segs}，E2前提满足")
                            print(f"     → 候选股待15:10流水线⑦步采集scored数据后确定")
                        elif preview.get("has_candidate") is False:
                            print(f"  ✘ E2今日不触发：{preview.get('reason', '')}")
                        else:
                            print(f"  ？ {preview.get('reason', 'raw数据尚未采集，等待15:10流水线')}")
                elif action in {"ALLOW_E2_BUY_TODAY", "PLAN_SELL_E2_TODAY"}:
                    print(f"  ✔ {reason}")
                elif action == "BLOCK_E2":
                    print(f"  ✘ E2不触发：{reason}")
                else:
                    print(f"  {action}：{reason}")
            elif e2_act_rows.empty:
                print("  — E2 无决策")
        print("─" * 60)

        return {"state": state_path, "decisions": decisions_path, "planned_orders": orders_path, "markdown": md_path}

    @staticmethod
    def write_markdown(path: Path, state: pd.DataFrame, decisions: pd.DataFrame, planned_orders: pd.DataFrame) -> None:
        active_mode = ""
        active_name = ""
        if not state.empty:
            active_mode = str(state.iloc[0].get("active_strategy_mode", "1"))
            active_name = str(state.iloc[0].get("active_strategy_name", "ACDE2"))
        if active_mode == "2":
            title = "L 独立龙头策略计划"
            status_leg = "L"
            status_title = "策略 L 状态"
        elif active_mode == "3":
            title = "model=3 自动切换实盘计划"
            status_leg = "MODEL3"
            status_title = "model=3 状态"
        else:
            title = "A+C+D+E2 组合实盘计划（B已删除）"
            status_leg = "E2"
            status_title = "策略 E2 状态"
        status_rows = (
            decisions[decisions["strategy_leg"] == status_leg]
            if not decisions.empty and "strategy_leg" in decisions.columns
            else pd.DataFrame()
        )
        strategy_status = ""
        if not status_rows.empty:
            status = status_rows.iloc[0]
            strategy_status = f"\n## {status_title}\n\n{status['reason']}\n"

        content = f"""# {title}

本报告只做组合状态机判断，不提交真实委托。

当前总策略模式：{active_mode}（{active_name}）

## 状态

{state.to_markdown(index=False)}

## 决策

{decisions.to_markdown(index=False) if not decisions.empty else "无决策。"}

## 组合计划单

{planned_orders.to_markdown(index=False) if not planned_orders.empty else "无组合计划单。"}
{strategy_status}
## 执行原则

- 若存在 D 待卖持仓，先卖 D，未确认卖出前阻断 A/C 买入。
- 若存在 A/C 旧持仓或仅人工退出的历史B仓，阻断 D 盘中买入，避免资金冲突。
- 若今日已有 A/C 买入计划，默认不启动 D 盘中买入监控。
- 若无持仓且无 A/C 买入计划，才允许 D 盘中监控；A/C/D 均空闲时，E2 可能触发。
- 普通空仓日目标仓位为总资产82.5%，任何单票仍受总资产85%硬顶；旧策略仓未实际清空前取消衔接开仓。
- E2 条件：40条R1规则的当日第一名并集 → neutral + 非ST + 成交可靠 → 流通市值最小1只；T+1按82.5%目标仓开仓，按命中规则在T+2或T+3到期日卖出。
- L 条件：仅在 active_strategy_profile.mode=2 且 strategy_l.live_order_enabled=true 时，才把昨日 L 信号转换成今日买入计划；默认 mode=1 不启用 L。
- model=3 条件：active_strategy_profile.mode=3 且 strategy_model3.live_order_enabled=true 时，先生成mode=1计划；mode=1空闲则允许L补位，mode=1有买入计划时仅允许满足创业板、theme_limit_count>=2、非after_1430的L替换。
- 真实下单仍必须经过 LiveOrderGateway 的交易时间、涨跌停、持仓、资金和重复委托校验。
"""
        path.write_text(content, encoding="utf-8")
