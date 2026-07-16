from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class BrokerConnectionConfig:
    """券商连接配置，不包含账号密码等敏感明文。"""

    broker_name: str
    account_id: str
    account_type: str
    qmt_path: str
    session_id: int


@dataclass(frozen=True)
class AccountSnapshot:
    """账户资金快照。"""

    account_id: str
    cash: float = 0.0
    available_cash: float = 0.0
    total_asset: float = 0.0
    market_value: float = 0.0
    frozen_cash: float = 0.0
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class PositionSnapshot:
    """持仓快照。"""

    account_id: str
    ts_code: str
    broker_code: str
    name: str = ""
    volume: int = 0
    can_use_volume: int = 0
    cost_price: float = 0.0
    market_value: float = 0.0
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class QuoteSnapshot:
    """实时行情快照，优先承载五档盘口、涨跌停价和当日累计成交额。

    ``amount`` 是截至快照时点的当日累计成交额，统一按人民币元保存；它不是
    当前一分钟或当前一笔的成交额。需要计算区间成交流量时，应对两个相邻快照
    的 ``amount`` 做差，并处理午间休市、行情重连或数据源重置造成的非递增值。
    ``raw`` 保留券商返回的完整原始快照，便于核验不同 QMT 版本的字段与单位。
    """

    ts_code: str
    broker_code: str
    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    pre_close: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    amount: float = 0.0
    bid_prices: list[float] | None = None
    bid_volumes: list[int] | None = None
    ask_prices: list[float] | None = None
    ask_volumes: list[int] | None = None
    suspended: bool = False
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class OrderRequest:
    """标准化委托请求。"""

    ts_code: str
    broker_code: str
    side: str
    quantity: int
    price_type: str
    price: float = 0.0
    strategy_name: str = "A_SYSTEM"
    remark: str = ""


@dataclass(frozen=True)
class OrderResult:
    """委托提交结果。"""

    ts_code: str
    broker_code: str
    side: str
    quantity: int
    accepted: bool
    order_id: str = ""
    message: str = ""
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class OrderFill:
    """委托成交确认结果。accepted（已受理）不等于 filled（已成交），此结构承载真实成交情况。"""

    order_id: str
    status_code: int = -1          # 券商原始状态码，-1 表示未知/未查到
    status_text: str = "UNKNOWN"   # 人类可读状态
    filled_qty: int = 0            # 已成交股数
    avg_price: float = 0.0         # 成交均价
    is_terminal: bool = False      # 订单已到终态（全成/废单/已撤/部撤），不会再有新成交
    is_filled: bool = False        # 全部成交
    is_partial: bool = False       # 部分成交（仍可能继续成交，除非同时 is_terminal）
    traded_at: str = ""            # 最后一笔成交回报的时间（券商原始格式），审计成交时点用
    raw: dict[str, Any] | None = None


def dataclass_to_dict(value: Any) -> dict[str, Any]:
    """将项目 dataclass 转成可落地 CSV/JSON 的字典。"""

    return asdict(value)


class BrokerAdapter(ABC):
    """券商适配器抽象层，策略层只能依赖这一层。"""

    @abstractmethod
    def connect(self) -> None:
        """连接券商客户端。"""

    @abstractmethod
    def disconnect(self) -> None:
        """断开连接。"""

    @abstractmethod
    def query_account(self) -> AccountSnapshot:
        """查询账户资金。"""

    @abstractmethod
    def query_positions(self) -> list[PositionSnapshot]:
        """查询持仓。"""

    @abstractmethod
    def query_orders(self) -> list[dict[str, Any]]:
        """查询当日委托。"""

    @abstractmethod
    def query_trades(self) -> list[dict[str, Any]]:
        """查询当日成交。"""

    @abstractmethod
    def get_full_tick(self, ts_codes: list[str]) -> dict[str, QuoteSnapshot]:
        """查询实时行情快照。"""

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        """提交真实委托。调用前必须经过实盘风控闸门。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销委托。返回 True 表示撤单请求已提交（不代表最终成功）。"""

    def get_order_fill(self, order_id: str) -> "OrderFill":
        """查询某笔委托的成交情况。默认返回 UNKNOWN，具体券商适配器应覆盖。"""
        return OrderFill(order_id=str(order_id))
