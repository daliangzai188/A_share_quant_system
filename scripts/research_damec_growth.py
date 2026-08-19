"""D/A/M/E/C 五策略小范围增样研究（只读研究，不修改实盘配置）。

目标不是按某个倍数反向拟合参数，而是在当前 481 个信号日、82.5% 仓位、
单账户串行占仓、现有费用/滑点/成交约束下，测试少量预先声明且可解释的邻近改动。

运行：
    python3 scripts/research_damec_growth.py

输出：
    reports/damec_growth_research/variant_summary.csv
    reports/damec_growth_research/period_summary.csv
    reports/damec_growth_research/robustness_gates.csv
    reports/damec_growth_research/README.md

本脚本不会写 config、不会改当前认证报告、不会连接 QMT、不会下单。
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.certify_current_executable_portfolio as cert  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return  # noqa: E402
from scripts.run_paper_ab_filtered_observation_window import (  # noqa: E402
    reject_strategy_risk_mask,
)
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strategy_m import apply_base_filters  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


OUT = ROOT / "reports" / "damec_growth_research"
STRATEGY_CONFIG = ROOT / "config" / "strategy_config.json"
RUNTIME_CONFIG = ROOT / "config" / "config.json"
FULL_FEATURE_SOURCE = ROOT / "data" / "processed" / "next_day_premium_trades_2y.csv"
M_SOURCE = ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
LEGACY_CENSORED_D_SOURCE = ROOT / "reports" / "strategy_d" / "d_trades.csv"
LEGACY_CENSORED_MULTIPLE = 4445.281570391435
WINDOW_START = "20240520"
WINDOW_END = "20260514"


# 只允许这些预先声明的单变量邻近改动。不要在本脚本里按结果动态穷举取最优。
A_VARIANTS: dict[str, dict[str, Any]] = {
    "CURRENT": {},
    "A_ADD_SEGMENT_5_10": {
        "segment_values": {"lt_5", "5_10"},
        "description": "A所属分段涨停桶由lt_5扩至lt_5+5_10",
    },
    "A_ADD_FD_1_2PCT": {
        "fd_values": {"0_5pct_1pct", "1pct_2pct"},
        "description": "A封单市值比加入相邻1%-2%桶",
    },
    "A_ADD_FD_0_3_0_5PCT": {
        "fd_values": {"0_3pct_0_5pct", "0_5pct_1pct"},
        "description": "A封单市值比加入相邻0.3%-0.5%桶",
    },
    "A_ADD_CHAIN_15_30": {
        "chain_values": {"8_15", "15_30"},
        "description": "A连板数量加入相邻15-30桶",
    },
    "A_ADD_CHAIN_3_8": {
        "chain_values": {"3_8", "8_15"},
        "description": "A连板数量加入相邻3-8桶",
    },
    "A_ALLOW_AMOUNT_0_8_1_2": {
        "exclude_amount": False,
        "description": "A取消成交额倍率0.8-1.2排除项",
    },
    "A_ALLOW_BJ_PREV_0_3": {
        "exclude_bj_prev": False,
        "description": "A取消北交所且前涨幅0-3%的复合排除项",
    },
    "A_FALLBACK_FD_0_3_0_5PCT": {
        "fallback_fd_values": {"0_3pct_0_5pct"},
        "description": "A原规则无候选时，才用封单0.3%-0.5%桶补位",
    },
    "A_FALLBACK_SEGMENT_5_10": {
        "fallback_segment_values": {"5_10"},
        "description": "A原规则无候选时，才用分段涨停5-10桶补位",
    },
    "A_FALLBACK_CHAIN_3_8": {
        "fallback_chain_values": {"3_8"},
        "description": "A原规则无候选时，才用连板3-8桶补位",
    },
    "A_FALLBACK_CHAIN_15_30": {
        "fallback_chain_values": {"15_30"},
        "description": "A原规则无候选时，才用连板15-30桶补位",
    },
}

C_VARIANTS: dict[str, dict[str, Any]] = {
    "CURRENT": {},
    "C_ADD_SEGMENT_20_40": {
        "segment_values": {"40_80", "20_40"},
        "description": "C所属分段涨停桶加入相邻20-40",
    },
    "C_ADD_SEGMENT_GTE80": {
        "segment_values": {"40_80", "gte_80"},
        "description": "C所属分段涨停桶加入相邻>=80",
    },
    "C_ADD_CHAIN_GTE30": {
        "chain_values": {"15_30", "gte_30"},
        "description": "C连板数量加入相邻>=30桶",
    },
    "C_ADD_CHAIN_8_15": {
        "chain_values": {"8_15", "15_30"},
        "description": "C连板数量加入相邻8-15桶（A仍优先）",
    },
    "C_ALLOW_HIGH_FD_ONLY": {
        "allow_high_fd": True,
        "description": "C仅取消封单市值比偏高拒绝，仍保留LOSS_OVERLAY拒绝",
    },
    "C_OPEN_TIMES_PLUS1": {
        "open_times_plus_one": True,
        "description": "C各板高炸板次数拒绝阈值统一放宽1次",
    },
    "C_FALLBACK_SEGMENT_20_40": {
        "fallback_segment_values": {"20_40"},
        "description": "C原规则无候选时，才用分段涨停20-40桶补位",
    },
    "C_FALLBACK_SEGMENT_GTE80": {
        "fallback_segment_values": {"gte_80"},
        "description": "C原规则无候选时，才用分段涨停>=80桶补位",
    },
    "C_FALLBACK_CHAIN_8_15": {
        "fallback_chain_values": {"8_15"},
        "description": "C原规则无候选时，才用连板8-15桶补位",
    },
    "C_FALLBACK_CHAIN_GTE30": {
        "fallback_chain_values": {"gte_30"},
        "description": "C原规则无候选时，才用连板>=30桶补位",
    },
}

D_VARIANTS: dict[str, dict[str, Any]] = {
    "CURRENT": {},
    "D_FILL_75": {"min_fill": 0.75, "description": "D最低成交概率80%降至75%"},
    "D_MAX_OPEN_4": {"max_open": 4, "description": "D炸板次数上限3放宽到4"},
    "D_ADD_EARLY_MORNING": {
        "first_buckets": {"early_morning", "midday", "afternoon", "late"},
        "description": "D首次封板加入10点前早盘桶",
    },
    "D_RESEAL_1330": {"last_hhmm": 1330, "description": "D最后回封下限14:00提前至13:30"},
    "D_ADD_NEUTRAL": {
        "sentiments": {"strong", "neutral"},
        "description": "D市场情绪加入相邻neutral桶",
    },
    "D_ADD_VERY_STRONG": {
        "sentiments": {"strong", "very_strong"},
        "description": "D市场情绪加入相邻very_strong桶",
    },
    "D_ADD_WEAK": {
        "sentiments": {"strong", "weak"},
        "description": "D市场情绪加入weak桶（压力对照）",
    },
    "D_FALLBACK_OPEN_4": {
        "fallback_open_times": {4},
        "description": "D原规则无候选时，才用炸板4次候选补位",
    },
    "D_FALLBACK_VERY_STRONG": {
        "fallback_sentiments": {"very_strong"},
        "description": "D原规则无候选时，才用very_strong情绪补位",
    },
    "D_FALLBACK_NEUTRAL": {
        "fallback_sentiments": {"neutral"},
        "description": "D原规则无候选时，才用neutral情绪补位",
    },
}

M_VARIANTS: dict[str, dict[str, Any]] = {
    "CURRENT": {},
    "M_ADD_NEUTRAL": {
        "sentiments": {"weak", "neutral"},
        "description": "M深市主板情绪由weak扩至weak+neutral",
    },
    "M_ADD_STRONG": {
        "sentiments": {"weak", "strong"},
        "description": "M深市主板情绪由weak扩至weak+strong（压力对照）",
    },
    "M_ADD_VERY_STRONG": {
        "sentiments": {"weak", "very_strong"},
        "description": "M深市主板情绪由weak扩至weak+very_strong（压力对照）",
    },
    "M_FALLBACK_NEUTRAL": {
        "fallback_sentiments": {"neutral"},
        "description": "M原weak规则无候选时，才用neutral情绪补位",
    },
    "M_FALLBACK_STRONG": {
        "fallback_sentiments": {"strong"},
        "description": "M原weak规则无候选时，才用strong情绪补位",
    },
    "M_FALLBACK_VERY_STRONG": {
        "fallback_sentiments": {"very_strong"},
        "description": "M原weak规则无候选时，才用very_strong情绪补位",
    },
}


def norm_date(value: Any) -> str:
    return cert.normalize_date(value)


def bool_series(frame: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=bool)
    return frame[column].astype(str).str.lower().isin({"true", "1", "yes"})


def account_return_from_stock(stock_return: float) -> float:
    """与当前 A/C 认证逐字同口径：滑点已在价格中，另显式扣三项费用。"""

    return (
        stock_return
        - cert.AC_BUY_FEE_RATE
        - (1.0 + stock_return) * cert.AC_SELL_FEE_RATE
    ) * cert.POSITION_PCT


def load_feature_pool() -> tuple[pd.DataFrame, dict[str, Any], PaperCandidateGenerator]:
    cfg = load_json_config(STRATEGY_CONFIG)
    generator = PaperCandidateGenerator(STRATEGY_CONFIG, input_trades_path=FULL_FEATURE_SOURCE)
    pool = generator.load_all_candidates()
    pool["trade_date"] = pool["trade_date"].map(norm_date)
    pool = pool[(pool["trade_date"] >= WINDOW_START) & (pool["trade_date"] <= WINDOW_END)].copy()
    return pool, cfg, generator


def filter_common_ac(
    pool: pd.DataFrame,
    generator: PaperCandidateGenerator,
    *,
    segment_values: set[str],
    chain_values: set[str],
    fd_values: set[str] | None,
    exclude_amount: bool,
    exclude_bj_prev: bool,
) -> pd.DataFrame:
    result = generator.apply_universe_filters(pool)
    result = result[result["segment_limit_up_count_bucket"].astype(str).isin(segment_values)]
    result = result[result["market_chain_count_bucket"].astype(str).isin(chain_values)]
    if fd_values is not None:
        result = result[result["fd_ratio_bucket"].astype(str).isin(fd_values)]
    if exclude_amount:
        result = result[result["amount_ratio_bucket"].astype(str).ne("0_8_1_2")]
    if exclude_bj_prev:
        blocked = (
            result["market_segment"].astype(str).eq("bj")
            & result["prev_pct_chg_bucket"].astype(str).eq("0_3")
        )
        result = result[~blocked]
    return result.copy()


def c_risk_config(base: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(base)
    rules = cfg["paper_ab_filtered_strategy"]["c_strategy"]["risk_reject_rules"]
    if variant.get("allow_high_fd"):
        for rule in rules:
            if rule.get("name") == "reject_fd_warn_or_loss_overlay":
                rule["risk_flags_contains_any"] = ["LOSS_OVERLAY_WATCH"]
    if variant.get("open_times_plus_one"):
        for rule in rules:
            if rule.get("name") != "reject_open_times_by_board_height":
                continue
            for group in rule.get("compound_conditions", []):
                for condition in group:
                    if condition.get("column") == "open_times" and condition.get("operator") == ">=":
                        condition["value"] = int(condition["value"]) + 1
    return cfg


def build_ac_map(
    pool: pd.DataFrame,
    base_cfg: dict[str, Any],
    generator: PaperCandidateGenerator,
    a_variant: dict[str, Any],
    c_variant: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    a_has_fallback = any(str(key).startswith("fallback_") for key in a_variant)
    a_segment = set(a_variant.get("segment_values", {"lt_5"})) if not a_has_fallback else {"lt_5"}
    a_chain = set(a_variant.get("chain_values", {"8_15"})) if not a_has_fallback else {"8_15"}
    a_fd = set(a_variant.get("fd_values", {"0_5pct_1pct"})) if not a_has_fallback else {"0_5pct_1pct"}
    a = filter_common_ac(
        pool,
        generator,
        segment_values=a_segment,
        chain_values=a_chain,
        fd_values=a_fd,
        exclude_amount=bool(a_variant.get("exclude_amount", True)),
        exclude_bj_prev=bool(a_variant.get("exclude_bj_prev", True)),
    )
    a_fallback = pd.DataFrame(columns=pool.columns)
    if a_has_fallback:
        a_fallback = filter_common_ac(
            pool,
            generator,
            segment_values=set(a_variant.get("fallback_segment_values", {"lt_5"})),
            chain_values=set(a_variant.get("fallback_chain_values", {"8_15"})),
            fd_values=set(a_variant.get("fallback_fd_values", {"0_5pct_1pct"})),
            exclude_amount=True,
            exclude_bj_prev=True,
        )

    c_has_fallback = any(str(key).startswith("fallback_") for key in c_variant)
    c_segment = set(c_variant.get("segment_values", {"40_80"})) if not c_has_fallback else {"40_80"}
    c_chain = set(c_variant.get("chain_values", {"15_30"})) if not c_has_fallback else {"15_30"}
    # C与当前实盘相同：继承顶层排除项，但用自己的两个include条件；不要求A的fd桶。
    c = filter_common_ac(
        pool,
        generator,
        segment_values=c_segment,
        chain_values=c_chain,
        fd_values=None,
        exclude_amount=True,
        exclude_bj_prev=True,
    )
    c_fallback = pd.DataFrame(columns=pool.columns)
    if c_has_fallback:
        c_fallback = filter_common_ac(
            pool,
            generator,
            segment_values=set(c_variant.get("fallback_segment_values", {"40_80"})),
            chain_values=set(c_variant.get("fallback_chain_values", {"15_30"})),
            fd_values=None,
            exclude_amount=True,
            exclude_bj_prev=True,
        )
    c_cfg = c_risk_config(base_cfg, c_variant)

    a_by = {date: rows for date, rows in a.groupby("trade_date")}
    a_fallback_by = {date: rows for date, rows in a_fallback.groupby("trade_date")}
    c_by = {date: rows for date, rows in c.groupby("trade_date")}
    c_fallback_by = {date: rows for date, rows in c_fallback.groupby("trade_date")}
    result: dict[str, dict[str, Any]] = {}
    for date in sorted(set(a_by) | set(a_fallback_by) | set(c_by) | set(c_fallback_by)):
        leg = ""
        picked: pd.Series | None = None
        if date in a_by:
            ranked = generator.rank_candidates(a_by[date].copy()).reset_index(drop=True)
            if not ranked.empty:
                leg, picked = "A", ranked.iloc[0]
        if picked is None and date in a_fallback_by:
            ranked = generator.rank_candidates(a_fallback_by[date].copy()).reset_index(drop=True)
            if not ranked.empty:
                leg, picked = "A", ranked.iloc[0]
        if picked is None and date in c_by:
            ranked = generator.rank_candidates(c_by[date].copy()).reset_index(drop=True)
            rejected = reject_strategy_risk_mask(ranked, c_cfg, "c_strategy")
            ranked = ranked[~pd.Series(rejected.values, index=ranked.index)]
            if not ranked.empty:
                leg, picked = "C", ranked.iloc[0]
        if picked is None and date in c_fallback_by:
            ranked = generator.rank_candidates(c_fallback_by[date].copy()).reset_index(drop=True)
            rejected = reject_strategy_risk_mask(ranked, c_cfg, "c_strategy")
            ranked = ranked[~pd.Series(rejected.values, index=ranked.index)]
            if not ranked.empty:
                leg, picked = "C", ranked.iloc[0]
        if picked is None:
            continue
        code = str(picked["ts_code"])
        hold = 2 if leg == "A" else 3
        status, buy_date, exit_date, stock_return = trade_return(date, code, hold)
        if status != "OK" or stock_return is None:
            continue
        result[date] = {
            "strategy_leg": leg,
            "ts_code": code,
            "name": str(picked.get("name", "")),
            "buy_date": norm_date(buy_date),
            "exit_date": norm_date(exit_date),
            "account_return": account_return_from_stock(float(stock_return)),
            "return_source": "DAMEC研究:A/C同口径逐日重建",
        }
    return result


def load_d_pool() -> pd.DataFrame:
    pool = pd.read_csv(FULL_FEATURE_SOURCE, low_memory=False)
    pool["trade_date"] = pool["trade_date"].map(norm_date)
    return pool[(pool["trade_date"] >= WINDOW_START) & (pool["trade_date"] <= WINDOW_END)].copy()


def build_d_frame(pool: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
    fallback_sentiments = variant.get("fallback_sentiments")
    fallback_open_times = variant.get("fallback_open_times")
    if fallback_sentiments is not None or fallback_open_times is not None:
        primary = build_d_frame(pool, {})
        secondary_spec: dict[str, Any] = {}
        if fallback_sentiments is not None:
            secondary_spec["sentiments"] = set(fallback_sentiments)
        if fallback_open_times is not None:
            secondary_spec["open_times_values"] = set(fallback_open_times)
            secondary_spec["max_open"] = max(int(value) for value in fallback_open_times)
        secondary = build_d_frame(pool, secondary_spec)
        secondary = secondary[~secondary.index.isin(primary.index)]
        return pd.concat([primary, secondary]).sort_index()

    min_fill = float(variant.get("min_fill", 0.80))
    max_open = int(variant.get("max_open", 3))
    open_times_values = variant.get("open_times_values")
    first_buckets = set(variant.get("first_buckets", {"midday", "afternoon", "late"}))
    last_hhmm = int(variant.get("last_hhmm", 1400))
    sentiments = set(variant.get("sentiments", {"strong"}))
    allowed_segments = set(
        json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
        .get("strategy_d", {})
        .get("allowed_market_segments", ["sh_main", "sz_main", "chi_next", "star", "bj", "other"])
    )
    open_times = pd.to_numeric(pool["open_times"], errors="coerce")
    open_mask = (
        open_times.isin(set(open_times_values))
        if open_times_values is not None
        else open_times.between(2, max_open)
    )
    mask = (
        pd.to_numeric(pool["limit_times"], errors="coerce").eq(1)
        & ~bool_series(pool, "is_st")
        & pool["market_sentiment_level"].astype(str).isin(sentiments)
        & pool["board_type"].astype(str).eq("multi_open")
        & open_mask
        & pool["first_time_bucket"].astype(str).isin(first_buckets)
        & pd.to_numeric(pool["last_time"], errors="coerce").ge(last_hhmm * 100)
        & pd.to_numeric(pool["fill_probability"], errors="coerce").ge(min_fill)
        & bool_series(pool, "is_fill_score_reliable")
        & pool["market_segment"].astype(str).isin(allowed_segments)
    )
    candidates = pool[mask].copy()
    candidates["_preferred"] = pd.to_numeric(candidates["open_times"], errors="coerce").eq(2).astype(int)
    candidates["_fd"] = pd.to_numeric(candidates["fd_amount_to_circ_mv"], errors="coerce").fillna(-np.inf)
    candidates = candidates.sort_values(
        ["trade_date", "_preferred", "_fd", "ts_code"],
        ascending=[True, False, False, False],
    ).drop_duplicates("trade_date", keep="first")
    return candidates.set_index("trade_date")


def load_m_pool() -> pd.DataFrame:
    pool = pd.read_csv(M_SOURCE, low_memory=False)
    pool["trade_date"] = pool["trade_date"].map(norm_date)
    return pool[(pool["trade_date"] >= WINDOW_START) & (pool["trade_date"] <= WINDOW_END)].copy()


def build_m_frame(pool: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
    fallback_sentiments = variant.get("fallback_sentiments")
    if fallback_sentiments is not None:
        primary = build_m_frame(pool, {})
        secondary = build_m_frame(pool, {"sentiments": set(fallback_sentiments)})
        secondary = secondary[~secondary.index.isin(primary.index)]
        return pd.concat([primary, secondary]).sort_index()

    sentiments = set(variant.get("sentiments", {"weak"}))
    eligible = apply_base_filters(pool)
    eligible = eligible[eligible["sz_main_market_sentiment_level"].astype(str).isin(sentiments)].copy()
    eligible["circ_mv"] = pd.to_numeric(eligible["circ_mv"], errors="coerce")
    eligible = eligible[eligible["circ_mv"].notna()].sort_values(
        ["trade_date", "circ_mv", "ts_code"], ascending=[True, True, True]
    ).drop_duplicates("trade_date", keep="first")
    rows: list[dict[str, Any]] = []
    for row in eligible.itertuples(index=False):
        date = str(row.trade_date)
        code = str(row.ts_code)
        status, buy_date, exit_date, net_return = trade_return(date, code, 2)
        if status != "OK" or net_return is None:
            continue
        rows.append(
            {
                "trade_date": date,
                "ts_code": code,
                "name": str(getattr(row, "name", "")),
                "market_segment": str(getattr(row, "market_segment", "")),
                "circ_mv": float(getattr(row, "circ_mv")),
                "sentiment": str(getattr(row, "sz_main_market_sentiment_level", "")),
                "buy_date": norm_date(buy_date),
                "exit_date": norm_date(exit_date),
                "net_return": float(net_return),
                "select_reason": "DAMEC研究:M相邻情绪桶+流通市值最小",
            }
        )
    return pd.DataFrame(rows).set_index("trade_date") if rows else pd.DataFrame()


def sources_with_d(base: cert.Sources, d_frame: pd.DataFrame) -> cert.Sources:
    baseline = base.baseline.copy()
    baseline["d_return"] = baseline["date"].isin(set(d_frame.index)).astype(float)
    return replace(base, baseline=baseline, strategy_d=d_frame)


def period_metrics(detail: pd.DataFrame, variant: str) -> list[dict[str, Any]]:
    dates = detail["signal_date"].astype(str)
    split_date = str(detail.iloc[len(detail) // 2]["signal_date"])
    periods = [
        ("全部", pd.Series(True, index=detail.index)),
        (f"前半段<{split_date}", dates < split_date),
        (f"后半段>={split_date}", dates >= split_date),
    ]
    periods.extend((year, dates.str[:4].eq(year)) for year in sorted(dates.str[:4].unique()))
    rows: list[dict[str, Any]] = []
    for label, mask in periods:
        trades = detail[mask & detail["status"].astype(str).eq("EXECUTED")]
        returns = pd.to_numeric(trades["account_return"], errors="raise")
        equity = (1.0 + returns).cumprod()
        drawdown = equity / equity.cummax() - 1.0 if len(equity) else pd.Series(dtype=float)
        rows.append(
            {
                "variant": variant,
                "period": label,
                "trade_count": int(len(returns)),
                "equity_multiple": float(equity.iloc[-1]) if len(equity) else 1.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_return": float(returns.mean()) if len(returns) else 0.0,
                "median_return": float(returns.median()) if len(returns) else 0.0,
                "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
            }
        )
    return rows


def validate_rebuild(
    base: cert.Sources,
    ac_current: dict[str, dict[str, Any]],
    d_current: pd.DataFrame,
    m_current: pd.DataFrame,
) -> None:
    def key_map(mapping: dict[str, dict[str, Any]]) -> dict[str, tuple[str, str]]:
        return {date: (str(row["strategy_leg"]), str(row["ts_code"])) for date, row in mapping.items()}

    if key_map(ac_current) != key_map(base.ac_daily):
        left, right = key_map(ac_current), key_map(base.ac_daily)
        changed = sorted(date for date in set(left) | set(right) if left.get(date) != right.get(date))
        raise RuntimeError(f"A/C当前口径重建未对齐，差异日期{len(changed)}，样例={changed[:8]}")
    d_codes = d_current["ts_code"].astype(str).to_dict()
    base_d_codes = base.strategy_d["ts_code"].astype(str).to_dict()
    # 正式认证现已使用完整逐日D候选，研究重建必须逐日、逐股完全一致。
    changed = sorted(
        date
        for date in set(d_codes) | set(base_d_codes)
        if d_codes.get(date) != base_d_codes.get(date)
    )
    if changed:
        raise RuntimeError(f"D锁定账本中的已有日期无法复现，差异{len(changed)}，样例={changed[:8]}")
    m_codes = m_current["ts_code"].astype(str).to_dict()
    base_m_codes = base.m_pool["ts_code"].astype(str).to_dict() if base.m_pool is not None else {}
    if m_codes != base_m_codes:
        changed = sorted(date for date in set(m_codes) | set(base_m_codes) if m_codes.get(date) != base_m_codes.get(date))
        raise RuntimeError(f"M当前口径重建未对齐，差异日期{len(changed)}，样例={changed[:8]}")


def evaluate(
    name: str,
    description: str,
    sources: cert.Sources,
    *,
    entry_gate_enabled: bool = True,
) -> tuple[dict[str, Any], pd.DataFrame, list[dict[str, Any]]]:
    detail = cert.replay(
        sources,
        entry_gate_enabled=entry_gate_enabled,
        m_enabled=True,
        block_d_on_handoff=True,
    )
    summary = cert.summarize(detail, name)
    summary["variant"] = name
    summary["description"] = description
    return summary, detail, period_metrics(detail, name)


def robustness_rows(summary: pd.DataFrame, periods: pd.DataFrame) -> pd.DataFrame:
    base_name = "CURRENT_COMPLETE_D"
    base = summary.set_index("variant").loc[base_name]
    p = periods.pivot(index="variant", columns="period", values="equity_multiple")
    split_cols = [column for column in p.columns if column.startswith("前半段") or column.startswith("后半段")]
    rows = []
    for _, row in summary.iterrows():
        name = str(row["variant"])
        first_ok = all(float(p.loc[name, col]) >= float(p.loc[base_name, col]) * 0.95 for col in split_cols)
        samples_up = int(row["executed_trade_count"]) > int(base["executed_trade_count"])
        full_up = float(row["equity_multiple"]) > float(base["equity_multiple"]) * (1.0 + 1e-10)
        dd_ok = float(row["max_drawdown"]) >= float(base["max_drawdown"]) - 0.03
        rows.append(
            {
                "variant": name,
                "samples_increased": samples_up,
                "full_multiple_increased": full_up,
                "both_halves_not_worse_than_95pct_of_current": first_ok,
                "drawdown_not_worse_by_over_3pct_points": dd_ok,
                "research_gate_passed": bool(samples_up and full_up and first_ok and dd_ok),
                "note": "研究门，不代表可实盘；全部数据均非真正未见样本",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = cert.load_sources()
    baseline_detail = cert.replay(base, entry_gate_enabled=True, m_enabled=True)
    baseline_summary = cert.summarize(baseline_detail, "CURRENT")
    if int(baseline_summary["executed_trade_count"]) != cert.EXPECTED_CURRENT_TRADE_COUNT:
        raise RuntimeError("当前样本数偏离冻结锚点，先停止研究")
    if abs(float(baseline_summary["equity_multiple"]) - cert.EXPECTED_CURRENT_MULTIPLE) > 1e-8:
        raise RuntimeError("当前复利偏离冻结锚点，先停止研究")

    feature_pool, strategy_cfg, generator = load_feature_pool()
    d_pool = load_d_pool()
    m_pool = load_m_pool()
    current_ac = build_ac_map(feature_pool, strategy_cfg, generator, {}, {})
    current_d = build_d_frame(d_pool, {})
    current_m = build_m_frame(m_pool, {})
    validate_rebuild(base, current_ac, current_d, current_m)

    summaries: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    details: dict[str, pd.DataFrame] = {}

    legacy_d = pd.read_csv(
        LEGACY_CENSORED_D_SOURCE, dtype={"signal_date": str}, low_memory=False
    )
    legacy_d["signal_date"] = legacy_d["signal_date"].map(norm_date)
    legacy_d = legacy_d.drop_duplicates("signal_date", keep="last").set_index("signal_date")
    legacy_sources = sources_with_d(base, legacy_d)
    summary, detail, period = evaluate(
        "LEGACY_CENSORED_D_4445_REFERENCE",
        "旧4445倍审计参考；D候选被旧A/B/C占仓路径提前裁剪，已作废且不能作为优化基线",
        legacy_sources,
    )
    if abs(float(summary["equity_multiple"]) - LEGACY_CENSORED_MULTIPLE) > 1e-8:
        raise RuntimeError("旧4445倍审计参考无法复现，拒绝写研究报告")
    summaries.append(summary); period_rows.extend(period); details["LEGACY_CENSORED_D_4445_REFERENCE"] = detail

    # 用当前D规则从完整母池逐日重建，并再次确认与正式认证候选完全一致。
    research_base = sources_with_d(base, current_d)
    summary, detail, period = evaluate(
        "CURRENT_COMPLETE_D",
        "当前全部规则；D改用完整逐日候选母池（研究主基线）",
        research_base,
    )
    summaries.append(summary); period_rows.extend(period); details["CURRENT_COMPLETE_D"] = detail

    for name, variant in A_VARIANTS.items():
        if name == "CURRENT":
            continue
        ac = build_ac_map(feature_pool, strategy_cfg, generator, variant, {})
        s = replace(research_base, ac_daily=ac)
        summary, detail, period = evaluate(name, str(variant["description"]), s)
        summaries.append(summary); period_rows.extend(period); details[name] = detail

    for name, variant in C_VARIANTS.items():
        if name == "CURRENT":
            continue
        ac = build_ac_map(feature_pool, strategy_cfg, generator, {}, variant)
        s = replace(research_base, ac_daily=ac)
        summary, detail, period = evaluate(name, str(variant["description"]), s)
        summaries.append(summary); period_rows.extend(period); details[name] = detail

    for name, variant in D_VARIANTS.items():
        if name == "CURRENT":
            continue
        d_frame = build_d_frame(d_pool, variant)
        s = sources_with_d(research_base, d_frame)
        summary, detail, period = evaluate(name, str(variant["description"]), s)
        summaries.append(summary); period_rows.extend(period); details[name] = detail

    for name, variant in M_VARIANTS.items():
        if name == "CURRENT":
            continue
        m_frame = build_m_frame(m_pool, variant)
        s = replace(research_base, m_pool=m_frame)
        summary, detail, period = evaluate(name, str(variant["description"]), s)
        summaries.append(summary); period_rows.extend(period); details[name] = detail

    summary, detail, period = evaluate(
        "E_NO_ENTRY_GATE",
        "E保留R1第一名但取消13:30-14:30门禁（不回补第二名）",
        research_base,
        entry_gate_enabled=False,
    )
    summaries.append(summary); period_rows.extend(period); details["E_NO_ENTRY_GATE"] = detail

    summary_df = pd.DataFrame(summaries)
    corrected_current = next(
        float(row["equity_multiple"])
        for row in summaries
        if str(row["variant"]) == "CURRENT_COMPLETE_D"
    )
    corrected_count = next(
        int(row["executed_trade_count"])
        for row in summaries
        if str(row["variant"]) == "CURRENT_COMPLETE_D"
    )
    summary_df["trade_delta"] = summary_df["executed_trade_count"] - corrected_count
    summary_df["multiple_vs_current"] = summary_df["equity_multiple"] / corrected_current
    summary_df["multiple_vs_legacy_4445"] = (
        summary_df["equity_multiple"] / LEGACY_CENSORED_MULTIPLE
    )
    summary_df = summary_df.sort_values("equity_multiple", ascending=False).reset_index(drop=True)
    periods_df = pd.DataFrame(period_rows)
    gates = robustness_rows(summary_df, periods_df)

    summary_df.to_csv(OUT / "variant_summary.csv", index=False, encoding="utf-8-sig")
    periods_df.to_csv(OUT / "period_summary.csv", index=False, encoding="utf-8-sig")
    gates.to_csv(OUT / "robustness_gates.csv", index=False, encoding="utf-8-sig")
    for name, detail in details.items():
        detail[detail["status"].astype(str).eq("EXECUTED")].to_csv(
            OUT / f"trades_{name.lower()}.csv", index=False, encoding="utf-8-sig"
        )

    view = summary_df[
        [
            "variant", "executed_trade_count", "trade_delta", "equity_multiple",
            "multiple_vs_current", "multiple_vs_legacy_4445", "max_drawdown", "win_rate", "avg_return",
            "median_return", "profit_loss_ratio", "max_consecutive_losses", "description",
        ]
    ]
    passed = gates[gates["research_gate_passed"]]["variant"].tolist()
    readme = [
        "# D/A/M/E/C 小范围增样研究",
        "",
        f"- 当前正式参考：{cert.EXPECTED_CURRENT_TRADE_COUNT}笔，{cert.EXPECTED_CURRENT_MULTIPLE:.6f}倍。",
        f"- 完整D逐日母池重建后的研究主基线：{corrected_count}笔，{corrected_current:.6f}倍。",
        f"- 旧错误参考：{LEGACY_CENSORED_MULTIPLE:.6f}倍；仅保留为审计对照，不参与研究门。",
        "- 差异原因：旧D账本先被历史A/B/C的POSITION_OCCUPIED_SKIP裁过，不是独立逐日D候选母池。",
        "- 所有变体均保持D>A>M>E>C、82.5%仓位、单账户占仓、费用/滑点、涨跌停和D成交压力口径。",
        "- 每次只改变一条腿的一个邻近条件；没有用10000倍作为筛选条件。",
        "- 前后半段只是稳定性复核，不是真正样本外，因为这些历史数据已参与过策略研发。",
        "- 机械复利没有认证资金容量，倍数不能视为可实际承载的资金结果。",
        "",
        f"研究门通过：{', '.join(passed) if passed else '无'}",
        "",
        "完整结果见 variant_summary.csv、period_summary.csv 和逐变体交易明细。",
    ]
    (OUT / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

    print("D/A/M/E/C 小范围研究完成（未修改任何实盘配置）")
    print(view.to_string(index=False))
    print("\n研究门通过:", passed if passed else "无")
    print("输出目录:", OUT)


if __name__ == "__main__":
    main()
