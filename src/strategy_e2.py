from __future__ import annotations

"""策略 E2 的唯一规则源。

本模块只使用信号日收盘时已经存在的数据生成候选，不读取次日开盘、退出价格、
实际成交结果或收益字段。实盘信号脚本与历史对齐脚本都必须调用这里，避免再次
出现“研究脚本一套规则、实盘脚本另一套规则”的口径漂移。
"""

import json
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd


E2_VERSION = "E2_R1_NO_LOOKAHEAD_SINGLE_ACCOUNT_ENTRY_GATE_V4"
DEFAULT_SCENARIO_PATH = Path("config/strategy_e2_r1_scenarios.json")

# 这些字段都属于买入日以后才能知道的结果，禁止出现在E2选股条件或排序规则中。
FORBIDDEN_SELECTION_COLUMNS = {
    "net_return",
    "dynamic_net_return",
    "dynamic_account_return",
    "buy_executed",
    "sell_executed",
    "buy_price",
    "exit_price",
    "exit_trade_date",
    "replay_rule",
    "status",
    "d1_open",
    "d2_close",
    "d3_close",
}

# 实盘构造候选时必须完整存在的基础风控字段。任一缺失都拒绝生成E2信号。
LIVE_BASE_REQUIRED_FIELDS = {
    "trade_date",
    "ts_code",
    "name",
    "circ_mv",
    "limit_data_quality",
    "strategy_compatible",
    "allow_buy_reliable",
    "is_fill_score_reliable",
    "is_fd_amount_abnormal",
    "segment_retreat_state_bucket",
    "amount_ratio_bucket",
}


def parse_scenario_name(scenario_name: str, scenario_rank: int) -> dict[str, Any]:
    """把锁定的scenario名称解析为条件、排序与退出规则。

    配置只保存原始scenario字符串，减少人工复制40组结构化字典时发生口径漂移。
    解析过程是确定性的，回测和实盘得到完全相同的规则对象。
    """

    prefix = "large_universe_sort_"
    if not scenario_name.startswith(prefix) or "|sort=" not in scenario_name or "|exit=" not in scenario_name:
        raise ValueError(f"E2 scenario格式非法：{scenario_name}")

    condition_text, remainder = scenario_name[len(prefix):].split("|sort=", 1)
    sort_rule, exit_token = remainder.split("|exit=", 1)
    exit_rule = exit_token
    for suffix in ("_desc", "_asc"):
        if exit_rule.endswith(suffix):
            exit_rule = exit_rule[: -len(suffix)]
            break

    conditions: dict[str, str] = {}
    for item in filter(None, condition_text.split(";")):
        if "=" not in item:
            raise ValueError(f"E2 scenario条件格式非法：{item}")
        column, value = item.split("=", 1)
        conditions[column] = value

    return {
        "scenario_rank": int(scenario_rank),
        "scenario": scenario_name,
        "conditions": conditions,
        "sort_rule": sort_rule,
        "exit_rule": exit_rule,
    }


