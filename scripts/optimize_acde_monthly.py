#!/usr/bin/env python3
"""每月用最新36个完整自然月优化ACDE并停在用户审核点。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acde_monthly_research import (  # noqa: E402
    latest_completed_month_cutoff,
    load_monthly_config,
    run_monthly_research,
)


DEFAULT_CONFIG = ROOT / "config/acde_rolling_optimization.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ACDE月度最近三年精确现金研究")
    parser.add_argument("--cutoff", "--as-of", dest="cutoff", help="自然月末，格式YYYYMMDD")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config if args.config.is_absolute() else ROOT / args.config
    config = load_monthly_config(config_path)
    cutoff = str(args.cutoff or latest_completed_month_cutoff())
    if args.output_dir:
        output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    else:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        output_dir = ROOT / config["data"]["report_root"] / cutoff / f"run_{cutoff}_{stamp}"
    result = run_monthly_research(config, cutoff=cutoff, output_dir=output_dir)
    print(json.dumps({
        "status": result["status"],
        "output_dir": str(output_dir),
        "selected_scenario": result.get("selected_scenario", ""),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] not in {"NOT_READY", "BASELINE_REPRODUCTION_FAILED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
