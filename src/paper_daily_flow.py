from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.paper_candidate_generator import PaperCandidateGenerator
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class PaperDailyFlowRunner:
    """
    单日模拟盘流程运行器。

    文件作用：
    1. 按指定信号日生成 T 日收盘后的模拟盘候选。
    2. 对计划买入标的生成 T+1 模拟买入计划。
    3. 如果本地审计逐笔交易中存在同一信号日、同一股票，则生成历史模拟成交、持仓和资金更新。
    4. 如果没有历史成交依据，只输出 PENDING，不假装成交。

    本模块只读写本地文件，不接实盘，不调用 QMT，不下真实订单。
    """

    SAFE_TRADE_MODES = {"paper", "simulation", "dry_run", "research"}

    def __init__(self, strategy_config_path: str | Path = "config/strategy_config.json") -> None:
        self.project_root = get_project_root()
        self.strategy_config_path = strategy_config_path
        self.config = load_json_config(strategy_config_path)
        self.logger = get_logger("paper_daily_flow")
        self.flow_config = self.config.get("paper_daily_flow", {})
        self.paper_trade_config = self.config.get("paper_trade", {})
        self.position_config = self.config.get("position", {})
        self.output_prefix = self.project_root / self.flow_config.get(
            "output_prefix",
            "reports/paper_trade/daily_flow/current_strategy",
        )
        self.audit_trades_path = self.project_root / self.flow_config.get(
            "input_audit_trades_path",
            self.paper_trade_config.get("input_trades_path", ""),
        )
        self.selected_action = self.config.get("paper_candidate", {}).get(
            "planned_action_for_selected",
            "PLAN_BUY_T1_OPEN",
        )
        self.initial_cash = float(self.position_config.get("initial_cash", 500000))
        self.round_lot_size = int(self.paper_trade_config.get("round_lot_size", 100))
        self.manual_review_blocks_execution = bool(self.flow_config.get("manual_review_blocks_execution", True))

    def run(self, signal_date: str | None = None, top_n: int | None = None) -> dict[str, Path]:
        self.assert_safe_mode()
        candidate_outputs = PaperCandidateGenerator(self.strategy_config_path).generate(
            signal_date=signal_date,
            top_n=top_n,
        )
        candidates = pd.read_csv(candidate_outputs["candidates"], dtype={"signal_date": str, "ts_code": str})
        resolved_signal_date = str(candidates["signal_date"].iloc[0])
        audit = self.load_audit_trades(self.audit_trades_path)

        planned_orders = self.build_planned_orders(candidates, audit)
        manual_review = self.build_manual_review_checklist(candidates, planned_orders)
        executions = self.build_execution_updates(candidates, audit)
        positions = self.build_position_updates(candidates, executions)
        equity_update = self.build_equity_update(executions, resolved_signal_date)
        summary = self.build_summary(
            signal_date=resolved_signal_date,
            candidates=candidates,
            planned_orders=planned_orders,
            executions=executions,
            positions=positions,
            equity_update=equity_update,
        )

        mkdir_p(self.output_prefix.parent)
        paths = {
            "candidates": candidate_outputs["candidates"],
            "candidate_summary": candidate_outputs["summary"],
            "planned_orders": self.output_path(resolved_signal_date, "_planned_orders.csv"),
            "manual_review": self.output_path(resolved_signal_date, "_manual_review.csv"),
            "executions": self.output_path(resolved_signal_date, "_executions.csv"),
            "positions": self.output_path(resolved_signal_date, "_positions.csv"),
            "equity_update": self.output_path(resolved_signal_date, "_equity_update.csv"),
            "summary": self.output_path(resolved_signal_date, "_summary.csv"),
            "markdown": self.output_path(resolved_signal_date, ".md"),
        }
        planned_orders.to_csv(paths["planned_orders"], index=False, encoding="utf-8-sig")
        manual_review.to_csv(paths["manual_review"], index=False, encoding="utf-8-sig")
        executions.to_csv(paths["executions"], index=False, encoding="utf-8-sig")
        positions.to_csv(paths["positions"], index=False, encoding="utf-8-sig")
        equity_update.to_csv(paths["equity_update"], index=False, encoding="utf-8-sig")
        summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
        self.write_markdown(
            paths["markdown"],
            summary,
            candidates,
            planned_orders,
            manual_review,
            executions,
            positions,
            equity_update,
        )

        self.logger.info("单日模拟盘计划委托已生成: %s", paths["planned_orders"])
        self.logger.info("单日模拟盘人工确认清单已生成: %s", paths["manual_review"])
        self.logger.info("单日模拟盘成交更新已生成: %s", paths["executions"])
        self.logger.info("单日模拟盘汇总已生成: %s", paths["summary"])
        return paths

    def assert_safe_mode(self) -> None:
        trade_mode = str(self.config.get("trade_mode", "")).strip().lower()
        if trade_mode not in self.SAFE_TRADE_MODES:
            raise RuntimeError(f"拒绝运行单日模拟盘流程：trade_mode 不是安全模式: {trade_mode}")
        if bool(self.config.get("live_trading_enabled", False)):
            raise RuntimeError("拒绝运行单日模拟盘流程：live_trading_enabled=true")
        if bool(self.config.get("broker_adapter_enabled", False)):
            raise RuntimeError("拒绝运行单日模拟盘流程：broker_adapter_enabled=true")
        if bool(self.config.get("qmt_enabled", False)):
            raise RuntimeError("拒绝运行单日模拟盘流程：qmt_enabled=true")
        if bool(self.flow_config.get("allow_live_order", False)):
            raise RuntimeError("拒绝运行单日模拟盘流程：paper_daily_flow.allow_live_order=true")

    def output_path(self, signal_date: str, suffix: str) -> Path:
        return self.output_prefix.with_name(self.output_prefix.name + f"_{signal_date}{suffix}")

    def load_audit_trades(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"审计逐笔交易文件不存在: {path}")
        trades = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        if "scenario_executed" in trades.columns:
            trades = trades[trades["scenario_executed"].map(self.normalize_bool)].copy()
        date_columns = ["trade_date", "buy_trade_date", "exit_trade_date"]
        for column in date_columns:
            if column in trades.columns:
                trades[column] = trades[column].map(self.normalize_date)
        numeric_columns = [
            "trade_order",
            "dynamic_buy_price",
            "dynamic_sell_price",
            "actual_buy_amount",
            "actual_position_pct",
            "dynamic_account_return",
            "dynamic_net_return",
            "equity_before",
            "equity_after",
            "buy_amount_ratio",
            "sell_amount_ratio",
            "dynamic_buy_slippage_rate",
            "dynamic_sell_slippage_rate",
            "limit_down_blocked_days",
        ]
        for column in numeric_columns:
            if column in trades.columns:
                trades[column] = pd.to_numeric(trades[column], errors="coerce").fillna(0.0)
        return trades.sort_values(["trade_date", "trade_order", "ts_code"]).reset_index(drop=True)

    def build_planned_orders(self, candidates: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
        rows = []
        selected = candidates[candidates["planned_action"].astype(str) == self.selected_action].copy()
        for row in selected.itertuples(index=False):
            manual_review_required = self.has_loss_overlay_watch(getattr(row, "risk_flags", ""))
            planned_equity = self.resolve_planned_equity(row, audit)
            planned_amount = planned_equity * self.normalize_number(getattr(row, "planned_position_pct", 0.0))
            planned_price = self.normalize_number(getattr(row, "historical_reference_next_open", 0.0))
            shares = self.estimate_shares(planned_amount, planned_price)
            rows.append(
                {
                    "paper_order_id": f"PLAN-{row.signal_date}-{row.ts_code}-B",
                    "signal_date": row.signal_date,
                    "planned_order_date": getattr(row, "historical_reference_next_trade_date", ""),
                    "side": "BUY",
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "planned_action": row.planned_action,
                    "order_status": "REVIEW_REQUIRED_PLAN_ONLY" if manual_review_required else "PLAN_ONLY",
                    "planned_position_pct": self.normalize_number(getattr(row, "planned_position_pct", 0.0)),
                    "planned_equity": planned_equity,
                    "planned_amount_by_equity": planned_amount,
                    "reference_price": planned_price,
                    "estimated_shares": shares["estimated_shares"],
                    "round_lot_shares": shares["round_lot_shares"],
                    "risk_flags": getattr(row, "risk_flags", ""),
                    "manual_review_required": manual_review_required,
                    "manual_review_status": "PENDING_MANUAL_REVIEW" if manual_review_required else "NOT_REQUIRED",
                    "manual_review_reason": "命中 LOSS_OVERLAY_WATCH，模拟买入前需要人工复核。"
                    if manual_review_required
                    else "",
                    "live_order_enabled": False,
                }
            )
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(
            columns=[
                "paper_order_id",
                "signal_date",
                "planned_order_date",
                "side",
                "ts_code",
                "name",
                "planned_action",
                "order_status",
                "planned_position_pct",
                "planned_equity",
                "planned_amount_by_equity",
                "reference_price",
                "estimated_shares",
                "round_lot_shares",
                "risk_flags",
                "manual_review_required",
                "manual_review_status",
                "manual_review_reason",
                "live_order_enabled",
            ]
        )

    def build_manual_review_checklist(self, candidates: pd.DataFrame, planned_orders: pd.DataFrame) -> pd.DataFrame:
        review_orders = planned_orders[
            planned_orders.get("manual_review_required", pd.Series(False, index=planned_orders.index)).astype(bool)
        ].copy()
        rows = []
        for row in review_orders.itertuples(index=False):
            matched_candidate = candidates[
                (candidates["signal_date"].astype(str) == str(row.signal_date))
                & (candidates["ts_code"].astype(str) == str(row.ts_code))
            ].copy()
            candidate = matched_candidate.iloc[0] if not matched_candidate.empty else pd.Series(dtype=object)
            rows.append(
                {
                    "signal_date": row.signal_date,
                    "planned_order_date": row.planned_order_date,
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "manual_review_status": row.manual_review_status,
                    "manual_review_reason": row.manual_review_reason,
                    "risk_flags": row.risk_flags,
                    "planned_position_pct": row.planned_position_pct,
                    "planned_amount_by_equity": row.planned_amount_by_equity,
                    "reference_price": row.reference_price,
                    "amount_ratio_bucket": candidate.get("amount_ratio_bucket", ""),
                    "open_times": candidate.get("open_times", ""),
                    "first_time_detail_bucket": candidate.get("first_time_detail_bucket", ""),
                    "turnover_rate_bucket": candidate.get("turnover_rate_bucket", ""),
                    "historical_reference_net_return": candidate.get("historical_reference_net_return", ""),
                    "review_instruction": "人工确认后才允许进入模拟买入观察；未确认时不得进入实盘或半自动流程。",
                    "live_order_enabled": False,
                }
            )
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(
            columns=[
                "signal_date",
                "planned_order_date",
                "ts_code",
                "name",
                "manual_review_status",
                "manual_review_reason",
                "risk_flags",
                "planned_position_pct",
                "planned_amount_by_equity",
                "reference_price",
                "amount_ratio_bucket",
                "open_times",
                "first_time_detail_bucket",
                "turnover_rate_bucket",
                "historical_reference_net_return",
                "review_instruction",
                "live_order_enabled",
            ]
        )

    def resolve_planned_equity(self, candidate_row: object, audit: pd.DataFrame) -> float:
        matched = audit[
            (audit["trade_date"].astype(str) == str(candidate_row.signal_date))
            & (audit["ts_code"].astype(str) == str(candidate_row.ts_code))
        ].copy()
        if not matched.empty:
            return self.normalize_number(matched.iloc[0].get("equity_before", self.initial_cash), self.initial_cash)

        earlier = audit[audit["trade_date"].astype(str) < str(candidate_row.signal_date)].copy()
        if earlier.empty:
            return self.initial_cash
        earlier = earlier.sort_values(["trade_date", "trade_order"])
        return self.normalize_number(earlier.iloc[-1].get("equity_after", self.initial_cash), self.initial_cash)

    def build_execution_updates(self, candidates: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
        rows = []
        selected = candidates[candidates["planned_action"].astype(str) == self.selected_action].copy()
        for row in selected.itertuples(index=False):
            if self.should_block_execution_for_manual_review(row):
                rows.append(self.manual_review_blocked_execution_row(row))
                continue
            matched = audit[
                (audit["trade_date"].astype(str) == str(row.signal_date))
                & (audit["ts_code"].astype(str) == str(row.ts_code))
            ].copy()
            if matched.empty:
                rows.append(self.pending_execution_row(row))
                continue
            audit_row = matched.iloc[0]
            rows.extend(self.filled_execution_rows(row, audit_row))
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(
            columns=[
                "paper_execution_id",
                "signal_date",
                "event_date",
                "side",
                "ts_code",
                "name",
                "execution_status",
                "price",
                "amount",
                "account_return",
                "equity_before",
                "equity_after",
                "message",
            ]
        )

    def should_block_execution_for_manual_review(self, row: object) -> bool:
        return self.manual_review_blocks_execution and self.has_loss_overlay_watch(getattr(row, "risk_flags", ""))

    def manual_review_blocked_execution_row(self, row: object) -> dict[str, Any]:
        return {
            "paper_execution_id": f"EXEC-{row.signal_date}-{row.ts_code}-REVIEW-BLOCKED",
            "signal_date": row.signal_date,
            "event_date": "",
            "side": "BUY",
            "ts_code": row.ts_code,
            "name": row.name,
            "execution_status": "MANUAL_REVIEW_REQUIRED_BLOCKED",
            "price": pd.NA,
            "amount": pd.NA,
            "account_return": 0.0,
            "equity_before": pd.NA,
            "equity_after": pd.NA,
            "message": "命中 LOSS_OVERLAY_WATCH，未人工确认前只保留计划和复核清单，不自动记为模拟成交。",
        }

    def pending_execution_row(self, row: object) -> dict[str, Any]:
        return {
            "paper_execution_id": f"EXEC-{row.signal_date}-{row.ts_code}-PENDING",
            "signal_date": row.signal_date,
            "event_date": "",
            "side": "BUY",
            "ts_code": row.ts_code,
            "name": row.name,
            "execution_status": "PENDING_NO_HISTORICAL_MATCH",
            "price": pd.NA,
            "amount": pd.NA,
            "account_return": 0.0,
            "equity_before": pd.NA,
            "equity_after": pd.NA,
            "message": "本地审计逐笔交易中没有匹配记录，只保留计划，不记为成交。",
        }

    def filled_execution_rows(self, candidate_row: object, audit_row: pd.Series) -> list[dict[str, Any]]:
        return [
            {
                "paper_execution_id": f"EXEC-{candidate_row.signal_date}-{candidate_row.ts_code}-BUY",
                "signal_date": candidate_row.signal_date,
                "event_date": audit_row.get("buy_trade_date", ""),
                "side": "BUY",
                "ts_code": candidate_row.ts_code,
                "name": candidate_row.name,
                "execution_status": "HISTORICAL_SIM_FILLED",
                "price": self.normalize_number(audit_row.get("dynamic_buy_price", 0.0)),
                "amount": self.normalize_number(audit_row.get("actual_buy_amount", 0.0)),
                "account_return": 0.0,
                "equity_before": self.normalize_number(audit_row.get("equity_before", 0.0)),
                "equity_after": self.normalize_number(audit_row.get("equity_before", 0.0)),
                "message": "使用本地审计记录生成历史模拟买入成交。",
            },
            {
                "paper_execution_id": f"EXEC-{candidate_row.signal_date}-{candidate_row.ts_code}-SELL",
                "signal_date": candidate_row.signal_date,
                "event_date": audit_row.get("exit_trade_date", ""),
                "side": "SELL",
                "ts_code": candidate_row.ts_code,
                "name": candidate_row.name,
                "execution_status": "HISTORICAL_SIM_FILLED"
                if self.normalize_bool(audit_row.get("sell_executed", True))
                else "HISTORICAL_SIM_UNRESOLVED",
                "price": self.normalize_number(audit_row.get("dynamic_sell_price", 0.0)),
                "amount": self.normalize_number(audit_row.get("sell_value_before_slippage", 0.0)),
                "account_return": self.normalize_number(audit_row.get("dynamic_account_return", 0.0)),
                "equity_before": self.normalize_number(audit_row.get("equity_before", 0.0)),
                "equity_after": self.normalize_number(audit_row.get("equity_after", 0.0)),
                "message": "使用本地审计记录生成历史模拟卖出和资金更新。",
            },
        ]

    def build_position_updates(self, candidates: pd.DataFrame, executions: pd.DataFrame) -> pd.DataFrame:
        rows = []
        selected = candidates[candidates["planned_action"].astype(str) == self.selected_action].copy()
        for row in selected.itertuples(index=False):
            matched_exec = executions[
                (executions["signal_date"].astype(str) == str(row.signal_date))
                & (executions["ts_code"].astype(str) == str(row.ts_code))
            ].copy()
            sell_exec = matched_exec[matched_exec["side"].astype(str) == "SELL"].copy()
            buy_exec = matched_exec[matched_exec["side"].astype(str) == "BUY"].copy()
            if sell_exec.empty or sell_exec.iloc[0]["execution_status"] != "HISTORICAL_SIM_FILLED":
                rows.append(
                    {
                        "signal_date": row.signal_date,
                        "ts_code": row.ts_code,
                        "name": row.name,
                        "position_status": "PLANNED_OR_PENDING",
                        "open_date": buy_exec.iloc[0]["event_date"] if not buy_exec.empty else "",
                        "close_date": "",
                        "account_return": 0.0,
                        "equity_after": pd.NA,
                        "message": "未形成完整历史模拟成交闭环。",
                    }
                )
                continue
            rows.append(
                {
                    "signal_date": row.signal_date,
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "position_status": "CLOSED_BY_HISTORICAL_SIM",
                    "open_date": buy_exec.iloc[0]["event_date"] if not buy_exec.empty else "",
                    "close_date": sell_exec.iloc[0]["event_date"],
                    "account_return": self.normalize_number(sell_exec.iloc[0]["account_return"]),
                    "equity_after": self.normalize_number(sell_exec.iloc[0]["equity_after"]),
                    "message": "已按本地审计记录完成模拟买卖闭环。",
                }
            )
        return pd.DataFrame(rows)

    def build_equity_update(self, executions: pd.DataFrame, signal_date: str) -> pd.DataFrame:
        sell_exec = executions[executions["side"].astype(str) == "SELL"].copy()
        rows = []
        if sell_exec.empty:
            rows.append(
                {
                    "signal_date": signal_date,
                    "equity_event": "NO_CLOSED_TRADE",
                    "event_date": "",
                    "equity_before": pd.NA,
                    "equity_after": pd.NA,
                    "account_return": 0.0,
                    "message": "没有完整卖出成交，资金不更新。",
                }
            )
        else:
            for row in sell_exec.itertuples(index=False):
                rows.append(
                    {
                        "signal_date": signal_date,
                        "equity_event": row.execution_status,
                        "event_date": row.event_date,
                        "equity_before": row.equity_before,
                        "equity_after": row.equity_after,
                        "account_return": row.account_return,
                        "message": row.message,
                    }
                )
        return pd.DataFrame(rows)

    def build_summary(
        self,
        signal_date: str,
        candidates: pd.DataFrame,
        planned_orders: pd.DataFrame,
        executions: pd.DataFrame,
        positions: pd.DataFrame,
        equity_update: pd.DataFrame,
    ) -> pd.DataFrame:
        selected = candidates[candidates["planned_action"].astype(str) == self.selected_action]
        closed_positions = positions[positions["position_status"].astype(str) == "CLOSED_BY_HISTORICAL_SIM"]
        pending_positions = positions[positions["position_status"].astype(str) == "PLANNED_OR_PENDING"]
        manual_review_blocked = executions[
            executions.get("execution_status", pd.Series("", index=executions.index)).astype(str)
            == "MANUAL_REVIEW_REQUIRED_BLOCKED"
        ]
        sell_updates = equity_update[equity_update["equity_event"].astype(str) == "HISTORICAL_SIM_FILLED"]
        risk_flags = candidates.get("risk_flags", pd.Series("", index=candidates.index)).fillna("").astype(str)
        selected_risk_flags = selected.get("risk_flags", pd.Series("", index=selected.index)).fillna("").astype(str)
        loss_overlay_mask = risk_flags.str.contains("LOSS_OVERLAY_WATCH", na=False)
        selected_loss_overlay_mask = selected_risk_flags.str.contains("LOSS_OVERLAY_WATCH", na=False)
        loss_overlay_codes = (
            candidates.loc[loss_overlay_mask, "ts_code"].astype(str)
            + " "
            + candidates.loc[loss_overlay_mask, "name"].astype(str)
            if "ts_code" in candidates.columns and "name" in candidates.columns
            else pd.Series(dtype=str)
        )
        manual_review_required = bool(selected_loss_overlay_mask.any()) if not selected.empty else False
        return pd.DataFrame(
            [
                {
                    "strategy_name": self.config.get("strategy_name", ""),
                    "trade_mode": self.config.get("trade_mode", ""),
                    "signal_date": signal_date,
                    "candidate_count": int(len(candidates)),
                    "selected_count": int(len(selected)),
                    "planned_order_count": int(len(planned_orders)),
                    "execution_event_count": int(len(executions)),
                    "closed_position_count": int(len(closed_positions)),
                    "pending_position_count": int(len(pending_positions)),
                    "manual_review_blocked_execution_count": int(len(manual_review_blocked)),
                    "top_ts_code": str(selected["ts_code"].iloc[0]) if not selected.empty else "",
                    "top_name": str(selected["name"].iloc[0]) if not selected.empty else "",
                    "top_risk_flags": str(selected["risk_flags"].iloc[0]) if not selected.empty else "",
                    "risk_warn_candidate_count": int((risk_flags != "无").sum()) if not candidates.empty else 0,
                    "loss_overlay_watch_candidate_count": int(loss_overlay_mask.sum()) if not candidates.empty else 0,
                    "selected_loss_overlay_watch_count": int(selected_loss_overlay_mask.sum()) if not selected.empty else 0,
                    "selected_loss_overlay_watch": manual_review_required,
                    "loss_overlay_watch_top_codes": ";".join(loss_overlay_codes.head(10).tolist()),
                    "manual_review_required": manual_review_required,
                    "manual_review_status": "PENDING_MANUAL_REVIEW" if manual_review_required else "NOT_REQUIRED",
                    "manual_review_reason": "选中标的命中 LOSS_OVERLAY_WATCH，进入模拟买入观察前需要人工复核。"
                    if manual_review_required
                    else "",
                    "historical_execution_found": bool(len(closed_positions) > 0),
                    "equity_before": float(sell_updates["equity_before"].iloc[-1]) if not sell_updates.empty else 0.0,
                    "equity_after": float(sell_updates["equity_after"].iloc[-1]) if not sell_updates.empty else 0.0,
                    "account_return": float(sell_updates["account_return"].iloc[-1]) if not sell_updates.empty else 0.0,
                    "live_order_enabled": False,
                }
            ]
        )

    def write_markdown(
        self,
        path: Path,
        summary: pd.DataFrame,
        candidates: pd.DataFrame,
        planned_orders: pd.DataFrame,
        manual_review: pd.DataFrame,
        executions: pd.DataFrame,
        positions: pd.DataFrame,
        equity_update: pd.DataFrame,
    ) -> None:
        candidate_columns = [
            "candidate_rank",
            "planned_action",
            "ts_code",
            "name",
            "profit_source_score",
            "risk_flags",
            "historical_reference_next_trade_date",
            "historical_reference_net_return",
        ]
        candidate_columns = [column for column in candidate_columns if column in candidates.columns]
        content = f"""# 单日模拟盘流程报告

本报告只串联本地候选生成和本地历史模拟成交更新，不接实盘，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 候选

{candidates[candidate_columns].to_markdown(index=False) if not candidates.empty else "无候选。"}

## 计划委托

{planned_orders.to_markdown(index=False) if not planned_orders.empty else "无计划委托。"}

## 人工确认清单

{manual_review.to_markdown(index=False) if not manual_review.empty else "无人工确认项。"}

## 成交更新

{executions.to_markdown(index=False) if not executions.empty else "无成交更新。"}

## 持仓更新

{positions.to_markdown(index=False) if not positions.empty else "无持仓更新。"}

## 资金更新

{equity_update.to_markdown(index=False) if not equity_update.empty else "无资金更新。"}

## 口径限制

如果 `historical_execution_found=false`，表示该日只生成计划，不记录成交。真实模拟盘仍需后续接入分钟 K、集合竞价、盘口五档和人工确认流程后再推进。
"""
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def has_loss_overlay_watch(risk_flags: object) -> bool:
        return "LOSS_OVERLAY_WATCH" in str(risk_flags)

    @staticmethod
    def normalize_date(value: object) -> str:
        if pd.isna(value):
            return ""
        text = str(value)
        return text[:-2] if text.endswith(".0") else text

    @staticmethod
    def normalize_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            return False
        return str(value).strip().lower() in {"true", "1", "yes"}

    @staticmethod
    def normalize_number(value: object, default: float = 0.0) -> float:
        result = pd.to_numeric(value, errors="coerce")
        if pd.isna(result):
            return default
        return float(result)

    def estimate_shares(self, amount: float, price: float) -> dict[str, float | int]:
        if amount <= 0 or price <= 0:
            return {"estimated_shares": 0.0, "round_lot_shares": 0}
        estimated = amount / price
        round_lot = int(estimated // self.round_lot_size * self.round_lot_size)
        return {"estimated_shares": float(estimated), "round_lot_shares": round_lot}
