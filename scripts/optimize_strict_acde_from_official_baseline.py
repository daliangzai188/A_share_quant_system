#!/usr/bin/env python3
"""复现2026-06-30早期D>A>E>C底座的A/E排序研究（历史归档）。

这不是当前A>C>E>D的半年优化入口。脚本显式重建133笔、300.312461倍的
历史底座，只用于复现当时A/E排序发现；直接运行必须加``--legacy-baseline``，
防止把旧信号日占仓顺序生成的结果误当成当前正式认证。

本脚本只做 STRICT_DISCOVERY 研究，不自动修改实盘规则。所有候选都满足：

1. 先显式重建新窗口应用优化前的A/E排序锚点，不受优化后当前配置漂移影响；
2. 每次只替换 D/A/E/C 中的一条腿，另外三腿候选完全冻结；
3. 固定现有入选池、持有期、82.5%仓位、D成交压力折扣、费用、滑点、
   涨跌停、T+1和D>A>E>C单账户占仓顺序；
4. 排序字段只能来自信号日可见数据；
5. 独立腿也按自身持仓期执行单账户占仓回放；只有独立策略机械复利和逐腿
   替换后的组合机械复利都严格提高，才标记双门槛通过。

运行：
    python3 scripts/optimize_strict_acde_from_official_baseline.py --legacy-baseline
"""
from __future__ import annotations

import argparse
from functools import lru_cache
from itertools import combinations, product
import json
import logging
from pathlib import Path
import sys
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    configured_c_condition_profiles,
    configured_c_conditions,
    reject_strategy_risk_mask,
)
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strategy_d_spec import historical_candidate_mask  # noqa: E402
from src.strategy_e import (  # noqa: E402
    apply_e_entry_gate,
    build_r1_universe_from_pool,
    load_e_spec,
    select_e_candidates,
)
from src.strict_asof import STRICT_DISCOVERY, assert_selection_columns_strict  # noqa: E402


OUTPUT_DIR = ROOT / "reports" / "strict_acde_optimization"
DETAIL_PATH = OUTPUT_DIR / "ranking_search.csv"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
TOLERANCE = 1e-12
BASELINE_TRADE_COUNT = 133
BASELINE_EQUITY_MULTIPLE = 300.31246148623836
BASELINE_LEG_COUNTS = {"D": 23, "A": 44, "E": 48, "C": 18}
LOGGER = logging.getLogger("strict_acde_optimizer")

D_RANK_COLUMNS = [
    "_d_open2_priority",
    "fd_amount_to_circ_mv",
    "fill_probability",
    "amount",
    "turnover_rate",
    "circ_mv",
    "first_time",
    "last_time",
    "open_times",
]
AC_RANK_COLUMNS = [
    "profit_source_score",
    "turnover_rate",
    "amount",
    "fill_probability",
    "sample_count",
    "circ_mv",
    "fd_amount_to_circ_mv",
    "volume_ratio",
    "open_times",
    "first_time",
    "last_time",
    "limit_times",
]
E_RANK_COLUMNS = [
    "circ_mv",
    "scenario_rank",
    "turnover_rate",
    "amount",
    "fill_probability",
    "sample_count",
    "volume_ratio",
    "open_times",
    "market_leader_rank",
    "limit_height_rank",
    "fd_amount_to_circ_mv",
    "first_time",
    "last_time",
]


def mechanical_metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    valid = frame[frame["status"].astype(str).eq("OK")].copy()
    values = pd.to_numeric(valid["account_return"], errors="raise").to_numpy(dtype=float)
    if len(values) == 0:
        return {"trade_count": 0, "equity_multiple": 1.0, "max_drawdown": 0.0}
    equity = np.cumprod(1.0 + values)
    drawdown = equity / np.maximum.accumulate(equity) - 1.0
    return {
        "trade_count": int(len(values)),
        "equity_multiple": float(equity[-1]),
        "max_drawdown": float(drawdown.min()),
    }


