#!/usr/bin/env python3
"""创建、校验实盘状态快照，或恢复到生产目录外的隔离演练目录。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime_state_backup import (  # noqa: E402
    create_runtime_snapshot,
    restore_snapshot_to_staging,
    verify_runtime_snapshot,
)


DEFAULT_CONFIG = PROJECT_ROOT / "config" / "runtime_state_backup.json"


def _read_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("运行状态备份配置根节点必须是对象")
    return payload


def _write_latest_status(config: dict, payload: dict) -> None:
    raw = Path(str(config.get("latest_status_path", "")).strip())
    if not str(raw):
        return
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="实盘关键运行状态备份与隔离恢复演练")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="创建新快照并立即校验")
    verify_parser = subparsers.add_parser("verify", help="只校验已有快照")
    verify_parser.add_argument("--snapshot", type=Path, required=True)
    restore_parser = subparsers.add_parser(
        "restore-drill", help="只恢复到生产项目外的全新隔离目录"
    )
    restore_parser.add_argument("--snapshot", type=Path, required=True)
    restore_parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    config = _read_config(args.config)

    if args.command == "create":
        snapshot, manifest = create_runtime_snapshot(PROJECT_ROOT, config)
        verification = verify_runtime_snapshot(snapshot)
        if verification["status"] != "PASS":
            raise RuntimeError("新快照生成后校验失败：" + verification["reason"])
        payload = {
            "status": "PASS",
            "action": "CREATE_AND_VERIFY",
            "snapshot": str(snapshot),
            "manifest": manifest,
            "verification": verification,
        }
    elif args.command == "verify":
        payload = {"action": "VERIFY", **verify_runtime_snapshot(args.snapshot)}
    else:
        payload = {
            "action": "RESTORE_DRILL",
            **restore_snapshot_to_staging(PROJECT_ROOT, args.snapshot, args.target),
        }
    _write_latest_status(config, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("status") != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
