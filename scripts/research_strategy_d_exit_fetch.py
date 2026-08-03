# -*- coding: utf-8 -*-
"""采集当前组合中普通D策略退出日的分钟行情。

本脚本只调用QMT ``xtdata`` 行情接口，不创建交易session、不查询账户、也不下单。
它只采集最终组合中 ``strategy_leg=D`` 的T+2退出样本；``D→A/C/E2`` 接力样本
必须继续按T+1集合竞价退出，因此不进入卖出POV研究。

Windows盘后运行：

    py -3.11 scripts\research_strategy_d_exit_fetch.py

只查看待采集样本，不访问QMT：

    py -3.11 scripts\research_strategy_d_exit_fetch.py --dry-run

输出：

    data/processed/research_strategy_d_exit_5m.csv
    data/processed/research_strategy_d_exit_1m_tail.csv
    reports/strategy_d/exit_pov/d_exit_fetch_report.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
DEFAULT_TRADES_PATH = (
    PROJECT_ROOT
    / "reports"
    / "current_portfolio_alignment"
    / "portfolio_trades.csv"
)
DEFAULT_5M_PATH = (
    PROJECT_ROOT / "data" / "processed" / "research_strategy_d_exit_5m.csv"
)
DEFAULT_1M_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "research_strategy_d_exit_1m_tail.csv"
)
DEFAULT_REPORT_PATH = (
    PROJECT_ROOT / "reports" / "strategy_d" / "exit_pov" / "d_exit_fetch_report.csv"
)
FIELDS = ["open", "close", "high", "low", "volume", "amount"]
EXPECTED_5M_BARS = 48
EXPECTED_1M_BARS = 16


def normalize_date(value: Any) -> str:
    """把日期统一为YYYYMMDD。"""

    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def load_d_exit_targets(path: Path) -> pd.DataFrame:
    """只读取当前组合真实执行的普通D样本，排除全部接力D。"""

    if not path.exists():
        raise FileNotFoundError(f"找不到当前组合逐笔账本：{path}")
    trades = pd.read_csv(
        path,
        dtype={"signal_date": str, "exit_date": str, "ts_code": str},
        low_memory=False,
    )
    required = {"status", "strategy_leg", "signal_date", "exit_date", "ts_code"}
    missing = sorted(required - set(trades.columns))
    if missing:
        raise ValueError(f"组合逐笔账本缺少字段：{missing}")
    targets = trades[
        trades["status"].astype(str).eq("EXECUTED")
        & trades["strategy_leg"].astype(str).str.upper().eq("D")
    ].copy()
    targets["signal_date"] = targets["signal_date"].map(normalize_date)
    targets["exit_date"] = targets["exit_date"].map(normalize_date)
    targets["ts_code"] = targets["ts_code"].astype(str).str.strip().str.upper()
    targets["key"] = targets["ts_code"] + "|" + targets["exit_date"]
    invalid = (
        targets["signal_date"].eq("")
        | targets["exit_date"].eq("")
        | targets["ts_code"].eq("")
    )
    if invalid.any():
        raise ValueError("普通D退出目标存在空日期或空股票代码")
    if targets["key"].duplicated().any():
        duplicated = targets.loc[targets["key"].duplicated(False), "key"].tolist()
        raise ValueError(f"普通D退出目标重复：{duplicated}")
    return targets[
        ["signal_date", "exit_date", "ts_code", "name", "key"]
    ].sort_values(["exit_date", "ts_code"]).reset_index(drop=True)


def normalize_bars(
    frame: pd.DataFrame,
    *,
    ts_code: str,
    exit_date: str,
    period: str,
) -> pd.DataFrame:
    """统一QMT分钟K字段，并保留原始bar时间。"""

    columns = [
        "ts_code",
        "exit_date",
        "leg",
        "period",
        "bar_time",
        *FIELDS,
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for timestamp, bar in frame.iterrows():
        row = {
            "ts_code": ts_code,
            "exit_date": exit_date,
            "leg": "D",
            "period": period,
            "bar_time": str(timestamp),
        }
        for field in FIELDS:
            row[field] = bar.get(field, 0.0)
        rows.append(row)
    result = pd.DataFrame(rows, columns=columns)
    for field in FIELDS:
        result[field] = pd.to_numeric(result[field], errors="coerce")
    return result


def load_existing(path: Path) -> pd.DataFrame:
    """读取已采集数据，支持中断后续传。"""

    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(
        path,
        dtype={"ts_code": str, "exit_date": str, "bar_time": str},
        low_memory=False,
    )


def complete_keys(frame: pd.DataFrame, expected_bars: int) -> set[str]:
    """只有bar数量完整的样本才允许跳过重新下载。"""

    if frame.empty or not {"ts_code", "exit_date"}.issubset(frame.columns):
        return set()
    keys = frame["ts_code"].astype(str) + "|" + frame["exit_date"].map(normalize_date)
    counts = keys.value_counts()
    return set(counts[counts == expected_bars].index)


def merge_and_save(current: pd.DataFrame, additions: list[pd.DataFrame], path: Path) -> None:
    """按股票、退出日、bar时间去重保存，避免续跑产生重复bar。"""

    frames = [frame for frame in [current, *additions] if not frame.empty]
    if not frames:
        return
    result = pd.concat(frames, ignore_index=True)
    result["exit_date"] = result["exit_date"].map(normalize_date)
    result = result.drop_duplicates(
        ["ts_code", "exit_date", "bar_time"], keep="last"
    ).sort_values(["exit_date", "ts_code", "bar_time"])
    path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(path, index=False, encoding="utf-8-sig")


def fetch_one(xtdata: Any, ts_code: str, exit_date: str, period: str) -> pd.DataFrame:
    """下载一个标的一个退出日的指定周期行情。"""

    xtdata.download_history_data(
        ts_code,
        period=period,
        start_time=exit_date,
        end_time=exit_date,
    )
    start_time = exit_date if period == "5m" else exit_date + "144500"
    data = xtdata.get_market_data_ex(
        FIELDS,
        [ts_code],
        period=period,
        start_time=start_time,
        end_time=exit_date + "150000",
    )
    return data.get(ts_code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集普通D退出日5分钟及尾盘1分钟行情")
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES_PATH)
    parser.add_argument("--dry-run", action="store_true", help="只打印目标，不访问QMT")
    parser.add_argument("--overwrite", action="store_true", help="忽略已完成样本并重新下载")
    parser.add_argument("--limit", type=int, default=0, help="仅采集前N个样本，0表示全部")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    targets = load_d_exit_targets(args.trades)
    if args.limit > 0:
        targets = targets.head(args.limit).copy()
    print(f"普通D退出样本：{len(targets)}笔（已排除D接力样本）")
    print(targets.to_string(index=False))
    if args.dry_run:
        return

    # 延迟导入，确保--dry-run在没有QMT的Mac环境也能执行。
    from xtquant import xtdata  # type: ignore

    existing_5m = load_existing(DEFAULT_5M_PATH)
    existing_1m = load_existing(DEFAULT_1M_PATH)
    done_5m = set() if args.overwrite else complete_keys(existing_5m, EXPECTED_5M_BARS)
    done_1m = set() if args.overwrite else complete_keys(existing_1m, EXPECTED_1M_BARS)
    additions_5m: list[pd.DataFrame] = []
    additions_1m: list[pd.DataFrame] = []
    report_rows: list[dict[str, Any]] = []

    for index, target in enumerate(targets.itertuples(index=False), 1):
        row: dict[str, Any] = {
            "signal_date": target.signal_date,
            "exit_date": target.exit_date,
            "ts_code": target.ts_code,
            "name": target.name,
            "key": target.key,
            "bars_5m": 0,
            "bars_1m": 0,
            "status_5m": "",
            "status_1m": "",
            "error_5m": "",
            "error_1m": "",
        }
        for period, done, expected, additions, status_key, error_key in (
            ("5m", done_5m, EXPECTED_5M_BARS, additions_5m, "status_5m", "error_5m"),
            ("1m", done_1m, EXPECTED_1M_BARS, additions_1m, "status_1m", "error_1m"),
        ):
            bars_key = "bars_5m" if period == "5m" else "bars_1m"
            if target.key in done:
                row[bars_key] = expected
                row[status_key] = "SKIPPED_COMPLETE"
                continue
            try:
                raw = fetch_one(xtdata, target.ts_code, target.exit_date, period)
                normalized = normalize_bars(
                    raw,
                    ts_code=target.ts_code,
                    exit_date=target.exit_date,
                    period=period,
                )
                row[bars_key] = len(normalized)
                row[status_key] = "COMPLETE" if len(normalized) == expected else "INCOMPLETE"
                if not normalized.empty:
                    additions.append(normalized)
            except Exception as exc:  # noqa: BLE001
                row[status_key] = "FAILED"
                row[error_key] = f"{type(exc).__name__}: {exc}"[:300]
        report_rows.append(row)
        # 每5笔落盘一次，Windows/QMT中途退出后可以从完整样本继续。
        if index % 5 == 0:
            merge_and_save(existing_5m, additions_5m, DEFAULT_5M_PATH)
            merge_and_save(existing_1m, additions_1m, DEFAULT_1M_PATH)
            print(f"采集进度：{index}/{len(targets)}")

    merge_and_save(existing_5m, additions_5m, DEFAULT_5M_PATH)
    merge_and_save(existing_1m, additions_1m, DEFAULT_1M_PATH)
    report = pd.DataFrame(report_rows)
    DEFAULT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(DEFAULT_REPORT_PATH, index=False, encoding="utf-8-sig")

    complete = report[
        report["status_5m"].isin({"COMPLETE", "SKIPPED_COMPLETE"})
        & report["status_1m"].isin({"COMPLETE", "SKIPPED_COMPLETE"})
    ]
    print(
        f"采集完成：完整{len(complete)}/{len(report)}笔；"
        f"5分钟→{DEFAULT_5M_PATH}；1分钟→{DEFAULT_1M_PATH}"
    )
    if len(complete) != len(report):
        print("存在不完整样本，请查看：", DEFAULT_REPORT_PATH)


if __name__ == "__main__":
    sys.exit(main())