class FrozenPortfolioReplay:
    """严格回放的缓存实现；语义必须与 strict.replay 完全一致。"""

    def __init__(self) -> None:
        self.trade_dates = strict.baseline_dates()

    @staticmethod
    @lru_cache(maxsize=None)
    def hit_limit_up(trade_date: str, ts_code: str, name: str) -> bool:
        return strict.cert.hit_limit_up(trade_date, ts_code, name)

    def replay(self, maps: dict[str, dict[str, dict[str, Any]]]) -> pd.DataFrame:
        equity = 1.0
        occupied_until = occupied_code = occupied_name = ""
        rows: list[dict[str, Any]] = []
        for signal_date in self.trade_dates:
            if occupied_until and signal_date < occupied_until:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "status": "SKIP_OCCUPIED",
                        "strategy_leg": "",
                        "account_return": 0.0,
                        "equity_after": equity,
                    }
                )
                continue
            blocking_handoff = bool(
                occupied_until
                and signal_date == occupied_until
                and not self.hit_limit_up(signal_date, occupied_code, occupied_name)
            )
            occupied_until = occupied_code = occupied_name = ""
            selected: dict[str, Any] | None = None
            if signal_date in maps["D"] and not blocking_handoff:
                selected = maps["D"][signal_date]
            else:
                for leg in ("A", "E", "C"):
                    if signal_date in maps[leg]:
                        selected = maps[leg][signal_date]
                        break
            if selected is None:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "status": "NO_CANDIDATE",
                        "strategy_leg": "",
                        "account_return": 0.0,
                        "equity_after": equity,
                    }
                )
                continue
            value = float(selected["account_return"])
            equity *= 1.0 + value
            occupied_until = str(selected["exit_date"])
            occupied_code = str(selected["ts_code"])
            occupied_name = str(selected.get("name", ""))
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "EXECUTED",
                    "strategy_leg": str(selected["strategy_leg"]),
                    "ts_code": occupied_code,
                    "name": occupied_name,
                    "exit_date": occupied_until,
                    "account_return": value,
                    "equity_after": equity,
                }
            )
        detail = pd.DataFrame(rows)
        detail["peak_equity"] = detail["equity_after"].cummax()
        detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
        return detail


