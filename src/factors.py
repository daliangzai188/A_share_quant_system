from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class NextDayPremiumAnalyzer:
    """基于可靠可买涨停样本统计 T+1 开盘买入、T+2 收盘卖出的次日溢价。"""

    GROUP_COLUMNS = [
        "limit_times_bucket",
        "board_type",
        "first_time_bucket",
        "market_sentiment_level",
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("next_day_premium")

        analysis_config = self.config.get("analysis", {})
        self.daily_merged_path = self.project_root / analysis_config.get(
            "input_daily_merged_path", "data/processed/daily_merged.csv"
        )
        self.limit_up_fill_scored_path = self.project_root / analysis_config.get(
            "input_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"
        )
        self.output_trades_path = self.project_root / analysis_config.get(
            "output_next_day_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        self.output_summary_path = self.project_root / analysis_config.get(
            "output_next_day_summary_path", "reports/next_day_premium_summary.csv"
        )
        self.output_group_path = self.project_root / analysis_config.get(
            "output_next_day_group_path", "reports/next_day_premium_by_group.csv"
        )
        self.commission_rate = float(analysis_config.get("commission_rate", 0.0003))
        self.stamp_tax_rate = float(analysis_config.get("stamp_tax_rate", 0.001))
        self.transfer_fee_rate = float(analysis_config.get("transfer_fee_rate", 0.00001))
        self.slippage_rate = float(analysis_config.get("slippage_rate", 0.001))

    def analyze(self) -> dict[str, Path]:
        trades = self.build_trade_samples()
        if trades.empty:
            raise RuntimeError("没有可分析的可靠可买样本，请先运行成交概率打标。")

        summary = self.summarize(trades, group_name="overall")
        groups = self.build_group_report(trades)

        self.output_trades_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_group_path.parent.mkdir(parents=True, exist_ok=True)

        trades.to_csv(self.output_trades_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        groups.to_csv(self.output_group_path, index=False, encoding="utf-8-sig")

        self.logger.info("次日溢价交易样本已生成: %s, 行数: %s", self.output_trades_path, len(trades))
        self.logger.info("次日溢价汇总报告已生成: %s", self.output_summary_path)
        self.logger.info("次日溢价分组报告已生成: %s, 行数: %s", self.output_group_path, len(groups))
        return {
            "trades": self.output_trades_path,
            "summary": self.output_summary_path,
            "group_report": self.output_group_path,
        }

    def build_trade_samples(self) -> pd.DataFrame:
        daily = pd.read_csv(
            self.daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "open", "close"],
        )
        limit_up = pd.read_csv(
            self.limit_up_fill_scored_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        limit_up = limit_up[limit_up["allow_buy_reliable"] == True].copy()  # noqa: E712
        limit_up = limit_up.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        daily = daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        daily["next_trade_date"] = daily.groupby("ts_code")["trade_date"].shift(-1)
        daily["next_open"] = daily.groupby("ts_code")["open"].shift(-1)
        daily["next_close"] = daily.groupby("ts_code")["close"].shift(-1)
        daily["exit_trade_date"] = daily.groupby("ts_code")["trade_date"].shift(-2)
        daily["exit_close"] = daily.groupby("ts_code")["close"].shift(-2)

        future_prices = daily[
            ["trade_date", "ts_code", "next_trade_date", "next_open", "exit_trade_date", "exit_close"]
        ].copy()
        trades = limit_up.merge(future_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        trades = trades[trades["next_open"].notna() & trades["exit_close"].notna()].copy()
        trades["gross_return"] = trades["exit_close"] / trades["next_open"] - 1
        trades["fee_rate"] = self.buy_fee_rate + self.sell_fee_rate + self.slippage_rate * 2
        trades["net_return"] = trades["gross_return"] - trades["fee_rate"]
        trades["is_win"] = trades["net_return"] > 0
        trades["holding_days_rule"] = "T+1_open_buy_T+2_close_sell"
        return trades

    def build_group_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        reports = [self.summarize(trades, group_name="overall")]
        for column in self.GROUP_COLUMNS:
            for value, group in trades.groupby(column, dropna=False):
                report = self.summarize(group, group_name=column)
                report.insert(1, "group_value", value)
                reports.append(report)
        return pd.concat(reports, ignore_index=True)

    def summarize(self, trades: pd.DataFrame, group_name: str) -> pd.DataFrame:
        returns = trades["net_return"].dropna()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        total_return_curve = (1 + returns).cumprod()
        max_drawdown = self.calculate_max_drawdown(total_return_curve)
        row = {
            "group_name": group_name,
            "sample_count": int(len(returns)),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return": float(returns.mean()) if len(returns) else 0.0,
            "median_return": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": float(total_return_curve.iloc[-1] - 1) if len(total_return_curve) else 0.0,
            "max_drawdown": float(max_drawdown),
            "max_profit": float(returns.max()) if len(returns) else 0.0,
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": int(self.max_consecutive_losses(returns)),
            "fee_rate": float(trades["fee_rate"].iloc[0]) if not trades.empty else 0.0,
        }
        return pd.DataFrame([row])

    @property
    def buy_fee_rate(self) -> float:
        return self.commission_rate + self.transfer_fee_rate

    @property
    def sell_fee_rate(self) -> float:
        return self.commission_rate + self.transfer_fee_rate + self.stamp_tax_rate

    @staticmethod
    def calculate_max_drawdown(curve: pd.Series) -> float:
        if curve.empty:
            return 0.0
        running_max = curve.cummax()
        drawdown = curve / running_max - 1
        return abs(float(drawdown.min()))

    @staticmethod
    def max_consecutive_losses(returns: pd.Series) -> int:
        max_count = 0
        current = 0
        for value in returns:
            if value <= 0:
                current += 1
                max_count = max(max_count, current)
            else:
                current = 0
        return max_count


class FactorAnalyzer:
    """对可靠可买样本做基础因子分组统计。"""

    FACTOR_COLUMNS = [
        "market_sentiment_level",
        "board_type",
        "first_time_bucket",
        "limit_times_bucket",
        "amount_bucket",
        "turnover_rate_bucket",
        "fd_ratio_bucket",
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("factor_analyzer")
        analysis_config = self.config.get("analysis", {})
        self.input_trades_path = self.project_root / analysis_config.get(
            "output_next_day_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        self.output_factor_report_path = self.project_root / analysis_config.get(
            "output_factor_report_path", "reports/factor_analysis_report.csv"
        )

    def analyze(self) -> Path:
        trades = pd.read_csv(self.input_trades_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        if trades.empty:
            raise RuntimeError("次日溢价交易样本为空，请先运行 analyze_next_day_premium.py。")

        trades = self.add_factor_buckets(trades)
        reports = []
        for factor in self.FACTOR_COLUMNS:
            for value, group in trades.groupby(factor, dropna=False):
                reports.append(self.summarize_factor_group(factor=factor, value=value, group=group))

        report = pd.DataFrame(reports)
        report = report.sort_values(["factor", "sample_count", "avg_return"], ascending=[True, False, False])
        self.output_factor_report_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_factor_report_path, index=False, encoding="utf-8-sig")
        self.logger.info("因子统计报告已生成: %s, 行数: %s", self.output_factor_report_path, len(report))
        return self.output_factor_report_path

    def add_factor_buckets(self, trades: pd.DataFrame) -> pd.DataFrame:
        trades = trades.copy()
        trades["amount_bucket"] = pd.cut(
            trades["amount"],
            bins=[-float("inf"), 100000, 300000, 800000, float("inf")],
            labels=["lt_1e8", "1e8_3e8", "3e8_8e8", "gte_8e8"],
        ).astype(str)
        trades["turnover_rate_bucket"] = pd.cut(
            trades["turnover_rate"],
            bins=[-float("inf"), 3, 8, 15, float("inf")],
            labels=["lt_3", "3_8", "8_15", "gte_15"],
        ).astype(str)
        trades["fd_ratio_bucket"] = pd.cut(
            trades["fd_amount_to_circ_mv"],
            bins=[-float("inf"), 0.005, 0.02, 0.05, float("inf")],
            labels=["lt_0_5pct", "0_5pct_2pct", "2pct_5pct", "gte_5pct"],
        ).astype(str)
        return trades

    @staticmethod
    def summarize_factor_group(factor: str, value: object, group: pd.DataFrame) -> dict[str, object]:
        returns = group["net_return"].dropna()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        curve = (1 + returns).cumprod()
        max_drawdown = NextDayPremiumAnalyzer.calculate_max_drawdown(curve)
        sample_count = len(returns)
        win_rate = float((returns > 0).mean()) if sample_count else 0.0
        avg_return = float(returns.mean()) if sample_count else 0.0
        return {
            "factor": factor,
            "factor_value": value,
            "sample_count": int(sample_count),
            "win_rate": win_rate,
            "avg_return": avg_return,
            "median_return": float(returns.median()) if sample_count else 0.0,
            "total_compound_return": float(curve.iloc[-1] - 1) if len(curve) else 0.0,
            "max_drawdown": float(max_drawdown),
            "max_profit": float(returns.max()) if sample_count else 0.0,
            "max_loss": float(returns.min()) if sample_count else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
            "is_candidate": bool(sample_count >= 300 and win_rate >= 0.45 and avg_return > 0),
        }


class FactorComboAnalyzer:
    """对基础候选因子做组合统计，并拆分训练集、测试集和样本外。"""

    DEFAULT_COMBOS = [
        {
            "combo_name": "strong_market",
            "conditions": {"market_sentiment_level": ["strong"]},
        },
        {
            "combo_name": "strong_market_multi_open",
            "conditions": {"market_sentiment_level": ["strong"], "board_type": ["multi_open"]},
        },
        {
            "combo_name": "strong_market_midday",
            "conditions": {"market_sentiment_level": ["strong"], "first_time_bucket": ["midday"]},
        },
        {
            "combo_name": "strong_market_low_fd_ratio",
            "conditions": {"market_sentiment_level": ["strong"], "fd_ratio_bucket": ["lt_0_5pct"]},
        },
        {
            "combo_name": "strong_multi_midday_low_fd",
            "conditions": {
                "market_sentiment_level": ["strong"],
                "board_type": ["multi_open"],
                "first_time_bucket": ["midday"],
                "fd_ratio_bucket": ["lt_0_5pct"],
            },
        },
        {
            "combo_name": "multi_midday_low_fd",
            "conditions": {
                "board_type": ["multi_open"],
                "first_time_bucket": ["midday"],
                "fd_ratio_bucket": ["lt_0_5pct"],
            },
        },
        {
            "combo_name": "one_board_multi_midday_low_fd",
            "conditions": {
                "limit_times_bucket": ["1"],
                "board_type": ["multi_open"],
                "first_time_bucket": ["midday"],
                "fd_ratio_bucket": ["lt_0_5pct"],
            },
        },
    ]

    PERIODS = [
        ("train_2019_2022", "20190101", "20221231"),
        ("test_2023_2024", "20230101", "20241231"),
        ("out_of_sample_2025_2026", "20250101", "20260518"),
        ("all", "20190101", "20260518"),
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("factor_combo_analyzer")
        analysis_config = self.config.get("analysis", {})
        self.input_trades_path = self.project_root / analysis_config.get(
            "output_next_day_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        self.output_combo_report_path = self.project_root / analysis_config.get(
            "output_factor_combo_report_path", "reports/factor_combo_report.csv"
        )

    def analyze(self) -> Path:
        trades = pd.read_csv(self.input_trades_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        trades = FactorAnalyzer(config_path="config/config.json").add_factor_buckets(trades)
        rows = []
        for combo in self.DEFAULT_COMBOS:
            combo_data = self.apply_conditions(trades, combo["conditions"])
            for period_name, start_date, end_date in self.PERIODS:
                period_data = combo_data[
                    (combo_data["trade_date"] >= start_date) & (combo_data["trade_date"] <= end_date)
                ].copy()
                summary = FactorAnalyzer.summarize_factor_group(
                    factor="combo",
                    value=combo["combo_name"],
                    group=period_data,
                )
                summary["period"] = period_name
                summary["conditions"] = self.format_conditions(combo["conditions"])
                summary["is_stable_candidate"] = False
                rows.append(summary)

        report = pd.DataFrame(rows)
        report = self.add_stability_flags(report)
        report = report.sort_values(["combo_name_sort", "period"]).drop(columns=["combo_name_sort"])
        self.output_combo_report_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_combo_report_path, index=False, encoding="utf-8-sig")
        self.logger.info("组合因子报告已生成: %s, 行数: %s", self.output_combo_report_path, len(report))
        return self.output_combo_report_path

    @staticmethod
    def apply_conditions(data: pd.DataFrame, conditions: dict[str, list[str]]) -> pd.DataFrame:
        result = data
        for column, allowed_values in conditions.items():
            result = result[result[column].astype(str).isin([str(value) for value in allowed_values])]
        return result.copy()

    @staticmethod
    def format_conditions(conditions: dict[str, list[str]]) -> str:
        return ";".join(f"{key} in {values}" for key, values in conditions.items())

    @staticmethod
    def add_stability_flags(report: pd.DataFrame) -> pd.DataFrame:
        report = report.rename(columns={"factor_value": "combo_name"})
        report["combo_name_sort"] = report["combo_name"]
        stable_combos = []
        for combo_name, group in report.groupby("combo_name"):
            indexed = group.set_index("period")
            required_periods = ["train_2019_2022", "test_2023_2024", "out_of_sample_2025_2026"]
            if not all(period in indexed.index for period in required_periods):
                continue
            is_stable = all(
                indexed.loc[period, "sample_count"] >= 300
                and indexed.loc[period, "avg_return"] > 0
                and indexed.loc[period, "win_rate"] >= 0.45
                for period in required_periods
            )
            if is_stable:
                stable_combos.append(combo_name)
        report["is_stable_candidate"] = report["combo_name"].isin(stable_combos)
        return report
