from __future__ import annotations

import argparse
from itertools import combinations, product
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import FactorAnalyzer, NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从大候选池搜索真实执行口径可用策略。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--max-sort-factor-count", type=int, default=None, help="覆盖配置里的最大排序因子数量。")
    parser.add_argument("--limit-rules", type=int, default=None, help="只扫描前 N 个排序规则，用于快速试跑。")
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
    outputs = LargeUniverseRealisticStrategyOptimizer(
        config_path=args.config,
        max_sort_factor_count=args.max_sort_factor_count,
        limit_rules=args.limit_rules,
    ).optimize()
    print("大候选池真实执行口径优化完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class LargeUniverseRealisticStrategyOptimizer:
    """从全量涨停候选池做每日 top1 排序，并按真实执行约束复利回测。"""

    def __init__(
        self,
        config_path: str | Path = "config/config.json",
        max_sort_factor_count: int | None = None,
        limit_rules: int | None = None,
    ) -> None:
        self.project_root = PROJECT_ROOT
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.logger = get_logger("large_universe_realistic_strategy")
        self.opt_config = self.config.get("large_universe_realistic_optimization", {})
        self.output_summary_path = self.project_root / self.opt_config.get(
            "output_summary_path",
            "reports/large_universe_realistic_optimization_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.opt_config.get(
            "output_yearly_path",
            "reports/large_universe_realistic_optimization_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.opt_config.get(
            "output_detail_path",
            "reports/large_universe_realistic_optimization_detail.csv",
        )
        self.input_daily_merged_path = self.project_root / self.opt_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.replay_rule_name = str(self.opt_config.get("replay_rule", "fixed_t2_close"))
        self.replay_max_hold_days = int(self.opt_config.get("replay_max_hold_days", 2))
        self.replay_exit_price_field = str(self.opt_config.get("replay_exit_price_field", "close"))
        self.initial_cash = float(self.opt_config.get("initial_cash", 500000))
        self.position_pct = float(self.opt_config.get("position_pct", 0.8))
        self.max_buy_amount_ratio = float(self.opt_config.get("max_buy_amount_ratio", 0.05))
        self.min_executed_trades = int(self.opt_config.get("min_executed_trades", 350))
        self.target_equity_multiple = float(self.opt_config.get("target_equity_multiple", 300.0))
        self.max_sort_factor_count = max_sort_factor_count or int(self.opt_config.get("max_sort_factor_count", 2))
        self.limit_rules = limit_rules
        self.max_detail_scenarios = int(self.opt_config.get("max_detail_scenarios", 20))
        self.evaluation_years = [str(year) for year in self.opt_config.get("evaluation_years", [])]
        self.candidate_sort_columns = list(self.opt_config.get("candidate_sort_columns", []))
        self.base_inclusions = dict(self.opt_config.get("base_inclusions", {}))
        self.base_exclusions = dict(self.opt_config.get("base_exclusions", {}))
        self.start_date = self.normalize_date(self.opt_config.get("start_date", ""))
        self.end_date = self.normalize_date(self.opt_config.get("end_date", ""))
        risk_config = self.config.get("risk", {})
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def optimize(self) -> dict[str, Path]:
        candidates = self.load_candidates()
        replayed = self.replay_candidates(candidates)
        replayed = self.attach_daily_liquidity(replayed)
        sort_rules = self.generate_sort_rules(replayed)
        if self.limit_rules:
            sort_rules = sort_rules[: self.limit_rules]

        self.logger.info(
            "开始大候选池真实执行搜索，候选: %s, 交易日: %s, 排序规则: %s",
            len(replayed),
            replayed["trade_date"].nunique(),
            len(sort_rules),
        )

        scored: list[tuple[dict[str, Any], pd.DataFrame]] = []
        for index, (sort_columns, ascending) in enumerate(sort_rules, start=1):
            selected = self.select_daily_top(replayed, sort_columns, ascending)
            simulated = self.simulate_single_position(selected, sort_columns, ascending)
            scored.append((self.summarize_scenario(simulated, sort_columns, ascending), simulated))
            if index % 100 == 0:
                self.logger.info("大候选池真实执行搜索进度: %s/%s", index, len(sort_rules))

        if not scored:
            raise RuntimeError("没有可评估的排序规则，请检查候选池和排序字段。")

        summary = pd.DataFrame([item[0] for item in scored]).sort_values(
            [
                "hit_user_target",
                "ranking_score",
                "equity_multiple",
                "executed_trade_count",
                "max_drawdown",
            ],
            ascending=[False, False, False, False, True],
        ).reset_index(drop=True)

        yearly_rows: list[dict[str, Any]] = []
        detail_frames: list[pd.DataFrame] = []
        keep_scenarios = set(summary.head(self.max_detail_scenarios)["scenario"].astype(str))
        for scenario_summary, simulated in scored:
            scenario_name = str(scenario_summary["scenario"])
            if scenario_name not in keep_scenarios:
                continue
            scenario_rank = int(summary.index[summary["scenario"] == scenario_name][0]) + 1
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario_name))
            detail = simulated.copy()
            detail["scenario_rank"] = scenario_rank
            detail_frames.append(detail)

        yearly = pd.DataFrame(yearly_rows)
        detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("大候选池真实执行优化汇总已生成: %s, 行数: %s", self.output_summary_path, len(summary))
        self.logger.info("大候选池真实执行优化年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("大候选池真实执行优化明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_candidates(self) -> pd.DataFrame:
        optimizer = StrategyConditionOptimizer(
            config_path=self.config_path,
            optimization_config_key="large_universe_realistic_optimization",
        )
        candidates = optimizer.load_trades()
        candidates = self.apply_date_filter(candidates)
        candidates = self.apply_base_filters(candidates)
        candidates = self.ensure_sort_columns(candidates)
        if candidates.empty:
            raise RuntimeError("大候选池为空，请检查 next_day_premium_trades.csv 和配置过滤条件。")
        return candidates.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def apply_date_filter(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        result["trade_date"] = result["trade_date"].map(self.normalize_date)
        if self.start_date:
            result = result[result["trade_date"] >= self.start_date].copy()
        if self.end_date:
            result = result[result["trade_date"] <= self.end_date].copy()
        return result

    def apply_base_filters(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        for column, values in self.base_inclusions.items():
            allowed = {str(value) for value in values}
            result = result[result[column].fillna("missing").astype(str).isin(allowed)].copy()
        for column, values in self.base_exclusions.items():
            excluded = {str(value) for value in values}
            result = result[~result[column].fillna("missing").astype(str).isin(excluded)].copy()
        return result

    def ensure_sort_columns(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        if "first_time_minutes" not in result.columns and "first_time" in result.columns:
            result["first_time_minutes"] = result["first_time"].apply(FactorAnalyzer.parse_time_to_minutes)
        for column in self.candidate_sort_columns:
            if column not in result.columns:
                result[column] = pd.NA
            result[column] = pd.to_numeric(result[column], errors="coerce")
        return result

    def replay_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        replay_engine = ConservativeTradeReplay(config_path=self.config_path)
        replay_engine.position_pct = self.position_pct
        replay_engine.max_hold_days = max(replay_engine.max_hold_days, self.replay_max_hold_days)
        forward_prices = replay_engine.load_forward_prices()
        replay_rule = ReplayRule(
            rule_name=self.replay_rule_name,
            max_hold_days=self.replay_max_hold_days,
            exit_price_field=self.replay_exit_price_field,
        )
        samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        replayed = replay_engine.replay_rule(samples, replay_rule)
        replayed = replayed.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        self.logger.info(
            "候选池日线保守回放完成，候选: %s, 买入被拒: %s, 卖出未完成: %s",
            len(replayed),
            int((replayed["buy_executed"] == False).sum()),  # noqa: E712
            int(((replayed["buy_executed"] == True) & (replayed["sell_executed"] == False)).sum()),  # noqa: E712
        )
        return replayed

    def attach_daily_liquidity(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily_amount_lookup_path = self.project_root / self.opt_config.get(
            "daily_amount_lookup_path",
            "data/processed/daily_amount_lookup.csv",
        )
        if daily_amount_lookup_path.exists():
            self.logger.info("使用日成交额轻量查询表: %s", daily_amount_lookup_path)
            daily = pd.read_csv(
                daily_amount_lookup_path,
                dtype={"trade_date": str, "ts_code": str},
                usecols=["trade_date", "ts_code", "amount_yuan"],
                low_memory=False,
            )
        else:
            self.logger.info("日成交额轻量查询表不存在，回退读取日线合并表: %s", self.input_daily_merged_path)
            daily = pd.read_csv(
                self.input_daily_merged_path,
                dtype={"trade_date": str, "ts_code": str},
                usecols=["trade_date", "ts_code", "amount"],
                low_memory=False,
            )
            daily["amount_yuan"] = pd.to_numeric(daily["amount"], errors="coerce") * 1000
        daily["trade_date"] = daily["trade_date"].map(self.normalize_date)
        buy_daily = daily.rename(
            columns={"trade_date": "buy_trade_date", "amount_yuan": "buy_day_amount_yuan"}
        )[["buy_trade_date", "ts_code", "buy_day_amount_yuan"]]
        sell_daily = daily.rename(
            columns={"trade_date": "exit_trade_date", "amount_yuan": "sell_day_amount_yuan"}
        )[["exit_trade_date", "ts_code", "sell_day_amount_yuan"]]
        merged = trades.merge(buy_daily, on=["buy_trade_date", "ts_code"], how="left", validate="many_to_one")
        merged = merged.merge(sell_daily, on=["exit_trade_date", "ts_code"], how="left", validate="many_to_one")
        return merged

    def generate_sort_rules(self, candidates: pd.DataFrame) -> list[tuple[list[str], list[bool]]]:
        available_columns = [
            column
            for column in self.candidate_sort_columns
            if column in candidates.columns and candidates[column].notna().any()
        ]
        rules: list[tuple[list[str], list[bool]]] = []
        for factor_count in range(1, self.max_sort_factor_count + 1):
            for columns in combinations(available_columns, factor_count):
                for ascending_flags in product([True, False], repeat=factor_count):
                    rules.append((list(columns), list(ascending_flags)))
        if not rules:
            raise RuntimeError("没有可用排序字段，请检查 large_universe_realistic_optimization.candidate_sort_columns。")
        return rules

    def select_daily_top(self, candidates: pd.DataFrame, sort_columns: list[str], ascending: list[bool]) -> pd.DataFrame:
        selected = candidates.sort_values(
            ["trade_date"] + sort_columns,
            ascending=[True] + ascending,
            na_position="last",
        )
        selected = selected.groupby("trade_date", as_index=False).head(1).copy()
        selected["selected_rank"] = 1
        return selected.sort_values(["buy_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def simulate_single_position(
        self,
        selected: pd.DataFrame,
        sort_columns: list[str],
        ascending: list[bool],
    ) -> pd.DataFrame:
        equity = self.initial_cash
        occupied_until = ""
        rows: list[dict[str, Any]] = []
        trade_order = 0
        scenario = self.format_sort_rule(sort_columns, ascending)
        for _, row in selected.iterrows():
            buy_trade_date = self.normalize_date(row.get("buy_trade_date", ""))
            if occupied_until and buy_trade_date <= occupied_until:
                rows.append(self.build_skipped_result(row, scenario, equity, "position_occupied"))
                continue
            if not bool(row.get("buy_executed", False)):
                rows.append(self.build_skipped_result(row, scenario, equity, str(row.get("buy_reject_reason", "buy_not_executed"))))
                continue
            if not bool(row.get("sell_executed", False)) or pd.isna(row.get("exit_price_before_slippage")):
                rows.append(self.build_skipped_result(row, scenario, equity, str(row.get("sell_reject_reason", "sell_not_executed"))))
                continue
            trade_order += 1
            result = self.build_trade_result(row, scenario, equity_before=equity, trade_order=trade_order)
            equity = float(result["equity_after"])
            occupied_until = self.normalize_date(row.get("exit_trade_date", ""))
            rows.append(result)
        return pd.DataFrame(rows)

    def build_trade_result(
        self,
        row: pd.Series,
        scenario: str,
        equity_before: float,
        trade_order: int,
    ) -> dict[str, Any]:
        target_buy_amount = equity_before * self.position_pct
        buy_day_amount = float(row["buy_day_amount_yuan"]) if pd.notna(row["buy_day_amount_yuan"]) else 0.0
        actual_buy_amount = min(target_buy_amount, buy_day_amount * self.max_buy_amount_ratio)
        actual_position_pct = actual_buy_amount / equity_before if equity_before > 0 else 0.0
        buy_amount_ratio = actual_buy_amount / buy_day_amount if buy_day_amount > 0 else 0.0
        buy_slippage = self.estimate_slippage_rate(buy_amount_ratio)
        buy_price = float(row["buy_price_before_slippage"]) * (1 + buy_slippage)

        sell_price_before_slippage = float(row["exit_price_before_slippage"])
        sell_value_before_slippage = actual_buy_amount * (sell_price_before_slippage / buy_price)
        sell_day_amount = float(row["sell_day_amount_yuan"]) if pd.notna(row["sell_day_amount_yuan"]) else 0.0
        sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
        sell_slippage = self.estimate_slippage_rate(sell_amount_ratio)
        sell_price = sell_price_before_slippage * (1 - sell_slippage)

        dynamic_net_return = sell_price / buy_price - 1 - self.fee_rate_without_slippage
        dynamic_account_return = dynamic_net_return * actual_position_pct
        equity_after = equity_before * (1 + dynamic_account_return)
        result = row.to_dict()
        result.update(
            {
                "scenario": scenario,
                "trade_order": trade_order,
                "scenario_executed": True,
                "skip_reason": "",
                "equity_before": equity_before,
                "target_buy_amount": target_buy_amount,
                "actual_buy_amount": actual_buy_amount,
                "actual_position_pct": actual_position_pct,
                "buy_amount_ratio": buy_amount_ratio,
                "dynamic_buy_slippage_rate": buy_slippage,
                "dynamic_buy_price": buy_price,
                "sell_value_before_slippage": sell_value_before_slippage,
                "sell_amount_ratio": sell_amount_ratio,
                "dynamic_sell_slippage_rate": sell_slippage,
                "dynamic_sell_price": sell_price,
                "dynamic_net_return": dynamic_net_return,
                "dynamic_account_return": dynamic_account_return,
                "equity_after": equity_after,
            }
        )
        return result

    @staticmethod
    def build_skipped_result(row: pd.Series, scenario: str, equity: float, skip_reason: str) -> dict[str, Any]:
        result = row.to_dict()
        result.update(
            {
                "scenario": scenario,
                "trade_order": pd.NA,
                "scenario_executed": False,
                "skip_reason": skip_reason,
                "equity_before": equity,
                "target_buy_amount": 0.0,
                "actual_buy_amount": 0.0,
                "actual_position_pct": 0.0,
                "buy_amount_ratio": 0.0,
                "dynamic_buy_slippage_rate": 0.0,
                "dynamic_buy_price": pd.NA,
                "sell_value_before_slippage": 0.0,
                "sell_amount_ratio": 0.0,
                "dynamic_sell_slippage_rate": 0.0,
                "dynamic_sell_price": pd.NA,
                "dynamic_net_return": 0.0,
                "dynamic_account_return": 0.0,
                "equity_after": equity,
            }
        )
        return result

    def summarize_scenario(
        self,
        simulated: pd.DataFrame,
        sort_columns: list[str],
        ascending: list[bool],
    ) -> dict[str, Any]:
        scenario = self.format_sort_rule(sort_columns, ascending)
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        returns = executed["dynamic_account_return"].dropna()
        equity_curve = executed["equity_after"] / self.initial_cash if len(executed) else pd.Series(dtype=float)
        final_equity = float(executed["equity_after"].iloc[-1]) if len(executed) else self.initial_cash
        equity_multiple = final_equity / self.initial_cash if self.initial_cash else 0.0
        max_drawdown = NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve)
        yearly_returns = self.calculate_yearly_returns(executed)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        hit_user_target = len(executed) >= self.min_executed_trades and equity_multiple >= self.target_equity_multiple
        return {
            "scenario": scenario,
            "sort_columns": ",".join(sort_columns),
            "ascending": ",".join(str(flag) for flag in ascending),
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "equity_multiple": equity_multiple,
            "target_equity_multiple": self.target_equity_multiple,
            "executed_trade_count": int(len(executed)),
            "min_executed_trades": self.min_executed_trades,
            "selected_signal_count": int(len(simulated)),
            "buy_rejected_count": int((simulated["skip_reason"].astype(str) == "open_limit_up_unbuyable").sum()),
            "position_occupied_skip_count": int((simulated["skip_reason"].astype(str) == "position_occupied").sum()),
            "sell_unresolved_count": int((simulated["skip_reason"].astype(str).str.contains("sell|limit_down|missing_exit", regex=True)).sum()),
            "hit_user_target": hit_user_target,
            "ranking_score": self.ranking_score(equity_multiple, max_drawdown, len(executed)),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
            "median_account_return": float(returns.median()) if len(returns) else 0.0,
            "max_drawdown": max_drawdown,
            "max_profit": float(returns.max()) if len(returns) else 0.0,
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "avg_actual_buy_amount": float(executed["actual_buy_amount"].mean()) if len(executed) else 0.0,
            "max_actual_buy_amount": float(executed["actual_buy_amount"].max()) if len(executed) else 0.0,
            "avg_actual_position_pct": float(executed["actual_position_pct"].mean()) if len(executed) else 0.0,
            "avg_buy_slippage": float(executed["dynamic_buy_slippage_rate"].mean()) if len(executed) else 0.0,
            "avg_sell_slippage": float(executed["dynamic_sell_slippage_rate"].mean()) if len(executed) else 0.0,
            "min_year_return": min(yearly_returns.values()) if yearly_returns else 0.0,
            "return_2020": yearly_returns.get("2020", 0.0),
            "return_2021": yearly_returns.get("2021", 0.0),
            "return_2022": yearly_returns.get("2022", 0.0),
            "return_2023": yearly_returns.get("2023", 0.0),
            "return_2024": yearly_returns.get("2024", 0.0),
            "return_2025": yearly_returns.get("2025", 0.0),
            "return_2026": yearly_returns.get("2026", 0.0),
        }

    def build_yearly_rows(self, simulated: pd.DataFrame, scenario: str) -> list[dict[str, Any]]:
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        if executed.empty:
            return []
        executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
        rows: list[dict[str, Any]] = []
        for year, group in executed.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            returns = group["dynamic_account_return"].dropna()
            equity_curve = group["equity_after"] / first_equity if first_equity else pd.Series(dtype=float)
            rows.append(
                {
                    "scenario": scenario,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "first_equity": first_equity,
                    "last_equity": last_equity,
                    "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_account_return": float(returns.median()) if len(returns) else 0.0,
                    "max_loss": float(returns.min()) if len(returns) else 0.0,
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        if executed.empty:
            return {}
        data = executed.copy()
        data["year"] = data["exit_trade_date"].astype(str).str[:4]
        yearly_returns: dict[str, float] = {}
        for year, group in data.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            yearly_returns[str(year)] = last_equity / first_equity - 1 if first_equity else 0.0
        return dict(sorted(yearly_returns.items()))

    def ranking_score(self, equity_multiple: float, max_drawdown: float, executed_count: int) -> float:
        if executed_count < self.min_executed_trades:
            return -1e9 + executed_count
        if equity_multiple <= 0:
            return -1e9
        target_bonus = 10.0 if equity_multiple >= self.target_equity_multiple else 1.0
        count_bonus = 1 + min(executed_count, 700) / 3500
        drawdown_penalty = 1 - min(max_drawdown, 0.95)
        return equity_multiple * drawdown_penalty * count_bonus * target_bonus

    def estimate_slippage_rate(self, amount_ratio: float) -> float:
        if pd.isna(amount_ratio) or amount_ratio <= 0:
            return 0.0
        epsilon = 1e-12
        for tier in self.opt_config.get("slippage_tiers", []):
            max_ratio = tier.get("max_amount_ratio")
            slippage_rate = float(tier.get("slippage_rate", 0.0))
            if max_ratio is None or amount_ratio <= float(max_ratio) + epsilon:
                return slippage_rate
        return 0.0

    @staticmethod
    def format_sort_rule(sort_columns: list[str], ascending: list[bool]) -> str:
        parts = []
        for column, asc in zip(sort_columns, ascending):
            parts.append(f"{column}_{'asc' if asc else 'desc'}")
        return "large_universe_sort_" + ";".join(parts)

    @staticmethod
    def normalize_date(value: object) -> str:
        if pd.isna(value):
            return ""
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        return text


if __name__ == "__main__":
    main()
