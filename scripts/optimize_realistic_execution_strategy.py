from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按真实执行口径重新优化 A5 系列策略。")
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
    outputs = RealisticExecutionStrategyOptimizer(config_path=args.config).optimize()
    print("真实执行口径策略优化完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class RealisticExecutionStrategyOptimizer:
    """用真实执行约束重新搜索过滤条件，不使用固定滑点收益作为目标。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("realistic_execution_strategy_optimization")
        self.opt_config = self.config.get("realistic_execution_strategy_optimization", {})
        self.input_trade_replay_path = self.project_root / self.opt_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.input_daily_merged_path = self.project_root / self.opt_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.output_summary_path = self.project_root / self.opt_config.get(
            "output_summary_path",
            "reports/realistic_execution_strategy_optimization_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.opt_config.get(
            "output_yearly_path",
            "reports/realistic_execution_strategy_optimization_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.opt_config.get(
            "output_detail_path",
            "reports/realistic_execution_strategy_optimization_detail.csv",
        )
        self.replay_rule = str(self.opt_config.get("replay_rule", "fixed_t2_close"))
        self.initial_cash = float(self.opt_config.get("initial_cash", 500000))
        self.position_pct = float(self.opt_config.get("position_pct", 0.8))
        self.max_buy_amount_ratio = float(self.opt_config.get("max_buy_amount_ratio", 0.05))
        self.min_executed_trades = int(self.opt_config.get("min_executed_trades", 180))
        self.top_single_conditions = int(self.opt_config.get("top_single_conditions", 40))
        self.top_pair_conditions = int(self.opt_config.get("top_pair_conditions", 18))
        self.top_triple_conditions = int(self.opt_config.get("top_triple_conditions", 10))
        self.max_report_scenarios = int(self.opt_config.get("max_report_scenarios", 120))
        self.base_a5_r1_skip = dict(self.opt_config.get("base_a5_r1_skip", {}))
        self.split_years = dict(self.opt_config.get("split_years", {}))
        self.candidate_factor_columns = list(self.opt_config.get("candidate_factor_columns", []))
        risk_config = self.config.get("risk", {})
        self.fee_rate_without_slippage = (
            float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("commission_rate", 0.0003))
            + float(risk_config.get("transfer_fee_rate", 0.00001))
            + float(risk_config.get("stamp_tax_rate", 0.001))
        )

    def optimize(self) -> dict[str, Path]:
        trades = self.load_universe()
        trades = self.attach_daily_liquidity(trades)
        scenarios = self.build_scenarios(trades)
        scored = []

        for idx, scenario in enumerate(scenarios, start=1):
            simulated = self.simulate_single_position(trades, scenario)
            summary = self.summarize_scenario(simulated, scenario)
            scored.append((summary, scenario, simulated))
            if idx % 500 == 0:
                self.logger.info("已评估真实执行策略场景: %s/%s", idx, len(scenarios))

        summaries = pd.DataFrame([item[0] for item in scored])
        summaries = summaries.sort_values(
            ["ranking_score", "equity_multiple", "max_drawdown", "executed_trade_count"],
            ascending=[False, False, True, False],
        ).reset_index(drop=True)
        keep_names = set(summaries.head(self.max_report_scenarios)["scenario"].astype(str))

        yearly_rows = []
        detail_frames = []
        scenario_by_name = {str(item[0]["scenario"]): item for item in scored}
        for scenario_name in keep_names:
            summary, scenario, simulated = scenario_by_name[scenario_name]
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario))
            selected = simulated.copy()
            selected["scenario_rank"] = int(summaries.index[summaries["scenario"] == scenario_name][0]) + 1
            detail_frames.append(selected)

        yearly = pd.DataFrame(yearly_rows)
        detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summaries.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("真实执行口径优化汇总已生成: %s, 行数: %s", self.output_summary_path, len(summaries))
        self.logger.info("真实执行口径优化年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("真实执行口径优化明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def load_universe(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        trades = trades[trades["replay_rule"].astype(str) == self.replay_rule].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到回放样本: {self.input_trade_replay_path}")
        for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
            trades[column] = trades[column].map(self.normalize_date)
        numeric_columns = [
            "buy_price_before_slippage",
            "exit_price_before_slippage",
            "daily_return",
            "net_return",
        ]
        for column in numeric_columns:
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
        return trades.sort_values(["buy_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def attach_daily_liquidity(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "amount"],
            low_memory=False,
        )
        daily["trade_date"] = daily["trade_date"].map(self.normalize_date)
        daily["amount_yuan"] = pd.to_numeric(daily["amount"], errors="coerce") * 1000
        buy_daily = daily.rename(
            columns={"trade_date": "buy_trade_date", "amount_yuan": "buy_day_amount_yuan"}
        )[["buy_trade_date", "ts_code", "buy_day_amount_yuan"]]
        sell_daily = daily.rename(
            columns={"trade_date": "exit_trade_date", "amount_yuan": "sell_day_amount_yuan"}
        )[["exit_trade_date", "ts_code", "sell_day_amount_yuan"]]
        merged = trades.merge(buy_daily, on=["buy_trade_date", "ts_code"], how="left", validate="many_to_one")
        merged = merged.merge(sell_daily, on=["exit_trade_date", "ts_code"], how="left", validate="many_to_one")
        return merged

    def build_scenarios(self, trades: pd.DataFrame) -> list[dict[str, Any]]:
        base_scenarios = [
            {"scenario": "A5_base_realistic", "description": "A5 原始信号，真实执行口径。", "conditions": []},
            {
                "scenario": "A5_R1_realistic",
                "description": "A5-R1：跳过 weak + segment neutral，真实执行口径。",
                "conditions": [],
                "skip_groups": [
                    {
                        "mode": "all",
                        "conditions": [("market_sentiment_level", "weak"), ("segment_market_sentiment_level", "neutral")],
                    }
                ],
            },
        ]
        conditions = self.build_candidate_conditions(trades)

        single_scenarios = [
            {
                "scenario": f"skip_{factor}={value}",
                "description": f"跳过 {factor}={value}",
                "conditions": [(factor, value)],
                "condition_mode": "any",
            }
            for factor, value in conditions
        ]
        single_ranked = self.rank_candidate_scenarios(trades, single_scenarios).head(self.top_single_conditions)
        top_single_conditions = [self.parse_condition_key(key) for key in single_ranked["condition_key"].tolist()]
        a5_r1_plus_single_scenarios = [
            self.build_a5_r1_plus_scenario([condition])
            for condition in top_single_conditions
        ]
        a5_r1_plus_single_ranked = self.rank_candidate_scenarios(trades, a5_r1_plus_single_scenarios).head(
            self.top_single_conditions
        )
        top_a5_r1_plus_single_conditions = [
            self.extract_any_conditions_key(key)
            for key in a5_r1_plus_single_ranked["condition_key"].tolist()
        ]

        pair_scenarios = []
        for combo in itertools.combinations(top_single_conditions, 2):
            if combo[0][0] == combo[1][0]:
                continue
            pair_scenarios.append(
                {
                    "scenario": "skip_" + "__or__".join(f"{factor}={value}" for factor, value in combo),
                    "description": "跳过 " + " 或 ".join(f"{factor}={value}" for factor, value in combo),
                    "conditions": list(combo),
                    "condition_mode": "any",
                }
            )
        pair_ranked = self.rank_candidate_scenarios(trades, pair_scenarios).head(self.top_pair_conditions)
        top_pair_conditions = [self.parse_conditions_key(key) for key in pair_ranked["condition_key"].tolist()]
        a5_r1_plus_pair_scenarios = [
            self.build_a5_r1_plus_scenario(combo)
            for combo in top_a5_r1_plus_single_conditions
            if combo
        ]
        a5_r1_pair_seed_conditions = list(dict.fromkeys([condition for combo in top_a5_r1_plus_single_conditions for condition in combo]))
        for combo in itertools.combinations(a5_r1_pair_seed_conditions, 2):
            if combo[0][0] == combo[1][0]:
                continue
            a5_r1_plus_pair_scenarios.append(self.build_a5_r1_plus_scenario(list(combo)))
        a5_r1_plus_pair_ranked = self.rank_candidate_scenarios(trades, a5_r1_plus_pair_scenarios).head(
            self.top_pair_conditions
        )
        top_a5_r1_plus_pair_conditions = [
            self.extract_any_conditions_key(key)
            for key in a5_r1_plus_pair_ranked["condition_key"].tolist()
        ]

        triple_seed_conditions = list(dict.fromkeys([condition for combo in top_pair_conditions for condition in combo]))
        triple_scenarios = []
        for combo in itertools.combinations(triple_seed_conditions, 3):
            factors = [condition[0] for condition in combo]
            if len(set(factors)) != len(factors):
                continue
            triple_scenarios.append(
                {
                    "scenario": "skip_" + "__or__".join(f"{factor}={value}" for factor, value in combo),
                    "description": "跳过 " + " 或 ".join(f"{factor}={value}" for factor, value in combo),
                    "conditions": list(combo),
                    "condition_mode": "any",
                }
            )
        triple_ranked = self.rank_candidate_scenarios(trades, triple_scenarios).head(self.top_triple_conditions)
        a5_r1_triple_seed_conditions = list(
            dict.fromkeys([condition for combo in top_a5_r1_plus_pair_conditions for condition in combo])
        )
        a5_r1_plus_triple_scenarios = []
        for combo in itertools.combinations(a5_r1_triple_seed_conditions, 3):
            factors = [condition[0] for condition in combo]
            if len(set(factors)) != len(factors):
                continue
            a5_r1_plus_triple_scenarios.append(self.build_a5_r1_plus_scenario(list(combo)))
        a5_r1_plus_triple_ranked = self.rank_candidate_scenarios(trades, a5_r1_plus_triple_scenarios).head(
            self.top_triple_conditions
        )
        top_triple_names = set(triple_ranked["scenario"].astype(str).tolist())
        top_pair_names = set(pair_ranked["scenario"].astype(str).tolist())
        top_single_names = set(single_ranked["scenario"].astype(str).tolist())
        top_a5_r1_plus_single_names = set(a5_r1_plus_single_ranked["scenario"].astype(str).tolist())
        top_a5_r1_plus_pair_names = set(a5_r1_plus_pair_ranked["scenario"].astype(str).tolist())
        top_a5_r1_plus_triple_names = set(a5_r1_plus_triple_ranked["scenario"].astype(str).tolist())

        scenarios = (
            base_scenarios
            + [scenario for scenario in single_scenarios if scenario["scenario"] in top_single_names]
            + [scenario for scenario in pair_scenarios if scenario["scenario"] in top_pair_names]
            + [scenario for scenario in triple_scenarios if scenario["scenario"] in top_triple_names]
            + [scenario for scenario in a5_r1_plus_single_scenarios if scenario["scenario"] in top_a5_r1_plus_single_names]
            + [scenario for scenario in a5_r1_plus_pair_scenarios if scenario["scenario"] in top_a5_r1_plus_pair_names]
            + [scenario for scenario in a5_r1_plus_triple_scenarios if scenario["scenario"] in top_a5_r1_plus_triple_names]
        )
        return self.deduplicate_scenarios(scenarios)

    @staticmethod
    def deduplicate_scenarios(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for scenario in scenarios:
            name = str(scenario["scenario"])
            if name in seen:
                continue
            seen.add(name)
            result.append(scenario)
        return result

    def build_a5_r1_plus_scenario(self, extra_conditions: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "scenario": "A5_R1_plus_skip_" + "__or__".join(f"{factor}={value}" for factor, value in extra_conditions),
            "description": "A5-R1 叠加跳过 " + " 或 ".join(f"{factor}={value}" for factor, value in extra_conditions),
            "conditions": [],
            "skip_groups": [
                {
                    "mode": "all",
                    "conditions": [("market_sentiment_level", "weak"), ("segment_market_sentiment_level", "neutral")],
                },
                {
                    "mode": "any",
                    "conditions": extra_conditions,
                },
            ],
        }

    def build_candidate_conditions(self, trades: pd.DataFrame) -> list[tuple[str, str]]:
        conditions = []
        for factor in self.candidate_factor_columns:
            if factor not in trades.columns:
                continue
            counts = trades[factor].fillna("missing").astype(str).value_counts()
            for value, count in counts.items():
                if value in {"unknown", "missing", "nan", "None"}:
                    continue
                if count < 3 or count > len(trades) * 0.7:
                    continue
                conditions.append((factor, str(value)))
        return conditions

    def rank_candidate_scenarios(self, trades: pd.DataFrame, scenarios: list[dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for scenario in scenarios:
            simulated = self.simulate_single_position(trades, scenario)
            summary = self.summarize_scenario(simulated, scenario)
            condition_key = self.scenario_conditions_key(scenario)
            rows.append(
                {
                    "scenario": scenario["scenario"],
                    "condition_key": condition_key,
                    "ranking_score": summary["ranking_score"],
                    "equity_multiple": summary["equity_multiple"],
                    "max_drawdown": summary["max_drawdown"],
                    "executed_trade_count": summary["executed_trade_count"],
                }
            )
        if not rows:
            return pd.DataFrame(columns=["scenario", "condition_key", "ranking_score"])
        return pd.DataFrame(rows).sort_values(
            ["ranking_score", "equity_multiple", "max_drawdown"],
            ascending=[False, False, True],
        )

    def simulate_single_position(self, trades: pd.DataFrame, scenario: dict[str, Any]) -> pd.DataFrame:
        equity = self.initial_cash
        occupied_until = ""
        rows = []
        trade_order = 0
        for _, row in trades.iterrows():
            skip_reason = self.skip_reason(row, scenario, occupied_until)
            if skip_reason:
                rows.append(self.build_skipped_result(row, scenario, equity, skip_reason))
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
            occupied_until = str(row["exit_trade_date"])
            rows.append(result)
        return pd.DataFrame(rows)

    def skip_reason(self, row: pd.Series, scenario: dict[str, Any], occupied_until: str) -> str:
        buy_trade_date = str(row.get("buy_trade_date", ""))
        if occupied_until and buy_trade_date <= occupied_until:
            return "position_occupied"
        for group in scenario.get("skip_groups", []):
            group_conditions = list(group.get("conditions", []))
            group_mode = str(group.get("mode", "any"))
            group_matches = [str(row.get(factor, "missing")) == str(value) for factor, value in group_conditions]
            if group_matches and ((group_mode == "all" and all(group_matches)) or (group_mode != "all" and any(group_matches))):
                return "candidate_filter"
        conditions = list(scenario.get("conditions", []))
        if not conditions:
            return ""
        mode = str(scenario.get("condition_mode", "any"))
        matches = []
        for factor, value in conditions:
            matches.append(str(row.get(factor, "missing")) == str(value))
        if mode == "all":
            return "candidate_filter" if all(matches) else ""
        return "candidate_filter" if any(matches) else ""

    def build_trade_result(
        self,
        row: pd.Series,
        scenario: dict[str, Any],
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

        gross_price_return_before_sell_slippage = float(row["exit_price_before_slippage"]) / buy_price - 1
        sell_value_before_slippage = actual_buy_amount * (1 + gross_price_return_before_sell_slippage)
        sell_day_amount = float(row["sell_day_amount_yuan"]) if pd.notna(row["sell_day_amount_yuan"]) else 0.0
        sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
        sell_slippage = self.estimate_slippage_rate(sell_amount_ratio)
        sell_price = float(row["exit_price_before_slippage"]) * (1 - sell_slippage)

        net_return = sell_price / buy_price - 1 - self.fee_rate_without_slippage
        account_return = net_return * actual_position_pct
        equity_after = equity_before * (1 + account_return)
        result = row.to_dict()
        result.update(
            {
                "scenario": str(scenario["scenario"]),
                "scenario_description": str(scenario.get("description", "")),
                "scenario_conditions": self.scenario_conditions_key(scenario),
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
                "dynamic_net_return": net_return,
                "dynamic_account_return": account_return,
                "equity_after": equity_after,
            }
        )
        return result

    def build_skipped_result(
        self,
        row: pd.Series,
        scenario: dict[str, Any],
        equity: float,
        skip_reason: str,
    ) -> dict[str, Any]:
        result = row.to_dict()
        result.update(
            {
                "scenario": str(scenario["scenario"]),
                "scenario_description": str(scenario.get("description", "")),
                "scenario_conditions": self.scenario_conditions_key(scenario),
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

    def summarize_scenario(self, simulated: pd.DataFrame, scenario: dict[str, Any]) -> dict[str, Any]:
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        returns = executed["dynamic_account_return"].dropna()
        equity_curve = executed["equity_after"] / self.initial_cash if len(executed) else pd.Series(dtype=float)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        final_equity = float(executed["equity_after"].iloc[-1]) if len(executed) else self.initial_cash
        yearly_returns = self.calculate_yearly_returns(executed)
        max_drawdown = NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve)
        equity_multiple = final_equity / self.initial_cash if self.initial_cash else 0.0
        min_year_return = min(yearly_returns.values()) if yearly_returns else 0.0
        ranking_score = self.ranking_score(equity_multiple, max_drawdown, len(executed), min_year_return)
        return {
            "scenario": str(scenario["scenario"]),
            "description": str(scenario.get("description", "")),
            "conditions": self.scenario_conditions_key(scenario),
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "equity_multiple": equity_multiple,
            "total_compound_return": equity_multiple - 1,
            "ranking_score": ranking_score,
            "executed_trade_count": int(len(executed)),
            "skipped_trade_count": int((simulated["scenario_executed"] != True).sum()),  # noqa: E712
            "candidate_filter_skip_count": int((simulated["skip_reason"].astype(str) == "candidate_filter").sum()),
            "position_occupied_skip_count": int((simulated["skip_reason"].astype(str) == "position_occupied").sum()),
            "avg_actual_buy_amount": self.mean(executed["actual_buy_amount"]),
            "max_actual_buy_amount": self.max_value(executed["actual_buy_amount"]),
            "avg_buy_slippage": self.mean(executed["dynamic_buy_slippage_rate"]),
            "avg_sell_slippage": self.mean(executed["dynamic_sell_slippage_rate"]),
            "win_rate": self.win_rate(returns),
            "avg_account_return": self.mean(returns),
            "median_account_return": self.median(returns),
            "max_drawdown": max_drawdown,
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "year_count": len(yearly_returns),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "min_year_return": min_year_return,
        }

    def build_yearly_rows(self, simulated: pd.DataFrame, scenario: dict[str, Any]) -> list[dict[str, Any]]:
        executed = simulated[simulated["scenario_executed"] == True].copy()  # noqa: E712
        rows = []
        if executed.empty:
            return rows
        executed["year"] = executed["exit_trade_date"].astype(str).str[:4]
        for year, group in executed.groupby("year"):
            if not str(year).isdigit():
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            returns = group["dynamic_account_return"].dropna()
            equity_curve = group["equity_after"] / first_equity if first_equity else pd.Series(dtype=float)
            rows.append(
                {
                    "scenario": str(scenario["scenario"]),
                    "year": year,
                    "sample_count": int(len(group)),
                    "first_equity": first_equity,
                    "last_equity": last_equity,
                    "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "max_loss": self.min_value(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                    "avg_actual_buy_amount": self.mean(group["actual_buy_amount"]),
                    "avg_buy_slippage": self.mean(group["dynamic_buy_slippage_rate"]),
                    "avg_sell_slippage": self.mean(group["dynamic_sell_slippage_rate"]),
                }
            )
        return rows

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        if executed.empty:
            return yearly_returns
        sample = executed.copy()
        sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
        for year, group in sample.groupby("year"):
            if not str(year).isdigit():
                continue
            first_equity = float(group["equity_before"].iloc[0])
            last_equity = float(group["equity_after"].iloc[-1])
            yearly_returns[str(year)] = last_equity / first_equity - 1 if first_equity else 0.0
        return yearly_returns

    def ranking_score(
        self,
        equity_multiple: float,
        max_drawdown: float,
        executed_count: int,
        min_year_return: float,
    ) -> float:
        if executed_count < self.min_executed_trades:
            return -1e9 + executed_count
        if equity_multiple <= 0:
            return -1e9
        return (
            equity_multiple
            * (1 - min(max_drawdown, 0.95))
            * (1 + min(max(min_year_return, -0.9), 3.0) * 0.2)
            * (1 + min(executed_count, 350) / 3500)
        )

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
    def conditions_key(conditions: list[tuple[str, str]]) -> str:
        if not conditions:
            return ""
        return "||".join(f"{factor}={value}" for factor, value in conditions)

    @classmethod
    def scenario_conditions_key(cls, scenario: dict[str, Any]) -> str:
        parts = []
        for group in scenario.get("skip_groups", []):
            group_key = cls.conditions_key(list(group.get("conditions", [])))
            if group_key:
                prefix = "A5_R1" if group.get("mode") == "all" else "ANY"
                parts.append(f"{prefix}||{group_key}")
        condition_key = cls.conditions_key(list(scenario.get("conditions", [])))
        if condition_key:
            parts.append(condition_key)
        return ";;".join(parts)

    @staticmethod
    def parse_condition_key(key: str) -> tuple[str, str]:
        factor, value = key.split("=", 1)
        return factor, value

    @classmethod
    def parse_conditions_key(cls, key: str) -> list[tuple[str, str]]:
        if not key:
            return []
        return [cls.parse_condition_key(part) for part in key.split("||")]

    @classmethod
    def extract_any_conditions_key(cls, key: str) -> list[tuple[str, str]]:
        for part in str(key).split(";;"):
            if part.startswith("ANY||"):
                return cls.parse_conditions_key(part.removeprefix("ANY||"))
        return []

    @staticmethod
    def normalize_date(value: object) -> str:
        if pd.isna(value):
            return ""
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        return text

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


if __name__ == "__main__":
    main()
