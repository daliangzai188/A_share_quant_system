"""策略发布复利硬底线门禁。

本模块只评估研究/认证结果，不连接券商、不修改候选、不下单。锚点固定为
用户确认时的当前实盘组合；后续优化不能通过滚动下调基准来绕过70%硬底线。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping


PASS_NONINFERIOR = "PASS_NONINFERIOR"
REVIEW_WITHIN_FLOOR = "REVIEW_REQUIRED_WITHIN_HARD_FLOOR"
REJECT_BELOW_FLOOR = "REJECT_BELOW_HARD_FLOOR"
REJECT_NOT_COMPARABLE = "REJECT_NOT_COMPARABLE"


class CompoundGuardError(ValueError):
    """复利门禁配置无效。"""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompoundGuardError(f"{field}不是有效数字") from exc
    if not number.is_finite():
        raise CompoundGuardError(f"{field}必须是有限数字")
    return number


def _date(value: Any, field: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise CompoundGuardError(f"{field}必须为YYYYMMDD")
    return text


def load_json_object(path: Path) -> dict[str, Any]:
    """读取JSON对象；根节点不是对象时直接失败。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompoundGuardError(f"无法读取{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise CompoundGuardError(f"{path}根节点不是对象")
    return payload


def validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """校验并标准化不可滚动下调的复利锚点。"""

    if int(policy.get("schema_version", 0) or 0) != 1:
        raise CompoundGuardError("复利门禁schema_version必须为1")
    if str(policy.get("status", "")).upper() != "ACTIVE":
        raise CompoundGuardError("复利门禁状态不是ACTIVE")
    policy_id = str(policy.get("policy_id", "")).strip()
    if not policy_id:
        raise CompoundGuardError("复利门禁缺少policy_id")
    anchor = policy.get("anchor")
    if not isinstance(anchor, Mapping):
        raise CompoundGuardError("复利门禁缺少anchor对象")

    required_text = ("release_id", "scenario", "input_sha256")
    for field in required_text:
        if not str(anchor.get(field, "")).strip():
            raise CompoundGuardError(f"anchor.{field}不能为空")
    start = _date(anchor.get("input_start_date"), "anchor.input_start_date")
    end = _date(anchor.get("input_end_date"), "anchor.input_end_date")
    if end < start:
        raise CompoundGuardError("anchor输入截止日早于起始日")

    anchor_multiple = _decimal(anchor.get("equity_multiple"), "anchor.equity_multiple")
    initial_equity = _decimal(anchor.get("initial_equity"), "anchor.initial_equity")
    position_pct = _decimal(anchor.get("position_pct"), "anchor.position_pct")
    floor_ratio = _decimal(policy.get("hard_floor_ratio"), "hard_floor_ratio")
    floor_multiple = _decimal(policy.get("hard_floor_multiple"), "hard_floor_multiple")
    automatic_ratio = _decimal(
        policy.get("automatic_release_ratio"), "automatic_release_ratio"
    )
    automatic_multiple = _decimal(
        policy.get("automatic_release_multiple"), "automatic_release_multiple"
    )
    if anchor_multiple <= 0 or initial_equity <= 0:
        raise CompoundGuardError("锚点复利和初始资金必须大于0")
    if not Decimal("0") < position_pct <= Decimal("1"):
        raise CompoundGuardError("锚点仓位比例必须在(0,1]内")
    if not Decimal("0") < floor_ratio <= Decimal("1"):
        raise CompoundGuardError("hard_floor_ratio必须在(0,1]内")
    if automatic_ratio < floor_ratio:
        raise CompoundGuardError("automatic_release_ratio不能低于hard_floor_ratio")

    tolerance = Decimal("0.000000001")
    expected_floor = anchor_multiple * floor_ratio
    expected_automatic = anchor_multiple * automatic_ratio
    if abs(floor_multiple - expected_floor) > tolerance:
        raise CompoundGuardError("hard_floor_multiple与锚点×hard_floor_ratio不一致")
    if abs(automatic_multiple - expected_automatic) > tolerance:
        raise CompoundGuardError(
            "automatic_release_multiple与锚点×automatic_release_ratio不一致"
        )

    statuses = {
        str(value).upper().strip()
        for value in policy.get("accepted_certification_statuses", [])
        if str(value).strip()
    }
    if not statuses:
        raise CompoundGuardError("accepted_certification_statuses不能为空")

    requirements = policy.get("comparison_requirements", {})
    if not isinstance(requirements, Mapping):
        raise CompoundGuardError("comparison_requirements必须是对象")
    required_checks = (
        "same_scenario",
        "same_input_window",
        "same_input_sha256",
        "same_initial_equity",
        "same_position_pct",
    )
    disabled = [name for name in required_checks if requirements.get(name) is not True]
    if disabled:
        raise CompoundGuardError("不可关闭可比性检查：" + "、".join(disabled))

    return {
        "policy_id": policy_id,
        "anchor_release_id": str(anchor["release_id"]).strip(),
        "scenario": str(anchor["scenario"]).strip(),
        "input_start_date": start,
        "input_end_date": end,
        "input_sha256": str(anchor["input_sha256"]).strip(),
        "initial_equity": initial_equity,
        "position_pct": position_pct,
        "anchor_multiple": anchor_multiple,
        "floor_ratio": floor_ratio,
        "floor_multiple": floor_multiple,
        "automatic_ratio": automatic_ratio,
        "automatic_multiple": automatic_multiple,
        "accepted_statuses": statuses,
    }


def evaluate_certification_candidate(
    policy: Mapping[str, Any],
    certification: Mapping[str, Any],
    runtime_config: Mapping[str, Any],
) -> dict[str, Any]:
    """按同口径检查候选组合，并给出发布级别结论。"""

    normalized = validate_policy(policy)
    issues: list[str] = []
    status = str(certification.get("status", "")).upper().strip()
    if status not in normalized["accepted_statuses"]:
        issues.append(f"认证状态{status or '缺失'}不在允许集合")
    if str(certification.get("scenario", "")).strip() != normalized["scenario"]:
        issues.append("认证场景与锚点不同")

    try:
        candidate_start = _date(
            certification.get("input_start_date"), "candidate.input_start_date"
        )
        candidate_end = _date(
            certification.get("input_end_date"), "candidate.input_end_date"
        )
    except CompoundGuardError as exc:
        issues.append(str(exc))
        candidate_start = ""
        candidate_end = ""
    if (
        candidate_start != normalized["input_start_date"]
        or candidate_end != normalized["input_end_date"]
    ):
        issues.append("认证历史窗口与锚点不同")
    if str(certification.get("input_sha256", "")).strip() != normalized["input_sha256"]:
        issues.append("认证历史输入摘要与锚点不同")

    portfolio = runtime_config.get("portfolio_certification", {})
    if not isinstance(portfolio, Mapping):
        portfolio = {}
    try:
        candidate_initial = _decimal(
            portfolio.get("initial_equity"), "portfolio_certification.initial_equity"
        )
        candidate_position = _decimal(
            portfolio.get("position_pct"), "portfolio_certification.position_pct"
        )
    except CompoundGuardError as exc:
        issues.append(str(exc))
        candidate_initial = Decimal("0")
        candidate_position = Decimal("0")
    if candidate_initial != normalized["initial_equity"]:
        issues.append("认证初始资金与锚点不同")
    if candidate_position != normalized["position_pct"]:
        issues.append("认证仓位比例与锚点不同")

    try:
        candidate_multiple = _decimal(
            certification.get("equity_multiple"), "candidate.equity_multiple"
        )
        if candidate_multiple <= 0:
            issues.append("候选复利倍数必须大于0")
    except CompoundGuardError as exc:
        issues.append(str(exc))
        candidate_multiple = Decimal("0")

    retained_ratio = (
        candidate_multiple / normalized["anchor_multiple"]
        if normalized["anchor_multiple"] > 0
        else Decimal("0")
    )
    hard_floor_passed = not issues and retained_ratio >= normalized["floor_ratio"]
    noninferior_passed = not issues and retained_ratio >= normalized["automatic_ratio"]
    if issues:
        decision_status = REJECT_NOT_COMPARABLE
        reason = "候选与冻结锚点不可直接比较：" + "；".join(issues)
    elif not hard_floor_passed:
        decision_status = REJECT_BELOW_FLOOR
        reason = (
            f"候选只保留当前基准的{float(retained_ratio):.2%}，"
            f"低于{float(normalized['floor_ratio']):.0%}硬底线，禁止发布。"
        )
    elif not noninferior_passed:
        decision_status = REVIEW_WITHIN_FLOOR
        reason = (
            f"候选保留当前基准的{float(retained_ratio):.2%}，未跌破硬底线，"
            "但总复利低于当前基准，只能人工审查并显式接受，不能自动发布。"
        )
    else:
        decision_status = PASS_NONINFERIOR
        reason = "候选总复利不低于当前冻结基准，复利非劣门禁通过。"

    return {
        "schema_version": 1,
        "policy_id": normalized["policy_id"],
        "anchor_release_id": normalized["anchor_release_id"],
        "status": decision_status,
        "reason": reason,
        "comparable": not issues,
        "comparability_issues": issues,
        "candidate_certification_status": status,
        "candidate_scenario": str(certification.get("scenario", "")),
        "candidate_input_start_date": candidate_start,
        "candidate_input_end_date": candidate_end,
        "candidate_equity_multiple": float(candidate_multiple),
        "anchor_equity_multiple": float(normalized["anchor_multiple"]),
        "retained_ratio": float(retained_ratio),
        "compound_reduction_ratio": float(Decimal("1") - retained_ratio),
        "hard_floor_ratio": float(normalized["floor_ratio"]),
        "hard_floor_multiple": float(normalized["floor_multiple"]),
        "hard_floor_passed": hard_floor_passed,
        "noninferior_ratio": float(normalized["automatic_ratio"]),
        "noninferior_multiple": float(normalized["automatic_multiple"]),
        "noninferior_passed": noninferior_passed,
        "automatic_release_allowed": decision_status == PASS_NONINFERIOR,
        "manual_review_eligible": decision_status == REVIEW_WITHIN_FLOOR,
        "historical_result_notice": (
            "历史复利仅用于同口径方案比较，不代表未来收益、真实成交容量或收益承诺。"
        ),
    }


def enforce_release_decision(
    decision: Mapping[str, Any], *, accept_reduction_within_floor: bool = False
) -> None:
    """为冻结发布执行硬阻断；70%至100%区间必须显式人工接受。"""

    status = str(decision.get("status", ""))
    reason = str(decision.get("reason", "复利门禁没有给出原因"))
    if status == PASS_NONINFERIOR:
        return
    if status == REVIEW_WITHIN_FLOOR and accept_reduction_within_floor:
        return
    if status == REVIEW_WITHIN_FLOOR:
        raise CompoundGuardError(
            reason
            + " 如已结合收益、回撤、样本外和过拟合证据确认，请显式添加"
            "--accept-compound-reduction-within-floor。"
        )
    raise CompoundGuardError(reason)
