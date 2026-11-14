from __future__ import annotations

import json
import logging
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
            pass  # 可以忽略：重试循环：创建失败后用 iterdir 判断目录是否已可用，最多5次
        try:
            next(iter(path.iterdir()), None)
            return  # iterdir() 成功说明目录已存在且可用
        except OSError:
            pass  # 可以忽略：重试循环：目录仍不可用就等2秒再试，最后一次会抛出原始异常
        if attempt < 4:
            time.sleep(2)
    path.mkdir(parents=True, exist_ok=True)  # 最后一次，抛原始异常


def load_json_config(path: str | Path) -> dict[str, Any]:
    """读取 JSON 配置文件。

    读 config/config.json 时，如果同目录下有 research/research.json（研究用的段，2026-11-14 从 config.json 拆出），
    就把这些段并进来返回：研究和测试读到的和拆分前一样。QMT 包里只有 config/*.json，没有这个文件，
    所以 QMT 里的生产代码只看到生产段。
    研究配置只是“尽量并进来”（2026-11-15）：它读不了、格式坏了，只记一条警告、返回生产配置；两份有同名段时
    以生产为准。生产代码从不读研究段，所以研究文件出什么问题都不影响生产加载。
    """
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = get_project_root() / config_path
    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    if config_path.name == "config.json" and config_path.parent.name == "config" and isinstance(config, dict):
        research_path = config_path.parent / "research" / "research.json"
        if research_path.exists():
            try:
                with research_path.open("r", encoding="utf-8") as file:
                    research = json.load(file)
                if not isinstance(research, dict):
                    raise ValueError("不是 JSON 对象")
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning("研究配置 %s 读不了，只用生产配置：%s", research_path, exc)
                return config
            overlap = sorted(set(config) & set(research))
            if overlap:
                logging.getLogger(__name__).warning(
                    "config.json 和 research/research.json 有同名段 %s：以生产配置为准", overlap)
            config.update({key: value for key, value in research.items() if key not in config})
    return config
