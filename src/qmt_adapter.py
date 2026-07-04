from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.broker_adapter import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerConnectionConfig,
    OrderFill,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    QuoteSnapshot,
)
from src.utils.config import get_project_root
from src.utils.logger import get_logger


def tushare_to_qmt_code(ts_code: str) -> str:
    """将 Tushare 代码转换成 QMT 常用代码。"""

    return str(ts_code).strip().upper()


def qmt_to_tushare_code(code: str) -> str:
    """将 QMT 代码转换成项目统一 Tushare 代码。"""

    return str(code).strip().upper()


# QMT/xtquant 委托状态码（xtconstant.ORDER_*）
_ORDER_STATUS_TEXT: dict[int, str] = {
    48: "未报",
    49: "待报",
    50: "已报",
    51: "已报待撤",
    52: "部成待撤",
    53: "部撤",
    54: "已撤",
    55: "部成",
    56: "已成",
    57: "废单",
    255: "未知",
}
# 终态：不会再产生新成交
_ORDER_TERMINAL_STATUS: set[int] = {53, 54, 56, 57}


def object_to_dict(value: Any) -> dict[str, Any]:
    """兼容 xtquant 返回对象、字典和普通 Python 对象。"""

    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if isinstance(attr, (str, int, float, bool, list, tuple, dict, type(None))):
            result[name] = attr
    return result


def first_present(data: dict[str, Any], names: list[str], default: Any = None) -> Any:
    """从多个可能字段名中取第一个有效值。"""

    for name in names:
        if name not in data:
            continue
        value = data[name]
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        return value
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    """安全转浮点数。"""

    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    """安全转整数。"""

    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def unique_values(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).lower()
        if key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result