def load_e2_spec(project_root: Path, scenario_path: Path | None = None) -> dict[str, Any]:
    """加载并校验E2锁定规则，发现前视字段时立即拒绝运行。"""

    path = scenario_path or project_root / DEFAULT_SCENARIO_PATH
    if not path.exists():
        raise FileNotFoundError(f"缺少E2 R1规则文件：{path}")
    spec = json.loads(path.read_text(encoding="utf-8"))

    if "scenarios" not in spec:
        names = list(spec.get("scenario_names", []))
        spec["scenarios"] = [parse_scenario_name(name, rank) for rank, name in enumerate(names, start=1)]

    if not spec["scenarios"]:
        raise ValueError("E2 R1规则为空，拒绝生成实盘信号。")

    sort_rules = spec.get("sort_rules", {})
    exit_rules = spec.get("exit_rules", {})
    used_columns: set[str] = set()
    seen_ranks: set[int] = set()
    for scenario in spec["scenarios"]:
        rank = int(scenario["scenario_rank"])
        if rank in seen_ranks:
            raise ValueError(f"E2 scenario_rank重复：{rank}")
        seen_ranks.add(rank)
        used_columns.update(str(column) for column in scenario.get("conditions", {}))

        sort_rule = str(scenario.get("sort_rule", ""))
        if sort_rule not in sort_rules:
            raise ValueError(f"E2缺少排序规则定义：{sort_rule}")
        columns = list(sort_rules[sort_rule].get("columns", []))
        ascending = list(sort_rules[sort_rule].get("ascending", []))
        if not columns or len(columns) != len(ascending):
            raise ValueError(f"E2排序规则列数不一致：{sort_rule}")
        used_columns.update(str(column) for column in columns)

        exit_rule = str(scenario.get("exit_rule", ""))
        if exit_rule not in exit_rules:
            raise ValueError(f"E2缺少退出规则定义：{exit_rule}")
        if int(exit_rules[exit_rule].get("hold_offset", 0)) not in {2, 3}:
            raise ValueError(f"E2只允许T+2或T+3退出：{exit_rule}")

    # 入场门禁必须放在“每日第一名已经确定”之后执行。若允许第一名被过滤后
    # 自动改买第二名，历史验证中的“删掉这一笔”就会被实盘偷换成另一只股票，
    # 形成新的口径漂移，因此这两个开关只能保持当前安全值。
    entry_gate = spec.get("entry_gate", {})
    exclude_values = entry_gate.get("exclude_values", {})
    if not isinstance(exclude_values, dict):
        raise ValueError("E2 entry_gate.exclude_values必须是字段到排除值列表的映射")
    if entry_gate and not bool(entry_gate.get("apply_after_daily_first_pick", False)):
        raise ValueError("E2入场门禁必须在每日第一名确定后执行")
    if bool(entry_gate.get("fallback_to_second_candidate", False)):
        raise ValueError("E2入场门禁禁止回补当日第二名")
    for column, values in exclude_values.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"E2入场门禁排除值非法：{column}")
        used_columns.add(str(column))

    forbidden = sorted(used_columns & FORBIDDEN_SELECTION_COLUMNS)
    if forbidden:
        raise ValueError(f"E2规则包含前视结果字段，拒绝运行：{forbidden}")
    return spec


def required_signal_fields(spec: dict[str, Any]) -> set[str]:
    """返回当前规则真正依赖的信号日字段集合。"""

    fields = set(LIVE_BASE_REQUIRED_FIELDS)
    fields.update(str(column) for column in spec.get("universe_prefilters", {}))
    for scenario in spec["scenarios"]:
        fields.update(str(column) for column in scenario.get("conditions", {}))
        sort_rule = spec["sort_rules"][scenario["sort_rule"]]
        fields.update(str(column) for column in sort_rule.get("columns", []))
    fields.update(
        str(column)
        for column in spec.get("entry_gate", {}).get("exclude_values", {})
    )
    # universe_prefilters里的键是配置动作名，不是数据列，单独移除。
    fields.discard("exclude_st_or_delisting")
    fields.discard("exclude_amount_ratio_bucket")
    return fields


def audit_signal_data_readiness(day_rows: pd.DataFrame, spec: dict[str, Any]) -> list[str]:
    """核验信号日数据是否足以完整重放全部R1规则。"""

    broken: list[str] = []
    for column in sorted(required_signal_fields(spec)):
        if column not in day_rows.columns:
            broken.append(f"{column}(字段缺失)")
            continue
        values = day_rows[column]
        text = values.astype(str).str.strip()
        unavailable = values.isna() | text.isin({"", "nan", "None", "unknown", "<NA>"})
        if bool(unavailable.all()):
            broken.append(f"{column}(整列不可用)")
    return broken


def _bool_series(frame: pd.DataFrame, column: str, default: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="bool")
    return frame[column].fillna(default).astype(str).str.lower().isin({"true", "1", "yes"})


