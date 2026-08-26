"""策略D盘中路径检查点。

D候选依赖从09:30开始连续观察到的首次封板、炸板和最后回封事件。检查点只用于
同一台机器、同一交易日、同一策略代码/配置下的短时进程重启恢复；任何证据不完整
都必须拒绝恢复，不能用午后行情快照补造早盘路径。
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import time
from typing import Any, Mapping, Sequence
import uuid


D_CHECKPOINT_SCHEMA_VERSION = 1
D_CHECKPOINT_STATUS_READY = "READY"
D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS = "SCAN_IN_PROGRESS"
D_CHECKPOINT_STATUS_CLOSED = "CLOSED"
D_CHECKPOINT_STATUS_RECOVERY_BLOCKED = "RECOVERY_BLOCKED"

# Windows 上 Syncthing、杀毒软件等短暂读取检查点时，os.replace/unlink 可能返回
# WinError 32（共享冲突）。这是持久化 I/O 故障，不等于盘中行情路径缺失；先在
# 2 秒内重试，仍失败再由恢复阻断标记保护旧 READY，禁止新进程读取陈旧状态。
_CHECKPOINT_IO_RETRY_ATTEMPTS = 20
_CHECKPOINT_IO_RETRY_DELAY_SECONDS = 0.1

_D_RUNTIME_CODE_FILES = (
    "scripts/monitor_strategy_d_intraday.py",
    "src/strategy_d_checkpoint.py",
    "src/strategy_d_factor_rules.py",
    "src/strategy_d_minute_alignment.py",
    "src/strategy_d_spec.py",
    "src/qmt_adapter.py",
    "src/fill_model.py",
    "src/data_cleaner.py",
)

_D_STRATEGY_CONFIG_KEYS = (
    "factor_release_path",
    "allowed_market_segments",
    "min_fill_probability",
    "min_open_times",
    "ranking_rule",
    "position_pct",
    "preferred_open_times",
    "max_open_times",
    "first_time_buckets",
    "tail_reseal_hhmm",
    "tracking_start_hhmm",
    "signal_start_hhmm",
    "cancel_hhmm",
    "sentiment_current_sealed_min",
    "sentiment_current_sealed_max",
    "checkpoint_max_age_sec",
)


@dataclass(frozen=True)
class StrategyDCheckpointCheck:
    ok: bool
    reason: str
    path: Path
    payload: dict[str, Any]


def strategy_d_checkpoint_path(project_root: Path, trade_date: str) -> Path:
    return (
        Path(project_root)
        / "data"
        / "state"
        / f"strategy_d_intraday_checkpoint_{trade_date}.json"
    )


def strategy_d_checkpoint_recovery_block_path(checkpoint_path: Path) -> Path:
    """返回检查点恢复阻断标记；标记存在时旧 READY 不得用于盘中恢复。"""

    path = Path(checkpoint_path)
    return path.with_name(f"{path.name}.recovery_blocked")


def strategy_d_machine_fingerprint() -> str:
    """只保存不可逆机器摘要，防止Syncthing把另一台设备的状态拿来恢复。"""

    identity = f"{platform.node()}|{platform.system()}|{platform.machine()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def strategy_d_universe_sha256(universe: Sequence[str]) -> str:
    normalized = sorted({str(code).strip().upper() for code in universe if str(code).strip()})
    encoded = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strategy_d_market_context_sha256(
    universe: Sequence[str],
    yesterday_limit_codes: Sequence[str] | set[str],
    name_map: Mapping[str, Any],
    circ_mv_map: Mapping[str, Any],
    previous_day_amount_map: Mapping[str, Any] | None = None,
) -> str:
    """绑定首板身份、ST名称和流通市值，防止重启时静态选股上下文漂移。"""

    rows: list[list[Any]] = []
    for raw_code in sorted({str(code).strip().upper() for code in universe}):
        raw_circ_mv = circ_mv_map.get(raw_code, 0.0)
        try:
            circ_mv = float(raw_circ_mv or 0.0)
        except (TypeError, ValueError):
            circ_mv = 0.0
        if not math.isfinite(circ_mv):
            circ_mv = 0.0
        raw_previous_amount = (previous_day_amount_map or {}).get(raw_code, 0.0)
        try:
            previous_amount = float(raw_previous_amount or 0.0)
        except (TypeError, ValueError):
            previous_amount = 0.0
        if not math.isfinite(previous_amount):
            previous_amount = 0.0
        rows.append(
            [
                raw_code,
                str(name_map.get(raw_code, "") or ""),
                circ_mv,
                previous_amount,
            ]
        )
    payload = {
        "universe_rows": rows,
        "yesterday_limit_codes": sorted(
            {str(code).strip().upper() for code in yesterday_limit_codes}
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_file_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    if path.suffix.lower() in {".py", ".json", ".csv", ".md", ".txt", ".yaml", ".yml"}:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return payload


def strategy_d_runtime_fingerprint(
    project_root: Path,
    config: Mapping[str, Any],
) -> str:
    """绑定会改变D路径解释的代码与配置，部署改动后旧检查点自动作废。"""

    strategy_config = config.get("strategy_d", {})
    selected_strategy_config = {
        key: strategy_config.get(key) for key in _D_STRATEGY_CONFIG_KEYS
    }
    selected_fill_config = dict(config.get("fill_model", {}))
    canonical = json.dumps(
        {
            "strategy_d": selected_strategy_config,
            "fill_model": selected_fill_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical)
    root = Path(project_root)
    for relative in sorted(_D_RUNTIME_CODE_FILES):
        path = root / relative
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_file_bytes(path))
        digest.update(b"\0")
    release_relative = str(
        strategy_config.get(
            "factor_release_path", "config/strategy_d_factor_release.json"
        )
    )
    release_path = Path(release_relative)
    if not release_path.is_absolute():
        release_path = root / release_path
    if not release_path.exists() or not release_path.is_file():
        raise FileNotFoundError(release_path)
    digest.update(release_relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(_normalized_file_bytes(release_path))
    digest.update(b"\0")
    return digest.hexdigest()


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_retryable_checkpoint_io_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {
        5,   # access denied
        32,  # sharing violation
        33,  # lock violation
    }


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(1, _CHECKPOINT_IO_RETRY_ATTEMPTS + 1):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if (
                not _is_retryable_checkpoint_io_error(exc)
                or attempt >= _CHECKPOINT_IO_RETRY_ATTEMPTS
            ):
                raise
            time.sleep(_CHECKPOINT_IO_RETRY_DELAY_SECONDS)


def _unlink_with_retry(path: Path) -> None:
    for attempt in range(1, _CHECKPOINT_IO_RETRY_ATTEMPTS + 1):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if (
                not _is_retryable_checkpoint_io_error(exc)
                or attempt >= _CHECKPOINT_IO_RETRY_ATTEMPTS
            ):
                raise
            time.sleep(_CHECKPOINT_IO_RETRY_DELAY_SECONDS)


def write_strategy_d_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    """同目录临时文件+原子替换，禁止读到半份股票状态。"""

    normalized = dict(payload)
    normalized["schema_version"] = D_CHECKPOINT_SCHEMA_VERSION
    normalized["payload_sha256"] = _payload_sha256(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        _replace_with_retry(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # 主文件替换结果才决定检查点是否成功；临时文件清理失败不能覆盖原异常。
            pass


def block_strategy_d_checkpoint_recovery(
    checkpoint_path: Path,
    *,
    trade_date: str,
    reason: str,
    recorded_at: dt.datetime,
) -> Path:
    """阻断旧 READY 恢复，直到完整扫描成功写入新 READY 后显式清除。"""

    block_path = strategy_d_checkpoint_recovery_block_path(checkpoint_path)
    write_strategy_d_checkpoint(
        block_path,
        {
            "status": D_CHECKPOINT_STATUS_RECOVERY_BLOCKED,
            "resume_allowed": False,
            "trade_date": str(trade_date),
            "recorded_at": recorded_at.isoformat(),
            "reason": str(reason),
        },
    )
    return block_path


def clear_strategy_d_checkpoint_recovery_block(checkpoint_path: Path) -> None:
    """新 READY 已原子落盘后清除阻断标记；Windows 文件占用同样有界重试。"""

    _unlink_with_retry(strategy_d_checkpoint_recovery_block_path(checkpoint_path))


def invalidate_strategy_d_checkpoint(
    path: Path,
    *,
    trade_date: str,
    status: str,
    reason: str,
    recorded_at: dt.datetime,
    machine_fingerprint: str,
    runtime_fingerprint: str,
) -> None:
    """扫描开始前原子覆盖旧READY；进程若在半轮扫描中崩溃就不能恢复。"""

    # 不先 unlink：os.replace 本身即为原子覆盖。先删除会扩大“既无旧检查点、又无
    # 新状态”的窗口，也更容易在 Windows 文件读取锁下触发 WinError 32。
    write_strategy_d_checkpoint(
        path,
        {
            "status": str(status),
            "resume_allowed": False,
            "trade_date": str(trade_date),
            "recorded_at": recorded_at.isoformat(),
            "reason": str(reason),
            "machine_fingerprint": str(machine_fingerprint),
            "runtime_fingerprint": str(runtime_fingerprint),
        },
    )


def _parse_datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def inspect_strategy_d_checkpoint(
    path: Path,
    *,
    trade_date: str,
    now: dt.datetime,
    max_age_seconds: float,
    expected_tracking_start_hhmm: int,
    expected_machine_fingerprint: str,
    expected_runtime_fingerprint: str,
    expected_universe_sha256: str | None = None,
    expected_universe_size: int | None = None,
    expected_market_context_sha256: str | None = None,
) -> StrategyDCheckpointCheck:
    """验证恢复证据；这里只要一项存疑就返回不可恢复。"""

    def reject(reason: str, payload: dict[str, Any] | None = None) -> StrategyDCheckpointCheck:
        return StrategyDCheckpointCheck(False, reason, path, payload or {})

    recovery_block_path = strategy_d_checkpoint_recovery_block_path(path)
    if recovery_block_path.exists():
        block_reason = "原因不可读取"
        try:
            block_payload = json.loads(recovery_block_path.read_text(encoding="utf-8"))
            block_reason = str(block_payload.get("reason", block_reason))
        except Exception:
            pass
        return reject(f"D检查点恢复仍被I/O保护标记阻断:{block_reason}")
    if not path.exists():
        return reject("不存在D盘中路径检查点")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return reject(f"D检查点无法解析:{exc}")
    if not isinstance(raw, dict):
        return reject("D检查点根节点不是对象")
    payload = dict(raw)
    if int(payload.get("schema_version", 0) or 0) != D_CHECKPOINT_SCHEMA_VERSION:
        return reject("D检查点版本不兼容", payload)
    expected_checksum = str(payload.get("payload_sha256", ""))
    if not expected_checksum or expected_checksum != _payload_sha256(payload):
        return reject("D检查点完整性摘要不一致", payload)
    if str(payload.get("status", "")) != D_CHECKPOINT_STATUS_READY:
        return reject(
            f"D检查点不是可恢复状态:{payload.get('status', 'UNKNOWN')}", payload
        )
    if not bool(payload.get("resume_allowed", False)):
        return reject("D检查点明确禁止恢复扫描", payload)
    if str(payload.get("trade_date", "")) != str(trade_date):
        return reject("D检查点不属于当前交易日", payload)
    if str(payload.get("machine_fingerprint", "")) != str(expected_machine_fingerprint):
        return reject("D检查点来自另一台设备", payload)
    if str(payload.get("runtime_fingerprint", "")) != str(expected_runtime_fingerprint):
        return reject("D策略代码或配置已变化", payload)
    if int(payload.get("tracking_start_hhmm", 0) or 0) != int(expected_tracking_start_hhmm):
        return reject("D检查点跟踪起点与认证口径不一致", payload)
    original_start = int(payload.get("original_session_start_hhmm", 9999) or 9999)
    if original_start > int(expected_tracking_start_hhmm):
        return reject("D原始监控没有在09:30前启动", payload)
    if bool(payload.get("path_integrity_failed", True)):
        return reject("D检查点记录的全日路径已失效", payload)
    if bool(payload.get("order_placed", False)) or payload.get("session_orders"):
        return reject("D已有委托，必须交由券商交易恢复链处理", payload)
    scan_round = int(payload.get("scan_round", 0) or 0)
    if scan_round <= 0:
        return reject("D检查点没有完成过全市场扫描", payload)
    last_scan_at = _parse_datetime(payload.get("last_complete_scan_at"))
    first_scan_at = _parse_datetime(payload.get("first_complete_scan_at"))
    if last_scan_at is None or first_scan_at is None:
        return reject("D检查点缺少带时区的完整扫描时间", payload)
    if last_scan_at < first_scan_at:
        return reject("D检查点扫描时间顺序异常", payload)
    if now.tzinfo is None:
        return reject("当前校验时间缺少时区", payload)
    age_seconds = (now.astimezone(dt.timezone.utc) - last_scan_at.astimezone(dt.timezone.utc)).total_seconds()
    if age_seconds < -2:
        return reject("D检查点时间晚于当前系统时间", payload)
    if age_seconds > float(max_age_seconds):
        return reject(
            f"D检查点已过期{age_seconds:.1f}秒>允许{float(max_age_seconds):.1f}秒",
            payload,
        )
    universe_size = int(payload.get("universe_size", 0) or 0)
    updated_count = int(payload.get("last_scan_updated_count", 0) or 0)
    if universe_size <= 0 or updated_count != universe_size:
        return reject("D最后一轮没有覆盖完整股票宇宙", payload)
    if expected_universe_size is not None and universe_size != int(expected_universe_size):
        return reject("D股票宇宙数量已变化", payload)
    if expected_universe_sha256 is not None and str(payload.get("universe_sha256", "")) != str(expected_universe_sha256):
        return reject("D股票宇宙成分已变化", payload)
    if expected_market_context_sha256 is not None and str(
        payload.get("market_context_sha256", "")
    ) != str(expected_market_context_sha256):
        return reject("D首板/ST/流通市值静态上下文已变化", payload)
    states = payload.get("states")
    if not isinstance(states, dict):
        return reject("D检查点逐票状态不是对象", payload)
    if int(payload.get("state_count", -1)) != len(states):
        return reject("D检查点逐票状态数量不一致", payload)
    for ts_code, state in states.items():
        if not isinstance(state, dict) or str(state.get("ts_code", "")) != str(ts_code):
            return reject("D检查点逐票状态键值不一致", payload)
    return StrategyDCheckpointCheck(
        True,
        f"D检查点有效：第{scan_round}轮，距今{max(age_seconds, 0.0):.1f}秒",
        path,
        payload,
    )
