#!/usr/bin/env python3
"""用交易守护进程中的明确成交回报修复卖出价为0的历史持仓账。

默认只做演练并打印核对结果。传入 ``--apply`` 后才会：
1. 为 positions.json 创建带时间戳的可恢复备份；
2. 仅回补“日期+股票代码的日志成交总股数 == 原始持仓总股数”的组；
3. 将完整成交量、成交额加权均价和来源写回持仓账；
4. 重建统一成交完成率汇总。

不会用收盘价、委托价或推测价格替代真实成交回报。数量无法完全核对的记录
保持原样，并在终端列为 unresolved。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSITIONS_FILE = PROJECT_ROOT / "data" / "processed" / "positions.json"
LOG_GLOB = "trading_daemon.log*"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

_LINE_DATE_RE = re.compile(r"^(?P<day>\d{4})-(?P<month>\d{2})-(?P<date>\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})")
_POV_RE = re.compile(
    r"\[卖出POV\]\s+(?P<code>\d{6}(?:\.(?:SH|SZ|BJ))?).*?"
    r"成(?P<qty>\d+)股@(?P<price>\d+(?:\.\d+)?)"
)
_WATCHDOG_RE = re.compile(
    r"\[平仓看门狗\]\s+(?P<code>\d{6}(?:\.(?:SH|SZ|BJ))?).*?"
    r"补挂确认成交(?P<qty>\d+)股\s+均价(?P<price>\d+(?:\.\d+)?)"
)


@dataclass(frozen=True)
class FillEvent:
    trade_date: str
    time: str
    ts_code: str
    quantity: int
    price: float
    channel: str
    source_file: str
    line_number: int


def _normalise_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def parse_fill_line(line: str, *, source_file: str = "", line_number: int = 0) -> FillEvent | None:
    """解析一条明确包含成交股数和成交价的退出日志。"""
    date_match = _LINE_DATE_RE.search(line)
    if not date_match:
        return None
    event_match = _POV_RE.search(line)
    channel = "卖出POV"
    if not event_match:
        event_match = _WATCHDOG_RE.search(line)
        channel = "收盘看门狗补挂"
    if not event_match:
        return None
    quantity = int(event_match.group("qty"))
    price = float(event_match.group("price"))
    if quantity <= 0 or price <= 0:
        return None
    trade_date = "".join(
        (date_match.group("day"), date_match.group("month"), date_match.group("date"))
    )
    return FillEvent(
        trade_date=trade_date,
        time=date_match.group("time"),
        ts_code=_normalise_code(event_match.group("code")),
        quantity=quantity,
        price=price,
        channel=channel,
        source_file=source_file,
        line_number=line_number,
    )


def collect_fill_events(log_paths: Iterable[Path]) -> list[FillEvent]:
    events: list[FillEvent] = []
    for path in sorted(log_paths, key=lambda item: item.name):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                event = parse_fill_line(
                    line, source_file=path.name, line_number=line_number
                )
                if event is not None:
                    events.append(event)
    return events


def _positive_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _positive_float(value: Any) -> float:
    try:
        return max(float(value or 0.0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _original_quantity(position: dict[str, Any]) -> int:
    return _positive_int(position.get("entry_shares")) or _positive_int(position.get("shares"))


def build_backfill_plan(
    positions: list[dict[str, Any]], events: list[FillEvent]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回可安全回补组和未解决组，不修改输入持仓。"""
    events_by_key: dict[tuple[str, str], list[FillEvent]] = defaultdict(list)
    for event in events:
        events_by_key[(event.trade_date, event.ts_code)].append(event)

    positions_by_key: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, position in enumerate(positions):
        if str(position.get("status", "")).lower() != "closed":
            continue
        if _positive_float(position.get("sell_price")) > 0:
            continue
        quantity = _original_quantity(position)
        sell_date = str(position.get("sell_date", "")).replace("-", "")[:8]
        ts_code = _normalise_code(position.get("ts_code"))
        if quantity > 0 and len(sell_date) == 8 and ts_code:
            positions_by_key[(sell_date, ts_code)].append((index, position))

    ready: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for key, indexed_positions in sorted(positions_by_key.items()):
        matched_events = events_by_key.get(key, [])
        position_qty = sum(_original_quantity(position) for _, position in indexed_positions)
        event_qty = sum(event.quantity for event in matched_events)
        event_amount = sum(event.quantity * event.price for event in matched_events)
        item = {
            "trade_date": key[0],
            "ts_code": key[1],
            "name": next(
                (str(position.get("name", "")) for _, position in indexed_positions if position.get("name")),
                "",
            ),
            "position_indices": [index for index, _ in indexed_positions],
            "position_count": len(indexed_positions),
            "position_qty": position_qty,
            "event_count": len(matched_events),
            "event_qty": event_qty,
            "event_amount": round(event_amount, 6),
            "weighted_price": round(event_amount / event_qty, 6) if event_qty > 0 else 0.0,
            "events": [asdict(event) for event in matched_events],
        }
        if position_qty > 0 and event_qty == position_qty and event_amount > 0:
            ready.append(item)
        else:
            item["reason"] = (
                "未找到明确成交日志" if event_qty == 0 else f"股数不一致:持仓{position_qty}/日志{event_qty}"
            )
            unresolved.append(item)
    return ready, unresolved


