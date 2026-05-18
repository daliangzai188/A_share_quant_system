from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str = "a_share_quant",
    log_dir: str | Path = "logs",
    log_file: str = "a_share_quant.log",
    level: str | int = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """创建同时输出到控制台和文件的日志对象。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    env_level = os.getenv("LOG_LEVEL")
    resolved_level = env_level or level
    if isinstance(resolved_level, str):
        resolved_level = getattr(logging, resolved_level.upper(), logging.INFO)

    logger.setLevel(resolved_level)
    logger.propagate = False

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(resolved_level)

    file_handler = RotatingFileHandler(
        log_path / log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(resolved_level)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取项目日志对象。"""
    if name:
        return logging.getLogger(f"a_share_quant.{name}")
    return logging.getLogger("a_share_quant")
