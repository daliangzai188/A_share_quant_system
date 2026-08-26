from __future__ import annotations

from scripts import monitor_strategy_d_intraday as d_monitor
from scripts.monitor_strategy_d_intraday import StrategyDMonitor
from src.broker_adapter import OrderFill


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class _FillBroker:
    def __init__(self, results: list[OrderFill | Exception]) -> None:
        self.results = list(results)
        self.calls = 0

    def get_order_fill(self, _order_id: str) -> OrderFill:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _monitor(
    broker: _FillBroker,
    records: list[dict] | None = None,
) -> StrategyDMonitor:
    monitor = object.__new__(StrategyDMonitor)
    monitor.broker = broker
    monitor.logger = _Logger()
    monitor.session_orders = {"OID-D": "301630.SZ"}
    monitor.session_order_details = {
        "OID-D": {
            "order_id": "OID-D",
            "ts_code": "301630.SZ",
            "name": "同宇新材",
            "shares": 300,
            "buy_price": 196.81,
            "actual_amount": 59_043.0,
            "buy_date": "20260826",
            "strategy_leg": "D",
        }
    }
    monitor.position_recorder = (records if records is not None else []).append
    monitor.position_opened = False
    monitor.waiting_order_only = True
    return monitor


def _reported_zero_fill() -> OrderFill:
    return OrderFill(
        order_id="OID-D",
        status_code=50,
        status_text="已报",
        filled_qty=0,
        avg_price=0.0,
        is_terminal=False,
        is_filled=False,
    )


def _full_fill() -> OrderFill:
    return OrderFill(
        order_id="OID-D",
        status_code=56,
        status_text="已成",
        filled_qty=300,
        avg_price=196.81,
        is_terminal=True,
        is_filled=True,
    )


def test_submit_confirmation_does_not_treat_reported_as_fill_complete(
    monkeypatch,
) -> None:
    monkeypatch.setattr(d_monitor.time, "sleep", lambda _seconds: None)
    broker = _FillBroker([_reported_zero_fill(), _full_fill()])
    monitor = _monitor(broker)

    fill = monitor._confirm_submitted_order("OID-D", "301630.SZ")

    assert broker.calls == 2
    assert fill.is_filled is True
    assert fill.filled_qty == 300


def test_active_order_fill_is_projected_from_same_qmt_order(monkeypatch) -> None:
    monkeypatch.setattr(d_monitor, "notify", lambda *_args, **_kwargs: True)
    records: list[dict] = []
    broker = _FillBroker([_reported_zero_fill(), _full_fill()])
    monitor = _monitor(broker, records)

    assert monitor._reconcile_active_d_orders_once() is False
    assert records == []
    assert monitor._reconcile_active_d_orders_once() is True

    assert len(records) == 1
    assert records[0]["order_id"] == "OID-D"
    assert records[0]["ts_code"] == "301630.SZ"
    assert records[0]["shares"] == 300
    assert records[0]["strategy_leg"] == "D"
    assert records[0]["planned_exit_date"] == "20260828"
    assert monitor.position_opened is True
    assert monitor.waiting_order_only is False


def test_repeated_cumulative_fill_does_not_duplicate_position_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(d_monitor, "notify", lambda *_args, **_kwargs: True)
    records: list[dict] = []
    partial = OrderFill(
        order_id="OID-D",
        status_code=55,
        status_text="部成",
        filled_qty=100,
        avg_price=196.80,
        is_partial=True,
    )
    broker = _FillBroker([partial, partial, _full_fill()])
    monitor = _monitor(broker, records)

    assert monitor._reconcile_active_d_orders_once() is False
    assert monitor._reconcile_active_d_orders_once() is False
    assert monitor._reconcile_active_d_orders_once() is True

    assert [record["shares"] for record in records] == [100, 300]
