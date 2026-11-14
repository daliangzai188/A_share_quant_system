"""策略D回测与实盘共用的候选规范（src/strategy_d_spec.py）：守护进程、D回测、影子账都按这里判定D候选。

2026-11-14 优化第1步“安全网”补的测试：把规则数值和每个边界钉住，任何改动都会在这里先暴露。
"""
from __future__ import annotations

import math
import unittest

import pandas as pd

from src import strategy_d_spec as spec


def candidate(**overrides) -> dict:
    row = {
        "limit_times": 1, "is_st": False, "market_sentiment_level": "strong", "board_type": "multi_open",
        "open_times": 2, "first_time_bucket": "midday", "last_time": 141500, "fill_probability": 0.8,
        "is_fill_score_reliable": True, "market_segment": "main",
    }
    row.update(overrides)
    return row


class RuleValueTests(unittest.TestCase):
    def test_rule_values_are_pinned(self):
        self.assertEqual(
            (spec.D_SENTIMENT_LEVEL, spec.D_BOARD_TYPE, spec.D_MIN_OPEN_TIMES, spec.D_MAX_OPEN_TIMES,
             spec.D_PREFERRED_OPEN_TIMES, spec.D_TAIL_RESEAL_HHMM, spec.D_MIN_FILL_PROBABILITY),
            ("strong", "multi_open", 2, 3, 2, 1400, 0.80))
        self.assertEqual(spec.D_FIRST_TIME_BUCKETS, frozenset({"midday", "afternoon", "late"}))
        self.assertEqual((spec.D_TRACKING_START_HHMM, spec.D_SIGNAL_START_HHMM, spec.D_ORDER_CANCEL_HHMM,
                          spec.D_LATEST_COMPLETE_HISTORY_START_HHMM, spec.D_CHECKPOINT_MAX_AGE_SECONDS),
                         (930, 1400, 1455, 930, 75))


class FirstSealBucketTests(unittest.TestCase):
    def test_bucket_boundaries(self):
        cases = {-5: "unknown", 0: "unknown", 925: "open_limit", 931: "open_limit", 932: "early_morning",
                 959: "early_morning", 1000: "midday", 1359: "midday", 1400: "afternoon", 1429: "afternoon",
                 1430: "late", 1500: "late"}
        for hhmm, bucket in cases.items():
            self.assertEqual(spec.classify_first_time_bucket_hhmm(hhmm), bucket, hhmm)


class CommonRejectionTests(unittest.TestCase):
    def reason(self, **kwargs):
        args = dict(open_times=2, first_seal_hhmm=1030)
        args.update(kwargs)
        return spec.common_candidate_rejection_reason(**args)

    def test_passing_candidate_has_no_reason(self):
        self.assertEqual(self.reason(), "")
        self.assertEqual(self.reason(open_times=3, first_seal_hhmm=1445, last_seal_hhmm=1400, require_tail_reseal=True), "")

    def test_reasons_in_order(self):
        self.assertEqual(self.reason(open_times=1), "炸板次数1低于回测multi_open下限2")
        self.assertEqual(self.reason(open_times=4), "炸板次数4超过回测上限3")
        self.assertEqual(self.reason(first_seal_hhmm=945),
                         "首次封板时段early_morning不在回测允许范围['afternoon', 'late', 'midday']")
        self.assertEqual(self.reason(open_times=1, first_seal_hhmm=945), "炸板次数1低于回测multi_open下限2", "只报第一个原因")

    def test_tail_reseal_only_when_required(self):
        self.assertEqual(self.reason(last_seal_hhmm=1359), "")
        self.assertEqual(self.reason(last_seal_hhmm=1359, require_tail_reseal=True), "最后回封时间1359早于回测下限1400")
        self.assertEqual(self.reason(last_seal_hhmm=None, require_tail_reseal=True), "最后回封时间0000早于回测下限1400")


class HistoricalMaskTests(unittest.TestCase):
    def test_each_condition_rejects(self):
        failing = {
            "limit_times": 2, "is_st": "True", "market_sentiment_level": "normal", "board_type": "one_word",
            "open_times": 1, "first_time_bucket": "early_morning", "last_time": 135959, "fill_probability": 0.79,
            "is_fill_score_reliable": "no",
        }
        rows = [candidate()] + [candidate(**{key: value}) for key, value in failing.items()]
        rows += [candidate(open_times=4), candidate(last_time=None), candidate(fill_probability="bad")]
        mask = spec.historical_candidate_mask(pd.DataFrame(rows))
        self.assertEqual(mask.tolist(), [True] + [False] * (len(rows) - 1))
        self.assertEqual(mask.dtype, bool)

    def test_text_flags_threshold_and_segments(self):
        rows = [candidate(is_st="0", is_fill_score_reliable="1"), candidate(is_st="no", is_fill_score_reliable="yes",
                                                                              open_times=3, market_segment="gem")]
        data = pd.DataFrame(rows)
        self.assertEqual(spec.historical_candidate_mask(data).tolist(), [True, True])
        self.assertEqual(spec.historical_candidate_mask(data, allowed_segments={"main"}).tolist(), [True, False])
        self.assertEqual(spec.historical_candidate_mask(data, min_fill_probability=0.81).tolist(), [False, False])

    def test_missing_columns_raise(self):
        with self.assertRaisesRegex(ValueError, "D候选数据缺少字段: fill_probability,last_time"):
            spec.historical_candidate_mask(pd.DataFrame([candidate()]).drop(columns=["last_time", "fill_probability"]))


class RankAndSessionTests(unittest.TestCase):
    def test_rank_prefers_two_opens_then_seal_ratio_then_code(self):
        rows = [dict(open_times=3, fd_amount_to_circ_mv=0.09, ts_code="000001.SZ"),
                dict(open_times=2, fd_amount_to_circ_mv=0.01, ts_code="000003.SZ"),
                dict(open_times=2, fd_amount_to_circ_mv=0.05, ts_code="000002.SZ"),
                dict(open_times=2, fd_amount_to_circ_mv=0.05, ts_code="000004.SZ")]
        ranked = sorted(rows, key=lambda r: spec.d_rank_key(**r), reverse=True)
        self.assertEqual([r["ts_code"] for r in ranked], ["000004.SZ", "000002.SZ", "000003.SZ", "000001.SZ"])
        self.assertEqual(spec.d_rank_key(open_times="2", fd_amount_to_circ_mv="0.5", ts_code=1), (1, 0.5, "1"))

    def test_live_sentiment_bounds_inclusive(self):
        self.assertEqual([spec.live_sentiment_is_historical_strong(n, minimum=40, maximum=80) for n in (39, 40, 80, 81)],
                         [False, True, True, False])

    def test_history_complete_only_from_open(self):
        self.assertEqual([spec.intraday_history_is_complete(v) for v in (925, 930, 931, 1300)], [True, True, False, False])

    def test_safe_float(self):
        self.assertEqual(spec.safe_float("1.5"), 1.5)
        self.assertEqual(spec.safe_float(None, 7.0), 7.0)
        self.assertEqual(spec.safe_float("abc", 2.0), 2.0)
        self.assertEqual(spec.safe_float(float("nan")), 0.0)
        self.assertEqual(spec.safe_float([1, 2], 3.0), 3.0)
        self.assertTrue(math.isinf(spec.safe_float("inf")))


if __name__ == "__main__":
    unittest.main()
