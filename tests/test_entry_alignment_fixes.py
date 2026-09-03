from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scripts.monitor_strategy_d_intraday import (
    DOrderCapacity,
    StockState,
    StrategyDMonitor,
    calculate_d_order_capacity,
)
from scripts.run_paper_ab_filtered_daily_ops import resolve_ac_selected_leg
from src.acde_rolling_framework import replay_action_date_cash_portfolio
from src.fill_model import fill_probability_from_amounts
from src.strategy_d_minute_alignment import (
    StrictMinutePath,
    expected_completed_minute_hhmm,
)


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class EntryAlignmentFixTests(unittest.TestCase):
    """回归四个会直接改变开仓结果的修复。"""

    @staticmethod
    def _write_d_release(
        path: Path,
        *,
        mode: str = "FACTOR_UNION",
        certified: bool = True,
        enabled: bool = True,
    ) -> Path:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "factor_schema_id": "D_RESEAL_FACTOR_VALUES_V1",
                    "release_id": "D_TEST_ENTRY_ALIGNMENT_GUARD",
                    "strategy_mode": mode,
                    "effective_from": "20260902",
                    "research_window": {
                        "start": "20230901",
                        "end": "20260831",
                    },
                    "entry_alignment": {
                        "historical_signal_time_fill_gate_certified": certified,
                        "runtime_new_buy_enabled": enabled,
                    },
                    "profiles": (
                        [
                            {
                                "profile_id": "P1",
                                "priority": 1,
                                "conditions": {
                                    "segment_bucket": "GROWTH_BOARD"
                                },
                            }
                        ]
                        if mode == "FACTOR_UNION"
                        else []
                    ),
                    "selection_policy": "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_c_accepted_fallback_survives_other_risk_rejections(self) -> None:
        leg, status = resolve_ac_selected_leg(
            None,
            pd.Series({"ts_code": "000001.SZ"}),
            pd.DataFrame(
                [
                    {"ts_code": "000002.SZ"},
                    {"ts_code": "000003.SZ"},
                ]
            ),
        )
        self.assertEqual(leg, "C")
        self.assertEqual(
            status,
            "A_NO_SELECTED_C_SELECTED:AFTER_2_RISK_REJECTED_FALLBACK",
        )

    def test_fill_formula_uses_actual_order_amount(self) -> None:
        probability = fill_probability_from_amounts(
            estimated_turnover_amount=1_000_000.0,
            current_queue_amount=340_800.0,
            planned_buy_amount=824_000.0,
        )
        self.assertAlmostEqual(probability, 0.8)

    def test_live_d_order_capacity_is_82_5_percent_not_41_25(self) -> None:
        class Account:
            available_cash = 1_000_000.0
            total_asset = 1_000_000.0

        capacity = calculate_d_order_capacity(
            Account(),
            price=10.0,
            position_pct=0.825,
            live_config={
                "max_position_pct": 0.85,
                "max_total_position_pct": 0.825,
                "cash_buffer_amount": 1000,
            },
        )
        self.assertEqual(capacity.shares, 82_400)
        self.assertEqual(capacity.actual_amount, 824_000.0)
        self.assertNotEqual(capacity.actual_amount, 412_500.0)

    def test_formal_d_release_keeps_history_closed_but_enables_realtime_gate(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        release = json.loads(
            (project_root / "config" / "strategy_d_factor_release.json").read_text(
                encoding="utf-8"
            )
        )
        alignment = release.get("entry_alignment", {})
        self.assertFalse(
            alignment.get("historical_signal_time_fill_gate_certified", True)
        )
        self.assertTrue(alignment.get("runtime_new_buy_enabled", False))

    def test_live_d_runtime_permission_does_not_require_fabricated_history(self) -> None:
        """历史L2缺失继续影响回测，但当天实时门有数据时可以进入逐单复核。"""
        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(
                Path(directory) / "release.json",
                certified=False,
                enabled=True,
            )
            monitor = StrategyDMonitor(
                broker=Mock(),
                live_order=True,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={"strategy_d": {"factor_release_path": str(release_path)}},
                position_recorder=lambda _payload: None,
            )

            self.assertFalse(monitor.historical_fill_gate_certified)
            self.assertTrue(monitor.runtime_new_buy_enabled)
            self.assertTrue(monitor._entry_alignment_allows_new_buy())

    def test_live_d_legacy_mode_cannot_bypass_entry_alignment_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(
                Path(directory) / "legacy_release.json",
                mode="LEGACY_FORMAL_D",
                certified=False,
                enabled=False,
            )
            broker = Mock()
            monitor = StrategyDMonitor(
                broker=broker,
                live_order=True,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={"strategy_d": {"factor_release_path": str(release_path)}},
                position_recorder=lambda _payload: None,
            )
            monitor._entry_gate_allows_buy = Mock(
                side_effect=AssertionError("未认证D不应进入组合开仓门")
            )

            monitor._check_and_fire()

            monitor._entry_gate_allows_buy.assert_not_called()
            broker.place_order.assert_not_called()
            self.assertFalse(monitor.order_placed)

    def test_live_d_direct_buy_and_order_calls_are_both_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(
                Path(directory) / "release.json",
                certified=False,
                enabled=False,
            )
            broker = Mock()
            monitor = StrategyDMonitor(
                broker=broker,
                live_order=True,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={"strategy_d": {"factor_release_path": str(release_path)}},
                position_recorder=lambda _payload: None,
            )
            state = StockState(
                ts_code="300001.SZ",
                name="D门禁测试",
                upper_limit=11.0,
                last_price=11.0,
            )

            self.assertFalse(monitor._fire_buy_signal(state))
            self.assertFalse(state.buy_signaled)
            self.assertFalse(monitor.order_placed)
            self.assertEqual(monitor.signal_records, [])

            record: dict[str, object] = {}
            self.assertFalse(monitor._place_d_order(state, record))
            self.assertEqual(record["order_status"], "REJECTED_ENTRY_ALIGNMENT")
            broker.place_order.assert_not_called()

    def test_live_d_partial_fill_branch_does_not_raise_after_order_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(Path(directory) / "release.json")
            broker = Mock()
            broker.place_order.return_value = SimpleNamespace(
                accepted=True,
                order_id="D-PARTIAL-1",
                message="accepted",
            )
            monitor = StrategyDMonitor(
                broker=broker,
                live_order=True,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={
                    "strategy_d": {"factor_release_path": str(release_path)},
                    "live_trade": {"max_single_order_amount": 0},
                },
                position_recorder=lambda _payload: None,
            )
            state = StockState(
                ts_code="300001.SZ",
                name="D部分成交测试",
                upper_limit=10.0,
                last_price=10.0,
            )
            monitor._resolve_order_capacity = Mock(
                return_value=DOrderCapacity(
                    available_cash=1_000_000.0,
                    total_asset=1_000_000.0,
                    target_amount=825_000.0,
                    shares=82_500,
                    actual_amount=825_000.0,
                )
            )

            def pass_fill(candidate: StockState, **_kwargs: object) -> tuple[bool, str]:
                candidate.fill_probability = 0.9
                candidate.fill_reliable = True
                candidate.fill_matched_source = "TEST"
                candidate.fill_planned_buy_amount = 825_000.0
                candidate.fill_estimated_turnover_amount = 1_700_000.0
                candidate.fill_current_queue_amount = 900_000.0
                return True, "通过"

            monitor._refresh_fill_gate = pass_fill
            monitor._confirm_submitted_order = Mock(
                return_value=SimpleNamespace(
                    status_code=50,
                    status_text="PARTIAL",
                    filled_qty=100,
                    avg_price=10.0,
                    raw={},
                )
            )
            monitor._record_filled_d_position = Mock()

            # 单元测试只验证部分成交分支，必须隔离真实 Bark/电话通知出口。
            # 否则测试桩中的股票、金额和订单号会被误发成实盘消息。
            with patch(
                "scripts.monitor_strategy_d_intraday.check_strategy_position_occupied",
                return_value=(False, ""),
            ), patch(
                "scripts.monitor_strategy_d_intraday.notify"
            ) as notify_mock, patch("builtins.print") as print_mock:
                result = monitor._place_d_order(state, {})

            self.assertTrue(result)
            broker.place_order.assert_called_once()
            monitor._record_filled_d_position.assert_called_once_with(
                "D-PARTIAL-1", 100, 10.0
            )
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args.args[0], "buy_result")
            self.assertEqual(notify_mock.call_args.args[1], "⏳ D开仓委托未全成")
            print_mock.assert_called_once()

    @staticmethod
    def _d_plan(**overrides: object) -> pd.DataFrame:
        row: dict[str, object] = {
            "signal_date": "20240102",
            "status": "OK",
            "strategy_leg": "D",
            "ts_code": "000001.SZ",
            "name": "D测试",
            "exit_date": "20240103",
            "position_open_until": "20240103",
            "entry_filled": True,
            "position_opened": True,
            "outcome_observable": True,
            "entry_reference_price": 10.0,
            "entry_price": 10.0,
            "exit_reference_price": 10.5,
            "exit_price": 10.5,
            "stock_return_before_fees": 0.05,
            "position_scale": 1.0,
            "fill_gate_required": True,
            "fill_probability_threshold": 0.8,
            "fill_probability_method": "SIGNAL_TIME_AMOUNT_SPACE_OVER_ACTUAL_ORDER_GROSS",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_cash_replay_fails_closed_without_historical_l2_queue(self) -> None:
        detail = replay_action_date_cash_portfolio(
            {"D": self._d_plan(fill_input_reliable=False)},
            action_dates=["20240102", "20240103"],
            priority=("D",),
        )
        self.assertEqual(
            detail.iloc[0]["status"],
            "PLAN_NOT_EXECUTED_FILL_GATE_UNVERIFIABLE",
        )
        self.assertFalse(detail["status"].eq("EXECUTED").any())

    def test_cash_replay_fill_gate_and_order_share_same_82_5_amount(self) -> None:
        detail = replay_action_date_cash_portfolio(
            {
                "D": self._d_plan(
                    fill_input_reliable=True,
                    estimated_turnover_amount=900_000.0,
                    current_queue_amount=400_000.0,
                )
            },
            action_dates=["20240102", "20240103"],
            priority=("D",),
        )
        trade = detail.loc[detail["status"].eq("EXECUTED")].iloc[0]
        self.assertEqual(trade["quantity"], 41_200)
        self.assertEqual(trade["planned_buy_amount"], 412_000.0)
        self.assertEqual(trade["buy_gross"], trade["planned_buy_amount"])
        self.assertAlmostEqual(trade["position_ratio"], 0.824)

    def test_cash_replay_rejects_probability_below_80_percent(self) -> None:
        detail = replay_action_date_cash_portfolio(
            {
                "D": self._d_plan(
                    fill_input_reliable=True,
                    estimated_turnover_amount=700_000.0,
                    current_queue_amount=400_000.0,
                )
            },
            action_dates=["20240102", "20240103"],
            priority=("D",),
        )
        self.assertEqual(
            detail.iloc[0]["status"],
            "PLAN_NOT_EXECUTED_FILL_PROBABILITY",
        )
        self.assertFalse(detail["status"].eq("EXECUTED").any())

    def test_live_d_market_context_only_counts_non_st_first_boards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release_path = Path(directory) / "release.json"
            release_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "factor_schema_id": "D_RESEAL_FACTOR_VALUES_V1",
                        "release_id": "D_TEST_ENTRY_ALIGNMENT",
                        "strategy_mode": "FACTOR_UNION",
                        "effective_from": "20260902",
                        "research_window": {"start": "20230901", "end": "20260831"},
                        "entry_alignment": {
                            "historical_signal_time_fill_gate_certified": True,
                            "runtime_new_buy_enabled": True
                        },
                        "profiles": [
                            {
                                "profile_id": "P1",
                                "priority": 1,
                                "conditions": {"segment_bucket": "GROWTH_BOARD"},
                            }
                        ],
                        "selection_policy": "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
                    }
                ),
                encoding="utf-8",
            )
            monitor = StrategyDMonitor(
                broker=None,
                live_order=False,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={"strategy_d": {"factor_release_path": str(release_path)}},
            )

            def state(code: str, *, st: bool = False, segment: str = "chi_next") -> StockState:
                return StockState(
                    ts_code=code,
                    market_segment=segment,
                    upper_limit=11.0,
                    was_sealed=True,
                    ever_sealed=True,
                    open_times_today=1,
                    st_suspect=st,
                )

            eligible = state("300001.SZ")
            st_stock = state("300002.SZ", st=True)
            previous_limit = state("300003.SZ")
            main_board = state("000004.SZ", segment="sz_main")
            monitor.states = {
                item.ts_code: item
                for item in (eligible, st_stock, previous_limit, main_board)
            }
            monitor.yesterday_limit_codes = {previous_limit.ts_code}
            monitor.strict_minute_paths = {
                code: StrictMinutePath(
                    certifiable=True,
                    last_completed_hhmm=1000,
                    was_sealed=True,
                    ever_sealed=True,
                    first_seal_hhmm=930,
                    last_seal_hhmm=1000,
                    last_reseal_hhmm=1000,
                    open_times=1,
                    last_break_hhmm=959,
                    last_break_close=10.9,
                    previous_seal_to_break_minutes=1,
                )
                for code in monitor.states
            }

            self.assertEqual(monitor.market_ever_sealed_count, 2)
            self.assertEqual(monitor.market_break_event_count, 2)
            self.assertEqual(
                monitor._strict_market_context("chi_next"),
                (2, 2, 2, 1, 1),
            )

    def test_strict_minute_paths_batch_more_than_50_first_boards(self) -> None:
        class MinuteBroker:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def get_minute_bars(
                self,
                ts_codes: list[str],
                *,
                start_time: str,
                end_time: str,
            ) -> dict[str, list[dict[str, float | int]]]:
                self.calls.append(list(ts_codes))
                return {
                    code: [
                        {
                            "hhmm": 930,
                            "open": 11.0,
                            "high": 11.0,
                            "low": 11.0,
                            "close": 11.0,
                            "volume": 1000,
                            "amount": 11_000.0,
                        }
                    ]
                    for code in ts_codes
                }

        with tempfile.TemporaryDirectory() as directory:
            release_path = Path(directory) / "release.json"
            release_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "factor_schema_id": "D_RESEAL_FACTOR_VALUES_V1",
                        "release_id": "D_TEST_BATCH",
                        "strategy_mode": "FACTOR_UNION",
                        "effective_from": "20260902",
                        "research_window": {"start": "20230901", "end": "20260831"},
                        "entry_alignment": {
                            "historical_signal_time_fill_gate_certified": True,
                            "runtime_new_buy_enabled": True,
                        },
                        "profiles": [
                            {
                                "profile_id": "P1",
                                "priority": 1,
                                "conditions": {"segment_bucket": "GROWTH_BOARD"},
                            }
                        ],
                        "selection_policy": "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
                    }
                ),
                encoding="utf-8",
            )
            broker = MinuteBroker()
            monitor = StrategyDMonitor(
                broker=broker,
                live_order=False,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={"strategy_d": {"factor_release_path": str(release_path)}},
            )
            monitor.states = {
                f"{index:06d}.SZ": StockState(
                    ts_code=f"{index:06d}.SZ",
                    market_segment="chi_next",
                    upper_limit=11.0,
                    was_sealed=True,
                    ever_sealed=True,
                    session_high_price=11.0,
                )
                for index in range(51)
            }

            with patch("scripts.monitor_strategy_d_intraday.now_hhmm", return_value=930):
                passed = monitor._refresh_strict_minute_paths()

            self.assertTrue(passed)
            self.assertEqual([len(batch) for batch in broker.calls], [50, 1])
            self.assertEqual(len(monitor.strict_minute_paths), 51)

    def test_late_d_restart_uses_complete_qmt_backfill_but_does_not_chase(self) -> None:
        """QMT回补可以恢复路径；恢复前已经错过的旧回封不能变成当前BUY。"""

        class MinuteBroker:
            def get_minute_bars(
                self,
                ts_codes: list[str],
                *,
                start_time: str,
                end_time: str,
            ) -> dict[str, list[dict[str, float | int]]]:
                self.assert_start = start_time
                self.assert_end = end_time
                return {code: list(bars) for code in ts_codes}

        bars: list[dict[str, float | int]] = []
        for hhmm in expected_completed_minute_hhmm(1044):
            if hhmm == 930:
                close = 10.00
            elif hhmm == 931:
                close = 11.00
            elif hhmm == 932:
                close = 10.98
            else:
                close = 11.00
            bars.append({
                "hhmm": hhmm,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "amount": 100_000.0,
            })

        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(Path(directory) / "release.json")
            monitor = StrategyDMonitor(
                broker=MinuteBroker(),
                live_order=False,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={
                    "strategy_d": {
                        "factor_release_path": str(release_path),
                        "late_start_qmt_1m_recovery_enabled": True,
                    }
                },
            )
            state = StockState(
                ts_code="300001.SZ",
                name="D回补测试",
                market_segment="chi_next",
                upper_limit=11.0,
                last_price=11.0,
                was_sealed=True,
                ever_sealed=True,
                session_high_price=11.0,
            )
            monitor.states = {state.ts_code: state}
            monitor.last_scan_updated_count = 5_546
            monitor._restore_ready_checkpoint = Mock(
                return_value=(False, "D策略代码或配置已变化")
            )

            ready, reason = monitor._prepare_intraday_history(
                1044,
                resumable_in_memory_path=False,
            )
            self.assertTrue(ready)
            self.assertIn("QMT回补09:30至当前", reason)
            self.assertTrue(monitor.minute_history_recovery_required)

            with patch(
                "scripts.monitor_strategy_d_intraday.now_hhmm",
                return_value=1044,
            ):
                self.assertTrue(monitor._refresh_strict_minute_paths())
                matched, reject_reason = monitor._factor_release_match(
                    state,
                    require_fresh_reseal=True,
                )

            self.assertTrue(monitor.minute_history_recovery_completed)
            self.assertFalse(monitor.minute_history_recovery_required)
            self.assertFalse(matched)
            self.assertIn("不是最新完成分钟", reject_reason)

    def test_late_d_restart_without_qmt_minute_history_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release_path = self._write_d_release(Path(directory) / "release.json")
            monitor = StrategyDMonitor(
                broker=object(),
                live_order=False,
                logger=_Logger(),
                signal_csv=Path(directory) / "signals.csv",
                config={
                    "strategy_d": {
                        "factor_release_path": str(release_path),
                        "late_start_qmt_1m_recovery_enabled": True,
                    }
                },
            )
            monitor._restore_ready_checkpoint = Mock(return_value=(False, "无检查点"))

            ready, reason = monitor._prepare_intraday_history(
                1044,
                resumable_in_memory_path=False,
            )

            self.assertFalse(ready)
            self.assertIn("不支持严格QMT 1m回补", reason)
            self.assertFalse(monitor.minute_history_recovery_required)


if __name__ == "__main__":
    unittest.main()
