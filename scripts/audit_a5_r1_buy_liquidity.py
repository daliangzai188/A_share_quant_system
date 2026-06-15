from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 1000 万单笔金额审计 A5-R1 买入流动性和滑点压力。")
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
    outputs = A5R1BuyLiquidityAuditor(config_path=args.config).audit()
    print("A5-R1 1000万买入流动性审计完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1BuyLiquidityAuditor:
    """用日线成交额估算 1000 万买入的流动性压力和滑点影响。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_buy_liquidity_audit")
        self.audit_config = self.config.get("a5_r1_buy_liquidity_audit", {})
        self.input_trade_replay_path = self.project_root / self.audit_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.input_daily_merged_path = self.project_root / self.audit_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.output_summary_path = self.project_root / self.audit_config.get(
            "output_summary_path",
            "reports/a5_r1_buy_liquidity_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.audit_config.get(
            "output_yearly_path",
            "reports/a5_r1_buy_liquidity_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.audit_config.get(
            "output_detail_path",
            "reports/a5_r1_buy_liquidity_detail.csv",
        )
        self.replay_rule = str(self.audit_config.get("replay_rule", "fixed_t2_close"))
        self.planned_buy_amount = float(self.audit_config.get("planned_buy_amount", 10000000))
        self.position_pct = float(self.audit_config.get("position_pct", 0.8))
        self.current_buy_slippage_rate = float(self.audit_config.get("current_buy_slippage_rate", 0.001))
        self.skip_market_sentiment = str(self.audit_config.get("skip_market_sentiment", "weak"))
        self.skip_segment_market_sentiment = str(
            self.audit_config.get("skip_segment_market_sentiment", "neutral")
        )
        risk_config = self.config.get("risk", {})
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def audit(self) -> dict[str, Path]:
        trades = self.load_a5_r1_rows()
        trades = self.attach_buy_day_liquidity(trades)
        trades = self.add_liquidity_metrics(trades)
        summary = self.build_summary(trades)
        yearly = self.build_yearly_report(trades)
        detail = self.build_detail_report(trades)

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 1000万买入流动性汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 1000万买入流动性年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 1000万买入流动性明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_a5_r1_rows(self) -> pd.DataFrame:
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
        trades = trades[~trades["is_a5_r1_dynamic_skip"]].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 计划交易样本: {self.input_trade_replay_path}")
        numeric_columns = [
            "net_return",
            "daily_return",
            "buy_price_before_slippage",
            "buy_price",
            "exit_price",
            "available_fill_amount",
            "fill_probability",
        ]
        for column in numeric_columns:
            if column in trades.columns:
                trades[column] = pd.to_numeric(trades[column], errors="coerce")
        trades["buy_trade_date"] = trades["buy_trade_date"].map(self.normalize_date)
        trades["exit_trade_date"] = trades["exit_trade_date"].map(self.normalize_date)
        return trades.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def attach_buy_day_liquidity(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "amount", "vol", "turnover_rate"],
            low_memory=False,
        )
        daily = daily.rename(
            columns={
                "trade_date": "buy_trade_date",
                "amount": "buy_day_amount_thousand_yuan",
                "vol": "buy_day_vol",
                "turnover_rate": "buy_day_turnover_rate",
            }
        )
        return trades.merge(
            daily,
            on=["buy_trade_date", "ts_code"],
            how="left",
            validate="many_to_one",
        )

    def add_liquidity_metrics(self, trades: pd.DataFrame) -> pd.DataFrame:
        result = trades.copy()
        result["planned_buy_amount_yuan"] = self.planned_buy_amount
        result["buy_day_amount_yuan"] = pd.to_numeric(
            result["buy_day_amount_thousand_yuan"],
            errors="coerce",
        ) * 1000
        result["planned_amount_to_buy_day_amount"] = (
            result["planned_buy_amount_yuan"] / result["buy_day_amount_yuan"]
        )
        result["estimated_buy_slippage_rate_10m"] = result["planned_amount_to_buy_day_amount"].map(
            self.estimate_slippage_rate
        )
        result["liquidity_bucket_10m"] = result["planned_amount_to_buy_day_amount"].map(self.classify_liquidity_bucket)
        result["buy_liquidity_pass_10m"] = result["liquidity_bucket_10m"].isin(
            {"very_liquid", "liquid", "tradable"}
        )
        result["signal_board_fill_probability_10m"] = (
            pd.to_numeric(result.get("available_fill_amount"), errors="coerce") / self.planned_buy_amount
        ).clip(upper=1)
        result["signal_board_fill_probability_10m"] = result["signal_board_fill_probability_10m"].fillna(0)
        result["signal_board_fill_pass_10m"] = result["signal_board_fill_probability_10m"] >= 0.6

        executed_mask = (
            (result["buy_executed"] == True)  # noqa: E712
            & (result["sell_executed"] == True)  # noqa: E712
            & result["buy_price_before_slippage"].notna()
            & result["exit_price"].notna()
        )
        result["current_daily_return"] = 0.0
        result.loc[executed_mask, "current_daily_return"] = result.loc[executed_mask, "daily_return"]
        adjusted_buy_price = result["buy_price_before_slippage"] * (1 + result["estimated_buy_slippage_rate_10m"])
        adjusted_net_return = result["exit_price"] / adjusted_buy_price - 1 - self.fee_rate_without_slippage
        result["liquidity_adjusted_daily_return_10m"] = 0.0
        result.loc[executed_mask, "liquidity_adjusted_daily_return_10m"] = (
            adjusted_net_return[executed_mask] * self.position_pct
        )
        result["slippage_return_impact"] = (
            result["liquidity_adjusted_daily_return_10m"] - result["current_daily_return"]
        )
        result["execution_state"] = "executed"
        result.loc[result["buy_executed"] != True, "execution_state"] = "buy_rejected"  # noqa: E712
        result.loc[
            (result["buy_executed"] == True) & (result["sell_executed"] != True),  # noqa: E712
            "execution_state",
        ] = "sell_unresolved"
        return result

    def build_summary(self, trades: pd.DataFrame) -> pd.DataFrame:
        executed = trades[trades["execution_state"].eq("executed")].copy()
        current = self.summarize_scenario(executed, "current_fixed_0_1pct_buy_slippage", "current_daily_return")
        adjusted = self.summarize_scenario(
            executed,
            "liquidity_adjusted_10m_buy_slippage",
            "liquidity_adjusted_daily_return_10m",
        )
        audit = {
            "scenario": "buy_liquidity_audit_10m",
            "planned_signal_count": int(len(trades)),
            "executed_trade_count": int(len(executed)),
            "buy_rejected_count": int((trades["execution_state"] == "buy_rejected").sum()),
            "liquidity_pass_count": int(executed["buy_liquidity_pass_10m"].sum()),
            "liquidity_pressure_count": int((~executed["buy_liquidity_pass_10m"]).sum()),
            "signal_board_fill_pass_count": int(executed["signal_board_fill_pass_10m"].sum()),
            "avg_amount_ratio": self.mean(executed["planned_amount_to_buy_day_amount"]),
            "median_amount_ratio": self.median(executed["planned_amount_to_buy_day_amount"]),
            "max_amount_ratio": self.max_value(executed["planned_amount_to_buy_day_amount"]),
            "avg_estimated_buy_slippage": self.mean(executed["estimated_buy_slippage_rate_10m"]),
            "median_estimated_buy_slippage": self.median(executed["estimated_buy_slippage_rate_10m"]),
            "max_estimated_buy_slippage": self.max_value(executed["estimated_buy_slippage_rate_10m"]),
            "current_total_compound_return": current["total_compound_return"],
            "liquidity_adjusted_total_compound_return": adjusted["total_compound_return"],
            "compound_return_delta": adjusted["total_compound_return"] - current["total_compound_return"],
            "current_max_drawdown": current["max_drawdown"],
            "liquidity_adjusted_max_drawdown": adjusted["max_drawdown"],
            "max_drawdown_delta": adjusted["max_drawdown"] - current["max_drawdown"],
        }
        return pd.DataFrame([current, adjusted, audit])

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        executed = trades[trades["execution_state"].eq("executed")].copy()
        executed["exit_year"] = executed["exit_trade_date"].astype(str).str[:4]
        rows = []
        for scenario, return_column in [
            ("current_fixed_0_1pct_buy_slippage", "current_daily_return"),
            ("liquidity_adjusted_10m_buy_slippage", "liquidity_adjusted_daily_return_10m"),
        ]:
            for year, group in executed.groupby("exit_year"):
                if not str(year).isdigit():
                    continue
                row = self.summarize_scenario(group, scenario, return_column)
                row["year"] = year
                row["liquidity_pressure_count"] = int((~group["buy_liquidity_pass_10m"]).sum())
                row["avg_estimated_buy_slippage"] = self.mean(group["estimated_buy_slippage_rate_10m"])
                rows.append(row)
        return pd.DataFrame(rows).sort_values(["year", "scenario"])

    def build_detail_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "trade_date",
            "ts_code",
            "name",
            "execution_state",
            "buy_trade_date",
            "buy_reject_reason",
            "market_segment",
            "limit_pct_bucket",
            "market_sentiment_level",
            "segment_market_sentiment_level",
            "limit_times_detail_bucket",
            "buy_price_before_slippage",
            "buy_day_amount_yuan",
            "planned_buy_amount_yuan",
            "planned_amount_to_buy_day_amount",
            "liquidity_bucket_10m",
            "buy_liquidity_pass_10m",
            "estimated_buy_slippage_rate_10m",
            "signal_board_fill_probability_10m",
            "signal_board_fill_pass_10m",
            "current_daily_return",
            "liquidity_adjusted_daily_return_10m",
            "slippage_return_impact",
            "exit_trade_date",
        ]
        return trades[[column for column in columns if column in trades.columns]].sort_values(
            ["execution_state", "planned_amount_to_buy_day_amount"],
            ascending=[True, False],
        )

    def summarize_scenario(self, trades: pd.DataFrame, scenario: str, return_column: str) -> dict[str, object]:
        returns = trades[return_column].dropna()
        daily_returns = trades.groupby("exit_trade_date")[return_column].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "scenario": scenario,
            "trade_count": int(len(returns)),
            "win_rate": self.win_rate(returns),
            "avg_daily_return": self.mean(returns),
            "median_daily_return": self.median(returns),
            "total_compound_return": self.compound_return(daily_returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    @staticmethod
    def estimate_slippage_rate(amount_ratio: float) -> float:
        if pd.isna(amount_ratio):
            return 0.02
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
        if pd.isna(amount_ratio):
            return "missing"
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
