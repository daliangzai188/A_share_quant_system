from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from scripts.collect_strategy_d_intraday_tushare_1m import (
    CollectionPolicy,
    build_cluster_jobs,
    build_cross_section_jobs,
    consolidate_minute_parts,
    fetch_job,
    fetch_job_with_retry,
    job_target_pairs,
    load_known_data_gaps,
    normalize_tushare_bars,
    recover_missing_parts_from_output,
    target_status_rows,
)
from src.strategy_d_strict_intraday import (
    REQUIRED_EXCHANGES,
    replay_price_time_queue,
    replay_synchronized_d_scans,
    strict_l2_manifest_gate,
)


def metadata(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "full_stock_order_stream": True,
        "sequence_complete": True,
        "includes_orders": True,
        "includes_trades": True,
        "coverage_start_hhmm": 915,
        "coverage_end_hhmm": 1500,
        "volume_unit": "SHARE",
    }
    result.update(overrides)
    return result


def events(rows: list[tuple[str, int, str, float, int, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "ts_code": "000001.SZ",
                "event_time": event_time,
                "sequence": sequence,
                "event_type": event_type,
                "price": price,
                "volume": volume,
                "side": side,
                "order_id": order_id,
            }
            for event_time, sequence, event_type, price, volume, side, order_id in rows
        ]
    )


def test_price_time_queue_reconstructs_full_fill_before_1455() -> None:
    result = replay_price_time_queue(
        events(
            [
                ("09:20:00.001", 1, "ORDER_ADD", 10.0, 1000, "BUY", "A"),
                # 同一信号时点新增的真实封单必须排在虚拟委托前面。
                ("14:01:00.000", 2, "ORDER_ADD", 10.0, 200, "BUY", "B"),
                ("14:02:00.000", 3, "TRADE", 10.0, 800, "", ""),
                ("14:03:00.000", 4, "ORDER_CANCEL", 10.0, 400, "BUY", "A"),
                ("14:04:00.000", 5, "TRADE", 10.0, 700, "", ""),
            ]
        ),
        limit_price=10.0,
        signal_time="14:01:00.000",
        order_quantity=500,
        metadata=metadata(),
    )
    assert result["certifiable"] is True
    assert result["status"] == "STRICT_FULL_FILL_BEFORE_1455"
    assert result["filled_quantity"] == 500
    assert result["cancelled_quantity"] == 0
    assert result["fill_hhmm"] == 1404


def test_price_time_queue_partial_fill_then_cancel_remainder() -> None:
    result = replay_price_time_queue(
        events(
            [
                ("09:20:00", 1, "ORDER_ADD", 10.0, 100, "BUY", "A"),
                ("14:02:00", 2, "TRADE", 10.0, 300, "", ""),
            ]
        ),
        limit_price=10.0,
        signal_time="14:01:00",
        order_quantity=500,
        metadata=metadata(),
    )
    assert result["status"] == "STRICT_PARTIAL_FILL_CANCEL_REMAINDER_1455"
    assert result["filled_quantity"] == 200
    assert result["cancelled_quantity"] == 300


def test_incomplete_stream_fails_closed() -> None:
    result = replay_price_time_queue(
        events([("14:02:00", 1, "TRADE", 10.0, 1000, "", "")]),
        limit_price=10.0,
        signal_time="14:01:00",
        order_quantity=500,
        metadata=metadata(sequence_complete=False),
    )
    assert result["certifiable"] is False
    assert result["status"] == "BLOCKED_INCOMPLETE_L2"
    assert "逐笔序列不完整" in result["reasons"]


def test_string_false_metadata_cannot_open_strict_gate() -> None:
    result = replay_price_time_queue(
        events([("14:02:00", 1, "TRADE", 10.0, 1000, "", "")]),
        limit_price=10.0,
        signal_time="14:01:00",
        order_quantity=500,
        metadata=metadata(sequence_complete="False"),
    )
    assert result["certifiable"] is False
    assert "逐笔序列不完整" in result["reasons"]


def test_duplicate_order_id_fails_closed() -> None:
    result = replay_price_time_queue(
        events(
            [
                ("09:30:00", 1, "ORDER_ADD", 10.0, 100, "BUY", "A"),
                ("09:31:00", 2, "ORDER_ADD", 10.0, 100, "BUY", "A"),
            ]
        ),
        limit_price=10.0,
        signal_time="14:01:00",
        order_quantity=500,
        metadata=metadata(),
    )
    assert result["certifiable"] is False
    assert any("重复order_id" in reason for reason in result["reasons"])


