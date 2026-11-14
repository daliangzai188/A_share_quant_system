"""生产代码清单（2026-11-14，项目优化第1步“安全网”）：从实盘入口按导入关系能走到的模块，必须和记下的清单一致。

生产代码多依赖了一个模块（例如顺手导入了研究脚本），或者少了一个，这里立刻失败并列出差别。
确实应该变的要在同一次改动里更新 tests/golden/expected/production_modules.txt：
    python tests/golden/project_metrics.py --modules > tests/golden/expected/production_modules.txt

目录边界（2026-11-14 第2步起）：src/、scripts/ 只放生产代码，研究代码放 research/，运维工具放 tools/；
研究和工具可以导入生产代码，生产代码不得导入研究和工具。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.golden.project_metrics import boundary, production_modules

EXPECTED = Path(__file__).resolve().parent / "golden" / "expected" / "production_modules.txt"


class ProductionModulesTests(unittest.TestCase):
    def test_production_closure_matches_the_recorded_list(self) -> None:
        expected = [line for line in EXPECTED.read_text(encoding="utf-8").splitlines() if line]
        actual = production_modules()
        added = sorted(set(actual) - set(expected))
        removed = sorted(set(expected) - set(actual))
        self.assertEqual((added, removed), ([], []),
                         "生产代码清单变了：新增 %s，减少 %s（确实应该变就按文件头说明更新清单）" % (added, removed))

    def test_production_dirs_hold_only_production_code(self) -> None:
        stray = boundary()["non_production_in_production_dirs"]
        self.assertEqual(stray, [], "src/、scripts/ 里有不在生产链上的模块：%s（研究代码放 research/，运维工具放 tools/）" % stray)

    def test_production_never_imports_research_or_tools(self) -> None:
        reached = boundary()["production_reaching_research_or_tools"]
        self.assertEqual(reached, [], "生产代码导入了研究或工具模块：%s（需要的功能先搬进 src/ 成为生产代码）" % reached)


if __name__ == "__main__":
    unittest.main()
