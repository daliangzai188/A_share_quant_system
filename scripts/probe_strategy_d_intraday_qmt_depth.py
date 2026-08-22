#!/usr/bin/env python3
"""只读探测QMT对D完整触板母池的1分钟/tick/五档历史覆盖。

本脚本只调用``xtdata``行情接口：不导入``xttrader``，不读资金账户，
不查持仓，不下单，不撤单。输出只是数据源能力报告，不是策略收益。

Windows/QMT客户端在线时运行：

    cd C:\\A_System
    py -3.11 scripts\\probe_strategy_d_intraday_qmt_depth.py

默认按日期分位取6个沪深目标，同时测试1分钟和tick。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = ROOT / "data/research/strategy_d_intraday/minute_target_manifest.csv"
REPORT_DIR = ROOT / "reports/strategy_d_intraday_research"
DETAIL_PATH = REPORT_DIR / "qmt_depth_probe.csv"
SUMMARY_PATH = REPORT_DIR / "qmt_depth_probe.json"
EXPECTED_TARGET_COUNT = 6848
LOGGER = logging.getLogger("probe_strategy_d_intraday_qmt_depth")
ONE_MINUTE_FIELDS = ["open", "high", "low", "close", "volume", "amount"]
BOOK_FIELD_NAMES = {
    "bidVol",
    "askVol",
    "bidPrice",
    "askPrice",
    "bid_volume",
    "ask_volume",
    "bid_price",
    "ask_price",
}


def load_targets(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str, "target_key": str},
        low_memory=False,
    )
    required = {"trade_date", "ts_code", "target_key", "market_segment"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D分钟目标缺少字段：{missing}")
    frame["trade_date"] = (
        frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    frame = frame.drop_duplicates("target_key", keep="last")
    if len(frame) != EXPECTED_TARGET_COUNT:
        raise RuntimeError(
            f"D分钟目标漂移：expected={EXPECTED_TARGET_COUNT} actual={len(frame)}"
        )
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def default_probe_targets(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    supported = frame[frame["market_segment"].ne("bj")].copy()
    dates = sorted(supported["trade_date"].unique())
    if not dates:
        return supported.head(0)
    if count <= 1:
        selected_dates = [dates[-1]]
    else:
        selected_dates = sorted(
            {
                dates[round(index * (len(dates) - 1) / (count - 1))]
                for index in range(count)
            }
        )
    rows = [
        supported[supported["trade_date"].eq(date)].iloc[0]
        for date in selected_dates
    ]
    return pd.DataFrame(rows).reset_index(drop=True)


def explicit_probe_targets(frame: pd.DataFrame, values: list[str]) -> pd.DataFrame:
    keys = []
    for value in values:
        if "|" not in value:
            raise ValueError(f"--target必须为YYYYMMDD|000001.SZ：{value}")
        date, code = value.split("|", maxsplit=1)
        keys.append(f"{date}|{code}")
    result = frame[frame["target_key"].isin(keys)].copy()
    missing = sorted(set(keys) - set(result["target_key"]))
    if missing:
        raise ValueError(f"以下目标不在冻结清单：{missing}")
    return result.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def frame_metadata(frame: Any) -> dict[str, Any]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return {
            "row_count": 0,
            "first_index": "",
            "last_index": "",
            "columns": [],
            "book_fields_present": [],
        }
    columns = [str(column) for column in frame.columns]
    book_fields = sorted(set(columns) & BOOK_FIELD_NAMES)
    # 兼容bidVol1/bidPrice1等平铺字段。
    book_fields.extend(
        sorted(
            column
            for column in columns
            if column.lower().startswith(("bidvol", "askvol", "bidprice", "askprice"))
            and column not in book_fields
        )
    )
    return {
        "row_count": int(len(frame)),
        "first_index": str(frame.index[0]),
        "last_index": str(frame.index[-1]),
        "columns": columns,
        "book_fields_present": book_fields,
    }


def fetch_period(
    xtdata: Any,
    *,
    ts_code: str,
    trade_date: str,
    period: str,
) -> tuple[dict[str, Any], str]:
    try:
        xtdata.download_history_data(
            ts_code,
            period=period,
            start_time=trade_date,
            end_time=trade_date,
        )
        fields = ONE_MINUTE_FIELDS if period == "1m" else []
        result = xtdata.get_market_data_ex(
            fields,
            [ts_code],
            period=period,
            start_time=trade_date + "093000",
            end_time=trade_date + "150000",
        )
        frame = result.get(ts_code) if isinstance(result, dict) else None
        return frame_metadata(frame), ""
    except Exception as exc:  # 探针要保留每个数据源的完整失败证据
        return frame_metadata(None), f"{type(exc).__name__}:{exc}"[:1000]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读探测D历史QMT分钟/tick/五档覆盖")
    parser.add_argument("--targets", type=Path, default=TARGET_PATH)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="显式目标YYYYMMDD|000001.SZ，可重复传入。",
    )
    parser.add_argument("--skip-tick", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    targets = load_targets(args.targets)
    probe = (
        explicit_probe_targets(targets, args.target)
        if args.target
        else default_probe_targets(targets, max(1, args.count))
    )
    LOGGER.info("QMT历史深度探针目标=%d", len(probe))
    if args.dry_run:
        print(
            probe[
                ["trade_date", "ts_code", "name", "market_segment", "target_key"]
            ].to_string(index=False)
        )
        return 0

    from xtquant import xtdata

    rows: list[dict[str, Any]] = []
    for row in probe.itertuples(index=False):
        minute, minute_error = fetch_period(
            xtdata,
            ts_code=str(row.ts_code),
            trade_date=str(row.trade_date),
            period="1m",
        )
        tick = frame_metadata(None)
        tick_error = "SKIPPED"
        if not args.skip_tick:
            tick, tick_error = fetch_period(
                xtdata,
                ts_code=str(row.ts_code),
                trade_date=str(row.trade_date),
                period="tick",
            )
        rows.append(
            {
                "trade_date": str(row.trade_date),
                "ts_code": str(row.ts_code),
                "name": str(row.name),
                "market_segment": str(row.market_segment),
                "minute_row_count": minute["row_count"],
                "minute_first_index": minute["first_index"],
                "minute_last_index": minute["last_index"],
                "minute_columns": json.dumps(minute["columns"], ensure_ascii=False),
                "minute_error": minute_error,
                "tick_row_count": tick["row_count"],
                "tick_first_index": tick["first_index"],
                "tick_last_index": tick["last_index"],
                "tick_columns": json.dumps(tick["columns"], ensure_ascii=False),
                "tick_book_fields": json.dumps(
                    tick["book_fields_present"], ensure_ascii=False
                ),
                "tick_error": tick_error,
            }
        )
        LOGGER.info(
            "%s %s: 1m=%d tick=%d book=%s",
            row.trade_date,
            row.ts_code,
            minute["row_count"],
            tick["row_count"],
            bool(tick["book_fields_present"]),
        )
    detail = pd.DataFrame(rows)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    detail.to_csv(DETAIL_PATH, index=False, encoding="utf-8-sig")
    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "strategy": "D",
        "probe_only": True,
        "read_only_xtdata": True,
        "account_accessed": False,
        "order_api_accessed": False,
        "target_count": int(len(detail)),
        "one_minute_available_count": int(detail["minute_row_count"].gt(0).sum()),
        "tick_available_count": int(detail["tick_row_count"].gt(0).sum()),
        "historical_book_available_count": int(
            detail["tick_book_fields"].astype(str).ne("[]").sum()
        ),
        "date_results": detail[
            [
                "trade_date",
                "ts_code",
                "minute_row_count",
                "tick_row_count",
                "tick_book_fields",
            ]
        ].to_dict("records"),
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    LOGGER.info("QMT探针报告：%s", SUMMARY_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
