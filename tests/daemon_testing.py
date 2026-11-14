"""测试里替换守护进程用到的函数、常量，读取守护进程源码（2026-11-14 项目优化第4步拆分守护进程后使用）。

守护进程从 scripts/trading_daemon.py 拆到 src/daemon/ 下的多个模块后，同一个名字会被导入到好几个模块里，
每个模块各自查找。以前 patch.object(daemon, "X", ...) 替换一处就对整个守护进程生效；现在要把守护进程
所有模块里的 X 一起换掉，效果才和拆分前一样。

patch_daemon("X", ...) 的其余参数与 patch.object(daemon, "X", ...) 完全相同，用法也相同
（with 语句、start()/stop()）。返回值同 patch.object：自动生成的 Mock，或传入的替换值。

DAEMON_SOURCE_FILES / read_daemon_source()：按源码检查守护进程的测试用。拆分前查一个文件，现在查启动入口和
src/daemon 下全部模块（按分层顺序拼在一起；每个模块内函数的先后与原文件相同）。
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.daemon  # noqa: E402
from scripts import trading_daemon  # noqa: E402,F401  确保守护进程已加载

DAEMON_PACKAGE_MODULES = [importlib.import_module("src.daemon." + info.name)
                          for info in pkgutil.iter_modules(src.daemon.__path__)]
DAEMON_SOURCE_FILES = [PROJECT_ROOT / "scripts" / "trading_daemon.py"] + [
    PROJECT_ROOT / "src" / "daemon" / ("%s.py" % name) for name in src.daemon.LAYERS]


def read_daemon_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in DAEMON_SOURCE_FILES)


def daemon_modules() -> list:
    """启动入口（含测试按文件另行加载的副本）和 src/daemon 下全部模块。"""
    entries = []
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "").replace("\\", "/")
        if path.endswith("scripts/trading_daemon.py"):
            entries.append(module)
    return entries + DAEMON_PACKAGE_MODULES


def modules_with(name: str) -> list:
    found = [m for m in daemon_modules() if name in vars(m)]
    if not found:
        raise AttributeError("守护进程的模块里都没有 %s" % name)
    return found


class patch_daemon:
    """把守护进程所有模块里的同一个名字一起替换；参数同 unittest.mock.patch.object 去掉第一个。"""

    def __init__(self, attribute: str, *args, **kwargs):
        self.attribute = attribute
        self.args = args
        self.kwargs = kwargs
        self._patchers: list = []

    def start(self):
        if self._patchers:
            raise RuntimeError("patch_daemon(%r) 已经启动" % self.attribute)
        targets = modules_with(self.attribute)
        first = mock.patch.object(targets[0], self.attribute, *self.args, **self.kwargs)
        result = first.start()
        self._patchers = [first]
        replacement = vars(targets[0])[self.attribute]
        for module in targets[1:]:
            patcher = mock.patch.object(module, self.attribute, replacement)
            patcher.start()
            self._patchers.append(patcher)
        return result

    def stop(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False
