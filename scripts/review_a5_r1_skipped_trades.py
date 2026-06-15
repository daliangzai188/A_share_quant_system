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
    parser = argparse.ArgumentParser(description="复盘 A5-R1 动态风控跳过的交易。")
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
    outputs = A5R1SkippedTradeReviewer(config_path=args.config).review()
    print("A5-R1 跳过交易复盘完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1SkippedTradeReviewer:
    """复盘动态风控跳过交易是否确实改善 A5。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_skipped_trade_review")
        self.review_config = self.config.get("a5_r1_skipped_trade_review", {})
        self.input_overlay_trade_path = self.project_root / self.review_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.output_summary_path = self.project_root / self.review_config.get(
            "output_summary_path",
            "reports/a5_r1_skipped_trade_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.review_config.get(
            "output_yearly_path",
            "reports/a5_r1_skipped_trade_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.review_config.get(
            "output_detail_path",
            "reports/a5_r1_skipped_trade_detail.csv",
        )
        self.target_policy = str(self.review_config.get("target_policy", "weak_and_segment_neutral_skip"))
        self.base_policy = str(self.review_config.get("base_policy", "base_no_overlay"))
        self.base_position_pct = float(self.review_config.get("base_position_pct", 0.8))

    def review(self) -> dict[str, Path]:
        trades = self.load_policy_trades()
        skipped = trades[trades["overlay_action"].astype(str) == "skip"].copy()
        traded = trades[trades["overlay_action"].astype(str) == "trade"].copy()

        summary = self.build_summary(trades=trades, skipped=skipped, traded=traded)
        yearly = self.build_yearly(skipped=skipped, traded=traded)
        detail = self.build_detail(skipped=skipped)

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 跳过交易汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 跳过交易年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 跳过交易明细已生成: %s", self.output_detail_path)
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_policy_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_overlay_trade_path,
            dtype={"trade_date": str, "exit_trade_date": str, "ts_code": str, "risk_policy": str},
            low_memory=False,
        )
        trades = trades[trades["risk_policy"].astype(str) == self.target_policy].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到目标风控策略样本: {self.target_policy}")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        if "raw_daily_return" in trades.columns:
            trades["a5_original_daily_return"] = pd.to_numeric(trades["raw_daily_return"], errors="coerce")
        else:
            trades["a5_original_daily_return"] = trades["net_return"] * self.base_position_pct
        trades["adjusted_daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["exit_year"] = trades["exit_trade_date"].astype(str).str[:4]
        trades["exit_month"] = trades["exit_trade_date"].astype(str).str[:6]
        return trades

    def build_summary(self, trades: pd.DataFrame, skipped: pd.DataFrame, traded: pd.DataFrame) -> pd.DataFrame:
        rows = [
            self.summarize_sample("target_policy_all_signals", "A5-R1 全部信号", trades, "adjusted_daily_return"),
            self.summarize_sample("target_policy_traded", "A5-R1 实际交易", traded, "adjusted_daily_return"),
            self.summarize_sample("target_policy_skipped_as_a5", "A5-R1 跳过交易按 A5 原收益测算", skipped, "a5_original_daily_return"),
        ]
        skipped_returns = skipped["a5_original_daily_return"].dropna()
        skipped_daily_returns = (
            skipped.groupby("exit_trade_date")["a5_original_daily_return"].sum().sort_index()
            if not skipped.empty
            else pd.Series(dtype=float)
        )
        rows.append(
            {
                "sample_name": "skipped_trade_contribution",
                "description": "跳过交易对 A5-R1 的直接贡献估算",
                "sample_count": int(len(skipped_returns)),
                "win_rate": float((skipped_returns > 0).mean()) if len(skipped_returns) else 0.0,
                "avg_return": float(skipped_returns.mean()) if len(skipped_returns) else 0.0,
                "median_return": float(skipped_returns.median()) if len(skipped_returns) else 0.0,
                "compound_return": self.compound_return(skipped_daily_returns),
                "sum_return": float(skipped_returns.sum()) if len(skipped_returns) else 0.0,
                "avoided_loss_sum": float(skipped_returns[skipped_returns < 0].abs().sum()) if len(skipped_returns) else 0.0,
                "missed_profit_sum": float(skipped_returns[skipped_returns > 0].sum()) if len(skipped_returns) else 0.0,
                "net_avoided_return_sum": float(-skipped_returns.sum()) if len(skipped_returns) else 0.0,
                "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown((1 + skipped_daily_returns).cumprod()),
                "max_loss": float(skipped_returns.min()) if len(skipped_returns) else 0.0,
                "max_profit": float(skipped_returns.max()) if len(skipped_returns) else 0.0,
                "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(skipped_returns)),
            }
        )
        return pd.DataFrame(rows)

    def summarize_sample(
        self,
        sample_name: str,
        description: str,
        sample: pd.DataFrame,
        return_column: str,
    ) -> dict[str, object]:
        returns = sample[return_column].dropna() if not sample.empty else pd.Series(dtype=float)
        daily_returns = (
            sample.groupby("exit_trade_date")[return_column].sum().sort_index()
            if not sample.empty
            else pd.Series(dtype=float)
        )
        return {
            "sample_name": sample_name,
            "description": description,
            "sample_count": int(len(returns)),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return": float(returns.mean()) if len(returns) else 0.0,
            "median_return": float(returns.median()) if len(returns) else 0.0,
            "compound_return": self.compound_return(daily_returns),
            "sum_return": float(returns.sum()) if len(returns) else 0.0,
            "avoided_loss_sum": 0.0,
            "missed_profit_sum": 0.0,
            "net_avoided_return_sum": 0.0,
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown((1 + daily_returns).cumprod()),
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "max_profit": float(returns.max()) if len(returns) else 0.0,
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
        }

    def build_yearly(self, skipped: pd.DataFrame, traded: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for group_name, sample, return_column in [
            ("skipped_as_a5", skipped, "a5_original_daily_return"),
            ("traded_as_r1", traded, "adjusted_daily_return"),
        ]:
            for year, group in sample.groupby("exit_year"):
                returns = group[return_column].dropna()
                daily_returns = group.groupby("exit_trade_date")[return_column].sum().sort_index()
                rows.append(
                    {
                        "group_name": group_name,
                        "year": str(year),
                        "sample_count": int(len(returns)),
                        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                        "avg_return": float(returns.mean()) if len(returns) else 0.0,
                        "median_return": float(returns.median()) if len(returns) else 0.0,
                        "compound_return": self.compound_return(daily_returns),
                        "sum_return": float(returns.sum()) if len(returns) else 0.0,
                        "loss_sum": float(returns[returns < 0].sum()) if len(returns) else 0.0,
                        "profit_sum": float(returns[returns > 0].sum()) if len(returns) else 0.0,
                        "max_loss": float(returns.min()) if len(returns) else 0.0,
                        "max_profit": float(returns.max()) if len(returns) else 0.0,
                    }
                )
        return pd.DataFrame(rows).sort_values(["group_name", "year"])

    @staticmethod
    def build_detail(skipped: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "trade_date",
            "exit_trade_date",
            "ts_code",
            "name",
            "a5_original_daily_return",
            "net_return",
            "market_sentiment_level",
            "segment_market_sentiment_level",
            "market_chain_count_bucket",
            "segment_limit_up_ratio_bucket",
            "segment_limit_up_count_bucket",
            "market_segment",
            "limit_pct_bucket",
            "limit_times",
            "first_time_detail_bucket",
            "open_times",
            "turnover_rate_bucket",
            "fd_ratio_bucket",
            "limit_down_blocked_days",
        ]
        available = [column for column in columns if column in skipped.columns]
        return skipped.sort_values("a5_original_daily_return")[available]

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
