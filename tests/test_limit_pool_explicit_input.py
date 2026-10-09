from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from scripts.generate_live_limit_pool_daily_ops import parse_args


class LimitPoolInputTests(unittest.TestCase):
    def test_pipeline_can_supply_current_live_scored_file(self) -> None:
        with patch.object(sys, "argv", ["generate_live_limit_pool_daily_ops.py", "--signal-date", "20261009",
                                        "--input-path", "data/processed/live_limit_up_fill_scored.csv"]):
            args = parse_args()
        self.assertEqual(args.input_path, "data/processed/live_limit_up_fill_scored.csv")

    def test_existing_standalone_default_is_preserved(self) -> None:
        with patch.object(sys, "argv", ["generate_live_limit_pool_daily_ops.py"]):
            self.assertEqual(parse_args().input_path, "data/processed/limit_up_fill_scored.csv")


if __name__ == "__main__":
    unittest.main()
