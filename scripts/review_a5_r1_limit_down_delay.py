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
    parser = argparse.ArgumentParser(description="复盘 A5-R1 跌停阻塞和延迟卖出对收益的影响。")
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
    outputs = A5R1LimitDownDelayReviewer(config_path=args.config).review()
    print("A5-R1 跌停延迟卖出影响复盘完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1LimitDownDelayReviewer:
    """比较当前跌停延迟卖出和假设 T+2 强行收盘卖出两个口径。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_limit_down_delay_review")
        self.review_config = self.config.get("a5_r1_limit_down_delay_review", {})
        self.input_trade_replay_path = self.project_root / self.review_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.output_summary_path = self.project_root / self.review_config.get(
            "output_summary_path",
            "reports/a5_r1_limit_down_delay_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.review_config.get(
            "output_yearly_path",
            "reports/a5_r1_limit_down_delay_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.review_config.get(
            "output_detail_path",
            "reports/a5_r1_limit_down_delay_detail.csv",
        )
        self.replay_rule = str(self.review_config.get("replay_rule", "fixed_t2_close"))
        self.position_pct = float(self.review_config.get("position_pct", 0.8))
        self.skip_market_sentiment = str(self.review_config.get("skip_market_sentiment", "weak"))
        self.skip_segment_market_sentiment = str(
            self.review_config.get("skip_segment_market_sentiment", "neutral")
        )
        trade_replay_config = self.config.get("trade_replay", {})
        risk_config = self.config.get("risk", {})
        self.sell_slippage_rate = float(trade_replay_config.get("sell_slippage_rate", risk_config.get("slippage_rate", 0.001)))
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def review(self) -> dict[str, Path]:
        trades = self.load_a5_r1_executed_trades()
        trades = self.add_forced_t2_scenario(trades)
        summary = self.build_summary(trades)
        yearly = self.build_yearly_report(trades)
        detail = self.build_detail_report(trades)

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 跌停延迟卖出汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 跌停延迟卖出年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 跌停延迟卖出明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_a5_r1_executed_trades(self) -> pd.DataFrame:
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
            & trades["daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 已成交交易: {self.input_trade_replay_path}")
        numeric_columns = [
            "daily_return",
            "net_return",
            "buy_price",
            "d2_close",
            "exit_price",
            "limit_down_blocked_days",
        ]
        for column in numeric_columns:
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
        trades["limit_down_blocked_days"] = trades["limit_down_blocked_days"].fillna(0).astype(int)
        trades["exit_trade_date"] = trades["exit_trade_date"].map(self.normalize_date)
        trades["d2_trade_date"] = trades["d2_trade_date"].map(self.normalize_date)
        return trades.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def add_forced_t2_scenario(self, trades: pd.DataFrame) -> pd.DataFrame:
        result = trades.copy()
        result["current_daily_return"] = result["daily_return"]
        result["current_exit_trade_date"] = result["exit_trade_date"]
        forced_exit_price = result["d2_close"] * (1 - self.sell_slippage_rate)
        forced_net_return = forced_exit_price / result["buy_price"] - 1 - self.fee_rate_without_slippage
        forced_daily_return = forced_net_return * self.position_pct
        blocked_mask = result["limit_down_blocked_days"] > 0
        result["forced_t2_daily_return"] = result["current_daily_return"]
        result["forced_t2_exit_trade_date"] = result["current_exit_trade_date"]
        result.loc[blocked_mask, "forced_t2_daily_return"] = forced_daily_return[blocked_mask]
        result.loc[blocked_mask, "forced_t2_exit_trade_date"] = result.loc[blocked_mask, "d2_trade_date"]
        result["delay_impact_daily_return"] = result["current_daily_return"] - result["forced_t2_daily_return"]
        result["delay_helped"] = result["delay_impact_daily_return"] > 0
        result["delay_hurt"] = result["delay_impact_daily_return"] < 0
        result["forced_t2_is_unrealistic"] = blocked_mask
        return result

    def build_summary(self, trades: pd.DataFrame) -> pd.DataFrame:
        current_metrics = self.summarize_scenario(
            scenario="current_limit_down_delay",
            trades=trades,
            return_column="current_daily_return",
            date_column="current_exit_trade_date",
        )
        forced_metrics = self.summarize_scenario(
            scenario="unrealistic_force_t2_close",
            trades=trades,
            return_column="forced_t2_daily_return",
            date_column="forced_t2_exit_trade_date",
        )
        blocked = trades[trades["limit_down_blocked_days"] > 0].copy()
        comparison = {
            "scenario": "delay_impact_on_blocked_trades",
            "trade_count": int(len(blocked)),
            "blocked_day_total": int(blocked["limit_down_blocked_days"].sum()),
            "delay_helped_count": int(blocked["delay_helped"].sum()),
            "delay_hurt_count": int(blocked["delay_hurt"].sum()),
            "delay_flat_count": int((blocked["delay_impact_daily_return"] == 0).sum()),
            "delay_impact_sum": float(blocked["delay_impact_daily_return"].sum()),
            "delay_impact_mean": float(blocked["delay_impact_daily_return"].mean()) if len(blocked) else 0.0,
            "current_total_compound_return": current_metrics["total_compound_return"],
            "forced_t2_total_compound_return": forced_metrics["total_compound_return"],
            "compound_return_delta": current_metrics["total_compound_return"] - forced_metrics["total_compound_return"],
            "current_max_drawdown": current_metrics["max_drawdown"],
            "forced_t2_max_drawdown": forced_metrics["max_drawdown"],
            "max_drawdown_delta": current_metrics["max_drawdown"] - forced_metrics["max_drawdown"],
        }
        return pd.DataFrame([current_metrics, forced_metrics, comparison])

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for scenario, return_column, date_column in [
            ("current_limit_down_delay", "current_daily_return", "current_exit_trade_date"),
            ("unrealistic_force_t2_close", "forced_t2_daily_return", "forced_t2_exit_trade_date"),
        ]:
            sample = trades.copy()
            sample["scenario_year"] = sample[date_column].astype(str).str[:4]
            for year, group in sample.groupby("scenario_year"):
                if not str(year).isdigit():
                    continue
                row = self.summarize_scenario(
                    scenario=scenario,
                    trades=group,
                    return_column=return_column,
                    date_column=date_column,
                )
                row["year"] = year
                row["blocked_trade_count"] = int((group["limit_down_blocked_days"] > 0).sum())
                row["delay_impact_sum"] = float(group["delay_impact_daily_return"].sum())
                rows.append(row)
        return pd.DataFrame(rows).sort_values(["year", "scenario"])

    def build_detail_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        blocked = trades[trades["limit_down_blocked_days"] > 0].copy()
        columns = [
            "trade_date",
            "ts_code",
            "name",
            "market_segment",
            "limit_pct_bucket",
            "market_sentiment_level",
            "segment_market_sentiment_level",
            "limit_times_detail_bucket",
            "buy_trade_date",
            "buy_price",
            "d2_trade_date",
            "d2_close",
            "current_exit_trade_date",
            "exit_price",
            "limit_down_blocked_days",
            "current_daily_return",
            "forced_t2_daily_return",
            "delay_impact_daily_return",
            "delay_helped",
            "delay_hurt",
            "forced_t2_is_unrealistic",
        ]
        return blocked[[column for column in columns if column in blocked.columns]].sort_values(
            "delay_impact_daily_return"
        )

    def summarize_scenario(
        self,
        scenario: str,
        trades: pd.DataFrame,
        return_column: str,
        date_column: str,
    ) -> dict[str, object]:
        returns = trades[return_column].dropna()
        daily_returns = trades.groupby(date_column)[return_column].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "scenario": scenario,
            "trade_count": int(len(returns)),
            "blocked_trade_count": int((trades["limit_down_blocked_days"] > 0).sum()),
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
