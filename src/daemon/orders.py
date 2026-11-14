"""委托确认与撤单：轮询确认真实成交、撤单。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import time
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing
from src.daemon.common import PROJECT_ROOT, logger
from src.daemon.qmt import _qmt_get, _qmt_lock


def _confirm_fill(
    broker_cfg: dict,
    order_id: str,
    expected_qty: int,
    tag: str,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
) -> "OrderFill":
    """轮询确认委托成交情况。受理(accepted)不等于成交(filled)，此处确认真实成交股数与均价。

    - fill_confirm_enabled=False 时返回"乐观全成"（avg_price=0，调用方回退到参考价）。
    - 每次轮询单独加锁、轮询间隔释放锁，不会长时间阻塞账户轮询线程。
    """
    from src.broker_adapter import OrderFill

    log = logger()
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    lt = config.get("live_trade", {})

    if not lt.get("fill_confirm_enabled", True):
        return OrderFill(order_id=str(order_id), filled_qty=int(expected_qty),
                         is_terminal=True, is_filled=True, status_text="确认已禁用")

    timeout = float(timeout_sec if timeout_sec is not None else lt.get("fill_confirm_timeout_sec", 60))
    poll = max(1.0, float(poll_sec if poll_sec is not None else lt.get("fill_confirm_poll_sec", 3)))
    # 集合竞价时段（09:15~09:25）挂的单要到09:25集中撮合才有成交回报，
    # 固定60秒超时必然误报"未成交"（如逾期持仓09:20清理挂单）。
    # 确认窗口自动延长到09:25:20之后，避免虚假critical告警。
    now_t = now_beijing()
    if datetime.time(9, 15) <= now_t.time() < datetime.time(9, 25):
        auction_done = now_t.replace(hour=9, minute=25, second=20, microsecond=0)
        wait_to_auction = (auction_done - now_t).total_seconds()
        if wait_to_auction + 10 > timeout:
            timeout = wait_to_auction + 10
            log.info("[%s] 集合竞价时段挂单，成交确认窗口延长至09:25:30（%.0f秒）", tag, timeout)
    # 收盘集合竞价(14:57~15:00)申报的单要到15:00统一撮合才有成交回报,
    # 同款自动延长确认窗口(收盘竞价旁路=14:57:05挂单拿收盘价的场景)。
    elif datetime.time(14, 56, 30) <= now_t.time() < datetime.time(15, 0):
        close_done = now_t.replace(hour=15, minute=0, second=20, microsecond=0)
        wait_to_close = (close_done - now_t).total_seconds()
        if wait_to_close + 10 > timeout:
            timeout = wait_to_close + 10
            log.info("[%s] 收盘集合竞价时段挂单，成交确认窗口延长至15:00:30（%.0f秒）", tag, timeout)
    deadline = time.monotonic() + timeout
    last_fill = OrderFill(order_id=str(order_id))

    while True:
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                last_fill = adapter.get_order_fill(order_id)
        except Exception as e:
            log.warning("[%s] 成交确认查询异常 order_id=%s：%s", tag, order_id, e)

        if expected_qty > 0 and last_fill.filled_qty >= expected_qty:
            return last_fill
        if last_fill.is_terminal:
            return last_fill
        if time.monotonic() >= deadline:
            log.warning("⚠️ [%s] 成交确认超时（%.0f秒）order_id=%s 已成%d/%d股 状态=%s",
                        tag, timeout, order_id, last_fill.filled_qty, expected_qty,
                        last_fill.status_text)
            return last_fill
        time.sleep(poll)


def _format_order_fill_detail(fill: Any) -> str:
    """把券商委托终态尽量转成可读说明，尤其用于废单排查。"""
    raw = fill.raw if isinstance(getattr(fill, "raw", None), dict) else {}
    detail_parts = [
        f"状态码={getattr(fill, 'status_code', '')}",
        f"状态={getattr(fill, 'status_text', '')}",
        f"已成={getattr(fill, 'filled_qty', 0)}股",
    ]
    interesting_keywords = (
        "error", "err", "fail", "失败", "废", "拒",
        "msg", "message", "remark", "memo", "note", "status", "reason",
        "m_str", "order", "entrust",
    )
    extra: list[str] = []
    for key, value in raw.items():
        if value in {None, ""}:
            continue
        key_text = str(key)
        if any(word.lower() in key_text.lower() for word in interesting_keywords):
            extra.append(f"{key_text}={value}")
    if extra:
        detail_parts.append("原始字段：" + "；".join(extra[:12]))
    return "；".join(detail_parts)


def _try_cancel_order(
    broker_cfg: dict,
    order_id: str,
    ts_code: str,
) -> None:
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            ok = adapter.cancel_order(order_id)
        logger().info("撤单 %s（%s）请求%s", ts_code, order_id, "已提交" if ok else "失败")
    except Exception as e:
        logger().warning("撤单 %s（%s）异常：%s", ts_code, order_id, e)
