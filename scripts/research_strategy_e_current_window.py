#!/usr/bin/env python3
"""在当前严格实盘锚点下重新研究策略E。

本脚本只写 ``reports/strategy_e_current_window/20260630`` 研究报告，不修改
``config/strategy_e_r1_scenarios.json``、实盘开关、券商状态或发布证书。

固定口径：

1. 窗口为2024-06-30至2026-06-30，首个实际信号日自然为2024-07-01；
2. D/A/C、D>A>E>C优先级、82.5%仓位、费用、滑点、前复权、T+1、
   涨停买不到和跌停延期卖出全部冻结；
3. 只改变E的信号日可见排序、筛选条件或每日第一名之后的无回补门禁；
4. 先用2024-07-01~2025-06-30开发段排序候选，再用2025H2做验证，
   最后只用2026H1检查测试表现；
5. 结果属于STRICT_DISCOVERY，不自动替换实盘规则。

运行：
    python3 scripts/research_strategy_e_current_window.py
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
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
from scripts.optimize_strict_acde_from_official_baseline import (  # noqa: E402
    FrozenPortfolioReplay,
)
from scripts.verify_strategy_e_alignment import (  # noqa: E402
    load_historical_bucketed_pool,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.strategy_e import (  # noqa: E402
    build_r1_universe_from_pool,
    load_e_spec,
    required_signal_fields,
    resolve_exit_offset,
)
from src.strict_asof import (  # noqa: E402
    STRICT_DISCOVERY,
    assert_selection_columns_strict,
)


LOGGER = logging.getLogger("strategy_e_current_window_research")
OUTPUT_DIR = ROOT / "reports" / "strategy_e_current_window" / "20260630"
VARIANT_PATH = OUTPUT_DIR / "variant_metrics.csv"
PERIOD_PATH = OUTPUT_DIR / "selected_comparison_by_period.csv"
PICKS_PATH = OUTPUT_DIR / "selected_candidate_picks.csv"
TRADES_PATH = OUTPUT_DIR / "selected_candidate_trades.csv"
CHALLENGER_PICKS_PATH = OUTPUT_DIR / "full_window_challenger_picks.csv"
CHALLENGER_TRADES_PATH = OUTPUT_DIR / "full_window_challenger_trades.csv"
STABLE_PICKS_PATH = OUTPUT_DIR / "stable_noninferiority_candidate_picks.csv"
STABLE_TRADES_PATH = OUTPUT_DIR / "stable_noninferiority_candidate_trades.csv"
STABLE_E_EXECUTED_PATH = OUTPUT_DIR / "stable_noninferiority_e_executed_trades.csv"
STABLE_COMBO_TRADES_PATH = OUTPUT_DIR / "stable_noninferiority_combo_trades.csv"
STABLE_CHANGED_PATH = OUTPUT_DIR / "stable_noninferiority_changed_signals.csv"
STABLE_COMBO_CHANGED_PATH = OUTPUT_DIR / "stable_noninferiority_combo_changed_trades.csv"
MONTHLY_PATH = OUTPUT_DIR / "stable_noninferiority_monthly_comparison.csv"
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
TOP_DEVELOPMENT_CANDIDATES = 12
DEPLOYED_VARIANT_NAME = "SCORE_TURNOVER_PLUS__amount_ratio_1d__asc"

PERIODS = {
    "full": (START, END),
    "development": (START, DEVELOPMENT_END),
    "validation_2025h2": (VALIDATION_START, VALIDATION_END),
    "test_2026h1": (TEST_START, TEST_END),
}

# 只搜索信号日收盘前已经存在、具备明确交易含义且非恒定的字段。成交结果、
# 次日开盘和退出价格由strict_asof黑名单再次校验，任何误加入都会立即失败。
NUMERIC_FACTORS = [
    "turnover_rate",
    "amount",
    "sample_count",
    "volume_ratio",
    "open_times",
    "market_leader_rank",
    "segment_market_leader_rank",
    "limit_height_rank",
    "segment_limit_height_rank",
    "fd_amount_to_circ_mv",
    "first_time",
    "last_time",
    "circ_mv",
    "limit_times",
    "prev_pct_chg",
    "prev2_pct_chg",
    "amount_ratio_1d",
    "turnover_ratio_1d",
    "market_emotion_score",
    "segment_emotion_score",
    "theme_heat_score",
    "theme_heat_rank",
    "theme_leader_rank",
    "same_theme_limit_count",
]

CATEGORICAL_FACTORS = [
    "market_segment",
    "board_type",
    "first_time_detail_bucket",
    "open_times_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "prev_pct_chg_bucket",
    "amount_ratio_bucket",
    "limit_up_count_bucket",
    "segment_limit_up_count_bucket",
    "market_chain_count_bucket",
    "market_limit_down_count_bucket",
    "market_emotion_state_bucket",
    "theme_heat_bucket",
    "theme_heat_rank_bucket",
    "theme_leader_rank_bucket",
    "theme_height_rank_bucket",
    "theme_is_mainline_bucket",
    "same_theme_limit_count_bucket",
    "segment_market_leader_rank_bucket",
    "segment_limit_height_rank_bucket",
]

CURRENT_STATE_VALUES = ("neutral",)
CURRENT_GATE_EXCLUSIONS = (("first_time_detail_bucket", "1330_1430"),)


@dataclass(frozen=True)
class VariantSpec:
    name: str
    family: str
    description: str
    state_values: tuple[str, ...] = CURRENT_STATE_VALUES
    score_factors: tuple[tuple[str, bool, float], ...] = (
        ("turnover_rate", True, 1.0),
    )
    pre_exclude_column: str = ""
    pre_exclude_value: str = ""
    numeric_filter_column: str = ""
    numeric_filter_operator: str = ""
    numeric_filter_value: float = 0.0
    gate_exclusions: tuple[tuple[str, str], ...] = CURRENT_GATE_EXCLUSIONS


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _max_consecutive_losses(values: np.ndarray) -> int:
    maximum = current = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def _wilson_lower(wins: int, count: int) -> float:
    if count <= 0:
        return 0.0
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(
        p * (1 - p) / count + z * z / (4 * count * count)
    ) / denominator
    return float(center - radius)


def _fast_return_metrics(returns: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(returns, errors="raise").dropna().to_numpy(dtype=float)
    if len(values) == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
            "win_rate_wilson_95_lower": 0.0,
        }
    positive = values[values > 0]
    negative = values[values < 0]
    wins = int((values > 0).sum())
    compound = mechanical_compound(values)
    return {
        "trade_count": int(len(values)),
        "win_rate": float(wins / len(values)),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "equity_multiple": compound.equity_multiple,
        "max_drawdown": compound.max_drawdown,
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_consecutive_losses": _max_consecutive_losses(values),
        "win_rate_wilson_95_lower": _wilson_lower(wins, len(values)),
    }


def _fast_combo_metrics(
    detail: pd.DataFrame,
    low: str,
    high: str,
) -> dict[str, Any]:
    sample = detail[
        detail["signal_date"].astype(str).between(str(low), str(high))
        & detail["status"].astype(str).eq("EXECUTED")
    ].copy()
    result = _fast_return_metrics(sample["account_return"])
    result["leg_counts"] = (
        sample["strategy_leg"].value_counts().sort_index().to_dict()
        if not sample.empty
        else {}
    )
    return result


class StrategyECurrentWindowResearch:
    def __init__(self) -> None:
        self.spec = load_e_spec(ROOT)
        self.source, self.source_audit = strict.source_audit()
        if not bool(self.source_audit.get("passed")):
            raise RuntimeError("严格as-of源审计失败，拒绝研究策略E")

        self.feature_pool = load_historical_bucketed_pool(START, END, 80)
        self.feature_pool["trade_date"] = self.feature_pool["trade_date"].astype(str)
        self.universe = build_r1_universe_from_pool(
            self.feature_pool,
            self.spec,
            audit_readiness=True,
        )
        self.universe["trade_date"] = self.universe["trade_date"].astype(str)
        self.neutral_universe = self.universe[
            self.universe["segment_retreat_state_bucket"].astype(str).eq("neutral")
        ].copy()

        source_audit, baseline_daily, baseline_legs = certifier.build_strict_snapshot()
        if not bool(source_audit.get("passed")):
            raise RuntimeError("当前正式组合源审计失败，拒绝优化E")
        self.baseline_daily = baseline_daily
        self.baseline_legs = baseline_legs
        self.baseline_maps = {
            leg: strict.candidate_map(frame) for leg, frame in baseline_legs.items()
        }
        self.baseline_e = baseline_legs["E"].copy()
        self.replayer = FrozenPortfolioReplay()

        self._outcome_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._picks_by_variant: dict[str, pd.DataFrame] = {}
        self._outcomes_by_variant: dict[str, pd.DataFrame] = {}
        self._standalone_by_variant: dict[str, pd.DataFrame] = {}
        self._combo_by_variant: dict[str, pd.DataFrame] = {}
        self._spec_by_name: dict[str, VariantSpec] = {}
        self.data_quality = self._audit_data_quality()

    def _audit_data_quality(self) -> dict[str, Any]:
        required = sorted(required_signal_fields(self.spec))
        missing_required = sorted(set(required) - set(self.feature_pool.columns))
        required_nulls = {
            column: int(self.neutral_universe[column].isna().sum())
            for column in required
            if column in self.neutral_universe.columns
        }
        factor_availability = {}
        for column in NUMERIC_FACTORS:
            if column not in self.neutral_universe.columns:
                factor_availability[column] = {
                    "available": False,
                    "nonnull_count": 0,
                    "unique_count": 0,
                }
                continue
            values = pd.to_numeric(self.neutral_universe[column], errors="coerce")
            factor_availability[column] = {
                "available": bool(values.notna().any()),
                "nonnull_count": int(values.notna().sum()),
                "unique_count": int(values.nunique(dropna=True)),
            }
        result = {
            "strict_source_path": str(strict.STRICT_SOURCE.relative_to(ROOT)),
            "strict_source_rows": int(len(self.source)),
            "strict_source_asof_passed": bool(self.source_audit.get("passed")),
            "strict_source_duplicate_keys": int(
                self.source.duplicated(["trade_date", "ts_code"]).sum()
            ),
            "feature_pool_rows": int(len(self.feature_pool)),
            "feature_pool_signal_dates": int(self.feature_pool["trade_date"].nunique()),
            "feature_pool_duplicate_keys": int(
                self.feature_pool.duplicated(["trade_date", "ts_code"]).sum()
            ),
            "r1_union_rows": int(len(self.universe)),
            "r1_union_signal_dates": int(self.universe["trade_date"].nunique()),
            "neutral_rows": int(len(self.neutral_universe)),
            "neutral_signal_dates": int(self.neutral_universe["trade_date"].nunique()),
            "segment_retreat_state_counts": self.universe[
                "segment_retreat_state_bucket"
            ].fillna("missing").astype(str).value_counts().to_dict(),
            "required_signal_fields": required,
            "missing_required_signal_fields": missing_required,
            "required_field_null_counts_in_neutral_universe": required_nulls,
            "numeric_factor_availability": factor_availability,
        }
        if (
            result["strict_source_duplicate_keys"]
            or result["feature_pool_duplicate_keys"]
            or missing_required
        ):
            raise RuntimeError(
                "策略E研究数据质量失败：" + json.dumps(result, ensure_ascii=False)
            )
        return result

    def _select_daily(self, variant: VariantSpec) -> pd.DataFrame:
        pool = self.universe[
            self.universe["segment_retreat_state_bucket"]
            .fillna("missing")
            .astype(str)
            .isin(set(variant.state_values))
        ].copy()
        if variant.pre_exclude_column:
            column = variant.pre_exclude_column
            if column not in pool.columns:
                raise RuntimeError(f"E前置排除字段不存在：{column}")
            pool = pool[
                ~pool[column].fillna("missing").astype(str).eq(
                    variant.pre_exclude_value
                )
            ].copy()
        if variant.numeric_filter_column:
            column = variant.numeric_filter_column
            if column not in pool.columns:
                raise RuntimeError(f"E数值过滤字段不存在：{column}")
            values = pd.to_numeric(pool[column], errors="coerce")
            if variant.numeric_filter_operator == "gte":
                keep = values >= float(variant.numeric_filter_value)
            elif variant.numeric_filter_operator == "lte":
                keep = values <= float(variant.numeric_filter_value)
            else:
                raise ValueError(f"未知E数值过滤符：{variant.numeric_filter_operator}")
            pool = pool[keep].copy()

        factor_columns = [column for column, _, _ in variant.score_factors]
        assert_selection_columns_strict(
            factor_columns,
            context="research_strategy_e_current_window.select_daily",
        )
        missing = [column for column in factor_columns if column not in pool.columns]
        if missing:
            raise RuntimeError(f"E排序字段不存在：{missing}")

        # 复合因子必须在相同的完整候选集合上计算日内分位数。先统一剔除
        # 任一排序因子缺失的行，避免后续因子改变候选集合、而早先因子的
        # 分位排名仍沿用旧分母，造成组合得分口径不一致。
        numeric_factors = pd.DataFrame(
            {
                column: pd.to_numeric(pool[column], errors="coerce")
                for column in factor_columns
            },
            index=pool.index,
        )
        pool = pool[numeric_factors.notna().all(axis=1)].copy()
        pool["_variant_score"] = 0.0
        total_weight = 0.0
        for column, higher_is_better, weight in variant.score_factors:
            values = pd.to_numeric(pool[column], errors="raise")
            # percentile分数只在同一信号日的候选之间计算。高值优先时，
            # ascending=True使最大值分数为1；低值优先时ascending=False，
            # 最小值自然得到1。rank已经完成方向处理，不能再次反转。
            score = values.groupby(pool["trade_date"]).rank(
                method="average",
                pct=True,
                ascending=bool(higher_is_better),
            )
            pool["_variant_score"] += score * float(weight)
            total_weight += float(weight)
        if total_weight <= 0:
            raise ValueError("E排序因子权重必须为正")
        pool["_variant_score"] /= total_weight
        ordered = pool.sort_values(
            ["trade_date", "_variant_score", "scenario_rank", "ts_code"],
            ascending=[True, False, True, True],
            na_position="last",
        )
        picks = ordered.groupby("trade_date", as_index=False).head(1).copy()

        # 门禁在每日第一名已经确定后执行，被排除日直接空仓，严禁回补第二名。
        keep = pd.Series(True, index=picks.index)
        for column, value in variant.gate_exclusions:
            if column not in picks.columns:
                raise RuntimeError(f"E入场门禁字段不存在：{column}")
            keep &= ~picks[column].fillna("missing").astype(str).eq(str(value))
        return picks.loc[keep].sort_values("trade_date").reset_index(drop=True)

    def _outcome(self, row: pd.Series) -> dict[str, Any]:
        signal_date = str(row["trade_date"])
        ts_code = str(row["ts_code"])
        hold = resolve_exit_offset(self.spec, str(row["exit_rule"]))
        key = (signal_date, ts_code, hold)
        cached = self._outcome_cache.get(key)
        if cached is not None:
            return cached
        execution = trade_return_details(
            signal_date,
            ts_code,
            hold,
            name=str(row.get("name", "")),
        )
        account_return = None
        if execution.status == "OK" and execution.stock_return is not None:
            account_return = strict.account_return(
                execution.stock_return,
                execution.exit_date,
            )
        record = {
            "signal_date": signal_date,
            "strategy_leg": "E",
            "ts_code": ts_code,
            "name": str(row.get("name", "")),
            "exit_rule": str(row.get("exit_rule", "")),
            "status": execution.status,
            "buy_date": execution.buy_date,
            "exit_date": execution.exit_date,
            "stock_return_before_fees": execution.stock_return,
            "account_return": account_return,
        }
        self._outcome_cache[key] = record
        return record

    def _outcomes(self, picks: pd.DataFrame) -> pd.DataFrame:
        rows = [self._outcome(row) for _, row in picks.iterrows()]
        if not rows:
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
        return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)

    def _replay_e(self, e_map: dict[str, dict[str, Any]]) -> pd.DataFrame:
        return self.replayer.replay(
            {"D": {}, "A": {}, "E": e_map, "C": {}}
        )

    def _replay_combo(self, e_map: dict[str, dict[str, Any]]) -> pd.DataFrame:
        maps = dict(self.baseline_maps)
        maps["E"] = e_map
        return self.replayer.replay(maps)

    @staticmethod
    def _period_metrics(
        standalone: pd.DataFrame,
        combo: pd.DataFrame,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            period: {
                "e": _fast_combo_metrics(standalone, low, high),
                "combo": _fast_combo_metrics(combo, low, high),
            }
            for period, (low, high) in PERIODS.items()
        }

    @staticmethod
    def _flatten_metrics(
        metrics: dict[str, dict[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        fields = (
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
        )
        for period, scopes in metrics.items():
            for scope, values in scopes.items():
                for field in fields:
                    row[f"{period}_{scope}_{field}"] = values[field]
        return row

    def _assert_live_config_reproduced(self, deployed: VariantSpec) -> None:
        """确认当前正式E与本研究中已落地的双因子变体逐票一致。"""

        picks = self._select_daily(deployed)
        outcomes = self._outcomes(picks)
        official = self.baseline_e[
            self.baseline_e["status"].astype(str).eq("OK")
        ].copy()
        current = outcomes[outcomes["status"].astype(str).eq("OK")].copy()
        identity = official[["signal_date", "ts_code"]].merge(
            current[["signal_date", "ts_code"]],
            on="signal_date",
            how="outer",
            suffixes=("_official", "_current"),
            indicator=True,
        )
        mismatch = identity[
            identity["_merge"].ne("both")
            | identity["ts_code_official"].ne(identity["ts_code_current"])
        ]
        if not mismatch.empty:
            raise RuntimeError(
                f"已落地策略E候选未复现正式锚点，差异日数={len(mismatch)}"
            )
        official_values = official.set_index("signal_date")[
            "account_return"
        ].sort_index()
        current_values = current.set_index("signal_date")[
            "account_return"
        ].sort_index()
        if not official_values.index.equals(current_values.index) or not np.allclose(
            official_values.to_numpy(float),
            current_values.to_numpy(float),
            atol=TOLERANCE,
            rtol=0,
        ):
            raise RuntimeError("已落地策略E收益未复现正式锚点")

    def _evaluate(
        self,
        variant: VariantSpec,
        baseline_picks: pd.DataFrame,
    ) -> dict[str, Any]:
        picks = self._select_daily(variant)
        outcomes = self._outcomes(picks)
        e_map = strict.candidate_map(outcomes)
        standalone = self._replay_e(e_map)
        combo = self._replay_combo(e_map)
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
        factor_text = ";".join(
            f"{column}:{'desc' if higher else 'asc'}:{weight:g}"
            for column, higher, weight in variant.score_factors
        )
        row = {
            "variant": variant.name,
            "family": variant.family,
            "description": variant.description,
            "state_values": ";".join(variant.state_values),
            "score_factors": factor_text,
            "pre_exclude": (
                f"{variant.pre_exclude_column}={variant.pre_exclude_value}"
                if variant.pre_exclude_column
                else ""
            ),
            "numeric_filter": (
                f"{variant.numeric_filter_column}{variant.numeric_filter_operator}"
                f"{variant.numeric_filter_value:.12g}"
                if variant.numeric_filter_column
                else ""
            ),
            "gate_exclusions": ";".join(
                f"{column}={value}" for column, value in variant.gate_exclusions
            ),
            "selected_signal_dates": int(len(picks)),
            "executable_candidate_count": ok_count,
            "candidate_ok_rate": float(ok_count / len(outcomes))
            if len(outcomes)
            else 0.0,
            "changed_signal_dates": int(len(changed)),
            **self._flatten_metrics(metrics),
        }
        self._picks_by_variant[variant.name] = picks
        self._outcomes_by_variant[variant.name] = outcomes
        self._standalone_by_variant[variant.name] = standalone
        self._combo_by_variant[variant.name] = combo
        self._spec_by_name[variant.name] = variant
        return row

    def _usable_numeric_factors(self) -> list[str]:
        development = self.neutral_universe[
            self.neutral_universe["trade_date"].between(START, DEVELOPMENT_END)
        ]
        result = []
        for column in NUMERIC_FACTORS:
            if column not in development.columns:
                continue
            values = pd.to_numeric(development[column], errors="coerce")
            if values.notna().mean() < 0.90 or values.nunique(dropna=True) < 3:
                continue
            result.append(column)
        return result

    def _variant_space(self) -> list[VariantSpec]:
        baseline = VariantSpec(
            name="BASELINE_CURRENT_E",
            family="baseline",
            description=(
                "当前策略E：segment_retreat_state_bucket=neutral，按turnover_rate"
                "降序选每日第一名，再无回补排除first_time=13:30~14:30。"
            ),
        )
        specs = [baseline]
        usable = self._usable_numeric_factors()

        for column in usable:
            for higher, label in ((True, "desc"), (False, "asc")):
                specs.append(
                    VariantSpec(
                        name=f"RANK_SINGLE__{column}__{label}",
                        family="single_factor_ranking",
                        description=f"保持当前母池和门禁，仅按{column} {label}选每日第一名。",
                        score_factors=((column, higher, 1.0),),
                    )
                )
                if column != "turnover_rate":
                    specs.append(
                        VariantSpec(
                            name=f"SCORE_TURNOVER_PLUS__{column}__{label}",
                            family="two_factor_equal_weight_score",
                            description=(
                                "保持当前母池和门禁，按日内百分位等权合成"
                                f"turnover_rate降序与{column} {label}。"
                            ),
                            score_factors=(
                                ("turnover_rate", True, 1.0),
                                (column, higher, 1.0),
                            ),
                        )
                    )

        # 少量有明确交易含义的三因子组合；不根据测试段结果临时拼接。
        fixed_scores = [
            (
                "SCORE_TURNOVER_LEADER_THEME",
                "换手率、全市场龙头排名和题材热度等权评分。",
                (
                    ("turnover_rate", True, 1.0),
                    ("market_leader_rank", False, 1.0),
                    ("theme_heat_score", True, 1.0),
                ),
            ),
            (
                "SCORE_TURNOVER_OPEN_FD",
                "换手率、开板次数和封单/流通市值比等权评分。",
                (
                    ("turnover_rate", True, 1.0),
                    ("open_times", True, 1.0),
                    ("fd_amount_to_circ_mv", True, 1.0),
                ),
            ),
            (
                "SCORE_TURNOVER_PREV_AMOUNT",
                "换手率、前一日涨幅和成交额倍率等权评分。",
                (
                    ("turnover_rate", True, 1.0),
                    ("prev_pct_chg", True, 1.0),
                    ("amount_ratio_1d", True, 1.0),
                ),
            ),
            (
                "SCORE_TURNOVER_SEGMENT_THEME",
                "换手率、分段情绪和同题材涨停数量等权评分。",
                (
                    ("turnover_rate", True, 1.0),
                    ("segment_emotion_score", True, 1.0),
                    ("same_theme_limit_count", True, 1.0),
                ),
            ),
        ]
        for name, description, factors in fixed_scores:
            if all(column in usable for column, _, _ in factors):
                specs.append(
                    VariantSpec(
                        name=name,
                        family="three_factor_equal_weight_score",
                        description=description,
                        score_factors=factors,
                    )
                )

        specs.append(
            VariantSpec(
                name="REMOVE_CURRENT_FIRST_TIME_GATE",
                family="relax_entry_gate",
                description="移除现行13:30~14:30首次涨停无回补门禁。",
                gate_exclusions=(),
            )
        )

        # 额外门禁只从开发段的现行每日第一名中产生；先选第一名、再删除当日，
        # 不允许删除后回补第二名。
        pre_gate = self._select_daily(
            VariantSpec(
                name="_PRE_GATE",
                family="internal",
                description="internal",
                gate_exclusions=(),
            )
        )
        development_pre_gate = pre_gate[
            pre_gate["trade_date"].between(START, DEVELOPMENT_END)
        ].copy()
        for column in CATEGORICAL_FACTORS:
            if column not in development_pre_gate.columns:
                continue
            assert_selection_columns_strict(
                [column],
                context="research_strategy_e_current_window.entry_gate_space",
            )
            values = development_pre_gate[column].fillna("missing").astype(str)
            for value, count in values.value_counts().items():
                if int(count) < 4 or int(count) > int(len(values) * 0.45):
                    continue
                gate = tuple(dict.fromkeys((*CURRENT_GATE_EXCLUSIONS, (column, value))))
                if gate == CURRENT_GATE_EXCLUSIONS:
                    continue
                specs.append(
                    VariantSpec(
                        name=f"ENTRY_GATE_ADD__{column}__{value}",
                        family="post_first_pick_no_fallback_gate",
                        description=(
                            "保留现行门禁，并在每日第一名确定后额外排除"
                            f"{column}={value}；不回补第二名。"
                        ),
                        gate_exclusions=gate,
                    )
                )

        development_neutral = self.neutral_universe[
            self.neutral_universe["trade_date"].between(START, DEVELOPMENT_END)
        ].copy()
        for column in CATEGORICAL_FACTORS:
            if column not in development_neutral.columns:
                continue
            assert_selection_columns_strict(
                [column],
                context="research_strategy_e_current_window.pre_filter_space",
            )
            values = development_neutral[column].fillna("missing").astype(str)
            for value, count in values.value_counts().items():
                if int(count) < 5 or int(count) > int(len(values) * 0.55):
                    continue
                remaining = development_neutral[~values.eq(value)]
                if remaining["trade_date"].nunique() < 35:
                    continue
                specs.append(
                    VariantSpec(
                        name=f"PRE_EXCLUDE__{column}__{value}",
                        family="pre_ranking_bucket_exclusion",
                        description=(
                            f"在每日排序前排除{column}={value}，允许剩余候选正常竞争。"
                        ),
                        pre_exclude_column=column,
                        pre_exclude_value=value,
                    )
                )

        # 数值阈值只由开发段分位数确定，验证和测试段不重新估计阈值。
        for column in usable:
            values = pd.to_numeric(development_neutral[column], errors="coerce").dropna()
            for quantile in (0.25, 0.50, 0.75):
                threshold = float(values.quantile(quantile))
                for operator in ("gte", "lte"):
                    candidate = development_neutral[
                        (
                            pd.to_numeric(development_neutral[column], errors="coerce")
                            >= threshold
                        )
                        if operator == "gte"
                        else (
                            pd.to_numeric(development_neutral[column], errors="coerce")
                            <= threshold
                        )
                    ]
                    if candidate["trade_date"].nunique() < 35:
                        continue
                    specs.append(
                        VariantSpec(
                            name=(
                                f"NUMERIC_FILTER__{column}__q{int(quantile * 100)}__"
                                f"{operator}"
                            ),
                            family="development_quantile_pre_filter",
                            description=(
                                f"使用开发段{column}的{quantile:.0%}分位"
                                f"{threshold:.6g}作为固定阈值，排序前保留{operator}样本。"
                            ),
                            numeric_filter_column=column,
                            numeric_filter_operator=operator,
                            numeric_filter_value=threshold,
                        )
                    )

        states = sorted(
            set(
                self.universe["segment_retreat_state_bucket"]
                .fillna("missing")
                .astype(str)
            )
            - {"neutral"}
        )
        for state in states:
            specs.append(
                VariantSpec(
                    name=f"RELAX_STATE__neutral_plus_{state}",
                    family="relax_segment_retreat_state",
                    description=f"E母池由neutral扩展为neutral或{state}，其余规则不变。",
                    state_values=("neutral", state),
                )
            )
        specs.append(
            VariantSpec(
                name="RELAX_STATE__all_r1_states",
                family="relax_segment_retreat_state",
                description="放开E的板块退潮状态过滤，允许全部R1候选参与竞争。",
                state_values=tuple(["neutral", *states]),
            )
        )
        return specs

    def _add_comparison_and_gates(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        baseline = result[result["variant"].eq("BASELINE_CURRENT_E")].iloc[0]
        for period in PERIODS:
            for scope in ("e", "combo"):
                column = f"{period}_{scope}_equity_multiple"
                result[f"{period}_{scope}_compound_uplift"] = (
                    result[column] / float(baseline[column]) - 1.0
                )

        def segment_pass(row: pd.Series, period: str) -> bool:
            baseline_count = int(baseline[f"{period}_e_trade_count"])
            minimum_count = max(5, int(np.floor(baseline_count * 0.70)))
            return bool(
                int(row[f"{period}_e_trade_count"]) >= minimum_count
                and float(row[f"{period}_e_equity_multiple"])
                > float(baseline[f"{period}_e_equity_multiple"]) + TOLERANCE
                and float(row[f"{period}_combo_equity_multiple"])
                > float(baseline[f"{period}_combo_equity_multiple"]) + TOLERANCE
                and float(row[f"{period}_e_max_drawdown"])
                >= float(baseline[f"{period}_e_max_drawdown"])
                - MAX_DRAWDOWN_WORSENING
                and float(row[f"{period}_combo_max_drawdown"])
                >= float(baseline[f"{period}_combo_max_drawdown"])
                - MAX_DRAWDOWN_WORSENING
                and float(row["candidate_ok_rate"]) >= MIN_OK_RATE
            )

        for period in PERIODS:
            result[f"{period}_gate_passed"] = result.apply(
                lambda row, period=period: segment_pass(row, period), axis=1
            )
        result["development_score"] = (
            np.log(
                result["development_e_equity_multiple"]
                / float(baseline["development_e_equity_multiple"])
            )
            + np.log(
                result["development_combo_equity_multiple"]
                / float(baseline["development_combo_equity_multiple"])
            )
            + 0.25
            * (
                result["development_e_max_drawdown"]
                - float(baseline["development_e_max_drawdown"])
            )
            + 0.25
            * (
                result["development_combo_max_drawdown"]
                - float(baseline["development_combo_max_drawdown"])
            )
        )
        result["all_research_gates_passed"] = (
            result["development_gate_passed"]
            & result["validation_2025h2_gate_passed"]
            & result["test_2026h1_gate_passed"]
            & result["full_gate_passed"]
        )
        return result

    @staticmethod
    def choose_sequential_candidate(frame: pd.DataFrame) -> tuple[str, list[str]]:
        development = frame[
            ~frame["variant"].eq("BASELINE_CURRENT_E")
            & frame["development_gate_passed"].astype(bool)
        ].sort_values(
            ["development_score", "development_e_compound_uplift"],
            ascending=False,
        )
        shortlist = development.head(TOP_DEVELOPMENT_CANDIDATES)
        for _, row in shortlist.iterrows():
            if bool(row["validation_2025h2_gate_passed"]):
                return str(row["variant"]), shortlist["variant"].astype(str).tolist()
        return "BASELINE_CURRENT_E", shortlist["variant"].astype(str).tolist()

    @staticmethod
    def _full_window_challenger(frame: pd.DataFrame) -> str:
        baseline = frame[frame["variant"].eq("BASELINE_CURRENT_E")].iloc[0]
        minimum_count = max(
            5,
            int(np.floor(float(baseline["full_e_trade_count"]) * 0.70)),
        )
        candidates = frame[
            ~frame["variant"].eq("BASELINE_CURRENT_E")
            & (frame["full_e_trade_count"] >= minimum_count)
            & (frame["full_e_equity_multiple"] > float(baseline["full_e_equity_multiple"]))
            & (
                frame["full_combo_equity_multiple"]
                > float(baseline["full_combo_equity_multiple"])
            )
            & (
                frame["full_e_max_drawdown"]
                >= float(baseline["full_e_max_drawdown"]) - MAX_DRAWDOWN_WORSENING
            )
            & (
                frame["full_combo_max_drawdown"]
                >= float(baseline["full_combo_max_drawdown"])
                - MAX_DRAWDOWN_WORSENING
            )
            & (frame["candidate_ok_rate"] >= MIN_OK_RATE)
        ].sort_values(
            ["full_combo_equity_multiple", "full_e_equity_multiple"],
            ascending=False,
        )
        return (
            str(candidates.iloc[0]["variant"])
            if not candidates.empty
            else "BASELINE_CURRENT_E"
        )

    @staticmethod
    def _stable_noninferiority_challenger(frame: pd.DataFrame) -> str:
        """选出各分段E均提升且组合不劣的全窗观察方案。

        该选择会查看测试段，只能作为STRICT_DISCOVERY观察结论，不能冒充
        未查看样本外结果。它用于区分“某一段大赚拉高全窗”与“各段方向
        一致的小幅改进”。
        """

        baseline = frame[frame["variant"].eq("BASELINE_CURRENT_E")].iloc[0]
        keep = ~frame["variant"].eq("BASELINE_CURRENT_E")
        for period in ("development", "validation_2025h2", "test_2026h1"):
            minimum_count = max(
                5,
                int(np.floor(float(baseline[f"{period}_e_trade_count"]) * 0.70)),
            )
            keep &= (
                (frame[f"{period}_e_trade_count"] >= minimum_count)
                & (
                    frame[f"{period}_e_equity_multiple"]
                    > float(baseline[f"{period}_e_equity_multiple"]) + TOLERANCE
                )
                & (
                    frame[f"{period}_combo_equity_multiple"]
                    >= float(baseline[f"{period}_combo_equity_multiple"])
                    - TOLERANCE
                )
                & (
                    frame[f"{period}_e_max_drawdown"]
                    >= float(baseline[f"{period}_e_max_drawdown"])
                    - MAX_DRAWDOWN_WORSENING
                )
                & (
                    frame[f"{period}_combo_max_drawdown"]
                    >= float(baseline[f"{period}_combo_max_drawdown"])
                    - MAX_DRAWDOWN_WORSENING
                )
            )
        keep &= (
            (frame["full_e_equity_multiple"] > float(baseline["full_e_equity_multiple"]))
            & (
                frame["full_combo_equity_multiple"]
                > float(baseline["full_combo_equity_multiple"])
            )
            & (frame["candidate_ok_rate"] >= MIN_OK_RATE)
        )
        candidates = frame[keep].copy()
        if candidates.empty:
            return "BASELINE_CURRENT_E"
        candidates["_minimum_segment_e_uplift"] = candidates[
            [
                "development_e_compound_uplift",
                "validation_2025h2_e_compound_uplift",
                "test_2026h1_e_compound_uplift",
            ]
        ].min(axis=1)
        candidates = candidates.sort_values(
            [
                "_minimum_segment_e_uplift",
                "full_combo_equity_multiple",
                "full_e_equity_multiple",
            ],
            ascending=False,
        )
        return str(candidates.iloc[0]["variant"])

    def _changed_signal_audit(self, candidate: str) -> pd.DataFrame:
        columns = ["signal_date", "ts_code", "status", "account_return"]
        baseline = self._outcomes_by_variant["BASELINE_CURRENT_E"][columns].rename(
            columns={
                "ts_code": "baseline_ts_code",
                "status": "baseline_status",
                "account_return": "baseline_account_return",
            }
        )
        challenger = self._outcomes_by_variant[candidate][columns].rename(
            columns={
                "ts_code": "candidate_ts_code",
                "status": "candidate_status",
                "account_return": "candidate_account_return",
            }
        )
        merged = baseline.merge(challenger, on="signal_date", how="outer")
        changed = merged[
            merged["baseline_ts_code"].fillna("").astype(str)
            .ne(merged["candidate_ts_code"].fillna("").astype(str))
        ].copy()
        changed["candidate_minus_baseline_return"] = (
            pd.to_numeric(changed["candidate_account_return"], errors="coerce")
            - pd.to_numeric(changed["baseline_account_return"], errors="coerce")
        )
        return changed.sort_values("signal_date").reset_index(drop=True)

    def _monthly_comparison(self, variants: Iterable[str]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for variant in variants:
            for scope, detail in (
                ("e", self._standalone_by_variant[variant]),
                ("combo", self._combo_by_variant[variant]),
            ):
                sample = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
                sample["month"] = sample["signal_date"].astype(str).str[:6]
                for month, group in sample.groupby("month", sort=True):
                    metrics = _fast_return_metrics(group["account_return"])
                    rows.append(
                        {
                            "variant": variant,
                            "scope": scope,
                            "month": month,
                            **metrics,
                        }
                    )
        return pd.DataFrame(rows)

    def _combo_changed_trade_audit(self, candidate: str) -> pd.DataFrame:
        columns = [
            "signal_date",
            "strategy_leg",
            "ts_code",
            "account_return",
            "exit_date",
        ]
        baseline = self._combo_by_variant["BASELINE_CURRENT_E"]
        baseline = baseline[baseline["status"].astype(str).eq("EXECUTED")][columns]
        baseline = baseline.rename(
            columns={column: f"baseline_{column}" for column in columns if column != "signal_date"}
        )
        challenger = self._combo_by_variant[candidate]
        challenger = challenger[challenger["status"].astype(str).eq("EXECUTED")][columns]
        challenger = challenger.rename(
            columns={column: f"candidate_{column}" for column in columns if column != "signal_date"}
        )
        merged = baseline.merge(challenger, on="signal_date", how="outer")
        changed = merged[
            merged["baseline_ts_code"].fillna("").astype(str)
            .ne(merged["candidate_ts_code"].fillna("").astype(str))
        ].copy()
        return changed.sort_values("signal_date").reset_index(drop=True)

    def _factor_diagnostics(self, baseline: VariantSpec) -> pd.DataFrame:
        pre_gate = VariantSpec(
            name="_PRE_GATE_DIAGNOSTIC",
            family="internal",
            description="internal",
            gate_exclusions=(),
        )
        picks = self._select_daily(pre_gate)
        outcomes = self._outcomes(picks)
        merged = picks.merge(
            outcomes[["signal_date", "ts_code", "status", "account_return"]],
            left_on=["trade_date", "ts_code"],
            right_on=["signal_date", "ts_code"],
            how="left",
        )
        merged = merged[merged["status"].astype(str).eq("OK")].copy()
        rows: list[dict[str, Any]] = []
        for column in CATEGORICAL_FACTORS:
            if column not in merged.columns:
                continue
            for value, group in merged.groupby(
                merged[column].fillna("missing").astype(str)
            ):
                if len(group) < 3:
                    continue
                metrics = _fast_return_metrics(group["account_return"])
                rows.append(
                    {
                        "factor": column,
                        "group": str(value),
                        "scope": "pre_gate_daily_first_candidate_pool",
                        **metrics,
                    }
                )
        return pd.DataFrame(rows).sort_values(
            ["factor", "trade_count", "group"],
            ascending=[True, False, True],
        )

    @staticmethod
    def _period_comparison(frame: pd.DataFrame, variants: Iterable[str]) -> pd.DataFrame:
        rows = []
        for variant in variants:
            item = frame[frame["variant"].eq(variant)].iloc[0]
            for period in PERIODS:
                rows.append(
                    {
                        "variant": variant,
                        "period": period,
                        "e_trade_count": item[f"{period}_e_trade_count"],
                        "e_win_rate": item[f"{period}_e_win_rate"],
                        "e_avg_account_return": item[f"{period}_e_avg_account_return"],
                        "e_median_account_return": item[f"{period}_e_median_account_return"],
                        "e_equity_multiple": item[f"{period}_e_equity_multiple"],
                        "e_max_drawdown": item[f"{period}_e_max_drawdown"],
                        "combo_trade_count": item[f"{period}_combo_trade_count"],
                        "combo_equity_multiple": item[f"{period}_combo_equity_multiple"],
                        "combo_max_drawdown": item[f"{period}_combo_max_drawdown"],
                        "gate_passed": bool(item[f"{period}_gate_passed"]),
                    }
                )
        return pd.DataFrame(rows)

    def _write_report(
        self,
        frame: pd.DataFrame,
        selected: str,
        shortlist: list[str],
        challenger: str,
        stable: str,
    ) -> None:
        baseline = frame[frame["variant"].eq("BASELINE_CURRENT_E")].iloc[0]
        selected_row = frame[frame["variant"].eq(selected)].iloc[0]
        challenger_row = frame[frame["variant"].eq(challenger)].iloc[0]
        stable_row = frame[frame["variant"].eq(stable)].iloc[0]
        stable_signal_changes = self._changed_signal_audit(stable)
        stable_combo_changes = self._combo_changed_trade_audit(stable)
        stable_combo_change_months = sorted(
            stable_combo_changes["signal_date"].astype(str).str[:6].unique().tolist()
        )
        promotion = bool(
            selected != "BASELINE_CURRENT_E"
            and selected_row["all_research_gates_passed"]
        )
        conclusion = (
            f"顺序研究候选{selected}通过开发、验证、测试和全窗双复利门槛；"
            "仍需前向模拟，不能自动替换实盘。"
            if promotion
            else (
                "严格顺序候选没有同时通过全部分段双复利门槛；"
                "用户已明确决定落地分段非劣双因子观察方案。"
            )
        )

        def table_row(label: str, row: pd.Series) -> str:
            return (
                f"| {label} | {int(row['full_e_trade_count'])} | "
                f"{float(row['full_e_win_rate']):.2%} | "
                f"{float(row['full_e_avg_account_return']):.2%} / "
                f"{float(row['full_e_median_account_return']):.2%} | "
                f"{float(row['full_e_equity_multiple']):.6f}倍 | "
                f"{float(row['full_e_max_drawdown']):.2%} | "
                f"{float(row['full_combo_equity_multiple']):.6f}倍 | "
                f"{float(row['full_combo_max_drawdown']):.2%} |"
            )

        content = [
            "# 策略E当前窗口再研究",
            "",
            f"> 结论：{conclusion}",
            "",
            "## 固定口径",
            "",
            f"- 窗口：{START}~{END}；开发段至{DEVELOPMENT_END}，验证段为2025H2，测试段为2026H1。",
            "- 固定D>A>E>C、82.5%仓位、费用、滑点、前复权、T+1和涨跌停处理；只替换E。",
            "- 测试段不参与候选排序；本轮不修改实盘配置。",
            "",
            "## 数据质量",
            "",
            f"- 严格源：{self.data_quality['strict_source_rows']}行，as-of通过={self.data_quality['strict_source_asof_passed']}。",
            f"- E特征池：{self.data_quality['feature_pool_rows']}行/{self.data_quality['feature_pool_signal_dates']}日。",
            f"- R1并集：{self.data_quality['r1_union_rows']}行/{self.data_quality['r1_union_signal_dates']}日；neutral母池{self.data_quality['neutral_rows']}行/{self.data_quality['neutral_signal_dates']}日。",
            f"- 必需字段缺失：{self.data_quality['missing_required_signal_fields'] or '无'}；重复键均为0。",
            "",
            "## 全窗结果",
            "",
            "| 方案 | E执行数 | E胜率 | E平均/中位 | E复利 | E最大回撤 | 组合复利 | 组合最大回撤 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            table_row("落地前单因子基准", baseline),
            table_row("顺序候选", selected_row),
            table_row("全窗观察候选", challenger_row),
            table_row("分段非劣观察候选", stable_row),
            "",
            f"- 顺序候选：`{selected}`；{selected_row['description']}",
            f"- 开发段前{TOP_DEVELOPMENT_CANDIDATES}名：{', '.join(shortlist) if shortlist else '无'}。",
            "- 顺序候选开发/验证/测试/全段门槛："
            f"{bool(selected_row['development_gate_passed'])}/"
            f"{bool(selected_row['validation_2025h2_gate_passed'])}/"
            f"{bool(selected_row['test_2026h1_gate_passed'])}/"
            f"{bool(selected_row['full_gate_passed'])}。",
            "",
            "## 全窗双提升观察候选",
            "",
            f"- 方案：`{challenger}`；{challenger_row['description']}",
            "- E全窗复利相对基准变化："
            f"{float(challenger_row['full_e_compound_uplift']):+.2%}；"
            "组合全窗复利变化："
            f"{float(challenger_row['full_combo_compound_uplift']):+.2%}。",
            "- 开发/验证/测试/全段门槛："
            f"{bool(challenger_row['development_gate_passed'])}/"
            f"{bool(challenger_row['validation_2025h2_gate_passed'])}/"
            f"{bool(challenger_row['test_2026h1_gate_passed'])}/"
            f"{bool(challenger_row['full_gate_passed'])}。",
            "",
            "## 分段非劣观察候选",
            "",
            f"- 方案：`{stable}`；{stable_row['description']}",
            "- E开发/验证/测试复利相对基准变化："
            f"{float(stable_row['development_e_compound_uplift']):+.2%} / "
            f"{float(stable_row['validation_2025h2_e_compound_uplift']):+.2%} / "
            f"{float(stable_row['test_2026h1_e_compound_uplift']):+.2%}。",
            "- 组合开发/验证/测试复利相对基准变化："
            f"{float(stable_row['development_combo_compound_uplift']):+.2%} / "
            f"{float(stable_row['validation_2025h2_combo_compound_uplift']):+.2%} / "
            f"{float(stable_row['test_2026h1_combo_compound_uplift']):+.2%}。",
            "- 全窗E/组合复利相对基准变化："
            f"{float(stable_row['full_e_compound_uplift']):+.2%} / "
            f"{float(stable_row['full_combo_compound_uplift']):+.2%}。",
            f"- 直接改变E信号{len(stable_signal_changes)}日；组合执行差异"
            f"{len(stable_combo_changes)}日，差异月份："
            f"{', '.join(stable_combo_change_months) if stable_combo_change_months else '无'}。",
            "- 组合增量集中于单一月份和持仓释放后的腿序切换，路径依赖明显，稳健性证据不足。",
            "- 该方案由全窗口分段一致性筛出且已经查看测试段；现已按用户明确决策落地，统计属性仍为STRICT_DISCOVERY，必须继续前向和小资金验证。",
            "",
            "## 风险与发布限制",
            "",
            "- 当前两年窗口及其历史报告已经参与规则研究，结果仍是STRICT_DISCOVERY，不是真正未查看样本外。",
            "- 本轮同时搜索排序、分位阈值、桶排除和门禁，存在多重比较风险。",
            "- E的40条R1母规则也来自更早历史样本内Top40，新增条件不能消除母池自身选择偏差。",
            "- 机械复利没有证明大资金盘口容量，也不代表未来收益。",
            "- 本研究脚本自身不会改动实盘；本次落地已由独立变更更新唯一规则源、认证锚点和实盘回归测试。",
            "",
        ]
        REPORT_PATH.write_text("\n".join(content), encoding="utf-8")

    def run(self) -> dict[str, Any]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        specs = self._variant_space()
        baseline_spec = specs[0]
        deployed_spec = next(
            variant for variant in specs if variant.name == DEPLOYED_VARIANT_NAME
        )
        self._assert_live_config_reproduced(deployed_spec)
        baseline_picks = self._select_daily(baseline_spec)

        rows: list[dict[str, Any]] = []
        seen_signatures: set[tuple[tuple[str, str], ...]] = set()
        for position, variant in enumerate(specs, 1):
            picks = self._select_daily(variant)
            signature = tuple(
                zip(
                    picks["trade_date"].astype(str),
                    picks["ts_code"].astype(str),
                )
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            LOGGER.info("评估E变体 %d/%d: %s", position, len(specs), variant.name)
            rows.append(self._evaluate(variant, baseline_picks))
        result = self._add_comparison_and_gates(pd.DataFrame(rows))
        selected, shortlist = self.choose_sequential_candidate(result)
        challenger = self._full_window_challenger(result)
        stable = self._stable_noninferiority_challenger(result)

        selected_row = result[result["variant"].eq(selected)].iloc[0]
        promotion = bool(
            selected != "BASELINE_CURRENT_E"
            and selected_row["all_research_gates_passed"]
        )
        variants = list(
            dict.fromkeys(("BASELINE_CURRENT_E", selected, challenger, stable))
        )
        period = self._period_comparison(result, variants)
        diagnostics = self._factor_diagnostics(baseline_spec)

        result.to_csv(VARIANT_PATH, index=False, encoding="utf-8-sig")
        period.to_csv(PERIOD_PATH, index=False, encoding="utf-8-sig")
        diagnostics.to_csv(FACTOR_PATH, index=False, encoding="utf-8-sig")
        self._picks_by_variant[selected].to_csv(PICKS_PATH, index=False, encoding="utf-8-sig")
        self._outcomes_by_variant[selected].to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")
        self._picks_by_variant[challenger].to_csv(
            CHALLENGER_PICKS_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        challenger_combo = self._combo_by_variant[challenger]
        challenger_combo[
            challenger_combo["status"].astype(str).eq("EXECUTED")
        ].to_csv(
            CHALLENGER_TRADES_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        self._picks_by_variant[stable].to_csv(
            STABLE_PICKS_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        self._outcomes_by_variant[stable].to_csv(
            STABLE_TRADES_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        stable_e = self._standalone_by_variant[stable]
        stable_e[stable_e["status"].astype(str).eq("EXECUTED")].to_csv(
            STABLE_E_EXECUTED_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        stable_combo = self._combo_by_variant[stable]
        stable_combo[stable_combo["status"].astype(str).eq("EXECUTED")].to_csv(
            STABLE_COMBO_TRADES_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        self._changed_signal_audit(stable).to_csv(
            STABLE_CHANGED_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        self._combo_changed_trade_audit(stable).to_csv(
            STABLE_COMBO_CHANGED_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        self._monthly_comparison(("BASELINE_CURRENT_E", stable)).to_csv(
            MONTHLY_PATH,
            index=False,
            encoding="utf-8-sig",
        )
        QUALITY_PATH.write_text(
            json.dumps(self.data_quality, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        baseline_row = result[result["variant"].eq("BASELINE_CURRENT_E")].iloc[0]
        challenger_row = result[result["variant"].eq(challenger)].iloc[0]
        stable_row = result[result["variant"].eq(stable)].iloc[0]
        summary = {
            "schema_version": 1,
            "research_protocol": STRICT_DISCOVERY,
            "release_eligible": False,
            "live_config_modified": False,
            "live_config_modified_by_this_research_script": False,
            "deployed_variant_at_report_time": DEPLOYED_VARIANT_NAME,
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
            "promotion_research_gates_passed": promotion,
            "full_window_challenger": challenger,
            "full_window_challenger_description": str(challenger_row["description"]),
            "stable_noninferiority_challenger": stable,
            "stable_noninferiority_description": str(stable_row["description"]),
            "baseline": {
                key: _json_value(value) for key, value in baseline_row.to_dict().items()
            },
            "selected": {
                key: _json_value(value) for key, value in selected_row.to_dict().items()
            },
            "challenger": {
                key: _json_value(value) for key, value in challenger_row.to_dict().items()
            },
            "stable_noninferiority": {
                key: _json_value(value) for key, value in stable_row.to_dict().items()
            },
            "data_quality": self.data_quality,
            "limitations": [
                "当前两年窗口参与规则研究，结论属于STRICT_DISCOVERY。",
                "多候选搜索存在多重比较和过拟合风险。",
                "E的40条R1母规则来自更早历史样本内Top40。",
                "机械复利不代表未来收益或大资金可成交容量。",
                "研究脚本不修改实盘配置、发布证书或券商状态。",
            ],
        }
        SUMMARY_PATH.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_report(result, selected, shortlist, challenger, stable)
        return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    summary = StrategyECurrentWindowResearch().run()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
