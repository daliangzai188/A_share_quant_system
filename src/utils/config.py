from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def get_project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[2]


def load_json_config(path: str | Path) -> dict[str, Any]:
    """读取 JSON 配置文件。"""
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = get_project_root() / config_path
    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)
