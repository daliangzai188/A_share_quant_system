"""策略 L 选股（src/strategy_l.py）的规则测试：公告去重、席位指标、两个分支的过滤与排序。

与研究的逐日一致性（1397 个买入日前 3 名全部相同）在研究目录 step15_prod_equiv.py 里核对，这里用小数据锁规则。
"""
import math

import pandas as pd

import src.strategy_l as SL


def _days(n=70):
    import datetime
    d0 = datetime.date(2024, 1, 1)
    out = []
    d = d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += datetime.timedelta(days=1)
    return out


def _bar(pre=10.0, op=10.0, hi=10.5, lo=9.8, cl=10.2, amt=1000.0, vol=100.0):
    return {"pre_close": pre, "open": op, "high": hi, "low": lo, "close": cl, "amount": amt, "vol": vol}


def _seats(code, reason, buys, sellers):
    rows = [{"ts_code": code, "reason": reason, "exalter": "买%d" % k, "buy": b, "sell": 0.0, "side": "0"} for k, b in enumerate(buys)]
    rows += [{"ts_code": code, "reason": reason, "exalter": s, "buy": 0.0, "sell": 1.0, "side": "1"} for s in sellers]
    return rows


def test_board_period_and_canonical_prefers_single_day_then_amount():
    assert SL.board_period("日涨幅偏离值达到7%的前5只证券") == 1
    assert SL.board_period("连续三个交易日内，涨幅偏离值累计达到20%的证券") == 3
    assert SL.board_period("严重异常期间日收盘价格涨幅偏离值累计达到100%的证券") == 0
    tl = pd.DataFrame([
        {"ts_code": "300001.SZ", "name": "甲", "amount": 5.0, "l_amount": 1.0, "l_sell": 1.0, "reason": "连续三个交易日内，涨幅偏离值累计达到20%的证券"},
        {"ts_code": "300001.SZ", "name": "甲", "amount": 3.0, "l_amount": 1.0, "l_sell": 1.0, "reason": "日振幅值达到15%的前5只证券"},
        {"ts_code": "300001.SZ", "name": "甲", "amount": 4.0, "l_amount": 1.0, "l_sell": 1.0, "reason": "日换手率达到20%的前5只证券"},
        {"ts_code": "113001.SH", "name": "转债", "amount": 9.0, "l_amount": 1.0, "l_sell": 1.0, "reason": "日涨幅达到15%的前5只可转债"},
    ])
    c = SL.canonical_board(tl)
    assert list(c.ts_code) == ["300001.SZ"]
    assert c.iloc[0].reason == "日换手率达到20%的前5只证券"


def test_seat_metrics_excludes_institution_and_connect_from_named_sellers():
    ti = pd.DataFrame(_seats("300001.SZ", "r", [60.0, 30.0, 10.0], ["机构专用", "深股通专用", "甲营业部", "甲营业部", "乙营业部"]))
    m = SL.seat_metrics(ti).iloc[0]
    assert abs(m.buy2_share - 0.9) < 1e-12
    assert abs(m.buy_hhi - (0.36 + 0.09 + 0.01)) < 1e-12
    assert m.named_sell == 2.0


def _inputs(mean, *, board, seats, bars_t, total_share=None, history_board=None, name="正常股"):
    days = _days()
    t = days[-1]
    bars = {d: {} for d in days}
    for code, b in bars_t.items():
        for d in days[:-1]:
            bars[d][code] = _bar()
        bars[t][code] = b
    return SL.LInputs(signal_date=t, days=days, bars=bars, pct_chg_mean=mean, top_list=pd.DataFrame(board),
                      top_inst=pd.DataFrame(seats), board_history=history_board or {}, total_share=total_share or {},
                      list_dates={c: "20200101" for c in bars_t}, name_on=lambda code, day: name)


