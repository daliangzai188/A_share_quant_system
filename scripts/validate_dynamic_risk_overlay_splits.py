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
    parser = argparse.ArgumentParser(description="按训练集、测试集、样本外拆分验证动态风控策略。")
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
    outputs = DynamicRiskOverlaySplitValidator(config_path=args.config).validate()
    print("动态风控样本拆分验证完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class DynamicRiskOverlaySplitValidator:
    """验证动态风控在训练集、测试集、样本外的表现。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("dynamic_risk_overlay_split_validator")
        self.validation_config = self.config.get("dynamic_risk_overlay_split_validation", {})
        self.input_overlay_trade_path = self.project_root / self.validation_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.output_summary_path = self.project_root / self.validation_config.get(
            "output_summary_path",
            "reports/dynamic_risk_overlay_split_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.validation_config.get(
            "output_yearly_path",
            "reports/dynamic_risk_overlay_split_yearly.csv",
        )
        self.output_rolling_path = self.project_root / self.validation_config.get(
            "output_rolling_path",
            "reports/dynamic_risk_overlay_split_rolling.csv",
        )
        self.initial_cash = float(self.validation_config.get("initial_cash", 1000000))
        self.target_policies = [str(value) for value in self.validation_config.get("target_policies", [])]
        self.splits = list(self.validation_config.get("splits", []))
        self.rolling_windows = [int(value) for value in self.validation_config.get("rolling_windows", [2, 3])]

    def validate(self) -> dict[str, Path]:
        trades = self.load_trades()
        summary_rows = []
        yearly_rows = []
        rolling_rows = []
        for policy_name, policy_trades in trades.groupby("risk_policy"):
            summary_rows.append(self.build_summary_row(policy_name, "all", "全样本复核", policy_trades))
            yearly_rows.extend(self.build_yearly_rows(policy_name, "all", policy_trades))
            rolling_rows.extend(self.build_rolling_rows(policy_name, "all", policy_trades))
            for split in self.splits:
                split_name = str(split["split_name"])
                split_trades = self.filter_year_range(
                    trades=policy_trades,
                    start_year=str(split["start_year"]),
                    end_year=str(split["end_year"]),
                )
                summary_rows.append(
                    self.build_summary_row(
                        policy_name=policy_name,
                        split_name=split_name,
                        description=str(split.get("description", "")),
                        trades=split_trades,
                    )
                )
                yearly_rows.extend(self.build_yearly_rows(policy_name, split_name, split_trades))
                rolling_rows.extend(self.build_rolling_rows(policy_name, split_name, split_trades))

        summary = pd.DataFrame(summary_rows)
        yearly = pd.DataFrame(yearly_rows)
        rolling = pd.DataFrame(rolling_rows)
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        rolling.to_csv(self.output_rolling_path, index=False, encoding="utf-8-sig")
        self.logger.info("动态风控拆分汇总已生成: %s", self.output_summary_path)
        self.logger.info("动态风控拆分年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("动态风控滚动窗口报告已生成: %s", self.output_rolling_path)
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "rolling": self.output_rolling_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_overlay_trade_path,
            dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str, "risk_policy": str},
            low_memory=False,
        )
        if self.target_policies:
            trades = trades[trades["risk_policy"].astype(str).isin(self.target_policies)].copy()
        trades = trades[
            trades["exit_trade_date"].notna()
            & trades["adjusted_daily_return"].notna()
            & trades["risk_policy"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有可验证的动态风控样本: {self.input_overlay_trade_path}")
        trades["adjusted_daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        return trades

    @staticmethod
    def filter_year_range(trades: pd.DataFrame, start_year: str, end_year: str) -> pd.DataFrame:
        return trades[(trades["year"] >= start_year) & (trades["year"] <= end_year)].copy()

    def build_summary_row(
        self,
        policy_name: str,
        split_name: str,
        description: str,
        trades: pd.DataFrame,
    ) -> dict[str, object]:
        traded = trades[trades["overlay_action"].astype(str) == "trade"].copy()
        returns = traded["adjusted_daily_return"].dropna()
        daily_returns = traded.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(traded)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "risk_policy": policy_name,
            "split_name": split_name,
            "description": description,
            "start_year": min(yearly_returns) if yearly_returns else "",
            "end_year": max(yearly_returns) if yearly_returns else "",
            "signal_count": int(len(trades)),
            "traded_count": int(len(traded)),
            "skipped_count": int((trades["overlay_action"].astype(str) == "skip").sum()),
            "reduced_count": int((trades["applied_position_pct"] < 0.8).sum() - (trades["overlay_action"].astype(str) == "skip").sum()),
            "trade_days": int(traded["exit_trade_date"].nunique()) if len(traded) else 0,
            "year_count": int(len(yearly_returns)),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
            "median_daily_return": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": self.compound_return(daily_returns),
            "final_equity": self.initial_cash * (1 + self.compound_return(daily_returns)),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": float(returns.max()) if len(returns) else 0.0,
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def build_yearly_rows(self, policy_name: str, split_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        traded = trades[trades["overlay_action"].astype(str) == "trade"].copy()
        rows = []
        for year, group in traded.groupby("year"):
            returns = group["adjusted_daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
            rows.append(
                {
                    "risk_policy": policy_name,
                    "split_name": split_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_daily_return": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    def build_rolling_rows(self, policy_name: str, split_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        traded = trades[trades["overlay_action"].astype(str) == "trade"].copy()
        yearly_returns = self.calculate_yearly_returns(traded)
        years = sorted(yearly_returns)
        rows = []
        for window in self.rolling_windows:
            if len(years) < window:
                continue
            for index in range(0, len(years) - window + 1):
                selected_years = years[index : index + window]
                value = 1.0
                for year in selected_years:
                    value *= 1 + yearly_returns[year]
                rows.append(
                    {
                        "risk_policy": policy_name,
                        "split_name": split_name,
                        "window_years": int(window),
                        "start_year": selected_years[0],
                        "end_year": selected_years[-1],
                        "rolling_return": float(value - 1),
                    }
                )
        return rows

    def calculate_yearly_returns(self, trades: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in trades.groupby("year"):
            daily_returns = group.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return dict(sorted(yearly_returns.items()))

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
