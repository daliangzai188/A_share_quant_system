#!/usr/bin/env python3
"""严格核验并回补人工提供的券商历史卖出证据。

默认仅演练。实际写入前必须停止daemon，再显式传入 ``--apply``。脚本会备份
positions.json，写入逐持仓退出金额、人工证据元数据、统一卖出事件并重建汇总。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.broker_exit_evidence import (
    BrokerExitEvidenceError,
    apply_broker_evidence_plan,
    build_broker_evidence_plan,
)
from src.execution_completion_tracker import ExecutionCompletionTracker


POSITIONS_PATH = PROJECT_ROOT / "data" / "processed" / "positions.json"
DAEMON_PID_PATH = PROJECT_ROOT / ".daemon_pid"


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _load_evidence(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
        raise BrokerExitEvidenceError("证据文件schema_version必须为1")
    records = value.get("records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise BrokerExitEvidenceError("证据文件records必须是对象数组")
    return records


def _print_plan(plans: list[dict[str, Any]]) -> None:
    print("严格匹配成功：")
    for plan in plans:
        evidence = plan["evidence"]
        order_status = evidence.broker_order_id or "截图未显示"
        print(
            f"  {evidence.exit_date} {evidence.exit_time} {evidence.ts_code} "
            f"{evidence.name} {evidence.strategy_leg} | {evidence.filled_qty}股 | "
            f"截图均价{evidence.displayed_fill_price} | 成交金额{evidence.fill_amount:.2f} | "
            f"委托编号:{order_status}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="回补经人工核验的券商历史卖出证据")
    parser.add_argument("--evidence", required=True, type=Path, help="本地证据JSON文件")
    parser.add_argument("--apply", action="store_true", help="创建备份并实际写回")
    args = parser.parse_args()

    if not POSITIONS_PATH.exists():
        raise BrokerExitEvidenceError(f"持仓账不存在：{POSITIONS_PATH}")
    positions = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(positions, list) or not all(isinstance(row, dict) for row in positions):
        raise BrokerExitEvidenceError("positions.json必须是对象数组")
    records = _load_evidence(args.evidence.resolve())
    plans = build_broker_evidence_plan(positions, records)
    _print_plan(plans)
    if not args.apply:
        print("演练完成，未修改任何文件。停止daemon后可增加 --apply 写回。")
        return 0
    if DAEMON_PID_PATH.exists():
        raise BrokerExitEvidenceError(
            "检测到.daemon_pid；为避免与持仓线程并发覆盖，请先运行stop_windows.py"
        )

    now = dt.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d%H%M%S")
    applied_at = now.isoformat(timespec="seconds")
    backup = POSITIONS_PATH.with_name(
        f"positions.backup_before_broker_evidence_{stamp}.json"
    )
    shutil.copy2(POSITIONS_PATH, backup)
    updated = apply_broker_evidence_plan(positions, plans, applied_at=applied_at)
    _write_json_atomic(POSITIONS_PATH, updated)

    tracker = ExecutionCompletionTracker(PROJECT_ROOT)
    for plan in plans:
        evidence = plan["evidence"]
        tracker.record_sell_slice(
            event_id=f"券商截图回补|{evidence.evidence_id}",
            entry_date=evidence.entry_date,
            exit_date=evidence.exit_date,
            time=evidence.exit_time,
            ts_code=evidence.ts_code,
            name=evidence.name,
            strategy_leg=evidence.strategy_leg,
            signal_date=evidence.signal_date,
            channel="券商截图回补",
            broker_order_id=evidence.broker_order_id,
            order_qty=evidence.filled_qty,
            filled_qty=evidence.filled_qty,
            fill_price=float(evidence.effective_fill_price),
            fill_amount=float(evidence.fill_amount),
            remaining_qty=0,
            status="已成交",
            note=(
                f"人工核验券商截图；原图SHA256={evidence.evidence_sha256 or '未提供'}；"
                f"委托编号={'已提供' if evidence.broker_order_id else '截图未显示'}"
            ),
            recorded_at=applied_at,
        )
    summary_path = tracker.rebuild_summary()
    mirror_audit = tracker.mirror_existing_events()
    audit_path = tracker.report_dir / f"broker_exit_evidence_import_{stamp}.json"
    _write_json_atomic(
        audit_path,
        {
            "schema_version": 1,
            "status": "APPLIED",
            "applied_at": applied_at,
            "evidence_path": str(args.evidence.resolve()),
            "positions_backup_path": str(backup),
            "record_count": len(plans),
            "records": records,
            "event_store_audit": mirror_audit,
        },
    )
    if mirror_audit.get("status") != "PASS":
        raise RuntimeError("执行事件镜像审计失败，请保留备份并立即检查")
    print(f"已回补{len(plans)}笔；持仓备份：{backup}")
    print(f"成交汇总：{summary_path}")
    print(f"审计报告：{audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

