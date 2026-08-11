"""本地密钥加载。

真实密钥只允许来自进程环境或项目根目录下被 Git 忽略的 ``.env``；禁止再从
``config/config.json`` 回退读取，避免密钥随策略配置进入版本库和认证报告。
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from src.utils.config import get_project_root


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return (
            value[1:-1]
            .replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r"\t", "\t")
            .replace(r'\"', '"')
            .replace(r"\\", "\\")
        )
    return value


def load_local_env(project_root: Path, *, override: bool = False) -> set[str]:
    """无第三方依赖地加载项目.env，且绝不返回或打印密钥内容。"""

    path = project_root.absolute() / ".env"
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RuntimeError(f"无法读取本地.env：{exc}") from exc
    loaded: set[str] = set()
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f".env第{line_number}行缺少等号")
        name, value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise RuntimeError(f".env第{line_number}行变量名不合法")
        if override or name not in os.environ:
            os.environ[name] = _env_value(value)
        loaded.add(name)
    return loaded


def tushare_token_env_name(config: Mapping[str, Any]) -> str:
    """读取并校验Tushare令牌环境变量名。"""

    source = config.get("data_source", {})
    if not isinstance(source, Mapping):
        source = {}
    name = str(source.get("token_env", "TUSHARE_TOKEN")).strip()
    if not _ENV_NAME.fullmatch(name):
        raise RuntimeError("data_source.token_env不是合法环境变量名")
    return name


def load_tushare_token(
    config: Mapping[str, Any], *, project_root: Path | None = None
) -> str:
    """从进程环境或本地.env加载令牌；缺失时返回空字符串。"""

    root = (project_root or get_project_root()).absolute()
    load_local_env(root, override=False)
    name = tushare_token_env_name(config)
    return str(os.getenv(name, "")).strip()


def ensure_tushare_token(
    config: Mapping[str, Any],
    *,
    project_root: Path | None = None,
    allow_prompt: bool = True,
) -> str:
    """确保令牌已进入环境；非交互任务缺失时立即给出可操作错误。"""

    token = load_tushare_token(config, project_root=project_root)
    if token:
        return token
    name = tushare_token_env_name(config)
    if allow_prompt and sys.stdin.isatty():
        token = getpass.getpass(
            "请输入 Tushare Pro Token（不会显示，且只用于当前进程）: "
        ).strip()
        if token:
            os.environ[name] = token
            return token
    raise RuntimeError(
        f"未找到{name}。请把真实Token写入项目根目录.env或操作系统环境变量；"
        "禁止写入config/config.json。"
    )