def apply_live_base_filters(pool: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    """应用与实盘一致的成交可靠性、数据质量和ST过滤。"""

    result = pool.copy()
    result = result[result["limit_data_quality"].fillna("").astype(str).eq("full")]
    result = result[_bool_series(result, "strategy_compatible", False)]
    result = result[_bool_series(result, "allow_buy_reliable", False)]
    result = result[_bool_series(result, "is_fill_score_reliable", False)]
    result = result[~_bool_series(result, "is_fd_amount_abnormal", True)]

    prefilters = spec.get("universe_prefilters", {})
    if bool(prefilters.get("exclude_st_or_delisting", True)):
        is_st = _bool_series(result, "is_st", False)
        names = result["name"].fillna("").astype(str).str.upper()
        result = result[~(is_st | names.str.contains("ST", regex=False) | names.str.contains("退", regex=False))]

    excluded_amount_ratios = {str(value) for value in prefilters.get("exclude_amount_ratio_bucket", [])}
    if excluded_amount_ratios:
        result = result[~result["amount_ratio_bucket"].astype(str).isin(excluded_amount_ratios)]
    return result.copy()


def build_r1_universe_from_pool(
    pool: pd.DataFrame,
    spec: dict[str, Any],
    signal_date: str | None = None,
    *,
    audit_readiness: bool = True,
) -> pd.DataFrame:
    """按40条R1规则构造候选并集；不读取任何未来成交或收益字段。"""

    day_rows = pool.copy()
    if signal_date is not None:
        day_rows = day_rows[day_rows["trade_date"].astype(str) == str(signal_date)].copy()
    if day_rows.empty:
        return pd.DataFrame()

    if audit_readiness:
        broken = audit_signal_data_readiness(day_rows, spec)
        if broken:
            raise RuntimeError("E2 R1信号日关键字段不可用：" + "、".join(broken))
    day_rows = apply_live_base_filters(day_rows, spec)
    if day_rows.empty:
        return pd.DataFrame()

    numeric_columns = {"circ_mv"}
    for scenario in spec["scenarios"]:
        numeric_columns.update(spec["sort_rules"][scenario["sort_rule"]]["columns"])
    for column in numeric_columns:
        if column in day_rows.columns:
            day_rows[column] = pd.to_numeric(day_rows[column], errors="coerce")

    group_columns = ["trade_date"] if signal_date is None else []
    picks: list[pd.DataFrame] = []
    for scenario in spec["scenarios"]:
        subset = day_rows
        for column, value in scenario["conditions"].items():
            subset = subset[subset[column].astype(str) == str(value)]
            if subset.empty:
                break
        if subset.empty:
            continue

        sort_spec = spec["sort_rules"][scenario["sort_rule"]]
        sort_columns = group_columns + list(sort_spec["columns"]) + ["ts_code"]
        ascending = ([True] if group_columns else []) + list(sort_spec["ascending"]) + [True]
        ordered = subset.sort_values(sort_columns, ascending=ascending, na_position="last")
        top = ordered.groupby("trade_date", as_index=False).head(1) if group_columns else ordered.head(1)
        top = top.copy()
        top["scenario"] = scenario["scenario"]
        top["scenario_rank"] = int(scenario["scenario_rank"])
        top["exit_rule"] = scenario["exit_rule"]
        picks.append(top)

    if not picks:
        return pd.DataFrame()
    universe = pd.concat(picks, ignore_index=True)
    dedup_keys = ["trade_date", "ts_code"] if signal_date is None else ["ts_code"]
    return (
        universe.sort_values(dedup_keys[:1] + ["scenario_rank", "ts_code"])
        .drop_duplicates(dedup_keys, keep="first")
        .reset_index(drop=True)
    )


def select_e2_candidates(universe: pd.DataFrame) -> pd.DataFrame:
    """在R1候选并集中取板块neutral，再按流通市值升序排列。"""

    if universe.empty:
        return pd.DataFrame()
    result = universe[universe["segment_retreat_state_bucket"].astype(str).eq("neutral")].copy()
    result["circ_mv"] = pd.to_numeric(result["circ_mv"], errors="coerce")
    result = result[result["circ_mv"].notna()].copy()
    sort_columns = [column for column in ["trade_date", "circ_mv", "scenario_rank", "ts_code"] if column in result]
    return result.sort_values(sort_columns).reset_index(drop=True)


def apply_e2_entry_gate(daily_picks: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    """对已经确定的每日第一名执行E2入场门禁。

    门禁只允许读取信号日已经存在的字段。调用方必须先选出每日第一名，再调用
    本函数；被排除后当日直接空仓，不允许回补第二名。这个顺序同时用于历史
    验证和实盘信号生成，确保“过滤提高复利”不是换票造成的理论结果。
    """

    if daily_picks.empty:
        return daily_picks.copy()
    result = daily_picks.copy()
    exclude_values = spec.get("entry_gate", {}).get("exclude_values", {})
    keep = pd.Series(True, index=result.index, dtype="bool")
    for column, values in exclude_values.items():
        if column not in result.columns:
            raise RuntimeError(f"E2入场门禁字段缺失：{column}")
        excluded = {str(value) for value in values}
        keep &= ~result[column].fillna("").astype(str).isin(excluded)
    return result.loc[keep].copy().reset_index(drop=True)


def select_e2_daily_picks(universe: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    """先按原R1规则取每日第一名，再执行无回补入场门禁。"""

    ranked = select_e2_candidates(universe)
    if ranked.empty:
        return ranked
    if "trade_date" in ranked.columns:
        daily_first = ranked.groupby("trade_date", as_index=False).head(1).copy()
    else:
        daily_first = ranked.head(1).copy()
    return apply_e2_entry_gate(daily_first, spec)


def resolve_exit_offset(spec: dict[str, Any], exit_rule: str) -> int:
    """把命中的退出规则转换为相对信号日的T+2/T+3偏移。"""

    rule = spec.get("exit_rules", {}).get(str(exit_rule))
    if not rule:
        raise ValueError(f"E2未知退出规则：{exit_rule}")
    offset = int(rule.get("hold_offset", 0))
    if offset not in {2, 3}:
        raise ValueError(f"E2非法退出偏移：{offset}")
    return offset


def _load_trade_days(project_root: Path) -> list[str]:
    path = project_root / "data" / "raw" / "trade_calendar.csv"
    calendar = pd.read_csv(path, dtype={"cal_date": str})
    if "is_open" in calendar.columns:
        calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    return sorted(calendar["cal_date"].astype(str).tolist())


def _load_volume(project_root: Path, trade_date: str) -> pd.Series | None:
    path = project_root / "data" / "raw" / "daily" / f"{trade_date}.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype={"ts_code": str}, usecols=["ts_code", "vol"])
    values = pd.to_numeric(frame["vol"], errors="coerce")
    return pd.Series(values.to_numpy(), index=frame["ts_code"].astype(str)).groupby(level=0).first()


def _compute_local_volume_ratio(project_root: Path, trade_date: str, trade_days: list[str]) -> pd.Series | None:
    """按当日成交量/前5个交易日日均量计算收盘量比，不写回原始数据。"""

    if trade_date not in trade_days:
        return None
    index = trade_days.index(trade_date)
    if index < 5:
        return None
    current = _load_volume(project_root, trade_date)
    history = [_load_volume(project_root, day) for day in trade_days[index - 5:index]]
    if current is None or any(item is None for item in history):
        return None
    average = pd.concat([item for item in history if item is not None], axis=1).mean(axis=1)
    ratio = current / average.where(average > 0)
    return ratio.replace([float("inf"), float("-inf")], pd.NA).dropna()


def patch_volume_ratio_for_dates(
    pool: pd.DataFrame,
    project_root: Path,
    target_dates: set[str],
) -> pd.DataFrame:
    """仅在内存中补齐目标日期量比：优先daily_basic，空值时用本地5日公式。"""

    result = pool.copy()
    if "volume_ratio" not in result.columns:
        result["volume_ratio"] = pd.NA
    values = pd.to_numeric(result["volume_ratio"], errors="coerce")
    trade_days: list[str] | None = None

    for trade_date in sorted(target_dates):
        mask = result["trade_date"].astype(str).eq(trade_date) & values.isna()
        if not bool(mask.any()):
            continue
        ratio: pd.Series | None = None
        basic_path = project_root / "data" / "raw" / "daily_basic" / f"{trade_date}.csv"
        if basic_path.exists():
            basic = pd.read_csv(basic_path, dtype={"ts_code": str}, usecols=["ts_code", "volume_ratio"])
            basic_values = pd.to_numeric(basic["volume_ratio"], errors="coerce")
            if bool(basic_values.notna().any()):
                ratio = pd.Series(basic_values.to_numpy(), index=basic["ts_code"].astype(str))
        if ratio is None:
            trade_days = trade_days or _load_trade_days(project_root)
            ratio = _compute_local_volume_ratio(project_root, trade_date, trade_days)
        if ratio is None:
            continue
        mapped = result.loc[mask, "ts_code"].astype(str).map(ratio)
        values.loc[mask] = mapped.to_numpy()

    result["volume_ratio"] = values
    return result


def load_bucketed_signal_pool(project_root: Path, signal_date: str, lookback_days: int = 80) -> pd.DataFrame:
    """加载信号日前历史窗口并生成R1所需全部bucket。

    只保留最近80个有涨停数据的交易日，足以计算板块状态的两次shift，同时避免
    每天实盘为一个信号重读四年逐日行情。历史验证可直接调用纯函数并传完整池。
    """

    historical = project_root / "data" / "processed" / "limit_up_fill_scored.csv"
    live = project_root / "data" / "processed" / "live_limit_up_fill_scored.csv"
    frames: list[pd.DataFrame] = []
    for path in (historical, live):
        if path.exists():
            frame = pd.read_csv(path, low_memory=False)
            frame["trade_date"] = frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
            frames.append(frame)
    if not frames:
        raise FileNotFoundError("历史涨停打分池和实盘涨停打分池均不存在。")

    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged = merged[merged["trade_date"].astype(str) <= str(signal_date)].copy()
    dates = sorted(merged["trade_date"].astype(str).unique())
    keep_dates = set(dates[-max(int(lookback_days), 3):])
    merged = merged[merged["trade_date"].astype(str).isin(keep_dates)].copy()
    merged = merged.sort_values("trade_date").drop_duplicates(["trade_date", "ts_code"], keep="last")
    merged = patch_volume_ratio_for_dates(merged, project_root, {str(signal_date)})

    processed = project_root / "data" / "processed"
    emotion_frames: list[pd.DataFrame] = []
    for path in (processed / "market_emotion_features.csv", processed / "live_market_emotion_features.csv"):
        if path.exists():
            emotion = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False)
            emotion = emotion[emotion["trade_date"].astype(str).isin(keep_dates)].copy()
            emotion_frames.append(emotion)

    # optimizer根据输入文件名是否以live_开头决定走逐日分片路径，所以临时文件
    # 固定以live_开头；TemporaryDirectory保证异常退出后也能自动清理。
    with tempfile.TemporaryDirectory(prefix="e2_r1_") as temp_dir:
        temp_root = Path(temp_dir)
        input_path = temp_root / "live_e2_r1_pool.csv"
        emotion_path = temp_root / "live_e2_r1_emotion.csv"
        merged.to_csv(input_path, index=False, encoding="utf-8-sig")

        from src.strategy_optimizer import StrategyConditionOptimizer

        optimizer = StrategyConditionOptimizer(config_path="config/config.json")
        optimizer.input_trades_path = input_path
        if emotion_frames:
            emotion = pd.concat(emotion_frames, ignore_index=True, sort=False)
            keys = ["trade_date", "market_segment"]
            emotion = emotion.sort_values("trade_date").drop_duplicates(keys, keep="last")
            emotion.to_csv(emotion_path, index=False, encoding="utf-8-sig")
            optimizer.optional_market_emotion_features_path = emotion_path
        return optimizer.load_trades(require_complete_exit=False)


def build_live_e2_candidates(project_root: Path, signal_date: str) -> pd.DataFrame:
    """实盘入口：数据准备、R1并集、neutral过滤全部走同一条规则链。"""

    spec = load_e2_spec(project_root)
    pool = load_bucketed_signal_pool(project_root, signal_date)
    universe = build_r1_universe_from_pool(pool, spec, signal_date=signal_date, audit_readiness=True)
    ranked = select_e2_candidates(universe)
    if ranked.empty:
        return ranked

    # 实盘候选文件仍可保留同日完整排序，便于审计；但只有原第一名通过门禁时
    # 才返回。第一名被排除时返回空表，绝不顺延买第二名。
    first_pick = ranked.head(1).copy()
    if apply_e2_entry_gate(first_pick, spec).empty:
        return ranked.iloc[0:0].copy()
    return ranked
