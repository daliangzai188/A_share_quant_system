from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from src.live_order_gateway import LiveOrderGateway
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.time_utils import today_beijing

_E2_POSITION_PCT = 0.8
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
    """A+B+C+D 组合实盘状态机。

    这个类只负责组合层面的顺序和阻断，不直接提交真实委托。
    """

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.gateway = LiveOrderGateway(config_path)
        self.positions_path = self.project_root / "data" / "processed" / "positions.json"
        self.output_dir = self.project_root / "reports" / "live_trade" / "combined"
        mkdir_p(self.output_dir)

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
            if not neutral_segs:
                result["has_candidate"] = False
                result["reason"] = f"今日无neutral板块，E2不触发。各板块：{segment_states}"
                return result

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

            candidates = load_e2_candidates(today, segment_states)
            if candidates.empty:
                result["has_candidate"] = False
                result["reason"] = f"neutral板块={neutral_segs}，但今日涨停池无符合条件候选（非ST+成交可靠）。"
                return result

            top = candidates.iloc[0]
            result["has_candidate"] = True
            result["candidate_count"] = len(candidates)
            result["ts_code"] = str(top.get("ts_code", ""))
            result["name"] = str(top.get("name", ""))
            result["circ_mv"] = float(top.get("circ_mv", 0) or 0)
            result["fill_probability"] = float(top.get("fill_probability", 0) or 0)
            result["limit_close"] = float(top.get("limit_close", 0) or 0)
        except Exception as exc:
            result["reason"] = f"预判异常：{exc}"
        return result

    def build_e2_buy_order(self, signal: dict[str, Any], today: str) -> dict[str, Any]:
        limit_close = float(signal.get("limit_close", 0.0))
        initial_equity = float(self.config.get("position", {}).get("initial_cash", 500_000.0))
        planned_amount = initial_equity * _E2_POSITION_PCT
        if str(self.config.get("trade_mode", "")).lower() == "live":
            max_single_order_amount = float(
                self.config.get("live_trade", {}).get("max_single_order_amount", planned_amount)
            )
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
            "exit_n_days": 1,  # buy T+1, sell T+2 = 1 day later
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

    def build_d_sell_decision(self, position: dict[str, Any], today: str) -> CombinedLiveDecision:
        return CombinedLiveDecision(
            action="PLAN_SELL_D_FIRST",
            strategy_leg="D",
            ts_code=str(position.get("ts_code", "")),
            name=str(position.get("name", "")),
            side="SELL",
            quantity=self.as_int(position.get("shares", 0)),
            reason=f"D持仓计划平仓日={position.get('planned_exit_date', '')}，今日={today}，必须先卖D再考虑A/B/C。",
            source="positions.json",
        )

    def build_d_sell_order(self, position: dict[str, Any], today: str) -> dict[str, Any]:
        return {
            "paper_order_id": f"D-SELL-{today}-{position.get('ts_code', '')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "D",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": "PLAN_SELL_D_FIRST",
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
                    reason="无D未平仓或待卖持仓，允许进入A/B/C买入预览；真实下单仍需LiveOrderGateway二次校验。",
                    source=source,
                )
            )
        return decisions

    def build_plan(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        today = today_beijing().strftime("%Y%m%d")
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
        abc_path, abc_orders = self.load_latest_abc_orders()

        decisions: list[CombinedLiveDecision] = []
        planned_orders: list[dict[str, Any]] = []
        state_rows: list[dict[str, Any]] = [
            {
                "today": today,
                "open_position_count": len(open_positions),
                "open_d_position_count": len(open_d_positions),
                "open_non_d_position_count": len(open_non_d_positions),
                "open_e2_position_count": len(open_e2_positions),
                "due_e2_count": len(due_e2_positions),
                "abc_planned_orders_path": str(abc_path or ""),
                "abc_planned_order_count": int(len(abc_orders)) if not abc_orders.empty else 0,
            }
        ]

        # ── E2 到期卖出（T+2 收盘，最优先加入计划单，不阻断其他流程） ──────────
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
                        f"今日={today}，T+2收盘卖出。"
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
            if due_d:
                for position in due_d:
                    decisions.append(self.build_d_sell_decision(position, today))
                    planned_orders.append(self.build_d_sell_order(position, today))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_ABC_BUY", strategy_leg="A+B+C",
                    reason="D持仓尚未确认卖出，阻断A/B/C买入，避免同一资金重复占用。",
                    source="combined_state_machine",
                ))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="已有D持仓处于待卖状态，今日不启动新的D盘中买入监控。",
                    source="combined_state_machine",
                ))
            else:
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_ABC_BUY", strategy_leg="A+B+C",
                    reason="D持仓未到平仓日，阻断A/B/C买入。",
                    source="combined_state_machine",
                ))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="已有D持仓占用资金，今日不启动新的D盘中买入监控。",
                    source="combined_state_machine",
                ))

        elif open_non_d_positions:
            decisions.append(CombinedLiveDecision(
                action="BLOCK_ABC_BUY", strategy_leg="A+B+C",
                reason="存在旧持仓（A/B/C或E2），组合状态机不允许重复开仓。",
                source="positions.json",
            ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                reason="存在旧持仓占用资金，D盘中策略跳过。",
                source="positions.json",
            ))

        else:
            # 账户完全空仓：按优先级 ABC > E2 > D 决定今日行动
            abc_decisions = self.build_abc_buy_decisions(abc_orders, str(abc_path or ""))
            if abc_decisions:
                decisions.extend(abc_decisions)
                planned_orders.extend(abc_orders.to_dict("records"))
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="今日存在A/B/C买入计划，D盘中策略不再使用同一资金。",
                    source="combined_state_machine",
                ))
            else:
                # 无 ABC，检查 E2 昨日信号
                yesterday_signal = self.load_yesterday_e2_signal(today)
                e2_order: dict[str, Any] | None = None
                if yesterday_signal:
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
                            "T+2收盘卖出。"
                        ),
                        source=str(self.project_root / "reports" / "strategy_e2"),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="NO_ABC_BUY", strategy_leg="A+B+C",
                        reason="今日无A/B/C买入计划，E2代替开仓。",
                        source=str(abc_path or ""),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                        reason="E2今日开仓使用同一资金，D盘中监控跳过。",
                        source="combined_state_machine",
                    ))
                else:
                    decisions.append(CombinedLiveDecision(
                        action="NO_ABC_BUY", strategy_leg="A+B+C",
                        reason="今日没有A/B/C买入计划。",
                        source=str(abc_path or ""),
                    ))
                    decisions.append(CombinedLiveDecision(
                        action="ALLOW_D_INTRADAY_MONITOR", strategy_leg="D",
                        reason="无持仓且无A/B/C买入计划，允许启动D盘中监控；D本身仍需实时行情、成交概率和风控校验。",
                        source="combined_state_machine",
                    ))

        # ── E2 盘中状态显示（摘要用） ─────────────────────────────────────────
        has_e2_buy = any(d.action == "ALLOW_E2_BUY" for d in decisions)
        has_e2_sell = any(d.action == "PLAN_SELL_E2" for d in decisions)
        has_abc_buy = any(d.action == "ALLOW_ABC_BUY_PREVIEW" for d in decisions)

        if has_e2_sell:
            e2_status_action = "PLAN_SELL_E2_TODAY"
            e2_status_reason = f"E2持仓今日到期，收盘前平仓（14:50 job_afternoon 执行）。"
        elif has_e2_buy:
            e2_status_action = "ALLOW_E2_BUY_TODAY"
            e2_status_reason = "E2今日T+1开仓，已加入组合计划单。"
        elif open_positions:
            e2_status_action = "BLOCK_E2"
            e2_status_reason = f"账户有 {len(open_positions)} 个未平仓头寸，E2 不触发。"
        elif has_abc_buy:
            e2_status_action = "BLOCK_E2"
            e2_status_reason = "今日 A/B/C 已生成买入计划，E2 不触发（资金冲突）。"
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

        # 打印 E2 状态
        print()
        print("─" * 60)
        print("  策略 E2 状态（板块中性小市值 T+1买T+2卖）")
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
                    print(f"  ⏳ 今日 T+2 平仓 → {code} {nm}  {qty_str}  14:50收盘前卖出")
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
        e2_rows = (
            decisions[decisions["strategy_leg"] == "E2"]
            if not decisions.empty and "strategy_leg" in decisions.columns
            else pd.DataFrame()
        )
        e2_status = ""
        if not e2_rows.empty:
            e2 = e2_rows.iloc[0]
            e2_status = f"\n## 策略 E2 状态\n\n{e2['reason']}\n"

        content = f"""# A+B+C+D+E2 组合实盘计划

本报告只做组合状态机判断，不提交真实委托。

## 状态

{state.to_markdown(index=False)}

## 决策

{decisions.to_markdown(index=False) if not decisions.empty else "无决策。"}

## 组合计划单

{planned_orders.to_markdown(index=False) if not planned_orders.empty else "无组合计划单。"}
{e2_status}
## 执行原则

- 若存在 D 待卖持仓，先卖 D，未确认卖出前阻断 A/B/C 买入。
- 若存在 A/B/C 旧持仓，阻断 D 盘中买入，避免资金冲突。
- 若今日已有 A/B/C 买入计划，默认不启动 D 盘中买入监控。
- 若无持仓且无 A/B/C 买入计划，才允许 D 盘中监控；ABCD 均空闲时，E2 可能触发。
- E2 条件：segment_retreat_state_bucket=neutral + 非ST + 成交可靠 → 流通市值最小1只；T+1开盘买80%仓，T+2收盘卖。
- 真实下单仍必须经过 LiveOrderGateway 的交易时间、涨跌停、持仓、资金和重复委托校验。
"""
        path.write_text(content, encoding="utf-8")
