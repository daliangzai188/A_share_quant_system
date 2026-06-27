from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.qmt_adapter import QMTBrokerAdapter
from src.utils.config import load_json_config


def main() -> int:
    parser = argparse.ArgumentParser(description="QMT账户可用性探测。")
    parser.add_argument("--preferred-only", action="store_true", help="只尝试配置里的首选 path/session。")
    args = parser.parse_args()

    adapter = None
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
        adapter.connect(preferred_only=args.preferred_only)
        account = adapter.query_account()
        positions = adapter.query_positions()
        payload = {
            "ok": True,
            "account_id": account.account_id,
            "available_cash": account.available_cash,
            "position_count": len(positions or []),
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
