from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.optimize_strategy_c_factor_union import (
    basic_metrics,
    profile_identifier,
    union_picks,
)
from scripts.run_paper_ab_filtered_daily_ops import build_c_factor_filtered_pool
from src.strategy_c_factor_rules import (
    FACTOR_COLUMNS,
    FACTOR_SCHEMA_ID,
    FACTOR_UNION_MODE,
    add_factor_values,
    apply_profile_union,
    load_factor_release,
    matching_profile_ids,
)


def factor_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "market_segment": "sz_main",
        "market_emotion_state_bucket": "warming",
        "segment_emotion_state_bucket": "warming",
        "market_chain_count_bucket": "15_30",
        "segment_chain_count_bucket": "1_3",
        "market_limit_down_count_bucket": "lt_5",
        "segment_limit_down_ratio_bucket": "lt_0_1pct",
        "segment_limit_max_height_bucket": "3",
        "market_leader_rank_bucket": "rank_1",
        "segment_market_leader_rank_bucket": "rank_1",
        "segment_limit_height_rank_bucket": "rank_1",
        "first_time_detail_bucket": "1100_1330",
        "limit_times_detail_bucket": "2",
        "open_times_bucket": "1",
        "amount_bucket": "3e8_8e8",
        "turnover_rate_bucket": "10_15",
        "volume_ratio_bucket": "2_4",
        "fd_ratio_bucket": "0_5pct_1pct",
        "prev_pct_chg_bucket": "3_7",
        "amount_ratio_bucket": "1_2_2",
        "limit_up_count_bucket": "50_80",
        "segment_limit_up_count_bucket": "40_80",
        "segment_limit_up_ratio_bucket": "2pct_3pct",
        "retreat_state_bucket": "neutral",
        "segment_retreat_state_bucket": "neutral",
        "board_type": "multi_open",
    }
    row.update(overrides)
    return row


def release(profiles: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "release_id": "C_TEST_UNION",
        "strategy_mode": FACTOR_UNION_MODE,
        "effective_from": "20260701",
        "research_window": {"start": "20240630", "end": "20260630"},
        "profiles": profiles,
        "selection_policy": "TEST",
    }


def test_c_factor_domains_and_or_matching_are_shared() -> None:
    frame = add_factor_values(pd.DataFrame([factor_row(), factor_row(market_segment="bj")]))
    profiles = [
        {"profile_id": "P1", "priority": 1, "conditions": {"market_segment": "sz_main"}},
        {
            "profile_id": "P2",
            "priority": 2,
            "conditions": {"market_chain_count_bucket": "15_30", "market_segment": "bj"},
        },
    ]

    selected = apply_profile_union(frame, profiles)

    assert set(FACTOR_COLUMNS).issubset(frame.columns)
    assert len(selected) == 2
    assert selected["matched_c_profile_ids"].tolist() == ["P1", "P2"]
    assert matching_profile_ids(factor_row(), profiles) == ["P1"]


def test_c_factor_release_is_fail_closed_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            release(
                [
                    {
                        "profile_id": "P1",
                        "conditions": {
                            "market_segment": "sz_main",
                            "market_chain_count_bucket": "15_30",
                        },
                    }
                ]
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = load_factor_release(path)

    assert loaded["strategy_mode"] == FACTOR_UNION_MODE
    assert loaded["profiles"][0]["conditions"] == {
        "market_chain_count_bucket": "15_30",
        "market_segment": "sz_main",
    }


def test_daily_c_pool_reads_same_factor_union_release(tmp_path: Path) -> None:
    release_path = tmp_path / "release.json"
    release_path.write_text(
        json.dumps(
            release(
                [
                    {
                        "profile_id": "P1",
                        "priority": 1,
                        "conditions": {"market_segment": "sz_main"},
                    }
                ]
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config/strategy_config.json").read_text(encoding="utf-8"))
    config["paper_ab_filtered_strategy"]["c_strategy"]["factor_release_path"] = str(
        release_path
    )
    candidates = pd.DataFrame(
        [
            factor_row(
                trade_date="20250102", ts_code="000001.SZ", name="测试",
                is_st=False,
            ),
            factor_row(
                trade_date="20250102", ts_code="830001.BJ", name="测试北交",
                market_segment="bj", is_st=False,
            ),
        ]
    )

    display, _generator, filtered, loaded = build_c_factor_filtered_pool(
        root / "config/strategy_config.json", config, candidates
    )

    assert display == [{"column": "factor_union_release", "value": "C_TEST_UNION"}]
    assert loaded["strategy_mode"] == FACTOR_UNION_MODE
    assert filtered["ts_code"].tolist() == ["000001.SZ"]
    assert filtered["matched_c_profile_ids"].tolist() == ["P1"]


def test_all_qualified_if_branches_are_or_merged_without_best_subset() -> None:
    rows = []
    for index, (segment, chain) in enumerate(
        [("sz_main", "15_30"), ("bj", "3_8"), ("chi_next", "8_15")], 1
    ):
        rows.append(
            factor_row(
                trade_date=f"2025010{index}",
                ts_code=f"00000{index}.SZ",
                name="测试",
                market_segment=segment,
                market_chain_count_bucket=chain,
                candidate_rank=index,
                _risk_rejected=False,
                status="OK",
                exit_date=f"2025011{index}",
                account_return=0.03,
                exit_hit_limit_up=False,
            )
        )
    pool = add_factor_values(pd.DataFrame(rows))
    conditions = {
        "P1": {"market_segment": "sz_main"},
        "P2": {"market_chain_count_bucket": "3_8"},
    }
    qualified = pd.DataFrame([{"profile_id": "P1"}, {"profile_id": "P2"}])

    picks, mask = union_picks(pool, qualified, conditions)

    assert mask.tolist() == [True, True, False]
    assert picks["ts_code"].tolist() == ["000001.SZ", "000002.SZ"]


def test_thresholds_are_strictly_greater_not_greater_or_equal() -> None:
    metrics = basic_metrics([0.02, 0.02, 0.02, -0.02])

    assert np.isclose(metrics["win_rate"], 0.75)
    assert metrics["avg_account_return"] < 0.02
    assert profile_identifier({"market_segment": "sz_main"}).startswith("CIF_")
