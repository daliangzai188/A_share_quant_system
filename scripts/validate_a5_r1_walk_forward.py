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
    parser = argparse.ArgumentParser(description="滚动验证 A5-R1 动态风控开关。")
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
    outputs = A5R1WalkForwardValidator(config_path=args.config).validate()
    print("A5-R1 walk-forward 验证完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1WalkForwardValidator:
    """用滚动训练/测试窗口验证 A5-R1 风控开关稳定性。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_walk_forward")
        self.validation_config = self.config.get("a5_r1_walk_forward_validation", {})
        self.input_overlay_trade_path = self.project_root / self.validation_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.output_summary_path = self.project_root / self.validation_config.get(
            "output_summary_path",
            "reports/a5_r1_walk_forward_summary.csv",
        )
        self.output_comparison_path = self.project_root / self.validation_config.get(
            "output_comparison_path",
            "reports/a5_r1_walk_forward_comparison.csv",
        )
        self.output_yearly_path = self.project_root / self.validation_config.get(
            "output_yearly_path",
            "reports/a5_r1_walk_forward_yearly.csv",
        )
        self.initial_cash = float(self.validation_config.get("initial_cash", 1000000))
        self.base_policy = str(self.validation_config.get("base_policy", "base_no_overlay"))
        self.target_policy = str(self.validation_config.get("target_policy", "weak_and_segment_neutral_skip"))
        self.windows = list(self.validation_config.get("windows", []))

    def validate(self) -> dict[str, Path]:
        trades = self.load_trades()
        summary_rows = []
        yearly_rows = []
        comparison_rows = []
        for window in self.windows:
            window_name = str(window["window_name"])
            self.logger.info("开始 A5-R1 walk-forward 窗口: %s", window_name)
            for phase, start_key, end_key in [
                ("train", "train_start_year", "train_end_year"),
                ("test_oos", "test_start_year", "test_end_year"),
            ]:
                phase_rows = []
                for policy_name in [self.base_policy, self.target_policy]:
                    sample = self.filter_policy_year_range(
                        trades=trades,
                        policy_name=policy_name,
                        start_year=str(window[start_key]),
                        end_year=str(window[end_key]),
                    )
                    summary = self.build_summary_row(window_name, phase, policy_name, sample)
                    summary_rows.append(summary)
                    phase_rows.append(summary)
                    yearly_rows.extend(self.build_yearly_rows(window_name, phase, policy_name, sample))
                comparison_rows.append(self.build_comparison_row(window_name, phase, phase_rows))

        summary = pd.DataFrame(summary_rows)
        comparison = pd.DataFrame(comparison_rows)
        yearly = pd.DataFrame(yearly_rows)
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        comparison.to_csv(self.output_comparison_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 walk-forward 汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 walk-forward 对比已生成: %s", self.output_comparison_path)
        self.logger.info("A5-R1 walk-forward 年度已生成: %s", self.output_yearly_path)
        return {
            "summary": self.output_summary_path,
            "comparison": self.output_comparison_path,
            "yearly": self.output_yearly_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_overlay_trade_path,
            dtype={"trade_date": str, "exit_trade_date": str, "ts_code": str, "risk_policy": str},
            low_memory=False,
        )
        trades = trades[trades["risk_policy"].astype(str).isin({self.base_policy, self.target_policy})].copy()
        trades = trades[
            trades["risk_policy"].notna()
            & trades["exit_trade_date"].notna()
            & trades["adjusted_daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有可验证的 A5-R1 样本: {self.input_overlay_trade_path}")
        trades["adjusted_daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        return trades

    def filter_policy_year_range(
        self,
        trades: pd.DataFrame,
        policy_name: str,
        start_year: str,
        end_year: str,
    ) -> pd.DataFrame:
        return trades[
            (trades["risk_policy"].astype(str) == policy_name)
            & (trades["year"] >= start_year)
            & (trades["year"] <= end_year)
        ].copy()

    def build_summary_row(
        self,
        window_name: str,
        phase: str,
        policy_name: str,
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
            "window_name": window_name,
            "phase": phase,
            "risk_policy": policy_name,
            "start_year": min(yearly_returns) if yearly_returns else "",
            "end_year": max(yearly_returns) if yearly_returns else "",
            "signal_count": int(len(trades)),
            "traded_count": int(len(traded)),
            "skipped_count": int((trades["overlay_action"].astype(str) == "skip").sum()),
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

    def build_comparison_row(
        self,
        window_name: str,
        phase: str,
        phase_rows: list[dict[str, object]],
    ) -> dict[str, object]:
        rows_by_policy = {str(row["risk_policy"]): row for row in phase_rows}
        base = rows_by_policy[self.base_policy]
        target = rows_by_policy[self.target_policy]
        return {
            "window_name": window_name,
            "phase": phase,
            "base_policy": self.base_policy,
            "target_policy": self.target_policy,
            "base_return": float(base["total_compound_return"]),
            "target_return": float(target["total_compound_return"]),
            "return_delta": float(target["total_compound_return"]) - float(base["total_compound_return"]),
            "base_max_drawdown": float(base["max_drawdown"]),
            "target_max_drawdown": float(target["max_drawdown"]),
            "drawdown_delta": float(target["max_drawdown"]) - float(base["max_drawdown"]),
            "base_win_rate": float(base["win_rate"]),
            "target_win_rate": float(target["win_rate"]),
            "win_rate_delta": float(target["win_rate"]) - float(base["win_rate"]),
            "base_traded_count": int(base["traded_count"]),
            "target_traded_count": int(target["traded_count"]),
            "target_skipped_count": int(target["skipped_count"]),
            "base_max_consecutive_losses": int(base["max_consecutive_losses"]),
            "target_max_consecutive_losses": int(target["max_consecutive_losses"]),
            "return_improved": bool(float(target["total_compound_return"]) > float(base["total_compound_return"])),
            "drawdown_improved": bool(float(target["max_drawdown"]) < float(base["max_drawdown"])),
        }

    def build_yearly_rows(
        self,
        window_name: str,
        phase: str,
        policy_name: str,
        trades: pd.DataFrame,
    ) -> list[dict[str, object]]:
        traded = trades[trades["overlay_action"].astype(str) == "trade"].copy()
        rows = []
        for year, group in traded.groupby("year"):
            returns = group["adjusted_daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
            rows.append(
                {
                    "window_name": window_name,
                    "phase": phase,
                    "risk_policy": policy_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_daily_return": float(returns.median()) if len(returns) else 0.0,
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