def test_cluster_jobs_stay_under_8000_rows_and_group_sparse_targets() -> None:
    open_dates = pd.bdate_range("2025-01-02", periods=70).strftime("%Y%m%d").tolist()
    targets = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * 4,
            "trade_date": [open_dates[0], open_dates[10], open_dates[32], open_dates[33]],
        }
    )
    jobs = build_cluster_jobs(targets, open_dates, max_open_days=33)
    assert len(jobs) == 2
    assert jobs.iloc[0]["target_count"] == 3
    assert jobs["open_day_span"].max() <= 33
    assert jobs["open_day_span"].max() * 241 <= 8000


def test_cross_section_jobs_batch_33_codes_without_losing_targets() -> None:
    targets = pd.DataFrame(
        {
            "trade_date": ["20250102"] * 70,
            "ts_code": [f"{index:06d}.SZ" for index in range(70)],
        }
    )

    jobs = build_cross_section_jobs(targets, max_codes_per_request=33)
    pairs = [pair for job in jobs.itertuples(index=False) for pair in job_target_pairs(job)]

    assert len(jobs) == 3
    assert jobs["target_count"].max() == 33
    assert jobs["theoretical_return_rows"].max() == 33 * 241
    assert len(pairs) == 70
    assert len(set(pairs)) == 70


def test_known_vendor_gaps_are_exact_targets_and_fail_closed() -> None:
    targets = pd.DataFrame(
        {
            "target_key": [
                "20241128|920149.BJ",
                "20241128|920199.BJ",
                "20241128|920765.BJ",
                "20241128|920802.BJ",
            ]
        }
    )

    gaps = load_known_data_gaps(targets)

    assert len(gaps) == 4
    assert set(gaps["target_key"]) == set(targets["target_key"])
    assert gaps["handling"].eq("FAIL_CLOSED_KEEP_IN_DENOMINATOR").all()


def test_tushare_normalization_keeps_only_target_dates() -> None:
    raw = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_time": ["2025-01-02 09:30:00", "2025-01-03 09:30:00"],
            "open": [10, 11],
            "high": [10, 11],
            "low": [10, 11],
            "close": [10, 11],
            "vol": [100, 200],
            "amount": [1000, 2200],
        }
    )
    result = normalize_tushare_bars(raw, target_dates={"20250103"})
    assert len(result) == 1
    assert result.iloc[0]["trade_date"] == "20250103"
    assert result.iloc[0]["volume"] == 200


def test_cluster_fetch_repairs_missing_target_with_exact_day_request() -> None:
    rows = pd.DataFrame(
        {
            "ts_code": ["920641.BJ", "920641.BJ"],
            "trade_time": ["2024-10-18 09:30:00", "2024-12-03 09:30:00"],
            "open": [11.54, 22.6],
            "high": [11.54, 22.6],
            "low": [11.54, 22.6],
            "close": [11.54, 22.6],
            "vol": [100, 200],
            "amount": [1154, 4520],
        }
    )

    class Source:
        calls: list[tuple[str, str]] = []

        def get_stock_minute_bars(
            self,
            _ts_code: str,
            start: str,
            end: str,
            **_kwargs: object,
        ) -> pd.DataFrame:
            self.calls.append((start, end))
            if start.startswith("2024-10-18") and end.startswith("2024-12-03"):
                return rows.iloc[[1]].copy()
            return rows.iloc[[0]].copy()

    source = Source()
    job = SimpleNamespace(
        ts_code="920641.BJ",
        start_date="20241018",
        end_date="20241203",
        target_dates="20241018;20241203",
    )
    result = fetch_job(source, job)
    assert set(result["trade_date"]) == {"20241018", "20241203"}
    assert len(source.calls) == 2


def test_cross_section_fetch_repairs_one_missing_code_and_writes_two_statuses() -> None:
    def raw(code: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ts_code": [code],
                "trade_time": ["2025-01-02 09:30:00"],
                "open": [10],
                "high": [10],
                "low": [10],
                "close": [10],
                "vol": [100],
                "amount": [1000],
            }
        )

    class Source:
        calls: list[str] = []

        def get_stock_minute_bars(
            self, ts_code: str, *_args: object, **_kwargs: object
        ) -> pd.DataFrame:
            self.calls.append(ts_code)
            if "," in ts_code:
                return raw("000001.SZ")
            return raw(ts_code)

    job = SimpleNamespace(
        job_key="CROSS_SECTION|20250102|0001|test",
        ts_code="000001.SZ,000002.SZ",
        start_date="20250102",
        end_date="20250102",
        target_dates="20250102",
        target_keys="20250102|000001.SZ;20250102|000002.SZ",
    )
    source = Source()
    result = fetch_job(source, job)
    statuses = target_status_rows(job, result)

    assert set(result["ts_code"]) == {"000001.SZ", "000002.SZ"}
    assert source.calls == ["000001.SZ,000002.SZ", "000002.SZ"]
    assert len(statuses) == 2


