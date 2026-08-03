#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用Tushare补齐8笔D接力的历史一分钟行情和最终竞价容量代理。

QMT当前实测没有返回历史tick，且一分钟数据只覆盖最近3笔。本脚本改用项目已有
Tushare Token调用``stk_mins``，补齐D旧仓与A/C新候选共16个角色的
09:30~10:30一分钟行情。

Tushare的09:30 bar在单一价格时，成交量可作为09:25最终开盘集合竞价匹配量代理。
它不是09:23实时虚拟盘口，也不含买卖未匹配量；报告和后续容量回放必须保留该限制，
不能把代理数据冒充tick。

本脚本只访问行情接口，不读取账户、不连接QMT、不下单。Tushare分钟接口限频较低，
当前项目Token实测约每小时只能调用1次，因此默认间隔3605秒，并按股票+日期缓存，
支持中断后续跑；权限提升后可显式缩短，但不得低于60秒。

运行：

    py -3.11 scripts\research_strategy_d_relay_tushare_fetch.py --dry-run
    py -3.11 scripts\research_strategy_d_relay_tushare_fetch.py --request-interval 3605
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_d_relay_fetch import (  # noqa: E402
    MIN_ONE_MINUTE_BARS,
    ONE_MINUTE_PATH,
    OUTPUT_DIR,
    complete_one_minute_keys,
    load_existing,
    load_relay_targets,
    merge_and_save,
)


REPORT_PATH = OUTPUT_DIR / "d_relay_tushare_fetch_report.csv"
AUCTION_PROXY_PATH = OUTPUT_DIR / "d_relay_auction_proxy.csv"
MINUTE_FIELDS = "ts_code,trade_time,open,close,high,low,vol,amount"


def minute_window(relay_date: str) -> tuple[str, str]:
    dashed = f"{relay_date[:4]}-{relay_date[4:6]}-{relay_date[6:8]}"
    return f"{dashed} 09:30:00", f"{dashed} 10:30:00"


