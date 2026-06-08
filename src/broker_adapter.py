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
    """实时行情快照，优先承载五档盘口和涨跌停价。"""

    ts_code: str
    broker_code: str
    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    pre_close: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
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

