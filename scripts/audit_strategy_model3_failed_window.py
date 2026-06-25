"""
审计 model=3 安全候选规则唯一失败测试窗口。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

失败窗口：
  rolling_20240813_20250808__test_20250811_20251110

目标：
  - 拆分 model=3 跑输 mode=1 的来源。
  - 区分 L 单笔替换收益差异，以及 L 持仓占用导致错过 mode=1 交易的机会成本。
  - 输出可解释的失败样本，给后续规则收紧提供依据。

输出：
  reports/strategy_model3/failed_window/model3_failed_window_events.csv
  reports/strategy_model3/failed_window/model3_failed_window_summary.csv
  reports/strategy_model3/failed_window/model3_failed_window_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import selected_l2_source, to_numeric  # noqa: E402
from scripts.search_strategy_model3_safe_modes import markdown_table  # noqa: E402


SPLIT_NAME = "rolling_20240813_20250808__test_20250811_20251110"
DETAIL_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_model3"
    / "safe_candidate_validation"
    / f"detail_{SPLIT_NAME}.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "failed_window"


def load_failed_window() -> pd.DataFrame:
    if not DETAIL_PATH.exists():
        raise FileNotFoundError(f"找不到失败窗口明细，请先运行 validate_strategy_model3_safe_candidate_choice.py: {DETAIL_PATH}")
    data = pd.read_csv(DETAIL_PATH, dtype={"date": str}, low_memory=False)
    data["date"] = data["date"].astype(str)
    data["mode1_return"] = to_numeric(data["mode1_return"])
    data["model3_return"] = to_numeric(data["model3_return"])
    data["daily_diff"] = data["model3_return"] - data["mode1_return"]
    return data


def load_l_features() -> pd.DataFrame:
    source = selected_l2_source().copy()
    keep = [
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "theme_name",
        "market_emotion_state_bucket",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
        "market_limit_down_count_bucket",
        "segment_limit_up_count_bucket",
        "segment_limit_down_count_bucket",
        "first_time_detail_bucket",
        "open_times_bucket",
        "theme_limit_count",
        "same_theme_limit_count",
        "l_account_return",
    ]
    keep = [col for col in keep if col in source.columns]
    features = source[keep].rename(columns={
        "trade_date": "date",
        "ts_code": "l_source_ts_code",
        "name": "l_source_name",
        "l_account_return": "l_theory_account_return",
    })
    features["date"] = features["date"].astype(str)
    return features


def build_events(data: pd.DataFrame) -> pd.DataFrame:
    features = load_l_features()
    rows: list[dict[str, Any]] = []
    active_l_code = ""
    active_l_name = ""
    active_l_start = ""

    for _, row in data.iterrows():
        op = str(row.get("model3_op", ""))
        mode1_return = float(row["mode1_return"])
        model3_return = float(row["model3_return"])
        daily_diff = float(row["daily_diff"])
        if op == "L":
            active_l_code = str(row.get("l_ts_code", ""))
            active_l_name = str(row.get("l_name", ""))
            active_l_start = str(row["date"])
            event_type = "L_TRADE"
        elif op.startswith("POSITION_OCCUPIED_BY_L"):
            event_type = "L_OCCUPY_MISSED_MODE1" if abs(mode1_return) > 1e-12 else "L_OCCUPY_NO_MODE1"
        elif abs(daily_diff) > 1e-12:
            event_type = "OTHER_DIFF"
        else:
            event_type = "SAME_AS_MODE1"

        if event_type == "SAME_AS_MODE1":
            continue

        rows.append({
            "date": str(row["date"]),
            "event_type": event_type,
            "mode1_return": mode1_return,
            "model3_return": model3_return,
            "daily_diff": daily_diff,
            "model3_op": op,
            "mode1_operation_status": row.get("mode1_operation_status", ""),
            "l_ts_code": row.get("l_ts_code", active_l_code),
            "l_name": row.get("l_name", active_l_name),
            "active_l_start": active_l_start,
            "active_l_code": active_l_code,
            "active_l_name": active_l_name,
            "l_exit_date": row.get("l_exit_date", ""),
            "l_status": row.get("l_status", ""),
            "conflict_type": row.get("conflict_type", ""),
        })

    events = pd.DataFrame(rows)
    if events.empty:
        return events
    events = events.merge(features, on="date", how="left")
    return events


def summarize(data: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    mode1_multiple = float((1.0 + data["mode1_return"]).prod())
    model3_multiple = float((1.0 + data["model3_return"]).prod())
    rows = [{
        "item": "window_total",
        "count": int(len(data)),
        "mode1_return_sum": float(data["mode1_return"].sum()),
        "model3_return_sum": float(data["model3_return"].sum()),
        "diff_sum": float(data["daily_diff"].sum()),
        "mode1_multiple": mode1_multiple,
        "model3_multiple": model3_multiple,
        "multiple_ratio": model3_multiple / max(mode1_multiple, 1e-12),
    }]
    if not events.empty:
        by_type = (
            events.groupby("event_type", dropna=False)
            .agg(
                count=("date", "count"),
                mode1_return_sum=("mode1_return", "sum"),
                model3_return_sum=("model3_return", "sum"),
                diff_sum=("daily_diff", "sum"),
            )
            .reset_index()
            .rename(columns={"event_type": "item"})
        )
        for _, row in by_type.iterrows():
            rows.append({
                "item": row["item"],
                "count": int(row["count"]),
                "mode1_return_sum": float(row["mode1_return_sum"]),
                "model3_return_sum": float(row["model3_return_sum"]),
                "diff_sum": float(row["diff_sum"]),
                "mode1_multiple": None,
                "model3_multiple": None,
                "multiple_ratio": None,
            })
    return pd.DataFrame(rows)


def write_report(data: pd.DataFrame, events: pd.DataFrame, summary: pd.DataFrame) -> None:
    event_cols = [
        "date",
        "event_type",
        "mode1_return",
        "model3_return",
        "daily_diff",
        "model3_op",
        "active_l_start",
        "active_l_code",
        "active_l_name",
        "l_ts_code",
        "l_name",
        "theme_name",
        "market_emotion_state_bucket",
        "market_chain_count_bucket",
        "first_time_detail_bucket",
        "open_times_bucket",
    ]
    event_cols = [col for col in event_cols if col in events.columns]
    worst_cols = event_cols
    lines = [
        "# model=3 唯一失败窗口审计",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 失败窗口：{SPLIT_NAME}",
        f"- 起止日期：{data['date'].min()} 至 {data['date'].max()}",
        "",
        "## 汇总",
        "",
        markdown_table(summary),
        "",
        "## 关键事件",
        "",
        markdown_table(events[event_cols]) if not events.empty else "无事件。",
        "",
        "## 拖累最大事件",
        "",
        markdown_table(events.sort_values("daily_diff", ascending=True).head(20)[worst_cols]) if not events.empty else "无事件。",
        "",
        "## 初步结论",
        "",
        "- 这段跑输不是因为 L 交易本身大幅亏损，而是 L 持仓占用期间错过了 mode=1 的正收益交易。",
        "- 后续收紧规则不能只看 L 单笔收益，还必须加入“替换后持仓占用机会成本”的验证。",
        "- 如果要接近实盘，model=3 切换前必须判断 mode=1 是否会在 L 持仓期内产生更高优先级计划；不能只看当日是否允许替换。",
    ]
    (OUTPUT_DIR / "model3_failed_window_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_failed_window()
    events = build_events(data)
    summary = summarize(data, events)
    events.to_csv(OUTPUT_DIR / "model3_failed_window_events.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_DIR / "model3_failed_window_summary.csv", index=False, encoding="utf-8-sig")
    write_report(data, events, summary)

    print("model=3 失败窗口审计完成")
    print(summary.to_string(index=False))
    if not events.empty:
        print(events.sort_values("daily_diff").head(10)[[
            "date",
            "event_type",
            "mode1_return",
            "model3_return",
            "daily_diff",
            "active_l_code",
            "active_l_name",
        ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
