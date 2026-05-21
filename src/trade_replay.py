from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


@dataclass(frozen=True)
class ReplayRule:
    rule_name: str
    max_hold_days: int
    exit_price_field: str = "close"
    stop_loss: float | None = None
    take_profit: float | None = None


class ConservativeTradeReplay:
    """用日线做保守成交回放；不使用不存在的盘口数据。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("trade_replay")
        self.replay_config = self.config.get("trade_replay", {})
        self.analysis_config = self.config.get("analysis", {})
        self.daily_merged_path = self.project_root / self.analysis_config.get(
            "input_daily_merged_path", "data/processed/daily_merged.csv"
        )
        self.output_trade_report_path = self.project_root / self.replay_config.get(
            "output_trade_report_path", "reports/trade_replay_report.csv"
        )
        self.output_summary_path = self.project_root / self.replay_config.get(
            "output_summary_path", "reports/trade_replay_summary.csv"
        )
        self.output_yearly_path = self.project_root / self.replay_config.get(
            "output_yearly_path", "reports/trade_replay_yearly.csv"
        )
        self.initial_cash = float(self.replay_config.get("initial_cash", 1000000))
        self.position_pct = float(self.replay_config.get("position_pct", 0.8))
        self.max_hold_days = int(self.replay_config.get("max_hold_days", 5))
        self.buy_slippage_rate = float(self.replay_config.get("buy_slippage_rate", 0.001))
        self.sell_slippage_rate = float(self.replay_config.get("sell_slippage_rate", 0.001))
        self.limit_price_tolerance = float(self.replay_config.get("limit_price_tolerance", 0.002))
        self.assume_limit_open_unbuyable = bool(self.replay_config.get("assume_limit_open_unbuyable", True))
        self.assume_limit_down_unsellable = bool(self.replay_config.get("assume_limit_down_unsellable", True))
        self.conservative_same_day_stop_first = bool(
            self.replay_config.get("conservative_same_day_stop_first", True)
        )
        risk_config = self.config.get("risk", {})
        self.commission_rate = float(risk_config.get("commission_rate", 0.0003))
        self.stamp_tax_rate = float(risk_config.get("stamp_tax_rate", 0.001))
        self.transfer_fee_rate = float(risk_config.get("transfer_fee_rate", 0.00001))

    def replay(self) -> dict[str, Path]:
        signals = self.load_strategy_signals()
        forward_prices = self.load_forward_prices()
        samples = signals.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        rules = self.build_replay_rules()
        frames = [self.replay_rule(samples, rule) for rule in rules]
        trades = pd.concat(frames, ignore_index=True)
        summary = self.build_summary(trades)
        yearly = self.build_yearly_report(trades)

        self.output_trade_report_path.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(self.output_trade_report_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("保守成交回放明细已生成: %s, 行数: %s", self.output_trade_report_path, len(trades))
        self.logger.info("保守成交回放汇总已生成: %s", self.output_summary_path)
        self.logger.info("保守成交回放年度报告已生成: %s", self.output_yearly_path)
        return {
            "trade_report": self.output_trade_report_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def load_strategy_signals(self) -> pd.DataFrame:
        optimizer = StrategyConditionOptimizer(config_path="config/config.json")
        trades = optimizer.load_trades()
        for column, value in self.replay_config.get("base_conditions", {}).items():
            trades = trades[trades[column].astype(str) == str(value)].copy()
        for column, values in self.replay_config.get("base_exclusions", {}).items():
            excluded_values = {str(value) for value in values}
            trades = trades[~trades[column].astype(str).isin(excluded_values)].copy()
        selected = optimizer.select_daily_candidates(trades, max_holding_count=1)
        for column, values in self.replay_config.get("post_selection_exclusions", {}).items():
            excluded_values = {str(value) for value in values}
            selected = selected[~selected[column].astype(str).isin(excluded_values)].copy()
        selected["planned_position_pct"] = self.position_pct
        return selected

    def load_forward_prices(self) -> pd.DataFrame:
        usecols = ["trade_date", "ts_code", "open", "high", "low", "close", "pre_close", "pct_chg"]
        daily = pd.read_csv(
            self.daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=usecols,
            low_memory=False,
        )
        daily = daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        grouped = daily.groupby("ts_code")
        for offset in range(1, self.max_hold_days + 1):
            for column in ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg"]:
                daily[f"d{offset}_{column}"] = grouped[column].shift(-offset)
        return daily[["trade_date", "ts_code"] + [
            f"d{offset}_{column}"
            for offset in range(1, self.max_hold_days + 1)
            for column in ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg"]
        ]]

    def build_replay_rules(self) -> list[ReplayRule]:
        rules = [
            ReplayRule(
                rule_name=rule["rule_name"],
                max_hold_days=int(rule["max_hold_days"]),
                exit_price_field=rule.get("exit_price_field", "close"),
            )
            for rule in self.replay_config.get("fixed_exit_rules", [])
        ]
        for rule in self.replay_config.get("stop_take_exit_rules", []):
            rules.append(
                ReplayRule(
                    rule_name=rule["rule_name"],
                    max_hold_days=int(rule["max_hold_days"]),
                    stop_loss=float(rule["stop_loss"]),
                    take_profit=float(rule["take_profit"]),
                )
            )
        for stop_loss in self.replay_config.get("stop_loss_list", []):
            for take_profit in self.replay_config.get("take_profit_list", []):
                rules.append(
                    ReplayRule(
                        rule_name=f"stop_{abs(int(stop_loss * 100))}_tp_{int(take_profit * 100)}_hold{self.max_hold_days}",
                        max_hold_days=self.max_hold_days,
                        stop_loss=float(stop_loss),
                        take_profit=float(take_profit),
                    )
                )
        return rules

    def replay_rule(self, samples: pd.DataFrame, rule: ReplayRule) -> pd.DataFrame:
        rows = []
        for row in samples.itertuples(index=False):
            rows.append(self.replay_one(row, rule))
        return pd.DataFrame(rows)

    def replay_one(self, row: object, rule: ReplayRule) -> dict[str, object]:
        buy_info = self.resolve_buy(row)
        base = row._asdict()
        result = {
            **base,
            "replay_rule": rule.rule_name,
            "planned_position_pct": self.position_pct,
            **buy_info,
        }
        if not buy_info["buy_executed"]:
            return {
                **result,
                "sell_executed": False,
                "exit_trade_date": pd.NA,
                "exit_price_before_slippage": pd.NA,
                "exit_price": pd.NA,
                "exit_reason": "no_buy",
                "net_return": 0.0,
                "daily_return": 0.0,
                "is_win": False,
                "path_conflict": False,
                "limit_down_blocked_days": 0,
            }

        exit_info = self.resolve_exit(row, rule, buy_info["buy_price_before_slippage"])
        if not exit_info["sell_executed"]:
            net_return = 0.0
        else:
            gross_return = exit_info["exit_price"] / buy_info["buy_price"] - 1
            net_return = gross_return - self.fee_rate_without_slippage

        return {
            **result,
            **exit_info,
            "net_return": net_return,
            "daily_return": net_return * self.position_pct,
            "is_win": net_return > 0,
        }

    def resolve_buy(self, row: object) -> dict[str, object]:
        buy_trade_date = getattr(row, "d1_trade_date")
        buy_open = getattr(row, "d1_open")
        buy_pre_close = getattr(row, "d1_pre_close")
        if pd.isna(buy_trade_date) or pd.isna(buy_open) or pd.isna(buy_pre_close):
            return {
                "buy_executed": False,
                "buy_reject_reason": "missing_buy_day",
                "buy_trade_date": buy_trade_date,
                "buy_price_before_slippage": pd.NA,
                "buy_price": pd.NA,
                "buy_price_model": "missing",
            }

        if self.assume_limit_open_unbuyable and self.is_limit_up_open(
            row.ts_code,
            buy_open,
            buy_pre_close,
            name=getattr(row, "name", None),
        ):
            return {
                "buy_executed": False,
                "buy_reject_reason": "open_limit_up_unbuyable",
                "buy_trade_date": buy_trade_date,
                "buy_price_before_slippage": buy_open,
                "buy_price": pd.NA,
                "buy_price_model": "daily_open_limit_up_queue_rejected",
            }

        return {
            "buy_executed": True,
            "buy_reject_reason": "",
            "buy_trade_date": buy_trade_date,
            "buy_price_before_slippage": buy_open,
            "buy_price": buy_open * (1 + self.buy_slippage_rate),
            "buy_price_model": "daily_open_plus_slippage_no_l2",
        }

    def resolve_exit(self, row: object, rule: ReplayRule, buy_price: float) -> dict[str, object]:
        limit_down_blocked_days = 0
        path_conflict = False
        if rule.stop_loss is None or rule.take_profit is None:
            offset = min(rule.max_hold_days, self.max_hold_days)
            return self.resolve_fixed_exit(row, offset, rule.exit_price_field, limit_down_blocked_days)

        stop_price = buy_price * (1 + rule.stop_loss)
        take_profit_price = buy_price * (1 + rule.take_profit)
        for offset in range(2, min(rule.max_hold_days, self.max_hold_days) + 1):
            day = self.get_day(row, offset)
            if day is None:
                return self.unresolved_exit("missing_exit_day", limit_down_blocked_days, path_conflict)
            if self.is_limit_down_day(row.ts_code, day["open"], day["close"], day["pre_close"], name=getattr(row, "name", None)):
                limit_down_blocked_days += 1
                continue
            hit_stop = day["low"] <= stop_price
            hit_take_profit = day["high"] >= take_profit_price
            path_conflict = path_conflict or (hit_stop and hit_take_profit)
            if hit_stop and (self.conservative_same_day_stop_first or not hit_take_profit):
                raw_exit_price = min(day["open"], stop_price) if day["open"] < stop_price else stop_price
                return self.resolved_exit(day["trade_date"], raw_exit_price, "stop_loss", limit_down_blocked_days, path_conflict)
            if hit_take_profit:
                raw_exit_price = max(day["open"], take_profit_price) if day["open"] > take_profit_price else take_profit_price
                return self.resolved_exit(
                    day["trade_date"],
                    raw_exit_price,
                    "take_profit",
                    limit_down_blocked_days,
                    path_conflict,
                )
        return self.resolve_fixed_exit(row, min(rule.max_hold_days, self.max_hold_days), "close", limit_down_blocked_days)

    def resolve_fixed_exit(
        self,
        row: object,
        offset: int,
        exit_price_field: str,
        limit_down_blocked_days: int,
    ) -> dict[str, object]:
        for current_offset in range(offset, self.max_hold_days + 1):
            day = self.get_day(row, current_offset)
            if day is None:
                return self.unresolved_exit("missing_exit_day", limit_down_blocked_days, False)
            if self.is_limit_down_day(row.ts_code, day["open"], day["close"], day["pre_close"], name=getattr(row, "name", None)):
                limit_down_blocked_days += 1
                continue
            raw_exit_price = day[exit_price_field]
            return self.resolved_exit(day["trade_date"], raw_exit_price, f"fixed_{exit_price_field}", limit_down_blocked_days, False)
        return self.unresolved_exit("limit_down_unsellable_until_horizon", limit_down_blocked_days, False)

    def get_day(self, row: object, offset: int) -> dict[str, object] | None:
        trade_date = getattr(row, f"d{offset}_trade_date")
        open_price = getattr(row, f"d{offset}_open")
        high = getattr(row, f"d{offset}_high")
        low = getattr(row, f"d{offset}_low")
        close = getattr(row, f"d{offset}_close")
        pre_close = getattr(row, f"d{offset}_pre_close")
        if any(pd.isna(value) for value in [trade_date, open_price, high, low, close, pre_close]):
            return None
        return {
            "trade_date": trade_date,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "pre_close": pre_close,
        }

    def resolved_exit(
        self,
        trade_date: str,
        raw_exit_price: float,
        reason: str,
        limit_down_blocked_days: int,
        path_conflict: bool,
    ) -> dict[str, object]:
        return {
            "sell_executed": True,
            "sell_reject_reason": "",
            "exit_trade_date": trade_date,
            "exit_price_before_slippage": raw_exit_price,
            "exit_price": raw_exit_price * (1 - self.sell_slippage_rate),
            "exit_reason": reason,
            "sell_price_model": "daily_price_minus_slippage_no_l2",
            "path_conflict": path_conflict,
            "limit_down_blocked_days": limit_down_blocked_days,
        }

    @staticmethod
    def unresolved_exit(reason: str, limit_down_blocked_days: int, path_conflict: bool) -> dict[str, object]:
        return {
            "sell_executed": False,
            "sell_reject_reason": reason,
            "exit_trade_date": pd.NA,
            "exit_price_before_slippage": pd.NA,
            "exit_price": pd.NA,
            "exit_reason": reason,
            "sell_price_model": "unresolved",
            "path_conflict": path_conflict,
            "limit_down_blocked_days": limit_down_blocked_days,
        }

    def is_limit_up_open(self, ts_code: str, open_price: float, pre_close: float, name: object | None = None) -> bool:
        return open_price >= pre_close * (1 + self.limit_up_pct(ts_code, name) - self.limit_price_tolerance)

    def is_limit_down_day(
        self,
        ts_code: str,
        open_price: float,
        close: float,
        pre_close: float,
        name: object | None = None,
    ) -> bool:
        if not self.assume_limit_down_unsellable:
            return False
        limit_down_price = pre_close * (1 - self.limit_up_pct(ts_code, name) + self.limit_price_tolerance)
        return open_price <= limit_down_price or close <= limit_down_price

    @staticmethod
    def limit_up_pct(ts_code: str, name: object | None = None) -> float:
        stock_name = "" if name is None or pd.isna(name) else str(name).upper()
        if "ST" in stock_name or "退" in stock_name:
            return 0.05
        if ts_code.endswith(".BJ") or ts_code.startswith(("4", "8", "9")):
            return 0.30
        if ts_code.startswith(("300", "301", "688", "689")):
            return 0.20
        return 0.10

    @property
    def fee_rate_without_slippage(self) -> float:
        return self.commission_rate + self.transfer_fee_rate + self.commission_rate + self.transfer_fee_rate + self.stamp_tax_rate

    def build_summary(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for rule_name, group in trades.groupby("replay_rule"):
            executed = group[group["buy_executed"] & group["sell_executed"]].copy()
            returns = executed["net_return"].dropna()
            daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            equity_curve = (1 + daily_returns).cumprod()
            final_equity = self.initial_cash * float(equity_curve.iloc[-1]) if len(equity_curve) else self.initial_cash
            gains = returns[returns > 0]
            losses = returns[returns <= 0]
            rows.append(
                {
                    "replay_rule": rule_name,
                    "initial_cash": self.initial_cash,
                    "final_equity": final_equity,
                    "total_compound_return": final_equity / self.initial_cash - 1,
                    "signal_count": int(len(group)),
                    "buy_executed_count": int(group["buy_executed"].sum()),
                    "sell_executed_count": int((group["buy_executed"] & group["sell_executed"]).sum()),
                    "buy_rejected_count": int((~group["buy_executed"]).sum()),
                    "sell_unresolved_count": int((group["buy_executed"] & ~group["sell_executed"]).sum()),
                    "path_conflict_count": int(group["path_conflict"].sum()),
                    "limit_down_blocked_trades": int((group["limit_down_blocked_days"] > 0).sum()),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                    "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
                    "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
                    "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
                    "position_pct": self.position_pct,
                }
            )
        return pd.DataFrame(rows).sort_values(["final_equity", "max_drawdown"], ascending=[False, True])

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        executed = trades[trades["buy_executed"] & trades["sell_executed"]].copy()
        executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
        for (rule_name, year), group in executed.groupby(["replay_rule", "year"]):
            returns = group["net_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "replay_rule": rule_name,
                    "year": year,
                    "sample_count": int(len(group)),
                    "year_return": float((1 + daily_returns).prod() - 1),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return pd.DataFrame(rows)
