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

from src.strict_asof import LOCKED_OOS, STRICT_ASOF_STANDARD_ID, WALK_FORWARD


@dataclass(frozen=True)
class LiveCertificationCheck:
    ok: bool
    reason: str
    path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class StrategyReleaseFreezeCheck:
    """当前认证是否仍绑定到人工确认过的冻结发布版本。"""

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
        "strategy_n": config.get("strategy_n", {}),
        "strategy_d": config.get("strategy_d", {}),
        "portfolio_certification": config.get("portfolio_certification", {}),
        "strict_asof": config.get("strict_asof", {}),
        "analysis": config.get("analysis", {}),
        "live_trade": selected_live_trade,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def certification_files_sha256(project_root: Path, files: list[str]) -> str:
    """按相对路径和内容生成跨Windows/Mac稳定摘要；缺文件直接失败。"""

    digest = hashlib.sha256()
    for value in sorted(str(item) for item in files):
        path = _resolve_path(project_root, value)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_certification_file_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()


_TEXT_HASH_SUFFIXES = frozenset(
    {".csv", ".json", ".md", ".py", ".txt", ".toml", ".yaml", ".yml"}
)


def _certification_file_bytes(path: Path) -> bytes:
    """文本文件统一换行符后再哈希，避免同一内容在Windows被误判漂移。"""

    payload = path.read_bytes()
    if path.suffix.lower() in _TEXT_HASH_SUFFIXES:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return payload


def certification_file_sha256(path: Path) -> str:
    """返回单个认证输入的跨平台稳定摘要。"""

    return hashlib.sha256(_certification_file_bytes(path)).hexdigest()


def certification_file_size(path: Path) -> int:
    """返回认证口径下的稳定字节数；文本文件不计Windows额外回车符。"""

    return len(_certification_file_bytes(path))


def _parse_datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _compact_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        dt.datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return ""
    return text


