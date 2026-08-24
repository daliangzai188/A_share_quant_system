from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


MANUAL_STOP_FILE_NAME = ".manual_stop.json"


def manual_stop_path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / MANUAL_STOP_FILE_NAME


def load_manual_stop(project_root: str | Path) -> dict[str, Any] | None:
    """读取人工停机标记。

    标记内容损坏时仍按“人工停机生效”处理，避免文件写入中断后无人值守任务
    反而自动启动真实交易进程。人工再次运行 start_windows.py 会显式清除它。
    """

    path = manual_stop_path(project_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "status": "MANUAL_STOP",
        "reason": "人工停机标记存在但内容不可读；为安全起见继续暂停自动启动",
        "marker_path": str(path),
    }


def write_manual_stop(
    project_root: str | Path,
    *,
    source: str,
    reason: str = "用户显式执行停止命令",
) -> Path:
    """原子写入人工停机标记，供 keeper 和 Windows 计划任务共同识别。"""

    path = manual_stop_path(project_root)
    payload = {
        "schema_version": 1,
        "status": "MANUAL_STOP",
        "stopped_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source),
        "reason": str(reason),
        "requested_by_pid": os.getpid(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def clear_manual_stop(project_root: str | Path) -> bool:
    """人工启动时清除暂停标记；返回此前是否存在标记。"""

    path = manual_stop_path(project_root)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed
