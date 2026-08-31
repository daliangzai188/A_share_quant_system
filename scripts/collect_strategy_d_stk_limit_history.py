#!/usr/bin/env python3
"""补齐策略D指定研究窗口的Tushare官方涨跌停价。

一分钟权限负责盘中路径，但生成全市场首板触板母池还需要每个交易日的官方
``stk_limit``。本脚本只补缺失日期，逐日原子落盘并支持中断续跑；不修改正式D。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


START = "20240630"
END = "20260630"
CALENDAR_PATH = ROOT / "data/raw/trade_calendar.csv"
OUTPUT_DIR = ROOT / "data/raw/stk_limit_history"
REPORT_PATH = (
    ROOT / "reports/strategy_d_intraday_research/stk_limit_window_collection.json"
)
REQUIRED_COLUMNS = frozenset(
    {"trade_date", "ts_code", "pre_close", "up_limit", "down_limit"}
)
FIELDS = "trade_date,ts_code,pre_close,up_limit,down_limit"
LOGGER = logging.getLogger("collect_strategy_d_stk_limit_history")


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def load_window_open_dates(
    path: Path = CALENDAR_PATH,
    *,
    start: str = START,
    end: str = END,
) -> list[str]:
    frame = pd.read_csv(path, dtype={"cal_date": str}, low_memory=False)
    missing = sorted({"cal_date", "is_open"} - set(frame.columns))
    if missing:
        raise ValueError(f"交易日历缺少字段：{missing}")
    dates = date_text(
        frame.loc[
            pd.to_numeric(frame["is_open"], errors="coerce").eq(1), "cal_date"
        ]
    )
    result = sorted(date for date in dates.unique() if str(start) < date <= str(end))
    if not result:
        raise RuntimeError("指定D研究窗口没有交易日")
    return result


def normalize_stk_limit(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError(f"stk_limit返回空数据：{trade_date}")
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"stk_limit缺少字段：{missing}")
    result = frame[list(sorted(REQUIRED_COLUMNS))].copy()
    result["trade_date"] = date_text(result["trade_date"])
    result["ts_code"] = result["ts_code"].astype(str).str.strip()
    if set(result["trade_date"]) != {trade_date}:
        raise ValueError(f"stk_limit混入非目标日期：{trade_date}")
    if result["ts_code"].eq("").any() or result["ts_code"].duplicated().any():
        raise ValueError(f"stk_limit股票代码为空或重复：{trade_date}")
    # 旧研究文件的pre_close未请求而为空，但D母池只使用交易所up_limit；
    # 不能因此把已有官方涨跌停价误判为整日无效。
    result["pre_close"] = pd.to_numeric(result["pre_close"], errors="coerce")
    result["up_limit"] = pd.to_numeric(result["up_limit"], errors="coerce")
    if result["up_limit"].isna().any() or result["up_limit"].le(0).any():
        raise ValueError(f"stk_limit字段up_limit缺失或非正：{trade_date}")
    result["down_limit"] = pd.to_numeric(result["down_limit"], errors="coerce")
    if result["down_limit"].isna().any() or result["down_limit"].lt(0).any():
        raise ValueError(f"stk_limit字段down_limit缺失或为负：{trade_date}")
    return result.sort_values("ts_code").reset_index(drop=True)


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def audit_existing(
    dates: list[str], *, output_dir: Path = OUTPUT_DIR
) -> tuple[list[str], list[dict[str, Any]]]:
    complete: list[str] = []
    invalid: list[dict[str, Any]] = []
    for trade_date in dates:
        path = output_dir / f"{trade_date}.csv"
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            frame = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})
            normalized = normalize_stk_limit(frame, trade_date=trade_date)
            complete.append(trade_date)
            if len(normalized) < 100:
                invalid.append(
                    {"trade_date": trade_date, "error": f"股票数异常偏少：{len(normalized)}"}
                )
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            invalid.append({"trade_date": trade_date, "error": str(exc)})
    invalid_dates = {item["trade_date"] for item in invalid}
    return [date for date in complete if date not in invalid_dates], invalid


def collect_missing(
    source: Any,
    dates: list[str],
    *,
    output_dir: Path = OUTPUT_DIR,
    overwrite: bool = False,
    limit_days: int | None = None,
    request_interval_seconds: float = 0.15,
    sleep_fn: Callable[[float], None] = time.sleep,
    start: str = START,
    end: str = END,
) -> dict[str, Any]:
    existing, invalid_before = audit_existing(dates, output_dir=output_dir)
    existing_set = set(existing)
    pending = dates if overwrite else [date for date in dates if date not in existing_set]
    if limit_days is not None:
        pending = pending[: max(int(limit_days), 0)]
    saved: list[str] = []
    errors: list[dict[str, str]] = []
    for index, trade_date in enumerate(pending, start=1):
        try:
            raw = source.get_stk_limit(trade_date=trade_date, fields=FIELDS)
            normalized = normalize_stk_limit(raw, trade_date=trade_date)
            atomic_write_csv(normalized, output_dir / f"{trade_date}.csv")
            saved.append(trade_date)
            if index == 1 or index % 25 == 0 or index == len(pending):
                LOGGER.info(
                    "stk_limit补齐进度：%d/%d，date=%s rows=%d",
                    index,
                    len(pending),
                    trade_date,
                    len(normalized),
                )
        except Exception as exc:  # noqa: BLE001 - 保留日期后继续，最终统一失败
            errors.append({"trade_date": trade_date, "error": f"{type(exc).__name__}:{exc}"})
        if request_interval_seconds > 0 and index < len(pending):
            sleep_fn(request_interval_seconds)
    complete, invalid_after = audit_existing(dates, output_dir=output_dir)
    complete_set = set(complete)
    missing = [date for date in dates if date not in complete_set]
    return {
        "schema_version": 1,
        "strategy": "D",
        "window": f"{start}~{end}",
        "formal_rule_modified": False,
        "source": "TUSHARE_STK_LIMIT",
        "expected_open_day_count": len(dates),
        "complete_day_count": len(complete),
        "saved_this_run_count": len(saved),
        "missing_day_count": len(missing),
        "missing_dates": missing,
        "invalid_before": invalid_before,
        "invalid_after": invalid_after,
        "request_errors": errors,
        "passed": not missing and not invalid_after and not errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="补齐D指定窗口官方stk_limit")
    parser.add_argument("--calendar", type=Path, default=CALENDAR_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--limit-days", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--request-interval-seconds", type=float, default=0.15)
    parser.add_argument("--start", default=START, help="自然日左边界，开区间")
    parser.add_argument("--end", default=END, help="自然日右边界，闭区间")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dt_start = pd.to_datetime(str(args.start), format="%Y%m%d")
    dt_end = pd.to_datetime(str(args.end), format="%Y%m%d")
    if dt_start >= dt_end:
        raise ValueError("stk_limit采集窗口必须满足start < end")
    dates = load_window_open_dates(
        args.calendar,
        start=str(args.start),
        end=str(args.end),
    )
    complete, invalid = audit_existing(dates, output_dir=args.output_dir)
    if args.dry_run:
        report = {
            "schema_version": 1,
            "strategy": "D",
            "window": f"{args.start}~{args.end}",
            "formal_rule_modified": False,
            "source": "TUSHARE_STK_LIMIT",
            "expected_open_day_count": len(dates),
            "complete_day_count": len(complete),
            "pending_day_count": len(dates) - len(complete),
            "invalid": invalid,
            "dry_run": True,
            "passed": len(complete) == len(dates) and not invalid,
        }
    else:
        # 延迟导入：纯清洗/断点续传函数和--dry-run不应强制要求当前解释器已安装
        # tushare；真正发起官方接口请求时再加载数据源并正常暴露依赖缺失错误。
        from src.data_source import TushareDataSource

        source = TushareDataSource()
        report = collect_missing(
            source,
            dates,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
            limit_days=args.limit_days,
            request_interval_seconds=args.request_interval_seconds,
            start=str(args.start),
            end=str(args.end),
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
