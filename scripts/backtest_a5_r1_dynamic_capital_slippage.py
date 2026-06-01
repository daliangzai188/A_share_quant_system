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
    parser = argparse.ArgumentParser(description="回测 A5-R1 50万起步动态复利滑点版本。")
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
    outputs = A5R1DynamicCapitalSlippageBacktester(config_path=args.config).backtest()
    print("A5-R1 50万起步动态复利滑点回测完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1DynamicCapitalSlippageBacktester:
    """用账户权益动态计算每笔买卖金额、成交额占比和滑点。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_dynamic_capital_slippage")
        self.backtest_config = self.config.get("a5_r1_dynamic_capital_slippage_backtest", {})
        self.input_trade_replay_path = self.project_root / self.backtest_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.input_daily_merged_path = self.project_root / self.backtest_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.output_summary_path = self.project_root / self.backtest_config.get(
            "output_summary_path",
            "reports/a5_r1_dynamic_capital_slippage_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.backtest_config.get(
            "output_yearly_path",
            "reports/a5_r1_dynamic_capital_slippage_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.backtest_config.get(
            "output_detail_path",
            "reports/a5_r1_dynamic_capital_slippage_detail.csv",
        )
        self.replay_rule = str(self.backtest_config.get("replay_rule", "fixed_t2_close"))
        self.initial_cash = float(self.backtest_config.get("initial_cash", 500000))
        self.position_pct = float(self.backtest_config.get("position_pct", 0.8))
        self.skip_market_sentiment = str(self.backtest_config.get("skip_market_sentiment", "weak"))
        self.skip_segment_market_sentiment = str(
            self.backtest_config.get("skip_segment_market_sentiment", "neutral")
        )
        self.scenarios = list(self.backtest_config.get("scenarios", []))
        risk_config = self.config.get("risk", {})
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def backtest(self) -> dict[str, Path]:
        trades = self.load_base_trades()
        trades = self.attach_daily_liquidity(trades)
        detail_frames = []
        summary_rows = []
        yearly_rows = []

        for scenario in self.scenarios:
            scenario_name = str(scenario["scenario"])
            execution_mode = str(scenario.get("execution_mode", "compound_sequence"))
            if execution_mode == "single_position":
                simulated = self.simulate_single_position(trades, scenario)
            else:
                simulated = self.simulate_compound_sequence(trades, scenario)
            detail_frames.append(simulated)
            summary_rows.append(self.summarize_scenario(simulated, scenario))
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario_name))

        summary = pd.DataFrame(summary_rows)
        if not summary.empty:
            summary = summary.sort_values(
                ["final_equity", "max_drawdown", "executed_trade_count"],
                ascending=[False, True, False],
            )
        yearly = pd.DataFrame(yearly_rows)
        detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 动态复利滑点汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 动态复利滑点年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 动态复利滑点明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_base_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        trades = trades[trades["replay_rule"].astype(str) == self.replay_rule].copy()
        trades["is_a5_r1_dynamic_skip"] = (
            trades["market_sentiment_level"].astype(str).eq(self.skip_market_sentiment)
            & trades["segment_market_sentiment_level"].astype(str).eq(self.skip_segment_market_sentiment)
        )
        trades = trades[
            (~trades["is_a5_r1_dynamic_skip"])
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
            & trades["buy_price_before_slippage"].notna()
            & trades["exit_price_before_slippage"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 已成交交易: {self.input_trade_replay_path}")

        numeric_columns = [
            "buy_price_before_slippage",
            "exit_price_before_slippage",
            "daily_return",
            "net_return",
        ]
        for column in numeric_columns:
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
        for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
            trades[column] = trades[column].map(self.normalize_date)
        return trades.sort_values(["buy_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def attach_daily_liquidity(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "amount"],
            low_memory=False,
        )
        daily["trade_date"] = daily["trade_date"].map(self.normalize_date)
        daily["amount_yuan"] = pd.to_numeric(daily["amount"], errors="coerce") * 1000

        buy_daily = daily.rename(
            columns={
                "trade_date": "buy_trade_date",
                "amount_yuan": "buy_day_amount_yuan",
            }
        )[["buy_trade_date", "ts_code", "buy_day_amount_yuan"]]
        sell_daily = daily.rename(
            columns={
                "trade_date": "exit_trade_date",
                "amount_yuan": "sell_day_amount_yuan",
            }
        )[["exit_trade_date", "ts_code", "sell_day_amount_yuan"]]

        merged = trades.merge(buy_daily, on=["buy_trade_date", "ts_code"], how="left", validate="many_to_one")
        merged = merged.merge(sell_daily, on=["exit_trade_date", "ts_code"], how="left", validate="many_to_one")
        return merged

    def simulate_compound_sequence(self, trades: pd.DataFrame, scenario: dict[str, Any]) -> pd.DataFrame:
        scenario_name = str(scenario["scenario"])
        equity = self.initial_cash
        rows = []
        for trade_order, (_, row) in enumerate(trades.iterrows(), start=1):
            result = self.build_trade_result(row, scenario, equity_before=equity, trade_order=trade_order)
            equity = float(result["equity_after"])
            rows.append(result)
        return pd.DataFrame(rows).assign(scenario=scenario_name)

    def simulate_single_position(self, trades: pd.DataFrame, scenario: dict[str, Any]) -> pd.DataFrame:
        scenario_name = str(scenario["scenario"])
        equity = self.initial_cash
        occupied_until = ""
        rows = []
        trade_order = 0
        for _, row in trades.iterrows():
            buy_trade_date = str(row["buy_trade_date"])
            if occupied_until and buy_trade_date <= occupied_until:
                rows.append(self.build_skipped_result(row, scenario, equity, "position_occupied"))
                continue
            trade_order += 1
            result = self.build_trade_result(row, scenario, equity_before=equity, trade_order=trade_order)
            equity = float(result["equity_after"])
            occupied_until = str(row["exit_trade_date"])
            rows.append(result)
        return pd.DataFrame(rows).assign(scenario=scenario_name)

    def build_trade_result(
        self,
        row: pd.Series,
        scenario: dict[str, Any],
        equity_before: float,
        trade_order: int,
    ) -> dict[str, Any]:
        target_buy_amount = equity_before * self.position_pct
        buy_day_amount = float(row["buy_day_amount_yuan"]) if pd.notna(row["buy_day_amount_yuan"]) else 0.0
        max_buy_amount_ratio = scenario.get("max_buy_amount_ratio")
        if max_buy_amount_ratio is None:
            actual_buy_amount = target_buy_amount
        else:
            actual_buy_amount = min(target_buy_amount, buy_day_amount * float(max_buy_amount_ratio))
        actual_position_pct = actual_buy_amount / equity_before if equity_before > 0 else 0.0

        buy_amount_ratio = actual_buy_amount / buy_day_amount if buy_day_amount > 0 else 0.0
        buy_slippage = self.estimate_slippage_rate(buy_amount_ratio, scenario)
        buy_price = float(row["buy_price_before_slippage"]) * (1 + buy_slippage)

        gross_price_return_before_sell_slippage = float(row["exit_price_before_slippage"]) / buy_price - 1
        sell_value_before_slippage = actual_buy_amount * (1 + gross_price_return_before_sell_slippage)
        sell_day_amount = float(row["sell_day_amount_yuan"]) if pd.notna(row["sell_day_amount_yuan"]) else 0.0
        sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
        sell_slippage = self.estimate_slippage_rate(sell_amount_ratio, scenario)
        sell_price = float(row["exit_price_before_slippage"]) * (1 - sell_slippage)

        net_return = sell_price / buy_price - 1 - self.fee_rate_without_slippage
        account_return = net_return * actual_position_pct
        equity_after = equity_before * (1 + account_return)

        result = row.to_dict()
        result.update(
            {
                "execution_mode": str(scenario.get("execution_mode", "compound_sequence")),
                "scenario_description": str(scenario.get("description", "")),
                "trade_order": trade_order,
                "scenario_executed": True,
                "skip_reason": "",
                "equity_before": equity_before,
                "target_position_pct": self.position_pct,
                "target_buy_amount": target_buy_amount,
                "actual_buy_amount": actual_buy_amount,
                "actual_position_pct": actual_position_pct,
                "buy_amount_ratio": buy_amount_ratio,
                "buy_liquidity_bucket": self.classify_liquidity_bucket(buy_amount_ratio),
                "dynamic_buy_slippage_rate": buy_slippage,
                "dynamic_buy_price": buy_price,
                "sell_value_before_slippage": sell_value_before_slippage,
                "sell_amount_ratio": sell_amount_ratio,
                "sell_liquidity_bucket": self.classify_liquidity_bucket(sell_amount_ratio),
                "dynamic_sell_slippage_rate": sell_slippage,
                "dynamic_sell_price": sell_price,
                "dynamic_net_return": net_return,
                "dynamic_account_return": account_return,
                "equity_after": equity_after,
            }
        )
        return result

    def build_skipped_result(
        self,
        row: pd.Series,
        scenario: dict[str, Any],
        equity: float,
        skip_reason: str,
    ) -> dict[str, Any]:
        result = row.to_dict()
        result.update(
            {
                "execution_mode": str(scenario.get("execution_mode", "single_position")),
                "scenario_description": str(scenario.get("description", "")),
                "trade_order": pd.NA,
                "scenario_executed": False,
                "skip_reason": skip_reason,
                "equity_before": equity,
                "target_position_pct": self.position_pct,
                "target_buy_amount": 0.0,
                "actual_buy_amount": 0.0,
                "actual_position_pct": 0.0,
                "buy_amount_ratio": 0.0,
                "buy_liquidity_bucket": "skip",
                "dynamic_buy_slippage_rate": 0.0,
                "dynamic_buy_price": pd.NA,
                "sell_value_before_slippage": 0.0,
                "sell_amount_ratio": 0.0,
                "sell_liquidity_bucket": "skip",
                "dynamic_sell_slippage_rate": 0.0,
                "dynamic_sell_price": pd.NA,
                "dynamic_net_return": 0.0,
                "dynamic_account_return": 0.0,
                "equity_after": equity,
            }
        )
        return result

    def summarize_scenario(self, simulated: pd.DataFrame, scenario: dict[str, Any]) -> dict[str, Any]:
        scenario_name = str(scenario["scenario"])
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        returns = executed["dynamic_account_return"].dropna()
        equity_curve = executed["equity_after"] / self.initial_cash if len(executed) else pd.Series(dtype=float)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        yearly_returns = self.calculate_yearly_returns(executed)
        final_equity = float(executed["equity_after"].iloc[-1]) if len(executed) else self.initial_cash
        return {
            "scenario": scenario_name,
            "description": str(scenario.get("description", "")),
            "execution_mode": str(scenario.get("execution_mode", "compound_sequence")),
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "equity_multiple": final_equity / self.initial_cash if self.initial_cash else 0.0,
            "total_compound_return": final_equity / self.initial_cash - 1 if self.initial_cash else 0.0,
            "executed_trade_count": int(len(executed)),
            "skipped_trade_count": int((simulated["scenario_executed"] != True).sum()),  # noqa: E712
            "avg_actual_buy_amount": self.mean(executed["actual_buy_amount"]),
            "median_actual_buy_amount": self.median(executed["actual_buy_amount"]),
            "max_actual_buy_amount": self.max_value(executed["actual_buy_amount"]),
            "avg_actual_position_pct": self.mean(executed["actual_position_pct"]),
            "avg_buy_amount_ratio": self.mean(executed["buy_amount_ratio"]),
            "max_buy_amount_ratio": self.max_value(executed["buy_amount_ratio"]),
            "avg_sell_amount_ratio": self.mean(executed["sell_amount_ratio"]),
            "max_sell_amount_ratio": self.max_value(executed["sell_amount_ratio"]),
            "avg_buy_slippage": self.mean(executed["dynamic_buy_slippage_rate"]),
            "max_buy_slippage": self.max_value(executed["dynamic_buy_slippage_rate"]),
            "avg_sell_slippage": self.mean(executed["dynamic_sell_slippage_rate"]),
            "max_sell_slippage": self.max_value(executed["dynamic_sell_slippage_rate"]),
            "win_rate": self.win_rate(returns),
            "avg_account_return": self.mean(returns),
            "median_account_return": self.median(returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "year_count": len(yearly_returns),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "min_year_return": min(yearly_returns.values()) if yearly_returns else 0.0,
        }

    def build_yearly_rows(self, simulated: pd.DataFrame, scenario_name: str) -> list[dict[str, Any]]:
        rows = []
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
        for year, group in executed.groupby("year"):
            if not str(year).isdigit():
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            returns = group["dynamic_account_return"].dropna()
            equity_curve = group["equity_after"] / first_equity if first_equity else pd.Series(dtype=float)
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": year,
                    "sample_count": int(len(group)),
                    "first_equity": first_equity,
                    "last_equity": last_equity,
                    "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                    "avg_actual_buy_amount": self.mean(group["actual_buy_amount"]),
                    "max_actual_buy_amount": self.max_value(group["actual_buy_amount"]),
                    "avg_actual_position_pct": self.mean(group["actual_position_pct"]),
                    "avg_buy_slippage": self.mean(group["dynamic_buy_slippage_rate"]),
                    "avg_sell_slippage": self.mean(group["dynamic_sell_slippage_rate"]),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "max_loss": self.min_value(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        if executed.empty:
            return yearly_returns
        sample = executed.copy()
        sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
        for year, group in sample.groupby("year"):
            if not str(year).isdigit():
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            yearly_returns[str(year)] = last_equity / first_equity - 1 if first_equity else 0.0
        return yearly_returns

    @staticmethod
    def estimate_slippage_rate(amount_ratio: float, scenario: dict[str, Any]) -> float:
        if pd.isna(amount_ratio) or amount_ratio <= 0:
            return 0.0
        epsilon = 1e-12
        for tier in scenario.get("slippage_tiers", []):
            max_ratio = tier.get("max_amount_ratio")
            slippage_rate = float(tier.get("slippage_rate", 0.0))
            if max_ratio is None or amount_ratio <= float(max_ratio) + epsilon:
                return slippage_rate
        return 0.0

    @staticmethod
    def classify_liquidity_bucket(amount_ratio: float) -> str:
        if pd.isna(amount_ratio) or amount_ratio <= 0:
            return "skip"
        if amount_ratio <= 0.005:
            return "very_liquid"
        if amount_ratio <= 0.01:
            return "liquid"
        if amount_ratio <= 0.02:
            return "tradable"
        if amount_ratio <= 0.05:
            return "high_pressure"
        return "not_recommended"

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
