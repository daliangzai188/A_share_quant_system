from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def get_project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).absolute().parents[2]


def mkdir_p(path: Path) -> None:
    """WebDAV 兼容的 mkdir -p。
    WebDAV 上 mkdir(exist_ok=True) 内部会调 stat()，在目录已存在时抛 WinError 58。
    用 iterdir() 作为回退判断目录是否可用，最多重试 5 次。"""
    for attempt in range(5):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except OSError:
            pass
        try:
            next(iter(path.iterdir()), None)
            return  # iterdir() 成功说明目录已存在且可用
        except OSError:
            pass
        if attempt < 4:
            time.sleep(2)
    path.mkdir(parents=True, exist_ok=True)  # 最后一次，抛原始异常


def load_json_config(path: str | Path) -> dict[str, Any]:
    """读取 JSON 配置文件。"""
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = get_project_root() / config_path
    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)
