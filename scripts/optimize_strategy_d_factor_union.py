#!/usr/bin/env python3
"""半年重算D回封板因子组合，并把全部合格条件按OR并集守擂。

与只取单一Top1规则不同，本脚本先把信号时点字段转换成固定因子值，再枚举一至
三个不同因子的实际取值组合。每条条件分支必须同时满足：

* 独立单账户执行至少配置笔数；
* 平均每笔账户收益严格大于2%；
* 胜率严格大于55%；
* 没有无法解析的退出。

所有合格分支先按实际逐日选择序列去重，再取OR并集形成候选D。只有候选D复利和
D>A>E>C总复利都高于当前正式版本时，候选才具备替换资格。默认只生成报告；显式
传入``--apply``才会原子替换D因子发布文件。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
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
from scripts.research_strategy_d_explosion_features import (  # noqa: E402
    build_current_other_legs,
    executed_metrics,
    replay_d_only,
)
from scripts.research_strategy_d_reseal_combinations import (  # noqa: E402
    basic_metrics,
    combo_replay_fast,
    fast_standalone_records,
    outcome_frame_from_picks,
)
from src.strategy_d_factor_rules import (  # noqa: E402
    FACTOR_COLUMNS,
    FACTOR_SCHEMA_ID,
    FACTOR_UNION_MODE,
    LEGACY_MODE,
    MISSING_FACTOR_VALUE,
    add_factor_values,
    load_factor_release,
    profile_mask,
)
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("optimize_strategy_d_factor_union")
DEFAULT_CONFIG = ROOT / "config/strategy_d_factor_optimizer.json"
DEFAULT_EVENTS = ROOT / "reports/strategy_d_reseal_combinations/all_reseal_signal_events.csv"
CURRENT_OFFICIAL_START = "20240630"
CURRENT_OFFICIAL_END = "20260630"
CURRENT_D_MULTIPLE = 2.0261239235922566
CURRENT_ACDE_MULTIPLE = 327.72671897548867
CURRENT_D_TRADES = 39
CURRENT_ACDE_TRADES = 132
TOLERANCE = 1e-12


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def profile_identifier(conditions: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(sorted(conditions.items())),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "DIF_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def selection_signature(picks: pd.DataFrame) -> str:
    values = "\n".join(
        f"{row.trade_date}|{int(row.event_id)}"
        for row in picks[["trade_date", "event_id"]].itertuples(index=False)
    )
    return hashlib.sha1(values.encode("utf-8")).hexdigest()


def natural_window_start(end: str, years: int) -> str:
    value = dt.datetime.strptime(str(end), "%Y%m%d").date()
    try:
        start = value.replace(year=value.year - int(years))
    except ValueError:
        start = value.replace(year=value.year - int(years), day=28)
    return start.strftime("%Y%m%d")


def next_calendar_day(value: str) -> str:
    parsed = dt.datetime.strptime(str(value), "%Y%m%d").date()
    return (parsed + dt.timedelta(days=1)).strftime("%Y%m%d")


def split_boundary(start: str) -> tuple[str, str]:
    parsed = dt.datetime.strptime(str(start), "%Y%m%d").date()
    try:
        first_end = parsed.replace(year=parsed.year + 1)
    except ValueError:
        first_end = parsed.replace(year=parsed.year + 1, day=28)
    return first_end.strftime("%Y%m%d"), next_calendar_day(first_end.strftime("%Y%m%d"))


def load_optimizer_config(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("D因子优化配置根节点必须是对象")
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise ValueError("D因子优化配置schema_version不支持")
    if str(payload.get("factor_schema_id", "")) != FACTOR_SCHEMA_ID:
        raise ValueError("D因子优化配置factor_schema_id不匹配")
    return payload


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_events(path: Path, start: str, end: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"缺少D全部回封事件账本: {path}；先更新分钟母账本并重建全部回封事件"
        )
    data = pd.read_csv(path, low_memory=False)
    required = {
        "event_id", "trade_date", "ts_code", "signal_hhmm", "open_times_at_signal",
        "queue_price_confirmed", "execution_status", "exit_date", "account_return",
    }
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError(f"D全部回封事件账本缺少字段: {','.join(missing)}")
    data["trade_date"] = date_text(data["trade_date"])
    duplicate_count = int(data.duplicated("event_id").sum())
    if duplicate_count:
        raise ValueError(f"D全部回封事件event_id重复: {duplicate_count}")
    available_start = str(data["trade_date"].min())
    available_end = str(data["trade_date"].max())
    start_gap = (
        dt.datetime.strptime(available_start, "%Y%m%d").date()
        - dt.datetime.strptime(start, "%Y%m%d").date()
    ).days
    end_gap = (
        dt.datetime.strptime(end, "%Y%m%d").date()
        - dt.datetime.strptime(available_end, "%Y%m%d").date()
    ).days
    # 自然日锚点可能是周末/节假日，事件首尾无需恰好落在锚点；但若两端
    # 超过一周没有任何回封事件，无法证明输入属于该更新窗口，按缺数据拒绝。
    if start_gap < 0 or start_gap > 7 or end_gap < 0 or end_gap > 7:
        raise RuntimeError(
            "D全部回封事件没有覆盖目标两年窗口："
            f"target={start}~{end} available={available_start}~{available_end}"
        )
    sample = data[data["trade_date"].between(start, end)].copy()
    if sample.empty:
        raise RuntimeError("D目标两年窗口没有回封事件")
    sample["queue_price_confirmed"] = (
        sample["queue_price_confirmed"].astype(str).str.lower().isin({"true", "1", "yes"})
    )
    return sample, {
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "sha256": sha256(path),
        "available_start": available_start,
        "available_end": available_end,
        "window_event_count": int(len(sample)),
        "window_stock_day_count": int(sample[["trade_date", "ts_code"]].drop_duplicates().shape[0]),
        "window_trade_day_count": int(sample["trade_date"].nunique()),
        "duplicate_event_id_count": duplicate_count,
    }


def selection_sorted(events: pd.DataFrame) -> pd.DataFrame:
    ranked = events.copy()
    ranked["_open2_priority"] = (
        pd.to_numeric(ranked["open_times_at_signal"], errors="coerce").eq(2).astype(int)
    )
    return ranked.sort_values(
        ["trade_date", "signal_hhmm", "_open2_priority", "ts_code", "event_id"],
        ascending=[True, True, False, True, True],
    ).reset_index(drop=True)


def calendar_dates(start: str, end: str) -> list[str]:
    calendar = pd.read_csv(
        ROOT / "data/raw/trade_calendar.csv", dtype={"cal_date": str}, low_memory=False
    )
    if "is_open" in calendar.columns:
        calendar = calendar[
            calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
        ]
    return sorted(calendar.loc[calendar["cal_date"].between(start, end), "cal_date"])


def metrics_from_picks(
    picks: pd.DataFrame,
    calendar: list[str],
    first_half_end: str,
    second_half_start: str,
) -> dict[str, Any]:
    outcomes = outcome_frame_from_picks(picks)
    records = fast_standalone_records(outcomes, calendar)
    values = [float(record["account_return"]) for record in records]
    first = [
        float(record["account_return"])
        for record in records
        if str(record["signal_date"]) <= first_half_end
    ]
    second = [
        float(record["account_return"])
        for record in records
        if str(record["signal_date"]) >= second_half_start
    ]
    unresolved = int(
        (
            picks["queue_price_confirmed"]
            & ~picks["execution_status"].astype(str).eq("OK")
        ).sum()
    )
    return {
        **basic_metrics(values),
        "candidate_day_count": int(len(picks)),
        "price_confirmed_day_count": int(picks["queue_price_confirmed"].sum()),
        "queue_unknown_day_count": int((~picks["queue_price_confirmed"]).sum()),
        "unresolved_exit_count": unresolved,
        "first_12m_trade_count": len(first),
        "first_12m_multiple": basic_metrics(first)["equity_multiple"],
        "second_12m_trade_count": len(second),
        "second_12m_multiple": basic_metrics(second)["equity_multiple"],
    }


def flatten(prefix: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_{key}": value
        for key, value in metrics.items()
        if key not in {"compound_standard_id", "leg_counts"}
    }


def iter_factor_groups(
    sorted_events: pd.DataFrame,
    factor_names: tuple[str, ...],
) -> Iterator[tuple[tuple[str, ...], pd.DataFrame]]:
    group_key: str | list[str]
    group_key = factor_names[0] if len(factor_names) == 1 else list(factor_names)
    for raw_values, group in sorted_events.groupby(
        group_key, observed=True, sort=False, dropna=False
    ):
        values = (str(raw_values),) if len(factor_names) == 1 else tuple(map(str, raw_values))
        yield values, group


def enumerate_profiles(
    events: pd.DataFrame,
    *,
    max_factor_count: int,
    min_trade_count: int,
    min_avg_return: float,
    min_win_rate: float,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], dict[str, pd.DataFrame], dict[str, Any]]:
    sorted_events = selection_sorted(events)
    calendar = calendar_dates(start, end)
    first_half_end, second_half_start = split_boundary(start)
    rows: list[dict[str, Any]] = []
    conditions_by_id: dict[str, dict[str, str]] = {}
    picks_by_id: dict[str, pd.DataFrame] = {}
    observed_group_count = 0
    support_pass_count = 0
    factor_sets = [
        names
        for count in range(1, max_factor_count + 1)
        for names in itertools.combinations(FACTOR_COLUMNS, count)
    ]

    for position, factor_names in enumerate(factor_sets, 1):
        for values, group in iter_factor_groups(sorted_events, factor_names):
            observed_group_count += 1
            if MISSING_FACTOR_VALUE in values:
                continue
            conditions = dict(zip(factor_names, values))
            picks = group.drop_duplicates("trade_date", keep="first").copy()
            if int(picks["queue_price_confirmed"].sum()) < min_trade_count:
                continue
            support_pass_count += 1
            metrics = metrics_from_picks(
                picks, calendar, first_half_end, second_half_start
            )
            profile_id = profile_identifier(conditions)
            qualifies = bool(
                int(metrics["trade_count"]) >= min_trade_count
                and float(metrics["avg_account_return"]) > min_avg_return
                and float(metrics["win_rate"]) > min_win_rate
                and int(metrics["unresolved_exit_count"]) == 0
            )
            conditions_by_id[profile_id] = conditions
            picks_by_id[profile_id] = picks
            rows.append(
                {
                    "profile_id": profile_id,
                    "factor_count": len(factor_names),
                    "factor_names": ";".join(factor_names),
                    "conditions_json": json.dumps(
                        conditions, ensure_ascii=False, sort_keys=True
                    ),
                    "description": " AND ".join(
                        f"{name}={value}" for name, value in conditions.items()
                    ),
                    "raw_event_count": int(len(group)),
                    "selection_signature": selection_signature(picks),
                    **metrics,
                    "threshold_qualified": qualifies,
                }
            )
        if position % 40 == 0 or position == len(factor_sets):
            LOGGER.info(
                "D因子组合进度：%d/%d组因子列，已评估支持充分条件%d条",
                position,
                len(factor_sets),
                len(rows),
            )

    result = pd.DataFrame(rows)
    if result.empty:
        raise RuntimeError("D因子组合没有任何满足最低支持度的条件")
    audit = {
        "factor_column_count": len(FACTOR_COLUMNS),
        "factor_set_count": len(factor_sets),
        "observed_factor_value_group_count": observed_group_count,
        "support_passed_profile_count": support_pass_count,
        "evaluated_profile_count": int(len(result)),
    }
    return result, conditions_by_id, picks_by_id, audit


def deduplicate_qualified_profiles(search: pd.DataFrame) -> pd.DataFrame:
    qualified = search[search["threshold_qualified"]].copy()
    if qualified.empty:
        return qualified
    return (
        qualified.sort_values(
            [
                "factor_count", "avg_account_return", "win_rate",
                "equity_multiple", "trade_count", "profile_id",
            ],
            ascending=[True, False, False, False, False, True],
        )
        .drop_duplicates("selection_signature", keep="first")
        .reset_index(drop=True)
    )


def minimize_profile_union(
    factorized_events: pd.DataFrame,
    qualified_unique: pd.DataFrame,
    conditions_by_id: Mapping[str, Mapping[str, str]],
) -> tuple[pd.DataFrame, np.ndarray]:
    covered = np.zeros(len(factorized_events), dtype=bool)
    kept: list[dict[str, Any]] = []
    ordered = qualified_unique.sort_values(
        ["factor_count", "trade_count", "avg_account_return", "profile_id"],
        ascending=[True, False, False, True],
    )
    for _, row in ordered.iterrows():
        profile_id = str(row["profile_id"])
        mask = profile_mask(
            factorized_events, conditions_by_id[profile_id]
        ).to_numpy(dtype=bool)
        added_count = int((mask & ~covered).sum())
        if added_count <= 0:
            continue
        covered |= mask
        payload = row.to_dict()
        payload["new_event_coverage_count"] = added_count
        kept.append(payload)
    return pd.DataFrame(kept), covered


def daily_union_picks(factorized_events: pd.DataFrame, union_mask: np.ndarray) -> pd.DataFrame:
    selected = factorized_events.loc[union_mask].copy()
    if selected.empty:
        return selected
    return (
        selection_sorted(selected)
        .drop_duplicates("trade_date", keep="first")
        .reset_index(drop=True)
    )


def picks_for_release(
    factorized_events: pd.DataFrame, release: Mapping[str, Any]
) -> pd.DataFrame:
    profiles = release.get("profiles", [])
    mask = np.zeros(len(factorized_events), dtype=bool)
    for profile in profiles:
        mask |= profile_mask(
            factorized_events, profile["conditions"]
        ).to_numpy(dtype=bool)
    return daily_union_picks(factorized_events, mask)


@contextmanager
def strict_window(start: str, end: str) -> Iterator[None]:
    old_start, old_end = strict.START, strict.END
    strict.START, strict.END = str(start), str(end)
    try:
        yield
    finally:
        strict.START, strict.END = old_start, old_end


def build_incumbent_and_other_legs(
    release: Mapping[str, Any], factorized_events: pd.DataFrame, start: str, end: str
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    with strict_window(start, end):
        source, source_audit = strict.source_audit()
        if not bool(source_audit.get("passed")):
            raise RuntimeError("严格as-of正式源审计未通过")
        if str(release["strategy_mode"]) == LEGACY_MODE:
            incumbent = strict.build_d(source, strict.daily_data())
        else:
            incumbent_picks = picks_for_release(factorized_events, release)
            incumbent = outcome_frame_from_picks(incumbent_picks)
        other_legs = build_current_other_legs()
    return incumbent, other_legs, source_audit


def replay_comparison(
    incumbent_outcomes: pd.DataFrame,
    candidate_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
    start: str,
    end: str,
) -> dict[str, Any]:
    incumbent_d_detail = replay_d_only(incumbent_outcomes, start, end)
    candidate_d_detail = replay_d_only(candidate_outcomes, start, end)
    incumbent_d = executed_metrics(incumbent_d_detail)
    candidate_d = executed_metrics(candidate_d_detail)
    with strict_window(start, end):
        incumbent_acde_detail, incumbent_acde = combo_replay_fast(
            incumbent_outcomes, other_legs
        )
        candidate_acde_detail, candidate_acde = combo_replay_fast(
            candidate_outcomes, other_legs
        )
    return {
        "incumbent_d_detail": incumbent_d_detail,
        "candidate_d_detail": candidate_d_detail,
        "incumbent_acde_detail": incumbent_acde_detail,
        "candidate_acde_detail": candidate_acde_detail,
        "incumbent_d": incumbent_d,
        "candidate_d": candidate_d,
        "incumbent_acde": incumbent_acde,
        "candidate_acde": candidate_acde,
    }


def assert_current_official_anchor(
    release: Mapping[str, Any], comparison: Mapping[str, Any], start: str, end: str
) -> None:
    if not (
        start == CURRENT_OFFICIAL_START
        and end == CURRENT_OFFICIAL_END
        and str(release["strategy_mode"]) == LEGACY_MODE
    ):
        return
    d = comparison["incumbent_d"]
    acde = comparison["incumbent_acde"]
    failures: list[str] = []
    if int(d["trade_count"]) != CURRENT_D_TRADES:
        failures.append(f"D笔数={d['trade_count']}")
    if abs(float(d["equity_multiple"]) - CURRENT_D_MULTIPLE) > TOLERANCE:
        failures.append(f"D复利={d['equity_multiple']}")
    if int(acde["trade_count"]) != CURRENT_ACDE_TRADES:
        failures.append(f"ACDE笔数={acde['trade_count']}")
    if abs(float(acde["equity_multiple"]) - CURRENT_ACDE_MULTIPLE) > TOLERANCE:
        failures.append(f"ACDE复利={acde['equity_multiple']}")
    if failures:
        raise RuntimeError("D半年因子优化正式锚点漂移：" + "；".join(failures))


def json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def release_profiles(
    effective: pd.DataFrame,
    conditions_by_id: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for priority, (_, row) in enumerate(effective.iterrows(), 1):
        profile_id = str(row["profile_id"])
        profiles.append(
            {
                "profile_id": profile_id,
                "priority": priority,
                "conditions": dict(conditions_by_id[profile_id]),
                "branch_metrics": {
                    key: json_value(row[key])
                    for key in (
                        "trade_count", "win_rate", "avg_account_return",
                        "median_account_return", "equity_multiple", "max_drawdown",
                        "max_profit", "max_loss", "profit_loss_ratio",
                        "max_consecutive_losses", "first_12m_trade_count",
                        "first_12m_multiple", "second_12m_trade_count",
                        "second_12m_multiple",
                    )
                },
            }
        )
    return profiles


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_release(
    release_path: Path,
    incumbent: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Path:
    history = release_path.parent / "strategy_d_factor_release_history"
    history.mkdir(parents=True, exist_ok=True)
    incumbent_id = str(incumbent.get("release_id", "D_UNKNOWN"))
    archive = history / f"{incumbent_id}.json"
    if release_path.exists() and not archive.exists():
        shutil.copy2(release_path, archive)
    write_json_atomic(release_path, candidate)
    return archive


def pseudo_code(profiles: Iterable[Mapping[str, Any]]) -> str:
    lines = [
        "# 机器生成的D回封板多条件并集；任一if成立即允许进入D候选排序",
        "allow_d = False",
    ]
    for profile in profiles:
        conditions = profile["conditions"]
        expression = " and ".join(
            f"factors[{name!r}] == {value!r}"
            for name, value in conditions.items()
        )
        lines.append(f"if {expression}:  # {profile['profile_id']}")
        lines.append("    allow_d = True")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="半年优化D回封板多因子条件并集")
    parser.add_argument("--as-of", default=CURRENT_OFFICIAL_END, help="更新节点YYYYMMDD，只允许0630/1231")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--apply", action="store_true",
        help="仅当D和ACDE双复利门禁通过时，原子替换正式D因子发布文件",
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
    end = str(args.as_of)
    dt.datetime.strptime(end, "%Y%m%d")
    allowed_nodes = {str(value) for value in config["allowed_update_nodes"]}
    if end[4:] not in allowed_nodes:
        raise ValueError(
            f"D只允许在半年节点0630/1231更新，收到as_of={end}"
        )
    start = natural_window_start(end, int(config["window_years"]))
    events_path = resolve_path(args.events)
    output_dir = (
        resolve_path(args.output_dir)
        if args.output_dir is not None
        else resolve_path(config["reports_root"]) / end
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    release_path = resolve_path(config["active_release_path"])
    incumbent_release = load_factor_release(release_path)

    LOGGER.info("加载D回封事件并冻结窗口：%s~%s", start, end)
    events, input_audit = load_events(events_path, start, end)
    factorized = add_factor_values(events)
    LOGGER.info(
        "开始枚举D因子值组合：因子%d个，最多%d因子AND",
        len(FACTOR_COLUMNS),
        int(config["max_factor_count"]),
    )
    search, conditions_by_id, _, search_audit = enumerate_profiles(
        factorized,
        max_factor_count=int(config["max_factor_count"]),
        min_trade_count=int(config["min_branch_trade_count"]),
        min_avg_return=float(config["min_branch_avg_account_return"]),
        min_win_rate=float(config["min_branch_win_rate"]),
        start=start,
        end=end,
    )
    qualified = search[search["threshold_qualified"]].copy()
    qualified_unique = deduplicate_qualified_profiles(search)
    effective, union_mask = minimize_profile_union(
        factorized, qualified_unique, conditions_by_id
    )
    union_picks = daily_union_picks(factorized, union_mask)
    candidate_outcomes = outcome_frame_from_picks(union_picks)

    incumbent_outcomes, other_legs, strict_source_audit = build_incumbent_and_other_legs(
        incumbent_release, factorized, start, end
    )
    comparison = replay_comparison(
        incumbent_outcomes, candidate_outcomes, other_legs, start, end
    )
    assert_current_official_anchor(incumbent_release, comparison, start, end)

    d_improved = bool(
        float(comparison["candidate_d"]["equity_multiple"])
        > float(comparison["incumbent_d"]["equity_multiple"]) + TOLERANCE
    )
    acde_improved = bool(
        float(comparison["candidate_acde"]["equity_multiple"])
        > float(comparison["incumbent_acde"]["equity_multiple"]) + TOLERANCE
    )
    release_eligible = bool(len(effective) > 0 and d_improved and acde_improved)
    profiles = release_profiles(effective, conditions_by_id)
    release_id = f"D_FACTOR_UNION_{end}_{hashlib.sha1(json.dumps(profiles, sort_keys=True).encode()).hexdigest()[:8]}"
    candidate_release = {
        "schema_version": 1,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "release_id": release_id,
        "strategy_mode": FACTOR_UNION_MODE,
        "effective_from": next_calendar_day(end),
        "research_window": {"start": start, "end": end},
        "profiles": profiles,
        "selection_policy": str(config["selection_policy"]),
        "branch_gate": {
            "trade_count_at_least": int(config["min_branch_trade_count"]),
            "avg_account_return_must_exceed": float(config["min_branch_avg_account_return"]),
            "win_rate_must_exceed": float(config["min_branch_win_rate"]),
        },
        "certified_metrics": {
            "d": comparison["candidate_d"],
            "acde": comparison["candidate_acde"],
        },
        "replaced_release_id": str(incumbent_release["release_id"]),
        "release_gate_passed": release_eligible,
    }

    applied = False
    archive_path = ""
    if args.apply:
        if not release_eligible:
            LOGGER.warning("候选未同时提高D与ACDE复利，正式D发布文件保持不变")
        else:
            archive = apply_release(release_path, incumbent_release, candidate_release)
            archive_path = str(archive.relative_to(ROOT))
            applied = True
            LOGGER.warning("D因子并集通过双门禁并已替换正式发布：%s", release_id)

    summary = {
        "schema_version": 1,
        "protocol": STRICT_DISCOVERY,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "window": {"start": start, "end": end},
        "input_audit": input_audit,
        "strict_source_audit_passed": bool(strict_source_audit.get("passed")),
        "optimizer_config_path": str(config_path.relative_to(ROOT)),
        "optimizer_config_sha256": sha256(config_path),
        "active_release_before_run": incumbent_release,
        "search_space": {
            **search_audit,
            "max_factor_count": int(config["max_factor_count"]),
            "threshold_qualified_profile_count": int(len(qualified)),
            "qualified_unique_selection_count": int(len(qualified_unique)),
            "effective_or_profile_count": int(len(effective)),
            "factor_columns": list(FACTOR_COLUMNS),
        },
        "branch_gate": {
            "trade_count_at_least": int(config["min_branch_trade_count"]),
            "avg_account_return_must_exceed": float(config["min_branch_avg_account_return"]),
            "win_rate_must_exceed": float(config["min_branch_win_rate"]),
            "comparison_is_strict": True,
        },
        "union_candidate": {
            "candidate_day_count": int(len(union_picks)),
            "profiles": profiles,
            "d_metrics": comparison["candidate_d"],
            "acde_metrics": comparison["candidate_acde"],
        },
        "incumbent": {
            "release_id": str(incumbent_release["release_id"]),
            "strategy_mode": str(incumbent_release["strategy_mode"]),
            "d_metrics": comparison["incumbent_d"],
            "acde_metrics": comparison["incumbent_acde"],
        },
        "replace_gate": {
            "d_compound_improved": d_improved,
            "acde_compound_improved": acde_improved,
            "release_eligible": release_eligible,
        },
        "candidate_release": candidate_release,
        "apply_requested": bool(args.apply),
        "formal_release_applied": applied,
        "archived_release_path": archive_path,
        "formal_strategy_modified": applied,
        "decision": (
            "REPLACE_FORMAL_D_WITH_FACTOR_UNION"
            if applied
            else (
                "FACTOR_UNION_PASSED_WAITING_EXPLICIT_APPLY"
                if release_eligible
                else "KEEP_INCUMBENT_D_DUAL_COMPOUND_GATE_NOT_PASSED"
            )
        ),
        "limitations": [
            "条件分支来自同一24个月的多重组合搜索；2%与55%只是入选线，不代表未来收益。",
            "前后12个月仅披露稳定性，不作为未来样本外。",
            "一分钟K无法恢复同一分钟内逐笔队列；始终封板且无价格穿透仍保守记未成交。",
            "更早6个月只作旁证，未来6个月才是真正前向样本外。",
        ],
    }

    search.sort_values(
        ["threshold_qualified", "avg_account_return", "win_rate", "equity_multiple"],
        ascending=[False, False, False, False],
    ).to_csv(output_dir / "all_factor_profiles.csv", index=False, encoding="utf-8-sig")
    qualified.sort_values(
        ["avg_account_return", "win_rate", "equity_multiple"], ascending=False
    ).to_csv(output_dir / "qualified_factor_profiles.csv", index=False, encoding="utf-8-sig")
    qualified_unique.to_csv(
        output_dir / "qualified_unique_profiles.csv", index=False, encoding="utf-8-sig"
    )
    effective.to_csv(
        output_dir / "effective_or_profiles.csv", index=False, encoding="utf-8-sig"
    )
    union_picks.to_csv(
        output_dir / "candidate_union_daily_picks.csv", index=False, encoding="utf-8-sig"
    )
    comparison["incumbent_d_detail"].to_csv(
        output_dir / "incumbent_d_detail.csv", index=False, encoding="utf-8-sig"
    )
    comparison["candidate_d_detail"].to_csv(
        output_dir / "candidate_d_detail.csv", index=False, encoding="utf-8-sig"
    )
    comparison["incumbent_acde_detail"].to_csv(
        output_dir / "incumbent_acde_detail.csv", index=False, encoding="utf-8-sig"
    )
    comparison["candidate_acde_detail"].to_csv(
        output_dir / "candidate_acde_detail.csv", index=False, encoding="utf-8-sig"
    )
    (output_dir / "d_if_conditions.py.txt").write_text(
        pseudo_code(profiles), encoding="utf-8"
    )
    write_json_atomic(output_dir / "candidate_release.json", candidate_release)
    write_json_atomic(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
