"""M历史体检与实盘信号共同核验严格停牌占位；源CSV保持原样。"""
from __future__ import annotations

from contextlib import ExitStack
import json
from unittest.mock import patch

import pandas as pd
import pytest

import scripts.run_strategy_m_signal as live_script
import src.strategy_m_reference as reference_module
from src.limit_source_quality import suspended_placeholder_mask
from tests.test_live_alignment_ace_m import _write_m_raw


PLACEHOLDER = "603272.SH"


def _strict_placeholder(day):
    return {"trade_date": day, "ts_code": PLACEHOLDER, "name": "源停牌占位", "limit": "U",
            "close": 0.0, "pct_chg": -100.0, "fd_amount": 0.0,
            "first_time": 0, "last_time": 0, "open_times": 0, "limit_times": 2}


def _add_row(raw, signal, day, row, *, quote_present=False):
    pool = raw / "limit_list" / f"{day}.csv"
    frame = pd.read_csv(pool, dtype={"ts_code": str})
    pd.concat([frame, pd.DataFrame([row])], ignore_index=True).to_csv(pool, index=False)
    basic = raw / "stock_basic" / "stock_basic_all.csv"
    names = pd.read_csv(basic, dtype={"ts_code": str, "list_date": str})
    pd.concat([names, pd.DataFrame([{"ts_code": PLACEHOLDER, "name": "源停牌占位", "list_date": "20200101"}])],
              ignore_index=True).to_csv(basic, index=False)
    metrics = raw / "daily_basic" / f"{signal}.csv"
    frame = pd.read_csv(metrics, dtype={"ts_code": str})
    pd.concat([frame, pd.DataFrame([{"ts_code": PLACEHOLDER, "circ_mv": 100000.0}])],
              ignore_index=True).to_csv(metrics, index=False)
    if quote_present:
        quote = {"ts_code": PLACEHOLDER, "open": 10.0, "high": 10.1, "low": 9.9,
                 "close": 10.0, "pre_close": 10.0, "amount": 50000.0, "vol": 1000.0}
        path = raw / "daily" / f"{day}.csv"
        frame = pd.read_csv(path, dtype={"ts_code": str})
        pd.concat([frame, pd.DataFrame([quote])], ignore_index=True).to_csv(path, index=False)


def _both_paths(tmp_path, raw, signal, buy, *, expected_mask, success):
    before = {path.relative_to(raw): path.read_bytes() for path in raw.rglob("*.csv")}
    observed_reference, observed_live = [], []

    def observe(seen):
        def check(frame, codes):
            mask = suspended_placeholder_mask(frame, codes)
            selected = frame["ts_code"].astype(str).eq(PLACEHOLDER)
            if selected.any():
                seen.extend(bool(value) for value in mask[selected])
            return mask
        return check

    with patch.object(reference_module, "suspended_placeholder_mask", side_effect=observe(observed_reference)), \
         patch.object(reference_module, "build_m_candidates", wraps=reference_module.build_m_candidates) as ref_build:
        result = reference_module.MReference(raw)(buy)
    out = tmp_path / "isolated_live_output"
    with ExitStack() as stack:
        for attr, value in {
            "RAW": raw, "CALENDAR_PATH": raw / "trade_calendar.csv",
            "STOCK_BASIC_PATH": raw / "stock_basic" / "stock_basic_all.csv", "OUTPUT_DIR": out,
            "ROLLING_SIGNAL_PATH": out / "m_signals_recent.json",
            "RUN_STATUS_PATH": out / "m_signal_runs_recent.json",
        }.items():
            stack.enter_context(patch.object(live_script, attr, value))
        stack.enter_context(patch.object(live_script, "suspended_placeholder_mask", side_effect=observe(observed_live)))
        live_build = stack.enter_context(patch.object(live_script, "build_m_candidates", wraps=live_script.build_m_candidates))
        exit_code = live_script.run(signal)
    assert observed_reference and observed_live
    assert all(value is expected_mask for value in observed_reference + observed_live)
    if success:
        assert result["status"] == "OK", result["error"]
        assert exit_code == 0
        live = pd.read_csv(out / f"m_signal_{signal}_candidates.csv", dtype={"ts_code": str})
        assert [c["ts_code"] for c in result["candidates"]] == live["ts_code"].tolist() == ["300100.SZ"]
        # 占位豁免仅作用于完整性校验，不改策略函数收到的原始涨停池。
        expected_pool = pd.read_csv(raw / "limit_list" / f"{signal}.csv",
                                    dtype={"ts_code": str, "trade_date": str, "list_date": str})
        pd.testing.assert_frame_equal(ref_build.call_args.args[3], expected_pool)
        pd.testing.assert_frame_equal(live_build.call_args.args[3], expected_pool)
    else:
        assert result["status"] == "ERROR" and "日线缺少涨停池股票" in result["error"]
        assert exit_code == 1
        run = json.loads((out / "m_signal_runs_recent.json").read_text(encoding="utf-8"))["runs"][-1]
        assert run["status"] == "ERROR" and PLACEHOLDER in run["error"]
        ref_build.assert_not_called()
        live_build.assert_not_called()
    assert {path.relative_to(raw): path.read_bytes() for path in raw.rglob("*.csv")} == before


@pytest.mark.parametrize("pool_day", ["yesterday", "today"])
def test_strict_zero_price_placeholder_without_quote_is_exempt_in_both_paths(tmp_path, pool_day):
    raw, signal, buy = _write_m_raw(tmp_path)
    days = sorted(pd.read_csv(raw / "trade_calendar.csv", dtype={"cal_date": str})["cal_date"])
    day = days[-3] if pool_day == "yesterday" else signal
    _add_row(raw, signal, day, _strict_placeholder(day))
    _both_paths(tmp_path, raw, signal, buy, expected_mask=True, success=True)


@pytest.mark.parametrize("pool_day", ["yesterday", "today"])
def test_quote_coverage_prevents_placeholder_exemption_without_manufacturing_failure(tmp_path, pool_day):
    raw, signal, buy = _write_m_raw(tmp_path)
    days = sorted(pd.read_csv(raw / "trade_calendar.csv", dtype={"cal_date": str})["cal_date"])
    day = days[-3] if pool_day == "yesterday" else signal
    _add_row(raw, signal, day, _strict_placeholder(day), quote_present=True)
    _both_paths(tmp_path, raw, signal, buy, expected_mask=False, success=True)


@pytest.mark.parametrize("pool_day", ["yesterday", "today"])
@pytest.mark.parametrize("case", ["positive_price", "missing_time_field", "nan_time_field", "quality_label_only"])
def test_incomplete_placeholder_evidence_never_hides_real_missing_quote(tmp_path, pool_day, case):
    raw, signal, buy = _write_m_raw(tmp_path)
    days = sorted(pd.read_csv(raw / "trade_calendar.csv", dtype={"cal_date": str})["cal_date"])
    day = days[-3] if pool_day == "yesterday" else signal
    row = _strict_placeholder(day)
    if case == "positive_price":
        row.update(close=11.0, pct_chg=10.0)
    elif case == "missing_time_field":
        row.pop("last_time")
    elif case == "nan_time_field":
        row["last_time"] = float("nan")
    else:
        row = {"trade_date": day, "ts_code": PLACEHOLDER, "name": "源停牌占位", "limit": "U",
               "fd_amount": 0.0, "limit_data_quality": "suspended_placeholder"}
    _add_row(raw, signal, day, row)
    _both_paths(tmp_path, raw, signal, buy, expected_mask=False, success=False)
