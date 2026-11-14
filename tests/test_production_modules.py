"""生产代码清单（2026-11-14，项目优化第1步“安全网”）：从实盘入口按导入关系能走到的模块，必须和记下的清单一致。

生产代码多依赖了一个模块（例如顺手导入了研究脚本），或者少了一个，这里立刻失败并列出差别。
确实应该变的要在同一次改动里更新 tests/golden/expected/production_modules.txt：
    python tests/golden/project_metrics.py --modules > tests/golden/expected/production_modules.txt

目录边界（2026-11-14 第2步起）：src/、scripts/ 只放生产代码，研究代码放 research/，运维工具放 tools/；
研究和工具可以导入生产代码，生产代码不得导入研究和工具。

守护进程分层（2026-11-14 第4步起）：守护进程按职责拆在 src/daemon/ 下，src/daemon/__init__.py 的 LAYERS
由低到高排列，只许高层导入低层，不许导入 scripts（含启动入口），也不许多出 LAYERS 以外的模块。
"""
from __future__ import annotations

import ast
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

    def test_daemon_modules_only_import_lower_layers(self) -> None:
        import src.daemon

        folder = Path(__file__).resolve().parents[1] / "src" / "daemon"
        layers = list(src.daemon.LAYERS)
        rank = {name: i for i, name in enumerate(layers)}
        self.assertEqual(sorted(p.stem for p in folder.glob("*.py") if p.stem != "__init__"), sorted(layers),
                         "src/daemon 下的模块要和 LAYERS 一一对应")
        problems = []
        for name in layers:
            tree = ast.parse((folder / (name + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module == "src.daemon":
                        modules = ["src.daemon." + a.name for a in node.names]
                    else:
                        modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                for module in modules:
                    parts = module.split(".")
                    if parts[0] == "scripts":
                        problems.append("%s 导入了 %s" % (name, module))
                    elif parts[:2] == ["src", "daemon"] and len(parts) > 2 and rank.get(parts[2], len(layers)) >= rank[name]:
                        problems.append("%s 导入了同层或更高层的 %s" % (name, module))
        self.assertEqual(problems, [], "守护进程分层被打破：%s" % problems)


if __name__ == "__main__":
    unittest.main()