class OfficialBaselineOptimizer:
    def __init__(self) -> None:
        source, source_audit = strict.source_audit()
        if not bool(source_audit.get("passed")):
            raise RuntimeError("严格as-of源审计失败，拒绝重建优化前锚点")
        self.daily_data = strict.daily_data()
        self.replayer = FrozenPortfolioReplay()
        self.outcome_cache: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        self.source_audit = source_audit

        # A优化前的排序是profit_source_score、turnover_rate降序。
        # C仍从c_strategy.ranking读取冻结排序，因此不会被A的基线重建串扰。
        baseline_ac_config = strict.load_json_config(strict.STRATEGY_CONFIG)
        baseline_ac_config["ranking"]["sort_rule"] = "profit_source_oos_resilient"
        baseline_ac_config["ranking"]["columns"] = [
            "profit_source_score",
            "turnover_rate",
        ]
        baseline_ac_config["ranking"]["ascending"] = [False, False]
        ac = strict.build_ac(strict.STRICT_SOURCE, config_override=baseline_ac_config)

        strategy_d = strict.build_d(source, self.daily_data)
        spec = load_e_spec(ROOT)
        pool = load_historical_bucketed_pool(strict.START, strict.END, 80)
        universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
        # E优化前最终排序为circ_mv升序；不向select_e_candidates
        # 传spec，利用其明确的历史默认排序重建本轮底座。
        e_ranked = select_e_candidates(universe)
        e_picks = e_ranked.groupby("trade_date", as_index=False).head(1).copy()
        e_picks = apply_e_entry_gate(e_picks, spec)
        e_picks["_resolved_hold"] = e_picks["exit_rule"].map(
            lambda value: int(spec["exit_rules"][str(value)]["hold_offset"])
        )
        strategy_e = self.outcome_frame("E", e_picks, 2)

        legs = {
            "D": strategy_d,
            "A": ac[ac["strategy_leg"].eq("A")].copy(),
            "E": strategy_e,
            "C": ac[ac["strategy_leg"].eq("C")].copy(),
        }
        self.baseline_legs = legs
        self.baseline_maps = {
            leg: strict.candidate_map(frame) for leg, frame in legs.items()
        }
        self.baseline_daily = strict.replay(self.baseline_maps, set(legs))
        self.baseline_combo = strict.combo_metrics(self.baseline_daily)
        self.baseline_candidate_pool_metrics = {
            leg: mechanical_metrics(frame) for leg, frame in legs.items()
        }
        self.baseline_leg_metrics = {
            leg: self.standalone_metrics(leg, frame) for leg, frame in legs.items()
        }
        self.results: list[dict[str, Any]] = []
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}
        self.seen_signatures: dict[str, set[tuple[tuple[str, str, int], ...]]] = {
            leg: set() for leg in ("D", "A", "E", "C")
        }

        actual_counts = {
            leg: int(self.baseline_combo["leg_counts"].get(leg, 0))
            for leg in ("D", "A", "E", "C")
        }
        if (
            int(self.baseline_combo["trade_count"]) != BASELINE_TRADE_COUNT
            or abs(
                float(self.baseline_combo["equity_multiple"])
                - BASELINE_EQUITY_MULTIPLE
            ) > TOLERANCE
            or actual_counts != BASELINE_LEG_COUNTS
        ):
            raise RuntimeError(
                "优化前新窗口锚点漂移："
                f"{self.baseline_combo['trade_count']}笔/"
                f"{self.baseline_combo['equity_multiple']}倍/{actual_counts}"
            )

        LOGGER.info(
            "已复现优化前新窗口锚点：window=%s~%s trades=%d multiple=%.12f legs=%s",
            strict.START,
            strict.END,
            int(self.baseline_combo["trade_count"]),
            float(self.baseline_combo["equity_multiple"]),
            actual_counts,
        )

        fast = strict.combo_metrics(self.replayer.replay(self.baseline_maps))
        for key in ("trade_count", "equity_multiple", "max_drawdown"):
            if abs(float(fast[key]) - float(self.baseline_combo[key])) > TOLERANCE:
                raise RuntimeError(f"缓存回放与正式基线不一致：{key}")

    @staticmethod
    def available_columns(frame: pd.DataFrame, requested: Iterable[str]) -> list[str]:
        return [
            column
            for column in requested
            if column in frame.columns and frame[column].notna().any()
        ]

    @staticmethod
    def rank_rules(columns: list[str]) -> Iterable[tuple[list[str], list[bool]]]:
        for count in (1, 2):
            for selected in combinations(columns, count):
                for ascending in product([True, False], repeat=count):
                    yield list(selected), list(ascending)

    @staticmethod
    def variant_name(columns: list[str], ascending: list[bool]) -> str:
        directions = ["asc" if value else "desc" for value in ascending]
        return "+".join(f"{column}:{direction}" for column, direction in zip(columns, directions))

    @staticmethod
    def signature(picks: pd.DataFrame, default_hold: int) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (
                str(row.get("trade_date", row.get("signal_date", ""))),
                str(row["ts_code"]),
                int(row.get("_resolved_hold", default_hold)),
            )
            for _, row in picks.sort_values("trade_date").iterrows()
        )

    def outcome(self, leg: str, row: pd.Series, hold: int) -> dict[str, Any]:
        signal_date = str(row.get("trade_date", row.get("signal_date", "")))
        ts_code = str(row["ts_code"])
        key = (leg, signal_date, ts_code, int(hold))
        cached = self.outcome_cache.get(key)
        if cached is not None:
            return cached
        if leg == "D":
            execution_row = row.copy()
            execution_row["signal_date"] = signal_date
            execution = strict.d_execution(execution_row, self.daily_data)
        else:
            result = trade_return_details(
                signal_date,
                ts_code,
                int(hold),
                name=str(row.get("name", "")),
            )
            value = None
            if result.status == "OK" and result.stock_return is not None:
                value = strict.account_return(result.stock_return, result.exit_date)
            execution = {
                "status": result.status,
                "buy_date": result.buy_date,
                "exit_date": result.exit_date,
                "stock_return_before_fees": result.stock_return,
                "account_return": value,
            }
        record = {
            "signal_date": signal_date,
            "strategy_leg": leg,
            "ts_code": ts_code,
            "name": str(row.get("name", "")),
            "hold_offset": int(hold),
            **execution,
        }
        self.outcome_cache[key] = record
        return record

    def outcome_frame(self, leg: str, picks: pd.DataFrame, default_hold: int) -> pd.DataFrame:
        rows = [
            self.outcome(
                leg,
                row,
                int(row.get("_resolved_hold", default_hold)),
            )
            for _, row in picks.iterrows()
        ]
        if rows:
            return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)
        return pd.DataFrame(
            columns=[
                "signal_date",
                "strategy_leg",
                "ts_code",
                "status",
                "account_return",
                "exit_date",
            ]
        )

    def standalone_metrics(self, leg: str, frame: pd.DataFrame) -> dict[str, Any]:
        """单腿也按真实持仓释放日回放，禁止把重叠候选直接全部连乘。"""

        maps = {name: {} for name in ("D", "A", "E", "C")}
        maps[leg] = strict.candidate_map(frame)
        return strict.combo_metrics(self.replayer.replay(maps))

    def evaluate(self, leg: str, variant: str, frame: pd.DataFrame) -> None:
        candidate_pool_metrics = mechanical_metrics(frame)
        leg_metrics = self.standalone_metrics(leg, frame)
        replacement_maps = dict(self.baseline_maps)
        replacement_maps[leg] = strict.candidate_map(frame)
        combo_detail = self.replayer.replay(replacement_maps)
        combo_metrics = strict.combo_metrics(combo_detail)
        leg_passed = (
            float(leg_metrics["equity_multiple"])
            > float(self.baseline_leg_metrics[leg]["equity_multiple"]) + TOLERANCE
        )
        combo_passed = (
            float(combo_metrics["equity_multiple"])
            > float(self.baseline_combo["equity_multiple"]) + TOLERANCE
        )
        self.results.append(
            {
                "strategy_leg": leg,
                "variant": variant,
                "baseline_leg_trade_count": self.baseline_leg_metrics[leg]["trade_count"],
                "candidate_leg_trade_count": leg_metrics["trade_count"],
                "baseline_leg_equity_multiple": self.baseline_leg_metrics[leg]["equity_multiple"],
                "candidate_leg_equity_multiple": leg_metrics["equity_multiple"],
                "candidate_leg_max_drawdown": leg_metrics["max_drawdown"],
                "candidate_pool_trade_count": candidate_pool_metrics["trade_count"],
                "candidate_pool_equity_multiple": candidate_pool_metrics["equity_multiple"],
                "candidate_pool_max_drawdown": candidate_pool_metrics["max_drawdown"],
                "baseline_combo_trade_count": self.baseline_combo["trade_count"],
                "candidate_combo_trade_count": combo_metrics["trade_count"],
                "baseline_combo_equity_multiple": self.baseline_combo["equity_multiple"],
                "candidate_combo_equity_multiple": combo_metrics["equity_multiple"],
                "candidate_combo_max_drawdown": combo_metrics["max_drawdown"],
                "leg_compound_improved": leg_passed,
                "combo_compound_improved": combo_passed,
                "dual_gate_passed": leg_passed and combo_passed,
            }
        )
        self.frames[(leg, variant)] = frame

    def evaluate_unique(
        self,
        leg: str,
        variant: str,
        picks: pd.DataFrame,
        default_hold: int,
    ) -> None:
        signature = self.signature(picks, default_hold)
        if signature in self.seen_signatures[leg]:
            return
        self.seen_signatures[leg].add(signature)
        self.evaluate(leg, variant, self.outcome_frame(leg, picks, default_hold))

    @staticmethod
    def generator(config: dict[str, Any]) -> PaperCandidateGenerator:
        item = PaperCandidateGenerator(
            strict.STRATEGY_CONFIG,
            input_trades_path=strict.STRICT_SOURCE,
        )
        item.config = config
        item.paper_config = config.get("paper_candidate", {})
        item.risk_thresholds = item.paper_config.get("risk_thresholds", {})
        return item

    def search_d(self, source: pd.DataFrame) -> None:
        pool = source[
            historical_candidate_mask(
                source,
                min_fill_probability=0.80,
                allowed_segments={"sh_main", "sz_main", "chi_next", "star", "bj"},
            )
            & source["trade_date"].between(strict.START, strict.END)
        ].copy()
        pool["_d_open2_priority"] = (
            pd.to_numeric(pool["open_times"], errors="coerce").eq(2).astype(int)
        )
        columns = self.available_columns(pool, D_RANK_COLUMNS)
        assert_selection_columns_strict(
            [column for column in columns if column != "_d_open2_priority"],
            context="OfficialBaselineOptimizer.search_d",
        )
        for rank_columns, ascending in self.rank_rules(columns):
            picks = (
                pool.sort_values(
                    ["trade_date", *rank_columns, "ts_code"],
                    ascending=[True, *ascending, False],
                    na_position="last",
                )
                .groupby("trade_date", as_index=False)
                .head(1)
                .copy()
            )
            self.evaluate_unique(
                "D", self.variant_name(rank_columns, ascending), picks, 2
            )

    def search_ac(self) -> None:
        config = strict.load_json_config(strict.STRATEGY_CONFIG)
        all_candidates = self.generator(config).load_all_candidates()
        settings = [
            ("A", config, 2),
            (
                "C",
                condition_strategy_config(
                    config,
                    configured_c_conditions(config),
                    "strict_c_ranking_search",
                    condition_profiles=configured_c_condition_profiles(config),
                ),
                3,
            ),
        ]
        for leg, leg_config, hold in settings:
            generator = self.generator(leg_config)
            pool = generator.apply_strategy_filters(all_candidates)
            pool = pool[pool["trade_date"].between(strict.START, strict.END)].copy()
            pool["profit_source_score"] = generator.calculate_profit_source_score(pool)
            columns = self.available_columns(pool, AC_RANK_COLUMNS)
            assert_selection_columns_strict(
                [column for column in columns if column != "profit_source_score"],
                context=f"OfficialBaselineOptimizer.search_{leg.lower()}",
            )
            for rank_columns, ascending in self.rank_rules(columns):
                picks = (
                    pool.sort_values(
                        [
                            "trade_date",
                            *rank_columns,
                            "amount",
                            "turnover_rate",
                            "ts_code",
                        ],
                        ascending=[True, *ascending, False, False, True],
                        na_position="last",
                    )
                    .groupby("trade_date", as_index=False)
                    .head(1)
                    .copy()
                )
                if leg == "C":
                    rejected = reject_strategy_risk_mask(picks, config, "c_strategy")
                    picks = picks[~pd.Series(rejected.values, index=picks.index)].copy()
                self.evaluate_unique(
                    leg, self.variant_name(rank_columns, ascending), picks, hold
                )

    def search_e(self) -> None:
        spec = load_e_spec(ROOT)
        pool = load_historical_bucketed_pool(strict.START, strict.END, 80)
        universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
        universe = universe[
            universe["segment_retreat_state_bucket"].astype(str).eq("neutral")
        ].copy()
        columns = self.available_columns(universe, E_RANK_COLUMNS)
        assert_selection_columns_strict(
            columns,
            context="OfficialBaselineOptimizer.search_e",
        )
        for rank_columns, ascending in self.rank_rules(columns):
            picks = (
                universe.sort_values(
                    ["trade_date", *rank_columns, "scenario_rank", "ts_code"],
                    ascending=[True, *ascending, True, True],
                    na_position="last",
                )
                .groupby("trade_date", as_index=False)
                .head(1)
                .copy()
            )
            picks = apply_e_entry_gate(picks, spec)
            picks["_resolved_hold"] = picks["exit_rule"].map(
                lambda value: int(spec["exit_rules"][str(value)]["hold_offset"])
            )
            self.evaluate_unique(
                "E", self.variant_name(rank_columns, ascending), picks, 2
            )

    def best_candidates(self, result: pd.DataFrame) -> dict[str, dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        for leg in ("D", "A", "E", "C"):
            passed = result[
                result["strategy_leg"].eq(leg) & result["dual_gate_passed"]
            ].sort_values(
                ["candidate_combo_equity_multiple", "candidate_leg_equity_multiple"],
                ascending=False,
            )
            if passed.empty:
                best[leg] = {"status": "KEEP_CURRENT", "passing_variant_count": 0}
                continue
            row = passed.iloc[0]
            variant = str(row["variant"])
            frame = self.frames[(leg, variant)]
            full_leg = self.standalone_metrics(leg, frame)
            candidate_pool = strict.return_metrics(
                frame.loc[frame["status"].astype(str).eq("OK"), "account_return"]
            )
            maps = dict(self.baseline_maps)
            maps[leg] = strict.candidate_map(frame)
            full_combo = strict.combo_metrics(self.replayer.replay(maps))
            best[leg] = {
                "status": "DUAL_GATE_PASSED",
                "passing_variant_count": int(len(passed)),
                "variant": variant,
                "leg_metrics": full_leg,
                "candidate_pool_metrics": candidate_pool,
                "combo_metrics": full_combo,
            }
        return best

    def combined_best(self, best: dict[str, dict[str, Any]]) -> dict[str, Any]:
        maps = dict(self.baseline_maps)
        applied: list[str] = []
        for leg in ("D", "A", "E", "C"):
            item = best[leg]
            if item["status"] != "DUAL_GATE_PASSED":
                continue
            variant = str(item["variant"])
            maps[leg] = strict.candidate_map(self.frames[(leg, variant)])
            applied.append(leg)
        metrics = strict.combo_metrics(self.replayer.replay(maps))
        return {
            "applied_legs": applied,
            "metrics": metrics,
            "above_official_baseline": (
                float(metrics["equity_multiple"])
                > float(self.baseline_combo["equity_multiple"]) + TOLERANCE
            ),
        }

    def run(self) -> dict[str, Any]:
        source, audit = strict.source_audit()
        if not bool(audit.get("passed")):
            raise RuntimeError("严格as-of源审计失败，拒绝优化")
        for leg, search in (
            ("D", lambda: self.search_d(source)),
            ("A/C", self.search_ac),
            ("E", self.search_e),
        ):
            before = len(self.results)
            LOGGER.info("开始搜索%s排序变体；只使用信号日可见字段。", leg)
            search()
            LOGGER.info("完成%s搜索：新增%d个唯一候选变体。", leg, len(self.results) - before)

        result = pd.DataFrame(self.results).sort_values(
            ["strategy_leg", "dual_gate_passed", "candidate_combo_equity_multiple"],
            ascending=[True, False, False],
        )
        best = self.best_candidates(result)
        combined = self.combined_best(best)
        for leg in ("D", "A", "E", "C"):
            baseline = self.baseline_leg_metrics[leg]
            selected = best[leg]
            if selected["status"] == "DUAL_GATE_PASSED":
                LOGGER.info(
                    "%s双门槛通过：独立单账户=%d笔/%.12f倍 -> %d笔/%.12f倍；"
                    "单腿替换组合=%.12f倍；variant=%s",
                    leg,
                    int(baseline["trade_count"]),
                    float(baseline["equity_multiple"]),
                    int(selected["leg_metrics"]["trade_count"]),
                    float(selected["leg_metrics"]["equity_multiple"]),
                    float(selected["combo_metrics"]["equity_multiple"]),
                    selected["variant"],
                )
            else:
                LOGGER.info(
                    "%s保持当前规则：独立单账户=%d笔/%.12f倍；无候选同时提高独立腿和组合复利。",
                    leg,
                    int(baseline["trade_count"]),
                    float(baseline["equity_multiple"]),
                )
        payload = {
            "schema_version": 1,
            "research_protocol": STRICT_DISCOVERY,
            "release_eligible": False,
            "window": f"{strict.START}~{strict.END}",
            "baseline_policy": "显式重建新窗口优化前A换手率次排序+E流通市值升序底座",
            "selection_policy": "固定当前入选池，仅搜索1至2个信号日可见排序字段",
            "replacement_policy": "每个候选只替换一腿，另外三腿冻结为正式锚点",
            "official_baseline_combo": self.baseline_combo,
            "official_baseline_legs": self.baseline_leg_metrics,
            "official_baseline_candidate_pools": self.baseline_candidate_pool_metrics,
            "unique_variant_count": int(len(result)),
            "best_by_leg": best,
            "combined_best": combined,
            "limitations": [
                "所有候选都在同一历史窗口发现，属于样本内STRICT_DISCOVERY，存在多重比较和过拟合风险。",
                "本脚本不自动修改实盘配置；通过双门槛也不等于取得LOCKED_OOS、模拟盘或容量认证。",
                "机械复利不代表大资金可按同倍数成交，也不是未来收益承诺。",
            ],
        }
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        result.to_csv(DETAIL_PATH, index=False, encoding="utf-8-sig")
        SUMMARY_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        LOGGER.info(
            "双门槛结论：D=%s A=%s E=%s C=%s；最终组合=%d笔/%.12f倍。",
            best["D"]["status"],
            best["A"]["status"],
            best["E"]["status"],
            best["C"]["status"],
            int(combined["metrics"]["trade_count"]),
            float(combined["metrics"]["equity_multiple"]),
        )
        LOGGER.info("优化摘要：%s；完整变体：%s", SUMMARY_PATH, DETAIL_PATH)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="复现2026-06-30早期D>A>E>C历史排序研究；不是当前组合优化器"
    )
    parser.add_argument(
        "--legacy-baseline",
        action="store_true",
        help="明确确认只复现133笔/300.312461倍历史底座",
    )
    args = parser.parse_args()
    if not args.legacy_baseline:
        parser.error(
            "本脚本是D>A>E>C历史排序研究，当前正式组合为A>C>E>D。"
            "如确需复现旧研究，请显式添加--legacy-baseline；当前统计请运行"
            "scripts/certify_strict_asof_portfolio.py。"
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    payload = OfficialBaselineOptimizer().run()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
