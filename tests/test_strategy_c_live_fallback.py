from __future__ import annotations

import pandas as pd

from scripts.run_paper_ab_filtered_daily_ops import resolve_ac_selected_leg


def test_c_accepted_fallback_is_not_blocked_by_other_rejected_candidates() -> None:
    accepted = pd.Series({"ts_code": "000001.SZ"})
    rejected = pd.DataFrame(
        [
            {"ts_code": "000002.SZ", "risk_rejected": True},
            {"ts_code": "000003.SZ", "risk_rejected": True},
        ]
    )

    leg, status = resolve_ac_selected_leg(None, accepted, rejected)

    assert leg == "C"
    assert status == "A_NO_SELECTED_C_SELECTED:AFTER_2_RISK_REJECTED_FALLBACK"


def test_a_still_has_priority_over_c_fallback() -> None:
    a_selected = pd.Series({"ts_code": "000010.SZ"})
    c_selected = pd.Series({"ts_code": "000011.SZ"})

    leg, status = resolve_ac_selected_leg(
        a_selected,
        c_selected,
        pd.DataFrame([{"ts_code": "000012.SZ"}]),
    )

    assert leg == "A"
    assert status == "A_SELECTED_HAS_PRIORITY"
