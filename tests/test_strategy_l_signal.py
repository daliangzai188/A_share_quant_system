"""L 晚间信号脚本（scripts/run_strategy_l_signal.py）的纯逻辑：取全判断、注册表格式、信号日推算、影子净值。"""
import hashlib
import json
from datetime import datetime

import pandas as pd

import scripts.run_strategy_l_signal as S
from src.strategy_l import l_shadow_nav


def _tl(rows):
    return pd.DataFrame([{"ts_code": c, "name": "x", "amount": 1.0, "l_amount": 1.0, "l_sell": 1.0, "reason": r} for c, r in rows])


def test_lhb_complete_requires_seats_for_every_board_row():
    keys = [("300001.SZ", "日振幅值达到15%的前5只证券"), ("300002.SZ", "日换手率达到20%的前5只证券")]
    tl = _tl(keys)
    ti = pd.DataFrame([{"ts_code": code, "reason": reason, "side": side, "buy": 1.0, "sell": 1.0}
                       for code, reason in keys for side in ("0", "1")])
    ok, why = S.lhb_complete(tl, ti.iloc[:3])
    assert not ok and "1 条公告" in why
    assert S.lhb_complete(tl, ti) == (True, "")
    ti.loc[3, "sell"] = float("nan")
    assert S.lhb_complete(tl, ti)[0] is False
    assert S.lhb_complete(pd.DataFrame(), ti)[0] is False


def test_registry_value_carries_dates_payload_and_checksum():
    plan = {"signal_date": "20261126", "planned_buy_date": "20261127", "ts_code": "300001.SZ", "l_allowed": True}
    value = S.registry_value(plan)
    head, sig, buy, body, digest = value.split("|")
    assert (head, sig, buy) == ("v1", "20261126", "20261127")
    assert json.loads(body) == plan
    assert digest == hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def test_default_signal_date_switches_at_16():
    days = ["20261125", "20261126", "20261127"]
    assert S.default_signal_date(days, datetime(2026, 11, 26, 15, 59)) == "20261125"
    assert S.default_signal_date(days, datetime(2026, 11, 26, 16, 30)) == "20261126"
    assert S.default_signal_date(days, datetime(2026, 11, 27, 8, 20)) == "20261126"


def test_l_shadow_nav_blocks_sell_day_and_compounds_90_percent():
    days = ["d1", "d2", "d3", "d4"]
    picks = {"d1": ["A"], "d2": ["B"], "d3": ["C"]}
    out = {("A", "d1"): ("d2", 10.0, 0.10), ("C", "d3"): ("d4", 10.0, -0.10)}
    nav = l_shadow_nav(days, picks, lambda c, d: out.get((c, d)), start="d1")
    # d1 买 A，d2 卖出入账（卖出当天不再买 B），d3 买 C，d4 卖出
    assert abs(nav["d2"] - (1 + 0.9 * 0.10 - 0.9 * 0.0007)) < 1e-12
    assert abs(nav["d4"] - nav["d2"] * (1 + 0.9 * -0.10 - 0.9 * 0.0007)) < 1e-12
