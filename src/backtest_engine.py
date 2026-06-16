from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class SimpleCandidateBacktester:
    """第一版候选池回测：T+1 开盘买入，T+2 收盘卖出，等权独立交易。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("backtest")
        backtest_config = self.config.get("backtest", {})
        self.input_candidate_pool_path = self.project_root / backtest_config.get(
            "input_candidate_pool_path", "data/processed/candidate_pool.csv"
        )
        self.output_trades_path = self.project_root / backtest_config.get(
            "output_trades_path", "reports/backtest_trades.csv"
        )
        self.output_equity_curve_path = self.project_root / backtest_config.get(
            "output_equity_curve_path", "reports/backtest_equity_curve.csv"
        )
        self.output_summary_path = self.project_root / backtest_config.get(
            "output_summary_path", "reports/backtest_summary.csv"
        )
        self.output_yearly_path = self.project_root / backtest_config.get(
            "output_yearly_path", "reports/backtest_yearly.csv"
        )
        self.initial_cash = float(backtest_config.get("initial_cash", 1000000))
        self.max_holding_count = int(backtest_config.get("max_holding_count", 5))
        self.max_position_pct_per_stock = float(backtest_config.get("max_position_pct_per_stock", 0.1))

    def run(self) -> dict[str, Path]:
        candidates = pd.read_csv(
            self.input_candidate_pool_path,
            dtype={"trade_date": str, "ts_code": str, "next_trade_date": str, "exit_trade_date": str},
            low_memory=False,
        )
        selected = self.select_daily_candidates(candidates)
        trades = self.build_trade_results(selected)
        equity_curve = self.build_equity_curve(trades)
        summary = self.build_summary(trades, equity_curve)
        yearly = self.build_yearly_report(trades)

        for path in [
            self.output_trades_path,
            self.output_equity_curve_path,
            self.output_summary_path,
            self.output_yearly_path,
        ]:
            mkdir_p(path.parent)

        trades.to_csv(self.output_trades_path, index=False, encoding="utf-8-sig")
        equity_curve.to_csv(self.output_equity_curve_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("回测交易明细已生成: %s, 行数: %s", self.output_trades_path, len(trades))
        self.logger.info("回测资金曲线已生成: %s, 行数: %s", self.output_equity_curve_path, len(equity_curve))
        self.logger.info("回测汇总已生成: %s", self.output_summary_path)
        self.logger.info("回测年度报告已生成: %s", self.output_yearly_path)
        return {
            "trades": self.output_trades_path,
            "equity_curve": self.output_equity_curve_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def select_daily_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        candidates = candidates.copy()
        candidates = candidates[candidates["allow_buy_reliable"] == True].copy()  # noqa: E712
        amount_bucket_rank = {
            "gte_8e8": 4,
            "3e8_8e8": 3,
            "1e8_3e8": 2,
            "lt_1e8": 1,
        }
        candidates["amount_bucket_rank"] = candidates["amount_bucket"].map(amount_bucket_rank).fillna(0)
        candidates = candidates.sort_values(
            ["trade_date", "rule_count", "fill_probability", "sample_count", "amount_bucket_rank"],
            ascending=[True, False, False, False, False],
        )
        selected = candidates.groupby("trade_date").head(self.max_holding_count).copy()
        selected["selected_rank"] = selected.groupby("trade_date").cumcount() + 1
        selected = selected.drop(columns=["amount_bucket_rank"])
        return selected

    def build_trade_results(self, selected: pd.DataFrame) -> pd.DataFrame:
        selected = selected.copy()
        selected["position_pct"] = self.max_position_pct_per_stock
        selected["capital_used"] = self.initial_cash * selected["position_pct"]
        selected["trade_pnl"] = selected["capital_used"] * selected["net_return"]
        return selected

    def build_equity_curve(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily_pnl = trades.groupby("exit_trade_date", dropna=False)["trade_pnl"].sum().reset_index()
        daily_pnl = daily_pnl.rename(columns={"exit_trade_date": "trade_date"})
        daily_pnl = daily_pnl[daily_pnl["trade_date"].notna()].copy()
        daily_pnl = daily_pnl.sort_values("trade_date")
        daily_pnl["equity"] = self.initial_cash + daily_pnl["trade_pnl"].cumsum()
        daily_pnl["daily_return"] = daily_pnl["trade_pnl"] / self.initial_cash
        daily_pnl["running_max"] = daily_pnl["equity"].cummax()
        daily_pnl["drawdown"] = daily_pnl["equity"] / daily_pnl["running_max"] - 1
        return daily_pnl

    def build_summary(self, trades: pd.DataFrame, equity_curve: pd.DataFrame) -> pd.DataFrame:
        returns = trades["net_return"].dropna()
        total_pnl = float(trades["trade_pnl"].sum()) if not trades.empty else 0.0
        final_equity = self.initial_cash + total_pnl
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        row = {
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "total_return": final_equity / self.initial_cash - 1,
            "trade_count": int(len(trades)),
            "trading_days": int(trades["trade_date"].nunique()) if not trades.empty else 0,
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_drawdown": abs(float(equity_curve["drawdown"].min())) if not equity_curve.empty else 0.0,
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
            "max_holding_count": self.max_holding_count,
            "max_position_pct_per_stock": self.max_position_pct_per_stock,
        }
        return pd.DataFrame([row])

    def build_yearly_report(self, trades: pd.DataFrame) -> pd.DataFrame:
        trades = trades.copy()
        trades["year"] = trades["trade_date"].astype(str).str[:4]
        rows = []
        for year, group in trades.groupby("year"):
            returns = group["net_return"].dropna()
            pnl = float(group["trade_pnl"].sum())
            gains = returns[returns > 0]
            losses = returns[returns <= 0]
            rows.append(
                {
                    "year": year,
                    "trade_count": int(len(group)),
                    "trading_days": int(group["trade_date"].nunique()),
                    "year_pnl": pnl,
                    "year_return_on_initial_cash": pnl / self.initial_cash,
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                    "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
                }
            )
        return pd.DataFrame(rows)
