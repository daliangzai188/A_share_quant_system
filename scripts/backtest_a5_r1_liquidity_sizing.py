from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回测 A5-R1 1000万流动性降仓/拆单版本。")
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
    outputs = A5R1LiquiditySizingBacktester(config_path=args.config).backtest()
    print("A5-R1 1000万流动性降仓/拆单回测完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1LiquiditySizingBacktester:
    """按流动性档位调整实际买入金额，并重新计算滑点和账户收益。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_liquidity_sizing")
        self.sizing_config = self.config.get("a5_r1_liquidity_sizing_backtest", {})
        self.input_trade_replay_path = self.project_root / self.sizing_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.input_daily_merged_path = self.project_root / self.sizing_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.output_summary_path = self.project_root / self.sizing_config.get(
            "output_summary_path",
            "reports/a5_r1_liquidity_sizing_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.sizing_config.get(
            "output_yearly_path",
            "reports/a5_r1_liquidity_sizing_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.sizing_config.get(
            "output_detail_path",
            "reports/a5_r1_liquidity_sizing_detail.csv",
        )
        self.replay_rule = str(self.sizing_config.get("replay_rule", "fixed_t2_close"))
        self.base_buy_amount = float(self.sizing_config.get("base_buy_amount", 10000000))
        self.base_position_pct = float(self.sizing_config.get("base_position_pct", 0.8))
        self.skip_market_sentiment = str(self.sizing_config.get("skip_market_sentiment", "weak"))
        self.skip_segment_market_sentiment = str(
            self.sizing_config.get("skip_segment_market_sentiment", "neutral")
        )
        self.scenarios = list(self.sizing_config.get("scenarios", []))
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
        trades = self.attach_buy_day_liquidity(trades)
        detail_frames = []
        summary_rows = []
        yearly_rows = []
        for scenario in self.scenarios:
            scenario_name = str(scenario["scenario"])
            bucket_amounts = {str(key): float(value) for key, value in scenario["bucket_amounts"].items()}
            simulated = self.apply_sizing_scenario(trades, scenario_name, bucket_amounts)
            detail_frames.append(simulated)
            summary_rows.append(self.summarize_scenario(simulated, scenario_name, bucket_amounts))
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario_name))

        summary = pd.DataFrame(summary_rows).sort_values(
            ["total_compound_return", "max_drawdown", "executed_trade_count"],
            ascending=[False, True, False],
        )
        yearly = pd.DataFrame(yearly_rows)
        detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 流动性降仓汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 流动性降仓年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 流动性降仓明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
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
            & trades["exit_price"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 已成交交易: {self.input_trade_replay_path}")
        numeric_columns = ["buy_price_before_slippage", "exit_price", "daily_return", "net_return"]
        for column in numeric_columns:
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
        trades["buy_trade_date"] = trades["buy_trade_date"].map(self.normalize_date)
        trades["exit_trade_date"] = trades["exit_trade_date"].map(self.normalize_date)
        return trades.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def attach_buy_day_liquidity(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "amount"],
            low_memory=False,
        )
        daily = daily.rename(
            columns={
                "trade_date": "buy_trade_date",
                "amount": "buy_day_amount_thousand_yuan",
            }
        )
        merged = trades.merge(daily, on=["buy_trade_date", "ts_code"], how="left", validate="many_to_one")
        merged["buy_day_amount_yuan"] = pd.to_numeric(
            merged["buy_day_amount_thousand_yuan"],
            errors="coerce",
        ) * 1000
        merged["base_amount_ratio"] = self.base_buy_amount / merged["buy_day_amount_yuan"]
        merged["base_liquidity_bucket"] = merged["base_amount_ratio"].map(self.classify_liquidity_bucket)
        return merged

    def apply_sizing_scenario(
        self,
        trades: pd.DataFrame,
        scenario_name: str,
        bucket_amounts: dict[str, float],
    ) -> pd.DataFrame:
        result = trades.copy()
        result["scenario"] = scenario_name
        result["base_buy_amount"] = self.base_buy_amount
        result["scenario_buy_amount"] = result["base_liquidity_bucket"].map(bucket_amounts).fillna(0.0)
        result["scenario_position_pct"] = (
            self.base_position_pct * result["scenario_buy_amount"] / self.base_buy_amount
        )
        result["scenario_amount_ratio"] = result["scenario_buy_amount"] / result["buy_day_amount_yuan"]
        result["scenario_buy_slippage_rate"] = result["scenario_amount_ratio"].map(self.estimate_slippage_rate)
        result["scenario_liquidity_bucket"] = result["scenario_amount_ratio"].map(self.classify_liquidity_bucket)
        result["scenario_executed"] = result["scenario_buy_amount"] > 0
        adjusted_buy_price = result["buy_price_before_slippage"] * (1 + result["scenario_buy_slippage_rate"])
        adjusted_net_return = result["exit_price"] / adjusted_buy_price - 1 - self.fee_rate_without_slippage
        result["scenario_daily_return"] = 0.0
        result.loc[result["scenario_executed"], "scenario_daily_return"] = (
            adjusted_net_return[result["scenario_executed"]] * result.loc[result["scenario_executed"], "scenario_position_pct"]
        )
        result["base_daily_return"] = result["daily_return"]
        result["sizing_return_impact"] = result["scenario_daily_return"] - result["base_daily_return"]
        return result

    def summarize_scenario(
        self,
        simulated: pd.DataFrame,
        scenario_name: str,
        bucket_amounts: dict[str, float],
    ) -> dict[str, object]:
        executed = simulated[simulated["scenario_executed"]].copy()
        returns = executed["scenario_daily_return"].dropna()
        daily_returns = executed.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        yearly_returns = self.calculate_yearly_returns(executed)
        return {
            "scenario": scenario_name,
            "bucket_amounts": ";".join(f"{key}:{int(value)}" for key, value in sorted(bucket_amounts.items())),
            "executed_trade_count": int(len(executed)),
            "skipped_due_to_zero_amount_count": int((~simulated["scenario_executed"]).sum()),
            "avg_buy_amount": self.mean(executed["scenario_buy_amount"]),
            "median_buy_amount": self.median(executed["scenario_buy_amount"]),
            "avg_position_pct": self.mean(executed["scenario_position_pct"]),
            "avg_buy_slippage": self.mean(executed["scenario_buy_slippage_rate"]),
            "win_rate": self.win_rate(returns),
            "avg_daily_return": self.mean(returns),
            "median_daily_return": self.median(returns),
            "total_compound_return": self.compound_return(daily_returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "year_count": len(yearly_returns),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "min_year_return": min(yearly_returns.values()) if yearly_returns else 0.0,
        }

    def build_yearly_rows(self, simulated: pd.DataFrame, scenario_name: str) -> list[dict[str, object]]:
        rows = []
        executed = simulated[simulated["scenario_executed"]].copy()
        executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
        for year, group in executed.groupby("year"):
            if not str(year).isdigit():
                continue
            returns = group["scenario_daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
            equity_curve = (1 + daily_returns).cumprod()
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": year,
                    "sample_count": int(len(returns)),
                    "avg_buy_amount": self.mean(group["scenario_buy_amount"]),
                    "avg_position_pct": self.mean(group["scenario_position_pct"]),
                    "avg_buy_slippage": self.mean(group["scenario_buy_slippage_rate"]),
                    "year_return": self.compound_return(daily_returns),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "max_loss": self.min_value(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        sample = executed.copy()
        sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
        for year, group in sample.groupby("year"):
            if not str(year).isdigit():
                continue
            daily_returns = group.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return yearly_returns

    @staticmethod
    def estimate_slippage_rate(amount_ratio: float) -> float:
        if pd.isna(amount_ratio) or amount_ratio <= 0:
            return 0.0
        if amount_ratio <= 0.005:
            return 0.001
        if amount_ratio <= 0.01:
            return 0.002
        if amount_ratio <= 0.02:
            return 0.005
        if amount_ratio <= 0.05:
            return 0.01
        return 0.02

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

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
