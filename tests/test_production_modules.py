"""生产代码清单（2026-11-14，项目优化第1步“安全网”）：从实盘入口按导入关系能走到的模块，必须和记下的清单一致。

生产代码多依赖了一个模块（例如顺手导入了研究脚本），或者少了一个，这里立刻失败并列出差别。
确实应该变的（例如第2步把研究代码移出生产链）要在同一次改动里更新 tests/golden/expected/production_modules.txt：
    python tests/golden/project_metrics.py --modules > tests/golden/expected/production_modules.txt
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.golden.project_metrics import production_modules

EXPECTED = Path(__file__).resolve().parent / "golden" / "expected" / "production_modules.txt"


class ProductionModulesTests(unittest.TestCase):
    def test_production_closure_matches_the_recorded_list(self) -> None:
        expected = [line for line in EXPECTED.read_text(encoding="utf-8").splitlines() if line]
        actual = production_modules()
        added = sorted(set(actual) - set(expected))
        removed = sorted(set(expected) - set(actual))
        self.assertEqual((added, removed), ([], []),
                         "生产代码清单变了：新增 %s，减少 %s（确实应该变就按文件头说明更新清单）" % (added, removed))


if __name__ == "__main__":
    unittest.main()
