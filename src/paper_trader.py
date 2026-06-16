from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class PaperTradeSimulator:
    """
    本地模拟盘账本生成器。

    文件作用：
    1. 读取 strategy_config.json 中固化的候选策略和模拟盘配置。
    2. 读取本地审计逐笔交易明细，不调用外部接口。
    3. 生成信号、委托、成交、持仓、资金曲线、风险事件和汇总报告。
    4. 强制 paper/simulation 模式，禁止实盘下单。
    """

    SAFE_TRADE_MODES = {"paper", "simulation", "dry_run", "research"}

    def __init__(self, strategy_config_path: str | Path = "config/strategy_config.json") -> None:
        self.project_root = get_project_root()
        self.strategy_config_path = strategy_config_path
        self.config = load_json_config(strategy_config_path)
        self.logger = get_logger("paper_trader")
        self.paper_config = self.config.get("paper_trade", {})
        self.position_config = self.config.get("position", {})
        self.risk_thresholds = self.paper_config.get("risk_thresholds", {})
        self.input_trades_path = self.project_root / self.paper_config.get(
            "input_trades_path",
            self.config.get("data_scope", {}).get(
                "audit_report_prefix",
                "reports/a_clean_exclude_star_prev0_3_bj_best_audit",
            )
            + "_trades.csv",
        )
        output_prefix = self.project_root / self.paper_config.get(
            "output_prefix",
            "reports/paper_trade/current_strategy",
        )
        self.output_prefix = output_prefix
        self.initial_cash = float(self.position_config.get("initial_cash", 500000))
        self.target_position_pct = float(self.position_config.get("target_position_pct", 0.8))
        self.round_lot_size = int(self.paper_config.get("round_lot_size", 100))

    def run(self) -> dict[str, Path]:
        self.assert_safe_mode()
        trades = self.load_trades(self.input_trades_path)
        signals = self.build_signals(trades)
        orders = self.build_orders(trades)
        fills = self.build_fills(orders)
        positions = self.build_positions(trades)
        equity_curve = self.build_equity_curve(trades)
        risk_events = self.build_risk_events(trades, equity_curve)
        summary = self.build_summary(trades, signals, orders, fills, risk_events, equity_curve)

        mkdir_p(self.output_prefix.parent)
        paths = {
            "signals": self.output_path("_signals.csv"),
            "orders": self.output_path("_orders.csv"),
            "fills": self.output_path("_fills.csv"),
            "positions": self.output_path("_positions.csv"),
            "equity_curve": self.output_path("_equity_curve.csv"),
            "risk_events": self.output_path("_risk_events.csv"),
            "summary": self.output_path("_summary.csv"),
            "markdown": self.output_prefix.with_suffix(".md"),
        }

        signals.to_csv(paths["signals"], index=False, encoding="utf-8-sig")
        orders.to_csv(paths["orders"], index=False, encoding="utf-8-sig")
        fills.to_csv(paths["fills"], index=False, encoding="utf-8-sig")
        positions.to_csv(paths["positions"], index=False, encoding="utf-8-sig")
        equity_curve.to_csv(paths["equity_curve"], index=False, encoding="utf-8-sig")
        risk_events.to_csv(paths["risk_events"], index=False, encoding="utf-8-sig")
        summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
        self.write_markdown(paths["markdown"], summary, risk_events, equity_curve)

        self.logger.info("模拟盘信号已生成: %s, 行数: %s", paths["signals"], len(signals))
        self.logger.info("模拟盘委托已生成: %s, 行数: %s", paths["orders"], len(orders))
        self.logger.info("模拟盘成交已生成: %s, 行数: %s", paths["fills"], len(fills))
        self.logger.info("模拟盘资金曲线已生成: %s", paths["equity_curve"])
        self.logger.info("模拟盘汇总已生成: %s", paths["summary"])
        return paths

    def assert_safe_mode(self) -> None:
        trade_mode = str(self.config.get("trade_mode", "")).strip().lower()
        paper_mode = str(self.paper_config.get("mode", "")).strip().lower()
        if trade_mode not in self.SAFE_TRADE_MODES:
            raise RuntimeError(f"拒绝运行模拟盘：trade_mode 不是安全模式: {trade_mode}")
        if bool(self.config.get("live_trading_enabled", False)):
            raise RuntimeError("拒绝运行模拟盘：live_trading_enabled=true")
        if bool(self.config.get("broker_adapter_enabled", False)):
            raise RuntimeError("拒绝运行模拟盘：broker_adapter_enabled=true")
        if bool(self.config.get("qmt_enabled", False)):
            raise RuntimeError("拒绝运行模拟盘：qmt_enabled=true")
        if bool(self.paper_config.get("allow_live_order", False)):
            raise RuntimeError("拒绝运行模拟盘：paper_trade.allow_live_order=true")
        if paper_mode not in {"historical_replay", "paper", "simulation", ""}:
            raise RuntimeError(f"拒绝运行模拟盘：paper_trade.mode 不受支持: {paper_mode}")

    def output_path(self, suffix: str) -> Path:
        return self.output_prefix.with_name(self.output_prefix.name + suffix)

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

    def load_trades(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"模拟盘输入逐笔交易文件不存在: {path}")
        trades = pd.read_csv(path, low_memory=False)
        required_columns = {
            "trade_date",
            "ts_code",
            "name",
            "buy_trade_date",
            "exit_trade_date",
            "buy_executed",
            "sell_executed",
            "dynamic_buy_price",
            "dynamic_sell_price",
            "actual_buy_amount",
            "dynamic_account_return",
            "equity_before",
            "equity_after",
        }
        missing = sorted(required_columns - set(trades.columns))
        if missing:
            raise RuntimeError(f"模拟盘输入文件缺少字段 {missing}: {path}")
        if "scenario_executed" in trades.columns:
            trades = trades[trades["scenario_executed"].map(self.normalize_bool)].copy()
        if trades.empty:
            raise RuntimeError(f"模拟盘输入文件没有已执行交易: {path}")

        date_columns = ["trade_date", "buy_trade_date", "exit_trade_date"]
        for column in date_columns:
            trades[column] = trades[column].map(self.normalize_date)
        bool_columns = ["buy_executed", "sell_executed"]
        for column in bool_columns:
            trades[column] = trades[column].map(self.normalize_bool)
        numeric_columns = [
            "trade_order",
            "dynamic_buy_price",
            "dynamic_sell_price",
            "buy_price_before_slippage",
            "exit_price_before_slippage",
            "actual_buy_amount",
            "target_buy_amount",
            "actual_position_pct",
            "buy_amount_ratio",
            "sell_amount_ratio",
            "dynamic_buy_slippage_rate",
            "dynamic_sell_slippage_rate",
            "dynamic_net_return",
            "dynamic_account_return",
            "equity_before",
            "equity_after",
            "limit_down_blocked_days",
        ]
        for column in numeric_columns:
            if column in trades.columns:
                trades[column] = pd.to_numeric(trades[column], errors="coerce").fillna(0.0)
        trades["paper_trade_id"] = [
            f"PT{index:05d}" for index in range(1, len(trades) + 1)
        ]
        return trades.sort_values(["trade_order", "trade_date", "ts_code"]).reset_index(drop=True)

    def build_signals(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for row in trades.itertuples(index=False):
            rows.append(
                {
                    "paper_trade_id": row.paper_trade_id,
                    "signal_date": row.trade_date,
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "market_segment": getattr(row, "market_segment", ""),
                    "signal_status": "ACCEPTED" if row.buy_executed else "REJECTED",
                    "planned_position_pct": self.target_position_pct,
                    "actual_position_pct": self.normalize_number(getattr(row, "actual_position_pct", 0.0)),
                    "strategy_name": self.config.get("strategy_name", ""),
                    "conditions": self.conditions_text(),
                    "sort_rule": self.config.get("ranking", {}).get("sort_rule", ""),
                    "exit_rule": self.config.get("exit_rule", {}).get("rule_name", ""),
                    "profit_source_score": self.normalize_number(getattr(row, "profit_source_score", 0.0)),
                    "risk_note": self.signal_risk_note(row),
                }
            )
        return pd.DataFrame(rows)

    def build_orders(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for row in trades.itertuples(index=False):
            shares = self.estimate_shares(row.actual_buy_amount, row.dynamic_buy_price)
            rows.append(
                {
                    "paper_order_id": f"{row.paper_trade_id}-B",
                    "paper_trade_id": row.paper_trade_id,
                    "order_date": row.buy_trade_date,
                    "side": "BUY",
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "order_status": "FILLED" if row.buy_executed else "REJECTED",
                    "reject_reason": getattr(row, "buy_reject_reason", ""),
                    "price_model": getattr(row, "buy_price_model", ""),
                    "limit_price": row.dynamic_buy_price,
                    "estimated_shares": shares["estimated_shares"],
                    "round_lot_shares": shares["round_lot_shares"],
                    "order_amount": row.actual_buy_amount,
                    "amount_ratio": self.normalize_number(getattr(row, "buy_amount_ratio", 0.0)),
                    "slippage_rate": self.normalize_number(getattr(row, "dynamic_buy_slippage_rate", 0.0)),
                }
            )
            rows.append(
                {
                    "paper_order_id": f"{row.paper_trade_id}-S",
                    "paper_trade_id": row.paper_trade_id,
                    "order_date": row.exit_trade_date,
                    "side": "SELL",
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "order_status": "FILLED" if row.sell_executed else "UNRESOLVED",
                    "reject_reason": getattr(row, "sell_reject_reason", ""),
                    "price_model": getattr(row, "sell_price_model", ""),
                    "limit_price": row.dynamic_sell_price,
                    "estimated_shares": shares["estimated_shares"],
                    "round_lot_shares": shares["round_lot_shares"],
                    "order_amount": self.normalize_number(getattr(row, "sell_value_before_slippage", 0.0)),
                    "amount_ratio": self.normalize_number(getattr(row, "sell_amount_ratio", 0.0)),
                    "slippage_rate": self.normalize_number(getattr(row, "dynamic_sell_slippage_rate", 0.0)),
                }
            )
        return pd.DataFrame(rows).sort_values(["order_date", "paper_order_id"]).reset_index(drop=True)

    def build_fills(self, orders: pd.DataFrame) -> pd.DataFrame:
        filled = orders[orders["order_status"] == "FILLED"].copy()
        filled["paper_fill_id"] = [f"PF{index:05d}" for index in range(1, len(filled) + 1)]
        filled["fill_date"] = filled["order_date"]
        filled["fill_price"] = filled["limit_price"]
        filled["fill_amount"] = filled["order_amount"]
        return filled[
            [
                "paper_fill_id",
                "paper_order_id",
                "paper_trade_id",
                "fill_date",
                "side",
                "ts_code",
                "name",
                "fill_price",
                "estimated_shares",
                "round_lot_shares",
                "fill_amount",
                "amount_ratio",
                "slippage_rate",
                "price_model",
            ]
        ].reset_index(drop=True)

    def build_positions(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for row in trades.itertuples(index=False):
            shares = self.estimate_shares(row.actual_buy_amount, row.dynamic_buy_price)
            rows.append(
                {
                    "paper_trade_id": row.paper_trade_id,
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "open_date": row.buy_trade_date,
                    "close_date": row.exit_trade_date if row.sell_executed else "",
                    "position_status": "CLOSED" if row.sell_executed else "OPEN_UNRESOLVED",
                    "buy_price": row.dynamic_buy_price,
                    "sell_price": row.dynamic_sell_price if row.sell_executed else pd.NA,
                    "estimated_shares": shares["estimated_shares"],
                    "round_lot_shares": shares["round_lot_shares"],
                    "actual_buy_amount": row.actual_buy_amount,
                    "actual_position_pct": self.normalize_number(getattr(row, "actual_position_pct", 0.0)),
                    "account_return": self.normalize_number(getattr(row, "dynamic_account_return", 0.0)),
                    "equity_before": row.equity_before,
                    "equity_after": row.equity_after,
                    "exit_reason": getattr(row, "exit_reason", ""),
                    "limit_down_blocked_days": self.normalize_number(getattr(row, "limit_down_blocked_days", 0.0)),
                }
            )
        return pd.DataFrame(rows)

    def build_equity_curve(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = [
            {
                "date": self.normalize_date(trades["trade_date"].min()),
                "event": "INITIAL",
                "paper_trade_id": "",
                "ts_code": "",
                "equity": self.initial_cash,
                "cash": self.initial_cash,
                "account_return": 0.0,
                "drawdown": 0.0,
            }
        ]
        for row in trades.itertuples(index=False):
            rows.append(
                {
                    "date": row.exit_trade_date,
                    "event": "TRADE_CLOSED" if row.sell_executed else "TRADE_UNRESOLVED",
                    "paper_trade_id": row.paper_trade_id,
                    "ts_code": row.ts_code,
                    "equity": row.equity_after,
                    "cash": row.equity_after,
                    "account_return": self.normalize_number(getattr(row, "dynamic_account_return", 0.0)),
                    "drawdown": 0.0,
                }
            )
        curve = pd.DataFrame(rows).sort_values(["date", "paper_trade_id"]).reset_index(drop=True)
        curve["peak_equity"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak_equity"] - 1.0
        return curve

    def build_risk_events(self, trades: pd.DataFrame, equity_curve: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for row in trades.itertuples(index=False):
            self.append_trade_risk_events(rows, row)
        drawdown_warn = float(self.risk_thresholds.get("max_drawdown_warn", -0.12))
        dd_events = equity_curve[equity_curve["drawdown"] <= drawdown_warn].copy()
        for row in dd_events.itertuples(index=False):
            if row.event == "INITIAL":
                continue
            rows.append(
                {
                    "event_date": row.date,
                    "paper_trade_id": row.paper_trade_id,
                    "ts_code": row.ts_code,
                    "risk_level": "WARN",
                    "risk_type": "MAX_DRAWDOWN_WARN",
                    "metric_value": row.drawdown,
                    "threshold": drawdown_warn,
                    "message": "资金曲线回撤达到预警阈值。",
                }
            )
        return pd.DataFrame(rows).sort_values(["event_date", "paper_trade_id", "risk_type"]).reset_index(drop=True)

    def append_trade_risk_events(self, rows: list[dict[str, Any]], row: object) -> None:
        checks = [
            (
                "SINGLE_TRADE_LOSS_WARN",
                self.normalize_number(getattr(row, "dynamic_account_return", 0.0)),
                float(self.risk_thresholds.get("max_single_trade_account_loss", -0.08)),
                "<=",
                "单笔账户亏损达到预警阈值。",
            ),
            (
                "BUY_AMOUNT_RATIO_WARN",
                self.normalize_number(getattr(row, "buy_amount_ratio", 0.0)),
                float(self.risk_thresholds.get("max_buy_amount_ratio_warn", 0.03)),
                ">",
                "买入成交额占比偏高。",
            ),
            (
                "SELL_AMOUNT_RATIO_WARN",
                self.normalize_number(getattr(row, "sell_amount_ratio", 0.0)),
                float(self.risk_thresholds.get("max_sell_amount_ratio_warn", 0.03)),
                ">",
                "卖出成交额占比偏高。",
            ),
            (
                "BUY_SLIPPAGE_WARN",
                self.normalize_number(getattr(row, "dynamic_buy_slippage_rate", 0.0)),
                float(self.risk_thresholds.get("max_buy_slippage_warn", 0.005)),
                ">",
                "买入滑点偏高。",
            ),
            (
                "SELL_SLIPPAGE_WARN",
                self.normalize_number(getattr(row, "dynamic_sell_slippage_rate", 0.0)),
                float(self.risk_thresholds.get("max_sell_slippage_warn", 0.005)),
                ">",
                "卖出滑点偏高。",
            ),
        ]
        for risk_type, value, threshold, operator, message in checks:
            triggered = value <= threshold if operator == "<=" else value > threshold
            if triggered:
                rows.append(
                    {
                        "event_date": getattr(row, "exit_trade_date", getattr(row, "trade_date", "")),
                        "paper_trade_id": row.paper_trade_id,
                        "ts_code": row.ts_code,
                        "risk_level": "WARN",
                        "risk_type": risk_type,
                        "metric_value": value,
                        "threshold": threshold,
                        "message": message,
                    }
                )
        if self.normalize_number(getattr(row, "limit_down_blocked_days", 0.0)) > 0:
            rows.append(
                {
                    "event_date": getattr(row, "exit_trade_date", getattr(row, "trade_date", "")),
                    "paper_trade_id": row.paper_trade_id,
                    "ts_code": row.ts_code,
                    "risk_level": "FAIL",
                    "risk_type": "LIMIT_DOWN_BLOCKED",
                    "metric_value": self.normalize_number(getattr(row, "limit_down_blocked_days", 0.0)),
                    "threshold": 0,
                    "message": "发生跌停延迟卖出，模拟盘必须重点复盘。",
                }
            )

    def build_summary(
        self,
        trades: pd.DataFrame,
        signals: pd.DataFrame,
        orders: pd.DataFrame,
        fills: pd.DataFrame,
        risk_events: pd.DataFrame,
        equity_curve: pd.DataFrame,
    ) -> pd.DataFrame:
        returns = trades["dynamic_account_return"].astype(float)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        final_equity = float(equity_curve["equity"].iloc[-1])
        max_drawdown = float(equity_curve["drawdown"].min())
        warn_count = int((risk_events["risk_level"] == "WARN").sum()) if not risk_events.empty else 0
        fail_count = int((risk_events["risk_level"] == "FAIL").sum()) if not risk_events.empty else 0
        return pd.DataFrame(
            [
                {
                    "strategy_name": self.config.get("strategy_name", ""),
                    "trade_mode": self.config.get("trade_mode", ""),
                    "paper_mode": self.paper_config.get("mode", ""),
                    "input_trades_path": str(self.input_trades_path),
                    "initial_cash": self.initial_cash,
                    "final_equity": final_equity,
                    "equity_multiple": final_equity / self.initial_cash if self.initial_cash else 0.0,
                    "signal_count": int(len(signals)),
                    "order_count": int(len(orders)),
                    "fill_count": int(len(fills)),
                    "executed_trade_count": int(len(trades)),
                    "buy_order_filled_count": int(((orders["side"] == "BUY") & (orders["order_status"] == "FILLED")).sum()),
                    "sell_order_filled_count": int(((orders["side"] == "SELL") & (orders["order_status"] == "FILLED")).sum()),
                    "unresolved_order_count": int((orders["order_status"] == "UNRESOLVED").sum()),
                    "rejected_order_count": int((orders["order_status"] == "REJECTED").sum()),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_account_return": float(returns.median()) if len(returns) else 0.0,
                    "max_profit": float(returns.max()) if len(returns) else 0.0,
                    "max_loss": float(returns.min()) if len(returns) else 0.0,
                    "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
                    "max_drawdown": max_drawdown,
                    "max_consecutive_losses": self.max_consecutive_losses(returns),
                    "avg_buy_amount_ratio": float(trades["buy_amount_ratio"].mean()) if "buy_amount_ratio" in trades else 0.0,
                    "max_buy_amount_ratio": float(trades["buy_amount_ratio"].max()) if "buy_amount_ratio" in trades else 0.0,
                    "avg_sell_amount_ratio": float(trades["sell_amount_ratio"].mean()) if "sell_amount_ratio" in trades else 0.0,
                    "max_sell_amount_ratio": float(trades["sell_amount_ratio"].max()) if "sell_amount_ratio" in trades else 0.0,
                    "avg_buy_slippage": float(trades["dynamic_buy_slippage_rate"].mean()) if "dynamic_buy_slippage_rate" in trades else 0.0,
                    "avg_sell_slippage": float(trades["dynamic_sell_slippage_rate"].mean()) if "dynamic_sell_slippage_rate" in trades else 0.0,
                    "risk_warn_count": warn_count,
                    "risk_fail_count": fail_count,
                    "live_order_enabled": False,
                }
            ]
        )

    def write_markdown(
        self,
        path: Path,
        summary: pd.DataFrame,
        risk_events: pd.DataFrame,
        equity_curve: pd.DataFrame,
    ) -> None:
        row = summary.iloc[0]
        risk_preview = risk_events.head(30).to_markdown(index=False) if not risk_events.empty else "无风险事件。"
        tail_curve = equity_curve.tail(10).to_markdown(index=False)
        content = f"""# 本地模拟盘账本报告

本报告只读取本地审计逐笔交易文件，不调用外部接口，不接实盘，不下真实订单。

## 策略

- 策略名：`{row['strategy_name']}`
- 模式：`{row['trade_mode']}` / `{row['paper_mode']}`
- 输入文件：`{row['input_trades_path']}`

## 汇总

{summary.to_markdown(index=False)}

## 风险事件预览

{risk_preview}

## 资金曲线尾部

{tail_curve}

## 结论限制

该模拟盘账本用于验证交易流程、资金记账、委托成交状态和风控事件输出。它仍基于日线审计成交结果，尚未使用分钟 K、集合竞价、盘口五档和真实排队数据，不能直接作为实盘依据。
"""
        path.write_text(content, encoding="utf-8")

    def conditions_text(self) -> str:
        filters = self.config.get("candidate_filters", {})
        include = [
            f"{condition.get('column')}={condition.get('value')}"
            for condition in filters.get("conditions", [])
        ]
        exclude = [
            f"exclude:{condition.get('column')}={condition.get('value')}"
            for condition in filters.get("exclude_conditions", [])
        ]
        for rule in filters.get("exclude_rules", []):
            text = "&&".join(
                f"{condition.get('column')}={condition.get('value')}"
                for condition in rule.get("conditions", [])
            )
            exclude.append(f"exclude:{text}")
        return ";".join(include + exclude)

    def signal_risk_note(self, row: object) -> str:
        notes = []
        if self.normalize_number(getattr(row, "buy_amount_ratio", 0.0)) > float(
            self.risk_thresholds.get("max_buy_amount_ratio_warn", 0.03)
        ):
            notes.append("买入成交额占比偏高")
        if self.normalize_number(getattr(row, "sell_amount_ratio", 0.0)) > float(
            self.risk_thresholds.get("max_sell_amount_ratio_warn", 0.03)
        ):
            notes.append("卖出成交额占比偏高")
        if self.normalize_number(getattr(row, "dynamic_account_return", 0.0)) <= float(
            self.risk_thresholds.get("max_single_trade_account_loss", -0.08)
        ):
            notes.append("单笔亏损预警")
        return ";".join(notes) if notes else "无"

    def estimate_shares(self, amount: float, price: float) -> dict[str, float | int]:
        if price <= 0 or amount <= 0:
            return {"estimated_shares": 0.0, "round_lot_shares": 0}
        estimated = amount / price
        round_lot = int(estimated // self.round_lot_size * self.round_lot_size)
        return {"estimated_shares": float(estimated), "round_lot_shares": round_lot}

    @staticmethod
    def max_consecutive_losses(returns: pd.Series) -> int:
        current = 0
        result = 0
        for value in returns.astype(float):
            if value <= 0:
                current += 1
                result = max(result, current)
            else:
                current = 0
        return result