def paid_policy() -> CollectionPolicy:
    return CollectionPolicy(
        access_tier="PAID_A_SHARE_HISTORY_MINUTE",
        request_limit_per_minute=500,
        request_interval_seconds=0.15,
        max_attempts_per_job=3,
        rate_limit_backoff_seconds=65,
        transient_backoff_seconds=3,
        request_mode="CROSS_SECTION_BY_TRADE_DATE",
        max_codes_per_request=33,
        checkpoint_every_jobs=25,
    )


def test_paid_tushare_rate_limit_waits_65_seconds_then_recovers() -> None:
    raw = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_time": ["2025-01-02 09:30:00"],
            "open": [10],
            "high": [10],
            "low": [10],
            "close": [10],
            "vol": [100],
            "amount": [1000],
        }
    )

    class Source:
        calls = 0

        def get_stock_minute_bars(self, *_args: object, **_kwargs: object) -> pd.DataFrame:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("频率超限")
            return raw

    source = Source()
    sleeps: list[float] = []
    job = SimpleNamespace(
        job_key="000001.SZ|20250102|20250102",
        ts_code="000001.SZ",
        start_date="20250102",
        end_date="20250102",
        target_dates="20250102",
    )
    result, error, attempts = fetch_job_with_retry(
        source,
        job,
        policy=paid_policy(),
        sleep_fn=sleeps.append,
    )
    assert error == ""
    assert attempts == 2
    assert sleeps == [65]
    assert len(result) == 1


def test_non_retryable_permission_error_fails_once() -> None:
    class Source:
        calls = 0

        def get_stock_minute_bars(self, *_args: object, **_kwargs: object) -> pd.DataFrame:
            self.calls += 1
            raise RuntimeError("抱歉，您没有stk_mins接口权限")

    source = Source()
    job = SimpleNamespace(
        job_key="000001.SZ|20250102|20250102",
        ts_code="000001.SZ",
        start_date="20250102",
        end_date="20250102",
        target_dates="20250102",
    )
    result, error, attempts = fetch_job_with_retry(
        source,
        job,
        policy=paid_policy(),
        sleep_fn=lambda _seconds: None,
    )
    assert result.empty
    assert "没有stk_mins接口权限" in error
    assert attempts == 1
    assert source.calls == 1


def test_timeout_error_uses_transient_backoff() -> None:
    class Source:
        calls = 0

        def get_stock_minute_bars(self, *_args: object, **_kwargs: object) -> pd.DataFrame:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("接口超过60秒未返回")
            return pd.DataFrame()

    source = Source()
    sleeps: list[float] = []
    job = SimpleNamespace(
        job_key="000001.SZ|20250102|20250102",
        ts_code="000001.SZ",
        start_date="20250102",
        end_date="20250102",
        target_dates="20250102",
    )
    result, error, attempts = fetch_job_with_retry(
        source,
        job,
        policy=paid_policy(),
        sleep_fn=sleeps.append,
    )
    assert result.empty
    assert error == ""
    assert attempts == 2
    assert sleeps == [3]


def test_minute_parts_consolidate_without_duplicate_bars(tmp_path: Path) -> None:
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    output = tmp_path / "minute.csv"
    first = pd.DataFrame(
        {
            "trade_date": ["20250102"],
            "ts_code": ["000001.SZ"],
            "hhmm": ["0930"],
            "close": [10.0],
        }
    )
    second = pd.DataFrame(
        {
            "trade_date": ["20250102", "20250102"],
            "ts_code": ["000001.SZ", "000001.SZ"],
            "hhmm": ["0930", "0931"],
            "close": [10.1, 10.2],
        }
    )
    first.to_csv(parts_dir / "a.csv", index=False)
    second.to_csv(parts_dir / "b.csv", index=False)
    result = consolidate_minute_parts(output_path=output, parts_dir=parts_dir)
    assert len(result) == 2
    assert result.loc[result["hhmm"].eq("0930"), "close"].iloc[0] == 10.1
    assert output.exists()


def test_complete_legacy_output_can_restore_missing_job_part(tmp_path: Path) -> None:
    output = tmp_path / "minute.csv"
    parts_dir = tmp_path / "parts"
    bars = pd.DataFrame(
        {
            "trade_date": ["20250102"] * 241,
            "ts_code": ["000002.SZ"] * 241,
            "hhmm": [f"{index:04d}" for index in range(241)],
            "close": [10.0] * 241,
        }
    )
    bars.to_csv(output, index=False)
    jobs = pd.DataFrame(
        [
            {
                "job_key": "000002.SZ|20250102|20250102",
                "ts_code": "000002.SZ",
                "start_date": "20250102",
                "end_date": "20250102",
                "target_dates": "20250102",
            }
        ]
    )
    recovered = recover_missing_parts_from_output(
        jobs,
        output_path=output,
        parts_dir=parts_dir,
    )
    assert recovered == 1
    assert (parts_dir / "000002.SZ_20250102_20250102.csv").exists()


