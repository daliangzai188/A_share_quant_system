import math
import unittest

from scripts.audit_strategy_l_full_universe import (
    first_time_bucket,
    parse_hhmmss_to_minutes,
)


class StrategyLFullUniverseAuditTest(unittest.TestCase):
    def test_hhmmss_is_converted_to_minutes_before_bucketing(self) -> None:
        actual_cases = {
            93839: "before_1000",
            95045: "before_1000",
            93244: "before_1000",
            143001: "after_1430",
        }
        for raw_time, expected_bucket in actual_cases.items():
            with self.subTest(raw_time=raw_time):
                minutes = parse_hhmmss_to_minutes(raw_time)
                self.assertEqual(first_time_bucket(minutes), expected_bucket)

    def test_invalid_or_missing_time_is_unknown(self) -> None:
        for raw_time in (None, "", 236060):
            with self.subTest(raw_time=raw_time):
                minutes = parse_hhmmss_to_minutes(raw_time)
                self.assertTrue(math.isnan(minutes))
                self.assertEqual(first_time_bucket(minutes), "unknown")

    def test_bucket_boundaries_use_real_clock_minutes(self) -> None:
        self.assertEqual(first_time_bucket(parse_hhmmss_to_minutes(93000)), "open_auction")
        self.assertEqual(first_time_bucket(parse_hhmmss_to_minutes(100000)), "before_1000")
        self.assertEqual(first_time_bucket(parse_hhmmss_to_minutes(100001)), "1000_1100")
        self.assertEqual(first_time_bucket(parse_hhmmss_to_minutes(143000)), "1330_1430")
        self.assertEqual(first_time_bucket(parse_hhmmss_to_minutes(143001)), "after_1430")


if __name__ == "__main__":
    unittest.main()
