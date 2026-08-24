"""
从当日 limit_up_fill_scored.csv 生成安全的次日模拟观察计划。

用途：
1. A/C 历史回放候选只能生成到有完整未来价格的日期，B策略已删除。
2. 收盘后实盘准备需要看到当日最新涨停池的模拟观察计划。
3. 本脚本只生成 PLAN_ONLY / WATCH_ONLY 文件，不接 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config, mkdir_p
from scripts.run_paper_ab_filtered_daily_ops import (
    configured_c_condition_profiles,
    reject_strategy_risk_mask,
)


OUTPUT_PREFIX = "reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_c_hold3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成当日涨停池模拟观察计划。")
    parser.add_argument("--signal-date", default=None, help="信号日期 YYYYMMDD。不传则使用 limit_up_fill_scored 最新日期。")
    parser.add_argument("--top-n", type=int, default=10, help="输出候选数量。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json")
    parser.add_argument("--runtime-config", default="config/config.json")
    parser.add_argument("--output-prefix", default=OUTPUT_PREFIX)
    return parser.parse_args()


def normalize_date(value: object) -> str:
    text = str(value).strip().replace("-", "")
    if len(text) >= 8:
        return text[:8]
    return text


def to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def to_float(value: object, default: float = 0.0) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(numeric) else float(numeric)


def bucket_numeric(data: pd.DataFrame, column: str, bins: list[float], labels: list[str]) -> pd.Series:
    if column not in data.columns:
        return pd.Series("unknown", index=data.index, dtype="object")
    values = pd.to_numeric(data[column], errors="coerce")
    return pd.cut(values, bins=bins, labels=labels).astype(str).replace("nan", "unknown")


def first_time_minutes(data: pd.DataFrame) -> pd.Series:
    raw = pd.to_numeric(data.get("first_time", pd.Series(index=data.index)), errors="coerce").fillna(0).astype(int)
    return (raw // 10000) * 60 + ((raw % 10000) // 100)


def add_runtime_buckets(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    result["first_time_minutes"] = first_time_minutes(result)
    result["first_time_detail_bucket"] = pd.cut(
        result["first_time_minutes"],
        bins=[-float("inf"), 570, 600, 660, 810, 870, float("inf")],
        labels=["open_auction", "before_1000", "1000_1100", "1100_1330", "1330_1430", "after_1430"],
    ).astype(str)
    result["open_times_bucket"] = bucket_numeric(
        result, "open_times", [-float("inf"), 0, 1, 3, float("inf")], ["0", "1", "2_3", "gte_4"]
    )
    result["turnover_rate_bucket"] = bucket_numeric(
        result, "turnover_rate", [-float("inf"), 3, 6, 10, 15, 25, float("inf")],
        ["lt_3", "3_6", "6_10", "10_15", "15_25", "gte_25"],
    )
    result["volume_ratio_bucket"] = bucket_numeric(
        result, "volume_ratio", [-float("inf"), 1, 2, 4, 8, float("inf")],
        ["lt_1", "1_2", "2_4", "4_8", "gte_8"],
    )
    result["fd_ratio_bucket"] = bucket_numeric(
        result, "fd_amount_to_circ_mv",
        [-float("inf"), 0.001, 0.003, 0.005, 0.01, 0.02, 0.05, float("inf")],
        ["lt_0_1pct", "0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct", "1pct_2pct", "2pct_5pct", "gte_5pct"],
    )
    result["limit_up_count_bucket"] = bucket_numeric(
        result, "limit_up_count", [-float("inf"), 30, 50, 80, 120, 180, float("inf")],
        ["lt_30", "30_50", "50_80", "80_120", "120_180", "gte_180"],
    )
    result["segment_limit_up_count_bucket"] = bucket_numeric(
        result, "segment_limit_up_count", [-float("inf"), 5, 10, 20, 40, 80, float("inf")],
        ["lt_5", "5_10", "10_20", "20_40", "40_80", "gte_80"],
    )
    result["market_chain_count_bucket"] = bucket_numeric(
        result, "limit_up_max_height", [-float("inf"), 3, 8, 15, 30, float("inf")],
        ["lt_3", "3_8", "8_15", "15_30", "gte_30"],
    )
    result["market_limit_down_count_bucket"] = bucket_numeric(
        result, "limit_down_count", [-float("inf"), 5, 15, 30, 60, float("inf")],
        ["lt_5", "5_15", "15_30", "30_60", "gte_60"],
    )
    result["amount_ratio_bucket"] = result.get("amount_ratio_bucket", pd.Series("unknown", index=result.index))
    result["prev_pct_chg_bucket"] = result.get("prev_pct_chg_bucket", pd.Series("unknown", index=result.index))
    result["market_emotion_state_bucket"] = result.get(
        "market_sentiment_level", pd.Series("unknown", index=result.index)
    ).fillna("unknown").astype(str)
    result["segment_emotion_state_bucket"] = result.get(
        "segment_market_sentiment_level", pd.Series("unknown", index=result.index)
    ).fillna("unknown").astype(str)
    result["retreat_state_bucket"] = result.get("retreat_state_bucket", pd.Series("unknown", index=result.index))
    # 与历史StrategyConditionOptimizer一致：板高降序、首次封板早者优先、
    # 炸板次数和成交额降序，得到T日收盘即可确定的全市场龙头排名。
    ranked_index = result.sort_values(
        ["trade_date", "limit_times", "first_time_minutes", "open_times", "amount"],
        ascending=[True, False, True, False, False],
    ).index
    leader_rank = pd.Series(index=result.index, dtype="float64")
    leader_rank.loc[ranked_index] = (
        result.loc[ranked_index].groupby("trade_date").cumcount() + 1
    ).to_numpy(dtype=float)
    result["market_leader_rank"] = leader_rank
    result["market_leader_rank_bucket"] = pd.cut(
        leader_rank,
        bins=[-float("inf"), 1, 3, 10, 30, float("inf")],
        labels=["rank_1", "rank_2_3", "rank_4_10", "rank_11_30", "rank_gt_30"],
    ).astype(str)
    return result


def assert_safe_runtime(runtime_config: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    if bool(strategy_config.get("paper_ab_filtered_strategy", {}).get("live_order_enabled", False)):
        raise RuntimeError("拒绝生成当日计划：策略实盘开关已开启。")
    # 本脚本只写本地 PLAN_ONLY/WATCH_ONLY CSV，不连接 QMT，不调用下单接口。
    # runtime_config 允许处于 live 连接状态，以免阻塞收盘后的数据准备流程。


def apply_universe_filters(data: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    result = data.copy()
    universe = config.get("universe", {})
    if bool(universe.get("exclude_st", True)):
        names = result.get("name", pd.Series("", index=result.index)).fillna("").astype(str).str.upper()
        is_st = result.get("is_st", pd.Series(False, index=result.index)).map(to_bool)
        result = result[~(is_st | names.str.contains("ST", na=False) | names.str.contains("退", na=False))].copy()
    if bool(universe.get("exclude_bj", False)) and "market_segment" in result.columns:
        result = result[result["market_segment"].astype(str) != "bj"].copy()
    if bool(universe.get("exclude_sh_main", False)) and "market_segment" in result.columns:
        result = result[result["market_segment"].astype(str) != "sh_main"].copy()
    if bool(universe.get("exclude_chi_next", False)) and "market_segment" in result.columns:
        result = result[result["market_segment"].astype(str) != "chi_next"].copy()
    excluded = {str(value) for value in universe.get("exclude_market_segments", [])}
    if excluded and "market_segment" in result.columns:
        result = result[~result["market_segment"].astype(str).isin(excluded)].copy()
    return result


def apply_conditions(data: pd.DataFrame, conditions: list[dict[str, Any]], strict_missing: bool = False) -> pd.DataFrame:
    result = data.copy()
    for condition in conditions:
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in result.columns:
            if strict_missing:
                return result.iloc[0:0].copy()
            continue
        result = result[result[column].fillna("missing").astype(str) == expected].copy()
    return result


def apply_condition_profiles(
    data: pd.DataFrame,
    profiles: list[dict[str, Any]],
) -> pd.DataFrame:
    if not profiles:
        return data.iloc[0:0].copy()
    union = pd.Series(False, index=data.index)
    hits = pd.Series("", index=data.index, dtype="object")
    for profile in profiles:
        profile_id = str(profile["profile_id"])
        matched = apply_conditions(
            data, profile.get("conditions", []), strict_missing=True
        ).index
        mask = data.index.isin(matched)
        union |= mask
        hits.loc[mask] = hits.loc[mask].map(
            lambda value: f"{value};{profile_id}".strip(";")
        )
    selected = data[union].copy()
    selected["matched_condition_profile_ids"] = hits.loc[selected.index]
    return selected


def apply_exclusions(data: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    result = data.copy()
    filters = config.get("candidate_filters", {})
    for condition in filters.get("exclude_conditions", []):
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column in result.columns:
            result = result[result[column].fillna("missing").astype(str) != expected].copy()
    for rule in filters.get("exclude_rules", []):
        mask = pd.Series(True, index=result.index)
        matched_any = False
        for condition in rule.get("conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            if column not in result.columns:
                matched_any = False
                break
            matched_any = True
            mask &= result[column].fillna("missing").astype(str) == expected
        if matched_any:
            result = result[~mask].copy()
    return result


def calculate_profit_source_score(data: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    score = pd.Series(0.0, index=data.index)
    for rule in config.get("ranking", {}).get("score_rules", []):
        column = str(rule.get("column", ""))
        if column not in data.columns:
            continue
        values = {str(value) for value in rule.get("values", [])}
        weight = float(rule.get("weight", 0.0))
        if values and weight:
            score.loc[data[column].fillna("missing").astype(str).isin(values)] += weight
    return score


def build_risk_flags(row: pd.Series, config: dict[str, Any]) -> str:
    thresholds = config.get("paper_candidate", {}).get("risk_thresholds", {})
    flags: list[str] = []
    if to_float(row.get("fill_probability", 0.0)) < float(thresholds.get("min_fill_probability_warn", 0.6)):
        flags.append("成交概率低于阈值")
    if to_float(row.get("fd_amount_to_circ_mv", 0.0)) > float(thresholds.get("max_fd_ratio_warn", 0.01)):
        flags.append("封单/流通市值偏高")
    if not to_bool(row.get("allow_buy_reliable", False)):
        flags.append("成交模型不允许买入")
    if not to_bool(row.get("is_fill_score_reliable", False)):
        flags.append("成交评分不可靠")
    if to_bool(row.get("is_st", False)) or "ST" in str(row.get("name", "")).upper():
        flags.append("ST风险")
    return ";".join(flags) if flags else "无"


def select_candidates(data: pd.DataFrame, config: dict[str, Any], top_n: int) -> tuple[str, pd.DataFrame]:
    base = apply_universe_filters(data, config)
    base = base[base.get("allow_buy_reliable", pd.Series(False, index=base.index)).map(to_bool)].copy()
    base = base[base.get("is_fill_score_reliable", pd.Series(False, index=base.index)).map(to_bool)].copy()
    base = apply_exclusions(base, config)

    a_filters = config.get("candidate_filters", {})
    a_profiles = a_filters.get("condition_profiles", [])
    a_conditions = a_filters.get("conditions", [])
    a_pool = (
        apply_condition_profiles(base, a_profiles)
        if a_profiles
        else apply_conditions(base, a_conditions, strict_missing=True)
    )
    if not a_pool.empty:
        return "LIVE_LIMIT_POOL_A", rank_candidates(a_pool, config, top_n)

    c_profiles = configured_c_condition_profiles(config)
    c_conditions = config.get("paper_ab_filtered_strategy", {}).get("c_strategy", {}).get("conditions", [])
    c_pool = (
        apply_condition_profiles(base, c_profiles)
        if c_profiles
        else apply_conditions(base, c_conditions, strict_missing=True)
    )
    if not c_pool.empty:
        c_rank_config = copy.deepcopy(config)
        c_ranking = config.get("paper_ab_filtered_strategy", {}).get("c_strategy", {}).get("ranking", {})
        if c_ranking:
            c_rank_config["ranking"] = copy.deepcopy(c_ranking)
            c_rank_config["ranking"].setdefault(
                "score_rules", copy.deepcopy(config.get("ranking", {}).get("score_rules", []))
            )
        c_pool["risk_flags"] = [build_risk_flags(row, config) for _, row in c_pool.iterrows()]
        c_pool = c_pool[~reject_strategy_risk_mask(c_pool, config, "c_strategy")].copy()
        if not c_pool.empty:
            return "LIVE_LIMIT_POOL_C", rank_candidates(c_pool, c_rank_config, top_n)

    return "LIVE_LIMIT_POOL_WATCH", rank_candidates(base, config, top_n)


def rank_candidates(data: pd.DataFrame, config: dict[str, Any], top_n: int) -> pd.DataFrame:
    result = data.copy()
    result["profit_source_score"] = calculate_profit_source_score(result, config)
    ranking = config.get("ranking", {})
    columns = [column for column in ranking.get("columns", []) if column in result.columns]
    if not columns:
        columns = ["profit_source_score", "fill_probability", "turnover_rate"]
    ascending = list(ranking.get("ascending", []))[: len(columns)]
    if len(ascending) != len(columns):
        ascending = [False] * len(columns)
    result = result.sort_values(columns + ["fill_probability", "turnover_rate"], ascending=ascending + [False, False])
    return result.head(top_n).reset_index(drop=True)


def output_paths(output_prefix: Path, signal_date: str) -> dict[str, Path]:
    return {
        "a_candidates": output_prefix.with_name(output_prefix.name + f"_{signal_date}_a_candidates.csv"),
        "c_candidates": output_prefix.with_name(output_prefix.name + f"_{signal_date}_c_candidates.csv"),
        "c_rejected": output_prefix.with_name(output_prefix.name + f"_{signal_date}_c_rejected_by_filter.csv"),
        "selected": output_prefix.with_name(output_prefix.name + f"_{signal_date}_selected.csv"),
        "planned_orders": output_prefix.with_name(output_prefix.name + f"_{signal_date}_planned_orders.csv"),
        "manual_review": output_prefix.with_name(output_prefix.name + f"_{signal_date}_manual_review.csv"),
        "execution_reference": output_prefix.with_name(output_prefix.name + f"_{signal_date}_execution_reference.csv"),
        "checklist": output_prefix.with_name(output_prefix.name + f"_{signal_date}_checklist.csv"),
        "markdown": output_prefix.with_name(output_prefix.name + f"_{signal_date}.md"),
    }


def build_outputs(candidates: pd.DataFrame, strategy_leg: str, signal_date: str, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    position = config.get("position", {})
    paper_trade = config.get("paper_trade", {})
    planned_equity = float(position.get("initial_cash", 500000))
    planned_position_pct = float(position.get("target_position_pct", 0.825))
    round_lot = int(paper_trade.get("round_lot_size", 100))
    # 兜底来源不是完整 A/C 历史回放结果，只允许生成观察清单，不生成 BUY 委托。
    selected_count = 0

    selected_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for idx, row in candidates.iterrows():
        rank = idx + 1
        planned_action = "PLAN_BUY_T1_OPEN" if rank <= selected_count else "WATCH_ONLY"
        risk_flags = build_risk_flags(row, config)
        base_row = {
            "signal_date": signal_date,
            "candidate_rank": rank,
            "planned_action": planned_action,
            "ts_code": row.get("ts_code", ""),
            "name": row.get("name", ""),
            "matched_condition_profile_ids": row.get(
                "matched_condition_profile_ids", ""
            ),
            "market_segment": row.get("market_segment", ""),
            "strategy_leg": strategy_leg,
            "selection_status": "TODAY_LIMIT_POOL_GENERATED",
            "operation_status": "PLAN_ONLY_PENDING" if planned_action == "PLAN_BUY_T1_OPEN" else "WATCH_ONLY",
            "profit_source_score": to_float(row.get("profit_source_score", 0.0)),
            "planned_position_pct": planned_position_pct if planned_action == "PLAN_BUY_T1_OPEN" else 0.0,
            "risk_flags": risk_flags,
            "fill_probability": to_float(row.get("fill_probability", 0.0)),
            "allow_buy_reliable": to_bool(row.get("allow_buy_reliable", False)),
            "is_fill_score_reliable": to_bool(row.get("is_fill_score_reliable", False)),
            "fd_ratio_bucket": row.get("fd_ratio_bucket", ""),
            "fd_amount_to_circ_mv": to_float(row.get("fd_amount_to_circ_mv", 0.0)),
            "segment_limit_up_count_bucket": row.get("segment_limit_up_count_bucket", ""),
            "market_chain_count_bucket": row.get("market_chain_count_bucket", ""),
            "limit_up_count_bucket": row.get("limit_up_count_bucket", ""),
            "market_leader_rank_bucket": row.get("market_leader_rank_bucket", ""),
            "board_type": row.get("board_type", ""),
            "first_time_detail_bucket": row.get("first_time_detail_bucket", ""),
            "turnover_rate_bucket": row.get("turnover_rate_bucket", ""),
            "amount_ratio_bucket": row.get("amount_ratio_bucket", ""),
            "open_times": to_float(row.get("open_times", 0.0)),
            "limit_times": to_float(row.get("limit_times", 0.0)),
            "first_time": row.get("first_time", ""),
            "last_time": row.get("last_time", ""),
            "amount": to_float(row.get("amount", 0.0)),
            "turnover_rate": to_float(row.get("turnover_rate", 0.0)),
            "reference_close": to_float(row.get("close", row.get("limit_close", 0.0))),
            "paper_note": "当日涨停池兜底计划，只用于模拟观察，不允许实盘。",
            "live_order_enabled": False,
        }
        selected_rows.append(base_row)
        if planned_action == "PLAN_BUY_T1_OPEN":
            reference_price = to_float(row.get("close", row.get("limit_close", 0.0)))
            planned_amount = planned_equity * planned_position_pct
            estimated_shares = int(planned_amount // reference_price) if reference_price > 0 else 0
            round_lot_shares = estimated_shares - estimated_shares % round_lot if round_lot > 0 else estimated_shares
            order_rows.append(
                {
                    "paper_order_id": f"LIVE-LIMIT-{signal_date}-{row.get('ts_code', '')}",
                    "signal_date": signal_date,
                    "strategy_leg": strategy_leg,
                    "planned_order_date": "",
                    "side": "BUY",
                    "ts_code": row.get("ts_code", ""),
                    "name": row.get("name", ""),
                    "planned_action": planned_action,
                    "order_status": "PLAN_ONLY",
                    "planned_position_pct": planned_position_pct,
                    "planned_equity": planned_equity,
                    "planned_amount_by_equity": planned_amount,
                    "reference_price": reference_price,
                    "estimated_shares": estimated_shares,
                    "round_lot_shares": round_lot_shares,
                    "risk_flags": risk_flags,
                    "manual_review_required": True,
                    "manual_review_status": "PENDING_MANUAL_REVIEW",
                    "manual_review_reason": "当日涨停池兜底计划，必须人工复核，只允许模拟观察。",
                    "live_order_enabled": False,
                }
            )

    selected = pd.DataFrame(selected_rows)
    planned_orders = pd.DataFrame(order_rows)
    checklist = pd.DataFrame(
        [
            {
                "signal_date": signal_date,
                "strategy_leg": strategy_leg if not candidates.empty else "NONE",
                "operation_status": "PLAN_ONLY_PENDING" if not planned_orders.empty else "NO_SELECTED",
                "selection_status": "TODAY_LIMIT_POOL_FALLBACK",
                "next_action": "已用当日涨停池生成模拟观察计划；不允许实盘下单，明早只做人工复核/预览。"
                if not planned_orders.empty
                else "当日涨停池没有可观察标的，明日暂不开仓。",
                "a_candidate_count": 0,
                "c_candidate_count": 0,
                "c_rejected_by_filter_count": 0,
                "selected_count": int(len(planned_orders)),
                "planned_order_count": int(len(planned_orders)),
                "manual_review_required": bool(not planned_orders.empty),
                "manual_review_count": int(len(planned_orders)),
                "top_ts_code": planned_orders["ts_code"].iloc[0] if not planned_orders.empty else "",
                "top_name": planned_orders["name"].iloc[0] if not planned_orders.empty else "",
                "risk_flags": planned_orders["risk_flags"].iloc[0] if not planned_orders.empty else "",
                "account_return": 0.0,
                "return_source": "today_limit_up_fill_scored_no_future_return",
                "live_order_enabled": False,
                "safety_note": "该文件来自当日涨停池兜底，不含未来收益验证；真实下单仍关闭。",
            }
        ]
    )
    return selected, planned_orders, checklist


def write_markdown(path: Path, checklist: pd.DataFrame, selected: pd.DataFrame, paths: dict[str, Path]) -> None:
    outputs = pd.DataFrame([{"name": name, "path": str(file_path)} for name, file_path in paths.items()])

    def table_text(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "无。"
        try:
            return frame.to_markdown(index=False)
        except ImportError:
            return frame.to_string(index=False)

    content = f"""# 当日涨停池模拟观察计划

