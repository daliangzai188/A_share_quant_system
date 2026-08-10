"""实盘组合认证门禁。

认证失败只能阻断新的买入计划，绝不能阻断已有持仓的卖出。认证文件由
``scripts/certify_current_executable_portfolio.py`` 生成，实盘状态机只负责按
fail-closed 口径读取和核对，不在运行时重新做历史回放。
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
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


def certification_config_sha256(config: Mapping[str, Any]) -> str:
    """只哈希会改变组合选择、仓位、费用和实盘成交的配置。"""

    live_trade = config.get("live_trade", {})
    selected_live_trade = {
        key: live_trade.get(key)
        for key in (
            "max_position_pct",
            "max_total_position_pct",
            "total_liquidity_cap_pct",
            "liquidity_cap_fail_closed",
            "same_stock_skip_enabled",
            "fill_confirm_enabled",
        )
    }
    payload = {
        "active_strategy_profile": config.get("active_strategy_profile", {}),
        "strategy_m": config.get("strategy_m", {}),
        "strategy_model3": config.get("strategy_model3", {}),
        "strategy_d": config.get("strategy_d", {}),
        "portfolio_certification": config.get("portfolio_certification", {}),
        "analysis": config.get("analysis", {}),
        "live_trade": selected_live_trade,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def certification_files_sha256(project_root: Path, files: list[str]) -> str:
    """按相对路径和内容生成稳定摘要；缺文件直接失败。"""

    digest = hashlib.sha256()
    for value in sorted(str(item) for item in files):
        path = _resolve_path(project_root, value)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def validate_live_certification(
    project_root: Path,
    model3_config: Mapping[str, Any],
    *,
    full_config: Mapping[str, Any] | None = None,
    now: dt.datetime | None = None,
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

    max_age_hours = float(model3_config.get("certification_max_age_hours", 0) or 0)
    if max_age_hours > 0:
        generated_at = _parse_datetime(payload.get("generated_at"))
        if generated_at is None:
            return LiveCertificationCheck(False, "认证生成时间缺失或格式错误", path, payload)
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        age_hours = (current.astimezone(dt.timezone.utc) - generated_at).total_seconds() / 3600
        if age_hours < -1:
            return LiveCertificationCheck(False, "认证时间晚于当前时间", path, payload)
        if age_hours > max_age_hours:
            return LiveCertificationCheck(
                False,
                f"认证已过期：{age_hours:.1f}小时，允许{max_age_hours:.1f}小时",
                path,
                payload,
            )

    if bool(model3_config.get("certification_require_hashes", False)):
        if full_config is None:
            return LiveCertificationCheck(False, "认证要求配置哈希但未传入完整配置", path, payload)
        expected_config_hash = str(payload.get("config_sha256", ""))
        actual_config_hash = certification_config_sha256(full_config)
        if not expected_config_hash or expected_config_hash != actual_config_hash:
            return LiveCertificationCheck(False, "当前配置与认证时配置不一致", path, payload)
        for label in ("code", "input"):
            files = payload.get(f"{label}_files")
            expected_hash = str(payload.get(f"{label}_sha256", ""))
            if not isinstance(files, list) or not files or not expected_hash:
                return LiveCertificationCheck(False, f"认证缺少{label}文件清单或哈希", path, payload)
            try:
                actual_hash = certification_files_sha256(project_root, files)
            except (OSError, FileNotFoundError) as exc:
                return LiveCertificationCheck(False, f"认证{label}文件缺失：{exc}", path, payload)
            if actual_hash != expected_hash:
                return LiveCertificationCheck(False, f"当前{label}文件与认证版本不一致", path, payload)

    return LiveCertificationCheck(
        True,
        f"认证通过：{actual_scenario}",
        path,
        payload,
    )
