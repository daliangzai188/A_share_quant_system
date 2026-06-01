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
    parser = argparse.ArgumentParser(description="复盘 A5-R1 收益优先版沪主板高回撤交易。")
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
    outputs = A5R1ShMainFailureReviewer(config_path=args.config).review()
    print("A5-R1 沪主板高回撤复盘完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1ShMainFailureReviewer:
    """复盘 A5-R1 收益优先版里沪主板交易的回撤和亏损来源。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_sh_main_failure_review")
        self.review_config = self.config.get("a5_r1_sh_main_failure_review", {})
        self.input_overlay_trade_path = self.project_root / self.review_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.output_period_path = self.project_root / self.review_config.get(
            "output_period_path",
            "reports/a5_r1_sh_main_failure_periods.csv",
        )
        self.output_monthly_path = self.project_root / self.review_config.get(
            "output_monthly_path",
            "reports/a5_r1_sh_main_failure_monthly.csv",
        )
        self.output_focus_group_path = self.project_root / self.review_config.get(
            "output_focus_group_path",
            "reports/a5_r1_sh_main_failure_focus_group.csv",
        )
        self.output_worst_trades_path = self.project_root / self.review_config.get(
            "output_worst_trades_path",
            "reports/a5_r1_sh_main_failure_worst_trades.csv",
        )
        self.target_policy = str(self.review_config.get("target_policy", "weak_and_segment_neutral_skip"))
        self.target_market_segment = str(self.review_config.get("target_market_segment", "sh_main"))
        self.min_group_samples = int(self.review_config.get("min_group_samples", 3))
        self.worst_trade_count = int(self.review_config.get("worst_trade_count", 40))
        self.factor_columns = list(
            self.review_config.get(
                "factor_columns",
                self.config.get("failure_attribution", {}).get("factor_columns", []),
            )
        )

    def review(self) -> dict[str, Path]:
        trades = self.load_trades()
        trades = self.annotate_focus_periods(trades)
        period_report = self.build_period_report(trades)
        monthly_report = self.build_monthly_report(trades)
        focus_group_report = self.build_focus_group_report(trades)
        worst_trades = self.build_worst_trades_report(trades)

        self.output_period_path.parent.mkdir(parents=True, exist_ok=True)
        period_report.to_csv(self.output_period_path, index=False, encoding="utf-8-sig")
        monthly_report.to_csv(self.output_monthly_path, index=False, encoding="utf-8-sig")
        focus_group_report.to_csv(self.output_focus_group_path, index=False, encoding="utf-8-sig")
        worst_trades.to_csv(self.output_worst_trades_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 沪主板重点区间报告已生成: %s, 行数: %s", self.output_period_path, len(period_report))
        self.logger.info("A5-R1 沪主板月度报告已生成: %s, 行数: %s", self.output_monthly_path, len(monthly_report))
        self.logger.info("A5-R1 沪主板重点区间分组报告已生成: %s, 行数: %s", self.output_focus_group_path, len(focus_group_report))
        self.logger.info("A5-R1 沪主板最差交易报告已生成: %s, 行数: %s", self.output_worst_trades_path, len(worst_trades))
        return {
            "period": self.output_period_path,
            "monthly": self.output_monthly_path,
            "focus_group": self.output_focus_group_path,
            "worst_trades": self.output_worst_trades_path,
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
            & (trades["market_segment"].astype(str) == self.target_market_segment)
            & trades["exit_trade_date"].notna()
            & trades["adjusted_daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 沪主板交易样本: {self.input_overlay_trade_path}")
        trades["daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        trades["month"] = trades["exit_trade_date"].astype(str).str[:6]
        trades["loss_amount_proxy"] = trades["daily_return"].clip(upper=0).abs()
        trades = trades.sort_values(["exit_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)
        trades["trade_sequence"] = range(1, len(trades) + 1)
        for column in self.factor_columns:
            if column not in trades.columns:
                trades[column] = "missing"
            trades[column] = trades[column].fillna("missing").astype(str)
        return trades

    def annotate_focus_periods(self, trades: pd.DataFrame) -> pd.DataFrame:
        result = trades.copy()
        result["in_max_drawdown_period"] = False
        result["in_longest_loss_streak"] = False
        drawdown_period = self.find_max_drawdown_period(result)
        if drawdown_period:
            start_date, end_date = drawdown_period
            result["in_max_drawdown_period"] = result["exit_trade_date"].astype(str).between(start_date, end_date)
        loss_streak_period = self.find_longest_loss_streak(result)
        if loss_streak_period:
            start_sequence, end_sequence = loss_streak_period
            result["in_longest_loss_streak"] = result["trade_sequence"].between(start_sequence, end_sequence)
        return result

    @staticmethod
    def find_max_drawdown_period(trades: pd.DataFrame) -> tuple[str, str] | None:
        daily_returns = trades.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        if daily_returns.empty:
            return None
        equity_curve = (1 + daily_returns).cumprod()
        drawdown = equity_curve / equity_curve.cummax() - 1
        trough_date = str(drawdown.idxmin())
        peak_date = str(equity_curve.loc[:trough_date].idxmax())
        return peak_date, trough_date

    @staticmethod
    def find_longest_loss_streak(trades: pd.DataFrame) -> tuple[int, int] | None:
        best_start = 0
        best_end = 0
        current_start = 0
        current_length = 0
        best_length = 0
        for row in trades.itertuples(index=False):
            is_loss = float(row.daily_return) <= 0
            if is_loss:
                if current_length == 0:
                    current_start = int(row.trade_sequence)
                current_length += 1
                if current_length > best_length:
                    best_length = current_length
                    best_start = current_start
                    best_end = int(row.trade_sequence)
            else:
                current_length = 0
        if best_length == 0:
            return None
        return best_start, best_end

    def build_period_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = [
            self.summarize_period("all_sh_main_trades", "A5-R1 沪主板全部交易", trades),
            self.summarize_period(
                "max_drawdown_period",
                "沪主板自动识别的最大回撤区间",
                trades[trades["in_max_drawdown_period"]].copy(),
            ),
            self.summarize_period(
                "longest_loss_streak",
                "沪主板自动识别的最长连续亏损交易段",
                trades[trades["in_longest_loss_streak"]].copy(),
            ),
        ]
        return pd.DataFrame(rows)

    def build_monthly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for month, group in trades.groupby("month"):
            rows.append(self.summarize_period(str(month), "沪主板月度表现", group))
        return pd.DataFrame(rows).sort_values(["compound_return", "loss_contribution"], ascending=[True, False])

    def build_focus_group_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        focus_definitions = {
            "max_drawdown_period": trades["in_max_drawdown_period"],
            "longest_loss_streak": trades["in_longest_loss_streak"],
        }
        for focus_name, focus_mask in focus_definitions.items():
            for factor in self.factor_columns:
                if factor not in trades.columns:
                    continue
                for value, group in trades.groupby(factor, dropna=False):
                    row = self.summarize_focus_group(focus_name, factor, str(value), group, focus_mask)
                    if row["focus_sample_count"] >= self.min_group_samples:
                        rows.append(row)
        report = pd.DataFrame(rows)
        if report.empty:
            return report
        return report.sort_values(
            ["focus_compound_return", "focus_loss_contribution", "focus_sample_count"],
            ascending=[True, False, False],
        )

    def build_worst_trades_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "trade_date",
            "ts_code",
            "name",
            "exit_trade_date",
            "daily_return",
            "net_return",
            "market_sentiment_level",
            "segment_market_sentiment_level",
            "retreat_state_bucket",
            "segment_retreat_state_bucket",
            "limit_up_count_bucket",
            "market_chain_count_bucket",
            "segment_limit_up_count_bucket",
            "segment_chain_count_bucket",
            "limit_times_detail_bucket",
            "first_time_detail_bucket",
            "open_times_bucket",
            "turnover_rate_bucket",
            "fd_ratio_bucket",
            "exit_reason",
            "limit_down_blocked_days",
        ]
        available_columns = [column for column in columns if column in trades.columns]
        return trades.sort_values("daily_return").head(self.worst_trade_count)[available_columns]

    def summarize_period(self, period_name: str, description: str, sample: pd.DataFrame) -> dict[str, object]:
        returns = sample["daily_return"].dropna() if not sample.empty else pd.Series(dtype=float)
        daily_returns = (
            sample.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            if not sample.empty
            else pd.Series(dtype=float)
        )
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "period_name": period_name,
            "description": description,
            "start_exit_trade_date": str(sample["exit_trade_date"].min()) if not sample.empty else "",
            "end_exit_trade_date": str(sample["exit_trade_date"].max()) if not sample.empty else "",
            "sample_count": int(len(returns)),
            "trade_days": int(sample["exit_trade_date"].nunique()) if not sample.empty else 0,
            "win_rate": self.win_rate(returns),
            "avg_daily_return": self.mean(returns),
            "median_daily_return": self.median(returns),
            "compound_return": self.compound_return(daily_returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_loss": self.min_value(returns),
            "max_profit": self.max_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "loss_contribution": float(sample["loss_amount_proxy"].sum()) if not sample.empty else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def summarize_focus_group(
        self,
        focus_name: str,
        factor: str,
        value: str,
        group: pd.DataFrame,
        focus_mask: pd.Series,
    ) -> dict[str, object]:
        group_focus_mask = focus_mask.reindex(group.index).fillna(False)
        focus = group[group_focus_mask].copy()
        baseline = group[~group_focus_mask].copy()
        focus_returns = focus["daily_return"].dropna()
        baseline_returns = baseline["daily_return"].dropna()
        overall_returns = group["daily_return"].dropna()
        return {
            "focus_name": focus_name,
            "factor": factor,
            "factor_value": value,
            "focus_sample_count": int(len(focus_returns)),
            "focus_win_rate": self.win_rate(focus_returns),
            "focus_avg_daily_return": self.mean(focus_returns),
            "focus_median_daily_return": self.median(focus_returns),
            "focus_compound_return": self.compound_return(focus_returns),
            "focus_loss_contribution": float(focus["loss_amount_proxy"].sum()),
            "baseline_sample_count": int(len(baseline_returns)),
            "baseline_win_rate": self.win_rate(baseline_returns),
            "baseline_compound_return": self.compound_return(baseline_returns),
            "overall_sample_count": int(len(overall_returns)),
            "overall_compound_return": self.compound_return(overall_returns),
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
