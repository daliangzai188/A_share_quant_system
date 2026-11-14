"""生产链对照标准（2026-11-14，项目优化第1步“安全网”）的测试入口。

完整对照要从原始行情重建研究池、回放影子账和ACE+M，约需数分钟，并依赖仓库外的冻结数据
（~/A_System_golden_data/20261110），所以默认跳过；每一步优化提交前必须这样跑一遍并全部一致：
    A_SYSTEM_GOLDEN=1 python -m pytest -q tests/test_golden_master.py
或直接：python tests/golden/golden_master.py check
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("A_SYSTEM_GOLDEN_DATA", str(Path.home() / "A_System_golden_data" / "20261110")))


@unittest.skipUnless(os.environ.get("A_SYSTEM_GOLDEN") == "1" and DATA.is_dir(),
                     "完整对照标准需要 A_SYSTEM_GOLDEN=1 和仓库外的冻结数据")
class GoldenMasterTests(unittest.TestCase):
    def test_production_chain_matches_the_golden_outputs(self) -> None:
        done = subprocess.run([sys.executable, str(ROOT / "tests" / "golden" / "golden_master.py"), "check",
                               "--data", str(DATA)], capture_output=True, text=True, timeout=3600,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", A_SYSTEM_DISABLE_NOTIFICATIONS="1"))
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
