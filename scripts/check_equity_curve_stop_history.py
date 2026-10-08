#!/usr/bin/env python3
"""只读检查方案甲历史输入；不会联网、生成判定或调用券商。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.equity_curve_stop_history import audit_history  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-date", required=True)
    args = parser.parse_args()
    config = load_json_config(ROOT / "config/config.json")
    result = audit_history(ROOT, config, str(config["equity_curve_stop"].get("history_start", "20190101")), args.signal_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
