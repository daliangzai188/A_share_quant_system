from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class StrategyFailureAttributor:
    """分析指定年份策略失效和回撤来源。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("failure_attribution")
        self.attr_config = self.config.get("failure_attribution", {})
        self.input_trade_replay_path = self.project_root / self.attr_config.get(
            "input_trade_replay_path", "reports/trade_replay_report.csv"
        )
        self.output_group_report_path = self.project_root / self.attr_config.get(
            "output_group_report_path", "reports/failure_attribution_group.csv"
        )
        self.output_monthly_report_path = self.project_root / self.attr_config.get(
            "output_monthly_report_path", "reports/failure_attribution_monthly.csv"
        )
        self.output_period_report_path = self.project_root / self.attr_config.get(
            "output_period_report_path", "reports/failure_attribution_periods.csv"
        )
        self.output_focus_group_report_path = self.project_root / self.attr_config.get(
            "output_focus_group_report_path", "reports/failure_attribution_focus_group.csv"
        )
        self.output_worst_trades_path = self.project_root / self.attr_config.get(
            "output_worst_trades_path", "reports/failure_attribution_worst_trades.csv"
        )
        self.output_filter_candidates_path = self.project_root / self.attr_config.get(
            "output_filter_candidates_path", "reports/failure_filter_candidates.csv"
        )
        self.output_filter_backtest_path = self.project_root / self.attr_config.get(
            "output_filter_backtest_path", "reports/failure_filter_backtest.csv"
        )
        self.output_filter_backtest_yearly_path = self.project_root / self.attr_config.get(
            "output_filter_backtest_yearly_path", "reports/failure_filter_backtest_yearly.csv"
        )
        self.replay_rule = self.attr_config.get("replay_rule", "fixed_t2_close")
        self.focus_years = {str(year) for year in self.attr_config.get("focus_years", ["2022", "2026"])}
        self.baseline_years = {str(year) for year in self.attr_config.get("baseline_years", [])}
        self.factor_columns = list(self.attr_config.get("factor_columns", []))
        self.min_group_samples = int(self.attr_config.get("min_group_samples", 5))
        self.worst_trade_count = int(self.attr_config.get("worst_trade_count", 50))

    def analyze(self) -> dict[str, Path]:
        trades = self.load_replay_trades()
        trades = self.annotate_focus_periods(trades)
        group_report = self.build_group_report(trades)
        monthly_report = self.build_monthly_report(trades)
        period_report = self.build_period_report(trades)
        focus_group_report = self.build_focus_group_report(trades)
        worst_trades = self.build_worst_trades_report(trades)
        filter_candidates = self.build_filter_candidates(focus_group_report)
        filter_backtest, filter_yearly = self.build_filter_backtest(trades, filter_candidates)

        mkdir_p(self.output_group_report_path.parent)
        group_report.to_csv(self.output_group_report_path, index=False, encoding="utf-8-sig")
        monthly_report.to_csv(self.output_monthly_report_path, index=False, encoding="utf-8-sig")
        period_report.to_csv(self.output_period_report_path, index=False, encoding="utf-8-sig")
        focus_group_report.to_csv(self.output_focus_group_report_path, index=False, encoding="utf-8-sig")
        worst_trades.to_csv(self.output_worst_trades_path, index=False, encoding="utf-8-sig")
        filter_candidates.to_csv(self.output_filter_candidates_path, index=False, encoding="utf-8-sig")
        filter_backtest.to_csv(self.output_filter_backtest_path, index=False, encoding="utf-8-sig")
        filter_yearly.to_csv(self.output_filter_backtest_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("失效归因分组报告已生成: %s, 行数: %s", self.output_group_report_path, len(group_report))
        self.logger.info("失效归因月度报告已生成: %s, 行数: %s", self.output_monthly_report_path, len(monthly_report))
        self.logger.info("失效归因重点区间报告已生成: %s, 行数: %s", self.output_period_report_path, len(period_report))
        self.logger.info("失效归因重点区间分组报告已生成: %s, 行数: %s", self.output_focus_group_report_path, len(focus_group_report))
        self.logger.info("最差交易报告已生成: %s, 行数: %s", self.output_worst_trades_path, len(worst_trades))
        self.logger.info("候选过滤条件报告已生成: %s, 行数: %s", self.output_filter_candidates_path, len(filter_candidates))
        self.logger.info("候选过滤回测报告已生成: %s, 行数: %s", self.output_filter_backtest_path, len(filter_backtest))
        self.logger.info("候选过滤年度报告已生成: %s, 行数: %s", self.output_filter_backtest_yearly_path, len(filter_yearly))
        return {
            "group_report": self.output_group_report_path,
            "monthly_report": self.output_monthly_report_path,
            "period_report": self.output_period_report_path,
            "focus_group_report": self.output_focus_group_report_path,
            "worst_trades": self.output_worst_trades_path,
            "filter_candidates": self.output_filter_candidates_path,
            "filter_backtest": self.output_filter_backtest_path,
            "filter_backtest_yearly": self.output_filter_backtest_yearly_path,
        }

    def load_replay_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str},
            low_memory=False,
        )
        trades = trades[
            (trades["replay_rule"].astype(str) == self.replay_rule)
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
            & trades["exit_trade_date"].notna()
            & trades["net_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到可归因的回放交易: replay_rule={self.replay_rule}")

        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        trades["month"] = trades["exit_trade_date"].astype(str).str[:6]
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce")
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
        result["in_config_focus_years"] = result["year"].isin(self.focus_years)
        result["in_max_drawdown_period"] = False
        result["in_longest_loss_streak"] = False

        drawdown_period = self.find_max_drawdown_period(result)
        if drawdown_period:
            start_date, end_date = drawdown_period
            exit_dates = result["exit_trade_date"].astype(str)
            result["in_max_drawdown_period"] = (exit_dates >= start_date) & (exit_dates <= end_date)

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
        running_max = equity_curve.cummax()
        drawdown = equity_curve / running_max - 1
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

    def build_group_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for factor in self.factor_columns:
            for value, group in trades.groupby(factor, dropna=False):
                rows.append(self.summarize_group(factor, str(value), group))
        report = pd.DataFrame(rows)
        if report.empty:
            return report
        report = report[report["focus_sample_count"] >= self.min_group_samples].copy()
        return report.sort_values(
            ["focus_compound_return", "focus_loss_contribution", "focus_sample_count"],
            ascending=[True, False, False],
        )

    def summarize_group(self, factor: str, value: str, group: pd.DataFrame) -> dict[str, object]:
        focus = group[group["year"].isin(self.focus_years)].copy()
        baseline = group[group["year"].isin(self.baseline_years)].copy() if self.baseline_years else group[~group["year"].isin(self.focus_years)].copy()
        overall = group.copy()
        focus_returns = focus["daily_return"].dropna()
        baseline_returns = baseline["daily_return"].dropna()
        overall_returns = overall["daily_return"].dropna()
        return {
            "factor": factor,
            "factor_value": value,
            "focus_years": ",".join(sorted(self.focus_years)),
            "focus_sample_count": int(len(focus_returns)),
            "focus_win_rate": self.win_rate(focus_returns),
            "focus_avg_daily_return": self.mean(focus_returns),
            "focus_median_daily_return": self.median(focus_returns),
            "focus_compound_return": self.compound_return(focus_returns),
            "focus_max_loss": self.min_value(focus_returns),
            "focus_loss_contribution": float(focus["loss_amount_proxy"].sum()),
            "focus_max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown((1 + focus_returns).cumprod()),
            "baseline_sample_count": int(len(baseline_returns)),
            "baseline_win_rate": self.win_rate(baseline_returns),
            "baseline_avg_daily_return": self.mean(baseline_returns),
            "baseline_compound_return": self.compound_return(baseline_returns),
            "overall_sample_count": int(len(overall_returns)),
            "overall_compound_return": self.compound_return(overall_returns),
        }

    def build_monthly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for month, group in trades.groupby("month"):
            returns = group["daily_return"].dropna()
            rows.append(
                {
                    "month": month,
                    "year": str(month)[:4],
                    "sample_count": int(len(returns)),
                    "win_rate": self.win_rate(returns),
                    "avg_daily_return": self.mean(returns),
                    "median_daily_return": self.median(returns),
                    "compound_return": self.compound_return(returns),
                    "max_loss": self.min_value(returns),
                    "loss_contribution": float(group["loss_amount_proxy"].sum()),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown((1 + returns).cumprod()),
                }
            )
        return pd.DataFrame(rows).sort_values(["compound_return", "loss_contribution"], ascending=[True, False])

    def build_period_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = [
            self.summarize_period("all_executed_trades", "全部已成交交易", trades),
            self.summarize_period(
                "config_focus_years",
                f"配置指定弱年份: {','.join(sorted(self.focus_years))}",
                trades[trades["in_config_focus_years"]].copy(),
            ),
            self.summarize_period(
                "max_drawdown_period",
                "自动识别的最大回撤区间",
                trades[trades["in_max_drawdown_period"]].copy(),
            ),
            self.summarize_period(
                "longest_loss_streak",
                "自动识别的最长连续亏损交易段",
                trades[trades["in_longest_loss_streak"]].copy(),
            ),
        ]
        return pd.DataFrame(rows)

    def summarize_period(self, period_name: str, description: str, sample: pd.DataFrame) -> dict[str, object]:
        returns = sample["daily_return"].dropna() if not sample.empty else pd.Series(dtype=float)
        daily_returns = sample.groupby("exit_trade_date")["daily_return"].sum().sort_index() if not sample.empty else pd.Series(dtype=float)
        equity_curve = (1 + daily_returns).cumprod()
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
            "max_loss": self.min_value(returns),
            "loss_contribution": float(sample["loss_amount_proxy"].sum()) if not sample.empty else 0.0,
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def build_focus_group_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        focus_definitions = {
            "config_focus_years": trades["in_config_focus_years"],
            "max_drawdown_period": trades["in_max_drawdown_period"],
            "longest_loss_streak": trades["in_longest_loss_streak"],
        }
        rows = []
        for focus_name, focus_mask in focus_definitions.items():
            for factor in self.factor_columns:
                for value, group in trades.groupby(factor, dropna=False):
                    rows.append(self.summarize_focus_group(focus_name, factor, str(value), group, focus_mask))
        report = pd.DataFrame(rows)
        if report.empty:
            return report
        report = report[report["focus_sample_count"] >= self.min_group_samples].copy()
        return report.sort_values(
            ["focus_compound_return", "focus_loss_contribution", "focus_sample_count"],
            ascending=[True, False, False],
        )

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
            "focus_max_loss": self.min_value(focus_returns),
            "focus_loss_contribution": float(focus["loss_amount_proxy"].sum()),
            "focus_max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown((1 + focus_returns).cumprod()),
            "baseline_sample_count": int(len(baseline_returns)),
            "baseline_win_rate": self.win_rate(baseline_returns),
            "baseline_avg_daily_return": self.mean(baseline_returns),
            "baseline_compound_return": self.compound_return(baseline_returns),
            "overall_sample_count": int(len(overall_returns)),
            "overall_compound_return": self.compound_return(overall_returns),
        }

    def build_worst_trades_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        focus = trades[trades["year"].isin(self.focus_years)].copy()
        columns = [
            "trade_date",
            "ts_code",
            "name",
            "exit_trade_date",
            "year",
            "daily_return",
            "net_return",
            "market_sentiment_level",
            "retreat_state",
            "limit_up_count",
            "limit_up_count_bucket",
            "limit_times",
            "limit_height_rank_bucket",
            "market_leader_rank_bucket",
            "first_time",
            "first_time_detail_bucket",
            "open_times",
            "open_times_bucket",
            "volume_ratio",
            "volume_ratio_bucket",
            "amount_ratio_1d",
            "amount_ratio_bucket",
            "turnover_rate",
            "turnover_rate_bucket",
            "fd_amount_to_circ_mv",
            "fd_ratio_bucket",
            "exit_reason",
            "limit_down_blocked_days",
        ]
        available_columns = [column for column in columns if column in focus.columns]
        return focus.sort_values("daily_return").head(self.worst_trade_count)[available_columns]

    def build_filter_candidates(self, group_report: pd.DataFrame) -> pd.DataFrame:
        if group_report.empty:
            return group_report
        candidates = group_report[
            (group_report["focus_sample_count"] >= self.min_group_samples)
            & (group_report["focus_compound_return"] < 0)
            & (group_report["baseline_compound_return"] <= 0)
        ].copy()
        if candidates.empty:
            candidates = group_report[
                (group_report["focus_sample_count"] >= self.min_group_samples)
                & (group_report["focus_compound_return"] < 0)
            ].copy()
        candidates["suggestion"] = "优先回测过滤或降权，不直接删除；需再验证整体收益、回撤和样本数变化。"
        return candidates.sort_values(
            ["focus_compound_return", "focus_loss_contribution", "focus_sample_count"],
            ascending=[True, False, False],
        )

    def build_filter_backtest(self, trades: pd.DataFrame, candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        scenarios = [("base_no_filter", trades.copy(), "不添加失效过滤")]
        for row in candidates.itertuples(index=False):
            focus_name = str(getattr(row, "focus_name", "focus"))
            factor = str(row.factor)
            value = str(row.factor_value)
            if factor not in trades.columns:
                continue
            filtered = trades[trades[factor].astype(str) != value].copy()
            scenarios.append((f"{focus_name}_exclude_{factor}={value}", filtered, f"{focus_name}: 排除 {factor}={value}"))

        summary_rows = []
        yearly_rows = []
        for scenario_name, sample, description in scenarios:
            summary_rows.append(self.summarize_backtest_scenario(scenario_name, sample, description))
            yearly_rows.extend(self.build_backtest_yearly_rows(scenario_name, sample))
        summary = pd.DataFrame(summary_rows).sort_values(
            ["focus_compound_return", "max_drawdown", "total_compound_return"],
            ascending=[False, True, False],
        )
        yearly = pd.DataFrame(yearly_rows)
        return summary, yearly

    def summarize_backtest_scenario(self, scenario_name: str, trades: pd.DataFrame, description: str) -> dict[str, object]:
        returns = trades["daily_return"].dropna()
        daily_returns = trades.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        focus = trades[trades["year"].isin(self.focus_years)]["daily_return"].dropna()
        baseline = trades[trades["year"].isin(self.baseline_years)]["daily_return"].dropna()
        return {
            "scenario": scenario_name,
            "description": description,
            "sample_count": int(len(returns)),
            "win_rate": self.win_rate(returns),
            "avg_daily_return": self.mean(returns),
            "median_daily_return": self.median(returns),
            "total_compound_return": self.compound_return(daily_returns),
            "focus_compound_return": self.compound_return(focus),
            "baseline_compound_return": self.compound_return(baseline),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_loss": self.min_value(returns),
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def build_backtest_yearly_rows(self, scenario_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for year, group in trades.groupby("year"):
            returns = group["daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": year,
                    "sample_count": int(len(returns)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": self.win_rate(returns),
                    "avg_daily_return": self.mean(returns),
                    "median_daily_return": self.median(returns),
                }
            )
        return rows

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
    def min_value(returns: pd.Series) -> float:
        return float(returns.min()) if len(returns) else 0.0

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0
