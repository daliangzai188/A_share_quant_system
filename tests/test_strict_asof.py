from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.backtest_engine import SimpleCandidateBacktester
from src.strict_asof import (
    LOCKED_OOS,
    PointInTimeContract,
    StrictAsOfError,
    audit_point_in_time_frame,
    validate_strict_research_frame,
)


def valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": "20240103",
                "ts_code": "000001.SZ",
                "as_of_date": "20240103",
                "model_training_end_date": "20240102",
                "fill_probability_method": "asof_turnover_space_proxy_v2",
                "is_fill_score_reliable": True,
                "allow_buy_reliable": True,
                "fill_probability": 0.8,
                "sample_count": 30,
                "amount": 100.0,
                "turnover_rate": 10.0,
                # 结果列可以留在宽表，但不能出现在selection_columns。
                "next_open": 10.0,
                "exit_close": 10.5,
                "net_return": 0.04,
            },
            {
                "trade_date": "20240104",
                "ts_code": "000002.SZ",
                "as_of_date": "20240104",
                "model_training_end_date": pd.NA,
                "fill_probability_method": "asof_turnover_space_proxy_v2",
                "is_fill_score_reliable": False,
                "allow_buy_reliable": False,
                "fill_probability": 0.0,
                "sample_count": 0,
                "amount": 80.0,
                "turnover_rate": 8.0,
                "next_open": 8.0,
                "exit_close": 7.5,
                "net_return": -0.07,
            },
        ]
    )


class StrictAsOfTests(unittest.TestCase):
    def test_valid_point_in_time_frame_passes(self) -> None:
        audit = audit_point_in_time_frame(
            valid_frame(), PointInTimeContract(dataset_name="valid")
        )
        self.assertTrue(audit.passed)
        self.assertEqual(audit.reliable_training_not_prior_count, 0)

    def test_as_of_after_signal_fails_closed(self) -> None:
        frame = valid_frame()
        frame.loc[0, "as_of_date"] = "20240104"
        with self.assertRaisesRegex(StrictAsOfError, "as-of"):
            audit_point_in_time_frame(frame, PointInTimeContract(dataset_name="future"))

    def test_same_day_model_training_fails_closed(self) -> None:
        frame = valid_frame()
        frame.loc[0, "model_training_end_date"] = frame.loc[0, "trade_date"]
        with self.assertRaisesRegex(StrictAsOfError, "训练截止日"):
            audit_point_in_time_frame(frame, PointInTimeContract(dataset_name="same_day_train"))

    def test_reliable_row_must_have_training_end_date(self) -> None:
        frame = valid_frame()
        frame.loc[0, "model_training_end_date"] = pd.NA
        with self.assertRaisesRegex(StrictAsOfError, "缺少模型训练截止日"):
            audit_point_in_time_frame(frame, PointInTimeContract(dataset_name="missing_train"))

    def test_wrong_fill_method_and_duplicate_key_fail(self) -> None:
        frame = valid_frame().iloc[[0, 0]].copy()
        frame["fill_probability_method"] = "full_sample_proxy"
        with self.assertRaises(StrictAsOfError):
            audit_point_in_time_frame(frame, PointInTimeContract(dataset_name="bad_method"))

    def test_equivalent_date_formats_are_still_duplicate_keys(self) -> None:
        frame = valid_frame().iloc[[0, 0]].copy()
        frame.iloc[1, frame.columns.get_loc("trade_date")] = "2024-01-03"
        frame.iloc[1, frame.columns.get_loc("as_of_date")] = "2024-01-03"
        with self.assertRaisesRegex(StrictAsOfError, "重复信号键"):
            audit_point_in_time_frame(frame, PointInTimeContract(dataset_name="normalised_duplicate"))

    def test_outcome_columns_may_exist_but_cannot_select(self) -> None:
        config = {"asof_mode": "STRICT", "research_protocol": "STRICT_DISCOVERY"}
        audit = validate_strict_research_frame(
            valid_frame(),
            contract=PointInTimeContract(dataset_name="wide_table"),
            selection_columns=["fill_probability", "amount"],
            section_config=config,
            context="unit_test",
        )
        self.assertFalse(audit["release_eligible"])
        self.assertEqual(audit["result_scope"], "DISCOVERY_ONLY")
        with self.assertRaisesRegex(StrictAsOfError, "未来/结果字段"):
            validate_strict_research_frame(
                valid_frame(),
                contract=PointInTimeContract(dataset_name="leaky_selection"),
                selection_columns=["fill_probability", "net_return"],
                section_config=config,
                context="unit_test",
            )

    def test_locked_oos_requires_frozen_spec_hash_and_post_train_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "strategy.json"
            spec.write_text('{"rule":"frozen"}', encoding="utf-8")
            digest = hashlib.sha256(spec.read_bytes()).hexdigest()
            frame = valid_frame().iloc[[0]].copy()
            frame["trade_date"] = "20240103"
            frame["as_of_date"] = "20240103"
            config = {
                "asof_mode": "STRICT",
                "research_protocol": LOCKED_OOS,
                "strategy_training_end_date": "20231231",
                "strategy_frozen_at": "2024-01-01T00:00:00+08:00",
                "evaluation_start_date": "20240102",
                "strategy_spec_path": str(spec),
                "strategy_spec_sha256": digest,
            }
            audit = validate_strict_research_frame(
                frame,
                contract=PointInTimeContract(dataset_name="locked_oos"),
                selection_columns=["fill_probability"],
                section_config=config,
                context="unit_test",
                project_root=root,
            )
            self.assertTrue(audit["release_eligible"])
            self.assertEqual(audit["result_scope"], "FORMAL_OOS")

            backdated = dict(config)
            backdated["strategy_frozen_at"] = "2024-01-04T00:00:00+08:00"
            backdated["evaluation_start_date"] = "20240105"
            with self.assertRaisesRegex(StrictAsOfError, "冻结日前"):
                validate_strict_research_frame(
                    frame,
                    contract=PointInTimeContract(dataset_name="backdated_oos"),
                    selection_columns=["fill_probability"],
                    section_config=backdated,
                    context="unit_test",
                    project_root=root,
                )

    def test_backtester_rejects_legacy_pool_before_writing_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pool = root / "legacy.csv"
            pd.DataFrame(
                [
                    {
                        "trade_date": "20240103",
                        "ts_code": "000001.SZ",
                        "allow_buy_reliable": True,
                        "amount_bucket": "gte_8e8",
                        "rule_count": 1,
                        "fill_probability": 0.8,
                        "sample_count": 30,
                        "net_return": 0.02,
                        "exit_trade_date": "20240105",
                    }
                ]
            ).to_csv(pool, index=False)
            config_path = root / "config.json"
            summary = root / "summary.csv"
            config_path.write_text(
                json.dumps(
                    {
                        "backtest": {
                            "asof_mode": "STRICT",
                            "research_protocol": "STRICT_DISCOVERY",
                            "input_candidate_pool_path": str(pool),
                            "output_trades_path": str(root / "trades.csv"),
                            "output_equity_curve_path": str(root / "equity.csv"),
                            "output_summary_path": str(summary),
                            "output_yearly_path": str(root / "yearly.csv"),
                            "output_strict_asof_audit_path": str(root / "audit.json"),
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StrictAsOfError, "缺少字段"):
                SimpleCandidateBacktester(config_path=config_path).run()
            self.assertFalse(summary.exists())


if __name__ == "__main__":
    unittest.main()
