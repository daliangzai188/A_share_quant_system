# -*- coding: utf-8 -*-
"""采集当前组合8笔D接力的竞价tick和开盘后分钟行情。

本脚本只调用QMT ``xtdata`` 行情接口，不创建交易session、不查询账户、不下单。
研究对象严格限定为当前132笔组合中的 ``D→A/C/E2`` 接力；普通D不进入本脚本。

采集范围：

* D旧仓：接力日09:15~10:30 tick及09:30~10:30一分钟行情；
* A/C/E2新仓：同一时段tick及一分钟行情；
* tick用于还原09:23虚拟开盘参考价、匹配量、未匹配量和五档盘口；
* 一分钟行情用于后续回放09:30后的“卖D→确认资金→买新仓”成对POV。

Windows盘后运行：

    py -3.11 scripts\research_strategy_d_relay_fetch.py --dry-run
    py -3.11 scripts\research_strategy_d_relay_fetch.py

输出：

    data/processed/research_strategy_d_relay_tick.csv
    data/processed/research_strategy_d_relay_1m.csv
    reports/strategy_d/relay_capacity/d_relay_targets.csv
    reports/strategy_d/relay_capacity/d_relay_fetch_report.csv
"""
from __future__ import annotations

import argparse
import bisect
import datetime
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
PORTFOLIO_PATH = (
    PROJECT_ROOT / "reports" / "current_portfolio_alignment" / "portfolio_trades.csv"
)
D_TRADES_PATH = PROJECT_ROOT / "reports" / "strategy_d" / "d_trades.csv"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
TICK_PATH = PROJECT_ROOT / "data" / "processed" / "research_strategy_d_relay_tick.csv"
ONE_MINUTE_PATH = (
    PROJECT_ROOT / "data" / "processed" / "research_strategy_d_relay_1m.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d" / "relay_capacity"
TARGETS_PATH = OUTPUT_DIR / "d_relay_targets.csv"
REPORT_PATH = OUTPUT_DIR / "d_relay_fetch_report.csv"
# 2026-08-07 D接力已全关（见 combined_live_engine 顶部「腿序与接力口径」），
# 组合里不再产生 D→A/C/E2，本研究工具随之没有研究对象。
# 历史值：A/C被裁口径 8笔；仅修A/C口径 9笔；再修衔接日D后 4笔。
# 保留脚本本身：若将来重新开启接力，把本常量改回实际笔数即可继续使用。
EXPECTED_RELAY_COUNT = 0
MIN_ONE_MINUTE_BARS = 60
RELAY_LEGS = {"D→A", "D→C", "D→E2"}
ONE_MINUTE_FIELDS = ["open", "close", "high", "low", "volume", "amount"]
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def normalize_date(value: Any) -> str:
    """统一日期为YYYYMMDD。"""

    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def extract_hhmm(value: Any) -> str:
    """尽量从QMT时间索引提取HHMM，仅用于判断采集覆盖范围。"""

    # DataFrame混合数值列时，13位毫秒时间戳可能被转换成float并显示为“.0”；
    # 必须在字符串清洗前按数值识别。
    if isinstance(value, (int, float)) and 1_000_000_000_000 <= float(value) < 10_000_000_000_000:
        timestamp = datetime.datetime.fromtimestamp(
            float(value) / 1000.0,
            tz=CHINA_TZ,
        )
        return timestamp.strftime("%H%M")
    digits = "".join(char for char in str(value) if char.isdigit())
    if len(digits) >= 12 and digits[:2] in {"19", "20"}:
        return digits[8:12]
    # 部分QMT版本把time保留为13位毫秒时间戳，必须按A股时区转换；不能把
    # 时间戳末尾数字误当成HHMM，否则会错误认定09:23竞价数据完整。
    if len(digits) == 13:
        timestamp = datetime.datetime.fromtimestamp(
            int(digits) / 1000.0,
            tz=CHINA_TZ,
        )
        return timestamp.strftime("%H%M")
    if len(digits) >= 6:
        return digits[-6:-2]
    return ""


def load_trade_calendar(path: Path = CALENDAR_PATH) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到交易日历：{path}")
    calendar = pd.read_csv(path, dtype=str, low_memory=False)
    required = {"cal_date", "is_open"}
    missing = sorted(required - set(calendar.columns))
    if missing:
        raise ValueError(f"交易日历缺少字段：{missing}")
    dates = sorted(
        {
            normalize_date(row.cal_date)
            for row in calendar.itertuples(index=False)
            if str(row.is_open) == "1" and normalize_date(row.cal_date)
        }
    )
    if not dates:
        raise ValueError("交易日历没有有效开市日")
    return dates


def nth_trade_date(calendar: list[str], signal_date: str, n: int) -> str:
    """返回信号日之后第n个交易日。"""

    index = bisect.bisect_right(calendar, signal_date)
    target = index + n - 1
    if target >= len(calendar):
        raise ValueError(f"交易日历不足：{signal_date}之后第{n}个交易日不存在")
    return calendar[target]


def load_relay_targets(
    portfolio_path: Path = PORTFOLIO_PATH,
    d_trades_path: Path = D_TRADES_PATH,
    calendar_path: Path = CALENDAR_PATH,
) -> pd.DataFrame:
    """从锁定组合提取8笔接力，并补齐D旧仓与新候选信息。"""

    if not portfolio_path.exists():
        raise FileNotFoundError(f"找不到组合逐笔账本：{portfolio_path}")
    if not d_trades_path.exists():
        raise FileNotFoundError(f"找不到D逐笔账本：{d_trades_path}")

    portfolio = pd.read_csv(
        portfolio_path,
        dtype={"signal_date": str, "buy_date": str, "exit_date": str, "ts_code": str},
        low_memory=False,
    )
    portfolio_required = {
        "signal_date",
        "status",
        "strategy_leg",
        "ts_code",
        "name",
        "buy_date",
        "exit_date",
        "account_return",
        "equity_before",
        "return_source",
    }
    missing = sorted(portfolio_required - set(portfolio.columns))
    if missing:
        raise ValueError(f"组合逐笔账本缺少字段：{missing}")

    relay = portfolio[
        portfolio["status"].astype(str).eq("EXECUTED")
        & portfolio["strategy_leg"].astype(str).isin(RELAY_LEGS)
    ].copy()
    if len(relay) != EXPECTED_RELAY_COUNT:
        raise ValueError(
            f"当前锁定组合接力必须为{EXPECTED_RELAY_COUNT}笔，实际{len(relay)}笔"
        )
    if EXPECTED_RELAY_COUNT == 0:
        # D接力已关闭，组合中不存在 D→A/C/E2，无采集对象。返回空表而不是继续
        # 往下做字段校验——那些校验假定至少有一笔接力。
        return relay.reset_index(drop=True)
    relay["signal_date"] = relay["signal_date"].map(normalize_date)
    relay["relay_date"] = relay["buy_date"].map(normalize_date)
    relay["next_exit_date"] = relay["exit_date"].map(normalize_date)
    relay["next_ts_code"] = relay["ts_code"].astype(str).str.strip().str.upper()
    relay["next_name"] = relay["name"].astype(str)
    relay["combined_account_return"] = pd.to_numeric(
        relay["account_return"], errors="raise"
    )
    relay["equity_before"] = pd.to_numeric(relay["equity_before"], errors="raise")

    d_trades = pd.read_csv(
        d_trades_path,
        dtype={"signal_date": str, "ts_code": str},
        low_memory=False,
    )
    d_required = {
        "signal_date",
        "ts_code",
        "name",
        "limit_close",
        "next_open",
        "exit_close",
        "exit_rule",
    }
    missing = sorted(d_required - set(d_trades.columns))
    if missing:
        raise ValueError(f"D逐笔账本缺少字段：{missing}")
    d_trades["signal_date"] = d_trades["signal_date"].map(normalize_date)
    if d_trades["signal_date"].duplicated().any():
        d_trades = d_trades.drop_duplicates("signal_date", keep="last")
    d_fields = d_trades[
        [
            "signal_date",
            "ts_code",
            "name",
            "limit_close",
            "next_open",
            "exit_close",
            "exit_rule",
        ]
    ].rename(
        columns={
            "ts_code": "d_ts_code",
            "name": "d_name",
            "limit_close": "d_entry_price",
            "next_open": "d_t1_open",
            "exit_close": "d_t2_close",
        }
    )
    relay = relay.merge(d_fields, on="signal_date", how="left", validate="one_to_one")
    relay["d_ts_code"] = relay["d_ts_code"].astype(str).str.strip().str.upper()
    for column in ("d_entry_price", "d_t1_open", "d_t2_close"):
        relay[column] = pd.to_numeric(relay[column], errors="coerce")

    # 回测复合收益字符串是当前D接力两腿收益的唯一锁定来源。
    relay["d_t1_account_return"] = pd.to_numeric(
        relay["return_source"].astype(str).str.extract(
            r"D_T1_RELAY\(([-0-9.]+)\)", expand=False
        ),
        errors="coerce",
    )
    relay["next_account_return"] = pd.to_numeric(
        relay["return_source"].astype(str).str.extract(
            r"\+(?:A|C|E2)\(([-0-9.]+)\)", expand=False
        ),
        errors="coerce",
    )

    calendar = load_trade_calendar(calendar_path)
    relay["d_t2_exit_date"] = relay["signal_date"].map(
        lambda value: nth_trade_date(calendar, value, 2)
    )
    # 接力与否由组合回放认定：portfolio_trades 的 strategy_leg 是 D→A/C/E2
    # 就是接力（上面已按 RELAY_LEGS 过滤）。
    #
    # d_trades 的 exit_rule 不能当判据：它是 D 单独回放时的结论，那次 A/C 被
    # baseline.abc_return 裁掉（见 certify 的 load_ac_daily），A/C 无候选的日子
    # D 只能自己做 T+2，于是记成 T+2_close。A/C 候选修正后这些日子有了接力对象，
    # 标签却还停在旧口径——和 baseline 是同一张作废持仓表的两个投影。
    #
    # 真正必需的是价格与收益字段，它们与 exit_rule 无关、始终按实际行情落盘。
    invalid = (
        relay["signal_date"].eq("")
        | relay["relay_date"].eq("")
        | relay["next_exit_date"].eq("")
        | relay["d_ts_code"].eq("")
        | relay["next_ts_code"].eq("")
        | relay["d_entry_price"].isna()
        | relay["d_entry_price"].le(0)
        | relay["d_t1_open"].isna()
        | relay["d_t1_open"].le(0)
        | relay["d_t1_account_return"].isna()
        | relay["next_account_return"].isna()
    )
    if invalid.any():
        raise ValueError(
            "接力元数据不完整："
            + ",".join(relay.loc[invalid, "signal_date"].astype(str).tolist())
        )
    if relay["signal_date"].duplicated().any():
        raise ValueError("接力信号日重复")

    columns = [
        "signal_date",
        "relay_date",
        "strategy_leg",
        "d_ts_code",
        "d_name",
        "d_entry_price",
        "d_t1_open",
        "d_t2_exit_date",
        "d_t2_close",
        "d_t1_account_return",
        "next_ts_code",
        "next_name",
        "next_exit_date",
        "next_account_return",
        "combined_account_return",
        "equity_before",
        "return_source",
    ]
    return relay[columns].sort_values("relay_date").reset_index(drop=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]
    return []


def normalize_ticks(
    frame: pd.DataFrame | None,
    *,
    signal_date: str,
    relay_date: str,
    role: str,
    ts_code: str,
) -> pd.DataFrame:
    """保留QMT竞价关键原始字段，并展开买卖五档。"""

    base_columns = [
        "signal_date",
        "relay_date",
        "role",
        "ts_code",
        "bar_time",
        "hhmm",
        "last_price",
        "pre_close",
        "amount",
        "volume",
        "pvolume",
    ]
    level_columns = [
        f"{side}{field}{level}"
        for side in ("bid", "ask")
        for field in ("_price_", "_volume_")
        for level in range(1, 6)
    ]
    columns = base_columns + level_columns
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for timestamp, bar in frame.iterrows():
        bid_prices = _as_list(bar.get("bidPrice", bar.get("bid_price")))
        ask_prices = _as_list(bar.get("askPrice", bar.get("ask_price")))
        bid_volumes = _as_list(bar.get("bidVol", bar.get("bid_volume")))
        ask_volumes = _as_list(bar.get("askVol", bar.get("ask_volume")))
        # get_full_tick通常返回五档数组，历史get_market_data_ex在部分QMT版本
        # 返回bidPrice1/askPrice1平铺字段；两种都必须兼容，不能因字段形态不同
        # 把真实竞价盘口误判为空。
        if not bid_prices:
            bid_prices = [bar.get(f"bidPrice{level}", 0.0) for level in range(1, 6)]
        if not ask_prices:
            ask_prices = [bar.get(f"askPrice{level}", 0.0) for level in range(1, 6)]
        if not bid_volumes:
            bid_volumes = [bar.get(f"bidVol{level}", 0.0) for level in range(1, 6)]
        if not ask_volumes:
            ask_volumes = [bar.get(f"askVol{level}", 0.0) for level in range(1, 6)]
        raw_time = bar.get("time", timestamp)
        row: dict[str, Any] = {
            "signal_date": signal_date,
            "relay_date": relay_date,
            "role": role,
            "ts_code": ts_code,
            "bar_time": str(raw_time),
            "hhmm": extract_hhmm(raw_time),
            "last_price": bar.get("lastPrice", bar.get("last_price", 0.0)),
            "pre_close": bar.get("lastClose", bar.get("preClose", 0.0)),
            "amount": bar.get("amount", 0.0),
            "volume": bar.get("volume", 0.0),
            "pvolume": bar.get("pvolume", 0.0),
        }
        for level in range(5):
            row[f"bid_price_{level + 1}"] = (
                bid_prices[level] if len(bid_prices) > level else 0.0
            )
            row[f"ask_price_{level + 1}"] = (
                ask_prices[level] if len(ask_prices) > level else 0.0
            )
            row[f"bid_volume_{level + 1}"] = (
                bid_volumes[level] if len(bid_volumes) > level else 0.0
            )
            row[f"ask_volume_{level + 1}"] = (
                ask_volumes[level] if len(ask_volumes) > level else 0.0
            )
        rows.append(row)
    result = pd.DataFrame(rows, columns=columns)
    numeric = [column for column in columns if column not in {
        "signal_date", "relay_date", "role", "ts_code", "bar_time", "hhmm"
    }]
    for column in numeric:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def normalize_one_minute(
    frame: pd.DataFrame | None,
    *,
    signal_date: str,
    relay_date: str,
    role: str,
    ts_code: str,
) -> pd.DataFrame:
    columns = [
        "signal_date",
        "relay_date",
        "role",
        "ts_code",
        "bar_time",
        "hhmm",
        *ONE_MINUTE_FIELDS,
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for timestamp, bar in frame.iterrows():
        raw_time = bar.get("time", timestamp)
        rows.append(
            {
                "signal_date": signal_date,
                "relay_date": relay_date,
                "role": role,
                "ts_code": ts_code,
                "bar_time": str(raw_time),
                "hhmm": extract_hhmm(raw_time),
                **{field: bar.get(field, 0.0) for field in ONE_MINUTE_FIELDS},
            }
        )
    result = pd.DataFrame(rows, columns=columns)
    for field in ONE_MINUTE_FIELDS:
        result[field] = pd.to_numeric(result[field], errors="coerce")
    return result


def fetch_ticks(xtdata: Any, ts_code: str, relay_date: str) -> pd.DataFrame | None:
    xtdata.download_history_data(
        ts_code,
        period="tick",
        start_time=relay_date,
        end_time=relay_date,
    )
    data = xtdata.get_market_data_ex(
        [],
        [ts_code],
        period="tick",
        start_time=relay_date + "091500",
        end_time=relay_date + "103000",
    )
    return data.get(ts_code)


def fetch_one_minute(xtdata: Any, ts_code: str, relay_date: str) -> pd.DataFrame | None:
    xtdata.download_history_data(
        ts_code,
        period="1m",
        start_time=relay_date,
        end_time=relay_date,
    )
    data = xtdata.get_market_data_ex(
        ONE_MINUTE_FIELDS,
        [ts_code],
        period="1m",
        start_time=relay_date + "093000",
        end_time=relay_date + "103000",
    )
    return data.get(ts_code)


def load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(
        path,
        dtype={
            "signal_date": str,
            "relay_date": str,
            "role": str,
            "ts_code": str,
            "bar_time": str,
            "hhmm": str,
        },
        low_memory=False,
    )


def _role_key(signal_date: str, role: str) -> str:
    return f"{signal_date}|{role}"


def complete_tick_keys(frame: pd.DataFrame) -> set[str]:
    """tick必须覆盖竞价时段并包含09:23前后的有效盘口。"""

    required = {"signal_date", "role", "hhmm", "bid_price_1", "ask_price_1"}
    if frame.empty or not required.issubset(frame.columns):
        return set()
    complete: set[str] = set()
    for (signal_date, role), group in frame.groupby(["signal_date", "role"]):
        hhmm = group["hhmm"].astype(str).str.zfill(4)
        auction = group[hhmm.between("0915", "0925")]
        around_0923 = group[hhmm.between("0922", "0924")]
        valid_book = (
            pd.to_numeric(around_0923["bid_price_1"], errors="coerce").fillna(0).gt(0)
            | pd.to_numeric(around_0923["ask_price_1"], errors="coerce").fillna(0).gt(0)
        )
        if not auction.empty and bool(valid_book.any()):
            complete.add(_role_key(str(signal_date), str(role)))
    return complete


def complete_one_minute_keys(frame: pd.DataFrame) -> set[str]:
    required = {"signal_date", "role", "hhmm", "open", "close", "amount"}
    if frame.empty or not required.issubset(frame.columns):
        return set()
    complete: set[str] = set()
    for (signal_date, role), group in frame.groupby(["signal_date", "role"]):
        hhmm = group["hhmm"].astype(str).str.zfill(4)
        window = group[hhmm.between("0930", "1030")]
        valid = (
            pd.to_numeric(window["open"], errors="coerce").fillna(0).gt(0)
            & pd.to_numeric(window["close"], errors="coerce").fillna(0).gt(0)
            & pd.to_numeric(window["amount"], errors="coerce").fillna(-1).ge(0)
        )
        if len(window) >= MIN_ONE_MINUTE_BARS and bool(valid.all()):
            complete.add(_role_key(str(signal_date), str(role)))
    return complete


def merge_and_save(
    current: pd.DataFrame,
    additions: list[pd.DataFrame],
    path: Path,
) -> pd.DataFrame:
    frames = [frame for frame in [current, *additions] if not frame.empty]
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    result["signal_date"] = result["signal_date"].map(normalize_date)
    result["relay_date"] = result["relay_date"].map(normalize_date)
    result = result.drop_duplicates(
        ["signal_date", "role", "bar_time"], keep="last"
    ).sort_values(["relay_date", "signal_date", "role", "bar_time"])
    path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(path, index=False, encoding="utf-8-sig")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集8笔D接力竞价tick和开盘后分钟行情")
    parser.add_argument("--dry-run", action="store_true", help="只显示8笔目标，不访问QMT")
    parser.add_argument("--overwrite", action="store_true", help="忽略完整样本并重新下载")
    parser.add_argument("--limit", type=int, default=0, help="只采集前N笔接力，0表示全部")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    targets = load_relay_targets()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets.to_csv(TARGETS_PATH, index=False, encoding="utf-8-sig")
    if args.limit > 0:
        targets = targets.head(args.limit).copy()
    print(f"D接力样本：{len(targets)}笔（锁定组合完整样本={EXPECTED_RELAY_COUNT}笔）")
    print(
        targets[
            [
                "signal_date",
                "relay_date",
                "strategy_leg",
                "d_ts_code",
                "d_name",
                "next_ts_code",
                "next_name",
            ]
        ].to_string(index=False)
    )
    if args.dry_run:
        return

    # 延迟导入，确保Mac及没有QMT的测试环境可以运行--dry-run和元数据测试。
    from xtquant import xtdata  # type: ignore

    existing_tick = load_existing(TICK_PATH)
    existing_one = load_existing(ONE_MINUTE_PATH)
    done_tick = set() if args.overwrite else complete_tick_keys(existing_tick)
    done_one = set() if args.overwrite else complete_one_minute_keys(existing_one)
    tick_additions: list[pd.DataFrame] = []
    one_additions: list[pd.DataFrame] = []
    report_rows: list[dict[str, Any]] = []
    tick_cache: dict[tuple[str, str], pd.DataFrame | None] = {}
    one_cache: dict[tuple[str, str], pd.DataFrame | None] = {}

    for index, target in enumerate(targets.itertuples(index=False), 1):
        for role, ts_code, name in (
            ("D", target.d_ts_code, target.d_name),
            ("NEXT", target.next_ts_code, target.next_name),
        ):
            key = _role_key(target.signal_date, role)
            report: dict[str, Any] = {
                "signal_date": target.signal_date,
                "relay_date": target.relay_date,
                "strategy_leg": target.strategy_leg,
                "role": role,
                "ts_code": ts_code,
                "name": name,
                "tick_rows": 0,
                "one_minute_rows": 0,
                "tick_status": "",
                "one_minute_status": "",
                "tick_error": "",
                "one_minute_error": "",
            }
            cache_key = (ts_code, target.relay_date)
            if key in done_tick:
                existing_group = existing_tick[
                    existing_tick["signal_date"].astype(str).eq(target.signal_date)
                    & existing_tick["role"].astype(str).eq(role)
                ]
                report["tick_rows"] = len(existing_group)
                report["tick_status"] = "SKIPPED_COMPLETE"
            else:
                try:
                    if cache_key not in tick_cache:
                        tick_cache[cache_key] = fetch_ticks(
                            xtdata, ts_code, target.relay_date
                        )
                    normalized = normalize_ticks(
                        tick_cache[cache_key],
                        signal_date=target.signal_date,
                        relay_date=target.relay_date,
                        role=role,
                        ts_code=ts_code,
                    )
                    report["tick_rows"] = len(normalized)
                    report["tick_status"] = (
                        "COMPLETE"
                        if key in complete_tick_keys(normalized)
                        else "INCOMPLETE"
                    )
                    if not normalized.empty:
                        tick_additions.append(normalized)
                except Exception as exc:  # noqa: BLE001
                    report["tick_status"] = "FAILED"
                    report["tick_error"] = f"{type(exc).__name__}: {exc}"[:300]

            if key in done_one:
                existing_group = existing_one[
                    existing_one["signal_date"].astype(str).eq(target.signal_date)
                    & existing_one["role"].astype(str).eq(role)
                ]
                report["one_minute_rows"] = len(existing_group)
                report["one_minute_status"] = "SKIPPED_COMPLETE"
            else:
                try:
                    if cache_key not in one_cache:
                        one_cache[cache_key] = fetch_one_minute(
                            xtdata, ts_code, target.relay_date
                        )
                    normalized = normalize_one_minute(
                        one_cache[cache_key],
                        signal_date=target.signal_date,
                        relay_date=target.relay_date,
                        role=role,
                        ts_code=ts_code,
                    )
                    report["one_minute_rows"] = len(normalized)
                    report["one_minute_status"] = (
                        "COMPLETE"
                        if key in complete_one_minute_keys(normalized)
                        else "INCOMPLETE"
                    )
                    if not normalized.empty:
                        one_additions.append(normalized)
                except Exception as exc:  # noqa: BLE001
                    report["one_minute_status"] = "FAILED"
                    report["one_minute_error"] = f"{type(exc).__name__}: {exc}"[:300]
            report_rows.append(report)

        if index % 2 == 0:
            merge_and_save(existing_tick, tick_additions, TICK_PATH)
            merge_and_save(existing_one, one_additions, ONE_MINUTE_PATH)
            print(f"采集进度：{index}/{len(targets)}笔接力")

    final_tick = merge_and_save(existing_tick, tick_additions, TICK_PATH)
    final_one = merge_and_save(existing_one, one_additions, ONE_MINUTE_PATH)
    report = pd.DataFrame(report_rows)
    report.to_csv(REPORT_PATH, index=False, encoding="utf-8-sig")

    final_tick_complete = complete_tick_keys(final_tick)
    final_one_complete = complete_one_minute_keys(final_one)
    expected_keys = {
        _role_key(str(row.signal_date), role)
        for row in targets.itertuples(index=False)
        for role in ("D", "NEXT")
    }
    complete_keys = expected_keys & final_tick_complete & final_one_complete
    print(
        f"采集完成：完整角色{len(complete_keys)}/{len(expected_keys)}；"
        f"对应接力笔数={len(complete_keys) // 2}/{len(targets)}。"
    )
    print(f"tick→{TICK_PATH}")
    print(f"1分钟→{ONE_MINUTE_PATH}")
    if complete_keys != expected_keys:
        print("存在不完整样本，禁止直接做容量认证，请查看：", REPORT_PATH)


if __name__ == "__main__":
    sys.exit(main())