def apply_backfill(
    positions: list[dict[str, Any]], ready: list[dict[str, Any]], *, applied_at: str
) -> None:
    for group in ready:
        sell_date = str(group["trade_date"])
        weighted_price = float(group["weighted_price"])
        sources = sorted({event["source_file"] for event in group["events"]})
        for index in group["position_indices"]:
            position = positions[int(index)]
            quantity = _original_quantity(position)
            position["entry_shares"] = quantity
            position["shares"] = 0
            position["status"] = "closed"
            position["sell_date"] = sell_date
            position["sell_price"] = weighted_price
            position["exit_fills_by_date"] = {
                sell_date: {
                    "qty": quantity,
                    "amount": round(quantity * weighted_price, 6),
                }
            }
            position["exit_fill_backfill"] = {
                "applied_at": applied_at,
                "method": "交易日志明确成交量价且组内总股数完全匹配",
                "group_event_count": int(group["event_count"]),
                "group_event_qty": int(group["event_qty"]),
                "group_weighted_price": weighted_price,
                "source_files": sources,
            }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _print_plan(ready: list[dict[str, Any]], unresolved: list[dict[str, Any]]) -> None:
    print("可安全回补：")
    if not ready:
        print("  无")
    for item in ready:
        print(
            f"  {item['trade_date']} {item['ts_code']} {item['name']} | "
            f"{item['position_count']}条/{item['position_qty']}股 | "
            f"日志{item['event_count']}笔/{item['event_qty']}股 | "
            f"加权均价{item['weighted_price']:.4f}"
        )
    print("未解决（保持原样）：")
    if not unresolved:
        print("  无")
    for item in unresolved:
        print(
            f"  {item['trade_date']} {item['ts_code']} {item['name']} | {item['reason']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="从明确成交日志回补sell_price=0的持仓记录")
    parser.add_argument("--apply", action="store_true", help="创建备份后写回；默认只演练")
    args = parser.parse_args()

    positions = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    if not isinstance(positions, list):
        raise RuntimeError(f"持仓文件格式错误，应为JSON数组:{POSITIONS_FILE}")
    log_paths = list((PROJECT_ROOT / "logs").glob(LOG_GLOB))
    events = collect_fill_events(log_paths)
    ready, unresolved = build_backfill_plan(positions, events)
    _print_plan(ready, unresolved)
    if not args.apply:
        print("演练完成，未修改任何文件；确认后可加 --apply。")
        return 0
    if not ready:
        print("没有满足严格匹配条件的记录，不执行写入。")
        return 0

    now = datetime.now(BEIJING_TZ)
    stamp = now.strftime("%Y%m%d%H%M%S")
    applied_at = now.isoformat(timespec="seconds")
    backup_path = POSITIONS_FILE.with_name(f"positions.backup_before_exit_backfill_{stamp}.json")
    shutil.copy2(POSITIONS_FILE, backup_path)
    apply_backfill(positions, ready, applied_at=applied_at)
    _write_json_atomic(POSITIONS_FILE, positions)

    report_path = (
        PROJECT_ROOT / "reports" / "execution_tracking" / f"exit_fill_backfill_{stamp}.json"
    )
    _write_json_atomic(
        report_path,
        {
            "applied_at": applied_at,
            "backup_path": str(backup_path),
            "ready": ready,
            "unresolved": unresolved,
        },
    )
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.execution_completion_tracker import ExecutionCompletionTracker

    summary_path = ExecutionCompletionTracker(PROJECT_ROOT).rebuild_summary()
    print(f"已写回持仓账；备份:{backup_path}")
    print(f"回补审计报告:{report_path}")
    print(f"统一成交完成率已重建:{summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
