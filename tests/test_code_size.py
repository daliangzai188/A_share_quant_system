"""生产代码体量上限（2026-11-15，项目优化验收）：单个文件不超过3000行，没有超过200行的函数。

统计口径同 tests/golden/project_metrics.py（实盘入口按导入关系能走到的全部模块，含虚拟机QMT外程序）。
超了就按职责拆：文件拆成同包下的几个模块（参照 src/daemon/、src/strategy_d_monitor/），函数把成段的步骤提成
小函数（不改行为，提交前过对照标准）。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.golden import project_metrics as pm

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_LINES = 3000
MAX_FUNCTION_LINES = 200


class CodeSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = pm.module_files(ROOT)
        cls.prod = sorted(pm.closure(pm.import_edges(cls.files), pm.ROOTS))

    def test_no_production_file_over_3000_lines(self) -> None:
        big = []
        for name in self.prod:
            lines = self.files[name].read_text(encoding="utf-8").count("\n")
            if lines > MAX_FILE_LINES:
                big.append("%s %d行" % (self.files[name].relative_to(ROOT), lines))
        self.assertEqual(big, [], "生产文件超过%d行，按职责拆成几个模块：%s" % (MAX_FILE_LINES, big))

    def test_no_production_function_over_200_lines(self) -> None:
        long_functions = []
        for name in self.prod:
            tree = ast.parse(self.files[name].read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    length = node.end_lineno - node.lineno + 1
                    if length > MAX_FUNCTION_LINES:
                        long_functions.append("%s:%d %s %d行" % (
                            self.files[name].relative_to(ROOT), node.lineno, node.name, length))
        self.assertEqual(long_functions, [], "函数超过%d行，把成段的步骤提成小函数：%s" % (
            MAX_FUNCTION_LINES, long_functions))


if __name__ == "__main__":
    unittest.main()
