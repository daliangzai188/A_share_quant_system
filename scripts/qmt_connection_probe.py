from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.qmt_adapter import QMTBrokerAdapter
from src.utils.config import load_json_config


def main() -> int:
    parser = argparse.ArgumentParser(description="QMT账户可用性探测。")
    parser.add_argument("--preferred-only", action="store_true", help="只尝试配置里的首选 path/session。")
    parser.add_argument("--qmt-path", default="", help="覆盖 QMT_PATH，用于优先尝试上次成功路径。")
    parser.add_argument("--session-id", default="", help="覆盖 QMT_SESSION_ID，用于优先尝试上次成功 session。")
    parser.add_argument("--skip-positions", action="store_true", help="只验证账户资产，不查询持仓。启动门禁用它提速。")
    args = parser.parse_args()

    adapter = None
    started_at = time.perf_counter()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        if args.qmt_path:
            os.environ[str(broker_cfg.get("qmt_path_env", "QMT_PATH"))] = args.qmt_path
        if args.session_id:
            os.environ[str(broker_cfg.get("session_id_env", "QMT_SESSION_ID"))] = args.session_id
        config_loaded_at = time.perf_counter()
        adapter = QMTBrokerAdapter.from_config(broker_cfg)
        adapter.connect(preferred_only=args.preferred_only)
        connected_at = time.perf_counter()
        account = adapter.query_account()
        account_at = time.perf_counter()
        positions = [] if args.skip_positions else adapter.query_positions()
        positions_at = time.perf_counter()
        payload = {
            "ok": True,
            "account_id": account.account_id,
            "available_cash": account.available_cash,
            "position_count": len(positions or []),
            "qmt_path": getattr(adapter, "_active_qmt_path", ""),
            "session_id": getattr(adapter, "_active_session_id", ""),
            "timing_sec": {
                "config": round(config_loaded_at - started_at, 3),
                "connect": round(connected_at - config_loaded_at, 3),
                "account": round(account_at - connected_at, 3),
                "positions": round(positions_at - account_at, 3),
                "total": round(positions_at - started_at, 3),
            },
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 1
    finally:
        if adapter is not None:
            try:
                adapter.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