def test_full_market_manifest_requires_every_date_and_exchange() -> None:
    dates = ["20250102", "20250103"]
    rows = []
    for trade_date in dates:
        for exchange in REQUIRED_EXCHANGES:
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": exchange,
                    "status": "COMPLETE",
                    "full_market": True,
                    "sequence_complete": True,
                    "includes_orders": True,
                    "includes_trades": True,
                    "includes_snapshots": True,
                    "coverage_start_hhmm": 915,
                    "coverage_end_hhmm": 1500,
                    "volume_unit": "SHARE",
                }
            )
    complete = strict_l2_manifest_gate(pd.DataFrame(rows), required_open_dates=dates)
    assert complete["passed"] is True
    incomplete = strict_l2_manifest_gate(
        pd.DataFrame(rows[:-1]), required_open_dates=dates
    )
    assert incomplete["passed"] is False
    assert incomplete["missing_file_count"] == 1


def test_synchronized_market_replay_reconstructs_sentiment_and_daily_ranking() -> None:
    paths = {
        "000001.SZ": {1000: 10, 1001: 9.9, 1002: 10, 1003: 9.9, 1401: 10},
        "000002.SZ": {1000: 20, 1001: 19.9, 1002: 20, 1003: 19.9, 1401: 20},
        "000003.SZ": {1000: 30, 1001: 30, 1002: 30, 1003: 30, 1401: 30},
    }
    rows = []
    for scan_index, hhmm in enumerate((1000, 1001, 1002, 1003, 1401), start=1):
        for code_index, (ts_code, path) in enumerate(paths.items(), start=1):
            limit = float(code_index * 10)
            rows.append(
                {
                    "trade_date": "20250102",
                    "scan_id": f"S{scan_index}",
                    "event_time": f"{hhmm // 100:02d}:{hhmm % 100:02d}:00",
                    "ts_code": ts_code,
                    "limit_price": limit,
                    "last_price": path[hhmm],
                    "bid_volume_1": 1000 + code_index * 100,
                    "circ_mv": 100000 + code_index * 1000,
                    "previous_day_limit_up": False,
                    "historical_st": False,
                    "market_segment": "sz_main",
                    "fill_probability": 0.9,
                    "fill_reliable": True,
                }
            )
    result = replay_synchronized_d_scans(
        pd.DataFrame(rows),
        coverage_metadata={
            "full_market": True,
            "sequence_complete": True,
            "includes_snapshots": True,
            "includes_orders": True,
            "includes_trades": True,
            "coverage_start_hhmm": 930,
            "coverage_end_hhmm": 1500,
            "universe_size": 3,
        },
        sentiment_minimum=1,
        sentiment_maximum=3,
    )
    assert result["certifiable"] is True
    assert len(result["signals"]) == 1
    signal = result["signals"][0]
    assert signal["signal_hhmm"] == 1401
    assert signal["sealed_count"] == 3
    # 两只候选都炸板2次，按封单/流通市值比取更高的000002.SZ。
    assert signal["ranked_candidate_codes"] == ["000002.SZ", "000001.SZ"]
    assert signal["ts_code"] == "000002.SZ"


def test_synchronized_market_replay_rejects_partial_scan_universe() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "scan_id": scan_id,
                "event_time": event_time,
                "ts_code": ts_code,
                "limit_price": 10,
                "last_price": 10,
                "bid_volume_1": 1000,
                "circ_mv": 100000,
                "previous_day_limit_up": False,
                "historical_st": False,
                "market_segment": "sz_main",
                "fill_probability": 0.9,
                "fill_reliable": True,
            }
            for scan_id, event_time, ts_code in (
                ("S1", "09:30:00", "000001.SZ"),
                ("S1", "09:30:00", "000002.SZ"),
                ("S2", "09:31:00", "000001.SZ"),
            )
        ]
    )
    result = replay_synchronized_d_scans(
        frame,
        coverage_metadata={
            "full_market": True,
            "sequence_complete": True,
            "includes_snapshots": True,
            "includes_orders": True,
            "includes_trades": True,
            "coverage_start_hhmm": 930,
            "coverage_end_hhmm": 1500,
            "universe_size": 2,
        },
    )
    assert result["certifiable"] is False
    assert "不同scan_id的股票宇宙不一致" in result["reasons"]
