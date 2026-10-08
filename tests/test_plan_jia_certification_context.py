from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import certify_plan_jia_release as certification


class JiaCertificationContextTests(unittest.TestCase):
    def test_fixed_zero_d_policy_has_no_missing_d_archive_dependency(self):
        observed = {}

        def read_context(**kwargs):
            path = kwargs["d_event_path"]
            observed["temporary"] = path
            frame = pd.read_csv(path)
            self.assertTrue(frame.empty)
            self.assertEqual(list(frame.columns), ["trade_date", "ts_code"])
            self.assertEqual(kwargs["minimum_limit_up_count"], 50)
            return {"d_events": frame}

        paths = {"strict_feature_pool": Path("pool.csv"),
                 "market_sentiment": Path("sentiment.csv"),
                 "trade_calendar": Path("calendar.csv")}
        with patch.object(certification, "_context", side_effect=read_context):
            result = certification.formal_context(
                {"market_controller": {"minimum_limit_up_count": 50}}, paths, object())
        self.assertTrue(result["d_events"].empty)
        self.assertFalse(observed["temporary"].exists())

    def test_real_market_input_failure_is_still_propagated(self):
        with patch.object(certification, "_context", side_effect=RuntimeError("market input missing")):
            with self.assertRaisesRegex(RuntimeError, "market input missing"):
                certification.formal_context(
                    {"market_controller": {"minimum_limit_up_count": 50}},
                    {"strict_feature_pool": Path("pool.csv"), "market_sentiment": Path("sentiment.csv"),
                     "trade_calendar": Path("calendar.csv")}, object())
