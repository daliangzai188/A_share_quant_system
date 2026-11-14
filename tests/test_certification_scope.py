"""方案甲认证覆盖的代码（2026-11-15）。

证书 reports/current_portfolio_alignment/return_first_live_certification.json 记录认证时的代码文件清单和校验码。
清单由 research/certify_plan_jia_release.py 的 certified_code_files() 自动列出：认证脚本和实盘交易入口按导入关系
能走到的全部模块（以前手写，重构拆出的新模块会漏）。这里核对：
- 证书的清单就是现在的清单（新拆出或新导入的模块不会漏在认证外）；
- 证书的校验码就是现在这些文件的校验码。代码改过就在这里失败：重新运行
  python research/certify_plan_jia_release.py（组合指标必须和锁定值逐位相同才写 PASS 证书），把证书和代码一起提交。
- 证书如实写明不覆盖的内容（M、停手期20%仓位、ACE+M资金分配、独立样本外、容量）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from research.certify_plan_jia_release import NOT_COVERED, certified_code_files
from research.live_certification import certification_files_sha256

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATE = ROOT / "reports/current_portfolio_alignment/return_first_live_certification.json"
RECERTIFY = "重新运行 python research/certify_plan_jia_release.py，把证书和代码一起提交"


class CertificationScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.certificate = json.loads(CERTIFICATE.read_text(encoding="utf-8"))
        cls.expected = certified_code_files(ROOT)

    def test_certificate_lists_every_module_the_certified_chain_imports(self) -> None:
        listed = self.certificate["code_files"]
        missing = [path for path in self.expected if path not in listed]
        extra = [path for path in listed if path not in self.expected]
        self.assertEqual((missing, extra), ([], []), "证书的代码清单和现在不同（缺少/多出）；%s" % RECERTIFY)

    def test_certificate_matches_current_code(self) -> None:
        current = certification_files_sha256(ROOT, self.certificate["code_files"])
        self.assertEqual(current, self.certificate["code_sha256"], "认证之后代码改过；%s" % RECERTIFY)

    def test_split_rule_modules_are_covered(self) -> None:
        for path in ("src/ac_rules.py", "src/acde_plan.py", "src/e_signal_inputs.py", "src/portfolio_ace_m.py",
                     "src/fixed_rule_execution.py", "src/market_rules.py", "src/strategy_m.py", "src/trade_replay.py",
                     "src/combined_live_engine.py", "src/equity_curve_stop.py", "src/config_check.py",
                     "src/daemon/buy.py", "src/strategy_d_monitor/__init__.py", "qmt_inner/engine.py",
                     "config/config.json", "config/research/research.json"):
            self.assertIn(path, self.expected)

    def test_certificate_states_what_it_does_not_cover(self) -> None:
        self.assertEqual(self.certificate["not_covered"], NOT_COVERED)
        self.assertFalse(self.certificate["independent_oos_certified"])
        self.assertFalse(self.certificate["capacity_certified"])
        text = "".join(NOT_COVERED)
        for word in ("策略M", "20%", "99.5%", "独立样本外", "容量"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