def normalize_tushare_minute(
    frame: pd.DataFrame | None,
    *,
    signal_date: str,
    relay_date: str,
    role: str,
    ts_code: str,
) -> pd.DataFrame:
    """把stk_mins字段统一成现有D接力一分钟研究格式。"""

    columns = [
        "signal_date",
        "relay_date",
        "role",
        "ts_code",
        "bar_time",
        "hhmm",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "data_source",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    required = {"trade_time", "open", "close", "high", "low", "vol", "amount"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"stk_mins缺少字段：{missing}")
    result = frame.copy()
    result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
    result = result[result["trade_time"].notna()].copy()
    result["signal_date"] = signal_date
    result["relay_date"] = relay_date
    result["role"] = role
    result["ts_code"] = ts_code
    result["bar_time"] = result["trade_time"].dt.strftime("%Y%m%d%H%M%S")
    result["hhmm"] = result["trade_time"].dt.strftime("%H%M")
    result["volume"] = pd.to_numeric(result["vol"], errors="coerce")
    result["data_source"] = "TUSHARE_STK_MINS"
    for field in ("open", "close", "high", "low", "amount"):
        result[field] = pd.to_numeric(result[field], errors="coerce")
    result = result[result["hhmm"].between("0930", "1030")].copy()
    return result[columns].sort_values("bar_time").reset_index(drop=True)


def build_auction_proxy(
    targets: pd.DataFrame,
    one_minute: pd.DataFrame,
) -> pd.DataFrame:
    """提取D腿09:30单一价格bar，形成最终竞价容量代理。"""

    rows: list[dict[str, Any]] = []
    for target in targets.itertuples(index=False):
        group = one_minute[
            one_minute["signal_date"].astype(str).eq(str(target.signal_date))
            & one_minute["role"].astype(str).eq("D")
            & one_minute["hhmm"].astype(str).str.zfill(4).eq("0930")
        ].copy()
        if group.empty:
            continue
        row = group.sort_values("bar_time").iloc[0]
        prices = {
            field: float(pd.to_numeric(row.get(field), errors="coerce"))
            for field in ("open", "close", "high", "low")
        }
        reference = prices["open"]
        price_range = max(prices.values()) - min(prices.values())
        single_price = bool(reference > 0 and price_range <= 0.011)
        amount = float(pd.to_numeric(row.get("amount"), errors="coerce"))
        raw_volume = float(pd.to_numeric(row.get("volume"), errors="coerce"))
        # QMT一分钟volume常按“手”，Tushare文档则按“股”。最终竞价成交额单位
        # 都是元，因此统一用amount/单一成交价反推股数，避免容量误差100倍。
        volume = amount / reference if reference > 0 and amount > 0 else 0.0
        raw_volume_unit_ratio = volume / raw_volume if raw_volume > 0 else float("nan")
        source_value = row.get("data_source", "")
        source_name = (
            str(source_value)
            if pd.notna(source_value) and str(source_value).strip()
            else "UNKNOWN_1M"
        )
        rows.append(
            {
                "signal_date": target.signal_date,
                "relay_date": target.relay_date,
                "d_ts_code": target.d_ts_code,
                "d_name": target.d_name,
                "auction_reference_price": reference,
                "matched_qty": volume,
                "matched_amount": amount,
                "raw_bar_volume": raw_volume,
                "shares_to_raw_volume_ratio": raw_volume_unit_ratio,
                "bar_open": prices["open"],
                "bar_close": prices["close"],
                "bar_high": prices["high"],
                "bar_low": prices["low"],
                "price_range": price_range,
                "single_price_proxy": single_price,
                "unmatched_volume_available": False,
                "data_source": source_name,
                "proxy_note": "09:30单一价格bar作为最终开盘竞价匹配量代理；不是09:23盘口",
            }
        )
    return pd.DataFrame(rows)


def complete_auction_proxy_keys(frame: pd.DataFrame) -> set[str]:
    required = {
        "signal_date",
        "auction_reference_price",
        "matched_qty",
        "single_price_proxy",
    }
    if frame.empty or not required.issubset(frame.columns):
        return set()
    price = pd.to_numeric(frame["auction_reference_price"], errors="coerce")
    quantity = pd.to_numeric(frame["matched_qty"], errors="coerce")
    single = frame["single_price_proxy"].astype(str).str.lower().isin({"true", "1"})
    valid = frame[price.gt(0) & quantity.gt(0) & single]
    return set(valid["signal_date"].astype(str))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tushare补齐D接力历史分钟行情")
    parser.add_argument("--dry-run", action="store_true", help="只显示待请求的股票日期")
    parser.add_argument("--overwrite", action="store_true", help="忽略已有完整角色重新请求")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前N个唯一股票日期")
    parser.add_argument(
        "--request-interval",
        type=float,
        default=3605.0,
        help="Tushare真实请求间隔秒数；当前Token默认3605秒",
    )
    return parser.parse_args()


def fetch_with_rate_retry(
    source: Any,
    *,
    ts_code: str,
    start_dt: str,
    end_dt: str,
    request_interval: float,
) -> pd.DataFrame:
    """限频错误等待后重试；无权限、字段错误等异常立即上抛。"""

    for attempt in range(2):
        try:
            return source.get_stock_minute_bars(
                ts_code,
                start_dt,
                end_dt,
                freq="1min",
                fields=MINUTE_FIELDS,
            )
        except Exception as exc:  # noqa: BLE001
            if "频率超限" not in str(exc) or attempt == 1:
                raise
            print(f"{ts_code}触发Tushare分钟限频，等待{request_interval:.0f}秒后重试一次")
            time.sleep(request_interval)
    return pd.DataFrame()


def main() -> None:
    args = parse_args()
    if args.request_interval < 60:
        raise ValueError("stk_mins当前权限按分钟限频，请求间隔不得小于60秒")
    targets = load_relay_targets()
    jobs: list[dict[str, str]] = []
    for target in targets.itertuples(index=False):
        for role, ts_code, name in (
            ("D", target.d_ts_code, target.d_name),
            ("NEXT", target.next_ts_code, target.next_name),
        ):
            jobs.append(
                {
                    "signal_date": str(target.signal_date),
                    "relay_date": str(target.relay_date),
                    "strategy_leg": str(target.strategy_leg),
                    "role": role,
                    "ts_code": str(ts_code),
                    "name": str(name),
                }
            )
    unique_requests = list(
        dict.fromkeys((job["ts_code"], job["relay_date"]) for job in jobs)
    )
    if args.limit > 0:
        allowed = set(unique_requests[: args.limit])
        jobs = [job for job in jobs if (job["ts_code"], job["relay_date"]) in allowed]
        unique_requests = unique_requests[: args.limit]
    print(f"D接力角色={len(jobs)}，唯一股票日期请求={len(unique_requests)}")
    for ts_code, relay_date in unique_requests:
        print(relay_date, ts_code)
    if args.dry_run:
        return

    from src.data_source import TushareDataSource

    source = TushareDataSource(PROJECT_ROOT / "config" / "config.json")
    existing = load_existing(ONE_MINUTE_PATH)
    done = set() if args.overwrite else complete_one_minute_keys(existing)
    raw_cache: dict[tuple[str, str], pd.DataFrame] = {}
    errors: dict[tuple[str, str], str] = {}
    request_count = 0
    for request_index, (ts_code, relay_date) in enumerate(unique_requests, 1):
        related = [
            job for job in jobs
            if job["ts_code"] == ts_code and job["relay_date"] == relay_date
        ]
        if related and all(f"{job['signal_date']}|{job['role']}" in done for job in related):
            continue
        start_dt, end_dt = minute_window(relay_date)
        try:
            raw_cache[(ts_code, relay_date)] = fetch_with_rate_retry(
                source,
                ts_code=ts_code,
                start_dt=start_dt,
                end_dt=end_dt,
                request_interval=args.request_interval,
            )
            request_count += 1
            # 每个唯一股票日期成功后立即落盘，避免14次低频请求中途退出时丢掉
            # 已经取得的数据；同票D/NEXT两个角色分别保留，后续门禁仍按16角色检查。
            request_additions = [
                normalize_tushare_minute(
                    raw_cache[(ts_code, relay_date)],
                    signal_date=job["signal_date"],
                    relay_date=job["relay_date"],
                    role=job["role"],
                    ts_code=job["ts_code"],
                )
                for job in related
            ]
            existing = merge_and_save(
                existing,
                [frame for frame in request_additions if not frame.empty],
                ONE_MINUTE_PATH,
            )
            done = complete_one_minute_keys(existing)
        except Exception as exc:  # noqa: BLE001
            errors[(ts_code, relay_date)] = f"{type(exc).__name__}: {exc}"[:500]
            raw_cache[(ts_code, relay_date)] = pd.DataFrame()
        print(f"Tushare请求进度：{request_index}/{len(unique_requests)}")
        if request_index < len(unique_requests):
            time.sleep(args.request_interval)

    additions: list[pd.DataFrame] = []
    report_rows: list[dict[str, Any]] = []
    for job in jobs:
        key = f"{job['signal_date']}|{job['role']}"
        if key in done:
            group = existing[
                existing["signal_date"].astype(str).eq(job["signal_date"])
                & existing["role"].astype(str).eq(job["role"])
            ]
            report_rows.append({**job, "rows": len(group), "status": "SKIPPED_COMPLETE", "error": ""})
            continue
        raw = raw_cache.get((job["ts_code"], job["relay_date"]), pd.DataFrame())
        normalized = normalize_tushare_minute(raw, **{
            field: job[field]
            for field in ("signal_date", "relay_date", "role", "ts_code")
        })
        if not normalized.empty:
            additions.append(normalized)
        status = "COMPLETE" if key in complete_one_minute_keys(normalized) else "INCOMPLETE"
        report_rows.append(
            {
                **job,
                "rows": len(normalized),
                "status": status,
                "error": errors.get((job["ts_code"], job["relay_date"]), ""),
            }
        )

    final_one = merge_and_save(existing, additions, ONE_MINUTE_PATH)
    report = pd.DataFrame(report_rows)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report.to_csv(REPORT_PATH, index=False, encoding="utf-8-sig")
    proxy = build_auction_proxy(targets, final_one)
    proxy.to_csv(AUCTION_PROXY_PATH, index=False, encoding="utf-8-sig")
    expected_roles = {f"{row.signal_date}|{role}" for row in targets.itertuples(index=False) for role in ("D", "NEXT")}
    minute_complete = expected_roles & complete_one_minute_keys(final_one)
    proxy_complete = complete_auction_proxy_keys(proxy)
    print(
        f"Tushare补齐完成：一分钟角色{len(minute_complete)}/{len(expected_roles)}；"
        f"竞价容量代理{len(proxy_complete)}/{len(targets)}；实际请求{request_count}次。"
    )
    print(f"一分钟→{ONE_MINUTE_PATH}")
    print(f"竞价代理→{AUCTION_PROXY_PATH}")
    if minute_complete != expected_roles or len(proxy_complete) != len(targets):
        print("数据仍不完整，禁止实盘认证，请查看：", REPORT_PATH)


if __name__ == "__main__":
    main()
