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
    parser = argparse.ArgumentParser(description="验证 A5-R1 收益优先版的日线成交真实性。")
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
    outputs = A5R1DailyExecutionRealityValidator(config_path=args.config).validate()
    print("A5-R1 日线成交真实性验证完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1DailyExecutionRealityValidator:
    """基于日线保守成交回放，验证 A5-R1 买入和卖出是否真实可执行。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_daily_execution_reality")
        self.reality_config = self.config.get("a5_r1_daily_execution_reality", {})
        self.input_trade_replay_path = self.project_root / self.reality_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.output_summary_path = self.project_root / self.reality_config.get(
            "output_summary_path",
            "reports/a5_r1_daily_execution_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.reality_config.get(
            "output_yearly_path",
            "reports/a5_r1_daily_execution_yearly.csv",
        )
        self.output_unbuyable_path = self.project_root / self.reality_config.get(
            "output_unbuyable_path",
            "reports/a5_r1_daily_execution_unbuyable.csv",
        )
        self.output_limit_down_blocked_path = self.project_root / self.reality_config.get(
            "output_limit_down_blocked_path",
            "reports/a5_r1_daily_execution_limit_down_blocked.csv",
        )
        self.replay_rule = str(self.reality_config.get("replay_rule", "fixed_t2_close"))
        self.position_pct = float(self.reality_config.get("position_pct", 0.8))
        self.skip_market_sentiment = str(self.reality_config.get("skip_market_sentiment", "weak"))
        self.skip_segment_market_sentiment = str(
            self.reality_config.get("skip_segment_market_sentiment", "neutral")
        )

    def validate(self) -> dict[str, Path]:
        trades = self.load_trades()
        summary = self.build_summary(trades)
        yearly = self.build_yearly_report(trades)
        unbuyable = self.build_unbuyable_report(trades)
        limit_down_blocked = self.build_limit_down_blocked_report(trades)

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        unbuyable.to_csv(self.output_unbuyable_path, index=False, encoding="utf-8-sig")
        limit_down_blocked.to_csv(self.output_limit_down_blocked_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 日线成交真实性汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 日线成交真实性年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 买不到信号报告已生成: %s, 行数: %s", self.output_unbuyable_path, len(unbuyable))
        self.logger.info(
            "A5-R1 跌停阻塞卖出报告已生成: %s, 行数: %s",
            self.output_limit_down_blocked_path,
            len(limit_down_blocked),
        )
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "unbuyable": self.output_unbuyable_path,
            "limit_down_blocked": self.output_limit_down_blocked_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "exit_trade_date": str, "ts_code": str},
            low_memory=False,
        )
        trades = trades[trades["replay_rule"].astype(str) == self.replay_rule].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到回放规则样本: replay_rule={self.replay_rule}")

        trades["is_a5_r1_dynamic_skip"] = (
            trades["market_sentiment_level"].astype(str).eq(self.skip_market_sentiment)
            & trades["segment_market_sentiment_level"].astype(str).eq(self.skip_segment_market_sentiment)
        )
        trades["is_a5_r1_planned_trade"] = ~trades["is_a5_r1_dynamic_skip"]
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce").fillna(0.0)
        trades["daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce").fillna(0.0)
        trades["limit_down_blocked_days"] = pd.to_numeric(
            trades["limit_down_blocked_days"],
            errors="coerce",
        ).fillna(0).astype(int)
        trades["a5_r1_realized_daily_return"] = 0.0
        executed_mask = (
            trades["is_a5_r1_planned_trade"]
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
        )
        trades.loc[executed_mask, "a5_r1_realized_daily_return"] = trades.loc[executed_mask, "net_return"] * self.position_pct
        trades["a5_r1_execution_state"] = "dynamic_skip"
        trades.loc[trades["is_a5_r1_planned_trade"], "a5_r1_execution_state"] = "planned"
        trades.loc[
            trades["is_a5_r1_planned_trade"] & (trades["buy_executed"] != True),  # noqa: E712
            "a5_r1_execution_state",
        ] = "buy_rejected"
        trades.loc[
            trades["is_a5_r1_planned_trade"]
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] != True),  # noqa: E712
            "a5_r1_execution_state",
        ] = "sell_unresolved"
        trades.loc[executed_mask, "a5_r1_execution_state"] = "executed"
        trades["planned_exit_trade_date"] = trades["d2_trade_date"].astype(str)
        trades["actual_exit_trade_date"] = trades["exit_trade_date"].astype(str)
        trades["is_exit_delayed_by_limit_down"] = (
            executed_mask
            & (trades["limit_down_blocked_days"] > 0)
            & (trades["actual_exit_trade_date"] != trades["planned_exit_trade_date"])
        )
        trades["signal_year"] = trades["trade_date"].astype(str).str[:4]
        trades["exit_year"] = trades["exit_trade_date"].astype(str).str[:4]
        return trades.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def build_summary(self, trades: pd.DataFrame) -> pd.DataFrame:
        planned = trades[trades["is_a5_r1_planned_trade"]].copy()
        executed = planned[planned["a5_r1_execution_state"].eq("executed")].copy()
        daily_returns = executed.groupby("exit_trade_date")["a5_r1_realized_daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        returns = executed["a5_r1_realized_daily_return"].dropna()
        blocked = executed[executed["limit_down_blocked_days"] > 0].copy()
        unbuyable = planned[planned["a5_r1_execution_state"].eq("buy_rejected")].copy()
        rows = [
            {
                "scope": "a5_r1_daily_execution_reality",
                "signal_count": int(len(trades)),
                "dynamic_skip_count": int(trades["is_a5_r1_dynamic_skip"].sum()),
                "planned_trade_count": int(len(planned)),
                "buy_executed_count": int((planned["buy_executed"] == True).sum()),  # noqa: E712
                "buy_rejected_count": int((planned["buy_executed"] != True).sum()),  # noqa: E712
                "sell_executed_count": int(len(executed)),
                "sell_unresolved_count": int((planned["a5_r1_execution_state"] == "sell_unresolved").sum()),
                "executed_trade_count": int(len(executed)),
                "limit_down_blocked_trade_count": int(len(blocked)),
                "limit_down_blocked_day_total": int(blocked["limit_down_blocked_days"].sum()),
                "exit_delayed_by_limit_down_count": int(executed["is_exit_delayed_by_limit_down"].sum()),
                "path_conflict_count": int(executed["path_conflict"].fillna(False).sum()),
                "win_rate": self.win_rate(returns),
                "avg_daily_return": self.mean(returns),
                "median_daily_return": self.median(returns),
                "total_compound_return": self.compound_return(daily_returns),
                "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                "max_profit": self.max_value(returns),
                "max_loss": self.min_value(returns),
                "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                "unbuyable_signal_list": ",".join(unbuyable["ts_code"].astype(str).tolist()),
            }
        ]
        return pd.DataFrame(rows)

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        rows = []
        years = sorted(
            year
            for year in (set(trades["signal_year"].dropna().astype(str)) | set(trades["exit_year"].dropna().astype(str)))
            if year.isdigit()
        )
        for year in years:
            signal_group = trades[trades["signal_year"].astype(str) == year].copy()
            planned = signal_group[signal_group["is_a5_r1_planned_trade"]].copy()
            return_group = trades[
                (trades["exit_year"].astype(str) == year)
                & trades["a5_r1_execution_state"].eq("executed")
            ].copy()
            returns = return_group["a5_r1_realized_daily_return"].dropna()
            daily_returns = return_group.groupby("exit_trade_date")["a5_r1_realized_daily_return"].sum().sort_index()
            equity_curve = (1 + daily_returns).cumprod()
            rows.append(
                {
                    "year": year,
                    "signal_count": int(len(signal_group)),
                    "dynamic_skip_count": int(signal_group["is_a5_r1_dynamic_skip"].sum()),
                    "planned_trade_count": int(len(planned)),
                    "buy_rejected_count": int((planned["buy_executed"] != True).sum()),  # noqa: E712
                    "executed_trade_count_by_exit_year": int(len(return_group)),
                    "limit_down_blocked_trade_count": int((return_group["limit_down_blocked_days"] > 0).sum()),
                    "exit_delayed_by_limit_down_count": int(return_group["is_exit_delayed_by_limit_down"].sum()),
                    "year_return": self.compound_return(daily_returns),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "max_loss": self.min_value(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return pd.DataFrame(rows).sort_values("year")

    def build_unbuyable_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        columns = self.detail_columns()
        unbuyable = trades[
            trades["is_a5_r1_planned_trade"]
            & trades["a5_r1_execution_state"].eq("buy_rejected")
        ].copy()
        return unbuyable[[column for column in columns if column in unbuyable.columns]].sort_values("trade_date")

    def build_limit_down_blocked_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        columns = self.detail_columns() + [
            "planned_exit_trade_date",
            "actual_exit_trade_date",
            "limit_down_blocked_days",
            "is_exit_delayed_by_limit_down",
            "exit_reason",
            "a5_r1_realized_daily_return",
        ]
        blocked = trades[
            trades["is_a5_r1_planned_trade"]
            & trades["a5_r1_execution_state"].eq("executed")
            & (trades["limit_down_blocked_days"] > 0)
        ].copy()
        return blocked[[column for column in columns if column in blocked.columns]].sort_values("trade_date")

    @staticmethod
    def detail_columns() -> list[str]:
        return [
            "trade_date",
            "ts_code",
            "name",
            "a5_r1_execution_state",
            "buy_trade_date",
            "buy_reject_reason",
            "buy_price_before_slippage",
            "buy_price",
            "d1_trade_date",
            "d1_open",
            "d1_high",
            "d1_low",
            "d1_close",
            "d2_trade_date",
            "d2_open",
            "d2_high",
            "d2_low",
            "d2_close",
            "market_segment",
            "limit_pct_bucket",
            "market_sentiment_level",
            "segment_market_sentiment_level",
            "limit_times_detail_bucket",
            "limit_up_count_bucket",
            "segment_limit_up_count_bucket",
        ]

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
