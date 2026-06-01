from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计 A5-R1 动态容量版的日线止盈止损路径。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    outputs = A5R1ExitPathAuditor(config_path=args.config).audit()
    print("A5-R1 日线止盈止损路径审计完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1ExitPathAuditor:
    """基于日线 OHLC 审计止盈止损触发、同日冲突和跌停阻塞。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_exit_path_audit")
        self.audit_config = self.config.get("a5_r1_exit_path_audit", {})
        self.input_dynamic_detail_path = self.project_root / self.audit_config.get(
            "input_dynamic_detail_path",
            "reports/a5_r1_dynamic_capital_slippage_detail.csv",
        )
        self.input_daily_merged_path = self.project_root / self.audit_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.output_summary_path = self.project_root / self.audit_config.get(
            "output_summary_path",
            "reports/a5_r1_exit_path_audit_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.audit_config.get(
            "output_yearly_path",
            "reports/a5_r1_exit_path_audit_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.audit_config.get(
            "output_detail_path",
            "reports/a5_r1_exit_path_audit_detail.csv",
        )
        self.target_scenario = str(self.audit_config.get("target_scenario", "single_position_conservative_slippage_cap_5pct"))
        self.initial_cash = float(self.audit_config.get("initial_cash", 500000))
        self.stop_loss = float(self.audit_config.get("stop_loss", -0.03))
        self.take_profit = float(self.audit_config.get("take_profit", 0.15))
        self.max_hold_days = int(self.audit_config.get("max_hold_days", 3))
        self.fallback_max_hold_days = int(self.audit_config.get("fallback_max_hold_days", 5))
        self.conservative_same_day_stop_first = bool(
            self.audit_config.get("conservative_same_day_stop_first", True)
        )
        self.assume_limit_down_unsellable = bool(self.audit_config.get("assume_limit_down_unsellable", True))
        self.limit_price_tolerance = float(self.audit_config.get("limit_price_tolerance", 0.002))
        risk_config = self.config.get("risk", {})
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def audit(self) -> dict[str, Path]:
        daily_amount = self.load_daily_amount()
        trades = self.load_target_trades()
        detail = self.build_detail(trades, daily_amount)
        summary = self.build_summary(detail)
        yearly = self.build_yearly(detail)

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 日线路径审计汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 日线路径审计年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 日线路径审计明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_daily_amount(self) -> dict[tuple[str, str], float]:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "amount"],
            low_memory=False,
        )
        daily["trade_date"] = daily["trade_date"].map(self.normalize_date)
        daily["amount_yuan"] = pd.to_numeric(daily["amount"], errors="coerce") * 1000
        return {
            (str(row.trade_date), str(row.ts_code)): float(row.amount_yuan)
            for row in daily.itertuples(index=False)
            if pd.notna(row.amount_yuan)
        }

    def load_target_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_dynamic_detail_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        trades = trades[
            (trades["scenario"].astype(str) == self.target_scenario)
            & (trades["scenario_executed"] == True)  # noqa: E712
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到目标场景已成交交易: {self.target_scenario}")

        for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
            trades[column] = trades[column].map(self.normalize_date)
        numeric_columns = [
            "dynamic_buy_price",
            "dynamic_account_return",
            "equity_before",
            "equity_after",
            "actual_buy_amount",
            "actual_position_pct",
            "buy_price_before_slippage",
            "exit_price_before_slippage",
        ]
        for offset in range(1, self.fallback_max_hold_days + 1):
            numeric_columns.extend(
                [
                    f"d{offset}_open",
                    f"d{offset}_high",
                    f"d{offset}_low",
                    f"d{offset}_close",
                    f"d{offset}_pre_close",
                ]
            )
            date_column = f"d{offset}_trade_date"
            if date_column in trades.columns:
                trades[date_column] = trades[date_column].map(self.normalize_date)
        for column in numeric_columns:
            if column in trades.columns:
                trades[column] = pd.to_numeric(trades[column], errors="coerce")
        return trades.sort_values(["exit_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def build_detail(self, trades: pd.DataFrame, daily_amount: dict[tuple[str, str], float]) -> pd.DataFrame:
        rows = []
        alternative_equity = self.initial_cash
        for order, row in enumerate(trades.itertuples(index=False), start=1):
            audit = self.resolve_stop_take_exit(row, daily_amount)
            current_return = float(getattr(row, "dynamic_account_return"))
            current_equity_before = float(getattr(row, "equity_before"))
            current_equity_after = float(getattr(row, "equity_after"))
            alt_return = float(audit["alternative_account_return"])
            alternative_equity_before = alternative_equity
            alternative_equity = alternative_equity * (1 + alt_return)

            item = row._asdict()
            item.update(audit)
            item.update(
                {
                    "path_order": order,
                    "current_account_return": current_return,
                    "current_equity_before": current_equity_before,
                    "current_equity_after": current_equity_after,
                    "alternative_equity_before": alternative_equity_before,
                    "alternative_equity_after": alternative_equity,
                    "return_diff_vs_current": alt_return - current_return,
                }
            )
            rows.append(item)
        return pd.DataFrame(rows)

    def resolve_stop_take_exit(self, row: object, daily_amount: dict[tuple[str, str], float]) -> dict[str, Any]:
        buy_price = float(getattr(row, "dynamic_buy_price"))
        stop_price = buy_price * (1 + self.stop_loss)
        take_profit_price = buy_price * (1 + self.take_profit)
        path_conflict = False
        blocked_days = 0
        hit_stop_any = False
        hit_take_profit_any = False

        for offset in range(2, self.max_hold_days + 1):
            day = self.get_day(row, offset)
            if day is None:
                return self.unresolved_exit(row, "missing_exit_day", blocked_days, path_conflict)
            if self.is_limit_down_day(str(getattr(row, "ts_code")), day, getattr(row, "name", None)):
                blocked_days += 1
                continue

            hit_stop = day["low"] <= stop_price
            hit_take_profit = day["high"] >= take_profit_price
            hit_stop_any = hit_stop_any or hit_stop
            hit_take_profit_any = hit_take_profit_any or hit_take_profit
            path_conflict = path_conflict or (hit_stop and hit_take_profit)

            if hit_stop and (self.conservative_same_day_stop_first or not hit_take_profit):
                raw_exit_price = min(day["open"], stop_price) if day["open"] < stop_price else stop_price
                return self.resolved_exit(
                    row,
                    day,
                    raw_exit_price,
                    "stop_loss",
                    blocked_days,
                    path_conflict,
                    hit_stop_any,
                    hit_take_profit_any,
                    daily_amount,
                )
            if hit_take_profit:
                raw_exit_price = max(day["open"], take_profit_price) if day["open"] > take_profit_price else take_profit_price
                return self.resolved_exit(
                    row,
                    day,
                    raw_exit_price,
                    "take_profit",
                    blocked_days,
                    path_conflict,
                    hit_stop_any,
                    hit_take_profit_any,
                    daily_amount,
                )

        for offset in range(self.max_hold_days, self.fallback_max_hold_days + 1):
            day = self.get_day(row, offset)
            if day is None:
                return self.unresolved_exit(row, "missing_exit_day", blocked_days, path_conflict)
            if self.is_limit_down_day(str(getattr(row, "ts_code")), day, getattr(row, "name", None)):
                blocked_days += 1
                continue
            return self.resolved_exit(
                row,
                day,
                day["close"],
                "fixed_hold_close",
                blocked_days,
                path_conflict,
                hit_stop_any,
                hit_take_profit_any,
                daily_amount,
            )

        return self.unresolved_exit(row, "limit_down_unsellable_until_horizon", blocked_days, path_conflict)

    def resolved_exit(
        self,
        row: object,
        day: dict[str, float | str],
        raw_exit_price: float,
        exit_reason: str,
        blocked_days: int,
        path_conflict: bool,
        hit_stop_any: bool,
        hit_take_profit_any: bool,
        daily_amount: dict[tuple[str, str], float],
    ) -> dict[str, Any]:
        buy_price = float(getattr(row, "dynamic_buy_price"))
        actual_buy_amount = float(getattr(row, "actual_buy_amount"))
        actual_position_pct = float(getattr(row, "actual_position_pct"))
        sell_value_before_slippage = actual_buy_amount * (raw_exit_price / buy_price)
        exit_trade_date = str(day["trade_date"])
        sell_day_amount = daily_amount.get((exit_trade_date, str(getattr(row, "ts_code"))), 0.0)
        sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
        sell_slippage = self.estimate_slippage_rate(sell_amount_ratio)
        dynamic_exit_price = raw_exit_price * (1 - sell_slippage)
        net_return = dynamic_exit_price / buy_price - 1 - self.fee_rate_without_slippage
        account_return = net_return * actual_position_pct
        return {
            "alternative_sell_executed": True,
            "alternative_exit_trade_date": exit_trade_date,
            "alternative_exit_reason": exit_reason,
            "alternative_exit_price_before_slippage": raw_exit_price,
            "alternative_sell_amount_ratio": sell_amount_ratio,
            "alternative_sell_slippage_rate": sell_slippage,
            "alternative_exit_price": dynamic_exit_price,
            "alternative_net_return": net_return,
            "alternative_account_return": account_return,
            "audit_stop_price": buy_price * (1 + self.stop_loss),
            "audit_take_profit_price": buy_price * (1 + self.take_profit),
            "audit_hit_stop_any": hit_stop_any,
            "audit_hit_take_profit_any": hit_take_profit_any,
            "audit_path_conflict": path_conflict,
            "audit_limit_down_blocked_days": blocked_days,
        }

    def unresolved_exit(
        self,
        row: object,
        reason: str,
        blocked_days: int,
        path_conflict: bool,
    ) -> dict[str, Any]:
        return {
            "alternative_sell_executed": False,
            "alternative_exit_trade_date": "",
            "alternative_exit_reason": reason,
            "alternative_exit_price_before_slippage": pd.NA,
            "alternative_sell_amount_ratio": 0.0,
            "alternative_sell_slippage_rate": 0.0,
            "alternative_exit_price": pd.NA,
            "alternative_net_return": 0.0,
            "alternative_account_return": 0.0,
            "audit_stop_price": float(getattr(row, "dynamic_buy_price")) * (1 + self.stop_loss),
            "audit_take_profit_price": float(getattr(row, "dynamic_buy_price")) * (1 + self.take_profit),
            "audit_hit_stop_any": False,
            "audit_hit_take_profit_any": False,
            "audit_path_conflict": path_conflict,
            "audit_limit_down_blocked_days": blocked_days,
        }

    def build_summary(self, detail: pd.DataFrame) -> pd.DataFrame:
        rows = [
            self.summarize_return_series(detail, "current_fixed_t2_close", "current_account_return", "current_equity_after"),
            self.summarize_return_series(
                detail,
                "daily_proxy_stop_3_tp_15_hold3",
                "alternative_account_return",
                "alternative_equity_after",
            ),
        ]
        audit_row = {
            "scenario": "path_audit_counts",
            "description": "日线级路径事件计数，不代表分钟级真实先后顺序。",
            "executed_trade_count": int(len(detail)),
            "stop_hit_count": int(detail["audit_hit_stop_any"].sum()),
            "take_profit_hit_count": int(detail["audit_hit_take_profit_any"].sum()),
            "same_day_conflict_count": int(detail["audit_path_conflict"].sum()),
            "limit_down_blocked_trade_count": int((detail["audit_limit_down_blocked_days"] > 0).sum()),
            "limit_down_blocked_day_total": int(detail["audit_limit_down_blocked_days"].sum()),
            "stop_exit_count": int((detail["alternative_exit_reason"] == "stop_loss").sum()),
            "take_profit_exit_count": int((detail["alternative_exit_reason"] == "take_profit").sum()),
            "fixed_exit_count": int((detail["alternative_exit_reason"] == "fixed_hold_close").sum()),
        }
        return pd.concat([pd.DataFrame(rows), pd.DataFrame([audit_row])], ignore_index=True)

    def summarize_return_series(
        self,
        detail: pd.DataFrame,
        scenario: str,
        return_column: str,
        equity_column: str,
    ) -> dict[str, Any]:
        returns = pd.to_numeric(detail[return_column], errors="coerce").dropna()
        equity_curve = pd.to_numeric(detail[equity_column], errors="coerce") / self.initial_cash
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        final_equity = float(detail[equity_column].dropna().iloc[-1]) if len(detail) else self.initial_cash
        return {
            "scenario": scenario,
            "description": "当前固定卖出" if scenario == "current_fixed_t2_close" else "日线代理止损止盈卖出",
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "equity_multiple": final_equity / self.initial_cash if self.initial_cash else 0.0,
            "total_compound_return": final_equity / self.initial_cash - 1 if self.initial_cash else 0.0,
            "executed_trade_count": int(len(returns)),
            "win_rate": self.win_rate(returns),
            "avg_account_return": self.mean(returns),
            "median_account_return": self.median(returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def build_yearly(self, detail: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for scenario, return_column, equity_column in [
            ("current_fixed_t2_close", "current_account_return", "current_equity_after"),
            ("daily_proxy_stop_3_tp_15_hold3", "alternative_account_return", "alternative_equity_after"),
        ]:
            sample = detail.copy()
            if scenario == "daily_proxy_stop_3_tp_15_hold3":
                sample["year"] = sample["alternative_exit_trade_date"].astype(str).str[:4]
            else:
                sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
            for year, group in sample.groupby("year"):
                if not str(year).isdigit():
                    continue
                returns = pd.to_numeric(group[return_column], errors="coerce").dropna()
                if scenario == "current_fixed_t2_close":
                    first_equity = float(group["current_equity_before"].iloc[0])
                    stop_exit_count = pd.NA
                    take_profit_exit_count = pd.NA
                    path_conflict_count = pd.NA
                else:
                    first_equity = float(group["alternative_equity_before"].iloc[0])
                    stop_exit_count = int((group["alternative_exit_reason"] == "stop_loss").sum())
                    take_profit_exit_count = int((group["alternative_exit_reason"] == "take_profit").sum())
                    path_conflict_count = int(group["audit_path_conflict"].sum())
                last_equity = float(group[equity_column].iloc[-1])
                equity_curve = group[equity_column] / first_equity if first_equity else pd.Series(dtype=float)
                rows.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "sample_count": int(len(group)),
                        "first_equity": first_equity,
                        "last_equity": last_equity,
                        "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                        "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                        "win_rate": self.win_rate(returns),
                        "max_loss": self.min_value(returns),
                        "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                        "stop_exit_count": stop_exit_count,
                        "take_profit_exit_count": take_profit_exit_count,
                        "path_conflict_count": path_conflict_count,
                    }
                )
        return pd.DataFrame(rows)

    def get_day(self, row: object, offset: int) -> dict[str, float | str] | None:
        values = {
            "trade_date": getattr(row, f"d{offset}_trade_date", ""),
            "open": getattr(row, f"d{offset}_open", pd.NA),
            "high": getattr(row, f"d{offset}_high", pd.NA),
            "low": getattr(row, f"d{offset}_low", pd.NA),
            "close": getattr(row, f"d{offset}_close", pd.NA),
            "pre_close": getattr(row, f"d{offset}_pre_close", pd.NA),
        }
        if any(pd.isna(value) or value == "" for value in values.values()):
            return None
        return values

    def is_limit_down_day(self, ts_code: str, day: dict[str, float | str], name: object | None = None) -> bool:
        if not self.assume_limit_down_unsellable:
            return False
        limit_down_price = float(day["pre_close"]) * (
            1 - self.limit_up_pct(ts_code, name) + self.limit_price_tolerance
        )
        return float(day["open"]) <= limit_down_price or float(day["close"]) <= limit_down_price

    def estimate_slippage_rate(self, amount_ratio: float) -> float:
        if pd.isna(amount_ratio) or amount_ratio <= 0:
            return 0.0
        epsilon = 1e-12
        for tier in self.audit_config.get("slippage_tiers", []):
            max_ratio = tier.get("max_amount_ratio")
            slippage_rate = float(tier.get("slippage_rate", 0.0))
            if max_ratio is None or amount_ratio <= float(max_ratio) + epsilon:
                return slippage_rate
        return 0.0

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

    @staticmethod
    def normalize_date(value: object) -> str:
        if pd.isna(value):
            return ""
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        return text

    @staticmethod
    def win_rate(returns: pd.Series) -> float:
        return float((returns > 0).mean()) if len(returns) else 0.0

    @staticmethod
    def mean(returns: pd.Series) -> float:
        return float(returns.mean()) if len(returns) else 0.0

    @staticmethod
    def median(returns: pd.Series) -> float:
        return float(returns.median()) if len(returns) else 0.0

    @staticmethod
    def max_value(returns: pd.Series) -> float:
        return float(returns.max()) if len(returns) else 0.0

    @staticmethod
    def min_value(returns: pd.Series) -> float:
        return float(returns.min()) if len(returns) else 0.0


if __name__ == "__main__":
    main()
