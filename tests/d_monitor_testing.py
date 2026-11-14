"""测试里替换D盘中监控用到的函数、常量，读取D盘中监控源码（2026-11-15 项目优化拆分D盘中监控后使用）。

D盘中监控从 scripts/monitor_strategy_d_intraday.py 拆出 src/strategy_d_monitor/ 下的几个模块后，同一个名字
（例如 now_hhmm、notify）会被导入到好几个模块里，每个模块各自查找。以前替换 scripts.monitor_strategy_d_intraday
里的一处就对整个D盘中监控生效；现在要把所有D模块里的这个名字一起换掉，效果才和拆分前一样。

patch_d_monitor("X", ...) 的其余参数与 patch.object(d_monitor, "X", ...) 完全相同，用法也相同（with 语句、
start()/stop()），返回值同 patch.object。pytest 的 monkeypatch 用 monkeypatch_d_monitor(monkeypatch, "X", 值)。

D_MONITOR_SOURCE_FILES / read_d_monitor_source()：按源码检查D盘中监控的测试用（入口文件加 src/strategy_d_monitor 下各模块）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.strategy_d_monitor  # noqa: E402
from scripts import monitor_strategy_d_intraday  # noqa: E402,F401  确保D盘中监控已加载

PACKAGE_MODULE_NAMES = ("support",) + tuple(src.strategy_d_monitor.MIXIN_MODULES)
D_MONITOR_PACKAGE_MODULES = [importlib.import_module("src.strategy_d_monitor." + name)
                             for name in PACKAGE_MODULE_NAMES]
D_MONITOR_SOURCE_FILES = [PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py"] + [
    PROJECT_ROOT / "src" / "strategy_d_monitor" / ("%s.py" % name) for name in PACKAGE_MODULE_NAMES]


def read_d_monitor_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in D_MONITOR_SOURCE_FILES)


def d_monitor_modules() -> list:
    """入口文件（含守护进程按文件另行加载的副本）和 src/strategy_d_monitor 下全部模块。"""
    entries = []
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "").replace("\\", "/")
        if path.endswith("scripts/monitor_strategy_d_intraday.py"):
            entries.append(module)
    return entries + D_MONITOR_PACKAGE_MODULES


def modules_with(name: str) -> list:
    found = [m for m in d_monitor_modules() if name in vars(m)]
    if not found:
        raise AttributeError("D盘中监控的模块里都没有 %s" % name)
    return found


class patch_d_monitor:
    """把D盘中监控所有模块里的同一个名字一起替换；参数同 unittest.mock.patch.object 去掉第一个。"""

    def __init__(self, attribute: str, *args, **kwargs):
        self.attribute = attribute
        self.args = args
        self.kwargs = kwargs
        self._patchers: list = []

    def start(self):
        if self._patchers:
            raise RuntimeError("patch_d_monitor(%r) 已经启动" % self.attribute)
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


def monkeypatch_d_monitor(monkeypatch, attribute: str, value) -> None:
    """pytest monkeypatch 版本：把D盘中监控所有模块里的同一个名字换成 value（测试结束自动还原）。"""
    for module in modules_with(attribute):
        monkeypatch.setattr(module, attribute, value)
