"""收盘汇总的持仓描述：按股票计只数，逐只写策略、股票、股数、市值收益、计划卖出时间。"""

from src.broker_adapter import PositionSnapshot
from src.daemon.pipeline import _closing_holdings_text


def _slice(code, name, leg, shares, price, exit_time="2026-11-26 14:55", exit_date="20261126", **extra):
    row = {"ts_code": code, "name": name, "strategy_leg": leg, "shares": shares, "buy_price": price,
           "planned_exit_time": exit_time, "planned_exit_date": exit_date, "status": "open"}
    row.update(extra)
    return row


def _broker(code, volume, market_value):
    return PositionSnapshot(account_id="x", ts_code=code, broker_code=code, volume=volume, market_value=market_value)


def test_two_slices_of_one_stock_count_as_one():
    # 2026-11-24 创新医疗：竞价1200股@14.66、平滑段100股@14.68，记成两条持仓记录
    rows = [_slice("002173.SZ", "创新医疗", "C", 1200, 14.66), _slice("002173.SZ", "创新医疗", "C", 100, 14.68)]
    text = _closing_holdings_text(rows, [_broker("002173.SZ", 1300, 18915.0)])
    assert text == "持仓1只：C 002173.SZ 创新医疗 1300股 市值1.89万 收益-0.76% 计划11-26 14:55卖出"


def test_without_broker_positions_shows_cost():
    rows = [_slice("002173.SZ", "创新医疗", "C", 1200, 14.66), _slice("002173.SZ", "创新医疗", "C", 100, 14.68)]
    text = _closing_holdings_text(rows, None)
    assert text == "持仓1只：C 002173.SZ 创新医疗 1300股 买入成本1.91万 计划11-26 14:55卖出"


def test_two_stocks_listed_separately():
    rows = [_slice("002652.SZ", "扬子新材", "E", 3000, 4.43, "2026-11-19 14:55", "20261119"),
            _slice("301288.SZ", "清研环境", "M", 100, 20.92, "2026-11-19 14:55", "20261119"),
            _slice("301288.SZ", "清研环境", "M", 400, 20.925, "2026-11-19 14:55", "20261119")]
    text = _closing_holdings_text(rows, [_broker("002652.SZ", 3000, 12840.0), _broker("301288.SZ", 500, 11415.0)])
    assert text.startswith("持仓2只：E 002652.SZ 扬子新材 3000股 市值1.28万 收益-3.39% 计划11-19 14:55卖出；")
    assert "M 301288.SZ 清研环境 500股 市值1.14万 收益+9.11% 计划11-19 14:55卖出" in text


def test_no_open_positions():
    assert _closing_holdings_text([], None) == "无持仓"


def test_exit_date_used_when_exit_time_missing():
    rows = [_slice("600000.SH", "浦发银行", "A", 100, 10.0, exit_time="", exit_date="20261127")]
    assert _closing_holdings_text(rows, []) == "持仓1只：A 600000.SH 浦发银行 100股 买入成本0.10万 计划11-27卖出"
