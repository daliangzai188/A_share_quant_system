"""实盘组合认证门禁。

认证失败只能阻断新的买入计划，绝不能阻断已有持仓的卖出。认证文件由
``scripts/certify_current_executable_portfolio.py`` 生成，实盘状态机只负责按
fail-closed 口径读取和核对，不在运行时重新做历史回放。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class LiveCertificationCheck:
    ok: bool
    reason: str
    path: Path
    payload: dict[str, Any]


def _resolve_path(project_root: Path, value: Any) -> Path:
    path = Path(str(value or "").strip())
    return path if path.is_absolute() else project_root / path


def validate_live_certification(
    project_root: Path,
    model3_config: Mapping[str, Any],
) -> LiveCertificationCheck:
    """核对当前model=3是否有明确通过且场景一致的认证文件。"""

    raw_path = model3_config.get("certification_summary_path", "")
    path = _resolve_path(project_root, raw_path)
    if not str(raw_path).strip():
        return LiveCertificationCheck(False, "未配置认证文件路径", path, {})
    if not path.exists():
        return LiveCertificationCheck(False, f"认证文件不存在：{path}", path, {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return LiveCertificationCheck(False, f"认证文件不可读：{exc}", path, {})
    if not isinstance(payload, dict):
        return LiveCertificationCheck(False, "认证文件根节点不是对象", path, {})

    required_status = str(model3_config.get("certification_required_status", "PASS")).upper()
    actual_status = str(payload.get("status", "")).upper()
    if actual_status != required_status:
        return LiveCertificationCheck(
            False,
            f"认证状态={actual_status or '缺失'}，要求={required_status}",
            path,
            payload,
        )

    expected_scenario = str(model3_config.get("certification_expected_scenario", "")).strip()
    actual_scenario = str(payload.get("scenario", "")).strip()
    if not expected_scenario:
        return LiveCertificationCheck(False, "未配置认证期望场景", path, payload)
    if actual_scenario != expected_scenario:
        return LiveCertificationCheck(
            False,
            f"认证场景={actual_scenario or '缺失'}，配置期望={expected_scenario}",
            path,
            payload,
        )
    if payload.get("current_executable") is not True:
        return LiveCertificationCheck(False, "认证文件未明确标记当前可执行场景", path, payload)

    return LiveCertificationCheck(
        True,
        f"认证通过：{actual_scenario}",
        path,
        payload,
    )
