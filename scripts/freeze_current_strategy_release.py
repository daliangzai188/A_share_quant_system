#!/usr/bin/env python3
"""显式冻结当前已认证策略发布；不连接券商、不下单。

该脚本故意不由认证脚本自动调用。策略配置、候选代码、历史输入或腿序变化后，
必须先完成认证，再提供发布编号、变更原因和新的样本外起点，才能更新冻结清单。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live_certification import validate_live_certification  # noqa: E402
from src.release_compound_guard import (  # noqa: E402
    REVIEW_WITHIN_FLOOR,
    enforce_release_decision,
    evaluate_certification_candidate,
    load_json_object,
)


CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
COMPOUND_POLICY_PATH = PROJECT_ROOT / "config" / "release_compound_floor.json"


def _compact_date(value: str) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        parsed = dt.datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"日期必须为YYYYMMDD：{value}") from exc
    return parsed.strftime("%Y%m%d")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_freeze_payload(
    config: dict[str, Any],
    certification: dict[str, Any],
    *,
    release_id: str,
    change_reason: str,
    oos_start_date: str,
    baseline_commit: str,
    compound_guard: dict[str, Any] | None = None,
    compound_reduction_accepted: bool = False,
) -> dict[str, Any]:
    model3 = config.get("strategy_model3", {})
    research_end = _compact_date(str(certification.get("input_end_date", "")))
    oos_start = _compact_date(oos_start_date)
    if oos_start <= research_end:
        raise ValueError(
            f"样本外起始日{oos_start}必须晚于冻结研究截止日{research_end}"
        )
    release = str(release_id or "").strip()
    reason = str(change_reason or "").strip()
    if not release:
        raise ValueError("release_id不能为空")
    if len(reason) < 8:
        raise ValueError("change_reason至少8个字符，必须说明为什么允许该版本进入实盘")
    order = [str(value).upper() for value in model3.get("strategy_priority_order", [])]
    if not order:
        raise ValueError("strategy_model3.strategy_priority_order不能为空")
    payload = {
        "schema_version": 1,
        "status": "FROZEN",
        "release_id": release,
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline_strategy_commit": str(baseline_commit or "").strip() or _git_commit(),
        "change_reason": reason,
        "strategy_priority_order": order,
        "research_input_end_date": research_end,
        "oos_start_date": oos_start,
        "certification_status": str(certification.get("status", "")),
        "certification_scenario": str(certification.get("scenario", "")),
        "config_sha256": str(certification.get("config_sha256", "")),
        "code_sha256": str(certification.get("code_sha256", "")),
        "input_sha256": str(certification.get("input_sha256", "")),
        "capacity_certified": bool(certification.get("capacity_certified", False)),
        "risk_acceptance_note": "；".join(
            note
            for note in (
                str(certification.get("e_gate_risk_acceptance_note", "")).strip(),
                str(certification.get("m_live_risk_acceptance_note", "")).strip(),
            )
            if note
        ),
        "e_strategy_leg": str(certification.get("e_strategy_leg", "E")),
        "e_strategy_variant": str(
            certification.get("e_strategy_variant", "E_CURRENT")
        ),
        "e_complete_sample_candidate_count_before_gate": int(
            certification.get("e_complete_sample_candidate_count_before_gate", 0)
        ),
        "e_complete_sample_candidate_count_after_gate": int(
            certification.get("e_complete_sample_candidate_count_after_gate", 0)
        ),
        "e_gate_noninferiority_passed": bool(
            certification.get("e_gate_noninferiority_passed", False)
        ),
        "note": (
            "冻结仅证明该版本与认证输入、代码、配置一致；样本外结果从oos_start_date起累计，"
            "不得用后续结果回填或重写本清单。当前按82.5%正式实盘；"
            "容量未认证不等于小资金模式，扩大资金前必须先完成容量验证。"
        ),
    }
    if compound_guard is not None:
        payload["compound_guard"] = {
            "policy_id": str(compound_guard.get("policy_id", "")),
            "status": str(compound_guard.get("status", "")),
            "anchor_equity_multiple": float(
                compound_guard.get("anchor_equity_multiple", 0.0)
            ),
            "candidate_equity_multiple": float(
                compound_guard.get("candidate_equity_multiple", 0.0)
            ),
            "retained_ratio": float(compound_guard.get("retained_ratio", 0.0)),
            "hard_floor_ratio": float(compound_guard.get("hard_floor_ratio", 0.0)),
            "hard_floor_multiple": float(
                compound_guard.get("hard_floor_multiple", 0.0)
            ),
            "compound_reduction_accepted": bool(compound_reduction_accepted),
            "reason": str(compound_guard.get("reason", "")),
        }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="冻结当前已认证策略发布")
    parser.add_argument("--release-id", required=True, help="唯一发布编号")
    parser.add_argument("--change-reason", required=True, help="本次发布原因，至少8个字符")
    parser.add_argument("--oos-start-date", required=True, help="新样本外起点YYYYMMDD")
    parser.add_argument("--baseline-commit", default="", help="策略行为基线提交，默认HEAD")
    parser.add_argument("--replace", action="store_true", help="显式替换已有冻结清单")
    parser.add_argument(
        "--accept-compound-reduction-within-floor",
        action="store_true",
        help="候选复利处于当前基准70%%至100%%时，显式接受复利下降；低于70%%仍不可发布",
    )
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    model3 = dict(config.get("strategy_model3", {}))
    raw_freeze_path = str(model3.get("strategy_release_freeze_path", "")).strip()
    if not raw_freeze_path:
        raise RuntimeError("未配置strategy_release_freeze_path")
    freeze_path = Path(raw_freeze_path)
    if not freeze_path.is_absolute():
        freeze_path = PROJECT_ROOT / freeze_path
    if freeze_path.exists() and not args.replace:
        raise FileExistsError(f"冻结清单已存在；如确认发布新版本，请加--replace：{freeze_path}")

    # 创建冻结清单前先关闭冻结要求，只核对认证本身，避免首次冻结形成循环依赖。
    model3_without_freeze = dict(model3)
    model3_without_freeze["require_strategy_release_freeze"] = False
    check = validate_live_certification(
        PROJECT_ROOT,
        model3_without_freeze,
        full_config=config,
    )
    if not check.ok:
        raise RuntimeError(f"当前认证未通过，禁止冻结：{check.reason}")
    compound_policy = load_json_object(COMPOUND_POLICY_PATH)
    compound_guard = evaluate_certification_candidate(
        compound_policy,
        check.payload,
        config,
    )
    enforce_release_decision(
        compound_guard,
        accept_reduction_within_floor=args.accept_compound_reduction_within_floor,
    )
    payload = build_freeze_payload(
        config,
        check.payload,
        release_id=args.release_id,
        change_reason=args.change_reason,
        oos_start_date=args.oos_start_date,
        baseline_commit=args.baseline_commit,
        compound_guard=compound_guard,
        compound_reduction_accepted=(
            compound_guard["status"] == REVIEW_WITHIN_FLOOR
            and args.accept_compound_reduction_within_floor
        ),
    )
    freeze_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = freeze_path.with_suffix(freeze_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(freeze_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
