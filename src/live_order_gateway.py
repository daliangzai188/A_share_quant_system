from __future__ import annotations

import glob
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from src.broker_adapter import OrderRequest, PositionSnapshot
from src.qmt_adapter import QMTBrokerAdapter, tushare_to_qmt_code
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger, setup_logger
from src.utils.time_utils import now_beijing


class LiveOrderGateway:
    """计划单到 QMT 实盘的安全网关。

    默认只做只读和预览。真实下单必须同时满足配置开关和命令行确认。
    """

    def __init__(self, runtime_config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.runtime_config_path = runtime_config_path
        self.config = load_json_config(runtime_config_path)
        logging_config = self.config.get("logging", {})
        setup_logger(
            log_dir=self.project_root / logging_config.get("log_dir", "logs"),
            log_file=logging_config.get("log_file", "a_share_quant.log"),
            level=logging_config.get("level", "INFO"),
        )
        self.logger = get_logger("live_order_gateway")
        self.broker_config = self.config.get("broker", {})
        self.live_config = self.config.get("live_trade", {})

    def assert_qmt_enabled(self) -> None:
        if str(self.broker_config.get("adapter", "")).lower() != "qmt":
            raise RuntimeError("当前 broker.adapter 不是 qmt，不能运行 QMT 网关。")
        if not bool(self.broker_config.get("enabled", False)):
            raise RuntimeError("broker.enabled=false，不能连接券商。")
        if not bool(self.config.get("broker_adapter_enabled", False)):
            raise RuntimeError("broker_adapter_enabled=false，不能连接券商。")
        if not bool(self.config.get("qmt_enabled", False)):
            raise RuntimeError("qmt_enabled=false，不能连接 QMT。")

    def assert_real_order_allowed(self, confirm: str) -> None:
        self.assert_qmt_enabled()
        expected = str(self.live_config.get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"))
        if str(self.config.get("trade_mode", "")).lower() != "live":
            raise RuntimeError("trade_mode 不是 live，拒绝真实下单。")
        if not bool(self.live_config.get("enabled", False)):
            raise RuntimeError("live_trade.enabled=false，拒绝真实下单。")
        if not bool(self.live_config.get("real_order_enabled", False)):
            raise RuntimeError("live_trade.real_order_enabled=false，拒绝真实下单。")
        if confirm != expected:
            raise RuntimeError(f"确认文本不匹配，拒绝真实下单。需要: {expected}")

    def assert_small_cash_test_allowed(self) -> None:
        self.assert_qmt_enabled()
        if str(self.config.get("trade_mode", "")).lower() != "live":
            raise RuntimeError("trade_mode 不是 live，拒绝小资金测试下单。")
        if not bool(self.live_config.get("enabled", False)):
            raise RuntimeError("live_trade.enabled=false，拒绝小资金测试下单。")
        if not bool(self.live_config.get("small_cash_test_enabled", False)):
            raise RuntimeError("live_trade.small_cash_test_enabled=false，拒绝小资金测试下单。")

    def create_adapter(self) -> QMTBrokerAdapter:
        self.assert_qmt_enabled()
        return QMTBrokerAdapter.from_config(self.broker_config)

    def latest_planned_orders_path(self) -> Path:
        pattern = str(self.project_root / "reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")
        files = [Path(path) for path in glob.glob(pattern)]
        if not files:
            raise RuntimeError("没有找到 A+B+C 每日操作台 planned_orders.csv，请先运行每日操作台。")
        return max(files, key=lambda path: path.stat().st_mtime)

    def load_planned_orders(self, planned_orders_path: str | Path) -> tuple[Path, pd.DataFrame]:
        path = self.latest_planned_orders_path() if str(planned_orders_path) == "latest" else Path(planned_orders_path)
        if not path.is_absolute():
            path = self.project_root / path
        if not path.exists():
            raise FileNotFoundError(f"计划单不存在: {path}")
        try:
            orders = pd.read_csv(path, low_memory=False)
        except EmptyDataError:
            orders = pd.DataFrame(
                columns=[
                    "signal_date",
                    "strategy_leg",
                    "planned_order_date",
                    "side",
                    "ts_code",
                    "name",
                    "round_lot_shares",
                    "reference_price",
                    "planned_amount_by_equity",
                ]
            )
        return path, orders

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        numeric = pd.to_numeric(value, errors="coerce")
        return default if pd.isna(numeric) else float(numeric)

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        numeric = pd.to_numeric(value, errors="coerce")
        return default if pd.isna(numeric) else int(float(numeric))

    @staticmethod
    def active_order_match(order: dict[str, Any], ts_code: str, side: str) -> bool:
        text = " ".join(str(value) for value in order.values()).upper()
        side_text = side.upper()
        active_words = ["REPORTED", "SUBMITTED", "PART", "未成交", "已报", "部成", "待报"]
        return ts_code.upper() in text and side_text in text and any(word in text for word in active_words)

    @staticmethod
    def live_preview_columns() -> list[str]:
        return [
            "ts_code",
            "broker_code",
            "name",
            "strategy_leg",
            "side",
            "quantity",
            "price_type",
            "price",
            "reference_price",
            "last_price",
            "upper_limit",
            "lower_limit",
            "planned_amount_by_equity",
            "estimated_live_amount",
            "available_cash",
            "total_asset",
            "current_market_value",
            "strategy_name",
            "remark",
            "validation_status",
            "reject_reasons",
            "real_order_enabled",
        ]

    def validate_planned_orders(
        self,
        planned_orders: pd.DataFrame,
        account_cash: float,
        open_orders: list[dict[str, Any]],
        quote_map: dict[str, Any],
        positions: list[PositionSnapshot] | None = None,
        account_total_asset: float | None = None,
        current_market_value: float | None = None,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        max_order_amount = float(self.live_config.get("max_single_order_amount", 50000))
        max_position_pct = float(self.live_config.get("max_position_pct", 0.8))
        max_total_position_pct = float(self.live_config.get("max_total_position_pct", 0.8))
        round_lot_size = int(self.live_config.get("round_lot_size", 100))
        limit_tolerance = float(self.live_config.get("limit_price_tolerance", 0.001))
        default_price_type = str(self.live_config.get("default_price_type", "LATEST_PRICE"))
        strategy_name = str(self.live_config.get("strategy_name", "A_SYSTEM_ABC"))
        reject_limit_up_buy = bool(self.live_config.get("reject_limit_up_buy", True))
        reject_limit_down_sell = bool(self.live_config.get("reject_limit_down_sell", True))
        duplicate_order_check = bool(self.live_config.get("duplicate_order_check", True))
        enforce_trading_time = bool(self.live_config.get("enforce_trading_time", True))
        total_asset = float(account_total_asset or account_cash)
        market_value = float(current_market_value or 0.0)
        position_map = self.build_position_map(positions or [])

        for _, order in planned_orders.iterrows():
            ts_code = str(order.get("ts_code", "")).strip().upper()
            side = str(order.get("side", "BUY")).strip().upper()
            quantity = self._to_int(order.get("round_lot_shares", order.get("estimated_shares", 0)))
            reference_price = self._to_float(order.get("reference_price", 0.0))
            planned_amount = self._to_float(order.get("planned_amount_by_equity", 0.0))
            broker_code = tushare_to_qmt_code(ts_code)
            quote = quote_map.get(ts_code)
            last_price = float(getattr(quote, "last_price", 0.0) or reference_price)
            upper_limit = float(getattr(quote, "upper_limit", 0.0) or 0.0)
            lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0)
            suspended = bool(getattr(quote, "suspended", False)) if quote is not None else False
            estimated_amount = quantity * (last_price if last_price > 0 else reference_price)
            current_position = position_map.get(ts_code, {"can_use_volume": 0, "market_value": 0.0})
            current_stock_market_value = float(current_position.get("market_value", 0.0))

            reject_reasons = []
            allowed_exchanges = [e.upper() for e in self.live_config.get("allowed_exchanges", [])]
            ts_exchange = ts_code.split(".")[-1] if "." in ts_code else ""
            if not ts_code:
                reject_reasons.append("EMPTY_TS_CODE")
            if allowed_exchanges and ts_exchange not in allowed_exchanges:
                reject_reasons.append(f"EXCHANGE_NOT_PERMITTED({ts_exchange})")
            if side not in {"BUY", "SELL"}:
                reject_reasons.append("UNSUPPORTED_SIDE")
            if quantity <= 0:
                reject_reasons.append("EMPTY_OR_ZERO_QUANTITY")
            if round_lot_size > 0 and quantity % round_lot_size != 0:
                reject_reasons.append("NOT_ROUND_LOT")
            if quote is None:
                reject_reasons.append("NO_REALTIME_QUOTE")
            if suspended:
                reject_reasons.append("SUSPENDED")
            if side == "BUY" and not bool(self.live_config.get("allow_buy", False)):
                reject_reasons.append("BUY_DISABLED")
            if side == "SELL" and not bool(self.live_config.get("allow_sell", False)):
                reject_reasons.append("SELL_DISABLED")
            if enforce_trading_time and not self.is_trading_time(side):
                reject_reasons.append("OUTSIDE_TRADING_TIME")
            if side == "BUY" and reject_limit_up_buy and upper_limit > 0 and last_price >= upper_limit * (1 - limit_tolerance):
                reject_reasons.append("LIMIT_UP_BUY_REJECTED")
            if side == "SELL" and reject_limit_down_sell and lower_limit > 0 and last_price <= lower_limit * (1 + limit_tolerance):
                reject_reasons.append("LIMIT_DOWN_SELL_REJECTED")
            if side == "SELL" and quantity > int(current_position.get("can_use_volume", 0)):
                reject_reasons.append("SELL_VOLUME_NOT_AVAILABLE")
            if side == "BUY" and total_asset > 0 and current_stock_market_value + estimated_amount > total_asset * max_position_pct:
                reject_reasons.append("EXCEED_POSITION_PCT")
            if side == "BUY" and total_asset > 0 and market_value + estimated_amount > total_asset * max_total_position_pct:
                reject_reasons.append("EXCEED_TOTAL_POSITION_PCT")
            if side == "BUY" and estimated_amount > account_cash:
                reject_reasons.append("INSUFFICIENT_CASH")
            if estimated_amount >= max_order_amount:
                reject_reasons.append("EXCEED_SINGLE_ORDER_AMOUNT")
            if duplicate_order_check and any(self.active_order_match(open_order, ts_code, side) for open_order in open_orders):
                reject_reasons.append("DUPLICATE_ACTIVE_ORDER")

            rows.append(
                {
                    "ts_code": ts_code,
                    "broker_code": broker_code,
                    "name": order.get("name", ""),
                    "strategy_leg": order.get("strategy_leg", ""),
                    "side": side,
                    "quantity": quantity,
                    "price_type": default_price_type,
                    "price": 0.0,
                    "reference_price": reference_price,
                    "last_price": last_price,
                    "upper_limit": upper_limit,
                    "lower_limit": lower_limit,
                    "planned_amount_by_equity": planned_amount,
                    "estimated_live_amount": estimated_amount,
                    "available_cash": account_cash,
                    "total_asset": total_asset,
                    "current_market_value": market_value,
                    "strategy_name": strategy_name,
                    "remark": f"{strategy_name}-{order.get('signal_date', '')}-{order.get('strategy_leg', '')}",
                    "validation_status": "PASS" if not reject_reasons else "REJECTED",
                    "reject_reasons": "|".join(reject_reasons),
                    "real_order_enabled": bool(self.live_config.get("real_order_enabled", False)),
                }
            )
        return pd.DataFrame(rows, columns=self.live_preview_columns())

    def account_check(self, output_prefix: str | Path) -> dict[str, Path]:
        adapter = self.create_adapter()
        output_prefix = Path(output_prefix)
        if not output_prefix.is_absolute():
            output_prefix = self.project_root / output_prefix
        mkdir_p(output_prefix.parent)

        try:
            adapter.connect()
            account = adapter.query_account()
            positions = adapter.query_positions()
            orders = adapter.query_orders()
            trades = adapter.query_trades()
        finally:
            adapter.disconnect()

        paths = {
            "account": output_prefix.with_name(output_prefix.name + "_account.csv"),
            "positions": output_prefix.with_name(output_prefix.name + "_positions.csv"),
            "orders": output_prefix.with_name(output_prefix.name + "_orders.csv"),
            "trades": output_prefix.with_name(output_prefix.name + "_trades.csv"),
        }
        pd.DataFrame([asdict(account)]).to_csv(paths["account"], index=False, encoding="utf-8-sig")
        pd.DataFrame([asdict(row) for row in positions]).to_csv(paths["positions"], index=False, encoding="utf-8-sig")
        pd.DataFrame(orders).to_csv(paths["orders"], index=False, encoding="utf-8-sig")
        pd.DataFrame(trades).to_csv(paths["trades"], index=False, encoding="utf-8-sig")
        return paths

    def preview(self, planned_orders_path: str | Path, output_prefix: str | Path) -> dict[str, Path]:
        adapter = self.create_adapter()
        actual_path, planned_orders = self.load_planned_orders(planned_orders_path)
        output_prefix = Path(output_prefix)
        if not output_prefix.is_absolute():
            output_prefix = self.project_root / output_prefix
        mkdir_p(output_prefix.parent)

        try:
            adapter.connect()
            account = adapter.query_account()
            open_orders = adapter.query_orders()
            positions = adapter.query_positions()
            ts_codes = sorted(planned_orders.get("ts_code", pd.Series(dtype=str)).dropna().astype(str).unique().tolist())
            quote_map = adapter.get_full_tick(ts_codes) if ts_codes else {}
            preview = self.validate_planned_orders(
                planned_orders,
                account.available_cash,
                open_orders,
                quote_map,
                positions=positions,
                account_total_asset=account.total_asset,
                current_market_value=account.market_value,
            )
        finally:
            adapter.disconnect()

        paths = {
            "source_planned_orders": actual_path,
            "preview": output_prefix.with_name(output_prefix.name + "_preview.csv"),
        }
        preview.to_csv(paths["preview"], index=False, encoding="utf-8-sig")
        return paths

    def submit(self, preview_path: str | Path, confirm: str, output_prefix: str | Path) -> dict[str, Path]:
        self.assert_real_order_allowed(confirm)
        preview_path = Path(preview_path)
        if not preview_path.is_absolute():
            preview_path = self.project_root / preview_path
        preview = pd.read_csv(preview_path, low_memory=False)
        executable = preview[
            (preview["validation_status"].astype(str) == "PASS")
            & (preview["real_order_enabled"].astype(str).str.lower().isin({"true", "1"}))
        ].copy()

        adapter = self.create_adapter()
        results = []
        try:
            adapter.connect()
            for _, row in executable.iterrows():
                side = str(row["side"]).upper()
                if bool(self.live_config.get("enforce_trading_time", True)) and not self.is_trading_time(side):
                    results.append(
                        {
                            "ts_code": str(row["ts_code"]),
                            "broker_code": str(row["broker_code"]),
                            "side": side,
                            "quantity": int(row["quantity"]),
                            "accepted": False,
                            "order_id": "",
                            "message": "OUTSIDE_TRADING_TIME_AT_SUBMIT",
                            "raw": {"source": str(preview_path)},
                        }
                    )
                    continue
                request = OrderRequest(
                    ts_code=str(row["ts_code"]),
                    broker_code=str(row["broker_code"]),
                    side=side,
                    quantity=int(row["quantity"]),
                    price_type=str(row["price_type"]),
                    price=float(row.get("price", 0.0)),
                    strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                    remark=str(row.get("remark", "")),
                )
                result = adapter.place_order(request)
                results.append(asdict(result))
        finally:
            adapter.disconnect()

        output_prefix = Path(output_prefix)
        if not output_prefix.is_absolute():
            output_prefix = self.project_root / output_prefix
        mkdir_p(output_prefix.parent)
        paths = {"orders": output_prefix.with_name(output_prefix.name + "_submitted_orders.csv")}
        pd.DataFrame(results).to_csv(paths["orders"], index=False, encoding="utf-8-sig")
        return paths

    def submit_small_cash_test(self, preview_path: str | Path, output_prefix: str | Path) -> dict[str, Path]:
        """提交 100 股小资金测试单。

        不要求命令行确认文本，但必须满足专用配置开关，并且只提交预览 PASS、
        数量不超过 small_cash_test_max_shares、金额不超过 small_cash_test_max_order_amount 的订单。
        """
        self.assert_small_cash_test_allowed()
        preview_path = Path(preview_path)
        if not preview_path.is_absolute():
            preview_path = self.project_root / preview_path
        preview = pd.read_csv(preview_path, low_memory=False)
        max_shares = int(self.live_config.get("small_cash_test_max_shares", 100))
        max_amount = float(self.live_config.get("small_cash_test_max_order_amount", 20000))
        executable = preview[preview["validation_status"].astype(str) == "PASS"].copy()

        adapter = self.create_adapter()
        results = []
        try:
            adapter.connect()
            for _, row in executable.iterrows():
                side = str(row["side"]).upper()
                quantity = int(row["quantity"])
                amount = float(row.get("estimated_live_amount", 0.0))
                reject_message = ""
                if quantity <= 0:
                    reject_message = "EMPTY_OR_ZERO_QUANTITY_AT_SUBMIT"
                elif quantity > max_shares:
                    reject_message = "EXCEED_SMALL_TEST_MAX_SHARES"
                elif amount > max_amount:
                    reject_message = "EXCEED_SMALL_TEST_MAX_AMOUNT"
                elif bool(self.live_config.get("enforce_trading_time", True)) and not self.is_trading_time(side):
                    reject_message = "OUTSIDE_TRADING_TIME_AT_SUBMIT"

                if reject_message:
                    results.append(
                        {
                            "ts_code": str(row["ts_code"]),
                            "broker_code": str(row["broker_code"]),
                            "side": side,
                            "quantity": quantity,
                            "accepted": False,
                            "order_id": "",
                            "message": reject_message,
                            "raw": {"source": str(preview_path), "mode": "small_cash_test"},
                        }
                    )
                    continue

                request = OrderRequest(
                    ts_code=str(row["ts_code"]),
                    broker_code=str(row["broker_code"]),
                    side=side,
                    quantity=quantity,
                    price_type=str(row["price_type"]),
                    price=float(row.get("price", 0.0)),
                    strategy_name=str(row.get("strategy_name", "A_SYSTEM_SMALL_TEST")),
                    remark=f"SMALL_TEST-{row.get('remark', '')}",
                )
                result = adapter.place_order(request)
                results.append(asdict(result))
        finally:
            adapter.disconnect()

        output_prefix = Path(output_prefix)
        if not output_prefix.is_absolute():
            output_prefix = self.project_root / output_prefix
        mkdir_p(output_prefix.parent)
        paths = {"orders": output_prefix.with_name(output_prefix.name + "_small_test_submitted_orders.csv")}
        pd.DataFrame(results).to_csv(paths["orders"], index=False, encoding="utf-8-sig")
        return paths

    @staticmethod
    def build_position_map(positions: list[PositionSnapshot]) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for position in positions:
            result[str(position.ts_code).upper()] = {
                "volume": int(position.volume),
                "can_use_volume": int(position.can_use_volume),
                "market_value": float(position.market_value),
            }
        return result

    @staticmethod
    def is_trading_time(side: str) -> bool:
        now = now_beijing()
        hhmm = now.hour * 100 + now.minute
        if side.upper() == "BUY":
            # 915-929：集合竞价预挂买单（T0收盘后已有T+1开仓计划时，9:15起挂涨停价排队）
            # 930-1455：连续竞价（含午间11:30-13:00，QMT午间挂单13:00生效）
            return 915 <= hhmm <= 1455
        if side.upper() == "SELL":
            # 915-925：集合竞价（9:23挂跌停价平仓）；930-1130、1300-1500：连续竞价
            return 915 <= hhmm <= 1130 or 1300 <= hhmm <= 1500
        return False
