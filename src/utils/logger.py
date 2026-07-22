from __future__ import annotations

import datetime
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class SafeRotatingFileHandler(RotatingFileHandler):
    """轮转失败不永久停写的 RotatingFileHandler(2026-07-22 修复)。

    根因:Windows 上标准 RotatingFileHandler 轮转时先 close 当前文件、再
    os.rename 成 .1。若此刻文件正被其他进程占用句柄(Syncthing 正在同步该
    10MB 大文件 / 用户 Get-Content -Wait 挂着 / 杀毒扫描),rename 抛
    PermissionError,logging 默认吞掉异常,结果 stream 已关闭却没重开,
    日志从此静默停写(业务不受影响,但可观测性丢失)。2026-07-21 生产实盘
    即此事故:trading_daemon.log 卡在 10MB、日志停在 17:41,而 daemon
    照常运行、收盘流水线跑完、次日正常成交。

    修复:轮转失败时放弃本次轮转、立即重开当前文件继续写(下次超限再试),
    并往 stderr 记一条,保证日志永不静默中断。
    """

    def doRollover(self) -> None:  # noqa: D401
        try:
            super().doRollover()
        except OSError as exc:
            # rename 撞文件锁:此时 stream 已被 super() 关闭,必须重开,
            # 否则后续 emit 全部失败=日志永久停写。宁可文件略超上限,不可停写。
            try:
                if self.stream is None:
                    self.stream = self._open()
            except OSError:
                self.stream = None
            try:
                sys.stderr.write(
                    f"[logger] 日志轮转失败(文件被占用),已重开继续写: {exc}\n"
                )
            except Exception:
                pass


class _BeijingFormatter(logging.Formatter):
    """所有日志时间戳强制使用北京时间（UTC+8），并压缩实盘日志前缀。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, tz=_BEIJING_TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # INFO 是实盘最高频日志，隐藏级别和 logger 名称，避免每行重复
        # "INFO | a_share_quant.trading_daemon"。WARNING/ERROR 保留级别，便于终端着色和排错。
        record.levelprefix = "" if record.levelno == logging.INFO else f"{record.levelname} | "
        return super().format(record)


def setup_logger(
    name: str = "a_share_quant",
    log_dir: str | Path = "logs",
    log_file: str = "a_share_quant.log",
    level: str | int = "INFO",
    max_bytes: int = 50 * 1024 * 1024,
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
    try:
        log_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # WebDAV WinError 58 workaround: directory may already exist
        try:
            next(iter(log_path.iterdir()), None)
        except OSError:
            log_path.mkdir(parents=True, exist_ok=True)

    formatter = _BeijingFormatter(
        fmt="%(asctime)s | %(levelprefix)s%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(resolved_level)

    # delay=True:第一次写日志才打开文件句柄,缩小启动期与同步/杀毒的占用窗口;
    # SafeRotatingFileHandler:轮转撞 Windows 文件锁时不永久停写(见类注释)。
    file_handler = SafeRotatingFileHandler(
        log_path / log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
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
