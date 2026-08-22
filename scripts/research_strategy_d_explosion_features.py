#!/usr/bin/env python3
"""研究策略D的爆发、爆亏特征与结构候选，不修改正式策略。

研究口径固定为：

1. 最近24个月 ``20240630~20260630`` 是唯一规则发现和更新决策窗口；
2. 更早6个月 ``20231230~20240629`` 只读旁路验证，不参与更新投票；
3. 当前A、E优化后组合 ``132笔/327.726718975489倍`` 是唯一组合锚点；
4. D候选每次只替换D腿，A/E/C、D>A>E>C、82.5%仓位、D成交压力折扣、
   费用、滑点、涨跌停、T+1和单账户占仓全部冻结；
5. 即使历史双复利通过，只要完整盘中母事件池缺失，仍不得自动修改正式D规则。

输出：

``reports/strategy_d_explosion_research/``

- ``summary.json``：机器可读结论和全部硬门槛；
- ``rule_search.csv``：结构规则、D独立腿和ACDE单腿替换结果；
- ``factor_groups.csv``：当前每日第一候选的分组诊断；
- ``baseline_daily_candidate_outcomes.csv``：当前53个逐日第一候选及收益；
- ``prior_six_months.csv``：更早6个月只读旁路结果；
- ``changed_daily_picks.csv``：14:00候选相对旧规则改变的逐日第一名；
- ``changed_executed_trades.csv``：14:00候选相对旧规则改变的实际独立交易；
- ``failed_touch_by_day.csv``：收盘涨停池遗漏的首板触板失败上界审计。

运行：

    python3 scripts/research_strategy_d_explosion_features.py

注意：本脚本属于 ``STRICT_DISCOVERY``，输出不是实盘收益承诺，也不取得发布资格。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.backtest_strategy_d import build_daily_candidate_ledger  # noqa: E402
from src.market_rules import (  # noqa: E402
    is_st_name,
    limit_up_price,
    market_segment,
    price_limit_pct,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.strategy_d_spec import historical_candidate_mask  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("strategy_d_explosion_research")

DECISION_START = "20240630"
DECISION_END = "20260630"
PRIOR_START = "20231230"
PRIOR_END = "20240629"
FIRST_HALF_END = "20250630"
SECOND_HALF_START = "20250701"

FEATURE_POOL_PATH = ROOT / "data/research/five_year_strict/strict_feature_pool.csv"
FEATURE_MANIFEST_PATH = ROOT / "data/research/five_year_strict/dataset_manifest.json"
FEATURE_AUDIT_PATH = ROOT / "data/research/five_year_strict/strict_asof_audit.json"
TRADE_CALENDAR_PATH = ROOT / "data/raw/trade_calendar.csv"
STOCK_BASIC_PATH = ROOT / "data/raw/stock_basic/stock_basic_all.csv"
DAILY_DIR = ROOT / "data/raw/daily"
LIMIT_LIST_DIR = ROOT / "data/raw/limit_list"
OUTPUT_DIR = ROOT / "reports/strategy_d_explosion_research"

ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj"}
EXPECTED_COMBO_TRADE_COUNT = 132
EXPECTED_COMBO_MULTIPLE = 327.72671897548867
EXPECTED_COMBO_LEG_COUNTS = {"D": 22, "A": 44, "E": 49, "C": 17}
EXPECTED_D_TRADE_COUNT = 39
EXPECTED_D_MULTIPLE = 2.0261239235922566
TOLERANCE = 1e-12

FEATURE_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "limit_close",
    "limit_times",
    "is_st",
    "market_sentiment_level",
    "board_type",
    "open_times",
    "first_time",
    "last_time",
    "first_time_bucket",
    "first_time_detail_bucket",
    "fill_probability",
    "is_fill_score_reliable",
    "allow_buy_reliable",
    "market_segment",
    "fd_amount_to_circ_mv",
    "turnover_rate",
    "volume_ratio",
    "limit_up_count",
    "market_chain_count",
    "market_chain_count_bucket",
    "theme_data_available",
    "theme_name",
    "theme_limit_count",
    "theme_limit_count_bucket",
    "theme_heat_rank",
    "theme_is_mainline",
    "theme_is_mainline_bucket",
    "open_times_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
]

FACTOR_GROUPS = [
    ("封板时间", "first_time_detail_bucket"),
    ("炸板次数", "open_times"),
    ("市场接力强度", "market_chain_count_bucket"),
    ("题材涨停数量", "theme_limit_count_bucket"),
    ("题材主线", "theme_is_mainline_bucket"),
    ("市场板块", "market_segment"),
    ("换手率", "turnover_rate_bucket"),
    ("量比", "volume_ratio_bucket"),
    ("封单/流通市值", "fd_ratio_bucket"),
]


@dataclass(frozen=True)
class RuleSpec:
    name: str
    description: str
    predicate: Callable[[pd.DataFrame], pd.Series]


def _all(_: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=_.index)


def _first_before(value: int) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: pd.to_numeric(frame["first_time"], errors="coerce").lt(value)


def _open2_between(low: int, high: int) -> Callable[[pd.DataFrame], pd.Series]:
    def predicate(frame: pd.DataFrame) -> pd.Series:
        first = pd.to_numeric(frame["first_time"], errors="coerce")
        opens = pd.to_numeric(frame["open_times"], errors="coerce")
        return opens.eq(2) & first.ge(low) & first.lt(high)

    return predicate


RULES = [
    RuleSpec("current", "当前正式D静态规则", _all),
    RuleSpec("first_before_1330", "首次封板早于13:30", _first_before(133000)),
    RuleSpec("first_before_1400", "首次封板早于14:00", _first_before(140000)),
    RuleSpec("first_before_1415", "首次封板早于14:15", _first_before(141500)),
    RuleSpec("first_before_1430", "首次封板早于14:30", _first_before(143000)),
    RuleSpec("first_before_1445", "首次封板早于14:45", _first_before(144500)),
    RuleSpec(
        "open2_first_1100_1330",
        "炸板2次且首次封板11:00~13:30",
        _open2_between(110000, 133000),
    ),
    RuleSpec(
        "open2_first_1130_1330",
        "炸板2次且首次封板11:30~13:30",
        _open2_between(113000, 133000),
    ),
]


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_feature_pool() -> tuple[pd.DataFrame, dict[str, Any]]:
    """加载并验证五年严格底座中的D研究特征子集。"""

    for path in (FEATURE_POOL_PATH, FEATURE_MANIFEST_PATH, FEATURE_AUDIT_PATH):
        if not path.exists():
            raise FileNotFoundError(f"D研究缺少输入：{path}")
    manifest = json.loads(FEATURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    audit = json.loads(FEATURE_AUDIT_PATH.read_text(encoding="utf-8"))
    if not bool(audit.get("passed")):
        raise RuntimeError("五年严格as-of底座审计未通过，拒绝研究D")
    expected_hash = str(manifest["files"]["feature_pool"]["sha256"])
    actual_hash = sha256(FEATURE_POOL_PATH)
    if actual_hash != expected_hash:
        raise RuntimeError(
            "D研究特征池哈希漂移："
            f"expected={expected_hash} actual={actual_hash}"
        )
    frame = pd.read_csv(FEATURE_POOL_PATH, usecols=FEATURE_COLUMNS, low_memory=False)
    frame["trade_date"] = date_text(frame["trade_date"])
    duplicate_count = int(frame.duplicated(["trade_date", "ts_code"]).sum())
    if duplicate_count:
        raise RuntimeError(f"D研究特征池存在{duplicate_count}条日期+股票重复记录")
    return frame, {
        "feature_pool_path": str(FEATURE_POOL_PATH.relative_to(ROOT)),
        "feature_pool_sha256": actual_hash,
        "strict_asof_audit_passed": True,
        "strict_asof_standard_id": str(audit.get("standard_id", "")),
        "loaded_row_count": int(len(frame)),
        "duplicate_key_count": duplicate_count,
    }


def d_pool(frame: pd.DataFrame, low: str, high: str) -> pd.DataFrame:
    mask = historical_candidate_mask(
        frame,
        min_fill_probability=0.80,
        allowed_segments=ALLOWED_SEGMENTS,
    )
    return frame[mask & frame["trade_date"].between(low, high)].copy()


class DOutcomeCache:
    def __init__(self, daily_data: strict.DailyData) -> None:
        self.daily_data = daily_data
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}

    def outcome(self, row: pd.Series) -> dict[str, Any]:
        signal_date = str(row.get("signal_date", row.get("trade_date", "")))
        code = str(row["ts_code"])
        key = (signal_date, code)
        if key not in self.cache:
            execution_row = row.copy()
            execution_row["signal_date"] = signal_date
            self.cache[key] = strict.d_execution(execution_row, self.daily_data)
        return self.cache[key]

    def frame(self, picks: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for _, row in picks.iterrows():
            record = row.to_dict()
            record["signal_date"] = str(row.get("signal_date", row.get("trade_date", "")))
            record["strategy_leg"] = "D"
            record.update(self.outcome(row))
            rows.append(record)
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


def calendar_dates(low: str, high: str) -> list[str]:
    calendar = pd.read_csv(TRADE_CALENDAR_PATH, dtype=str, low_memory=False)
    if "is_open" in calendar.columns:
        calendar = calendar[
            calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
        ]
    return sorted(
        calendar.loc[calendar["cal_date"].between(low, high), "cal_date"].astype(str)
    )


def replay_d_only(
    outcomes: pd.DataFrame,
    low: str,
    high: str,
    *,
    hit_limit_up: Callable[[str, str, str], bool] = strict.cert.hit_limit_up,
) -> pd.DataFrame:
    """按正式组合的持仓释放语义回放单独D腿。"""

    candidates = strict.candidate_map(outcomes)
    equity = 1.0
    occupied_until = occupied_code = occupied_name = ""
    rows: list[dict[str, Any]] = []
    for signal_date in calendar_dates(low, high):
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
            and not hit_limit_up(signal_date, occupied_code, occupied_name)
        )
        occupied_until = occupied_code = occupied_name = ""
        selected = candidates.get(signal_date)
        if selected is None or blocking_handoff:
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
                "strategy_leg": "D",
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


def executed_metrics(detail: pd.DataFrame) -> dict[str, Any]:
    trades = detail[detail["status"].eq("EXECUTED")]
    return strict.return_metrics(trades["account_return"])


def simple_metrics(values: pd.Series) -> dict[str, Any]:
    """用于候选池分组诊断；不把候选池复利冒充独立策略复利。"""

    array = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(array) == 0:
        return {
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "diagnostic_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "explosion_rate_gte_10pct": 0.0,
            "big_loss_rate_lte_minus_5pct": 0.0,
        }
    compound = mechanical_compound(array)
    return {
        "sample_count": int(len(array)),
        "win_rate": float((array > 0).mean()),
        "avg_return": float(array.mean()),
        "median_return": float(np.median(array)),
        "diagnostic_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "max_profit": float(array.max()),
        "max_loss": float(array.min()),
        "explosion_rate_gte_10pct": float((array >= 0.10).mean()),
        "big_loss_rate_lte_minus_5pct": float((array <= -0.05).mean()),
    }


def factor_groups(baseline_outcomes: pd.DataFrame) -> pd.DataFrame:
    valid = baseline_outcomes[baseline_outcomes["status"].eq("OK")].copy()
    rows: list[dict[str, Any]] = []
    for dimension, column in FACTOR_GROUPS:
        if column not in valid.columns:
            continue
        grouped = valid.assign(_group=valid[column].fillna("MISSING").astype(str))
        for group, sample in grouped.groupby("_group", sort=True):
            if len(sample) < 2:
                continue
            rows.append(
                {
                    "scope": "daily_first_candidate_pool_diagnostic_not_standalone",
                    "dimension": dimension,
                    "feature_column": column,
                    "group": group,
                    **simple_metrics(sample["account_return"]),
                }
            )
    return pd.DataFrame(rows).sort_values(
        ["dimension", "avg_return", "sample_count"], ascending=[True, False, False]
    )


def flat_metrics(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    keys = [
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
        "avg_return_bootstrap_95_lower",
        "avg_return_bootstrap_95_upper",
    ]
    return {f"{prefix}_{key}": metrics.get(key) for key in keys}


def build_current_other_legs() -> dict[str, pd.DataFrame]:
    """重建A、E优化后当前腿；它们在D研究中完全冻结。"""

    LOGGER.info("重建当前A/C候选")
    ac = strict.build_ac(strict.STRICT_SOURCE)
    LOGGER.info("重建当前E候选")
    _, strategy_e = strict.build_e()
    return {
        "A": ac[ac["strategy_leg"].eq("A")].copy(),
        "E": strategy_e.copy(),
        "C": ac[ac["strategy_leg"].eq("C")].copy(),
    }


def combo_replay(
    d_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    legs = {"D": d_outcomes, **other_legs}
    maps = {leg: strict.candidate_map(frame) for leg, frame in legs.items()}
    detail = strict.replay(maps, set(legs))
    return detail, strict.combo_metrics(detail)


def assert_current_anchor(
    d_metrics: dict[str, Any], combo_metrics: dict[str, Any]
) -> None:
    actual_counts = {
        leg: int(combo_metrics["leg_counts"].get(leg, 0))
        for leg in ("D", "A", "E", "C")
    }
    failures: list[str] = []
    if int(d_metrics["trade_count"]) != EXPECTED_D_TRADE_COUNT:
        failures.append(f"D笔数={d_metrics['trade_count']}")
    if abs(float(d_metrics["equity_multiple"]) - EXPECTED_D_MULTIPLE) > TOLERANCE:
        failures.append(f"D复利={d_metrics['equity_multiple']}")
    if int(combo_metrics["trade_count"]) != EXPECTED_COMBO_TRADE_COUNT:
        failures.append(f"组合笔数={combo_metrics['trade_count']}")
    if abs(float(combo_metrics["equity_multiple"]) - EXPECTED_COMBO_MULTIPLE) > TOLERANCE:
        failures.append(f"组合复利={combo_metrics['equity_multiple']}")
    if actual_counts != EXPECTED_COMBO_LEG_COUNTS:
        failures.append(f"分腿={actual_counts}")
    if failures:
        raise RuntimeError("D研究当前正式锚点漂移，拒绝继续：" + "；".join(failures))


def evaluate_rules(
    pool: pd.DataFrame,
    outcome_cache: DOutcomeCache,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    rows: list[dict[str, Any]] = []
    outcome_frames: dict[str, pd.DataFrame] = {}
    standalone_details: dict[str, pd.DataFrame] = {}
    baseline_d: dict[str, Any] | None = None
    baseline_combo: dict[str, Any] | None = None
    for spec in RULES:
        selected_pool = pool[spec.predicate(pool).fillna(False)].copy()
        picks = build_daily_candidate_ledger(selected_pool)
        outcomes = outcome_cache.frame(picks)
        standalone = replay_d_only(outcomes, DECISION_START, DECISION_END)
        d_metrics = executed_metrics(standalone)
        combo_detail, combo_metrics = combo_replay(outcomes, other_legs)
        candidate_metrics = simple_metrics(
            outcomes.loc[outcomes["status"].eq("OK"), "account_return"]
        )
        if spec.name == "current":
            assert_current_anchor(d_metrics, combo_metrics)
            baseline_d = d_metrics
            baseline_combo = combo_metrics
        if baseline_d is None or baseline_combo is None:
            raise RuntimeError("RULES必须把current放在第一项")
        d_improved = float(d_metrics["equity_multiple"]) > float(
            baseline_d["equity_multiple"]
        ) + TOLERANCE
        combo_improved = float(combo_metrics["equity_multiple"]) > float(
            baseline_combo["equity_multiple"]
        ) + TOLERANCE
        first_half = executed_metrics(
            standalone[standalone["signal_date"].between(DECISION_START, FIRST_HALF_END)]
        )
        second_half = executed_metrics(
            standalone[standalone["signal_date"].between(SECOND_HALF_START, DECISION_END)]
        )
        rows.append(
            {
                "rule": spec.name,
                "description": spec.description,
                "raw_pool_count": int(len(selected_pool)),
                "candidate_day_count": int(len(picks)),
                "candidate_pool_count": candidate_metrics["sample_count"],
                "candidate_pool_diagnostic_multiple": candidate_metrics[
                    "diagnostic_multiple"
                ],
                **flat_metrics("d", d_metrics),
                **flat_metrics("combo", combo_metrics),
                "combo_d_count": int(combo_metrics["leg_counts"].get("D", 0)),
                "combo_a_count": int(combo_metrics["leg_counts"].get("A", 0)),
                "combo_e_count": int(combo_metrics["leg_counts"].get("E", 0)),
                "combo_c_count": int(combo_metrics["leg_counts"].get("C", 0)),
                "d_first_12m_trade_count": first_half["trade_count"],
                "d_first_12m_multiple": first_half["equity_multiple"],
                "d_second_12m_trade_count": second_half["trade_count"],
                "d_second_12m_multiple": second_half["equity_multiple"],
                "d_compound_improved": d_improved,
                "combo_compound_improved": combo_improved,
                "dual_gate_passed": bool(d_improved and combo_improved),
                "release_eligible": False,
            }
        )
        outcome_frames[spec.name] = outcomes
        standalone_details[spec.name] = standalone
        LOGGER.info(
            "%s：D=%d笔/%.6f倍，ACDE=%d笔/%.6f倍，双门槛=%s",
            spec.name,
            int(d_metrics["trade_count"]),
            float(d_metrics["equity_multiple"]),
            int(combo_metrics["trade_count"]),
            float(combo_metrics["equity_multiple"]),
            bool(d_improved and combo_improved),
        )
    return pd.DataFrame(rows), outcome_frames, standalone_details


def evaluate_prior_six_months(
    pool: pd.DataFrame, outcome_cache: DOutcomeCache
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in RULES:
        selected_pool = pool[spec.predicate(pool).fillna(False)].copy()
        picks = build_daily_candidate_ledger(selected_pool)
        outcomes = outcome_cache.frame(picks)
        detail = replay_d_only(outcomes, PRIOR_START, PRIOR_END)
        metrics = executed_metrics(detail)
        rows.append(
            {
                "rule": spec.name,
                "description": spec.description,
                "role": "read_only_side_validation_not_decision_gate",
                "raw_pool_count": int(len(selected_pool)),
                "candidate_day_count": int(len(picks)),
                **flat_metrics("d", metrics),
                "sample_sufficiency": (
                    "INSUFFICIENT_DATA" if int(metrics["trade_count"]) < 20 else "OBSERVABLE"
                ),
                "may_change_24m_decision": False,
            }
        )
    return pd.DataFrame(rows)


def changed_rows(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    columns: list[str],
) -> pd.DataFrame:
    left = baseline[columns].copy()
    right = candidate[columns].copy()
    merged = left.merge(
        right,
        on="signal_date",
        how="outer",
        suffixes=("_current", "_candidate"),
        indicator=True,
    )
    left_code = merged.get("ts_code_current", pd.Series("", index=merged.index)).fillna("")
    right_code = merged.get("ts_code_candidate", pd.Series("", index=merged.index)).fillna("")
    return merged[(merged["_merge"] != "both") | left_code.ne(right_code)].copy()


def winsorized_multiple(values: pd.Series, low: float = 0.05, high: float = 0.95) -> float:
    array = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(array) == 0:
        return 1.0
    lower, upper = np.quantile(array, [low, high])
    return float(mechanical_compound(np.clip(array, lower, upper)).equity_multiple)


def outlier_robustness(
    baseline_detail: pd.DataFrame, candidate_detail: pd.DataFrame
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, detail in (("current", baseline_detail), ("first_before_1400", candidate_detail)):
        values = detail.loc[detail["status"].eq("EXECUTED"), "account_return"]
        array = pd.to_numeric(values, errors="raise").to_numpy(dtype=float)
        without_best = np.delete(array, int(array.argmax())) if len(array) else array
        without_worst = np.delete(array, int(array.argmin())) if len(array) else array
        result[label] = {
            "trade_count": int(len(array)),
            "best_return": float(array.max()) if len(array) else 0.0,
            "worst_return": float(array.min()) if len(array) else 0.0,
            "multiple_without_best_trade": float(
                mechanical_compound(without_best).equity_multiple
            ),
            "multiple_without_worst_trade": float(
                mechanical_compound(without_worst).equity_multiple
            ),
            "winsorized_5_95_multiple": winsorized_multiple(values),
        }
    return result


def first_board_failed_touch_audit(strong_dates: list[str]) -> pd.DataFrame:
    """审计收盘涨停池之外的首板触板失败上界。

    日线只能证明股票当日触及涨停后未收在涨停，不能证明它是否出现2~3次炸板，
    也不能证明14:00后曾形成可买回封。因此本表是“缺失母样本的上界证据”，
    不能把每一条失败触板都算成D的真实失败交易。
    """

    calendar = calendar_dates("20190101", "20991231")
    date_index = {date: index for index, date in enumerate(calendar)}
    basic = pd.read_csv(STOCK_BASIC_PATH, dtype=str, low_memory=False)
    basic = basic.drop_duplicates("ts_code", keep="last").set_index("ts_code")
    metadata = {
        str(code): (
            str(row.get("name", "") or ""),
            str(row.get("list_date", "") or "").replace(".0", ""),
        )
        for code, row in basic.iterrows()
    }

    def resolved_limit(code: str, date: str, pre_close: float) -> float | None:
        name, list_date = metadata.get(code, ("", ""))
        listing_day = None
        if len(list_date) == 8 and list_date <= date:
            listing_day = bisect_right(calendar, date) - bisect_left(calendar, list_date)
        return limit_up_price(
            pre_close,
            price_limit_pct(
                code,
                name=name,
                trade_date=date,
                listing_day_number=listing_day,
            ),
        )

    rows: list[dict[str, Any]] = []
    for date in strong_dates:
        index = date_index.get(date)
        daily_path = DAILY_DIR / f"{date}.csv"
        if index is None or index == 0 or not daily_path.exists():
            raise FileNotFoundError(f"首板触板审计缺少交易日或日线：{date}")
        previous_date = calendar[index - 1]
        previous_limit_path = LIMIT_LIST_DIR / f"{previous_date}.csv"
        if not previous_limit_path.exists():
            raise FileNotFoundError(f"首板触板审计缺少昨日涨停池：{previous_limit_path}")
        previous_limit = pd.read_csv(
            previous_limit_path, dtype={"ts_code": str}, low_memory=False
        )
        previous_codes = set(previous_limit["ts_code"].astype(str))
        daily = pd.read_csv(daily_path, dtype={"ts_code": str}, low_memory=False)
        touched = failed_close = 0
        for row in daily.itertuples(index=False):
            code = str(row.ts_code)
            name, _ = metadata.get(code, ("", ""))
            if (
                code in previous_codes
                or market_segment(code) not in ALLOWED_SEGMENTS
                or is_st_name(name)
            ):
                continue
            pre_close = float(row.pre_close or 0.0)
            high = float(row.high or 0.0)
            close = float(row.close or 0.0)
            cap = resolved_limit(code, date, pre_close) if pre_close > 0 else None
            if cap is None or high < cap - 1e-9:
                continue
            touched += 1
            if close < cap - 1e-9:
                failed_close += 1
        rows.append(
            {
                "trade_date": date,
                "first_board_daily_high_touched": touched,
                "touched_but_not_limit_close": failed_close,
                "limit_close_after_touch": touched - failed_close,
            }
        )
    return pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)


def research_decision(
    *, dual_gate_passed: bool, complete_intraday_event_pool: bool
) -> str:
    """双复利是必要条件；完整失败路径是盘中打板策略的发布前置条件。"""

    if not dual_gate_passed:
        return "KEEP_CURRENT_DUAL_GATE_NOT_PASSED"
    if not complete_intraday_event_pool:
        return "KEEP_CURRENT_PENDING_COMPLETE_INTRADAY_EVENT_POOL"
    return "RESEARCH_CANDIDATE_READY_FOR_SEPARATE_CERTIFICATION"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.info("加载并校验严格D研究特征池")
    features, source_audit = load_feature_pool()
    decision_pool = d_pool(features, DECISION_START, DECISION_END)
    prior_pool = d_pool(features, PRIOR_START, PRIOR_END)
    LOGGER.info(
        "D静态收盘幸存池：决策窗口%d行/%d日；更早6个月%d行/%d日",
        len(decision_pool),
        decision_pool["trade_date"].nunique(),
        len(prior_pool),
        prior_pool["trade_date"].nunique(),
    )

    daily_data = strict.daily_data()
    outcome_cache = DOutcomeCache(daily_data)
    other_legs = build_current_other_legs()
    rule_search, outcomes, standalone = evaluate_rules(
        decision_pool, outcome_cache, other_legs
    )
    prior = evaluate_prior_six_months(prior_pool, outcome_cache)
    groups = factor_groups(outcomes["current"])

    baseline_picks = outcomes["current"]
    candidate_picks = outcomes["first_before_1400"]
    changed_picks = changed_rows(
        baseline_picks,
        candidate_picks,
        ["signal_date", "ts_code", "name", "first_time", "open_times"],
    )
    baseline_executed = standalone["current"][
        standalone["current"]["status"].eq("EXECUTED")
    ]
    candidate_executed = standalone["first_before_1400"][
        standalone["first_before_1400"]["status"].eq("EXECUTED")
    ]
    changed_executed = changed_rows(
        baseline_executed,
        candidate_executed,
        ["signal_date", "ts_code", "name", "account_return"],
    )

    LOGGER.info("审计收盘涨停池遗漏的首板失败触板上界")
    failed_touch = first_board_failed_touch_audit(
        sorted(decision_pool["trade_date"].unique())
    )
    failed_total = int(failed_touch["touched_but_not_limit_close"].sum())
    touched_total = int(failed_touch["first_board_daily_high_touched"].sum())
    failed_days = int((failed_touch["touched_but_not_limit_close"] > 0).sum())

    rule_row = rule_search.set_index("rule").loc["first_before_1400"]
    decision = research_decision(
        dual_gate_passed=bool(rule_row["dual_gate_passed"]),
        complete_intraday_event_pool=False,
    )
    robustness = outlier_robustness(
        standalone["current"], standalone["first_before_1400"]
    )
    valid_baseline = baseline_picks[baseline_picks["status"].eq("OK")].copy()
    valid_baseline["account_return"] = pd.to_numeric(
        valid_baseline["account_return"], errors="raise"
    )

    def tail_examples(sample: pd.DataFrame) -> list[dict[str, Any]]:
        return [
            {
                "signal_date": str(row["signal_date"]),
                "ts_code": str(row["ts_code"]),
                "name": str(row.get("name", "")),
                "account_return": float(row["account_return"]),
            }
            for _, row in sample.iterrows()
        ]

    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "research_protocol": STRICT_DISCOVERY,
        "strategy": "D",
        "release_eligible": False,
        "formal_rule_modified": False,
        "decision_window": {
            "start": DECISION_START,
            "end": DECISION_END,
            "role": "only_rule_discovery_and_update_decision_window",
        },
        "prior_six_months": {
            "start": PRIOR_START,
            "end": PRIOR_END,
            "role": "read_only_side_validation_not_decision_gate",
        },
        "source_audit": source_audit,
        "frozen_portfolio_standard": {
            "priority": "D>A>E>C",
            "position_pct": strict.POSITION_PCT,
            "d_fill_stress": strict.D_FILL_STRESS,
            "fees_slippage_limit_rules_t1_unchanged": True,
            "current_combo_trade_count": EXPECTED_COMBO_TRADE_COUNT,
            "current_combo_equity_multiple": EXPECTED_COMBO_MULTIPLE,
            "current_combo_leg_counts": EXPECTED_COMBO_LEG_COUNTS,
        },
        "static_closing_survivor_pool": {
            "row_count": int(len(decision_pool)),
            "signal_day_count": int(decision_pool["trade_date"].nunique()),
        },
        "failed_touch_upper_bound_audit": {
            "strong_day_count": int(len(failed_touch)),
            "first_board_daily_high_touched": touched_total,
            "touched_but_not_limit_close": failed_total,
            "days_with_failed_close_touch": failed_days,
            "median_failed_close_touch_per_day": float(
                failed_touch["touched_but_not_limit_close"].median()
            ),
            "max_failed_close_touch_per_day": int(
                failed_touch["touched_but_not_limit_close"].max()
            ),
            "interpretation": (
                "只证明收盘涨停池遗漏了大量失败路径；日线不能判断其中多少满足"
                "炸板2~3次且14:00后真实回封，不能直接记作D失败交易。"
            ),
        },
        "best_structural_candidate": {
            "rule": "first_before_1400",
            "description": "首次封板早于14:00，正式买点仍要求14:00后真实回封",
            "d_trade_count": int(rule_row["d_trade_count"]),
            "d_equity_multiple": float(rule_row["d_equity_multiple"]),
            "d_max_drawdown": float(rule_row["d_max_drawdown"]),
            "combo_trade_count": int(rule_row["combo_trade_count"]),
            "combo_equity_multiple": float(rule_row["combo_equity_multiple"]),
            "combo_max_drawdown": float(rule_row["combo_max_drawdown"]),
            "dual_gate_passed_in_closing_survivors": bool(rule_row["dual_gate_passed"]),
            "changed_daily_pick_count": int(len(changed_picks)),
            "changed_executed_trade_count": int(len(changed_executed)),
            "outlier_robustness": robustness,
        },
        "current_daily_first_candidate_tail": {
            "scope": "candidate_pool_diagnostic_not_standalone_compound",
            "candidate_count": int(len(valid_baseline)),
            "explosion_count_gte_10pct": int(
                (valid_baseline["account_return"] >= 0.10).sum()
            ),
            "big_loss_count_lte_minus_5pct": int(
                (valid_baseline["account_return"] <= -0.05).sum()
            ),
            "largest_profits": tail_examples(
                valid_baseline.nlargest(5, "account_return")
            ),
            "largest_losses": tail_examples(
                valid_baseline.nsmallest(7, "account_return")
            ),
        },
        "formal_decision": decision,
        "formal_decision_reason": (
            "当前历史D母池来自收盘涨停幸存者，缺少触板后失败收盘及真实盘中回封路径；"
            "双复利只能解释收盘幸存者内部差异，不能认证真实盘中策略收益。"
        ),
        "next_required_research": (
            "建立覆盖全部首板触板股票（含失败收盘）的分钟/逐笔事件账本，按第一次"
            "可交易回封、排队成交和14:55撤单重放，再重新执行D独立腿与ACDE双门槛。"
        ),
        "limitations": [
            "最近24个月内发现并比较多个候选，属于样本内STRICT_DISCOVERY。",
            "更早6个月实际独立D样本极少，只能披露，不能提供统计确认。",
            "市场连板数和题材主线等收盘特征只能解释幸存者，未做14:00时点重建前不能直接进入盘中选股。",
            "机械复利不代表资金容量、真实排队成交或未来收益。",
        ],
    }

    rule_search.to_csv(OUTPUT_DIR / "rule_search.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(OUTPUT_DIR / "factor_groups.csv", index=False, encoding="utf-8-sig")
    baseline_picks.to_csv(
        OUTPUT_DIR / "baseline_daily_candidate_outcomes.csv",
        index=False,
        encoding="utf-8-sig",
    )
    prior.to_csv(OUTPUT_DIR / "prior_six_months.csv", index=False, encoding="utf-8-sig")
    changed_picks.to_csv(
        OUTPUT_DIR / "changed_daily_picks.csv", index=False, encoding="utf-8-sig"
    )
    changed_executed.to_csv(
        OUTPUT_DIR / "changed_executed_trades.csv", index=False, encoding="utf-8-sig"
    )
    failed_touch.to_csv(
        OUTPUT_DIR / "failed_touch_by_day.csv", index=False, encoding="utf-8-sig"
    )
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    LOGGER.info("D研究结论：%s", decision)
    LOGGER.info("研究产物：%s", OUTPUT_DIR)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
