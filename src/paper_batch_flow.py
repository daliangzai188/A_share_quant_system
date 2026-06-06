from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.paper_candidate_generator import PaperCandidateGenerator
from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class PaperBatchFlowRunner:
    """
    多日模拟盘批量流程运行器。

    文件作用：
    1. 一次性加载本地候选特征，避免逐日重复加载。
    2. 按日期区间逐日生成候选、计划委托、历史模拟成交、持仓和资金更新。
    3. 生成连续资金曲线、每日状态、风险事件和汇总报告。
    4. 全程只读写本地文件，不接实盘，不调用 QMT，不下真实订单。
    """

    def __init__(self, strategy_config_path: str | Path = "config/strategy_config.json") -> None:
        self.project_root = get_project_root()
        self.strategy_config_path = strategy_config_path
        self.config = load_json_config(strategy_config_path)
        self.logger = get_logger("paper_batch_flow")
        self.batch_config = self.config.get("paper_batch_flow", {})
        self.paper_trade_config = self.config.get("paper_trade", {})
        self.position_config = self.config.get("position", {})
        self.output_prefix = self.project_root / self.batch_config.get(
            "output_prefix",
            "reports/paper_trade/batch_flow/current_strategy",
        )
        self.default_start_date = str(self.batch_config.get("default_start_date", ""))
        self.default_end_date = str(self.batch_config.get("default_end_date", ""))
        self.include_no_candidate_days = bool(self.batch_config.get("include_no_candidate_days", True))
        self.initial_cash = float(self.position_config.get("initial_cash", 500000))
        self.selected_action = self.config.get("paper_candidate", {}).get(
            "planned_action_for_selected",
            "PLAN_BUY_T1_OPEN",
        )
        self.risk_thresholds = self.paper_trade_config.get("risk_thresholds", {})

    def run(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        top_n: int | None = None,
    ) -> dict[str, Path]:
        self.assert_safe_mode()
        candidate_generator = PaperCandidateGenerator(self.strategy_config_path)
        daily_runner = PaperDailyFlowRunner(self.strategy_config_path)
        candidate_generator.assert_safe_mode()
        daily_runner.assert_safe_mode()

        all_candidates = candidate_generator.load_all_candidates()
        filtered = candidate_generator.apply_strategy_filters(all_candidates)
        audit = daily_runner.load_audit_trades(daily_runner.audit_trades_path)
        dates = self.resolve_dates(all_candidates, filtered, start_date, end_date)
        manual_review_blocked_keys: set[tuple[str, str]] = set()

        daily_summaries: list[dict[str, Any]] = []
        candidate_frames: list[pd.DataFrame] = []
        planned_order_frames: list[pd.DataFrame] = []
        execution_frames: list[pd.DataFrame] = []
        position_frames: list[pd.DataFrame] = []
        equity_update_frames: list[pd.DataFrame] = []

        current_equity = self.resolve_start_equity(dates[0], audit) if dates else self.initial_cash
        equity_rows = [
            {
                "date": dates[0] if dates else "",
                "event": "INITIAL",
                "signal_date": "",
                "ts_code": "",
                "name": "",
                "equity": current_equity,
                "account_return": 0.0,
                "daily_status": "INITIAL",
            }
        ]

        for signal_date in dates:
            day_filtered = filtered[filtered["trade_date"].astype(str) == signal_date].copy()
            if day_filtered.empty:
                daily_summaries.append(self.no_candidate_summary(signal_date, current_equity))
                if self.include_no_candidate_days:
                    equity_rows.append(
                        self.equity_row(signal_date, "NO_CANDIDATE", "", "", current_equity, 0.0)
                    )
                continue

            active_position = self.active_audit_position(signal_date, audit, manual_review_blocked_keys)
            if not active_position.empty and not self.has_same_day_audit_trade(signal_date, audit):
                ranked = candidate_generator.rank_candidates(day_filtered)
                candidates = candidate_generator.build_output(
                    ranked=ranked,
                    signal_date=signal_date,
                    top_n=top_n or candidate_generator.default_top_n,
                )
                daily_summary = self.position_occupied_summary(signal_date, candidates, active_position, current_equity)
                daily_summaries.append(daily_summary)
                candidate_frames.append(candidates)
                equity_rows.append(
                    self.equity_row(
                        signal_date=signal_date,
                        daily_status="POSITION_OCCUPIED_SKIP",
                        ts_code=str(daily_summary.get("top_ts_code", "")),
                        name=str(daily_summary.get("top_name", "")),
                        equity=current_equity,
                        account_return=0.0,
                    )
                )
                continue

            ranked = candidate_generator.rank_candidates(day_filtered)
            candidates = candidate_generator.build_output(
                ranked=ranked,
                signal_date=signal_date,
                top_n=top_n or candidate_generator.default_top_n,
            )
            manual_review_blocked_keys.update(self.collect_manual_review_blocked_keys(candidates, daily_runner))
            planned_orders = daily_runner.build_planned_orders(candidates, audit)
            executions = daily_runner.build_execution_updates(candidates, audit)
            positions = daily_runner.build_position_updates(candidates, executions)
            equity_update = daily_runner.build_equity_update(executions, signal_date)
            summary = daily_runner.build_summary(
                signal_date=signal_date,
                candidates=candidates,
                planned_orders=planned_orders,
                executions=executions,
                positions=positions,
                equity_update=equity_update,
            )

            daily_summary = dict(summary.iloc[0])
            daily_summary["daily_status"] = self.resolve_daily_status(daily_summary)
            daily_summary["equity_start_of_day"] = current_equity
            if daily_summary["historical_execution_found"]:
                account_return = float(daily_summary.get("account_return", 0.0))
                daily_summary["equity_before"] = current_equity
                current_equity = current_equity * (1.0 + account_return)
                daily_summary["equity_after"] = current_equity
            daily_summary["equity_end_of_day"] = current_equity
            daily_summaries.append(daily_summary)

            candidate_frames.append(candidates)
            planned_order_frames.append(planned_orders)
            execution_frames.append(executions)
            position_frames.append(positions)
            equity_update_frames.append(equity_update)
            equity_rows.append(
                self.equity_row(
                    signal_date=signal_date,
                    daily_status=str(daily_summary["daily_status"]),
                    ts_code=str(daily_summary.get("top_ts_code", "")),
                    name=str(daily_summary.get("top_name", "")),
                    equity=current_equity,
                    account_return=float(daily_summary.get("account_return", 0.0)),
                )
            )

        daily_report = pd.DataFrame(daily_summaries)
        candidates_report = self.concat_or_empty(candidate_frames)
        planned_orders_report = self.concat_or_empty(planned_order_frames)
        manual_review_report = self.build_manual_review_report(planned_orders_report, candidates_report)
        executions_report = self.concat_or_empty(execution_frames)
        positions_report = self.concat_or_empty(position_frames)
        equity_updates_report = self.concat_or_empty(equity_update_frames)
        equity_curve = self.build_equity_curve(pd.DataFrame(equity_rows))
        risk_events = self.build_risk_events(daily_report, executions_report, equity_curve)
        summary = self.build_summary(daily_report, equity_curve, risk_events, start_date=dates[0], end_date=dates[-1])

        self.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        path_suffix = f"_{dates[0]}_{dates[-1]}" if dates else ""
        paths = {
            "summary": self.output_path(path_suffix, "_summary.csv"),
            "daily": self.output_path(path_suffix, "_daily.csv"),
            "candidates": self.output_path(path_suffix, "_candidates.csv"),
            "planned_orders": self.output_path(path_suffix, "_planned_orders.csv"),
            "manual_review": self.output_path(path_suffix, "_manual_review.csv"),
            "executions": self.output_path(path_suffix, "_executions.csv"),
            "positions": self.output_path(path_suffix, "_positions.csv"),
            "equity_updates": self.output_path(path_suffix, "_equity_updates.csv"),
            "equity_curve": self.output_path(path_suffix, "_equity_curve.csv"),
            "risk_events": self.output_path(path_suffix, "_risk_events.csv"),
            "markdown": self.output_path(path_suffix, ".md"),
        }

        summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
        daily_report.to_csv(paths["daily"], index=False, encoding="utf-8-sig")
        candidates_report.to_csv(paths["candidates"], index=False, encoding="utf-8-sig")
        planned_orders_report.to_csv(paths["planned_orders"], index=False, encoding="utf-8-sig")
        manual_review_report.to_csv(paths["manual_review"], index=False, encoding="utf-8-sig")
        executions_report.to_csv(paths["executions"], index=False, encoding="utf-8-sig")
        positions_report.to_csv(paths["positions"], index=False, encoding="utf-8-sig")
        equity_updates_report.to_csv(paths["equity_updates"], index=False, encoding="utf-8-sig")
        equity_curve.to_csv(paths["equity_curve"], index=False, encoding="utf-8-sig")
        risk_events.to_csv(paths["risk_events"], index=False, encoding="utf-8-sig")
        self.write_markdown(paths["markdown"], summary, daily_report, manual_review_report, equity_curve, risk_events)

        self.logger.info("多日模拟盘汇总已生成: %s", paths["summary"])
        self.logger.info("多日模拟盘每日状态已生成: %s, 行数: %s", paths["daily"], len(daily_report))
        self.logger.info("多日模拟盘人工确认清单已生成: %s, 行数: %s", paths["manual_review"], len(manual_review_report))
        self.logger.info("多日模拟盘资金曲线已生成: %s", paths["equity_curve"])
        return paths

    def assert_safe_mode(self) -> None:
        if bool(self.batch_config.get("allow_live_order", False)):
            raise RuntimeError("拒绝运行批量模拟盘流程：paper_batch_flow.allow_live_order=true")

    def output_path(self, path_suffix: str, file_suffix: str) -> Path:
        return self.output_prefix.with_name(self.output_prefix.name + path_suffix + file_suffix)

    def resolve_dates(
        self,
        all_candidates: pd.DataFrame,
        filtered: pd.DataFrame,
        start_date: str | None,
        end_date: str | None,
    ) -> list[str]:
        start = str(start_date or self.default_start_date or filtered["trade_date"].astype(str).min())
        end = str(end_date or self.default_end_date or filtered["trade_date"].astype(str).max())
        date_source = all_candidates if self.include_no_candidate_days else filtered
        dates = sorted(
            date
            for date in date_source["trade_date"].dropna().astype(str).unique()
            if start <= date <= end
        )
        if not dates:
            raise RuntimeError(f"日期区间没有可用候选数据: {start}-{end}")
        return dates

    def resolve_start_equity(self, start_date: str, audit: pd.DataFrame) -> float:
        earlier = audit[audit["trade_date"].astype(str) < str(start_date)].copy()
        if earlier.empty:
            return self.initial_cash
        earlier = earlier.sort_values(["trade_date", "trade_order"])
        return float(earlier.iloc[-1].get("equity_after", self.initial_cash))

    def no_candidate_summary(self, signal_date: str, current_equity: float) -> dict[str, Any]:
        return {
            "strategy_name": self.config.get("strategy_name", ""),
            "trade_mode": self.config.get("trade_mode", ""),
            "signal_date": signal_date,
            "candidate_count": 0,
            "selected_count": 0,
            "planned_order_count": 0,
            "execution_event_count": 0,
            "closed_position_count": 0,
            "pending_position_count": 0,
            "top_ts_code": "",
            "top_name": "",
            "historical_execution_found": False,
            "equity_before": current_equity,
            "equity_after": current_equity,
            "account_return": 0.0,
            "live_order_enabled": False,
            "daily_status": "NO_CANDIDATE",
            "equity_start_of_day": current_equity,
            "equity_end_of_day": current_equity,
        }

    def position_occupied_summary(
        self,
        signal_date: str,
        candidates: pd.DataFrame,
        active_position: pd.DataFrame,
        current_equity: float,
    ) -> dict[str, Any]:
        selected = candidates[candidates["planned_action"].astype(str) == self.selected_action].copy()
        active_codes = ";".join(
            (active_position["ts_code"].astype(str) + " " + active_position["name"].astype(str)).head(5)
        )
        active_exit_dates = ";".join(
            active_position.get("exit_trade_date", pd.Series(dtype=str)).astype(str).head(5)
        )
        return {
            "strategy_name": self.config.get("strategy_name", ""),
            "trade_mode": self.config.get("trade_mode", ""),
            "signal_date": signal_date,
            "candidate_count": int(len(candidates)),
            "selected_count": 0,
            "planned_order_count": 0,
            "execution_event_count": 0,
            "closed_position_count": 0,
            "pending_position_count": 0,
            "top_ts_code": str(selected["ts_code"].iloc[0]) if not selected.empty else "",
            "top_name": str(selected["name"].iloc[0]) if not selected.empty else "",
            "historical_execution_found": False,
            "equity_before": current_equity,
            "equity_after": current_equity,
            "account_return": 0.0,
            "live_order_enabled": False,
            "daily_status": "POSITION_OCCUPIED_SKIP",
            "position_occupied_by": active_codes,
            "position_occupied_exit_dates": active_exit_dates,
            "equity_start_of_day": current_equity,
            "equity_end_of_day": current_equity,
        }

    @staticmethod
    def resolve_daily_status(daily_summary: dict[str, Any]) -> str:
        if int(daily_summary.get("candidate_count", 0)) == 0:
            return "NO_CANDIDATE"
        if int(daily_summary.get("selected_count", 0)) == 0:
            return "NO_SELECTED"
        if int(daily_summary.get("manual_review_blocked_execution_count", 0)) > 0:
            return "REVIEW_REQUIRED_PLAN_ONLY"
        if int(daily_summary.get("pending_position_count", 0)) > 0:
            return "PENDING_NO_HISTORICAL_MATCH"
        if bool(daily_summary.get("historical_execution_found", False)):
            return "CLOSED_BY_HISTORICAL_SIM"
        return "PLAN_ONLY"

    @staticmethod
    def equity_row(
        signal_date: str,
        daily_status: str,
        ts_code: str,
        name: str,
        equity: float,
        account_return: float,
    ) -> dict[str, Any]:
        return {
            "date": signal_date,
            "event": daily_status,
            "signal_date": signal_date,
            "ts_code": ts_code,
            "name": name,
            "equity": equity,
            "account_return": account_return,
            "daily_status": daily_status,
        }

    @staticmethod
    def concat_or_empty(frames: list[pd.DataFrame]) -> pd.DataFrame:
        cleaned_frames = []
        for frame in frames:
            if frame.empty:
                continue
            cleaned = frame.dropna(axis=1, how="all")
            if cleaned.empty:
                continue
            cleaned_frames.append(cleaned)
        if not cleaned_frames:
            return pd.DataFrame()
        return pd.concat(cleaned_frames, ignore_index=True)

    def build_manual_review_report(self, planned_orders: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
        if planned_orders.empty:
            return self.empty_manual_review_report()
        review_orders = planned_orders[
            planned_orders.get("manual_review_required", pd.Series(False, index=planned_orders.index))
            .astype(str)
            .str.lower()
            .isin({"true", "1"})
        ].copy()
        if review_orders.empty:
            return self.empty_manual_review_report()
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
                    "order_status": row.order_status,
                    "manual_review_status": row.manual_review_status,
                    "manual_review_reason": row.manual_review_reason,
                    "risk_flags": row.risk_flags,
                    "planned_position_pct": row.planned_position_pct,
                    "planned_equity": row.planned_equity,
                    "planned_amount_by_equity": row.planned_amount_by_equity,
                    "reference_price": row.reference_price,
                    "round_lot_shares": row.round_lot_shares,
                    "amount_ratio_bucket": candidate.get("amount_ratio_bucket", ""),
                    "open_times": candidate.get("open_times", ""),
                    "first_time_detail_bucket": candidate.get("first_time_detail_bucket", ""),
                    "turnover_rate_bucket": candidate.get("turnover_rate_bucket", ""),
                    "market_segment": candidate.get("market_segment", ""),
                    "historical_reference_net_return": candidate.get("historical_reference_net_return", ""),
                    "historical_reference_is_win": candidate.get("historical_reference_is_win", ""),
                    "review_instruction": "人工确认后才允许进入模拟买入观察；未确认时不得进入实盘或半自动流程。",
                    "live_order_enabled": False,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def empty_manual_review_report() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "planned_order_date",
                "ts_code",
                "name",
                "order_status",
                "manual_review_status",
                "manual_review_reason",
                "risk_flags",
                "planned_position_pct",
                "planned_equity",
                "planned_amount_by_equity",
                "reference_price",
                "round_lot_shares",
                "amount_ratio_bucket",
                "open_times",
                "first_time_detail_bucket",
                "turnover_rate_bucket",
                "market_segment",
                "historical_reference_net_return",
                "historical_reference_is_win",
                "review_instruction",
                "live_order_enabled",
            ]
        )

    @staticmethod
    def build_equity_curve(equity_rows: pd.DataFrame) -> pd.DataFrame:
        curve = equity_rows.copy()
        curve["equity"] = pd.to_numeric(curve["equity"], errors="coerce").ffill().fillna(0.0)
        curve["peak_equity"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak_equity"] - 1.0
        return curve

    def build_risk_events(
        self,
        daily_report: pd.DataFrame,
        executions: pd.DataFrame,
        equity_curve: pd.DataFrame,
    ) -> pd.DataFrame:
        rows = []
        loss_threshold = float(self.risk_thresholds.get("max_single_trade_account_loss", -0.08))
        drawdown_threshold = float(self.risk_thresholds.get("max_drawdown_warn", -0.12))
        if not executions.empty and "account_return" in executions.columns:
            sell_exec = executions[executions["side"].astype(str) == "SELL"].copy()
            sell_exec["account_return"] = pd.to_numeric(sell_exec["account_return"], errors="coerce").fillna(0.0)
            for row in sell_exec[sell_exec["account_return"] <= loss_threshold].itertuples(index=False):
                rows.append(
                    {
                        "event_date": getattr(row, "event_date", ""),
                        "signal_date": getattr(row, "signal_date", ""),
                        "ts_code": getattr(row, "ts_code", ""),
                        "risk_level": "WARN",
                        "risk_type": "SINGLE_TRADE_LOSS_WARN",
                        "metric_value": getattr(row, "account_return", 0.0),
                        "threshold": loss_threshold,
                        "message": "单笔账户亏损达到预警阈值。",
                    }
            )
        if not daily_report.empty:
            pending = daily_report[daily_report["daily_status"].astype(str) == "PENDING_NO_HISTORICAL_MATCH"]
            for row in pending.itertuples(index=False):
                rows.append(
                    {
                        "event_date": getattr(row, "signal_date", ""),
                        "signal_date": getattr(row, "signal_date", ""),
                        "ts_code": getattr(row, "top_ts_code", ""),
                        "risk_level": "WARN",
                        "risk_type": "PENDING_NO_HISTORICAL_MATCH",
                        "metric_value": 1,
                        "threshold": 0,
                        "message": "有计划候选但没有本地历史成交匹配，只能保留计划。",
                    }
                )
        dd = equity_curve[equity_curve["drawdown"] <= drawdown_threshold].copy()
        for row in dd.itertuples(index=False):
            if getattr(row, "event", "") == "INITIAL":
                continue
            rows.append(
                {
                    "event_date": getattr(row, "date", ""),
                    "signal_date": getattr(row, "signal_date", ""),
                    "ts_code": getattr(row, "ts_code", ""),
                    "risk_level": "WARN",
                    "risk_type": "MAX_DRAWDOWN_WARN",
                    "metric_value": getattr(row, "drawdown", 0.0),
                    "threshold": drawdown_threshold,
                    "message": "连续资金曲线回撤达到预警阈值。",
                }
            )
        if not rows:
            return pd.DataFrame(
                columns=[
                    "event_date",
                    "signal_date",
                    "ts_code",
                    "risk_level",
                    "risk_type",
                    "metric_value",
                    "threshold",
                    "message",
                ]
            )
        return pd.DataFrame(rows).sort_values(["event_date", "signal_date", "risk_type"]).reset_index(drop=True)

    def build_summary(
        self,
        daily_report: pd.DataFrame,
        equity_curve: pd.DataFrame,
        risk_events: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        final_equity = float(equity_curve["equity"].iloc[-1]) if not equity_curve.empty else self.initial_cash
        trade_days = len(daily_report)
        closed_days = int((daily_report["daily_status"].astype(str) == "CLOSED_BY_HISTORICAL_SIM").sum())
        no_candidate_days = int((daily_report["daily_status"].astype(str) == "NO_CANDIDATE").sum())
        pending_days = int((daily_report["daily_status"].astype(str) == "PENDING_NO_HISTORICAL_MATCH").sum())
        manual_review_blocked_days = int(
            (daily_report["daily_status"].astype(str) == "REVIEW_REQUIRED_PLAN_ONLY").sum()
        )
        position_occupied_skip_days = int(
            (daily_report["daily_status"].astype(str) == "POSITION_OCCUPIED_SKIP").sum()
        )
        manual_review_required_days = int(
            daily_report.get("manual_review_required", pd.Series(False, index=daily_report.index))
            .astype(str)
            .str.lower()
            .isin({"true", "1"})
            .sum()
        )
        returns = pd.to_numeric(daily_report.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        closed_mask = daily_report["daily_status"].astype(str) == "CLOSED_BY_HISTORICAL_SIM"
        closed_returns = pd.to_numeric(
            daily_report.loc[closed_mask, "account_return"]
            if "account_return" in daily_report.columns
            else pd.Series(dtype=float),
            errors="coerce",
        ).fillna(0.0)
        initial_equity = self.resolve_initial_equity(equity_curve)
        return pd.DataFrame(
            [
                {
                    "strategy_name": self.config.get("strategy_name", ""),
                    "trade_mode": self.config.get("trade_mode", ""),
                    "start_date": start_date,
                    "end_date": end_date,
                    "trade_day_count": int(trade_days),
                    "closed_trade_day_count": closed_days,
                    "no_candidate_day_count": no_candidate_days,
                    "pending_day_count": pending_days,
                    "manual_review_blocked_day_count": manual_review_blocked_days,
                    "position_occupied_skip_day_count": position_occupied_skip_days,
                    "manual_review_required_day_count": manual_review_required_days,
                    "initial_equity": initial_equity,
                    "final_equity": final_equity,
                    "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
                    "win_rate": float((closed_returns > 0).mean()) if len(closed_returns) else 0.0,
                    "closed_trade_win_rate": float((closed_returns > 0).mean()) if len(closed_returns) else 0.0,
                    "positive_day_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_account_return": float(closed_returns.mean()) if len(closed_returns) else 0.0,
                    "median_account_return": float(closed_returns.median()) if len(closed_returns) else 0.0,
                    "avg_daily_account_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_daily_account_return": float(returns.median()) if len(returns) else 0.0,
                    "max_profit": float(closed_returns.max()) if len(closed_returns) else 0.0,
                    "max_loss": float(closed_returns.min()) if len(closed_returns) else 0.0,
                    "max_drawdown": float(equity_curve["drawdown"].min()) if not equity_curve.empty else 0.0,
                    "risk_event_count": int(len(risk_events)),
                    "live_order_enabled": False,
                }
            ]
        )

    @staticmethod
    def resolve_initial_equity(equity_curve: pd.DataFrame) -> float:
        if equity_curve.empty:
            return 0.0
        return float(equity_curve["equity"].iloc[0])

    def write_markdown(
        self,
        path: Path,
        summary: pd.DataFrame,
        daily_report: pd.DataFrame,
        manual_review_report: pd.DataFrame,
        equity_curve: pd.DataFrame,
        risk_events: pd.DataFrame,
    ) -> None:
        daily_preview_columns = [
            "signal_date",
            "daily_status",
            "candidate_count",
            "selected_count",
            "top_ts_code",
            "top_name",
            "top_risk_flags",
            "selected_loss_overlay_watch",
            "manual_review_required",
            "manual_review_blocked_execution_count",
            "manual_review_status",
            "account_return",
            "equity_end_of_day",
        ]
        daily_preview_columns = [column for column in daily_preview_columns if column in daily_report.columns]
        manual_review_columns = [
            "signal_date",
            "planned_order_date",
            "ts_code",
            "name",
            "order_status",
            "manual_review_status",
            "risk_flags",
            "planned_amount_by_equity",
            "historical_reference_net_return",
            "review_instruction",
        ]
        manual_review_columns = [column for column in manual_review_columns if column in manual_review_report.columns]
        content = f"""# 多日模拟盘批量流程报告

本报告按日期区间批量串联本地候选生成、计划委托、历史模拟成交、持仓和资金更新。不接实盘，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 每日状态预览

{daily_report[daily_preview_columns].to_markdown(index=False) if not daily_report.empty else "无每日状态。"}

## 人工确认清单

{manual_review_report[manual_review_columns].to_markdown(index=False) if not manual_review_report.empty else "无人工确认项。"}

## 资金曲线尾部

{equity_curve.tail(20).to_markdown(index=False) if not equity_curve.empty else "无资金曲线。"}

## 风险事件

{risk_events.to_markdown(index=False) if not risk_events.empty else "无风险事件。"}

## 口径限制

该批量流程仍使用本地历史审计成交作为模拟成交依据。没有历史匹配的计划不会被记为成交；真实模拟盘还需要接入分钟 K、集合竞价、盘口五档和人工确认。
"""
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def has_same_day_audit_trade(signal_date: str, audit: pd.DataFrame) -> bool:
        if audit.empty or "trade_date" not in audit.columns:
            return False
        return not audit[audit["trade_date"].astype(str) == str(signal_date)].empty

    @staticmethod
    def collect_manual_review_blocked_keys(
        candidates: pd.DataFrame,
        daily_runner: PaperDailyFlowRunner,
    ) -> set[tuple[str, str]]:
        if candidates.empty:
            return set()
        selected = candidates[candidates["planned_action"].astype(str) == daily_runner.selected_action].copy()
        keys: set[tuple[str, str]] = set()
        for row in selected.itertuples(index=False):
            if daily_runner.should_block_execution_for_manual_review(row):
                keys.add((str(row.signal_date), str(row.ts_code)))
        return keys

    @staticmethod
    def active_audit_position(
        signal_date: str,
        audit: pd.DataFrame,
        blocked_trade_keys: set[tuple[str, str]] | None = None,
    ) -> pd.DataFrame:
        if audit.empty:
            return audit.copy()
        required = {"trade_date", "buy_trade_date", "exit_trade_date"}
        if not required.issubset(set(audit.columns)):
            return pd.DataFrame()
        trade_date = audit["trade_date"].astype(str)
        buy_date = audit["buy_trade_date"].astype(str)
        exit_date = audit["exit_trade_date"].astype(str)
        mask = (trade_date < str(signal_date)) & (buy_date <= str(signal_date)) & (str(signal_date) < exit_date)
        active = audit[mask].copy()
        if not blocked_trade_keys or active.empty:
            return active
        active_keys = list(zip(active["trade_date"].astype(str), active["ts_code"].astype(str)))
        keep_mask = [key not in blocked_trade_keys for key in active_keys]
        return active[keep_mask].copy()
