"""实盘关键运行状态快照、校验与隔离恢复演练。"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping


SUPPORTED_KINDS = {"file", "json", "csv", "sqlite"}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: Any) -> Path:
    path = Path(str(value or "").strip())
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"备份路径必须是项目内安全相对路径：{value}")
    return path


def _validate_json(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig") as handle:
        json.load(handle)


def _validate_csv(path: Path) -> None:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
    if not header or not any(str(value).strip() for value in header):
        raise ValueError(f"CSV缺少表头：{path}")


def _validate_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA quick_check").fetchall()
    finally:
        connection.close()
    if result != [("ok",)]:
        raise ValueError(f"SQLite quick_check失败：{path} {result}")


def _validate_kind(path: Path, kind: str) -> None:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"不支持的备份类型：{kind}")
    if kind == "json":
        _validate_json(path)
    elif kind == "csv":
        _validate_csv(path)
    elif kind == "sqlite":
        _validate_sqlite(path)


def _copy_sqlite_online(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # WAL库在最后一个进程退出后可能只剩主库文件。此时纯mode=ro连接无法
    # 重建-wal/-shm并完成恢复，backup()会报“unable to open database file”。
    # 允许SQLite完成连接级恢复后立刻启用query_only，业务层仍不能写源库。
    source_uri = source.resolve().as_uri() + "?mode=rw"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    _validate_sqlite(destination)


def _copy_regular_stable(source: Path, destination: Path, kind: str) -> None:
    """复制同一打开句柄中的稳定版本；检测并拒绝原地并发写。"""

    for _attempt in range(3):
        with source.open("rb") as source_handle, destination.open("wb") as target_handle:
            before = os.fstat(source_handle.fileno())
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            after = os.fstat(source_handle.fileno())
        stable = (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and destination.stat().st_size == after.st_size
        )
        if stable:
            _validate_kind(destination, kind)
            return
        destination.unlink(missing_ok=True)
    raise RuntimeError(f"源文件复制期间持续变化，拒绝生成不一致快照：{source}")


def _copy_item(source: Path, destination: Path, kind: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "sqlite":
        _copy_sqlite_online(source, destination)
    else:
        _copy_regular_stable(source, destination, kind)


def _normalized_items(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    if int(config.get("schema_version", 0)) != 1:
        raise ValueError("运行状态备份配置版本必须为1")
    raw_items = config.get("items", [])
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("运行状态备份配置items不能为空")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("运行状态备份items成员必须是对象")
        relative = _relative_path(raw.get("path"))
        key = relative.as_posix()
        if key in seen:
            raise ValueError(f"运行状态备份路径重复：{key}")
        seen.add(key)
        kind = str(raw.get("kind", "file")).lower()
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"不支持的备份类型：{kind}")
        items.append(
            {"path": key, "kind": kind, "required": bool(raw.get("required", False))}
        )
    return items


def _snapshot_id(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def create_runtime_snapshot(
    project_root: Path,
    config: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """创建完整快照；任一必需文件缺失时不留下可误用的半快照。"""

    root = project_root.resolve()
    items = _normalized_items(config)
    snapshot_root = root / _relative_path(config.get("snapshot_root"))
    identifier = _snapshot_id(now)
    final = snapshot_root / identifier
    temporary = snapshot_root / f".{identifier}.{os.getpid()}.tmp"
    if final.exists() or temporary.exists():
        raise FileExistsError(f"快照目录已存在：{final}")
    temporary.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    missing_required: list[str] = []
    try:
        for item in items:
            relative = Path(item["path"])
            source = root / relative
            destination = temporary / "files" / relative
            if not source.exists() or not source.is_file():
                records.append(
                    {
                        **item,
                        "present": False,
                        "size": 0,
                        "sha256": "",
                        "snapshot_path": "",
                    }
                )
                if item["required"]:
                    missing_required.append(item["path"])
                continue
            _copy_item(source, destination, item["kind"])
            records.append(
                {
                    **item,
                    "present": True,
                    "size": int(destination.stat().st_size),
                    "sha256": _sha256(destination),
                    "snapshot_path": (Path("files") / relative).as_posix(),
                }
            )
        if missing_required:
            raise FileNotFoundError("缺少必需运行状态：" + "、".join(missing_required))
        manifest = {
            "schema_version": 1,
            "status": "PASS",
            "snapshot_id": identifier,
            "created_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
            "consistency": "FILEWISE_ATOMIC_SQLITE_ONLINE_BACKUP_NOT_GLOBAL_TRANSACTION",
            "source_root": str(root),
            "file_count": int(sum(bool(record["present"]) for record in records)),
            "required_file_count": int(sum(bool(record["required"]) for record in records)),
            "optional_missing_count": int(
                sum(not record["present"] and not record["required"] for record in records)
            ),
            "files": records,
            "note": "每个文件已校验；运行中跨文件不保证同一事务时点，隔离恢复后仍须与券商对账。",
        }
        _atomic_json(temporary / "manifest.json", manifest)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, final)
        return final, manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_runtime_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """校验清单、路径、大小、哈希和文件内部结构。"""

    snapshot = snapshot_dir.resolve()
    manifest_path = snapshot / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "reason": f"清单不可读：{exc}", "snapshot": str(snapshot)}
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        return {"status": "FAIL", "reason": "清单根节点不是对象", "snapshot": str(snapshot)}
    if int(manifest.get("schema_version", 0)) != 1:
        errors.append("清单版本不是1")
    if str(manifest.get("status", "")).upper() != "PASS":
        errors.append("清单状态不是PASS")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        errors.append("清单文件列表为空或格式错误")
        raw_files = []
    present_count = sum(
        bool(item.get("present", False)) for item in raw_files if isinstance(item, Mapping)
    )
    required_count = sum(
        bool(item.get("required", False)) for item in raw_files if isinstance(item, Mapping)
    )
    optional_missing_count = sum(
        not bool(item.get("present", False)) and not bool(item.get("required", False))
        for item in raw_files
        if isinstance(item, Mapping)
    )
    if int(manifest.get("file_count", -1)) != present_count:
        errors.append("清单已备份文件数不一致")
    if int(manifest.get("required_file_count", -1)) != required_count:
        errors.append("清单必需文件数不一致")
    if int(manifest.get("optional_missing_count", -1)) != optional_missing_count:
        errors.append("清单可选缺失文件数不一致")
    verified = 0
    seen_paths: set[str] = set()
    for item in raw_files:
        try:
            if not isinstance(item, Mapping):
                raise ValueError("清单文件记录不是对象")
            relative = _relative_path(item.get("path"))
            relative_key = relative.as_posix()
            if relative_key in seen_paths:
                raise ValueError(f"清单路径重复：{relative_key}")
            seen_paths.add(relative_key)
            snapshot_relative = item.get("snapshot_path")
            present = bool(item.get("present", False))
            if not present:
                if bool(item.get("required", False)):
                    errors.append(f"清单标记必需文件缺失：{relative.as_posix()}")
                continue
            stored = snapshot / _relative_path(snapshot_relative)
            if not stored.exists() or not stored.is_file():
                errors.append(f"快照文件不存在：{relative.as_posix()}")
                continue
            if int(stored.stat().st_size) != int(item.get("size", -1)):
                errors.append(f"文件大小不一致：{relative.as_posix()}")
                continue
            if _sha256(stored) != str(item.get("sha256", "")):
                errors.append(f"SHA256不一致：{relative.as_posix()}")
                continue
            _validate_kind(stored, str(item.get("kind", "file")).lower())
            verified += 1
        except Exception as exc:
            errors.append(f"{item.get('path', '')}校验失败：{exc}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "reason": "全部快照文件校验通过" if not errors else "；".join(errors),
        "snapshot": str(snapshot),
        "snapshot_id": str(manifest.get("snapshot_id", "")),
        "verified_file_count": int(verified),
        "error_count": int(len(errors)),
        "errors": errors,
    }


def restore_snapshot_to_staging(
    project_root: Path,
    snapshot_dir: Path,
    staging_root: Path,
) -> dict[str, Any]:
    """只恢复到项目目录之外的新隔离目录；拒绝覆盖任意已有目录。"""

    root = project_root.resolve()
    target = staging_root.resolve()
    if target == root or root in target.parents:
        raise ValueError("隔离恢复目录不得等于或位于生产项目目录内")
    if target.exists():
        raise FileExistsError(f"隔离恢复目录必须尚不存在：{target}")
    verification = verify_runtime_snapshot(snapshot_dir)
    if verification["status"] != "PASS":
        raise RuntimeError("快照校验失败，拒绝恢复：" + verification["reason"])
    snapshot = snapshot_dir.resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8-sig"))
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"隔离恢复临时目录已存在：{temporary}")
    temporary.mkdir(parents=True, exist_ok=False)
    restored: list[dict[str, Any]] = []
    try:
        for item in manifest.get("files", []):
            if not bool(item.get("present", False)):
                continue
            relative = _relative_path(item.get("path"))
            source = snapshot / _relative_path(item.get("snapshot_path"))
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            _validate_kind(destination, str(item.get("kind", "file")))
            if _sha256(destination) != str(item.get("sha256", "")):
                raise ValueError(f"隔离恢复后哈希不一致：{relative.as_posix()}")
            restored.append(
                {
                    "path": relative.as_posix(),
                    "size": int(destination.stat().st_size),
                    "sha256": _sha256(destination),
                }
            )
        report = {
            "schema_version": 1,
            "status": "PASS",
            "mode": "ISOLATED_RESTORE_DRILL_ONLY",
            "snapshot_id": str(manifest.get("snapshot_id", "")),
            "production_root_untouched": True,
            "restored_file_count": int(len(restored)),
            "files": restored,
            "note": "隔离恢复通过不代表可跳过券商对账；真实灾难恢复后必须先dry_run核对持仓和活动委托。",
        }
        _atomic_json(temporary / "RESTORE_DRILL_REPORT.json", report)
        os.replace(temporary, target)
        report["staging_root"] = str(target)
        return report
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
