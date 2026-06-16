from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class MinuteTradeReplay:
    """用分钟 K 线逐分钟验证止盈止损先后；没有分钟数据时只输出不可验证报告。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("minute_trade_replay")
        self.replay_config = self.config.get("minute_trade_replay", {})
        self.minute_bars_path = self.project_root / self.replay_config.get(
            "minute_bars_path", "data/processed/minute_bars.csv"
        )
        self.output_trade_report_path = self.project_root / self.replay_config.get(
            "output_trade_report_path", "reports/minute_trade_replay_report.csv"
        )
        self.output_summary_path = self.project_root / self.replay_config.get(
            "output_summary_path", "reports/minute_trade_replay_summary.csv"
        )
        self.output_yearly_path = self.project_root / self.replay_config.get(
            "output_yearly_path", "reports/minute_trade_replay_yearly.csv"
        )
        self.required_columns = list(
            self.replay_config.get(
                "required_columns",
                ["ts_code", "trade_date", "trade_time", "open", "high", "low", "close", "volume", "amount"],
            )
        )
        self.initial_cash = float(self.replay_config.get("initial_cash", 1000000))
        self.position_pct = float(self.replay_config.get("position_pct", 0.8))
        self.max_hold_days = int(self.replay_config.get("max_hold_days", 5))
        self.buy_slippage_rate = float(self.replay_config.get("buy_slippage_rate", 0.001))
        self.sell_slippage_rate = float(self.replay_config.get("sell_slippage_rate", 0.001))
        self.limit_price_tolerance = float(self.replay_config.get("limit_price_tolerance", 0.002))
        self.assume_limit_open_unbuyable = bool(self.replay_config.get("assume_limit_open_unbuyable", True))
        self.assume_limit_down_unsellable = bool(self.replay_config.get("assume_limit_down_unsellable", True))
        self.conservative_same_minute_stop_first = bool(
            self.replay_config.get("conservative_same_minute_stop_first", True)
        )
        self.chunk_size = int(self.replay_config.get("chunk_size", 300000))
        risk_config = self.config.get("risk", {})
        self.commission_rate = float(risk_config.get("commission_rate", 0.0003))
        self.stamp_tax_rate = float(risk_config.get("stamp_tax_rate", 0.001))
        self.transfer_fee_rate = float(risk_config.get("transfer_fee_rate", 0.00001))

    def replay(self) -> dict[str, Path]:
        data_status = self.inspect_minute_data()
        if data_status["status"] != "READY":
            return self.write_unavailable_reports(data_status)

        daily_replay = self.build_daily_replay_adapter()
        signals = daily_replay.load_strategy_signals()
        forward_prices = daily_replay.load_forward_prices()
        samples = signals.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        rules = self.build_minute_rules()
        target_dates = self.collect_target_dates(samples)
        minute_bars = self.load_minute_bars(set(samples["ts_code"].astype(str)), target_dates)
        frames = [self.replay_rule(samples, rule, minute_bars) for rule in rules]
        trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        summary = self.build_summary(trades, data_status)
        yearly = self.build_yearly_report(trades)

        mkdir_p(self.output_trade_report_path.parent)
        trades.to_csv(self.output_trade_report_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("分钟级成交回放明细已生成: %s, 行数: %s", self.output_trade_report_path, len(trades))
        self.logger.info("分钟级成交回放汇总已生成: %s", self.output_summary_path)
        self.logger.info("分钟级成交回放年度报告已生成: %s", self.output_yearly_path)
        return {
            "trade_report": self.output_trade_report_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def inspect_minute_data(self) -> dict[str, Any]:
        if not self.minute_bars_path.exists():
            return {
                "status": "MISSING_MINUTE_DATA",
                "path": str(self.minute_bars_path),
                "row_count": 0,
                "missing_columns": ",".join(self.required_columns),
            }
        columns = pd.read_csv(self.minute_bars_path, nrows=0, low_memory=False).columns.tolist()
        columns = [str(column).lstrip("\ufeff") for column in columns]
        missing_columns = [column for column in self.required_columns if column not in columns]
        if missing_columns:
            return {
                "status": "INCOMPLETE_MINUTE_DATA",
                "path": str(self.minute_bars_path),
                "row_count": 0,
                "missing_columns": ",".join(missing_columns),
            }
        row_count = self.count_csv_rows(self.minute_bars_path)
        return {
            "status": "READY",
            "path": str(self.minute_bars_path),
            "row_count": row_count,
            "missing_columns": "",
        }

    @staticmethod
    def count_csv_rows(path: Path) -> int:
        with path.open("rb") as file:
            line_count = sum(1 for _ in file)
        return max(line_count - 1, 0)

    def write_unavailable_reports(self, data_status: dict[str, Any]) -> dict[str, Path]:
        report = pd.DataFrame(
            [
                {
                    "validation_status": data_status["status"],
                    "minute_bars_path": data_status["path"],
                    "missing_columns": data_status["missing_columns"],
                    "message": "当前没有可用分钟 K 线，不能逐分钟验证止盈止损先后顺序。",
                }
            ]
        )
        summary = pd.DataFrame(
            [
                {
                    "replay_rule": "minute_path_validation",
                    "data_status": data_status["status"],
                    "initial_cash": self.initial_cash,
                    "final_equity": self.initial_cash,
                    "signal_count": 0,
                    "buy_executed_count": 0,
                    "sell_executed_count": 0,
                    "path_conflict_count": 0,
                    "minute_confirmed_exit_count": 0,
                    "message": "补充 data/processed/minute_bars.csv 后再运行本脚本。",
                }
            ]
        )
        yearly = pd.DataFrame(
            columns=[
                "replay_rule",
                "year",
                "sample_count",
                "year_return",
                "win_rate",
                "avg_return_per_trade",
                "median_return_per_trade",
            ]
        )
        mkdir_p(self.output_trade_report_path.parent)
        report.to_csv(self.output_trade_report_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.warning("分钟 K 线不可用，已生成不可验证报告: %s", self.output_summary_path)
        return {
            "trade_report": self.output_trade_report_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def build_daily_replay_adapter(self) -> ConservativeTradeReplay:
        adapter = ConservativeTradeReplay(config_path="config/config.json")
        adapter.replay_config = {
            **adapter.replay_config,
            "base_conditions": self.replay_config.get("base_conditions", {}),
            "position_pct": self.position_pct,
            "max_hold_days": self.max_hold_days,
            "stop_loss_list": self.replay_config.get("stop_loss_list", []),
            "take_profit_list": self.replay_config.get("take_profit_list", []),
            "buy_slippage_rate": self.buy_slippage_rate,
            "sell_slippage_rate": self.sell_slippage_rate,
            "limit_price_tolerance": self.limit_price_tolerance,
            "assume_limit_open_unbuyable": self.assume_limit_open_unbuyable,
            "assume_limit_down_unsellable": self.assume_limit_down_unsellable,
        }
        adapter.position_pct = self.position_pct
        adapter.max_hold_days = self.max_hold_days
        adapter.buy_slippage_rate = self.buy_slippage_rate
        adapter.sell_slippage_rate = self.sell_slippage_rate
        adapter.limit_price_tolerance = self.limit_price_tolerance
        adapter.assume_limit_open_unbuyable = self.assume_limit_open_unbuyable
        adapter.assume_limit_down_unsellable = self.assume_limit_down_unsellable
        return adapter

    def build_minute_rules(self) -> list[ReplayRule]:
        rules = []
        for stop_loss in self.replay_config.get("stop_loss_list", []):
            for take_profit in self.replay_config.get("take_profit_list", []):
                rules.append(
                    ReplayRule(
                        rule_name=f"minute_stop_{abs(int(stop_loss * 100))}_tp_{int(take_profit * 100)}_hold{self.max_hold_days}",
                        max_hold_days=self.max_hold_days,
                        stop_loss=float(stop_loss),
                        take_profit=float(take_profit),
                    )
                )
        return rules

    def collect_target_dates(self, samples: pd.DataFrame) -> set[str]:
        target_dates: set[str] = set()
        for offset in range(2, self.max_hold_days + 1):
            column = f"d{offset}_trade_date"
            if column in samples:
                target_dates.update(samples[column].dropna().astype(str).tolist())
        return target_dates

    def load_minute_bars(self, target_codes: set[str], target_dates: set[str]) -> pd.DataFrame:
        frames = []
        usecols = self.required_columns
        for chunk in pd.read_csv(
            self.minute_bars_path,
            dtype={"ts_code": str, "trade_date": str, "trade_time": str},
            usecols=usecols,
            chunksize=self.chunk_size,
            low_memory=False,
        ):
            matched = chunk[
                chunk["ts_code"].astype(str).isin(target_codes)
                & chunk["trade_date"].astype(str).isin(target_dates)
            ].copy()
            if not matched.empty:
                frames.append(matched)
        if not frames:
            return pd.DataFrame(columns=usecols)
        minute = pd.concat(frames, ignore_index=True)
        minute["trade_time"] = minute["trade_time"].map(self.normalize_trade_time)
        numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
        for column in numeric_columns:
            minute[column] = pd.to_numeric(minute[column], errors="coerce")
        return minute.sort_values(["ts_code", "trade_date", "trade_time"]).reset_index(drop=True)

    @staticmethod
    def normalize_trade_time(value: object) -> str:
        text = str(value).strip()
        if ":" in text:
            parts = text.split(":")
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        text = text.zfill(4)
        return f"{text[:2]}:{text[2:4]}"

    def replay_rule(self, samples: pd.DataFrame, rule: ReplayRule, minute_bars: pd.DataFrame) -> pd.DataFrame:
        rows = []
        grouped_minutes = {
            key: group
            for key, group in minute_bars.groupby(["ts_code", "trade_date"], sort=False)
        }
        daily_adapter = self.build_daily_replay_adapter()
        for row in samples.itertuples(index=False):
            rows.append(self.replay_one(row, rule, grouped_minutes, daily_adapter))
        return pd.DataFrame(rows)

    def replay_one(
        self,
        row: object,
        rule: ReplayRule,
        grouped_minutes: dict[tuple[str, str], pd.DataFrame],
        daily_adapter: ConservativeTradeReplay,
    ) -> dict[str, object]:
        buy_info = daily_adapter.resolve_buy(row)
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
                "sell_reject_reason": "",
                "exit_trade_date": pd.NA,
                "exit_trade_time": pd.NA,
                "exit_price_before_slippage": pd.NA,
                "exit_price": pd.NA,
                "exit_reason": "no_buy",
                "sell_price_model": "no_sell",
                "net_return": 0.0,
                "daily_return": 0.0,
                "is_win": False,
                "path_conflict": False,
                "limit_down_blocked_days": 0,
                "minute_data_found": False,
            }

        exit_info = self.resolve_minute_exit(row, rule, buy_info["buy_price_before_slippage"], grouped_minutes)
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

    def resolve_minute_exit(
        self,
        row: object,
        rule: ReplayRule,
        buy_price_before_slippage: float,
        grouped_minutes: dict[tuple[str, str], pd.DataFrame],
    ) -> dict[str, object]:
        stop_price = buy_price_before_slippage * (1 + float(rule.stop_loss))
        take_profit_price = buy_price_before_slippage * (1 + float(rule.take_profit))
        limit_down_blocked_days = 0
        path_conflict = False
        minute_data_found = False

        for offset in range(2, min(rule.max_hold_days, self.max_hold_days) + 1):
            day = self.get_daily_day(row, offset)
            if day is None:
                return self.unresolved_exit("missing_exit_day", limit_down_blocked_days, path_conflict, minute_data_found)
            if self.is_limit_down_day(
                row.ts_code,
                day["open"],
                day["close"],
                day["pre_close"],
                name=getattr(row, "name", None),
            ):
                limit_down_blocked_days += 1
                continue
            minute = grouped_minutes.get((str(row.ts_code), str(day["trade_date"])))
            if minute is None or minute.empty:
                continue
            minute_data_found = True
            for bar in minute.itertuples(index=False):
                hit_stop = float(bar.low) <= stop_price
                hit_take_profit = float(bar.high) >= take_profit_price
                if hit_stop and hit_take_profit:
                    path_conflict = True
                if hit_stop and (self.conservative_same_minute_stop_first or not hit_take_profit):
                    raw_exit_price = min(float(bar.open), stop_price) if float(bar.open) < stop_price else stop_price
                    return self.resolved_exit(
                        str(day["trade_date"]),
                        str(bar.trade_time),
                        raw_exit_price,
                        "minute_stop_loss",
                        limit_down_blocked_days,
                        path_conflict,
                        minute_data_found,
                    )
                if hit_take_profit:
                    raw_exit_price = max(float(bar.open), take_profit_price) if float(bar.open) > take_profit_price else take_profit_price
                    return self.resolved_exit(
                        str(day["trade_date"]),
                        str(bar.trade_time),
                        raw_exit_price,
                        "minute_take_profit",
                        limit_down_blocked_days,
                        path_conflict,
                        minute_data_found,
                    )

        fallback_day = self.get_daily_day(row, min(rule.max_hold_days, self.max_hold_days))
        if fallback_day is None:
            return self.unresolved_exit("missing_exit_day", limit_down_blocked_days, path_conflict, minute_data_found)
        if self.is_limit_down_day(
            row.ts_code,
            fallback_day["open"],
            fallback_day["close"],
            fallback_day["pre_close"],
            name=getattr(row, "name", None),
        ):
            return self.unresolved_exit(
                "limit_down_unsellable_until_horizon",
                limit_down_blocked_days + 1,
                path_conflict,
                minute_data_found,
            )
        return self.resolved_exit(
            str(fallback_day["trade_date"]),
            "15:00",
            float(fallback_day["close"]),
            "minute_no_trigger_fallback_close",
            limit_down_blocked_days,
            path_conflict,
            minute_data_found,
        )

    def get_daily_day(self, row: object, offset: int) -> dict[str, object] | None:
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
        trade_time: str,
        raw_exit_price: float,
        reason: str,
        limit_down_blocked_days: int,
        path_conflict: bool,
        minute_data_found: bool,
    ) -> dict[str, object]:
        return {
            "sell_executed": True,
            "sell_reject_reason": "",
            "exit_trade_date": trade_date,
            "exit_trade_time": trade_time,
            "exit_price_before_slippage": raw_exit_price,
            "exit_price": raw_exit_price * (1 - self.sell_slippage_rate),
            "exit_reason": reason,
            "sell_price_model": "minute_bar_trigger_minus_slippage",
            "path_conflict": path_conflict,
            "limit_down_blocked_days": limit_down_blocked_days,
            "minute_data_found": minute_data_found,
        }

    @staticmethod
    def unresolved_exit(
        reason: str,
        limit_down_blocked_days: int,
        path_conflict: bool,
        minute_data_found: bool,
    ) -> dict[str, object]:
        return {
            "sell_executed": False,
            "sell_reject_reason": reason,
            "exit_trade_date": pd.NA,
            "exit_trade_time": pd.NA,
            "exit_price_before_slippage": pd.NA,
            "exit_price": pd.NA,
            "exit_reason": reason,
            "sell_price_model": "unresolved",
            "path_conflict": path_conflict,
            "limit_down_blocked_days": limit_down_blocked_days,
            "minute_data_found": minute_data_found,
        }

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

    def build_summary(self, trades: pd.DataFrame, data_status: dict[str, Any]) -> pd.DataFrame:
        if trades.empty:
            return pd.DataFrame()
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
                    "data_status": data_status["status"],
                    "initial_cash": self.initial_cash,
                    "final_equity": final_equity,
                    "total_compound_return": final_equity / self.initial_cash - 1,
                    "signal_count": int(len(group)),
                    "buy_executed_count": int(group["buy_executed"].sum()),
                    "sell_executed_count": int((group["buy_executed"] & group["sell_executed"]).sum()),
                    "minute_data_found_count": int(group["minute_data_found"].sum()),
                    "minute_confirmed_exit_count": int(group["exit_reason"].astype(str).str.startswith("minute_").sum()),
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
        if trades.empty:
            return pd.DataFrame(
                columns=[
                    "replay_rule",
                    "year",
                    "sample_count",
                    "year_return",
                    "win_rate",
                    "avg_return_per_trade",
                    "median_return_per_trade",
                ]
            )
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