def _board_row(code, l_amount=5e8, reason="日振幅值达到15%的前5只证券"):
    return {"ts_code": code, "name": "x", "amount": 1e9, "l_amount": l_amount, "l_sell": 1.0, "reason": reason}


def test_branch_one_filters_amplitude_and_l_amount_and_ranks_by_limit_up_count():
    big = _bar(pre=10.0, hi=12.0, lo=9.9, cl=12.0)          # 振幅 21%、收盘涨停（20cm）
    small = _bar(pre=10.0, hi=11.0, lo=9.9, cl=11.0)        # 振幅 11%
    board = [_board_row("300001.SZ"), _board_row("300002.SZ"), _board_row("300003.SZ", l_amount=1.2e9)]
    # 买入前两名占比：300001 0.67（分位 1/3）、300002 0.70、300003 0.80（分位 100% 超上限，但它先因成交额超 10 亿被排除）
    seats = _seats("300001.SZ", "日振幅值达到15%的前5只证券", [34, 33, 33], ["甲", "乙", "丙"]) + \
        _seats("300002.SZ", "日振幅值达到15%的前5只证券", [40, 30, 30], ["甲", "乙", "丙"]) + \
        _seats("300003.SZ", "日振幅值达到15%的前5只证券", [50, 30, 20], ["甲", "乙", "丙"])
    out, diag = SL.select_l_candidates(_inputs(0.5, board=board, seats=seats,
                                                bars_t={"300001.SZ": big, "300002.SZ": small, "300003.SZ": big}))
    assert diag["branch"] == 1
    assert list(out.ts_code) == ["300001.SZ"]          # 300002 振幅不够，300003 龙虎榜成交额超 10 亿
    assert out.iloc[0].limit_up_count_60 == 1
    assert diag["rejects"] == {"amplitude": 1, "l_amount": 1}


def test_branch_two_requires_three_named_sellers_and_ranks_by_board_days():
    big = _bar(pre=10.0, hi=12.0, lo=9.9, cl=11.5)
    board = [_board_row("300001.SZ"), _board_row("300002.SZ"), _board_row("300003.SZ")]
    reason = "日振幅值达到15%的前5只证券"
    seats = _seats("300001.SZ", reason, [90, 5, 5], ["甲", "乙", "丙"]) + \
        _seats("300002.SZ", reason, [90, 5, 5], ["甲", "乙", "丙", "丁"]) + \
        _seats("300003.SZ", reason, [90, 5, 5], ["甲", "乙"])           # 只有 2 家普通营业部
    days = _days()
    hist = {days[-5]: {"300002.SZ"}, days[-6]: {"300002.SZ"}, days[-7]: {"300001.SZ"}}
    out, diag = SL.select_l_candidates(_inputs(-1.0, board=board, seats=seats, history_board=hist,
                                                total_share={"300001.SZ": 5e4, "300002.SZ": 5e4, "300003.SZ": 5e4},
                                                bars_t={"300001.SZ": big, "300002.SZ": big, "300003.SZ": big}))
    assert diag["branch"] == 2
    assert list(out.ts_code) == ["300002.SZ", "300001.SZ"]   # 上榜天数 2 > 1；300003 卖方营业部不足 3 家
    assert diag["rejects"].get("named_sell") == 1


def test_st_and_missing_mean_go_to_branch_two_and_st_excluded():
    big = _bar(pre=10.0, hi=12.0, lo=9.9, cl=11.5)
    reason = "日振幅值达到15%的前5只证券"
    out, diag = SL.select_l_candidates(_inputs(None, board=[_board_row("300001.SZ")],
                                                seats=_seats("300001.SZ", reason, [90, 5, 5], ["甲", "乙", "丙"]),
                                                bars_t={"300001.SZ": big}, total_share={"300001.SZ": 5e4}, name="ST某某"))
    assert diag["branch"] == 2
    assert out.empty and diag["rejects"] == {"st": 1}
    assert not math.isnan(SL.AMPLITUDE_MIN)
