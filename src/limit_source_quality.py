"""Identify untradable provider placeholders using actual daily quote coverage."""
from __future__ import annotations

import pandas as pd


def suspended_placeholder_mask(frame: pd.DataFrame, daily_codes: set[str]) -> pd.Series:
    """Zero-price/-100%/zero-time entries without a daily quote are not limit-ups.

    Merely missing a joined fundamental row, or a positive-price quote, cannot
    trigger this filter. Raw source records must remain preserved in the CSV.
    """
    columns = {"ts_code", "close", "pct_chg", "fd_amount", "first_time", "last_time", "open_times"}
    if columns - set(frame.columns):
        return pd.Series(False, index=frame.index)
    return (
        pd.to_numeric(frame["close"], errors="coerce").eq(0)
        & pd.to_numeric(frame["pct_chg"], errors="coerce").eq(-100)
        & pd.to_numeric(frame["fd_amount"], errors="coerce").eq(0)
        & pd.to_numeric(frame["first_time"], errors="coerce").eq(0)
        & pd.to_numeric(frame["last_time"], errors="coerce").eq(0)
        & pd.to_numeric(frame["open_times"], errors="coerce").eq(0)
        & ~frame["ts_code"].astype(str).isin(daily_codes)
    )