本报告由 `limit_up_fill_scored.csv` 生成，只用于收盘后的次日模拟观察。

## 操作清单

{table_text(checklist)}

## 候选列表

{table_text(selected) if not selected.empty else "无候选。"}

## 输出文件

{table_text(outputs)}

## 安全限制

- `live_order_enabled=False`，不允许真实下单。
- 本报告不使用未来收益字段，只解决“当天数据已更新但 A/C 历史回放日期滞后”的启动问题。
- 实盘前必须继续走 QMT 预览、人工复核和小资金验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    runtime_config = load_json_config(args.runtime_config)
    strategy_config = load_json_config(args.strategy_config)
    assert_safe_runtime(runtime_config, strategy_config)

    input_path = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
    if not input_path.exists():
        raise FileNotFoundError(f"缺少当日成交概率文件: {input_path}")
    data = pd.read_csv(input_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    if data.empty:
        raise RuntimeError("limit_up_fill_scored.csv 为空。")
    data["trade_date"] = data["trade_date"].map(normalize_date)
    latest_date = str(data["trade_date"].max())
    if args.signal_date:
        requested = normalize_date(args.signal_date)
        if data["trade_date"].astype(str).eq(requested).any():
            signal_date = requested
        else:
            print(f"WARNING: limit_up_fill_scored.csv 无日期 {requested} 的记录，自动使用最新日期 {latest_date}")
            signal_date = latest_date
    else:
        signal_date = latest_date
    daily = data[data["trade_date"].astype(str) == signal_date].copy()

    daily = add_runtime_buckets(daily)
    strategy_leg, candidates = select_candidates(daily, strategy_config, args.top_n)
    selected, planned_orders, checklist = build_outputs(candidates, strategy_leg, signal_date, strategy_config)

    output_prefix = Path(args.output_prefix)
    if not output_prefix.is_absolute():
        output_prefix = PROJECT_ROOT / output_prefix
    mkdir_p(output_prefix.parent)
    paths = output_paths(output_prefix, signal_date)

    empty = pd.DataFrame()
    a_output = selected if strategy_leg == "LIVE_LIMIT_POOL_A" else empty
    c_output = selected if strategy_leg == "LIVE_LIMIT_POOL_C" else empty
    a_output.to_csv(paths["a_candidates"], index=False, encoding="utf-8-sig")
    c_output.to_csv(paths["c_candidates"], index=False, encoding="utf-8-sig")
    empty.to_csv(paths["c_rejected"], index=False, encoding="utf-8-sig")
    selected.to_csv(paths["selected"], index=False, encoding="utf-8-sig")
    planned_orders.to_csv(paths["planned_orders"], index=False, encoding="utf-8-sig")
    planned_orders.to_csv(paths["manual_review"], index=False, encoding="utf-8-sig")
    empty.to_csv(paths["execution_reference"], index=False, encoding="utf-8-sig")
    checklist.to_csv(paths["checklist"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], checklist, selected, paths)

    print("当日涨停池模拟观察计划完成：")
    print(f"- signal_date: {signal_date}")
    print(f"- strategy_leg: {strategy_leg}")
    print(f"- selected_count: {int(len(planned_orders))}")
    print(f"- planned_orders: {paths['planned_orders']}")
    print(checklist.to_string(index=False))


if __name__ == "__main__":
    main()
