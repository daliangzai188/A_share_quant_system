#!/usr/bin/env python3
"""研究D完整首板触板母池的一分钟路径，不修改正式策略。

本脚本只分析已经由``build_strategy_d_intraday_event_ledger.py``生成的事件账本：

1. 验证6,848只次目标均有完整241根一分钟K；
2. 对比一分钟路径与旧五分钟近似路径的信号稳定性；
3. 统计第一次可交易回封后的成交证据、失败收盘、爆发和爆亏结构；
4. 对仅使用信号时点已知字段的候选结构做样本内诊断。

同日存在多个互斥候选，且缺少信号时点封单/流通市值排名和历史买一队列，
因此这里的复利、回撤只能称为“事件流诊断”，不得冒充D独立策略或ACDE组合复利。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


START = "20240630"
END = "20260630"
FIRST_12M_END = "20250630"
SECOND_12M_START = "20250701"
OUTLIER_DATES = ("20240926", "20240927")

EXPECTED_TARGET_COUNT = 6848
EXPECTED_BAR_COUNT = 241
EXPECTED_SIGNAL_COUNT = 370
EXPECTED_CONFIRMED_FILL_COUNT = 263
EXPECTED_QUEUE_UNKNOWN_COUNT = 107
EXPECTED_FAILED_CLOSE_SIGNAL_COUNT = 60
EXPECTED_STATIC_OVERLAP_COUNT = 93
EXPECTED_SOURCE = "TUSHARE_STK_MINS_1M_UNADJUSTED"

DEFAULT_LEDGER = ROOT / "data/research/strategy_d_intraday/event_ledger.csv"
DEFAULT_FIVE_MINUTE_LEDGER = (
    ROOT / "data/research/strategy_d_intraday/event_ledger_5m_baostock.csv"
)
DEFAULT_OUTPUT_DIR = ROOT / "reports/strategy_d_intraday_1m_research"


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bool_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.fillna("").astype(str).str.lower().isin({"true", "1", "yes"})


def load_one_minute_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"缺少D一分钟事件账本：{path}")
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    required = {
        "trade_date",
        "ts_code",
        "minute_data_source",
        "minute_status",
        "bar_count",
        "first_hhmm",
        "last_hhmm",
        "signal_rule_current",
        "eligible_signal_hhmm",
        "first_seal_hhmm",
        "open_times_at_signal",
        "queue_fill_status",
        "confirmed_fill_by_price",
        "failed_to_close_at_limit",
        "execution_status",
        "account_return",
        "path_ambiguous",
        "in_current_static_d_pool",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D一分钟事件账本缺少字段：{missing}")
    frame["trade_date"] = date_text(frame["trade_date"])
    duplicate_count = int(frame.duplicated(["trade_date", "ts_code"]).sum())
    source_values = sorted(frame["minute_data_source"].dropna().astype(str).unique())
    status_values = sorted(frame["minute_status"].dropna().astype(str).unique())
    failures: list[str] = []
    if len(frame) != EXPECTED_TARGET_COUNT:
        failures.append(f"目标数={len(frame)}")
    if duplicate_count:
        failures.append(f"重复键={duplicate_count}")
    if source_values != [EXPECTED_SOURCE]:
        failures.append(f"数据源={source_values}")
    if status_values != ["READY_1M_PATH_NO_QUEUE_DEPTH"]:
        failures.append(f"覆盖状态={status_values}")
    if not pd.to_numeric(frame["bar_count"], errors="coerce").eq(EXPECTED_BAR_COUNT).all():
        failures.append("并非每个目标都是241根")
    if not pd.to_numeric(frame["first_hhmm"], errors="coerce").eq(930).all():
        failures.append("并非全部从09:30开始")
    if not pd.to_numeric(frame["last_hhmm"], errors="coerce").eq(1500).all():
        failures.append("并非全部覆盖至15:00")
    if failures:
        raise RuntimeError("D一分钟事件账本未通过冻结校验：" + "；".join(failures))

    for column in (
        "signal_rule_current",
        "confirmed_fill_by_price",
        "failed_to_close_at_limit",
        "path_ambiguous",
        "in_current_static_d_pool",
    ):
        frame[column] = bool_series(frame[column])
    frame["account_return"] = pd.to_numeric(frame["account_return"], errors="coerce")
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def signal_frame(ledger: pd.DataFrame) -> pd.DataFrame:
    signals = ledger[ledger["signal_rule_current"]].copy()
    if not signals["eligible_signal_hhmm"].lt(1455).all():
        raise RuntimeError("事件账本包含14:55及之后才生成的无效委托信号")
    if not signals["execution_status"].eq("OK").all():
        counts = signals["execution_status"].value_counts(dropna=False).to_dict()
        raise RuntimeError(f"D路径信号退出结果不完整：{counts}")
    expected = {
        "signal": EXPECTED_SIGNAL_COUNT,
        "confirmed": EXPECTED_CONFIRMED_FILL_COUNT,
        "unknown": EXPECTED_QUEUE_UNKNOWN_COUNT,
        "failed_close": EXPECTED_FAILED_CLOSE_SIGNAL_COUNT,
        "static_overlap": EXPECTED_STATIC_OVERLAP_COUNT,
    }
    actual = {
        "signal": int(len(signals)),
        "confirmed": int(signals["confirmed_fill_by_price"].sum()),
        "unknown": int((~signals["confirmed_fill_by_price"]).sum()),
        "failed_close": int(signals["failed_to_close_at_limit"].sum()),
        "static_overlap": int(signals["in_current_static_d_pool"].sum()),
    }
    if actual != expected:
        raise RuntimeError(f"D一分钟路径冻结结果漂移：expected={expected} actual={actual}")
    return signals


def max_consecutive_losses(values: np.ndarray) -> int:
    maximum = current = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def event_metrics(frame: pd.DataFrame, *, seed: int = 20260822) -> dict[str, Any]:
    sample = frame.sort_values(["trade_date", "ts_code"]).copy()
    values = pd.to_numeric(sample["account_return"], errors="raise").to_numpy(float)
    if len(values) == 0:
        return {
            "sample_count": 0,
            "trading_day_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "sum_account_return": 0.0,
            "diagnostic_event_stream_multiple": 1.0,
            "diagnostic_event_stream_max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
            "explosion_count_gte_10pct": 0,
            "explosion_rate_gte_10pct": 0.0,
            "big_loss_count_lte_minus_5pct": 0,
            "big_loss_rate_lte_minus_5pct": 0.0,
            "equal_day_mean_return": 0.0,
            "equal_day_mean_bootstrap_95_lower": 0.0,
            "equal_day_mean_bootstrap_95_upper": 0.0,
        }
    positive = values[values > 0]
    negative = values[values < 0]
    compound = mechanical_compound(values)
    daily_means = sample.groupby("trade_date")["account_return"].mean().to_numpy(float)
    rng = np.random.default_rng(seed)
    indexes = rng.integers(0, len(daily_means), size=(5000, len(daily_means)))
    boot = daily_means[indexes].mean(axis=1)
    low, high = np.quantile(boot, [0.025, 0.975])
    return {
        "sample_count": int(len(values)),
        "trading_day_count": int(len(daily_means)),
        "win_rate": float((values > 0).mean()),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "sum_account_return": float(values.sum()),
        "diagnostic_event_stream_multiple": float(compound.equity_multiple),
        "diagnostic_event_stream_max_drawdown": float(compound.max_drawdown),
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(values),
        "explosion_count_gte_10pct": int((values >= 0.10).sum()),
        "explosion_rate_gte_10pct": float((values >= 0.10).mean()),
        "big_loss_count_lte_minus_5pct": int((values <= -0.05).sum()),
        "big_loss_rate_lte_minus_5pct": float((values <= -0.05).mean()),
        "equal_day_mean_return": float(daily_means.mean()),
        "equal_day_mean_bootstrap_95_lower": float(low),
        "equal_day_mean_bootstrap_95_upper": float(high),
    }


def add_research_buckets(signals: pd.DataFrame) -> pd.DataFrame:
    frame = signals.copy()
    frame["signal_time_bucket"] = pd.cut(
        frame["eligible_signal_hhmm"],
        bins=[1399, 1414, 1429, 1444, 1454],
        labels=["14:00-14:14", "14:15-14:29", "14:30-14:44", "14:45-14:54"],
    ).astype(str)
    frame["fill_evidence"] = np.where(
        frame["confirmed_fill_by_price"], "PRICE_CONFIRMED", "QUEUE_UNKNOWN"
    )
    frame["close_result"] = np.where(
        frame["failed_to_close_at_limit"], "FAILED_CLOSE", "CLOSED_AT_LIMIT"
    )
    frame["first_before_1400"] = frame["first_seal_hhmm"].lt(1400)
    return frame


def group_metrics(signals: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        ("成交证据_信号后可知", "fill_evidence"),
        ("最终收盘结果_未来字段不可选股", "close_result"),
        ("信号时炸板次数", "open_times_at_signal"),
        ("首次封板时段", "first_time_bucket"),
        ("第一次可交易回封时段", "signal_time_bucket"),
        ("首次封板是否早于1400", "first_before_1400"),
    ]
    rows: list[dict[str, Any]] = []
    for dimension, column in dimensions:
        for group, sample in signals.groupby(column, dropna=False, sort=True):
            rows.append(
                {
                    "scope": "event_population_diagnostic_not_standalone_strategy",
                    "dimension": dimension,
                    "feature_column": column,
                    "group": str(group),
                    **event_metrics(sample, seed=20260822 + len(rows)),
                }
            )
    return pd.DataFrame(rows)


def candidate_rule_diagnostics(signals: pd.DataFrame) -> pd.DataFrame:
    rules: list[tuple[str, str, Callable[[pd.DataFrame], pd.Series]]] = [
        ("path_universe", "全部14:00~14:54第一次可交易回封", lambda x: pd.Series(True, index=x.index)),
        ("first_before_1400", "首次封板早于14:00", lambda x: x["first_seal_hhmm"].lt(1400)),
        ("signal_before_1430", "第一次可交易回封早于14:30", lambda x: x["eligible_signal_hhmm"].lt(1430)),
        ("signal_before_1445", "第一次可交易回封早于14:45", lambda x: x["eligible_signal_hhmm"].lt(1445)),
        ("open_times_2", "信号时累计炸板2次", lambda x: x["open_times_at_signal"].eq(2)),
        ("open_times_3", "信号时累计炸板3次", lambda x: x["open_times_at_signal"].eq(3)),
        (
            "first_before_1400_and_signal_before_1445",
            "首次封板早于14:00且第一次可交易回封早于14:45",
            lambda x: x["first_seal_hhmm"].lt(1400) & x["eligible_signal_hhmm"].lt(1445),
        ),
        (
            "open_times_3_and_signal_before_1445",
            "信号时炸板3次且第一次可交易回封早于14:45",
            lambda x: x["open_times_at_signal"].eq(3) & x["eligible_signal_hhmm"].lt(1445),
        ),
    ]
    scopes: list[tuple[str, Callable[[pd.DataFrame], pd.Series]]] = [
        ("full_24m", lambda x: x["trade_date"].between(START, END)),
        ("first_12m", lambda x: x["trade_date"].between(START, FIRST_12M_END)),
        ("second_12m", lambda x: x["trade_date"].between(SECOND_12M_START, END)),
        (
            "exclude_20240926_20240927",
            lambda x: ~x["trade_date"].isin(OUTLIER_DATES),
        ),
    ]
    rows: list[dict[str, Any]] = []
    for rule, description, predicate in rules:
        rule_mask = predicate(signals).fillna(False)
        for scope, scope_predicate in scopes:
            sample = signals[rule_mask & scope_predicate(signals).fillna(False)]
            rows.append(
                {
                    "rule": rule,
                    "description": description,
                    "scope": scope,
                    "uses_only_signal_time_known_fields": True,
                    "formal_d_compound_certifiable": False,
                    "acde_replacement_certifiable": False,
                    **event_metrics(sample, seed=20261822 + len(rows)),
                }
            )
    return pd.DataFrame(rows)


def compare_frequencies(
    one_minute: pd.DataFrame, five_minute_path: Path
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not five_minute_path.exists():
        return pd.DataFrame(), {"available": False, "path": str(five_minute_path)}
    five = pd.read_csv(
        five_minute_path,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    five["trade_date"] = date_text(five["trade_date"])
    five_signal = bool_series(five["signal_rule_current"]) & pd.to_numeric(
        five["eligible_signal_hhmm"], errors="coerce"
    ).lt(1455)
    left = one_minute[["trade_date", "ts_code", "signal_rule_current"]].copy()
    right = five[["trade_date", "ts_code"]].copy()
    right["five_minute_signal_before_cancel"] = five_signal.to_numpy()
    merged = left.merge(right, on=["trade_date", "ts_code"], validate="one_to_one")
    one = bool_series(merged["signal_rule_current"])
    five_valid = bool_series(merged["five_minute_signal_before_cancel"])
    both = int((one & five_valid).sum())
    only_one = int((one & ~five_valid).sum())
    only_five = int((~one & five_valid).sum())
    neither = int((~one & ~five_valid).sum())
    union = both + only_one + only_five
    summary = {
        "available": True,
        "path": str(five_minute_path.relative_to(ROOT)),
        "sha256": sha256(five_minute_path),
        "one_minute_signal_count": int(one.sum()),
        "five_minute_signal_count_before_1455": int(five_valid.sum()),
        "both_signal_count": both,
        "one_minute_only_count": only_one,
        "five_minute_only_count": only_five,
        "neither_count": neither,
        "jaccard_similarity": float(both / union) if union else 1.0,
        "interpretation": "五分钟bar内顺序歧义导致信号身份不稳定，只能作预检。",
    }
    comparison = pd.DataFrame(
        [
            {"one_minute_signal": False, "five_minute_signal": False, "count": neither},
            {"one_minute_signal": True, "five_minute_signal": False, "count": only_one},
            {"one_minute_signal": False, "five_minute_signal": True, "count": only_five},
            {"one_minute_signal": True, "five_minute_signal": True, "count": both},
        ]
    )
    return comparison, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究策略D一分钟事件路径和盈亏结构")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument(
        "--five-minute-ledger", type=Path, default=DEFAULT_FIVE_MINUTE_LEDGER
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ledger_path = args.ledger if args.ledger.is_absolute() else ROOT / args.ledger
    five_path = (
        args.five_minute_ledger
        if args.five_minute_ledger.is_absolute()
        else ROOT / args.five_minute_ledger
    )
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger = load_one_minute_ledger(ledger_path)
    signals = add_research_buckets(signal_frame(ledger))
    groups = group_metrics(signals)
    candidates = candidate_rule_diagnostics(signals)
    frequency_table, frequency_summary = compare_frequencies(ledger, five_path)

    overall = event_metrics(signals)
    confirmed = event_metrics(signals[signals["confirmed_fill_by_price"]])
    failed_close = event_metrics(signals[signals["failed_to_close_at_limit"]])
    without_outlier_dates = event_metrics(
        signals[~signals["trade_date"].isin(OUTLIER_DATES)]
    )
    explosion_by_date = (
        signals.assign(explosion=signals["account_return"].ge(0.10))
        .groupby("trade_date", as_index=False)
        .agg(signal_count=("ts_code", "size"), explosion_count=("explosion", "sum"))
        .sort_values(["explosion_count", "signal_count"], ascending=False)
    )
    top_two_explosions = int(
        explosion_by_date[
            explosion_by_date["trade_date"].isin(OUTLIER_DATES)
        ]["explosion_count"].sum()
    )
    leader = candidates[
        candidates["rule"].eq("open_times_3_and_signal_before_1445")
    ].set_index("scope")

    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "research_protocol": STRICT_DISCOVERY,
        "strategy": "D",
        "window": f"{START}~{END}",
        "formal_rule_modified": False,
        "release_eligible": False,
        "frozen_formal_baseline": {
            "d_trade_count": 39,
            "d_equity_multiple": 2.0261239235922566,
            "acde_trade_count": 132,
            "acde_equity_multiple": 327.72671897548867,
            "acde_leg_counts": {"D": 22, "A": 44, "E": 49, "C": 17},
            "priority": "D>A>E>C",
            "position_pct": 0.825,
            "d_fill_stress": 0.80,
            "fees_slippage_limit_rules_t1_unchanged": True,
        },
        "input_audit": {
            "ledger_path": str(ledger_path.relative_to(ROOT)),
            "ledger_sha256": sha256(ledger_path),
            "target_count": int(len(ledger)),
            "duplicate_key_count": int(ledger.duplicated(["trade_date", "ts_code"]).sum()),
            "source": EXPECTED_SOURCE,
            "bars_per_target": EXPECTED_BAR_COUNT,
            "one_minute_coverage_complete": True,
            "queue_depth_coverage_complete": False,
        },
        "path_replay": {
            "signal_window": "14:00<=first_eligible_reseal<14:55",
            "signal_count": int(len(signals)),
            "signal_day_count": int(signals["trade_date"].nunique()),
            "confirmed_fill_by_price_count": int(signals["confirmed_fill_by_price"].sum()),
            "queue_unknown_cancel_1455_count": int((~signals["confirmed_fill_by_price"]).sum()),
            "failed_close_signal_count": int(signals["failed_to_close_at_limit"].sum()),
            "ambiguous_signal_count": int(signals["path_ambiguous"].sum()),
            "static_closing_pool_overlap_count": int(signals["in_current_static_d_pool"].sum()),
            "open_times_at_signal_counts": {
                str(int(key)): int(value)
                for key, value in signals["open_times_at_signal"].value_counts().sort_index().items()
            },
        },
        "frequency_comparison": frequency_summary,
        "outcome_diagnostics": {
            "scope_warning": (
                "同日多事件互斥，缺少盘中排名；下列复利和回撤仅为按日期+代码串接的"
                "事件流诊断，不是D独立策略或ACDE组合结果。"
            ),
            "all_path_signals": overall,
            "price_confirmed_fill_only": confirmed,
            "failed_close_after_signal": failed_close,
            "excluding_20240926_20240927": without_outlier_dates,
        },
        "explosion_concentration": {
            "explosion_count_gte_10pct": int(overall["explosion_count_gte_10pct"]),
            "explosions_on_20240926_20240927": top_two_explosions,
            "share_on_two_dates": float(
                top_two_explosions / overall["explosion_count_gte_10pct"]
            ),
            "mean_return_excluding_two_dates": float(
                without_outlier_dates["avg_account_return"]
            ),
            "interpretation": "爆发主要是日期/市场状态聚集，不是稳定的单票路径特征。",
        },
        "highest_in_sample_path_candidate": {
            "rule": "open_times_3_and_signal_before_1445",
            "description": "信号时炸板3次且第一次可交易回封早于14:45",
            "full_24m": leader.loc["full_24m"].to_dict(),
            "second_12m": leader.loc["second_12m"].to_dict(),
            "excluding_two_outlier_dates": leader.loc[
                "exclude_20240926_20240927"
            ].to_dict(),
            "decision": "DIAGNOSTIC_ONLY_NOT_ROBUST_ENOUGH",
        },
        "formal_decision": "KEEP_CURRENT_D_INTRADAY_CERTIFICATION_INCOMPLETE",
        "formal_decision_reasons": [
            "370个一分钟信号全部至少存在一个分钟内事件先后歧义，炸板次数仍非逐笔精确值。",
            "107个信号始终封板且缺买一历史队列，不能证明成交。",
            "缺少信号时点封单/流通市值，无法重建同日多候选的正式第一名。",
            "40个>=10%爆发中25个集中在2024-09-26和2024-09-27；剔除两日后事件均值转负。",
            "未取得D独立复利和ACDE逐腿替换双门槛认证，正式D不得修改。",
        ],
        "next_required_research": (
            "补齐历史逐笔成交/盘口队列和信号时点候选排名字段；先认证263个价格穿透"
            "事件中的每日第一名，再处理107个始终封板的排队成交。"
        ),
        "limitations": [
            "结果来自最近24个月规则发现窗口，属于样本内STRICT_DISCOVERY。",
            "收益沿用当前D的T+2退出、82.5%仓位乘80%成交压力和费用口径，但没有执行同日唯一候选。",
            "最终收盘成功/失败与信号后价格穿透属于信号后字段，只用于诊断，禁止作为14:00选股条件。",
            "一分钟OHLCV不含逐笔先后和买一排队量，不能认证真实成交概率。",
        ],
    }

    signals.to_csv(output_dir / "one_minute_signal_outcomes.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(output_dir / "path_group_metrics.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(
        output_dir / "candidate_rule_diagnostics.csv", index=False, encoding="utf-8-sig"
    )
    explosion_by_date.to_csv(
        output_dir / "explosion_by_date.csv", index=False, encoding="utf-8-sig"
    )
    if not frequency_table.empty:
        frequency_table.to_csv(
            output_dir / "frequency_comparison.csv", index=False, encoding="utf-8-sig"
        )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
