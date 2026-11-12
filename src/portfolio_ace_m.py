from __future__ import annotations

"""ACE + M 组合的资金规则唯一来源（2026-11-12 用户确认上线版本）。

与研究回测（~/A_System_复核/2026-11-11_M大法研究/scripts/m14_verify.py）逐条一致：

ACE（A > C > E > D，选股与退出不变）
- 方案甲允许开仓：目标仓位 82.5%；
- 方案甲停手（判定文件有效且明确停手）：目标仓位 20%，照常执行 A > C > E 计划；
- 判定缺失、过期、版本不符等 fail-closed 情形：不开新仓（与原来相同）；
- 任何时候 ACE 目标不超过 99.5% − M 当前持仓占比；单票硬顶 = 目标 + 2.5 个百分点（同受 99.5% 约束）。

M（20cm 强势涨停接力）
- ACE 当日无交易（无计划或被阻断）且无持仓：M 用总资产 82.5%；
- ACE 持仓或当日开仓：M 只用闲置资金，最多 17.5%；
- ACE + M 合计不超过 99.5%；M 不设停手；同一时间最多 1 只；不买 ACE 持有/计划买入的股票。
"""

from typing import Any, Mapping

ACE_LEGS = ("A", "C", "E", "D")
M_LEG = "M"

ACE_FULL_TARGET_PCT = 0.825
ACE_REDUCED_TARGET_PCT = 0.20
ACE_HARD_MARGIN_PCT = 0.025
ACE_MIN_ACCEPT_RATIO = 0.80 / 0.825     # 原 80% 验收线 / 82.5% 目标，按目标等比例缩放
TOTAL_POSITION_CAP_PCT = 0.995

M_PCT_WHEN_ACE_IDLE = 0.825
M_PCT_BESIDE_ACE = 0.175

GATE_FULL = "FULL"
GATE_REDUCED = "REDUCED"
GATE_BLOCKED = "BLOCKED"


def normalize_leg(value: Any) -> str:
    return str(value or "").strip().upper()


def is_m_leg(value: Any) -> bool:
    return normalize_leg(value) == M_LEG


def is_m_position(position: Mapping[str, Any]) -> bool:
    return is_m_leg(position.get("strategy_leg", ""))


def position_in_family(position: Mapping[str, Any], family: str | None) -> bool:
    """family=None 全部；"ACE" 只看 A/C/E/D 及历史腿；"M" 只看 M。"""

    if family is None:
        return True
    fam = str(family).upper()
    if fam == M_LEG:
        return is_m_position(position)
    if fam == "ACE":
        return not is_m_position(position)
    raise ValueError("unknown position family: %s" % family)


def ace_gate_mode(gate_check: Any) -> str:
    """把方案甲 live_gate 结果映射为 ACE 执行模式。"""

    if bool(getattr(gate_check, "allowed", False)):
        return GATE_FULL
    if bool(getattr(gate_check, "halt_valid", False)):
        return GATE_REDUCED
    return GATE_BLOCKED


def ace_base_target_pct(mode: str) -> float:
    if mode == GATE_FULL:
        return ACE_FULL_TARGET_PCT
    if mode == GATE_REDUCED:
        return ACE_REDUCED_TARGET_PCT
    return 0.0


def ace_limits(mode: str, total_asset: float, m_value: float) -> dict[str, float]:
    """返回 ACE 本次开仓的目标/硬顶/验收比例（相对总资产）与对应金额。

    m_value：M 当前持仓（本系统 M 仓）的市值；ACE 不得挤占 M 已用资金。
    """

    base = ace_base_target_pct(mode)
    ta = max(float(total_asset or 0.0), 0.0)
    m_pct = (max(float(m_value or 0.0), 0.0) / ta) if ta > 0 else 0.0
    room = max(0.0, TOTAL_POSITION_CAP_PCT - m_pct)
    target_pct = min(base, room)
    hard_pct = min(base + ACE_HARD_MARGIN_PCT, room) if base > 0 else 0.0
    min_pct = target_pct * ACE_MIN_ACCEPT_RATIO
    return {
        "mode_target_pct": base,
        "m_pct": m_pct,
        "target_pct": target_pct,
        "hard_pct": hard_pct,
        "min_pct": min_pct,
        "target_amount": ta * target_pct,
        "hard_amount": ta * hard_pct,
        "min_amount": ta * min_pct,
    }


def m_position_pct(ace_active_today: bool) -> float:
    return M_PCT_BESIDE_ACE if ace_active_today else M_PCT_WHEN_ACE_IDLE


def m_budget(
    *,
    total_asset: float,
    available_cash: float,
    ace_active_today: bool,
    ace_value: float,
    ace_unfilled_target: float,
    cash_buffer: float,
) -> dict[str, float]:
    """M 本次可用金额（元）。

    total_asset：09:20 开仓前总资产快照（与 ACE 同一基数）；
    ace_value：ACE 当前持仓市值；ace_unfilled_target：ACE 今日目标尚未成交的金额（给 ACE 预留）。
    """

    ta = max(float(total_asset or 0.0), 0.0)
    pct = m_position_pct(ace_active_today)
    by_pct = ta * pct
    by_total_cap = ta * TOTAL_POSITION_CAP_PCT - max(float(ace_value or 0.0), 0.0) - max(float(ace_unfilled_target or 0.0), 0.0)
    by_cash = float(available_cash or 0.0) - max(float(cash_buffer or 0.0), 0.0) - max(float(ace_unfilled_target or 0.0), 0.0)
    amount = max(0.0, min(by_pct, by_total_cap, by_cash))
    return {"pct": pct, "by_pct": by_pct, "by_total_cap": by_total_cap, "by_cash": by_cash, "amount": amount}