class QMTBrokerAdapter(BrokerAdapter):
    """QMT / miniQMT 券商适配器。

    该类只在运行时懒加载 xtquant，开发机没有 QMT 环境时也可以正常导入项目代码。
    """

    def __init__(self, config: BrokerConnectionConfig) -> None:
        self.config = config
        self.logger = get_logger("qmt_adapter")
        self.xttrader_module: Any = None
        self.xttype_module: Any = None
        self.xtconstant_module: Any = None
        self.xtdata_module: Any = None
        self.trader: Any = None
        self.account: Any = None
        self._active_session_id: int = config.session_id
        self._active_qmt_path: str = config.qmt_path

    @classmethod
    def from_config(cls, broker_config: dict[str, Any]) -> "QMTBrokerAdapter":
        """从 config/config.json 的 broker 配置创建 QMT 适配器。"""

        project_root = get_project_root()
        # 不覆盖调用方已经设置的环境变量。
        # 启动门禁和自动重连会把“上次验证成功的 qmt_path/session_id”临时写入环境变量，
        # 如果这里 override=True，会被 .env 里的默认 session_id 覆盖回旧值，
        # 导致日志显示尝试缓存会话，实际仍连接默认 1001，启动被无效失败拖慢。
        load_dotenv(project_root / ".env", override=False)

        account_id = os.getenv(str(broker_config.get("account_id_env", "QMT_ACCOUNT_ID")), "").strip()
        qmt_path = os.getenv(str(broker_config.get("qmt_path_env", "QMT_PATH")), "").strip()
        session_id_env = str(broker_config.get("session_id_env", "QMT_SESSION_ID"))
        session_id = to_int(os.getenv(session_id_env), int(broker_config.get("session_id", 1001)))
        account_type = os.getenv(
            str(broker_config.get("account_type_env", "QMT_ACCOUNT_TYPE")),
            str(broker_config.get("account_type", "STOCK")),
        ).strip()

        connection_config = BrokerConnectionConfig(
            broker_name=str(broker_config.get("broker_name", "qmt")),
            account_id=account_id,
            account_type=account_type or "STOCK",
            qmt_path=qmt_path,
            session_id=session_id,
        )
        return cls(connection_config)

    def _load_xtquant(self) -> None:
        if self.xttrader_module is not None:
            return
        try:
            from xtquant import xtconstant, xtdata  # type: ignore
            from xtquant import xttrader, xttype  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "未找到 xtquant。请先在券商 QMT/miniQMT 环境中安装或配置 xtquant，"
                "并使用 QMT 支持的 Python 环境运行本脚本。"
            ) from exc

        self.xttrader_module = xttrader
        self.xttype_module = xttype
        self.xtconstant_module = xtconstant
        self.xtdata_module = xtdata

    def _constant(self, name: str, fallback: Any) -> Any:
        if self.xtconstant_module is None:
            return fallback
        return getattr(self.xtconstant_module, name, fallback)

    def connect(self, preferred_only: bool = False) -> None:
        self._load_xtquant()
        if not self.config.account_id:
            raise RuntimeError("未配置 QMT_ACCOUNT_ID，不能连接 QMT 账户。")
        if not self.config.qmt_path:
            raise RuntimeError("未配置 QMT_PATH，不能连接 QMT 客户端。")

        configured_path = Path(self.config.qmt_path).expanduser()
        candidate_paths = unique_values(
            [
                configured_path,
                configured_path / "userdata_mini",
                configured_path / "userdata",
                configured_path.parent / "userdata_mini",
                configured_path.parent / "userdata",
            ]
        )
        candidate_paths = [path for path in candidate_paths if path.exists()]
        session_ids = unique_values([self.config.session_id, 1001, 1002, 10001, 20001, 31001])
        if preferred_only:
            candidate_paths = candidate_paths[:1]
            session_ids = session_ids[:1]
        errors: list[str] = []

        for qmt_path in candidate_paths:
            for session_id in session_ids:
                trader = None
                try:
                    trader = self.xttrader_module.XtQuantTrader(str(qmt_path), int(session_id))
                    account = self.xttype_module.StockAccount(self.config.account_id, self.config.account_type)
                    trader.start()
                    connect_result = trader.connect()
                    if connect_result not in {0, True, None}:
                        errors.append(f"path={qmt_path}; session_id={session_id}; connect={connect_result}")
                        if hasattr(trader, "stop"):
                            trader.stop()
                        continue
                    self.trader = trader
                    self.account = account
                    self._active_session_id = int(session_id)
                    self._active_qmt_path = str(qmt_path)
                    if hasattr(self.trader, "subscribe"):
                        subscribe_result = self.trader.subscribe(self.account)
                        self.logger.debug("QMT 账户订阅结果: %s", subscribe_result)
                    self.logger.info(
                        "QMT 已连接: account_id=%s qmt_path=%s session_id=%s",
                        self.config.account_id,
                        qmt_path,
                        session_id,
                    )
                    return
                except Exception as exc:
                    errors.append(f"path={qmt_path}; session_id={session_id}; error={exc.__class__.__name__}: {exc}")
                    if trader is not None and hasattr(trader, "stop"):
                        try:
                            trader.stop()
                        except Exception:
                            pass

        raise RuntimeError("QMT connect 全部失败: " + " | ".join(errors))

    def disconnect(self) -> None:
        if self.trader is not None and hasattr(self.trader, "stop"):
            self.trader.stop()
        self.logger.debug("QMT 连接已关闭。")

    def query_account(self) -> AccountSnapshot:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")
        raw_asset = self.trader.query_stock_asset(self.account)
        data = object_to_dict(raw_asset)
        return AccountSnapshot(
            account_id=self.config.account_id,
            cash=to_float(first_present(data, ["cash", "m_dCash", "enable_balance", "available_cash"])),
            available_cash=to_float(
                first_present(data, ["available_cash", "cash", "m_dAvailable", "enable_balance", "m_dCash"])
            ),
            total_asset=to_float(first_present(data, ["total_asset", "m_dBalance", "asset_balance", "total_assets"])),
            market_value=to_float(first_present(data, ["market_value", "m_dMarketValue", "stock_value"])),
            frozen_cash=to_float(first_present(data, ["frozen_cash", "m_dFrozenCash", "frozen_balance"])),
            raw=data,
        )

    def query_positions(self) -> list[PositionSnapshot]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")
        raw_positions = self.trader.query_stock_positions(self.account) or []
        positions = []
        for raw in raw_positions:
            data = object_to_dict(raw)
            code = str(first_present(data, ["stock_code", "code", "ts_code", "m_strInstrumentID"], "")).upper()
            positions.append(
                PositionSnapshot(
                    account_id=self.config.account_id,
                    ts_code=qmt_to_tushare_code(code),
                    broker_code=code,
                    name=str(first_present(data, ["stock_name", "name", "m_strInstrumentName"], "")),
                    volume=to_int(first_present(data, ["volume", "m_nVolume", "total_volume"])),
                    can_use_volume=to_int(first_present(data, ["can_use_volume", "m_nCanUseVolume", "enable_amount"])),
                    cost_price=to_float(first_present(data, ["open_price", "cost_price", "m_dOpenPrice"])),
                    market_value=to_float(first_present(data, ["market_value", "m_dMarketValue"])),
                    raw=data,
                )
            )
        return positions

    def query_orders(self) -> list[dict[str, Any]]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")
        return [object_to_dict(order) for order in (self.trader.query_stock_orders(self.account) or [])]

    def query_trades(self) -> list[dict[str, Any]]:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")
        return [object_to_dict(trade) for trade in (self.trader.query_stock_trades(self.account) or [])]

    def get_full_tick(self, ts_codes: list[str]) -> dict[str, QuoteSnapshot]:
        self._load_xtquant()
        broker_codes = [tushare_to_qmt_code(code) for code in ts_codes]
        raw_ticks = self.xtdata_module.get_full_tick(broker_codes) or {}
        result: dict[str, QuoteSnapshot] = {}
        for broker_code in broker_codes:
            raw = object_to_dict(raw_ticks.get(broker_code, {}))
            bid_prices = list(first_present(raw, ["bidPrice", "bid_price", "bid_prices"], []) or [])
            ask_prices = list(first_present(raw, ["askPrice", "ask_price", "ask_prices"], []) or [])
            bid_volumes = [to_int(v) for v in list(first_present(raw, ["bidVol", "bid_volume", "bid_volumes"], []) or [])]
            ask_volumes = [to_int(v) for v in list(first_present(raw, ["askVol", "ask_volume", "ask_volumes"], []) or [])]
            ts_code = qmt_to_tushare_code(broker_code)
            result[ts_code] = QuoteSnapshot(
                ts_code=ts_code,
                broker_code=broker_code,
                last_price=to_float(first_present(raw, ["lastPrice", "last_price", "price"])),
                open_price=to_float(first_present(raw, ["open", "openPrice", "open_price"])),
                high_price=to_float(first_present(raw, ["high", "highPrice", "high_price"])),
                low_price=to_float(first_present(raw, ["low", "lowPrice", "low_price"])),
                pre_close=to_float(first_present(raw, ["lastClose", "preClose", "pre_close"])),
                upper_limit=to_float(first_present(raw, ["upperLimit", "upper_limit", "limitUp", "up_limit"])),
                lower_limit=to_float(first_present(raw, ["lowerLimit", "lower_limit", "limitDown", "down_limit"])),
                bid_prices=[to_float(v) for v in bid_prices],
                bid_volumes=bid_volumes,
                ask_prices=[to_float(v) for v in ask_prices],
                ask_volumes=ask_volumes,
                suspended=bool(first_present(raw, ["suspendFlag", "suspended", "is_suspended"], False)),
                raw=raw,
            )
        return result

    def place_order(self, request: OrderRequest) -> OrderResult:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")

        side = request.side.upper()
        if side == "BUY":
            order_type = self._constant("STOCK_BUY", 23)
        elif side == "SELL":
            order_type = self._constant("STOCK_SELL", 24)
        else:
            raise ValueError(f"不支持的委托方向: {request.side}")

        price_type_name = request.price_type.upper()
        price_type = self._constant(price_type_name, None)
        if price_type is None:
            price_type = self._constant("LATEST_PRICE", 5)

        raw_order_id = self.trader.order_stock(
            self.account,
            request.broker_code,
            order_type,
            int(request.quantity),
            price_type,
            float(request.price),
            request.strategy_name,
            request.remark,
        )
        accepted = raw_order_id not in {None, "", -1}
        return OrderResult(
            ts_code=request.ts_code,
            broker_code=request.broker_code,
            side=side,
            quantity=int(request.quantity),
            accepted=bool(accepted),
            order_id=str(raw_order_id or ""),
            message="ORDER_SUBMITTED" if accepted else "ORDER_REJECTED_BY_QMT",
            raw={"order_id": raw_order_id},
        )

    def cancel_order(self, order_id: str) -> bool:
        if self.trader is None or self.account is None:
            raise RuntimeError("QMT 尚未连接。")
        try:
            result = self.trader.cancel_order_stock(self.account, int(order_id))
            self.logger.info("撤单请求: order_id=%s result=%s", order_id, result)
            return result not in {None, -1}
        except Exception as e:
            self.logger.error("撤单失败: order_id=%s error=%s", order_id, e)
            return False

    def get_order_fill(self, order_id: str) -> OrderFill:
        """查询某笔委托的成交情况。

        成交股数/均价优先以成交回报（query_trades）为准，回报缺失时回退到委托对象上的
        已成字段；订单状态以委托对象（query_orders）为准，用于判断是否已到终态。
        """
        oid = str(order_id).strip()
        _id_names = ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"]

        # 1) 委托状态
        status_code = -1
        order_traded_qty = 0
        order_avg_price = 0.0
        raw_order: dict[str, Any] = {}
        try:
            for o in self.query_orders():
                if str(first_present(o, _id_names, "")).strip() == oid:
                    raw_order = o
                    status_code = to_int(
                        first_present(o, ["order_status", "m_nOrderStatus", "status"], -1), -1
                    )
                    order_traded_qty = to_int(
                        first_present(o, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"])
                    )
                    order_avg_price = to_float(
                        first_present(o, ["traded_price", "m_dTradedPrice", "avg_price", "deal_price"])
                    )
                    break
        except Exception as e:
            self.logger.warning("查询委托状态失败 order_id=%s: %s", oid, e)

        # 2) 成交回报（按 order_id 聚合，VWAP）
        trade_qty = 0
        trade_amount = 0.0
        traded_at = ""
        try:
            for t in self.query_trades():
                if str(first_present(t, _id_names, "")).strip() == oid:
                    q = to_int(first_present(t, ["traded_volume", "m_nVolume", "volume", "traded_qty"]))
                    p = to_float(first_present(t, ["traded_price", "m_dPrice", "price"]))
                    if q > 0:
                        trade_qty += q
                        trade_amount += q * p
                        tt = str(first_present(t, ["traded_time", "m_strTradeTime", "trade_time", "m_nTradeTime"], "") or "")
                        if tt:
                            traded_at = tt
        except Exception as e:
            self.logger.warning("查询成交回报失败 order_id=%s: %s", oid, e)

        if trade_qty > 0:
            filled_qty = trade_qty
            avg_price = trade_amount / trade_qty
        else:
            filled_qty = order_traded_qty
            avg_price = order_avg_price

        status_text = _ORDER_STATUS_TEXT.get(status_code, "UNKNOWN")
        is_terminal = status_code in _ORDER_TERMINAL_STATUS
        is_filled = status_code == 56
        is_partial = status_code == 55 or (filled_qty > 0 and not is_filled)

        return OrderFill(
            order_id=oid,
            status_code=status_code,
            status_text=status_text,
            filled_qty=filled_qty,
            avg_price=avg_price,
            is_terminal=is_terminal,
            is_filled=is_filled,
            is_partial=is_partial,
            traded_at=traded_at,
            raw=raw_order,
        )
