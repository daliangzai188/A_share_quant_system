#!/usr/bin/env python3
"""检查候选策略是否满足冻结总复利硬底线；不连接券商、不下单。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_compound_guard import (  # noqa: E402
    PASS_NONINFERIOR,
    REVIEW_WITHIN_FLOOR,
    evaluate_certification_candidate,
    load_json_object,
)


DEFAULT_POLICY = PROJECT_ROOT / "config" / "release_compound_floor.json"
DEFAULT_CERTIFICATION = (
    PROJECT_ROOT / "reports" / "current_portfolio_alignment" / "live_certification.json"
)
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "release_guard" / "latest_status.json"


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="检查候选策略总复利发布门禁")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--candidate-certification", default=str(DEFAULT_CERTIFICATION))
    parser.add_argument("--runtime-config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    policy = load_json_object(_resolve(args.policy))
    certification = load_json_object(_resolve(args.candidate_certification))
    runtime_config = load_json_object(_resolve(args.runtime_config))
    result = evaluate_certification_candidate(policy, certification, runtime_config)
    result["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    result["candidate_certification_path"] = str(
        _resolve(args.candidate_certification).relative_to(PROJECT_ROOT)
        if _resolve(args.candidate_certification).is_relative_to(PROJECT_ROOT)
        else _resolve(args.candidate_certification)
    )

    output = _resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == PASS_NONINFERIOR:
        return 0
    if result["status"] == REVIEW_WITHIN_FLOOR:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
