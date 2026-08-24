#!/usr/bin/env python3
"""在当前严格实盘锚点下重新研究策略A。

本脚本只写 ``reports/strategy_a_current_window/20260630`` 研究报告，不修改
``config/strategy_config.json``、发布证书、实盘开关或券商状态。

固定口径：

1. 研究窗口为 2024-06-30 至 2026-06-30；2024-06-30 为非交易日，
   首个实际信号日自然是 2024-07-01；
2. D/E/C、D>A>E>C 单账户占仓顺序、82.5% 仓位、费用、滑点、前复权、
   T+1、涨停买不到和跌停延期卖出全部冻结；
3. 只改变策略A的信号日可见筛选或排序字段；
4. 先用 2024-07-01~2025-06-30 开发段排序候选，再用 2025H2 做验证
   门槛，最后只在 2026H1 检查测试表现；
5. 结果属于 STRICT_DISCOVERY，不自动替换实盘规则。

运行：
    python3 scripts/research_strategy_a_current_window.py
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
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

from scripts import certify_strict_asof_portfolio as certifier  # noqa: E402
from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY, assert_selection_columns_strict  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


LOGGER = logging.getLogger("strategy_a_current_window_research")
OUTPUT_DIR = ROOT / "reports" / "strategy_a_current_window" / "20260630"
VARIANT_PATH = OUTPUT_DIR / "variant_metrics.csv"
PERIOD_PATH = OUTPUT_DIR / "selected_comparison_by_period.csv"
PICKS_PATH = OUTPUT_DIR / "selected_candidate_picks.csv"
TRADES_PATH = OUTPUT_DIR / "selected_candidate_trades.csv"
CHALLENGER_PICKS_PATH = OUTPUT_DIR / "full_window_challenger_picks.csv"
CHALLENGER_TRADES_PATH = OUTPUT_DIR / "full_window_challenger_trades.csv"
FACTOR_PATH = OUTPUT_DIR / "baseline_factor_diagnostics.csv"
QUALITY_PATH = OUTPUT_DIR / "data_quality.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
REPORT_PATH = OUTPUT_DIR / "report.md"

START = strict.START
END = strict.END
DEVELOPMENT_END = "20250630"
VALIDATION_START = "20250701"
VALIDATION_END = "20251231"
TEST_START = "20260101"
TEST_END = END
TOLERANCE = 1e-12
MAX_DRAWDOWN_WORSENING = 0.03
MIN_OK_RATE = 0.95
TOP_DEVELOPMENT_CANDIDATES = 10

PERIODS = {
    "full": (START, END),
    "development": (START, DEVELOPMENT_END),
    "validation_2025h2": (VALIDATION_START, VALIDATION_END),
    "test_2026h1": (TEST_START, TEST_END),
}

# 只搜索有明确交易含义且在信号日可见的字段。竞价、开盘5分钟、资金流和
# 龙虎榜字段在当前A池全缺失，故明确不进入搜索空间。
RANK_FACTOR_COLUMNS = [
    "turnover_rate",
    "amount",
    "circ_mv",
    "fd_amount_to_circ_mv",
    "volume_ratio",
    "open_times",
    "first_time",
    "last_time",
    "market_leader_rank",
    "segment_market_leader_rank",
    "limit_height_rank",
    "segment_limit_height_rank",
    "market_emotion_score",
    "segment_emotion_score",
    "theme_heat_score",
    "theme_heat_rank",
    "theme_leader_rank",
    "theme_height_rank",
    "same_theme_limit_count",
    "limit_up_count",
    "market_limit_down_count",
    "segment_open_rate",
    "amount_ratio_1d",
    "turnover_ratio_1d",
    "prev_pct_chg",
    "prev2_pct_chg",
]

EXCLUSION_FACTOR_COLUMNS = [
    "market_segment",
    "board_type",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
    "market_emotion_state_bucket",
    "market_limit_down_count_bucket",
    "limit_up_count_bucket",
    "first_time_detail_bucket",
    "open_times_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "prev_pct_chg_bucket",
    "amount_ratio_bucket",
    "limit_times_detail_bucket",
    "segment_limit_height_rank_bucket",
    "theme_heat_bucket",
    "theme_heat_rank_bucket",
    "theme_leader_rank_bucket",
    "theme_height_rank_bucket",
    "theme_is_mainline_bucket",
]

CORE_CONDITIONS = {
    "segment_limit_up_count_bucket": "lt_5",
    "market_chain_count_bucket": "8_15",
    "fd_ratio_bucket": "0_5pct_1pct",
}

# 只向相邻区间扩一档，避免一次放开到完全不同的策略母池。
CORE_ADJACENT_EXPANSIONS = {
    "segment_limit_up_count_bucket": ["5_10"],
    "market_chain_count_bucket": ["3_8", "15_30"],
    "fd_ratio_bucket": ["0_3pct_0_5pct", "1pct_2pct"],
}


@dataclass(frozen=True)
class VariantSpec:
    name: str
    family: str
    description: str
    pool_key: str = "current"
    exclude_column: str = ""
    exclude_value: str = ""
    rank_columns: tuple[str, ...] = ("profit_source_score", "limit_times")
    rank_ascending: tuple[bool, ...] = (False, False)


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def _generator(config: dict[str, Any]) -> PaperCandidateGenerator:
    item = PaperCandidateGenerator(
        strict.STRATEGY_CONFIG,
        input_trades_path=strict.STRICT_SOURCE,
    )
    item.config = config
    item.paper_config = config.get("paper_candidate", {})
    item.risk_thresholds = item.paper_config.get("risk_thresholds", {})
    return item


def _rank_daily(
    pool: pd.DataFrame,
    generator: PaperCandidateGenerator,
    columns: Iterable[str],
    ascending: Iterable[bool],
) -> pd.DataFrame:
    columns = list(columns)
    ascending = list(ascending)
    if len(columns) != len(ascending):
        raise ValueError("排序字段和方向数量不一致")
    missing = [column for column in columns if column not in pool.columns and column != "profit_source_score"]
    if missing:
        raise RuntimeError(f"策略A研究排序字段缺失: {missing}")
    assert_selection_columns_strict(
        [column for column in columns if column != "profit_source_score"],
        context="research_strategy_a_current_window.rank_daily",
    )
    rows: list[pd.DataFrame] = []
    for _, daily in pool.groupby("trade_date", sort=True):
        ranked = daily.copy()
        ranked["profit_source_score"] = generator.calculate_profit_source_score(ranked)
        ranked = ranked.sort_values(
            columns + ["amount", "turnover_rate"],
            ascending=ascending + [False, False],
            na_position="last",
        )
        if not ranked.empty:
            rows.append(ranked.head(1))
    if not rows:
        return pd.DataFrame(columns=list(pool.columns) + ["profit_source_score"])
    return pd.concat(rows, ignore_index=True).sort_values("trade_date").reset_index(drop=True)


class StrategyACurrentWindowResearch:
    def __init__(self) -> None:
        self.config = load_json_config(strict.STRATEGY_CONFIG)
        self.generator = _generator(self.config)
        self.source, self.source_audit = strict.source_audit()
        if not bool(self.source_audit.get("passed")):
            raise RuntimeError("严格as-of源审计失败，拒绝研究策略A")

        self.all_candidates = self.generator.load_all_candidates()
        self.all_candidates["trade_date"] = self.all_candidates["trade_date"].astype(str)
        self.window_candidates = self.all_candidates[
            self.all_candidates["trade_date"].between(START, END)
        ].copy()
        self.current_pool = self.generator.apply_strategy_filters(self.all_candidates)
        self.current_pool = self.current_pool[
            self.current_pool["trade_date"].astype(str).between(START, END)
        ].copy()

        # 核心条件扩展时仍保留现行股票池、单项排除和复合排除规则。
        broad = self.generator.apply_universe_filters(self.all_candidates)
        broad = self.generator.apply_exclude_conditions(broad)
        broad = self.generator.apply_exclude_rules(broad)
        self.broad_pool = broad[broad["trade_date"].astype(str).between(START, END)].copy()

        self.data_quality = self._audit_data_quality()
        self._outcome_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._picks_by_variant: dict[str, pd.DataFrame] = {}
        self._outcomes_by_variant: dict[str, pd.DataFrame] = {}
        self._standalone_by_variant: dict[str, pd.DataFrame] = {}
        self._combo_by_variant: dict[str, pd.DataFrame] = {}

        source_audit, baseline_daily, baseline_legs = certifier.build_strict_snapshot()
        if not bool(source_audit.get("passed")):
            raise RuntimeError("当前正式组合源审计失败，拒绝优化")
        self.baseline_daily = baseline_daily
        self.baseline_legs = baseline_legs
        self.baseline_maps = {
            leg: strict.candidate_map(frame) for leg, frame in baseline_legs.items()
        }
        self.baseline_a = baseline_legs["A"].copy()
        self.baseline_a_standalone = strict.replay(
            {"D": {}, "A": self.baseline_maps["A"], "E": {}, "C": {}},
            {"A"},
        )
        self.baseline_metrics = self._period_metrics(
            self.baseline_a_standalone, self.baseline_daily
        )
        self._assert_current_baseline_reproduced()

    def _audit_data_quality(self) -> dict[str, Any]:
        source_window = self.source[self.source["trade_date"].astype(str).between(START, END)].copy()
        critical_columns = {
            *CORE_CONDITIONS,
            "trade_date",
            "ts_code",
            "amount",
            "turnover_rate",
            "limit_times",
            *[
                str(rule.get("column", ""))
                for rule in self.config.get("ranking", {}).get("score_rules", [])
            ],
        }
        missing_critical = {
            column: int(self.current_pool[column].isna().sum())
            for column in sorted(critical_columns)
            if column and column in self.current_pool.columns
        }
        missing_columns = sorted(column for column in critical_columns if column not in self.current_pool.columns)
        unavailable_optional = {
            column: int(self.current_pool[column].isna().sum())
            for column in (
                "auction_strength_score",
                "open_5m_strength_score",
                "sector_moneyflow_score",
                "top_list_net_buy_score",
            )
            if column in self.current_pool.columns
        }
        result = {
            "strict_source_audit": self.source_audit,
            "window": f"{START}~{END}",
            "source_window_rows": int(len(source_window)),
            "source_window_trade_dates": int(source_window["trade_date"].nunique()),
            "source_window_first_date": str(source_window["trade_date"].astype(str).min()),
            "source_window_last_date": str(source_window["trade_date"].astype(str).max()),
            "source_window_duplicate_keys": int(source_window.duplicated(["trade_date", "ts_code"]).sum()),
            "feature_window_rows": int(len(self.window_candidates)),
            "feature_window_duplicate_keys": int(
                self.window_candidates.duplicated(["trade_date", "ts_code"]).sum()
            ),
            "current_pool_rows": int(len(self.current_pool)),
            "current_pool_signal_dates": int(self.current_pool["trade_date"].nunique()),
            "current_pool_first_date": str(self.current_pool["trade_date"].astype(str).min()),
            "current_pool_last_date": str(self.current_pool["trade_date"].astype(str).max()),
            "current_pool_duplicate_keys": int(
                self.current_pool.duplicated(["trade_date", "ts_code"]).sum()
            ),
            "critical_missing_counts": missing_critical,
            "critical_missing_columns": missing_columns,
            "optional_factor_missing_counts": unavailable_optional,
        }
        if result["source_window_duplicate_keys"] or result["feature_window_duplicate_keys"]:
            raise RuntimeError("策略A研究输入存在 trade_date+ts_code 重复键")
        if missing_columns or any(missing_critical.values()):
            raise RuntimeError(
                "策略A当前池关键字段缺失："
                + json.dumps({"columns": missing_columns, "counts": missing_critical}, ensure_ascii=False)
            )
        return result

    def _assert_current_baseline_reproduced(self) -> None:
        columns = tuple(str(value) for value in self.config["ranking"]["columns"])
        ascending = tuple(bool(value) for value in self.config["ranking"]["ascending"])
        picks = _rank_daily(self.current_pool, self.generator, columns, ascending)
        expected = self.baseline_a[["signal_date", "ts_code"]].copy()
        actual = picks[["trade_date", "ts_code"]].rename(columns={"trade_date": "signal_date"})
        expected["signal_date"] = expected["signal_date"].astype(str)
        actual["signal_date"] = actual["signal_date"].astype(str)
        merged = expected.merge(actual, on="signal_date", how="outer", suffixes=("_expected", "_actual"), indicator=True)
        mismatch = merged[
            merged["_merge"].ne("both")
            | merged["ts_code_expected"].ne(merged["ts_code_actual"])
        ]
        if not mismatch.empty:
            raise RuntimeError(
                f"当前策略A候选未复现正式锚点，差异日数={len(mismatch)}"
            )
        outcomes = self._outcomes(picks)
        official = self.baseline_a[self.baseline_a["status"].astype(str).eq("OK")].copy()
        current = outcomes[outcomes["status"].astype(str).eq("OK")].copy()
        official_values = official.set_index("signal_date")["account_return"].sort_index()
        current_values = current.set_index("signal_date")["account_return"].sort_index()
        if not official_values.index.equals(current_values.index) or not np.allclose(
            official_values.to_numpy(float), current_values.to_numpy(float), atol=TOLERANCE, rtol=0
        ):
            raise RuntimeError("当前策略A收益未复现正式锚点")

    def _outcome(self, row: pd.Series) -> dict[str, Any]:
        signal_date = str(row["trade_date"])
        ts_code = str(row["ts_code"])
        key = (signal_date, ts_code)
        cached = self._outcome_cache.get(key)
        if cached is not None:
            return cached
        result = trade_return_details(
            signal_date,
            ts_code,
            2,
            name=str(row.get("name", "")),
        )
        account_return = None
        if result.status == "OK" and result.stock_return is not None:
            account_return = strict.account_return(result.stock_return, result.exit_date)
        record = {
            "signal_date": signal_date,
            "strategy_leg": "A",
            "ts_code": ts_code,
            "name": str(row.get("name", "")),
            "status": result.status,
            "buy_date": result.buy_date,
            "exit_date": result.exit_date,
            "stock_return_before_fees": result.stock_return,
            "account_return": account_return,
        }
        self._outcome_cache[key] = record
        return record

    def _outcomes(self, picks: pd.DataFrame) -> pd.DataFrame:
        rows = [self._outcome(row) for _, row in picks.iterrows()]
        if not rows:
            return pd.DataFrame(
                columns=["signal_date", "strategy_leg", "ts_code", "status", "account_return", "exit_date"]
            )
        return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)

    @staticmethod
    def _period_metrics(
        standalone: pd.DataFrame,
        combo: pd.DataFrame,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for period, (low, high) in PERIODS.items():
            result[period] = {
                "a": strict.combo_metrics(standalone, low, high),
                "combo": strict.combo_metrics(combo, low, high),
            }
        return result

    @staticmethod
    def _flatten_metrics(metrics: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for period, scopes in metrics.items():
            for scope, values in scopes.items():
                for field in (
                    "trade_count",
                    "win_rate",
                    "avg_account_return",
                    "median_account_return",
                    "equity_multiple",
                    "max_drawdown",
                    "max_profit",
                    "max_loss",
                    "profit_loss_ratio",
                    "max_consecutive_losses",
                    "win_rate_wilson_95_lower",
                    "avg_return_bootstrap_95_lower",
                ):
                    row[f"{period}_{scope}_{field}"] = values[field]
        return row

    def _pool_for_spec(self, spec: VariantSpec, pools: dict[str, pd.DataFrame]) -> pd.DataFrame:
        pool = pools[spec.pool_key].copy()
        if spec.exclude_column:
            assert_selection_columns_strict(
                [spec.exclude_column],
                context="research_strategy_a_current_window.exclude",
            )
            pool = pool[
                ~pool[spec.exclude_column].fillna("missing").astype(str).eq(spec.exclude_value)
            ].copy()
        return pool

    def _evaluate(
        self,
        spec: VariantSpec,
        pools: dict[str, pd.DataFrame],
        baseline_picks: pd.DataFrame,
    ) -> dict[str, Any]:
        pool = self._pool_for_spec(spec, pools)
        picks = _rank_daily(
            pool,
            self.generator,
            spec.rank_columns,
            spec.rank_ascending,
        )
        outcomes = self._outcomes(picks)
        a_map = strict.candidate_map(outcomes)
        standalone = strict.replay(
            {"D": {}, "A": a_map, "E": {}, "C": {}},
            {"A"},
        )
        maps = dict(self.baseline_maps)
        maps["A"] = a_map
        combo = strict.replay(maps, {"D", "A", "E", "C"})
        metrics = self._period_metrics(standalone, combo)

        identity = baseline_picks[["trade_date", "ts_code"]].merge(
            picks[["trade_date", "ts_code"]],
            on="trade_date",
            how="outer",
            suffixes=("_baseline", "_variant"),
            indicator=True,
        )
        changed = identity[
            identity["_merge"].ne("both")
            | identity["ts_code_baseline"].ne(identity["ts_code_variant"])
        ]
        ok_count = int(outcomes["status"].astype(str).eq("OK").sum())
        row = {
            "variant": spec.name,
            "family": spec.family,
            "description": spec.description,
            "pool_rows": int(len(pool)),
            "pool_signal_dates": int(pool["trade_date"].nunique()),
            "selected_signal_dates": int(len(picks)),
            "executable_candidate_count": ok_count,
            "candidate_ok_rate": float(ok_count / len(outcomes)) if len(outcomes) else 0.0,
            "changed_signal_dates": int(len(changed)),
            "rank_columns": ";".join(spec.rank_columns),
            "rank_ascending": ";".join(str(value).lower() for value in spec.rank_ascending),
            **self._flatten_metrics(metrics),
        }
        self._picks_by_variant[spec.name] = picks
        self._outcomes_by_variant[spec.name] = outcomes
        self._standalone_by_variant[spec.name] = standalone
        self._combo_by_variant[spec.name] = combo
        return row

    def _core_expansion_pool(self, column: str, value: str) -> pd.DataFrame:
        mask = pd.Series(True, index=self.broad_pool.index)
        for current_column, current_value in CORE_CONDITIONS.items():
            allowed = {current_value}
            if current_column == column:
                allowed.add(value)
            mask &= self.broad_pool[current_column].fillna("missing").astype(str).isin(allowed)
        return self.broad_pool.loc[mask].copy()

    def _relaxed_pool(self, relax: str) -> pd.DataFrame:
        config = copy.deepcopy(self.config)
        if relax == "amount_ratio_0_8_1_2":
            config["candidate_filters"]["exclude_conditions"] = [
                item
                for item in config["candidate_filters"].get("exclude_conditions", [])
                if not (
                    str(item.get("column")) == "amount_ratio_bucket"
                    and str(item.get("value")) == "0_8_1_2"
                )
            ]
        elif relax == "bj_prev_pct_0_3":
            config["candidate_filters"]["exclude_rules"] = [
                item
                for item in config["candidate_filters"].get("exclude_rules", [])
                if str(item.get("name")) != "exclude_bj_prev_pct_0_3"
            ]
        elif relax == "star":
            config["universe"]["exclude_market_segments"] = [
                value
                for value in config["universe"].get("exclude_market_segments", [])
                if str(value) != "star"
            ]
            config["candidate_filters"]["exclude_conditions"] = [
                item
                for item in config["candidate_filters"].get("exclude_conditions", [])
                if not (
                    str(item.get("column")) == "market_segment"
                    and str(item.get("value")) == "star"
                )
            ]
        else:
            raise ValueError(f"未知放宽规则: {relax}")
        generator = _generator(config)
        pool = generator.apply_strategy_filters(self.all_candidates)
        return pool[pool["trade_date"].astype(str).between(START, END)].copy()

    def _variant_space(self) -> tuple[list[VariantSpec], dict[str, pd.DataFrame]]:
        pools: dict[str, pd.DataFrame] = {"current": self.current_pool}
        specs = [
            VariantSpec(
                name="BASELINE_CURRENT_A",
                family="baseline",
                description="当前策略A：profit_source_score降序，再按limit_times降序。",
            )
        ]

        for column in RANK_FACTOR_COLUMNS:
            if column not in self.current_pool.columns:
                continue
            values = pd.to_numeric(self.current_pool[column], errors="coerce")
            if values.notna().sum() < 10 or values.nunique(dropna=True) < 2:
                continue
            for direction, ascending in (("asc", True), ("desc", False)):
                specs.append(
                    VariantSpec(
                        name=f"RANK_ADD__{column}__{direction}",
                        family="rank_add_factor",
                        description=(
                            "保留当前收益来源分和连板高度排序，再增加"
                            f"{column}作为第三排序键（{direction}）。"
                        ),
                        rank_columns=("profit_source_score", "limit_times", column),
                        rank_ascending=(False, False, ascending),
                    )
                )

        for column in EXCLUSION_FACTOR_COLUMNS:
            if column not in self.current_pool.columns:
                continue
            assert_selection_columns_strict(
                [column],
                context="research_strategy_a_current_window.variant_space",
            )
            values = self.current_pool[column].fillna("missing").astype(str)
            for value, count in values.value_counts().items():
                if int(count) < 5:
                    continue
                remaining = self.current_pool.loc[~values.eq(value)]
                if remaining["trade_date"].nunique() < 40:
                    continue
                specs.append(
                    VariantSpec(
                        name=f"EXCLUDE__{column}__{value}",
                        family="single_bucket_exclusion",
                        description=f"在当前策略A池上额外排除 {column}={value}。",
                        exclude_column=column,
                        exclude_value=value,
                    )
                )

        for column, adjacent_values in CORE_ADJACENT_EXPANSIONS.items():
            assert_selection_columns_strict(
                [column],
                context="research_strategy_a_current_window.core_expansion",
            )
            for value in adjacent_values:
                key = f"expand::{column}::{value}"
                pools[key] = self._core_expansion_pool(column, value)
                specs.append(
                    VariantSpec(
                        name=f"EXPAND__{column}__{value}",
                        family="adjacent_core_expansion",
                        description=(
                            f"核心条件 {column} 在当前 {CORE_CONDITIONS[column]} 基础上"
                            f"相邻扩展 {value}，其余条件不变。"
                        ),
                        pool_key=key,
                    )
                )
                fallback_key = f"fallback::{column}::{value}"
                current_dates = set(self.current_pool["trade_date"].astype(str))
                fallback_only = pools[key][
                    ~pools[key]["trade_date"].astype(str).isin(current_dates)
                ].copy()
                pools[fallback_key] = pd.concat(
                    [self.current_pool, fallback_only],
                    ignore_index=True,
                ).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
                specs.append(
                    VariantSpec(
                        name=f"FALLBACK__{column}__{value}",
                        family="adjacent_core_fallback",
                        description=(
                            f"保持当前 {column}={CORE_CONDITIONS[column]} 候选绝对优先；"
                            f"仅在当天当前池为空时，用相邻区间 {value} 补充候选。"
                        ),
                        pool_key=fallback_key,
                    )
                )

        for relax, description in (
            ("amount_ratio_0_8_1_2", "放回当前排除的成交额倍率0.8~1.2区间。"),
            ("bj_prev_pct_0_3", "放回北交所且前一日涨幅0~3%的候选。"),
            ("star", "放回科创板候选；仅作策略条件研究，不代表账户权限门槛。"),
        ):
            key = f"relax::{relax}"
            pools[key] = self._relaxed_pool(relax)
            specs.append(
                VariantSpec(
                    name=f"RELAX__{relax}",
                    family="relax_existing_filter",
                    description=description,
                    pool_key=key,
                )
            )
        return specs, pools

    def _add_comparison_and_gates(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        baseline = result[result["variant"].eq("BASELINE_CURRENT_A")].iloc[0]
        for period in PERIODS:
            for scope in ("a", "combo"):
                column = f"{period}_{scope}_equity_multiple"
                result[f"{period}_{scope}_compound_uplift"] = (
                    result[column] / float(baseline[column]) - 1.0
                )

        def segment_pass(row: pd.Series, period: str, *, strict_improvement: bool) -> bool:
            base_a_count = int(baseline[f"{period}_a_trade_count"])
            minimum_count = max(5, int(np.floor(base_a_count * 0.70)))
            a_multiple = float(row[f"{period}_a_equity_multiple"])
            base_a_multiple = float(baseline[f"{period}_a_equity_multiple"])
            combo_multiple = float(row[f"{period}_combo_equity_multiple"])
            base_combo_multiple = float(baseline[f"{period}_combo_equity_multiple"])
            if strict_improvement:
                compound_passed = bool(
                    a_multiple > base_a_multiple + TOLERANCE
                    and combo_multiple > base_combo_multiple + TOLERANCE
                )
            else:
                compound_passed = bool(
                    a_multiple >= base_a_multiple * 0.98 - TOLERANCE
                    and combo_multiple >= base_combo_multiple * 0.98 - TOLERANCE
                )
            return bool(
                int(row[f"{period}_a_trade_count"]) >= minimum_count
                and compound_passed
                and float(row[f"{period}_a_max_drawdown"])
                >= float(baseline[f"{period}_a_max_drawdown"]) - MAX_DRAWDOWN_WORSENING
            )

        result["development_gate_passed"] = result.apply(
            lambda row: segment_pass(row, "development", strict_improvement=True), axis=1
        )
        result["validation_gate_passed"] = result.apply(
            lambda row: segment_pass(row, "validation_2025h2", strict_improvement=False), axis=1
        )
        result["test_gate_passed"] = result.apply(
            lambda row: segment_pass(row, "test_2026h1", strict_improvement=True), axis=1
        )
        result["full_gate_passed"] = result.apply(
            lambda row: bool(
                segment_pass(row, "full", strict_improvement=True)
                and float(row["candidate_ok_rate"]) >= MIN_OK_RATE
                and int(row["changed_signal_dates"]) >= 2
            ),
            axis=1,
        )
        result["development_score"] = (
            np.log(
                result["development_a_equity_multiple"]
                / float(baseline["development_a_equity_multiple"])
            )
            + np.log(
                result["development_combo_equity_multiple"]
                / float(baseline["development_combo_equity_multiple"])
            )
        )
        result["all_research_gates_passed"] = (
            result["development_gate_passed"]
            & result["validation_gate_passed"]
            & result["test_gate_passed"]
            & result["full_gate_passed"]
        )
        return result

    @staticmethod
    def choose_sequential_candidate(frame: pd.DataFrame) -> tuple[str, list[str]]:
        """只按开发段排序，验证段只做门槛；测试段不参与选择。"""

        ranked = frame[
            ~frame["variant"].eq("BASELINE_CURRENT_A")
            & frame["development_gate_passed"]
        ].sort_values(
            ["development_score", "development_a_compound_uplift"],
            ascending=False,
        )
        shortlist = ranked.head(TOP_DEVELOPMENT_CANDIDATES)
        names = shortlist["variant"].astype(str).tolist()
        validated = shortlist[shortlist["validation_gate_passed"]]
        if validated.empty:
            return "BASELINE_CURRENT_A", names
        # 顺序已由开发段固定；选择首个验证非劣的方案，不用测试段重排。
        return str(validated.iloc[0]["variant"]), names

    def _factor_diagnostics(self, baseline_picks: pd.DataFrame) -> pd.DataFrame:
        outcomes = self._outcomes_by_variant["BASELINE_CURRENT_A"]
        merged = baseline_picks.merge(
            outcomes[["signal_date", "ts_code", "status", "account_return"]],
            left_on=["trade_date", "ts_code"],
            right_on=["signal_date", "ts_code"],
            how="left",
        )
        rows: list[dict[str, Any]] = []
        for column in EXCLUSION_FACTOR_COLUMNS:
            if column not in merged.columns:
                continue
            for value, group in merged.groupby(merged[column].fillna("missing").astype(str)):
                returns = pd.to_numeric(
                    group.loc[group["status"].astype(str).eq("OK"), "account_return"],
                    errors="coerce",
                ).dropna()
                if returns.empty:
                    continue
                metrics = strict.return_metrics(returns)
                rows.append(
                    {
                        "factor": column,
                        "bucket": str(value),
                        "candidate_count": int(len(group)),
                        **metrics,
                    }
                )
        return pd.DataFrame(rows).sort_values(
            ["factor", "candidate_count", "bucket"], ascending=[True, False, True]
        )

    def _period_comparison(self, selected: str, challenger: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for period in PERIODS:
            variants = list(dict.fromkeys(("BASELINE_CURRENT_A", selected, challenger)))
            for variant in variants:
                metrics = self._period_metrics(
                    self._standalone_by_variant[variant],
                    self._combo_by_variant[variant],
                )[period]
                rows.append(
                    {
                        "period": period,
                        "variant": variant,
                        "a_trade_count": metrics["a"]["trade_count"],
                        "a_win_rate": metrics["a"]["win_rate"],
                        "a_avg_return": metrics["a"]["avg_account_return"],
                        "a_median_return": metrics["a"]["median_account_return"],
                        "a_equity_multiple": metrics["a"]["equity_multiple"],
                        "a_max_drawdown": metrics["a"]["max_drawdown"],
                        "a_profit_loss_ratio": metrics["a"]["profit_loss_ratio"],
                        "a_max_profit": metrics["a"]["max_profit"],
                        "a_max_loss": metrics["a"]["max_loss"],
                        "a_max_consecutive_losses": metrics["a"]["max_consecutive_losses"],
                        "combo_trade_count": metrics["combo"]["trade_count"],
                        "combo_equity_multiple": metrics["combo"]["equity_multiple"],
                        "combo_max_drawdown": metrics["combo"]["max_drawdown"],
                    }
                )
        return pd.DataFrame(rows)

    def _write_report(
        self,
        result: pd.DataFrame,
        selected: str,
        challenger: str,
        shortlist: list[str],
        promotion_passed: bool,
    ) -> None:
        baseline = result[result["variant"].eq("BASELINE_CURRENT_A")].iloc[0]
        candidate = result[result["variant"].eq(selected)].iloc[0]
        challenger_row = result[result["variant"].eq(challenger)].iloc[0]
        verdict = (
            "发现按预设时序门槛仍优于当前基准的研究候选，但仍只能进入冻结模拟盘/前向观察。"
            if promotion_passed
            else "没有发现足以替换当前实盘基准的可信候选；保持当前策略A规则。"
        )
        lines = [
            "# 策略A当前窗口再研究",
            "",
            f"> 结论：{verdict}",
            "",
            "## 固定口径",
            "",
            f"- 窗口：{START}~{END}；开发段至{DEVELOPMENT_END}，验证段为2025H2，测试段为2026H1。",
            "- A为T+1开盘买、T+2收盘卖、82.5%仓位；费用、滑点、前复权、涨跌停和T+1规则不变。",
            "- 组合固定D>A>E>C优先级，只替换A；D/E/C候选冻结。",
            "- 测试段不参与候选排序，研究不修改实盘配置。",
            "",
            "## 数据质量",
            "",
            f"- 严格源：{self.data_quality['strict_source_audit']['row_count']}行，as-of通过={self.data_quality['strict_source_audit']['passed']}。",
            f"- 目标窗源数据：{self.data_quality['source_window_rows']}行/{self.data_quality['source_window_trade_dates']}个交易日，重复键{self.data_quality['source_window_duplicate_keys']}。",
            f"- A特征母池：{self.data_quality['feature_window_rows']}行；当前条件池：{self.data_quality['current_pool_rows']}行/{self.data_quality['current_pool_signal_dates']}日。",
            "- 当前A池的竞价、开盘5分钟、板块资金流和龙虎榜分数字段全部缺失，未进入搜索。",
            "",
            "## 基准与顺序候选",
            "",
            "| 方案 | A执行数 | A胜率 | A平均 | A中位 | A复利 | A最大回撤 | 组合复利 | 组合最大回撤 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for label, row in (
            ("当前基准", baseline),
            ("顺序候选", candidate),
            ("全窗观察候选", challenger_row),
        ):
            lines.append(
                f"| {label} | {int(row['full_a_trade_count'])} | {float(row['full_a_win_rate']):.2%} | "
                f"{float(row['full_a_avg_account_return']):.2%} | {float(row['full_a_median_account_return']):.2%} | "
                f"{float(row['full_a_equity_multiple']):.6f}倍 | {float(row['full_a_max_drawdown']):.2%} | "
                f"{float(row['full_combo_equity_multiple']):.6f}倍 | {float(row['full_combo_max_drawdown']):.2%} |"
            )
        lines.extend(
            [
                "",
                f"- 顺序候选：`{selected}`；{candidate['description']}",
                f"- 开发段前{TOP_DEVELOPMENT_CANDIDATES}名：{', '.join(shortlist) if shortlist else '无'}。",
                f"- 开发/验证/测试/全段门槛：{bool(candidate['development_gate_passed'])}/"
                f"{bool(candidate['validation_gate_passed'])}/{bool(candidate['test_gate_passed'])}/"
                f"{bool(candidate['full_gate_passed'])}。",
                "",
                "## 全窗收益最高的双提升观察候选",
                "",
                f"- 方案：`{challenger}`；{challenger_row['description']}",
                f"- A全窗复利相对基准变化：{float(challenger_row['full_a_compound_uplift']):+.2%}；"
                f"组合全窗复利变化：{float(challenger_row['full_combo_compound_uplift']):+.2%}。",
                f"- 开发/验证/测试/全段门槛：{bool(challenger_row['development_gate_passed'])}/"
                f"{bool(challenger_row['validation_gate_passed'])}/{bool(challenger_row['test_gate_passed'])}/"
                f"{bool(challenger_row['full_gate_passed'])}。",
                "- 该方案开发段落后、随后两个半年段领先，存在阶段切换或近期窗口拟合两种解释；"
                "只能作为前向影子观察候选，不能按全窗最高值直接替换实盘。",
                "",
                "## 风险与发布限制",
                "",
                "- 当前两年窗口及其历史报告已被用于规则研究，因此本结果仍是STRICT_DISCOVERY，不是真正未查看样本外。",
                "- 多候选搜索存在多重比较风险；开发—验证—测试顺序只能降低、不能消除过拟合。",
                "- 机械复利没有模拟资金增长后的盘口容量，不能当作可实现资金预测或未来收益承诺。",
                "- 即使所有研究门槛通过，也必须先冻结规则，完成前向模拟盘、成交容量和发布认证后才能考虑切换。",
                "",
            ]
        )
        REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    def run(self) -> dict[str, Any]:
        specs, pools = self._variant_space()
        baseline_spec = specs[0]
        baseline_picks = _rank_daily(
            self.current_pool,
            self.generator,
            baseline_spec.rank_columns,
            baseline_spec.rank_ascending,
        )
        rows: list[dict[str, Any]] = []
        for position, spec in enumerate(specs, 1):
            LOGGER.info("评估策略A变体 %d/%d: %s", position, len(specs), spec.name)
            rows.append(self._evaluate(spec, pools, baseline_picks))
        result = self._add_comparison_and_gates(pd.DataFrame(rows))
        selected, shortlist = self.choose_sequential_candidate(result)
        selected_row = result[result["variant"].eq(selected)].iloc[0]
        baseline_unsorted = result[result["variant"].eq("BASELINE_CURRENT_A")].iloc[0]
        challenger_pool = result[
            ~result["variant"].eq("BASELINE_CURRENT_A")
            & (result["full_a_equity_multiple"] > float(baseline_unsorted["full_a_equity_multiple"]))
            & (result["full_combo_equity_multiple"] > float(baseline_unsorted["full_combo_equity_multiple"]))
        ].sort_values(
            ["full_combo_equity_multiple", "full_a_equity_multiple"],
            ascending=False,
        )
        challenger = (
            str(challenger_pool.iloc[0]["variant"])
            if not challenger_pool.empty
            else "BASELINE_CURRENT_A"
        )
        promotion_passed = bool(
            selected != "BASELINE_CURRENT_A"
            and selected_row["development_gate_passed"]
            and selected_row["validation_gate_passed"]
            and selected_row["test_gate_passed"]
            and selected_row["full_gate_passed"]
        )

        result = result.sort_values(
            ["development_gate_passed", "development_score", "full_a_equity_multiple"],
            ascending=[False, False, False],
        ).reset_index(drop=True)
        result.to_csv(VARIANT_PATH, index=False, encoding="utf-8-sig")
        self._factor_diagnostics(baseline_picks).to_csv(
            FACTOR_PATH, index=False, encoding="utf-8-sig"
        )
        period = self._period_comparison(selected, challenger)
        period.to_csv(PERIOD_PATH, index=False, encoding="utf-8-sig")

        selected_picks = self._picks_by_variant[selected].copy()
        selected_outcomes = self._outcomes_by_variant[selected].copy()
        selected_picks.merge(
            selected_outcomes,
            left_on=["trade_date", "ts_code"],
            right_on=["signal_date", "ts_code"],
            how="left",
            suffixes=("", "_outcome"),
        ).to_csv(PICKS_PATH, index=False, encoding="utf-8-sig")
        selected_trades = self._combo_by_variant[selected]
        selected_trades[selected_trades["status"].eq("EXECUTED")].to_csv(
            TRADES_PATH, index=False, encoding="utf-8-sig"
        )
        challenger_picks = self._picks_by_variant[challenger].copy()
        challenger_outcomes = self._outcomes_by_variant[challenger].copy()
        challenger_picks.merge(
            challenger_outcomes,
            left_on=["trade_date", "ts_code"],
            right_on=["signal_date", "ts_code"],
            how="left",
            suffixes=("", "_outcome"),
        ).to_csv(CHALLENGER_PICKS_PATH, index=False, encoding="utf-8-sig")
        challenger_trades = self._combo_by_variant[challenger]
        challenger_trades[challenger_trades["status"].eq("EXECUTED")].to_csv(
            CHALLENGER_TRADES_PATH, index=False, encoding="utf-8-sig"
        )
        QUALITY_PATH.write_text(
            json.dumps(self.data_quality, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        baseline_row = result[result["variant"].eq("BASELINE_CURRENT_A")].iloc[0]
        selected_row = result[result["variant"].eq(selected)].iloc[0]
        challenger_row = result[result["variant"].eq(challenger)].iloc[0]
        payload = {
            "schema_version": 1,
            "research_protocol": STRICT_DISCOVERY,
            "release_eligible": False,
            "live_config_modified": False,
            "window": f"{START}~{END}",
            "selection_sequence": {
                "development": f"{START}~{DEVELOPMENT_END}",
                "validation": f"{VALIDATION_START}~{VALIDATION_END}",
                "test": f"{TEST_START}~{TEST_END}",
                "top_development_candidate_limit": TOP_DEVELOPMENT_CANDIDATES,
                "test_used_for_ranking": False,
            },
            "variant_count": int(len(result)),
            "development_shortlist": shortlist,
            "selected_variant": selected,
            "selected_description": str(selected_row["description"]),
            "promotion_research_gates_passed": promotion_passed,
            "full_window_challenger": challenger,
            "full_window_challenger_description": str(challenger_row["description"]),
            "baseline": {
                key: _json_value(value)
                for key, value in baseline_row.to_dict().items()
            },
            "selected": {
                key: _json_value(value)
                for key, value in selected_row.to_dict().items()
            },
            "challenger": {
                key: _json_value(value)
                for key, value in challenger_row.to_dict().items()
            },
            "data_quality": self.data_quality,
            "limitations": [
                "当前窗口及既有报告已参与历史规则研究，本结果属于STRICT_DISCOVERY，不是真正untouched OOS。",
                "多候选搜索有多重比较风险，三段时序验证只能降低、不能消除过拟合。",
                "机械复利未按资金增长动态约束盘口容量，不代表可实现资金收益或未来收益。",
                "脚本不修改实盘配置；通过研究门槛也必须先做冻结规则后的前向模拟和发布认证。",
            ],
        }
        SUMMARY_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_report(
            result,
            selected,
            challenger,
            shortlist,
            promotion_passed,
        )
        LOGGER.info(
            "策略A研究完成：variants=%d selected=%s gates_passed=%s baseline_A=%.6f candidate_A=%.6f",
            len(result),
            selected,
            promotion_passed,
            float(baseline_row["full_a_equity_multiple"]),
            float(selected_row["full_a_equity_multiple"]),
        )
        return payload


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = StrategyACurrentWindowResearch().run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
