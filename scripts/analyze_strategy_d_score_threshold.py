"""
计算策略D实盘综合分门槛。

用途：
  - 复刻 monitor_strategy_d_intraday.py 的D综合分。
  - 按分数段统计扣成本收益。
  - 给出 min_buy_score 建议，避免低分候选浪费开仓机会。

运行：
  .venv/bin/python scripts/analyze_strategy_d_score_threshold.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
INPUT_PATH = PROJECT_ROOT / "data" / "processed" / "next_day_premium_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d"
BUCKET_REPORT = OUTPUT_DIR / "score_threshold_buckets.csv"
THRESHOLD_REPORT = OUTPUT_DIR / "score_threshold_candidates.csv"
SUMMARY_JSON = OUTPUT_DIR / "score_threshold_summary.json"

ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj", "other"}
MIN_SAMPLE_COUNT = 10
MIN_AVG_NET_RETURN = 0.005


def normalize_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def hhmm(value: object) -> int:
    if pd.isna(value):
        return 0
    raw = int(float(value))
    return raw // 100 if raw > 10000 else raw


def open_times_score(value: object) -> int:
    try:
        open_times = int(float(value))
    except (TypeError, ValueError):
        return 10
    return {2: 40, 1: 30, 3: 10}.get(open_times, 10)


def reseal_time_score(value: object) -> int:
    t = hhmm(value)
    if t < 1000:
        return 40
    if t < 1200:
        return 30
    if t < 1300:
        return 20
    if t < 1400:
        return 15
    if t < 1430:
        return 10
    return 5


def seal_volume_score(shares: object) -> int:
    try:
        value = float(shares)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 500_000:
        return 20
    if value >= 200_000:
        return 15
    if value >= 50_000:
        return 10
    return 5


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1 + returns.fillna(0.0)).cumprod()
    drawdown = equity / equity.cummax() - 1
    return float(drawdown.min())


def summarize(group: pd.DataFrame) -> dict:
    t2 = group["t2_close_net"].dropna()
    t1 = group["t1_open_net"].dropna()
    return {
        "sample_count": int(len(group)),
        "t2_win_rate": float((t2 > 0).mean()) if len(t2) else 0.0,
        "t2_avg_net_return": float(t2.mean()) if len(t2) else 0.0,
        "t2_median_net_return": float(t2.median()) if len(t2) else 0.0,
        "t2_max_drawdown": max_drawdown(t2),
        "t2_max_loss": float(t2.min()) if len(t2) else 0.0,
        "t2_max_profit": float(t2.max()) if len(t2) else 0.0,
        "t1_win_rate": float((t1 > 0).mean()) if len(t1) else 0.0,
        "t1_avg_net_return": float(t1.mean()) if len(t1) else 0.0,
        "t1_median_net_return": float(t1.median()) if len(t1) else 0.0,
    }


def load_d_samples() -> pd.DataFrame:
    data = pd.read_csv(INPUT_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    numeric_columns = [
        "limit_times", "open_times", "last_time", "limit_close", "fd_amount",
        "fill_probability", "next_open", "exit_close", "fee_rate",
    ]
    for column in numeric_columns:
        data[column] = pd.to_numeric(data.get(column), errors="coerce")

    mask = (
        data["limit_times"].eq(1)
        & (~normalize_bool(data["is_st"]))
        & data["market_sentiment_level"].astype(str).eq("strong")
        & data["board_type"].astype(str).eq("multi_open")
        & data["first_time_bucket"].astype(str).isin({"midday", "afternoon", "late"})
        & data["last_time"].ge(140000)
        & data["fill_probability"].ge(0.8)
        & normalize_bool(data["is_fill_score_reliable"])
        & data["market_segment"].astype(str).isin(ALLOWED_SEGMENTS)
        & data["next_open"].notna()
        & data["exit_close"].notna()
        & data["limit_close"].gt(0)
    )
    result = data.loc[mask].copy()
    result["hist_seal_shares"] = result["fd_amount"] / result["limit_close"]
    result["open_score"] = result["open_times"].map(open_times_score)
    result["time_score"] = result["last_time"].map(reseal_time_score)
    result["volume_score"] = result["hist_seal_shares"].map(seal_volume_score)
    result["d_score"] = result["open_score"] + result["time_score"] + result["volume_score"]
    fee = result["fee_rate"].fillna(0.0015)
    result["t1_open_net"] = result["next_open"] / result["limit_close"] - 1 - fee
    result["t2_close_net"] = result["exit_close"] / result["limit_close"] - 1 - fee
    return result.sort_values(["trade_date", "d_score"], ascending=[True, False])


def build_reports(samples: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    bucketed = samples.copy()
    bucketed["score_bucket"] = pd.cut(
        bucketed["d_score"],
        bins=[-1, 39.999, 49.999, 59.999, 69.999, 79.999, 1000],
        labels=["lt_40", "40_50", "50_60", "60_70", "70_80", "80_plus"],
    )
    bucket_rows = []
    for bucket, group in bucketed.groupby("score_bucket", observed=False):
        row = {"score_bucket": str(bucket)}
        row.update(summarize(group))
        bucket_rows.append(row)
    bucket_report = pd.DataFrame(bucket_rows)

    threshold_rows = []
    for min_score in range(40, 85, 5):
        group = samples[samples["d_score"] >= min_score]
        row = {"min_score": min_score}
        row.update(summarize(group))
        row["passes_gate"] = (
            row["sample_count"] >= MIN_SAMPLE_COUNT
            and row["t2_avg_net_return"] >= MIN_AVG_NET_RETURN
        )
        threshold_rows.append(row)
    threshold_report = pd.DataFrame(threshold_rows)

    passed = threshold_report[threshold_report["passes_gate"]].copy()
    recommended = int(passed["min_score"].iloc[0]) if not passed.empty else None
    summary = {
        "input_path": str(INPUT_PATH),
        "sample_start": str(samples["trade_date"].min()) if not samples.empty else "",
        "sample_end": str(samples["trade_date"].max()) if not samples.empty else "",
        "sample_count": int(len(samples)),
        "gate": {
            "min_sample_count": MIN_SAMPLE_COUNT,
            "min_t2_avg_net_return": MIN_AVG_NET_RETURN,
        },
        "recommended_min_buy_score": recommended,
        "note": (
            "当前报告使用历史收盘封单金额折算封单股数，近似复刻实盘买一封单股数；"
            "若样本覆盖日期过短，需要补齐历史 next_day_premium_trades 后再定最终门槛。"
        ),
    }
    return bucket_report, threshold_report, summary


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    samples = load_d_samples()
    bucket_report, threshold_report, summary = build_reports(samples)
    bucket_report.to_csv(BUCKET_REPORT, index=False, encoding="utf-8-sig")
    threshold_report.to_csv(THRESHOLD_REPORT, index=False, encoding="utf-8-sig")
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"D样本: {summary['sample_count']} 笔，{summary['sample_start']} ~ {summary['sample_end']}")
    print(f"分数段报告: {BUCKET_REPORT}")
    print(f"阈值报告: {THRESHOLD_REPORT}")
    print(f"推荐最低分: {summary['recommended_min_buy_score']}")
    print(threshold_report.to_string(index=False))


if __name__ == "__main__":
    main()
