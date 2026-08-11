#!/usr/bin/env python3
"""生成冻结发布版本样本外稳健性报告。"""
from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_oos_robustness import write_release_oos_report  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(write_release_oos_report(PROJECT_ROOT), ensure_ascii=False, indent=2))
