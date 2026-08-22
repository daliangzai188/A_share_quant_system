#!/usr/bin/env python3
"""执行策略D历史L2供应商样本的付款前内容验收。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategy_d_l2_sample_acceptance import validate_sample_package  # noqa: E402


DEFAULT_SAMPLE_ROOT = ROOT / "data/research/strategy_d_l2_vendor_sample"
DEFAULT_OUTPUT = (
    ROOT / "reports/strategy_d_intraday_research/l2_vendor_sample_acceptance.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验收D历史L2供应商三市场真实样本")
    parser.add_argument("--sample-root", type=Path, default=DEFAULT_SAMPLE_ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = validate_sample_package(
        sample_root=args.sample_root,
        manifest_path=args.manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "passed": report["passed"],
                "passed_sample_count": report["passed_sample_count"],
                "expected_sample_count": report["expected_sample_count"],
                "missing_sample_count": report["missing_sample_count"],
                "invalid_sample_count": report["invalid_sample_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告：{args.output}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
