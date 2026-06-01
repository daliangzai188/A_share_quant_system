from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


@dataclass(frozen=True)
class RiskOverlayPolicy:
    policy_name: str
    pause_loss_streak: int | None = None
    pause_drawdown: float | None = None
    cooldown_signals: int = 0
    reduce_market_weak: bool = False
    reduce_segment_neutral: bool = False
    reduce_weak_and_segment_neutral: bool = False
    reduce_weak_and_chain_8_15: bool = False
    skip_market_weak: bool = False
    skip_weak_and_segment_neutral: bool = False


class DynamicRiskOverlayEvaluator:
    """在 A5 已成交明细上评估可复现的动态风控开关。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("dynamic_risk_overlay")
        self.overlay_config = self.config.get("dynamic_risk_overlay_evaluation", {})
        self.input_trade_replay_path = self.project_root / self.overlay_config.get(
            "input_trade_replay_path", "reports/trade_replay_report.csv"
        )
        self.output_trade_report_path = self.project_root / self.overlay_config.get(
            "output_trade_report_path", "reports/dynamic_risk_overlay_trades.csv"
        )
        self.output_summary_path = self.project_root / self.overlay_config.get(
            "output_summary_path", "reports/dynamic_risk_overlay_summary.csv"
        )
        self.output_yearly_path = self.project_root / self.overlay_config.get(
            "output_yearly_path", "reports/dynamic_risk_overlay_yearly.csv"
        )
        self.replay_rule = str(self.overlay_config.get("replay_rule", "fixed_t2_close"))
        self.initial_cash = float(self.overlay_config.get("initial_cash", 1000000))
        self.base_position_pct = float(self.overlay_config.get("base_position_pct", 0.8))
        self.reduced_position_pct = float(self.overlay_config.get("reduced_position_pct", 0.4))

    def evaluate(self) -> dict[str, Path]:
        trades = self.load_trades()
        all_trades = []
        summary_rows = []
        yearly_rows = []
        for policy in self.build_policies():
            simulated = self.apply_policy(trades, policy)
            summary_rows.append(self.summarize_policy(policy.policy_name, simulated))
            yearly_rows.extend(self.build_yearly_rows(policy.policy_name, simulated))
            all_trades.append(simulated)
            self.logger.info("完成动态风控策略评估: %s", policy.policy_name)

        trade_report = pd.concat(all_trades, ignore_index=True)
        summary = pd.DataFrame(summary_rows).sort_values(
            ["total_compound_return", "max_drawdown"],
            ascending=[False, True],
        )
        yearly = pd.DataFrame(yearly_rows)

        self.output_trade_report_path.parent.mkdir(parents=True, exist_ok=True)
        trade_report.to_csv(self.output_trade_report_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.info("动态风控交易明细已生成: %s", self.output_trade_report_path)
        self.logger.info("动态风控汇总已生成: %s", self.output_summary_path)
        self.logger.info("动态风控年度报告已生成: %s", self.output_yearly_path)
        return {
            "trade_report": self.output_trade_report_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str},
            low_memory=False,
        )
        trades = trades[
            (trades["replay_rule"].astype(str) == self.replay_rule)
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
            & trades["trade_date"].notna()
            & trades["exit_trade_date"].notna()
            & trades["net_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到可评估的交易: {self.input_trade_replay_path}, replay_rule={self.replay_rule}")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["raw_daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce")
        trades = trades.sort_values(["trade_date", "exit_trade_date", "ts_code"]).reset_index(drop=True)
        trades["signal_sequence"] = range(1, len(trades) + 1)
        return trades

    @staticmethod
    def build_policies() -> list[RiskOverlayPolicy]:
        return [
            RiskOverlayPolicy(policy_name="base_no_overlay"),
            RiskOverlayPolicy(policy_name="pause_after_2_losses_5_signals", pause_loss_streak=2, cooldown_signals=5),
            RiskOverlayPolicy(policy_name="pause_after_3_losses_5_signals", pause_loss_streak=3, cooldown_signals=5),
            RiskOverlayPolicy(policy_name="pause_after_3_losses_10_signals", pause_loss_streak=3, cooldown_signals=10),
            RiskOverlayPolicy(policy_name="pause_after_realized_dd_15pct_5_signals", pause_drawdown=0.15, cooldown_signals=5),
            RiskOverlayPolicy(policy_name="pause_after_realized_dd_20pct_5_signals", pause_drawdown=0.20, cooldown_signals=5),
            RiskOverlayPolicy(policy_name="weak_market_half_position", reduce_market_weak=True),
            RiskOverlayPolicy(policy_name="weak_or_segment_neutral_half_position", reduce_market_weak=True, reduce_segment_neutral=True),
            RiskOverlayPolicy(policy_name="weak_and_segment_neutral_half_position", reduce_weak_and_segment_neutral=True),
            RiskOverlayPolicy(policy_name="weak_and_chain_8_15_half_position", reduce_weak_and_chain_8_15=True),
            RiskOverlayPolicy(policy_name="weak_market_skip", skip_market_weak=True),
            RiskOverlayPolicy(policy_name="weak_and_segment_neutral_skip", skip_weak_and_segment_neutral=True),
            RiskOverlayPolicy(
                policy_name="loss2_or_dd15_pause5",
                pause_loss_streak=2,
                pause_drawdown=0.15,
                cooldown_signals=5,
            ),
            RiskOverlayPolicy(
                policy_name="loss3_or_dd20_pause5",
                pause_loss_streak=3,
                pause_drawdown=0.20,
                cooldown_signals=5,
            ),
            RiskOverlayPolicy(
                policy_name="loss2_or_dd15_pause5_plus_weak_half",
                pause_loss_streak=2,
                pause_drawdown=0.15,
                cooldown_signals=5,
                reduce_market_weak=True,
            ),
        ]

    def apply_policy(self, trades: pd.DataFrame, policy: RiskOverlayPolicy) -> pd.DataFrame:
        rows = []
        realized = []
        pending_realized: list[dict[str, object]] = []
        cooldown_left = 0
        current_equity = 1.0
        realized_peak_equity = 1.0
        consecutive_losses = 0
        last_loss_streak_pause_at = 0
        drawdown_pause_armed = True

        for row in trades.itertuples(index=False):
            signal_date = str(row.trade_date)
            newly_realized, pending_realized = self.pop_realized_before_signal(pending_realized, signal_date)
            for trade in newly_realized:
                current_equity *= 1 + float(trade["daily_return"])
                realized_peak_equity = max(realized_peak_equity, current_equity)
                consecutive_losses = consecutive_losses + 1 if float(trade["daily_return"]) <= 0 else 0
                if consecutive_losses == 0:
                    last_loss_streak_pause_at = 0
                realized.append(trade)

            realized_drawdown = 1 - current_equity / realized_peak_equity if realized_peak_equity > 0 else 0.0
            if policy.pause_drawdown is not None and realized_drawdown < policy.pause_drawdown:
                drawdown_pause_armed = True

            should_pause, reason, cooldown_left, last_loss_streak_pause_at, drawdown_pause_armed = self.resolve_pause_state(
                policy=policy,
                cooldown_left=cooldown_left,
                consecutive_losses=consecutive_losses,
                realized_drawdown=realized_drawdown,
                last_loss_streak_pause_at=last_loss_streak_pause_at,
                drawdown_pause_armed=drawdown_pause_armed,
            )
            if should_pause:
                applied_position_pct = 0.0
            else:
                applied_position_pct = self.resolve_position(row, policy)
                if applied_position_pct == 0:
                    reason = "market_state_skip"
                elif applied_position_pct == self.base_position_pct:
                    reason = "normal"
                else:
                    reason = "reduced_position"

            adjusted_daily_return = float(row.net_return) * applied_position_pct
            output = row._asdict()
            output.update(
                {
                    "risk_policy": policy.policy_name,
                    "applied_position_pct": applied_position_pct,
                    "overlay_action": "skip" if applied_position_pct == 0 else "trade",
                    "overlay_reason": reason,
                    "realized_equity_before_signal": current_equity,
                    "realized_drawdown_before_signal": realized_drawdown,
                    "realized_consecutive_losses_before_signal": consecutive_losses,
                    "adjusted_daily_return": adjusted_daily_return,
                }
            )
            rows.append(output)

            if applied_position_pct > 0:
                pending_realized.append(
                    {
                        "exit_trade_date": str(row.exit_trade_date),
                        "daily_return": adjusted_daily_return,
                    }
                )

        return pd.DataFrame(rows)

    @staticmethod
    def pop_realized_before_signal(
        pending_realized: list[dict[str, object]],
        signal_date: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        newly_realized = []
        still_pending = []
        for trade in pending_realized:
            if str(trade["exit_trade_date"]) < signal_date:
                newly_realized.append(trade)
            else:
                still_pending.append(trade)
        return newly_realized, still_pending

    def resolve_pause_state(
        self,
        policy: RiskOverlayPolicy,
        cooldown_left: int,
        consecutive_losses: int,
        realized_drawdown: float,
        last_loss_streak_pause_at: int,
        drawdown_pause_armed: bool,
    ) -> tuple[bool, str, int, int, bool]:
        if cooldown_left > 0:
            return True, "cooldown", max(cooldown_left - 1, 0), last_loss_streak_pause_at, drawdown_pause_armed
        if (
            policy.pause_loss_streak is not None
            and consecutive_losses >= policy.pause_loss_streak
            and consecutive_losses > last_loss_streak_pause_at
        ):
            next_cooldown_left = max(policy.cooldown_signals - 1, 0)
            return (
                True,
                f"loss_streak_gte_{policy.pause_loss_streak}",
                next_cooldown_left,
                consecutive_losses,
                drawdown_pause_armed,
            )
        if (
            policy.pause_drawdown is not None
            and realized_drawdown >= policy.pause_drawdown
            and drawdown_pause_armed
        ):
            next_cooldown_left = max(policy.cooldown_signals - 1, 0)
            return (
                True,
                f"realized_dd_gte_{policy.pause_drawdown:.0%}",
                next_cooldown_left,
                last_loss_streak_pause_at,
                False,
            )
        return False, "normal", cooldown_left, last_loss_streak_pause_at, drawdown_pause_armed

    def resolve_position(self, row: object, policy: RiskOverlayPolicy) -> float:
        market_sentiment = str(getattr(row, "market_sentiment_level", ""))
        segment_sentiment = str(getattr(row, "segment_market_sentiment_level", ""))
        market_chain = str(getattr(row, "market_chain_count_bucket", ""))
        if policy.skip_market_weak and market_sentiment == "weak":
            return 0.0
        if policy.skip_weak_and_segment_neutral and market_sentiment == "weak" and segment_sentiment == "neutral":
            return 0.0
        if policy.reduce_market_weak and str(getattr(row, "market_sentiment_level", "")) == "weak":
            return self.reduced_position_pct
        if policy.reduce_segment_neutral and segment_sentiment == "neutral":
            return self.reduced_position_pct
        if policy.reduce_weak_and_segment_neutral and market_sentiment == "weak" and segment_sentiment == "neutral":
            return self.reduced_position_pct
        if policy.reduce_weak_and_chain_8_15 and market_sentiment == "weak" and market_chain == "8_15":
            return self.reduced_position_pct
        return self.base_position_pct

    def summarize_policy(self, policy_name: str, trades: pd.DataFrame) -> dict[str, object]:
        traded = trades[trades["overlay_action"] == "trade"].copy()
        returns = traded["adjusted_daily_return"].dropna()
        daily_returns = traded.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        final_equity = self.initial_cash * float(equity_curve.iloc[-1]) if len(equity_curve) else self.initial_cash
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "risk_policy": policy_name,
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "total_compound_return": final_equity / self.initial_cash - 1,
            "signal_count": int(len(trades)),
            "traded_count": int(len(traded)),
            "skipped_count": int((trades["overlay_action"] == "skip").sum()),
            "reduced_count": int((trades["applied_position_pct"] == self.reduced_position_pct).sum()),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
            "median_daily_return": float(returns.median()) if len(returns) else 0.0,
            "max_profit": float(returns.max()) if len(returns) else 0.0,
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
        }

    def build_yearly_rows(self, policy_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        traded = trades[trades["overlay_action"] == "trade"].copy()
        if traded.empty:
            return []
        traded["year"] = traded["exit_trade_date"].astype(str).str[:4]
        rows = []
        for year, group in traded.groupby("year"):
            returns = group["adjusted_daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["adjusted_daily_return"].sum().sort_index()
            rows.append(
                {
                    "risk_policy": policy_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": float((1 + daily_returns).prod() - 1),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_daily_return": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows
