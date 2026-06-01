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
    parser = argparse.ArgumentParser(description="复盘 A5-R1 收益优先版的分市场和涨停制度表现。")
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
    outputs = A5R1MarketStructureReviewer(config_path=args.config).review()
    print("A5-R1 分市场与涨停制度复盘完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1MarketStructureReviewer:
    """复盘 A5-R1 收益优先版在不同市场和涨停制度下的表现。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_market_structure_review")
        self.review_config = self.config.get("a5_r1_market_structure_review", {})
        self.input_overlay_trade_path = self.project_root / self.review_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.output_group_path = self.project_root / self.review_config.get(
            "output_group_path",
            "reports/a5_r1_market_structure_group.csv",
        )
        self.output_yearly_path = self.project_root / self.review_config.get(
            "output_yearly_path",
            "reports/a5_r1_market_structure_yearly.csv",
        )
        self.output_split_path = self.project_root / self.review_config.get(
            "output_split_path",
            "reports/a5_r1_market_structure_split.csv",
        )
        self.target_policy = str(self.review_config.get("target_policy", "weak_and_segment_neutral_skip"))
        self.min_group_samples = int(self.review_config.get("min_group_samples", 5))
        self.group_definitions = [list(group) for group in self.review_config.get("group_definitions", [])]
        self.splits = list(self.review_config.get("splits", []))

    def review(self) -> dict[str, Path]:
        trades = self.load_trades()
        group_report = self.build_group_report(trades)
        yearly_report = self.build_yearly_report(trades)
        split_report = self.build_split_report(trades)

        self.output_group_path.parent.mkdir(parents=True, exist_ok=True)
        group_report.to_csv(self.output_group_path, index=False, encoding="utf-8-sig")
        yearly_report.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        split_report.to_csv(self.output_split_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 市场结构分组报告已生成: %s, 行数: %s", self.output_group_path, len(group_report))
        self.logger.info("A5-R1 市场结构年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly_report))
        self.logger.info("A5-R1 市场结构拆分报告已生成: %s, 行数: %s", self.output_split_path, len(split_report))
        return {
            "group": self.output_group_path,
            "yearly": self.output_yearly_path,
            "split": self.output_split_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_overlay_trade_path,
            dtype={"trade_date": str, "exit_trade_date": str, "ts_code": str, "risk_policy": str},
            low_memory=False,
        )
        trades = trades[
            (trades["risk_policy"].astype(str) == self.target_policy)
            & (trades["overlay_action"].astype(str) == "trade")
            & trades["exit_trade_date"].notna()
            & trades["adjusted_daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 收益优先版交易样本: {self.input_overlay_trade_path}")
        trades["daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        for group_definition in self.group_definitions:
            for column in group_definition:
                if column not in trades.columns:
                    trades[column] = "missing"
                trades[column] = trades[column].fillna("missing").astype(str)
        return trades.sort_values(["exit_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def build_group_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = [self.summarize_group("all", "all", trades)]
        for group_definition in self.group_definitions:
            group_name = "+".join(group_definition)
            for values, group in trades.groupby(group_definition, dropna=False):
                if not isinstance(values, tuple):
                    values = (values,)
                group_value = "|".join(str(value) for value in values)
                if len(group) < self.min_group_samples:
                    continue
                rows.append(self.summarize_group(group_name, group_value, group))
        return pd.DataFrame(rows).sort_values(
            ["total_compound_return", "sample_count"],
            ascending=[False, False],
        )

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for group_definition in self.group_definitions:
            group_name = "+".join(group_definition)
            for values, group in trades.groupby(group_definition, dropna=False):
                if not isinstance(values, tuple):
                    values = (values,)
                group_value = "|".join(str(value) for value in values)
                for year, year_group in group.groupby("year"):
                    if len(year_group) < 1:
                        continue
                    row = self.summarize_group(group_name, group_value, year_group)
                    row["year"] = year
                    rows.append(row)
        report = pd.DataFrame(rows)
        if report.empty:
            return report
        return report.sort_values(["group_name", "group_value", "year"])

    def build_split_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for split in self.splits:
            split_sample = trades[
                (trades["year"] >= str(split["start_year"]))
                & (trades["year"] <= str(split["end_year"]))
            ].copy()
            if split_sample.empty:
                continue
            row = self.summarize_group("all", "all", split_sample)
            row["split_name"] = split["split_name"]
            row["start_year"] = split["start_year"]
            row["end_year"] = split["end_year"]
            rows.append(row)
            for group_definition in self.group_definitions:
                group_name = "+".join(group_definition)
                for values, group in split_sample.groupby(group_definition, dropna=False):
                    if len(group) < self.min_group_samples:
                        continue
                    if not isinstance(values, tuple):
                        values = (values,)
                    row = self.summarize_group(group_name, "|".join(str(value) for value in values), group)
                    row["split_name"] = split["split_name"]
                    row["start_year"] = split["start_year"]
                    row["end_year"] = split["end_year"]
                    rows.append(row)
        report = pd.DataFrame(rows)
        if report.empty:
            return report
        return report.sort_values(["split_name", "total_compound_return"], ascending=[True, False])

    def summarize_group(self, group_name: str, group_value: str, sample: pd.DataFrame) -> dict[str, object]:
        returns = sample["daily_return"].dropna()
        daily_returns = sample.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "group_name": group_name,
            "group_value": group_value,
            "sample_count": int(len(returns)),
            "trade_days": int(sample["exit_trade_date"].nunique()) if len(sample) else 0,
            "start_exit_trade_date": str(sample["exit_trade_date"].min()) if len(sample) else "",
            "end_exit_trade_date": str(sample["exit_trade_date"].max()) if len(sample) else "",
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
