"""策略D历史L2供应商样本的内容级验收。

本模块只验证已经按仓库样本契约标准化的供应商文件，不连接数据商、不购买
数据，也不修改正式D。与只检查清单声明的全窗口闸门不同，这里会实际读取
逐笔委托、逐笔成交和同步盘口文件；任何缺文件、缺字段或关联失败都 fail-closed。
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from src.strategy_d_strict_intraday import event_hhmm, normalize_event_time


REQUIRED_SAMPLE_DATES = ("20240701", "20250630", "20260629")
REQUIRED_SAMPLE_EXCHANGES = ("SSE", "SZSE", "BSE")
REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "trade_date",
        "exchange",
        "orders_file",
        "transactions_file",
        "snapshots_file",
        "full_market",
        "sequence_complete",
        "sequence_gap_detection",
        "order_trade_linkage",
        "raw_unadjusted_price",
        "volume_unit",
        "coverage_start_hhmm",
        "coverage_end_hhmm",
        "expected_security_count",
    }
)
REQUIRED_ORDER_COLUMNS = frozenset(
    {
        "trade_date",
        "exchange",
        "ts_code",
        "event_time",
        "channel_no",
        "sequence",
        "order_id",
        "action",
        "side",
        "price",
        "volume",
    }
)
REQUIRED_TRANSACTION_COLUMNS = frozenset(
    {
        "trade_date",
        "exchange",
        "ts_code",
        "event_time",
        "channel_no",
        "sequence",
        "price",
        "volume",
        "bid_order_id",
        "ask_order_id",
    }
)
REQUIRED_SNAPSHOT_COLUMNS = frozenset(
    {
        "trade_date",
        "exchange",
        "ts_code",
        "event_time",
        "scan_id",
        "last_price",
        "bid_price_1",
        "bid_volume_1",
        "bid_order_count_1",
        "bid_queue_1",
    }
)


def as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def clean_text(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def integer_value(value: object, default: int = 0) -> int:
    number = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(number) else int(number)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_sample_file(sample_root: Path, relative_path: object) -> Path:
    root = sample_root.resolve()
    path = (root / str(relative_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"样本文件越出sample_root：{relative_path}") from exc
    return path


def read_csv_content(path: Path, required: frozenset[str], label: str) -> pd.DataFrame:
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{label}文件不存在或为空：{path}")
    frame = pd.read_csv(path, dtype=str, low_memory=False)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{label}缺少字段：{missing}")
    if frame.empty:
        raise ValueError(f"{label}没有数据行")
    return frame


def normalize_common(
    frame: pd.DataFrame, *, trade_date: str, exchange: str, label: str
) -> tuple[pd.DataFrame, list[str]]:
    data = frame.copy()
    errors: list[str] = []
    data["trade_date"] = data["trade_date"].map(clean_text)
    data["exchange"] = data["exchange"].astype(str).str.upper().str.strip()
    data["ts_code"] = data["ts_code"].astype(str).str.upper().str.strip()
    data["event_time_key"] = data["event_time"].map(normalize_event_time)
    if set(data["trade_date"]) != {trade_date}:
        errors.append(f"{label}含非目标trade_date")
    if set(data["exchange"]) != {exchange}:
        errors.append(f"{label}含非目标exchange")
    if data["ts_code"].eq("").any():
        errors.append(f"{label}存在空ts_code")
    if data["event_time_key"].le(0).any():
        errors.append(f"{label}存在无法解析的event_time")
    return data, errors


def validate_sequence(data: pd.DataFrame, label: str) -> list[str]:
    errors: list[str] = []
    data["channel_no"] = data["channel_no"].map(clean_text)
    data["sequence_number"] = pd.to_numeric(data["sequence"], errors="coerce")
    if data["channel_no"].eq("").any():
        errors.append(f"{label}存在空channel_no")
    if data["sequence_number"].isna().any():
        errors.append(f"{label}存在无法解析的sequence")
        return errors
    if data.duplicated(["channel_no", "sequence_number"]).any():
        errors.append(f"{label}存在重复(channel_no, sequence)")
    for channel, group in data.groupby("channel_no", sort=False):
        ordered = group.sort_values("sequence_number")
        if not ordered["event_time_key"].is_monotonic_increasing:
            errors.append(f"{label}频道{channel}的sequence与event_time倒序")
            break
    return errors


def parse_queue(value: object) -> list[float]:
    text = clean_text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("bid_queue_1不是JSON数组") from exc
    if not isinstance(parsed, list):
        raise ValueError("bid_queue_1不是JSON数组")
    result: list[float] = []
    for item in parsed:
        number = pd.to_numeric(item, errors="coerce")
        if pd.isna(number) or float(number) < 0:
            raise ValueError("bid_queue_1含非数值或负数")
        result.append(float(number))
    return result


def validate_orders(
    frame: pd.DataFrame, *, trade_date: str, exchange: str
) -> tuple[pd.DataFrame, list[str], set[tuple[str, str]]]:
    data, errors = normalize_common(
        frame, trade_date=trade_date, exchange=exchange, label="逐笔委托"
    )
    errors.extend(validate_sequence(data, "逐笔委托"))
    data["action"] = data["action"].astype(str).str.upper().str.strip()
    data["side"] = data["side"].astype(str).str.upper().str.strip()
    data["order_id"] = data["order_id"].map(clean_text)
    data["price_number"] = pd.to_numeric(data["price"], errors="coerce")
    data["volume_number"] = pd.to_numeric(data["volume"], errors="coerce")
    if not set(data["action"]).issubset({"ADD", "CANCEL"}):
        errors.append("逐笔委托action只允许ADD/CANCEL")
    if not {"ADD", "CANCEL"}.issubset(set(data["action"])):
        errors.append("逐笔委托样本必须同时包含ADD和CANCEL")
    if not set(data["side"]).issubset({"BUY", "SELL"}):
        errors.append("逐笔委托side只允许BUY/SELL")
    if data["order_id"].eq("").any():
        errors.append("逐笔委托存在空order_id")
    if data["price_number"].isna().any() or data["price_number"].lt(0).any():
        errors.append("逐笔委托price缺失或为负")
    if data["volume_number"].isna().any() or data["volume_number"].le(0).any():
        errors.append("逐笔委托volume缺失或非正")
    adds = data[data["action"].eq("ADD")]
    add_keys = set(zip(adds["channel_no"], adds["order_id"]))
    if adds.duplicated(["channel_no", "order_id"]).any():
        errors.append("ADD存在重复(channel_no, order_id)")
    cancels = data[data["action"].eq("CANCEL")]
    cancel_keys = set(zip(cancels["channel_no"], cancels["order_id"]))
    unresolved = cancel_keys - add_keys
    if unresolved:
        errors.append(f"CANCEL引用未知委托：{len(unresolved)}个")
    return data, errors, add_keys


def validate_transactions(
    frame: pd.DataFrame,
    *,
    trade_date: str,
    exchange: str,
    add_keys: set[tuple[str, str]],
) -> tuple[pd.DataFrame, list[str]]:
    data, errors = normalize_common(
        frame, trade_date=trade_date, exchange=exchange, label="逐笔成交"
    )
    errors.extend(validate_sequence(data, "逐笔成交"))
    data["bid_order_id"] = data["bid_order_id"].map(clean_text)
    data["ask_order_id"] = data["ask_order_id"].map(clean_text)
    data["price_number"] = pd.to_numeric(data["price"], errors="coerce")
    data["volume_number"] = pd.to_numeric(data["volume"], errors="coerce")
    if data["price_number"].isna().any() or data["price_number"].le(0).any():
        errors.append("逐笔成交price缺失或非正")
    if data["volume_number"].isna().any() or data["volume_number"].le(0).any():
        errors.append("逐笔成交volume缺失或非正")
    if data[["bid_order_id", "ask_order_id"]].eq("").any(axis=1).any():
        errors.append("逐笔成交缺少买方或卖方委托编号")
    referenced = set(zip(data["channel_no"], data["bid_order_id"])) | set(
        zip(data["channel_no"], data["ask_order_id"])
    )
    unresolved = {key for key in referenced if key[1]} - add_keys
    if unresolved:
        errors.append(f"逐笔成交引用未知委托：{len(unresolved)}个")
    return data, errors


def validate_snapshots(
    frame: pd.DataFrame,
    *,
    trade_date: str,
    exchange: str,
    expected_security_count: int,
    coverage_start_hhmm: int,
    coverage_end_hhmm: int,
) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    data, errors = normalize_common(
        frame, trade_date=trade_date, exchange=exchange, label="同步盘口"
    )
    data["scan_id"] = data["scan_id"].map(clean_text)
    numeric_columns = [
        "last_price", "bid_price_1", "bid_volume_1", "bid_order_count_1"
    ]
    for column in numeric_columns:
        data[f"{column}_number"] = pd.to_numeric(data[column], errors="coerce")
    if data["scan_id"].eq("").any():
        errors.append("同步盘口存在空scan_id")
    if data.duplicated(["scan_id", "ts_code"]).any():
        errors.append("同步盘口同一scan_id存在重复股票")
    if data[[f"{column}_number" for column in numeric_columns]].isna().any().any():
        errors.append("同步盘口数值字段存在缺失或非数值")

    universes: list[frozenset[str]] = []
    scan_times: list[int] = []
    for scan_id, group in data.groupby("scan_id", sort=False):
        universes.append(frozenset(group["ts_code"]))
        times = set(int(value) for value in group["event_time_key"])
        if len(times) != 1:
            errors.append(f"同步盘口scan_id={scan_id}的event_time不一致")
        elif times:
            scan_times.append(event_hhmm(next(iter(times))))
    if len(universes) < 2:
        errors.append("同步盘口至少需要两个全市场scan_id")
    if universes and any(universe != universes[0] for universe in universes):
        errors.append("同步盘口不同scan_id的股票宇宙不一致")
    actual_security_count = len(universes[0]) if universes else 0
    if expected_security_count <= 0:
        errors.append("expected_security_count必须为正")
    elif actual_security_count != expected_security_count:
        errors.append(
            "同步盘口股票数与声明不一致："
            f"actual={actual_security_count} expected={expected_security_count}"
        )
    if not scan_times or min(scan_times) > coverage_start_hhmm:
        errors.append("同步盘口未覆盖声明的起始时刻")
    if not scan_times or max(scan_times) < coverage_end_hhmm:
        errors.append("同步盘口未覆盖声明的结束时刻")

    non_empty_queue_count = 0
    for row in data.itertuples(index=False):
        try:
            queue = parse_queue(row.bid_queue_1)
        except ValueError as exc:
            errors.append(str(exc))
            break
        if len(queue) > 50:
            errors.append("bid_queue_1超过最优价前50笔")
            break
        bid_count = float(getattr(row, "bid_order_count_1_number"))
        bid_volume = float(getattr(row, "bid_volume_1_number"))
        if len(queue) > bid_count:
            errors.append("bid_queue_1笔数大于bid_order_count_1")
            break
        if sum(queue) > bid_volume + 1e-6:
            errors.append("bid_queue_1数量合计大于bid_volume_1")
            break
        if queue:
            non_empty_queue_count += 1
    if non_empty_queue_count == 0:
        errors.append("同步盘口没有任何最优价委托队列明细")
    metrics = {
        "scan_count": int(data["scan_id"].nunique()),
        "actual_security_count": actual_security_count,
        "non_empty_bid_queue_row_count": non_empty_queue_count,
        "first_scan_hhmm": min(scan_times) if scan_times else 0,
        "last_scan_hhmm": max(scan_times) if scan_times else 0,
    }
    return data, errors, metrics


def validate_sample_entry(
    sample_root: Path, entry: Mapping[str, Any]
) -> dict[str, Any]:
    trade_date = clean_text(entry.get("trade_date"))
    exchange = str(entry.get("exchange", "")).upper().strip()
    errors: list[str] = []
    missing_fields = sorted(REQUIRED_MANIFEST_FIELDS - set(entry))
    if missing_fields:
        errors.append(f"样本清单缺少字段：{missing_fields}")
    truth_fields = [
        "full_market",
        "sequence_complete",
        "sequence_gap_detection",
        "order_trade_linkage",
        "raw_unadjusted_price",
    ]
    for field in truth_fields:
        if not as_bool(entry.get(field, False)):
            errors.append(f"样本清单{field}不是true")
    if str(entry.get("volume_unit", "")).upper() != "SHARE":
        errors.append("样本清单volume_unit不是SHARE")
    start_hhmm = integer_value(entry.get("coverage_start_hhmm"))
    end_hhmm = integer_value(entry.get("coverage_end_hhmm"))
    if start_hhmm > 930:
        errors.append("样本清单未覆盖09:30前")
    if end_hhmm < 1455:
        errors.append("样本清单未覆盖14:55")
    expected_count = integer_value(entry.get("expected_security_count"))

    files: dict[str, dict[str, Any]] = {}
    loaded: dict[str, pd.DataFrame] = {}
    specs = {
        "orders": (entry.get("orders_file", ""), REQUIRED_ORDER_COLUMNS, "逐笔委托"),
        "transactions": (
            entry.get("transactions_file", ""),
            REQUIRED_TRANSACTION_COLUMNS,
            "逐笔成交",
        ),
        "snapshots": (
            entry.get("snapshots_file", ""),
            REQUIRED_SNAPSHOT_COLUMNS,
            "同步盘口",
        ),
    }
    for role, (relative_path, required, label) in specs.items():
        try:
            path = safe_sample_file(sample_root, relative_path)
            frame = read_csv_content(path, required, label)
            loaded[role] = frame
            files[role] = {
                "path": str(path),
                "row_count": int(len(frame)),
                "sha256": file_sha256(path),
            }
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            errors.append(str(exc))

    metrics: dict[str, Any] = {}
    add_keys: set[tuple[str, str]] = set()
    orders = pd.DataFrame()
    transactions = pd.DataFrame()
    snapshots = pd.DataFrame()
    if "orders" in loaded:
        orders, order_errors, add_keys = validate_orders(
            loaded["orders"], trade_date=trade_date, exchange=exchange
        )
        errors.extend(order_errors)
    if "transactions" in loaded:
        transactions, transaction_errors = validate_transactions(
            loaded["transactions"],
            trade_date=trade_date,
            exchange=exchange,
            add_keys=add_keys,
        )
        errors.extend(transaction_errors)
    if "snapshots" in loaded:
        snapshots, snapshot_errors, snapshot_metrics = validate_snapshots(
            loaded["snapshots"],
            trade_date=trade_date,
            exchange=exchange,
            expected_security_count=expected_count,
            coverage_start_hhmm=start_hhmm,
            coverage_end_hhmm=end_hhmm,
        )
        errors.extend(snapshot_errors)
        metrics.update(snapshot_metrics)
    if not orders.empty and not snapshots.empty:
        snapshot_universe = set(snapshots["ts_code"])
        outside = set(orders["ts_code"]) - snapshot_universe
        if outside:
            errors.append(f"逐笔委托有{len(outside)}只股票不在同步盘口宇宙")
    if not transactions.empty and not snapshots.empty:
        snapshot_universe = set(snapshots["ts_code"])
        outside = set(transactions["ts_code"]) - snapshot_universe
        if outside:
            errors.append(f"逐笔成交有{len(outside)}只股票不在同步盘口宇宙")
    metrics.update(
        {
            "order_row_count": int(len(orders)),
            "transaction_row_count": int(len(transactions)),
            "snapshot_row_count": int(len(snapshots)),
            "add_order_key_count": int(len(add_keys)),
        }
    )
    unique_errors = list(dict.fromkeys(errors))
    return {
        "trade_date": trade_date,
        "exchange": exchange,
        "passed": not unique_errors,
        "errors": unique_errors,
        "files": files,
        "metrics": metrics,
    }


def validate_sample_package(
    *,
    sample_root: Path,
    manifest_path: Path | None = None,
    required_dates: Sequence[str] = REQUIRED_SAMPLE_DATES,
    required_exchanges: Sequence[str] = REQUIRED_SAMPLE_EXCHANGES,
) -> dict[str, Any]:
    manifest = manifest_path or (sample_root / "manifest.json")
    expected = {
        (str(date), str(exchange).upper())
        for date in required_dates
        for exchange in required_exchanges
    }
    if not manifest.exists():
        return {
            "schema_version": 1,
            "status": "BLOCKED_NO_VENDOR_SAMPLE_MANIFEST",
            "passed": False,
            "manifest": str(manifest),
            "expected_sample_count": len(expected),
            "passed_sample_count": 0,
            "missing_sample_count": len(expected),
            "missing_samples": [f"{date}|{exchange}" for date, exchange in sorted(expected)],
            "invalid_sample_count": 0,
            "samples": [],
            "formal_rule_modified": False,
        }
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1,
            "status": "BLOCKED_INVALID_VENDOR_SAMPLE_MANIFEST",
            "passed": False,
            "manifest": str(manifest),
            "errors": [str(exc)],
            "expected_sample_count": len(expected),
            "passed_sample_count": 0,
            "missing_sample_count": len(expected),
            "missing_samples": [f"{date}|{exchange}" for date, exchange in sorted(expected)],
            "invalid_sample_count": 0,
            "samples": [],
            "formal_rule_modified": False,
        }
    entries = payload.get("samples", [])
    if not isinstance(entries, list):
        entries = []
    results = [validate_sample_entry(sample_root, entry) for entry in entries if isinstance(entry, dict)]
    result_by_key = {
        (result["trade_date"], result["exchange"]): result for result in results
    }
    present = set(result_by_key)
    missing = sorted(expected - present)
    invalid = sorted(
        key for key in expected & present if not result_by_key[key]["passed"]
    )
    unexpected = sorted(present - expected)
    duplicate_count = len(results) - len(result_by_key)
    passed = not missing and not invalid and not unexpected and duplicate_count == 0
    package_errors: list[str] = []
    if unexpected:
        package_errors.append(f"存在非门禁日期/市场样本：{len(unexpected)}个")
    if duplicate_count:
        package_errors.append(f"样本清单存在重复日期/市场：{duplicate_count}个")
    return {
        "schema_version": 1,
        "status": "PREPAYMENT_SAMPLE_GATE_PASSED" if passed else "BLOCKED_VENDOR_SAMPLE_REJECTED",
        "passed": passed,
        "provider": str(payload.get("provider", "")).strip(),
        "manifest": str(manifest),
        "manifest_sha256": file_sha256(manifest),
        "expected_sample_count": len(expected),
        "passed_sample_count": sum(
            1 for key in expected & present if result_by_key[key]["passed"]
        ),
        "missing_sample_count": len(missing),
        "missing_samples": [f"{date}|{exchange}" for date, exchange in missing],
        "invalid_sample_count": len(invalid),
        "invalid_samples": [f"{date}|{exchange}" for date, exchange in invalid],
        "unexpected_samples": [f"{date}|{exchange}" for date, exchange in unexpected],
        "errors": package_errors,
        "samples": results,
        "formal_rule_modified": False,
    }