def validate_strategy_release_freeze(
    project_root: Path,
    certification_config: Mapping[str, Any],
    certification_payload: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> StrategyReleaseFreezeCheck:
    """验证认证文件没有脱离人工冻结的策略发布版本。

    历史认证脚本可以重复运行以刷新时效，但只有显式更新并提交冻结清单，才允许
    更换策略配置、候选代码、历史输入或腿序。这样可避免在同一历史窗口重新调参后，
    仅靠重跑认证就悄悄进入实盘。
    """

    raw_path = certification_config.get("strategy_release_freeze_path", "")
    path = _resolve_path(project_root, raw_path)
    if not str(raw_path).strip():
        return StrategyReleaseFreezeCheck(False, "未配置策略冻结清单路径", path, {})
    if not path.exists():
        return StrategyReleaseFreezeCheck(False, f"策略冻结清单不存在：{path}", path, {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return StrategyReleaseFreezeCheck(False, f"策略冻结清单不可读：{exc}", path, {})
    if not isinstance(payload, dict):
        return StrategyReleaseFreezeCheck(False, "策略冻结清单根节点不是对象", path, {})
    if int(payload.get("schema_version", 0) or 0) != 1:
        return StrategyReleaseFreezeCheck(False, "策略冻结清单版本不是1", path, payload)
    release_id = str(payload.get("release_id", "")).strip()
    if not release_id:
        return StrategyReleaseFreezeCheck(False, "策略冻结清单缺少release_id", path, payload)
    if str(payload.get("status", "")).upper() != "FROZEN":
        return StrategyReleaseFreezeCheck(False, "策略发布状态不是FROZEN", path, payload)

    expected_order = [
        str(value).upper()
        for value in certification_config.get("strategy_priority_order", [])
    ]
    frozen_order = [str(value).upper() for value in payload.get("strategy_priority_order", [])]
    if not expected_order:
        return StrategyReleaseFreezeCheck(False, "配置缺少结构化策略腿序", path, payload)
    if frozen_order != expected_order:
        return StrategyReleaseFreezeCheck(
            False,
            f"冻结腿序={frozen_order}，当前配置腿序={expected_order}",
            path,
            payload,
        )

    comparisons = {
        "certification_status": "status",
        "certification_scenario": "scenario",
        "config_sha256": "config_sha256",
        "code_sha256": "code_sha256",
        "input_sha256": "input_sha256",
    }
    for freeze_key, certification_key in comparisons.items():
        frozen_value = str(payload.get(freeze_key, "")).strip()
        certified_value = str(certification_payload.get(certification_key, "")).strip()
        if not frozen_value or frozen_value != certified_value:
            return StrategyReleaseFreezeCheck(
                False,
                f"冻结清单{freeze_key}与当前认证不一致",
                path,
                payload,
            )

    research_end = _compact_date(payload.get("research_input_end_date"))
    certified_end = _compact_date(certification_payload.get("input_end_date"))
    oos_start = _compact_date(payload.get("oos_start_date"))
    if not research_end or research_end != certified_end:
        return StrategyReleaseFreezeCheck(
            False, "冻结研究截止日与认证输入截止日不一致", path, payload
        )
    if not oos_start or oos_start <= research_end:
        return StrategyReleaseFreezeCheck(
            False, "样本外起始日必须晚于冻结研究截止日", path, payload
        )

    frozen_at = _parse_datetime(payload.get("frozen_at"))
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    if frozen_at is None:
        return StrategyReleaseFreezeCheck(False, "冻结时间缺失或格式错误", path, payload)
    if frozen_at > current.astimezone(dt.timezone.utc) + dt.timedelta(hours=1):
        return StrategyReleaseFreezeCheck(False, "冻结时间晚于当前时间", path, payload)

    return StrategyReleaseFreezeCheck(
        True,
        f"策略发布已冻结：{release_id}，样本外起点{oos_start}",
        path,
        payload,
    )


def validate_live_certification(
    project_root: Path,
    certification_config: Mapping[str, Any],
    *,
    full_config: Mapping[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> LiveCertificationCheck:
    """核对当前正式组合是否有明确通过且场景一致的认证文件。"""

    raw_path = certification_config.get("certification_summary_path", "")
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

    required_status = str(
        certification_config.get("certification_required_status", "PASS")
    ).upper()
    actual_status = str(payload.get("status", "")).upper()
    if actual_status != required_status:
        return LiveCertificationCheck(
            False,
            f"认证状态={actual_status or '缺失'}，要求={required_status}",
            path,
            payload,
        )

    expected_scenario = str(
        certification_config.get("certification_expected_scenario", "")
    ).strip()
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

    if bool(certification_config.get("certification_require_strict_asof", False)):
        certified_standard = str(payload.get("strict_asof_standard_id", "")).strip()
        certified_protocol = str(payload.get("research_protocol", "")).upper()
        if certified_standard != STRICT_ASOF_STANDARD_ID:
            return LiveCertificationCheck(False, "认证缺少当前严格as-of标准标识", path, payload)
        if payload.get("strict_asof_passed") is not True:
            return LiveCertificationCheck(False, "认证未通过严格as-of数据门禁", path, payload)
        if certified_protocol not in {LOCKED_OOS, WALK_FORWARD}:
            return LiveCertificationCheck(
                False,
                "认证不是冻结样本外或walk-forward协议，开发段收益不得发布",
                path,
                payload,
            )
        if payload.get("release_eligible") is not True:
            return LiveCertificationCheck(False, "严格as-of认证未标记可发布", path, payload)
        raw_audit_path = str(payload.get("strict_asof_audit_path", "")).strip()
        expected_audit_hash = str(payload.get("strict_asof_audit_sha256", "")).strip()
        if not raw_audit_path or not expected_audit_hash:
            return LiveCertificationCheck(False, "认证缺少严格as-of审计文件或哈希", path, payload)
        audit_path = _resolve_path(project_root, raw_audit_path)
        if not audit_path.exists() or not audit_path.is_file():
            return LiveCertificationCheck(False, f"严格as-of审计文件不存在：{audit_path}", path, payload)
        if certification_file_sha256(audit_path) != expected_audit_hash:
            return LiveCertificationCheck(False, "严格as-of审计文件哈希不一致", path, payload)
        try:
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return LiveCertificationCheck(False, f"严格as-of审计文件不可读：{exc}", path, payload)
        if (
            not isinstance(audit_payload, dict)
            or audit_payload.get("standard_id") != STRICT_ASOF_STANDARD_ID
            or audit_payload.get("strict_asof_passed") is not True
            or str(audit_payload.get("research_protocol", "")).upper()
            not in {LOCKED_OOS, WALK_FORWARD}
            or audit_payload.get("release_eligible") is not True
        ):
            return LiveCertificationCheck(False, "严格as-of审计内容不具备发布资格", path, payload)

    max_age_hours = float(
        certification_config.get("certification_max_age_hours", 0) or 0
    )
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

    if bool(certification_config.get("certification_require_hashes", False)):
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

    if bool(certification_config.get("require_strategy_release_freeze", False)):
        freeze = validate_strategy_release_freeze(
            project_root,
            certification_config,
            payload,
            now=now,
        )
        if not freeze.ok:
            return LiveCertificationCheck(
                False,
                f"策略发布冻结校验失败：{freeze.reason}",
                path,
                payload,
            )

    return LiveCertificationCheck(
        True,
        (
            f"认证通过且策略发布已冻结：{actual_scenario}"
            if bool(certification_config.get("require_strategy_release_freeze", False))
            else f"认证通过：{actual_scenario}"
        ),
        path,
        payload,
    )
