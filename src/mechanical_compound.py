"""严格机械逐笔复利的唯一公共实现。

这里只接受已经按真实单账户时序筛选出的实际成交收益。候选、跳过交易、
固定本金收益和各策略单腿复利都不得混入组合复利。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd


MECHANICAL_COMPOUND_STANDARD_ID = "A_SYSTEM_MECHANICAL_COMPOUND_V1"


class MechanicalCompoundError(ValueError):
    """机械复利输入不满足可复现的单账户逐笔口径。"""


@dataclass(frozen=True)
class MechanicalCompoundResult:
    standard_id: str
    trade_count: int
    initial_equity: float
    final_equity: float
    equity_multiple: float
    total_compound_return: float
    max_drawdown: float

    def to_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


def mechanical_compound(
    returns: pd.Series | Iterable[float],
    *,
    initial_equity: float = 1.0,
) -> MechanicalCompoundResult:
    """按输入顺序执行 ``equity *= 1 + account_return``。

    传入值必须是扣除仓位、滑点和费用后的账户收益率。任何空值、无穷值或
    小于等于-100%的单笔收益都会直接失败，禁止静默丢样本或截断亏损。
    """

    try:
        values = pd.to_numeric(pd.Series(returns, dtype="float64"), errors="raise")
    except (TypeError, ValueError) as exc:
        raise MechanicalCompoundError("机械复利包含非数值账户收益") from exc
    if values.isna().any():
        raise MechanicalCompoundError("机械复利包含缺失账户收益，禁止静默丢弃")
    array = values.to_numpy(dtype=float)
    if not np.isfinite(array).all():
        raise MechanicalCompoundError("机械复利包含NaN或无穷值")
    if (array <= -1.0).any():
        raise MechanicalCompoundError("机械复利存在小于等于-100%的非法单笔账户收益")
    if not np.isfinite(initial_equity) or float(initial_equity) <= 0:
        raise MechanicalCompoundError("机械复利初始权益必须是正的有限数")

    if len(array) == 0:
        multiple = 1.0
        max_drawdown = 0.0
    else:
        curve = np.cumprod(1.0 + array)
        if not np.isfinite(curve).all():
            raise MechanicalCompoundError("机械复利资金曲线溢出或无效")
        peaks = np.maximum.accumulate(np.concatenate(([1.0], curve)))
        curve_with_initial = np.concatenate(([1.0], curve))
        drawdown = curve_with_initial / peaks - 1.0
        multiple = float(curve[-1])
        max_drawdown = float(drawdown.min())

    final_equity = float(initial_equity) * multiple
    return MechanicalCompoundResult(
        standard_id=MECHANICAL_COMPOUND_STANDARD_ID,
        trade_count=int(len(array)),
        initial_equity=float(initial_equity),
        final_equity=final_equity,
        equity_multiple=multiple,
        total_compound_return=multiple - 1.0,
        max_drawdown=max_drawdown,
    )


def mechanical_compound_frame(
    frame: pd.DataFrame,
    *,
    return_column: str = "account_return",
    date_column: str = "signal_date",
    initial_equity: float = 1.0,
    require_unique_dates: bool = True,
) -> MechanicalCompoundResult:
    """验证成交明细时序后计算单账户机械逐笔复利。"""

    missing = [column for column in (date_column, return_column) if column not in frame.columns]
    if missing:
        raise MechanicalCompoundError(f"机械复利成交明细缺少字段：{missing}")
    dates = (
        frame[date_column]
        .astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"[^0-9]", "", regex=True)
    )
    parsed = pd.to_datetime(dates, format="%Y%m%d", errors="coerce")
    if parsed.isna().any():
        raise MechanicalCompoundError("机械复利成交明细包含非法信号日期")
    if not parsed.is_monotonic_increasing:
        raise MechanicalCompoundError("机械复利成交明细未按信号日期升序排列")
    if require_unique_dates and dates.duplicated().any():
        raise MechanicalCompoundError("单账户机械复利同一信号日出现多笔成交")
    return mechanical_compound(frame[return_column], initial_equity=initial_equity)
