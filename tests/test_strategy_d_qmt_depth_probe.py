from __future__ import annotations

import unittest

from scripts.probe_strategy_d_intraday_qmt_depth import (
    TARGET_PATH,
    explicit_probe_targets,
    load_targets,
    report_paths,
)


class StrategyDQMTDepthProbeTest(unittest.TestCase):
    def test_explicit_targets_accept_same_day_sh_sz_bj_sample(self) -> None:
        targets = load_targets(TARGET_PATH)
        selected = explicit_probe_targets(
            targets,
            [
                "20260629|600113.SH",
                "20260629|000017.SZ",
                "20260629|920367.BJ",
            ],
        )

        self.assertEqual(len(selected), 3)
        self.assertEqual(set(selected["ts_code"].str[-2:]), {"SH", "SZ", "BJ"})

    def test_report_stem_cannot_escape_report_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "只允许"):
            report_paths("../outside")

        detail, summary = report_paths("qmt_three_market_probe")
        self.assertEqual(detail.name, "qmt_three_market_probe.csv")
        self.assertEqual(summary.name, "qmt_three_market_probe.json")


if __name__ == "__main__":
    unittest.main()
