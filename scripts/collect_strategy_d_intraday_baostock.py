#!/usr/bin/env python3
"""为策略D完整首板触板母池回填BaoStock历史5分钟K。

这是免费数据源覆盖预检，不是正式盘中路径或排队成交认证：

* BaoStock只提供5分钟OHLCV，同一bar内触板/炸板/回封顺序不可知；
* 没有历史买一队列深度，始终封板的排队单不能判定成交；
* 北交所不在BaoStock股票代码覆盖内，会明确记为UNSUPPORTED_BJ。

运行：

    python3 scripts/build_strategy_d_intraday_event_ledger.py --targets-only
    python3 scripts/collect_strategy_d_intraday_baostock.py

脚本可断点续传，已成功和已确认无数据的目标不会重复请求。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import time
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = ROOT / "data/research/strategy_d_intraday/minute_target_manifest.csv"
OUTPUT_PATH = ROOT / "data/research/strategy_d_intraday/minute_5m_baostock.csv"
STATUS_PATH = ROOT / "data/research/strategy_d_intraday/baostock_5m_status.csv"
SUMMARY_PATH = ROOT / "reports/strategy_d_intraday_research/baostock_5m_collection.json"
EXPECTED_TARGET_COUNT = 6848
FIELDS = "time,open,high,low,close,volume,amount"
LOGGER = logging.getLogger("collect_strategy_d_intraday_baostock")


def to_baostock_code(ts_code: str) -> str:
    code, exchange = str(ts_code).split(".", maxsplit=1)
    if exchange == "SH":
        return f"sh.{code}"
    if exchange == "SZ":
        return f"sz.{code}"
    return ""


def load_targets(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"缺少D分钟采集目标：{path}；请先运行母池构建脚本。"
        )
    frame = pd.read_csv(
        path, dtype={"trade_date": str, "ts_code": str, "target_key": str}
    )
    required = {"trade_date", "ts_code", "target_key", "market_segment"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D分钟采集目标缺少字段：{missing}")
    frame["trade_date"] = (
        frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    frame = frame.drop_duplicates("target_key", keep="last")
    if len(frame) != EXPECTED_TARGET_COUNT:
        raise RuntimeError(
            "D分钟采集目标漂移："
            f"expected={EXPECTED_TARGET_COUNT} actual={len(frame)}"
        )
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def load_status(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(
            columns=[
                "target_key",
                "trade_date",
                "ts_code",
                "status",
                "bar_count",
                "error_message",
                "updated_at",
            ]
        )
    frame = pd.read_csv(
        path,
        dtype={"target_key": str, "trade_date": str, "ts_code": str},
        low_memory=False,
    )
    return frame.drop_duplicates("target_key", keep="last")


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def append_bars(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(
        path,
        mode="a",
        header=not path.exists() or path.stat().st_size == 0,
        index=False,
        encoding="utf-8-sig",
    )


def compact_output(path: Path) -> int:
    if not path.exists():
        return 0
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str, "bar_time": str},
        low_memory=False,
    )
    if frame.empty:
        return 0
    frame = frame.drop_duplicates(
        ["trade_date", "ts_code", "bar_time"], keep="last"
    ).sort_values(["trade_date", "ts_code", "bar_time"])
    atomic_write_csv(frame, path)
    return int(len(frame))


def fetch_one(bs: Any, *, ts_code: str, trade_date: str) -> tuple[list[dict[str, Any]], str]:
    vendor_code = to_baostock_code(ts_code)
    if not vendor_code:
        return [], "UNSUPPORTED_BJ"
    iso_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    result = bs.query_history_k_data_plus(
        vendor_code,
        FIELDS,
        start_date=iso_date,
        end_date=iso_date,
        frequency="5",
        # 涨停价是真实历史价，盘中K线必须不复权。
        adjustflag="3",
    )
    if str(result.error_code) != "0":
        raise RuntimeError(f"{result.error_code}:{result.error_msg}")
    rows: list[dict[str, Any]] = []
    while result.next():
        raw = result.get_row_data()
        if len(raw) != 7:
            continue
        bar_time, open_, high, low, close, volume, amount = raw
        rows.append(
            {
                "trade_date": trade_date,
                "ts_code": ts_code,
                "bar_time": str(bar_time)[:14],
                "open": pd.to_numeric(open_, errors="coerce"),
                "high": pd.to_numeric(high, errors="coerce"),
                "low": pd.to_numeric(low, errors="coerce"),
                "close": pd.to_numeric(close, errors="coerce"),
                "volume": pd.to_numeric(volume, errors="coerce"),
                "amount": pd.to_numeric(amount, errors="coerce"),
                "source": "BAOSTOCK_5M_UNADJUSTED_APPROXIMATE",
            }
        )
    return rows, "OK" if rows else "EMPTY"


def update_status(
    status: pd.DataFrame,
    *,
    target_key: str,
    trade_date: str,
    ts_code: str,
    result_status: str,
    bar_count: int,
    error_message: str = "",
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "target_key": target_key,
                "trade_date": trade_date,
                "ts_code": ts_code,
                "status": result_status,
                "bar_count": int(bar_count),
                "error_message": error_message[:500],
                "updated_at": pd.Timestamp.now(
                    tz="Asia/Ho_Chi_Minh"
                ).isoformat(),
            }
        ]
    )
    status = pd.concat([status, new_row], ignore_index=True)
    return status.drop_duplicates("target_key", keep="last")


def summary_payload(
    targets: pd.DataFrame,
    status: pd.DataFrame,
    *,
    output_rows: int,
    attempted_this_run: int,
) -> dict[str, Any]:
    counts = {
        str(key): int(value)
        for key, value in status["status"].value_counts().to_dict().items()
    }
    terminal = status["status"].astype(str).isin({"OK", "EMPTY", "UNSUPPORTED_BJ"})
    return {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "strategy": "D",
        "source": "BAOSTOCK_5M_UNADJUSTED_APPROXIMATE",
        "formal_rule_modified": False,
        "certification_eligible": False,
        "target_count": int(len(targets)),
        "attempted_this_run": int(attempted_this_run),
        "terminal_target_count": int(terminal.sum()),
        "pending_target_count": int(len(targets) - terminal.sum()),
        "status_counts": counts,
        "output_bar_count": int(output_rows),
        "limitations": [
            "5分钟OHLCV无法确定同一bar内触板、炸板、回封的先后顺序。",
            "没有历史tick/买一队列深度，不能认证始终封板期间的排队成交。",
            "BaoStock不覆盖北交所，北交所目标必须由其他数据源补齐。",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填D首板触板母池BaoStock 5分钟K")
    parser.add_argument("--targets", type=Path, default=TARGET_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--status", type=Path, default=STATUS_PATH)
    parser.add_argument("--limit", type=int, default=0, help="本次最多请求目标数；0=全部。")
    parser.add_argument("--save-every", type=int, default=25)
    parser.add_argument("--request-delay", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    targets = load_targets(args.targets)
    status = load_status(args.status)
    terminal_keys = set(
        status.loc[
            status["status"].astype(str).isin({"OK", "EMPTY", "UNSUPPORTED_BJ"}),
            "target_key",
        ].astype(str)
    )
    pending = targets[~targets["target_key"].isin(terminal_keys)].copy()
    if args.limit > 0:
        pending = pending.head(args.limit)
    LOGGER.info(
        "D 5分钟目标=%d，已完成=%d，本次待请求=%d",
        len(targets),
        len(terminal_keys),
        len(pending),
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "target_count": int(len(targets)),
                    "completed_count": int(len(terminal_keys)),
                    "pending_this_run": int(len(pending)),
                    "formal_rule_modified": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock登录失败：{login.error_code}:{login.error_msg}")
    attempted = 0
    buffer: list[dict[str, Any]] = []
    try:
        for row in pending.itertuples(index=False):
            attempted += 1
            try:
                bars, result_status = fetch_one(
                    bs, ts_code=str(row.ts_code), trade_date=str(row.trade_date)
                )
                buffer.extend(bars)
                status = update_status(
                    status,
                    target_key=str(row.target_key),
                    trade_date=str(row.trade_date),
                    ts_code=str(row.ts_code),
                    result_status=result_status,
                    bar_count=len(bars),
                )
            except Exception as exc:  # 保留失败后续传，不因单股中断母池
                LOGGER.warning(
                    "采集失败 %s %s: %s", row.trade_date, row.ts_code, exc
                )
                status = update_status(
                    status,
                    target_key=str(row.target_key),
                    trade_date=str(row.trade_date),
                    ts_code=str(row.ts_code),
                    result_status="ERROR",
                    bar_count=0,
                    error_message=f"{type(exc).__name__}:{exc}",
                )
            if attempted % max(1, args.save_every) == 0:
                append_bars(buffer, args.output)
                buffer.clear()
                atomic_write_csv(status, args.status)
                LOGGER.info("D 5分钟进度 %d/%d", attempted, len(pending))
            if args.request_delay > 0:
                time.sleep(args.request_delay)
    finally:
        append_bars(buffer, args.output)
        atomic_write_csv(status, args.status)
        bs.logout()

    output_rows = compact_output(args.output)
    summary = summary_payload(
        targets, status, output_rows=output_rows, attempted_this_run=attempted
    )
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
