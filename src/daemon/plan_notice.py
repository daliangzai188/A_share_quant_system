"""开仓计划推送里L那一行的摘要，和“L持仓或L计划占用时D不启动”的说明（ACE+M+L新框架，2026-11-26；只读，不参与下单）。

从 reports.py 拆出（reports 到了生产代码单文件3000行的上限）；现行框架下两个函数都返回空，推送和决策链与原来相同。
"""
from __future__ import annotations

from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing
from src.open_plan_notification import ace_notice_has_plan, l_plan_notice
from src.portfolio_ace_m import FRAMEWORK_ACE_M_L, L_LEG, l_target_pct, position_in_family
from src.rolling_signal_store import SIGNAL_READY
from src.daemon.common import PROJECT_ROOT, logger
from src.daemon.state import load_positions
from src.daemon.plans import framework_from_config, has_open_local_position, loss_streak_multiplier_for
from src.daemon.buy_l import L_POV_END_TIME, L_SIGNAL_RECENT_PATH


def l_plan_line(action_date: str, *, plan: dict, gate_mode: str, occasion: str) -> tuple[str, str, dict]:
    """ACE+M+L新框架的L计划摘要（推送用）；返回(文字, 首选名称, 给M摘要用的新框架参数)。现行框架返回空。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if framework_from_config(config) != FRAMEWORK_ACE_M_L:
            return "", "", {}
        from src.rolling_signal_store import latest_signal_for_buy_date
        from src.strategy_l import L_VERSION

        signal = latest_signal_for_buy_date(L_SIGNAL_RECENT_PATH, str(action_date))
        streak = loss_streak_multiplier_for(str(action_date), config)
        positions = load_positions()
        ace_codes = {str(p.get("ts_code", "")) for p in positions
                     if str(p.get("status", "")).lower() in {"open", "sell_pending"} and position_in_family(p, "ACE")}
        if ace_notice_has_plan(plan, gate_mode):
            ace_codes.add(str(plan["final_buy"].get("ts_code", "")))
        now = now_beijing()
        text, first = l_plan_notice(
            signal, occasion=occasion, action_date=action_date, gate_mode=gate_mode,
            has_l_position=has_open_local_position(L_LEG), streak_mult=streak, ace_codes=ace_codes,
            l_version=L_VERSION, plan=plan, positions=positions, config=config,
            window_closed=(str(action_date) < now.strftime("%Y%m%d")
                           or (str(action_date) == now.strftime("%Y%m%d") and now.time() >= L_POV_END_TIME)))
        reserve = (l_target_pct(ace_mode=gate_mode, l_self_allowed=bool((signal or {}).get("l_allowed", False)),
                                streak_mult=streak) if first else 0.0)
        return text, first, {"framework": FRAMEWORK_ACE_M_L, "l_reserve_pct": reserve, "streak_mult": streak,
                             "l_codes": [str((signal or {}).get("ts_code", ""))] if first else []}
    except Exception as exc:
        logger().warning("L计划摘要生成失败（不影响交易）：%s", exc)
        return "L：计划摘要没生成（不影响交易，以执行日志为准）。", "", {}


def l_blocks_d_reason(action_date: str, positions: list[dict[str, Any]], day_label: str) -> str:
    """新框架：L持仓或L当天有计划时D不启动（与组合状态机同口径）；现行框架返回空。"""
    if framework_from_config() != FRAMEWORK_ACE_M_L:
        return ""
    from src.rolling_signal_store import latest_signal_for_buy_date

    signal = latest_signal_for_buy_date(L_SIGNAL_RECENT_PATH, str(action_date)) or {}
    if (str(signal.get("status", "")) == SIGNAL_READY and signal.get("ts_code")) or any(
            position_in_family(position, L_LEG) for position in positions):
        return f"L持仓或L开仓计划占用资金，D{day_label}不启动（D只在账户含M、L完全空仓时运行）"
    return ""
