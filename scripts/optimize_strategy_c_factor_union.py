#!/usr/bin/env python3
"""半年重算C固定因子if分支，并把全部达标分支按OR合并回测。

每条分支是1～3个T日已知因子值的AND组合。分支只有在独立单账户实际执行
至少配置笔数、平均账户收益严格大于2%、胜率严格大于55%时才入选。全部
入选分支不做样本内择优或子集搜索，直接OR合并为候选C；只有候选C独立复利
和按D>A>E>C逐腿替换后的ACDE复利同时高于当前正式版本，才允许--apply发布。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
from functools import lru_cache
import hashlib
import itertools
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable, Iterator, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts.optimize_strategy_d_factor_union import (  # noqa: E402
    build_incumbent_and_other_legs,
    latest_completed_update_node,
    load_events as load_d_events,
    natural_window_start,
    next_calendar_day,
)
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    reject_strategy_risk_mask,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.market_rules import limit_up_price, listing_trade_day_number, price_limit_pct  # noqa: E402
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strategy_c_factor_rules import (  # noqa: E402
    FACTOR_COLUMNS,
    FACTOR_SCHEMA_ID,
    FACTOR_UNION_MODE,
    LEGACY_MODE,
    MISSING_FACTOR_VALUE,
    add_factor_values,
    load_factor_release,
    normalize_factor_value,
    profile_mask,
)
from src.strategy_d_factor_rules import (  # noqa: E402
    add_factor_values as add_d_factor_values,
    load_factor_release as load_d_factor_release,
)
from src.strict_asof import STRICT_DISCOVERY, assert_selection_columns_strict  # noqa: E402


LOGGER = logging.getLogger("optimize_strategy_c_factor_union")
DEFAULT_CONFIG = ROOT / "config/strategy_c_factor_optimizer.json"
STRATEGY_CONFIG = ROOT / "config/strategy_config.json"
CURRENT_OFFICIAL_START = "20240630"
CURRENT_OFFICIAL_END = "20260630"
CURRENT_C_TRADES = 35
CURRENT_C_MULTIPLE = 3.1108307989904436
CURRENT_ACDE_TRADES = 129
CURRENT_ACDE_MULTIPLE = 486.3661434308374
CURRENT_ACDE_LEG_COUNTS = {"A": 47, "C": 21, "D": 17, "E": 44}
TOLERANCE = 1e-12

METRIC_KEYS = (
    "trade_count", "win_rate", "avg_account_return", "median_account_return",
    "equity_multiple", "max_drawdown", "max_profit", "max_loss",
    "profit_loss_ratio", "max_consecutive_losses", "first_12m_trade_count",
    "first_12m_multiple", "second_12m_trade_count", "second_12m_multiple",
    "candidate_day_count", "execution_ok_day_count", "unbuyable_day_count",
    "unresolved_exit_count",
)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def profile_identifier(conditions: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(sorted(conditions.items())), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    return "CIF_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def selection_signature(picks: pd.DataFrame) -> str:
    values = "\n".join(
        f"{row.trade_date}|{row.ts_code}"
        for row in picks[["trade_date", "ts_code"]].itertuples(index=False)
    )
    return hashlib.sha1(values.encode("utf-8")).hexdigest()


def split_boundary(start: str) -> tuple[str, str]:
    parsed = dt.datetime.strptime(start, "%Y%m%d").date()
    try:
        first_end = parsed.replace(year=parsed.year + 1)
    except ValueError:
        first_end = parsed.replace(year=parsed.year + 1, day=28)
    first = first_end.strftime("%Y%m%d")
    return first, next_calendar_day(first)


def load_optimizer_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("C因子优化配置根节点必须是对象")
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise ValueError("C因子优化配置schema_version不支持")
    if str(payload.get("factor_schema_id", "")) != FACTOR_SCHEMA_ID:
        raise ValueError("C因子优化配置factor_schema_id不匹配")
    return payload


def build_generator(config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(
        STRATEGY_CONFIG, input_trades_path=strict.STRICT_SOURCE
    )
    generator.config = config
    generator.paper_config = config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


def current_a_signal_dates(
    config: dict[str, Any], all_candidates: pd.DataFrame, start: str, end: str
) -> set[str]:
    generator = build_generator(config)
    pool = generator.apply_strategy_filters(all_candidates)
    pool = pool[pool["trade_date"].astype(str).between(start, end)]
    # A没有独立风险拒绝层；过滤后当日只要还有一行，就会选出首选并压过C。
    return set(pool["trade_date"].astype(str).unique())


def build_mother_pool(
    start: str, end: str
) -> tuple[pd.DataFrame, PaperCandidateGenerator, dict[str, Any]]:
    config = strict.load_json_config(STRATEGY_CONFIG)
    mother_config = condition_strategy_config(
        config, [], "strategy_c_factor_union_mother_pool"
    )
    generator = build_generator(mother_config)
    all_candidates = generator.load_all_candidates()
    a_dates = current_a_signal_dates(config, all_candidates, start, end)
    pool = generator.apply_strategy_filters(all_candidates)
    pool["trade_date"] = pool["trade_date"].astype(str)
    pool = pool[pool["trade_date"].between(start, end)].copy()
    before_a_gate = len(pool)
    pool = pool[~pool["trade_date"].isin(a_dates)].copy()
    factorized = add_factor_values(pool).reset_index(drop=True)
    factorized["_mother_row_id"] = np.arange(len(factorized), dtype=int)
    assert_selection_columns_strict(
        FACTOR_COLUMNS,
        context="optimize_strategy_c_factor_union.build_mother_pool",
    )
    ranked = generator.rank_candidates(factorized)
    ranked = ranked.sort_values(
        ["trade_date", "candidate_rank", "ts_code"],
        ascending=[True, True, True],
    ).reset_index(drop=True)
    ranked["_risk_rejected"] = reject_strategy_risk_mask(
        ranked, config, "c_strategy"
    ).astype(bool).to_numpy()
    return ranked, generator, {
        "all_reliable_candidate_count": int(len(all_candidates)),
        "window_rows_before_a_gate": int(before_a_gate),
        "a_blocked_signal_day_count": int(len(a_dates)),
        "mother_pool_row_count": int(len(ranked)),
        "mother_pool_signal_day_count": int(ranked["trade_date"].nunique()),
        "risk_rejected_row_count": int(ranked["_risk_rejected"].sum()),
    }


def attach_outcomes(pool: pd.DataFrame) -> pd.DataFrame:
    """一次计算母池逐票执行结果，后续所有条件只做只读索引。"""

    rows: list[dict[str, Any]] = []
    total = len(pool)
    for position, row in enumerate(pool.to_dict("records"), 1):
        signal_date = str(row["trade_date"])
        ts_code = str(row["ts_code"])
        result = trade_return_details(
            signal_date, ts_code, 3, name=str(row.get("name", ""))
        )
        value = None
        if result.status == "OK" and result.stock_return is not None:
            value = strict.account_return(result.stock_return, result.exit_date)
        rows.append(
            {
                "_mother_row_id": int(row["_mother_row_id"]),
                "status": result.status,
                "buy_date": result.buy_date,
                "exit_date": result.exit_date,
                "stock_return_before_fees": result.stock_return,
                "account_return": value,
                "exit_hit_limit_up": bool(
                    result.status == "OK"
                    and hit_limit_up(result.exit_date, ts_code, str(row.get("name", "")))
                ),
            }
        )
        if position % 5000 == 0 or position == total:
            LOGGER.info("C母池逐票执行结果：%d/%d", position, total)
    return pool.merge(pd.DataFrame(rows), on="_mother_row_id", how="left", validate="one_to_one")


@lru_cache(maxsize=None)
def hit_limit_up(trade_date: str, ts_code: str, name: str) -> bool:
    """复现正式交接判断，但逐日行情只读取一次，供大规模分支回放。"""

    data = cached_daily_data()
    frame = data.day(str(trade_date))
    if frame.empty or str(ts_code) not in frame.index:
        return False
    row = frame.loc[str(ts_code)]
    pre_close = float(row.get("pre_close", 0) or 0)
    high = float(row.get("high", 0) or 0)
    if pre_close <= 0 or high <= 0:
        return False
    resolved_name = str(name or "")
    list_date = ""
    if str(ts_code) in data.stock_basic.index:
        basic = data.stock_basic.loc[str(ts_code)]
        resolved_name = resolved_name or str(basic.get("name", "") or "")
        list_date = str(basic.get("list_date", "") or "").replace(".0", "")
    listing_day = listing_trade_day_number(
        list_date, str(trade_date), data.trade_dates
    )
    cap = limit_up_price(
        pre_close,
        price_limit_pct(
            str(ts_code), name=resolved_name, trade_date=str(trade_date),
            listing_day_number=listing_day,
        ),
    )
    return bool(
        cap is not None
        and high >= cap - float(strict.cert.TAKEPROFIT_OFFSET) - 1e-9
    )


@lru_cache(maxsize=1)
def cached_daily_data() -> strict.DailyData:
    return strict.daily_data()


def standalone_records(picks: pd.DataFrame) -> list[tuple[str, float]]:
    occupied_until = ""
    occupied_handoff_allowed = False
    records: list[tuple[str, float]] = []
    columns = [
        "trade_date", "status", "exit_date", "account_return", "exit_hit_limit_up"
    ]
    for signal, status, exit_date, account_return, handoff_allowed in picks[columns].itertuples(
        index=False, name=None
    ):
        signal_date = str(signal)
        if occupied_until and signal_date < occupied_until:
            continue
        blocking_handoff = bool(
            occupied_until
            and signal_date == occupied_until
            and not occupied_handoff_allowed
        )
        occupied_until = ""
        occupied_handoff_allowed = False
        if blocking_handoff or str(status) != "OK":
            continue
        records.append((signal_date, float(account_return)))
        occupied_until = str(exit_date)
        occupied_handoff_allowed = bool(handoff_allowed)
    return records


def basic_metrics(values: Iterable[float]) -> dict[str, Any]:
    array = np.asarray(list(values), dtype=float)
    if len(array) == 0:
        return {
            "trade_count": 0, "win_rate": 0.0, "avg_account_return": 0.0,
            "median_account_return": 0.0, "equity_multiple": 1.0,
            "max_drawdown": 0.0, "max_profit": 0.0, "max_loss": 0.0,
            "profit_loss_ratio": 0.0, "max_consecutive_losses": 0,
        }
    compound = mechanical_compound(array)
    gains = array[array > 0]
    losses = array[array < 0]
    maximum = current = 0
    for value in array:
        current = current + 1 if value <= 0 else 0
        maximum = max(maximum, current)
    return {
        "trade_count": int(len(array)),
        "win_rate": float((array > 0).mean()),
        "avg_account_return": float(array.mean()),
        "median_account_return": float(np.median(array)),
        "equity_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "max_profit": float(array.max()),
        "max_loss": float(array.min()),
        "profit_loss_ratio": (
            float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0
        ),
        "max_consecutive_losses": int(maximum),
    }


def metrics_from_picks(
    picks: pd.DataFrame, first_half_end: str, second_half_start: str
) -> dict[str, Any]:
    records = standalone_records(picks)
    values = [value for _, value in records]
    first = [
        value for signal_date, value in records if signal_date <= first_half_end
    ]
    second = [
        value for signal_date, value in records if signal_date >= second_half_start
    ]
    statuses = picks["status"].astype(str)
    return {
        **basic_metrics(values),
        "first_12m_trade_count": int(len(first)),
        "first_12m_multiple": basic_metrics(first)["equity_multiple"],
        "second_12m_trade_count": int(len(second)),
        "second_12m_multiple": basic_metrics(second)["equity_multiple"],
        "candidate_day_count": int(len(picks)),
        "execution_ok_day_count": int(statuses.eq("OK").sum()),
        "unbuyable_day_count": int(statuses.eq("LIMIT_UP_UNBUYABLE").sum()),
        "unresolved_exit_count": int(
            (~statuses.isin({"OK", "LIMIT_UP_UNBUYABLE", "NO_PRICE", "NO_CALENDAR"})).sum()
        ),
    }


def iter_groups(
    selected: pd.DataFrame, factor_names: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], pd.DataFrame]]:
    key: str | list[str] = factor_names[0] if len(factor_names) == 1 else list(factor_names)
    for values, group in selected.groupby(key, observed=True, sort=False, dropna=False):
        normalized = (str(values),) if len(factor_names) == 1 else tuple(map(str, values))
        yield normalized, group


def enumerate_profiles(
    pool: pd.DataFrame,
    *,
    max_factor_count: int,
    min_trade_count: int,
    min_avg_return: float,
    min_win_rate: float,
    start: str,
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], dict[str, Any]]:
    first_half_end, second_half_start = split_boundary(start)
    factor_sets = [
        names
        for count in range(1, max_factor_count + 1)
        for names in itertools.combinations(FACTOR_COLUMNS, count)
    ]
    rows: list[dict[str, Any]] = []
    conditions_by_id: dict[str, dict[str, str]] = {}
    observed = support = 0
    for position, factor_names in enumerate(factor_sets, 1):
        selected = pool.drop_duplicates(
            [*factor_names, "trade_date"], keep="first"
        )
        # 与每日执行一致：每个if分支先锁定首选；首选触发风险拒绝后不回补第二名。
        selected = selected.loc[
            ~selected["_risk_rejected"],
            [
                *factor_names, "trade_date", "ts_code", "name", "status",
                "exit_date", "account_return", "exit_hit_limit_up",
            ],
        ]
        for values, group in iter_groups(selected, factor_names):
            observed += 1
            if MISSING_FACTOR_VALUE in values or len(group) < min_trade_count:
                continue
            support += 1
            conditions = dict(zip(factor_names, values))
            metrics = metrics_from_picks(group, first_half_end, second_half_start)
            profile_id = profile_identifier(conditions)
            qualifies = bool(
                int(metrics["trade_count"]) >= min_trade_count
                and float(metrics["avg_account_return"]) > min_avg_return
                and float(metrics["win_rate"]) > min_win_rate
                and int(metrics["unresolved_exit_count"]) == 0
            )
            conditions_by_id[profile_id] = conditions
            rows.append(
                {
                    "profile_id": profile_id,
                    "factor_count": int(len(factor_names)),
                    "factor_names": ";".join(factor_names),
                    "conditions_json": json.dumps(conditions, ensure_ascii=False, sort_keys=True),
                    "description": " AND ".join(f"{name}={value}" for name, value in conditions.items()),
                    "selection_signature": selection_signature(group),
                    **metrics,
                    "threshold_qualified": qualifies,
                }
            )
        if position % 100 == 0 or position == len(factor_sets):
            LOGGER.info(
                "C因子组合进度：%d/%d组因子列，支持充分条件%d条",
                position, len(factor_sets), len(rows),
            )
    result = pd.DataFrame(rows)
    if result.empty:
        raise RuntimeError("C因子组合没有任何满足最低支持度的条件")
    return result, conditions_by_id, {
        "factor_column_count": int(len(FACTOR_COLUMNS)),
        "factor_set_count": int(len(factor_sets)),
        "observed_factor_value_group_count": int(observed),
        "support_passed_profile_count": int(support),
        "evaluated_profile_count": int(len(result)),
    }


def union_picks(
    pool: pd.DataFrame,
    qualified: pd.DataFrame,
    conditions_by_id: Mapping[str, Mapping[str, str]],
) -> tuple[pd.DataFrame, np.ndarray]:
    mask = np.zeros(len(pool), dtype=bool)
    for profile_id in qualified["profile_id"].astype(str):
        mask |= profile_mask(pool, conditions_by_id[profile_id]).to_numpy(dtype=bool)
    selected = pool.loc[mask].drop_duplicates("trade_date", keep="first")
    selected = selected[~selected["_risk_rejected"]].copy().reset_index(drop=True)
    return selected, mask


def outcome_frame(picks: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "trade_date", "ts_code", "name", "status", "buy_date", "exit_date",
        "stock_return_before_fees", "account_return", "matched_c_profile_ids",
    ]
    result = picks.copy()
    if "matched_c_profile_ids" not in result.columns:
        result["matched_c_profile_ids"] = ""
    result = result[[column for column in columns if column in result.columns]].copy()
    result = result.rename(columns={"trade_date": "signal_date"})
    result.insert(1, "strategy_leg", "C")
    return result.sort_values("signal_date").reset_index(drop=True)


@contextmanager
def strict_window(start: str, end: str) -> Iterator[None]:
    old_start, old_end = strict.START, strict.END
    strict.START, strict.END = start, end
    try:
        yield
    finally:
        strict.START, strict.END = old_start, old_end


def standalone_metrics(frame: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    maps = {leg: {} for leg in ("D", "A", "E", "C")}
    maps["C"] = strict.candidate_map(frame)
    with strict_window(start, end):
        detail = strict.replay(maps, {"C"})
        metrics = strict.combo_metrics(detail)
    return detail, metrics


def portfolio_metrics(
    d: pd.DataFrame,
    a: pd.DataFrame,
    e: pd.DataFrame,
    c: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    legs = {"D": d, "A": a, "E": e, "C": c}
    maps = {leg: strict.candidate_map(frame) for leg, frame in legs.items()}
    with strict_window(start, end):
        detail = strict.replay(maps, set(legs))
        metrics = strict.combo_metrics(detail)
    return detail, metrics


def assert_current_anchor(
    release: Mapping[str, Any], c: Mapping[str, Any], acde: Mapping[str, Any], start: str, end: str
) -> None:
    if not (
        start == CURRENT_OFFICIAL_START
        and end == CURRENT_OFFICIAL_END
        and str(release["strategy_mode"]) == LEGACY_MODE
    ):
        return
    actual_counts = {
        leg: int(acde.get("leg_counts", {}).get(leg, 0))
        for leg in ("A", "C", "D", "E")
    }
    failures: list[str] = []
    if int(c["trade_count"]) != CURRENT_C_TRADES:
        failures.append(f"C笔数={c['trade_count']}")
    if abs(float(c["equity_multiple"]) - CURRENT_C_MULTIPLE) > TOLERANCE:
        failures.append(f"C复利={c['equity_multiple']}")
    if int(acde["trade_count"]) != CURRENT_ACDE_TRADES:
        failures.append(f"ACDE笔数={acde['trade_count']}")
    if abs(float(acde["equity_multiple"]) - CURRENT_ACDE_MULTIPLE) > TOLERANCE:
        failures.append(f"ACDE复利={acde['equity_multiple']}")
    if actual_counts != CURRENT_ACDE_LEG_COUNTS:
        failures.append(f"ACDE腿分布={actual_counts}")
    if failures:
        raise RuntimeError("C半年因子优化正式锚点漂移：" + "；".join(failures))


def json_value(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def release_profiles(
    qualified: pd.DataFrame, conditions_by_id: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    ordered = qualified.sort_values(
        ["factor_count", "avg_account_return", "win_rate", "trade_count", "profile_id"],
        ascending=[True, False, False, False, True],
    )
    profiles: list[dict[str, Any]] = []
    for priority, (_, row) in enumerate(ordered.iterrows(), 1):
        profile_id = str(row["profile_id"])
        profiles.append(
            {
                "profile_id": profile_id,
                "priority": priority,
                "conditions": dict(conditions_by_id[profile_id]),
                "branch_metrics": {
                    key: json_value(row[key]) for key in METRIC_KEYS
                },
            }
        )
    return profiles


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_release(path: Path, incumbent: Mapping[str, Any], candidate: Mapping[str, Any]) -> Path:
    history = path.parent / "strategy_c_factor_release_history"
    history.mkdir(parents=True, exist_ok=True)
    archive = history / f"{incumbent.get('release_id', 'C_UNKNOWN')}.json"
    if path.exists() and not archive.exists():
        shutil.copy2(path, archive)
    write_json_atomic(path, candidate)
    return archive


def pct(value: Any) -> str:
    return f"{float(value):+.8%}"


def multiple(value: Any) -> str:
    return f"{float(value):.10f}倍"


def pseudo_code(profiles: Iterable[Mapping[str, Any]]) -> str:
    lines = ["# 机器生成的C达标if条件并集；发布后由收盘候选与严格回放共同读取", "allow_c = False"]
    for profile in profiles:
        expression = " and ".join(
            f"factors[{name!r}] == {value!r}"
            for name, value in profile["conditions"].items()
        )
        lines.append(f"if {expression}:  # {profile['profile_id']}")
        lines.append("    allow_c = True")
    return "\n".join(lines) + "\n"


def render_report(payload: Mapping[str, Any]) -> str:
    current = payload["incumbent"]
    candidate = payload["candidate_union"]
    profiles = payload["candidate_release"]["profiles"]
    gate = payload["branch_gate"]
    c0, c1 = current["c_metrics"], candidate["c_metrics"]
    p0, p1 = current["acde_metrics"], candidate["acde_metrics"]
    lines = [
        "C策略半年因子条件并集完整结果",
        "==============================",
        "",
        "【一眼结论】",
        f"达标if分支：{len(profiles)}条（全部OR合并，不选唯一最佳）",
        f"C复利：{multiple(c0['equity_multiple'])} → {multiple(c1['equity_multiple'])}",
        f"ACDE复利：{multiple(p0['equity_multiple'])} → {multiple(p1['equity_multiple'])}",
        f"双复利闸门：{'通过' if payload['replace_gate']['release_eligible'] else '不通过'}",
        f"正式C是否修改：{'是' if payload['formal_strategy_modified'] else '否'}",
        "",
        "一、锁定口径",
        "",
        f"窗口：{payload['window']['start']}～{payload['window']['end']}",
        "严格as-of、82.5%仓位、T+1开盘买、C为T+3收盘卖、涨跌停延期、费用和双边滑点不变。",
        "C只在A当日无候选时参与；组合严格按D > A > E > C单账户逐笔机械复利。",
        "默认只生成报告；只有双复利提高且显式--apply才修改正式C。",
        "",
        "二、分支门槛与搜索规模",
        "",
        f"固定T日因子：{payload['search_space']['factor_column_count']}个。",
        f"因子列组合：{payload['search_space']['factor_set_count']:,}组。",
        f"实际观察因子值组合：{payload['search_space']['observed_factor_value_group_count']:,}条。",
        f"支持度充分并完整评估：{payload['search_space']['evaluated_profile_count']:,}条。",
        f"最终达标：{len(profiles):,}条。",
        "门槛：独立单账户至少"
        f"{int(gate['trade_count_at_least'])}笔、平均每笔账户收益严格大于"
        f"{float(gate['avg_account_return_must_exceed']):.2%}、胜率严格大于"
        f"{float(gate['win_rate_must_exceed']):.2%}、退出全部可解析。",
        "",
        "三、C独立严格回测",
        "",
        "指标                 当前正式C              达标条件OR后的C",
        f"交易数               {int(c0['trade_count']):>10}              {int(c1['trade_count']):>10}",
        f"胜率                 {pct(c0['win_rate']):>14}       {pct(c1['win_rate']):>14}",
        f"平均每笔收益         {pct(c0['avg_account_return']):>14}       {pct(c1['avg_account_return']):>14}",
        f"中位数收益           {pct(c0['median_account_return']):>14}       {pct(c1['median_account_return']):>14}",
        f"机械复利             {multiple(c0['equity_multiple']):>16}     {multiple(c1['equity_multiple']):>16}",
        f"最大回撤             {pct(c0['max_drawdown']):>14}       {pct(c1['max_drawdown']):>14}",
        f"最大单笔盈利         {pct(c0['max_profit']):>14}       {pct(c1['max_profit']):>14}",
        f"最大单笔亏损         {pct(c0['max_loss']):>14}       {pct(c1['max_loss']):>14}",
        f"盈亏比               {float(c0['profit_loss_ratio']):>14.8f}       {float(c1['profit_loss_ratio']):>14.8f}",
        f"最大连续亏损         {int(c0['max_consecutive_losses']):>10}              {int(c1['max_consecutive_losses']):>10}",
        "",
        "四、逐腿替换后的ACDE",
        "",
        "指标                 当前正式ACDE           只替换C后的ACDE",
        f"交易数               {int(p0['trade_count']):>10}              {int(p1['trade_count']):>10}",
        f"胜率                 {pct(p0['win_rate']):>14}       {pct(p1['win_rate']):>14}",
        f"平均每笔收益         {pct(p0['avg_account_return']):>14}       {pct(p1['avg_account_return']):>14}",
        f"机械复利             {multiple(p0['equity_multiple']):>16}     {multiple(p1['equity_multiple']):>16}",
        f"最大回撤             {pct(p0['max_drawdown']):>14}       {pct(p1['max_drawdown']):>14}",
        f"当前腿分布：{json.dumps(p0.get('leg_counts', {}), ensure_ascii=False, sort_keys=True)}",
        f"候选腿分布：{json.dumps(p1.get('leg_counts', {}), ensure_ascii=False, sort_keys=True)}",
        "",
        "五、双复利替换闸门",
        "",
        f"C复利提高：{'通过' if payload['replace_gate']['c_compound_improved'] else '不通过'}",
        f"ACDE复利提高：{'通过' if payload['replace_gate']['acde_compound_improved'] else '不通过'}",
        f"候选具备替换资格：{'是' if payload['replace_gate']['release_eligible'] else '否'}",
        f"本次已修改正式C：{'是' if payload['formal_strategy_modified'] else '否'}",
        "",
        "六、全部达标if分支（OR关系）",
        "",
    ]
    if not profiles:
        lines.append("无达标分支，正式C保持不变。")
    for index, profile in enumerate(profiles, 1):
        conditions = " AND ".join(
            f"{name}={value}" for name, value in profile["conditions"].items()
        )
        metrics = profile["branch_metrics"]
        lines.append(
            f"{index}. {profile['profile_id']} | if {conditions} | "
            f"{int(metrics['trade_count'])}笔 | 胜率{float(metrics['win_rate']):.2%} | "
            f"平均{float(metrics['avg_account_return']):.2%} | 复利{float(metrics['equity_multiple']):.6f}倍"
        )
    lines.extend(
        [
            "",
            "七、风险说明",
            "",
            "1. 所有if分支来自同一24个月样本内搜索，2%和55%只是入选线，不代表未来表现。",
            "2. 全部分支直接OR可能产生交互和首选换票，因此最终只认并集C与ACDE的真实重放结果。",
            "3. 前后12个月只用于稳定性披露，不属于真正未来样本外；未来6个月才是前向账本。",
            "4. 机械复利只用于同口径版本比较，不代表资金容量或收益承诺。",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="半年优化C多因子if条件并集")
    parser.add_argument("--as-of", default=None, help="更新节点YYYYMMDD；默认最近0630/1231")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--apply", action="store_true",
        help="人工阅读报告后使用；仅双复利都提高时原子替换正式C",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = resolve_path(args.config)
    config = load_optimizer_config(config_path)
    end = str(args.as_of or latest_completed_update_node())
    dt.datetime.strptime(end, "%Y%m%d")
    if end[4:] not in {str(value) for value in config["allowed_update_nodes"]}:
        raise ValueError(f"C只允许在半年节点0630/1231更新，收到as_of={end}")
    start = natural_window_start(end, int(config["window_years"]))
    output_dir = (
        resolve_path(args.output_dir)
        if args.output_dir is not None
        else resolve_path(config["reports_root"]) / end
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    release_path = resolve_path(config["active_release_path"])
    incumbent_release = load_factor_release(release_path)

    LOGGER.info("构建C因子母池并锁定A无候选前提：%s~%s", start, end)
    mother, _generator, mother_audit = build_mother_pool(start, end)
    mother = attach_outcomes(mother)
    LOGGER.info(
        "开始枚举C固定因子组合：因子%d个，最多%d因子AND",
        len(FACTOR_COLUMNS), int(config["max_factor_count"]),
    )
    search, conditions_by_id, search_audit = enumerate_profiles(
        mother,
        max_factor_count=int(config["max_factor_count"]),
        min_trade_count=int(config["min_branch_trade_count"]),
        min_avg_return=float(config["min_branch_avg_account_return"]),
        min_win_rate=float(config["min_branch_win_rate"]),
        start=start,
    )
    qualified = search[search["threshold_qualified"]].copy()
    candidate_picks, union_mask = union_picks(mother, qualified, conditions_by_id)
    candidate_outcomes = outcome_frame(candidate_picks)

    d_event_path = resolve_path(config["d_event_path"])
    d_events, d_event_audit = load_d_events(d_event_path, start, end)
    d_factorized = add_d_factor_values(d_events)
    d_release = load_d_factor_release(ROOT / "config/strategy_d_factor_release.json")
    incumbent_d, other_legs, strict_source_audit = build_incumbent_and_other_legs(
        d_release, d_factorized, start, end
    )
    incumbent_c = other_legs["C"]
    incumbent_c_detail, incumbent_c_metrics = standalone_metrics(incumbent_c, start, end)
    candidate_c_detail, candidate_c_metrics = standalone_metrics(candidate_outcomes, start, end)
    incumbent_acde_detail, incumbent_acde_metrics = portfolio_metrics(
        incumbent_d, other_legs["A"], other_legs["E"], incumbent_c, start, end
    )
    candidate_acde_detail, candidate_acde_metrics = portfolio_metrics(
        incumbent_d, other_legs["A"], other_legs["E"], candidate_outcomes, start, end
    )
    assert_current_anchor(
        incumbent_release, incumbent_c_metrics, incumbent_acde_metrics, start, end
    )

    c_improved = bool(
        float(candidate_c_metrics["equity_multiple"])
        > float(incumbent_c_metrics["equity_multiple"]) + TOLERANCE
    )
    acde_improved = bool(
        float(candidate_acde_metrics["equity_multiple"])
        > float(incumbent_acde_metrics["equity_multiple"]) + TOLERANCE
    )
    release_eligible = bool(len(qualified) and c_improved and acde_improved)
    profiles = release_profiles(qualified, conditions_by_id)
    digest = hashlib.sha1(
        json.dumps(profiles, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    release_id = f"C_FACTOR_UNION_{end}_{digest}"
    candidate_release = {
        "schema_version": 1,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "release_id": release_id,
        "strategy_mode": FACTOR_UNION_MODE if profiles else LEGACY_MODE,
        "effective_from": next_calendar_day(end),
        "research_window": {"start": start, "end": end},
        "profiles": profiles,
        "selection_policy": str(config["selection_policy"]),
        "candidate_merge_policy": str(config["candidate_merge_policy"]),
        "branch_gate": {
            "trade_count_at_least": int(config["min_branch_trade_count"]),
            "avg_account_return_must_exceed": float(config["min_branch_avg_account_return"]),
            "win_rate_must_exceed": float(config["min_branch_win_rate"]),
        },
        "certified_metrics": {
            "c": candidate_c_metrics,
            "acde": candidate_acde_metrics,
        },
        "replaced_release_id": str(incumbent_release["release_id"]),
        "release_gate_passed": release_eligible,
        "human_review_required": True,
        "human_review_completed": False,
        "applied_at": "",
    }

    applied = False
    archive_path = ""
    if args.apply:
        if not release_eligible:
            LOGGER.warning("C条件并集未同时提高C和ACDE复利，正式C保持不变")
        else:
            candidate_release["human_review_completed"] = True
            candidate_release["applied_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            archive = apply_release(release_path, incumbent_release, candidate_release)
            archive_path = str(archive.relative_to(ROOT))
            applied = True
            LOGGER.warning("C达标条件并集已通过双门并发布：%s", release_id)

    summary = {
        "schema_version": 1,
        "protocol": STRICT_DISCOVERY,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "window": {"start": start, "end": end},
        "optimizer_config_path": str(config_path.relative_to(ROOT)),
        "optimizer_config_sha256": sha256(config_path),
        "strict_source_audit_passed": bool(strict_source_audit.get("passed")),
        "d_event_audit": d_event_audit,
        "mother_pool_audit": mother_audit,
        "search_space": {
            **search_audit,
            "max_factor_count": int(config["max_factor_count"]),
            "threshold_qualified_profile_count": int(len(qualified)),
            "factor_columns": list(FACTOR_COLUMNS),
            "union_mother_row_count": int(union_mask.sum()),
            "union_candidate_day_count": int(len(candidate_picks)),
        },
        "branch_gate": candidate_release["branch_gate"],
        "incumbent": {
            "release_id": str(incumbent_release["release_id"]),
            "strategy_mode": str(incumbent_release["strategy_mode"]),
            "c_metrics": incumbent_c_metrics,
            "acde_metrics": incumbent_acde_metrics,
        },
        "candidate_union": {
            "release_id": release_id,
            "profile_count": int(len(profiles)),
            "c_metrics": candidate_c_metrics,
            "acde_metrics": candidate_acde_metrics,
        },
        "replace_gate": {
            "c_compound_improved": c_improved,
            "acde_compound_improved": acde_improved,
            "release_eligible": release_eligible,
        },
        "candidate_release": candidate_release,
        "apply_requested": bool(args.apply),
        "formal_strategy_modified": applied,
        "archived_release_path": archive_path,
        "decision": (
            "REPLACE_FORMAL_C_WITH_ALL_QUALIFIED_OR_UNION"
            if applied else (
                "QUALIFIED_OR_UNION_PASSED_WAITING_HUMAN_DECISION"
                if release_eligible else "KEEP_INCUMBENT_C_DUAL_COMPOUND_GATE_FAILED"
            )
        ),
        "limitations": [
            "分支来自同一24个月内多重组合搜索，存在数据挖掘和过拟合风险。",
            "全部达标分支按用户规则直接OR，不做会再次引入样本内择优的子集搜索。",
            "前后12个月只作稳定性披露，更早6个月只作旁证，未来6个月才是真正前向样本外。",
            "机械复利仅用于相同执行口径版本比较，不代表未来收益或资金容量。",
        ],
    }

    search.sort_values(
        ["threshold_qualified", "avg_account_return", "win_rate", "equity_multiple"],
        ascending=[False, False, False, False],
    ).to_csv(output_dir / "all_factor_profiles.csv", index=False, encoding="utf-8-sig")
    qualified.sort_values(
        ["avg_account_return", "win_rate", "equity_multiple"], ascending=False
    ).to_csv(output_dir / "qualified_factor_profiles.csv", index=False, encoding="utf-8-sig")
    candidate_picks.to_csv(output_dir / "candidate_union_daily_picks.csv", index=False, encoding="utf-8-sig")
    incumbent_c_detail.to_csv(output_dir / "incumbent_c_detail.csv", index=False, encoding="utf-8-sig")
    candidate_c_detail.to_csv(output_dir / "candidate_c_detail.csv", index=False, encoding="utf-8-sig")
    incumbent_acde_detail.to_csv(output_dir / "incumbent_acde_detail.csv", index=False, encoding="utf-8-sig")
    candidate_acde_detail.to_csv(output_dir / "candidate_acde_detail.csv", index=False, encoding="utf-8-sig")
    (output_dir / "c_if_conditions.py.txt").write_text(pseudo_code(profiles), encoding="utf-8")
    write_json_atomic(output_dir / "candidate_release.json", candidate_release)
    write_json_atomic(output_dir / "summary.json", summary)
    report_path = output_dir / "best_c_factor_union.txt"
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "decision": summary["decision"],
                "best_result_file": str(report_path),
                "qualified_profile_count": int(len(profiles)),
                "candidate_c_multiple": candidate_c_metrics["equity_multiple"],
                "candidate_acde_multiple": candidate_acde_metrics["equity_multiple"],
                "candidate_release_eligible": release_eligible,
                "formal_strategy_modified": applied,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
