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
    def as_int(value: Any, default: int = 0) -> int:
        try:
            if value in {None, ""}:
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

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
        abc_path, abc_orders = self.load_latest_abc_orders()

        decisions: list[CombinedLiveDecision] = []
        planned_orders: list[dict[str, Any]] = []
        state_rows: list[dict[str, Any]] = [
            {
                "today": today,
                "open_position_count": len(open_positions),
                "open_d_position_count": len(open_d_positions),
                "open_non_d_position_count": len(open_non_d_positions),
                "abc_planned_orders_path": str(abc_path or ""),
                "abc_planned_order_count": int(len(abc_orders)) if not abc_orders.empty else 0,
            }
        ]

        if open_d_positions:
            due_positions = [
                p for p in open_d_positions
                if str(p.get("planned_exit_date", "99991231")) <= today
                or str(p.get("status", "")).lower() == "sell_pending"
            ]
            if due_positions:
                for position in due_positions:
                    decisions.append(self.build_d_sell_decision(position, today))
                    planned_orders.append(self.build_d_sell_order(position, today))
                decisions.append(
                    CombinedLiveDecision(
                        action="BLOCK_ABC_BUY",
                        strategy_leg="A+B+C",
                        reason="D持仓尚未确认卖出，阻断A/B/C买入，避免同一资金重复占用。",
                        source="combined_state_machine",
                    )
                )
                decisions.append(
                    CombinedLiveDecision(
                        action="BLOCK_D_INTRADAY_MONITOR",
                        strategy_leg="D",
                        reason="已有D持仓处于待卖状态，今日不启动新的D盘中买入监控。",
                        source="combined_state_machine",
                    )
                )
            else:
                decisions.append(
                    CombinedLiveDecision(
                        action="BLOCK_ABC_BUY",
                        strategy_leg="A+B+C",
                        reason="D持仓未到平仓日，阻断A/B/C买入。",
                        source="combined_state_machine",
                    )
                )
                decisions.append(
                    CombinedLiveDecision(
                        action="BLOCK_D_INTRADAY_MONITOR",
                        strategy_leg="D",
                        reason="已有D持仓占用资金，今日不启动新的D盘中买入监控。",
                        source="combined_state_machine",
                    )
                )
        elif open_non_d_positions:
            decisions.append(
                CombinedLiveDecision(
                    action="BLOCK_ABC_BUY",
                    strategy_leg="A+B+C",
                    reason="存在A/B/C旧持仓，组合状态机不允许重复开仓。",
                    source="positions.json",
                )
            )
            decisions.append(
                CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR",
                    strategy_leg="D",
                    reason="存在A/B/C旧持仓占用资金，D盘中策略跳过。",
                    source="positions.json",
                )
            )
        else:
            abc_decisions = self.build_abc_buy_decisions(abc_orders, str(abc_path or ""))
            if abc_decisions:
                decisions.extend(abc_decisions)
                planned_orders.extend(abc_orders.to_dict("records"))
                decisions.append(
                    CombinedLiveDecision(
                        action="BLOCK_D_INTRADAY_MONITOR",
                        strategy_leg="D",
                        reason="今日存在A/B/C买入计划，D盘中策略不再使用同一资金。",
                        source="combined_state_machine",
                    )
                )
            else:
                decisions.append(
                    CombinedLiveDecision(
                        action="NO_ABC_BUY",
                        strategy_leg="A+B+C",
                        reason="今日没有A/B/C买入计划。",
                        source=str(abc_path or ""),
                    )
                )
                decisions.append(
                    CombinedLiveDecision(
                        action="ALLOW_D_INTRADAY_MONITOR",
                        strategy_leg="D",
                        reason="无持仓且无A/B/C买入计划，允许启动D盘中监控；D本身仍需实时行情、成交概率和风控校验。",
                        source="combined_state_machine",
                    )
                )

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
        return {"state": state_path, "decisions": decisions_path, "planned_orders": orders_path, "markdown": md_path}

    @staticmethod
    def write_markdown(path: Path, state: pd.DataFrame, decisions: pd.DataFrame, planned_orders: pd.DataFrame) -> None:
        content = f"""# A+B+C+D 组合实盘计划

本报告只做组合状态机判断，不提交真实委托。

## 状态

{state.to_markdown(index=False)}

## 决策

{decisions.to_markdown(index=False) if not decisions.empty else "无决策。"}

## 组合计划单

{planned_orders.to_markdown(index=False) if not planned_orders.empty else "无组合计划单。"}

## 执行原则

- 若存在 D 待卖持仓，先卖 D，未确认卖出前阻断 A/B/C 买入。
- 若存在 A/B/C 旧持仓，阻断 D 盘中买入，避免资金冲突。
- 若今日已有 A/B/C 买入计划，默认不启动 D 盘中买入监控。
- 若无持仓且无 A/B/C 买入计划，才允许 D 盘中监控。
- 真实下单仍必须经过 LiveOrderGateway 的交易时间、涨跌停、持仓、资金和重复委托校验。
"""
        path.write_text(content, encoding="utf-8")
