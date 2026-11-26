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

ACE + M + L 新框架（2026-11-26 用户确认落地，配置 portfolio_framework="ace_m_l" 时生效；默认 "ace_m" 即上面的现行规则）
- ACE 不变（82.5%，停手期 20%），不超过 99.5% − M − L 当前持仓；
- L：ACE 停手期 60%，ACE 正常期 25%；L 自身停手时 ×20/82.5；不超过 99.5% − ACE − M；
- M：只在 ACE 当日无计划且无持仓时开仓，min(82.5%, 99.5% − L 持仓)；ACE 持仓或当日开仓时不开；
- 同日顺序 ACE → L → M；
- 连亏降仓：最近连续 2 笔已平仓交易都亏损时，当天所有新开仓仓位 ×0.3，直到出现 1 笔盈利。
研究依据与规则全文见 docs/strategy_l.md。
"""

from typing import Any, Callable, Mapping

# ── 策略腿（2026-11-15 起唯一出处，其它模块引用这里的名字，不再自己写字母组合）──────────────
# 固定腿序 A>C>E>D（AGENTS.md 第二十五节）：实盘组合状态机按它依次尝试开仓，影子账、停手判定和认证回放按它裁决。
ACE_LEG_PRIORITY = ("A", "C", "E", "D")
ACE_LEGS = ACE_LEG_PRIORITY                                               # ACE 家族（含D）
ACE_STATIC_LEGS = tuple(leg for leg in ACE_LEG_PRIORITY if leg != "D")    # 收盘后冻结计划的腿；D 走盘中路径
AC_LEGS = tuple(leg for leg in ACE_STATIC_LEGS if leg != "E")             # 收盘流水线⑥出计划、共用“ABC开仓”动作
M_LEG = "M"
L_LEG = "L"
ALL_LEGS = ACE_LEG_PRIORITY + (M_LEG, L_LEG)                              # 系统会开仓的全部策略腿（L 只在新框架下开仓）
REPORT_LEGS = ACE_STATIC_LEGS + (M_LEG,)                                  # 日报、对账、警告按腿列出的顺序（现行框架）
REPORT_LEGS_WITH_L = ACE_STATIC_LEGS + (M_LEG, L_LEG)                     # 新框架下按腿列出的顺序
REMARK_LEG_MATCH_ORDER = ("E", "D", "A", "C")   # 从委托策略名/备注认腿时的匹配顺序（沿用执行服务原顺序，与开仓腿序无关）

ACE_FULL_TARGET_PCT = 0.825
ACE_REDUCED_TARGET_PCT = 0.20
ACE_HARD_MARGIN_PCT = 0.025
ACE_FULL_HARD_CAP_PCT = 0.85          # 方案甲允许时的单票硬顶 = 82.5%目标 + 2.5个百分点
ACE_MIN_ACCEPT_RATIO = 0.80 / 0.825     # 原 80% 验收线 / 82.5% 目标，按目标等比例缩放
TOTAL_POSITION_CAP_PCT = 0.995

M_PCT_WHEN_ACE_IDLE = 0.825
M_PCT_BESIDE_ACE = 0.175

# ── ACE + M + L 新框架（2026-11-26）───────────────────────────────────────────────
FRAMEWORK_ACE_M = "ace_m"
FRAMEWORK_ACE_M_L = "ace_m_l"
FRAMEWORKS = (FRAMEWORK_ACE_M, FRAMEWORK_ACE_M_L)
L_PCT_WHEN_ACE_HALTED = 0.60          # 方案甲停手期（ACE 只用 20%）L 用 60%
L_PCT_WHEN_ACE_NORMAL = 0.25          # ACE 正常期 L 最多 25%
L_SELF_STOP_SCALE = ACE_REDUCED_TARGET_PCT / ACE_FULL_TARGET_PCT   # L 自身停手时仓位 ×20/82.5（同 ACE 停手比例）
M_PCT_BESIDE_ACE_L_FRAMEWORK = 0.0    # 新框架：ACE 持仓或当日开仓时 M 不开
LOSS_STREAK_TRIGGER = 2               # 连亏降仓：最近连续 2 笔已平仓交易都亏损
LOSS_STREAK_MULTIPLIER = 0.3          # 当天所有新开仓仓位 ×0.3，直到出现 1 笔盈利

GATE_FULL = "FULL"
GATE_REDUCED = "REDUCED"
GATE_BLOCKED = "BLOCKED"


def first_leg_to_open(openers: Mapping[str, Callable[[], bool]]) -> str | None:
    """按固定腿序 A>C>E>D 依次调用各腿的开仓函数（返回是否开仓），返回第一条开成的腿；之后的腿不再调用。

    没有给出开仓函数的腿跳过（D 走盘中路径，不在收盘计划里）。
    """
    for leg in ACE_LEG_PRIORITY:
        opener = openers.get(leg)
        if opener is not None and opener():
            return leg
    return None


def normalize_leg(value: Any) -> str:
    return str(value or "").strip().upper()


def is_m_leg(value: Any) -> bool:
    return normalize_leg(value) == M_LEG


def is_m_position(position: Mapping[str, Any]) -> bool:
    return is_m_leg(position.get("strategy_leg", ""))


def is_l_leg(value: Any) -> bool:
    return normalize_leg(value) == L_LEG


def is_l_position(position: Mapping[str, Any]) -> bool:
    return is_l_leg(position.get("strategy_leg", ""))


def position_in_family(position: Mapping[str, Any], family: str | None) -> bool:
    """family=None 全部；"ACE" 只看 A/C/E/D 及历史腿；"M" 只看 M；"L" 只看 L。"""

    if family is None:
        return True
    fam = str(family).upper()
    if fam == M_LEG:
        return is_m_position(position)
    if fam == L_LEG:
        return is_l_position(position)
    if fam == "ACE":
        return not is_m_position(position) and not is_l_position(position)
    raise ValueError("unknown position family: %s" % family)


def portfolio_framework(config: Mapping[str, Any] | None) -> str:
    """读取组合框架开关；缺省、空或未知值一律按现行 ACE+M（fail-safe：不会意外打开新框架）。"""

    value = str(((config or {}).get("portfolio_framework") or FRAMEWORK_ACE_M)).strip().lower()
    return value if value in FRAMEWORKS else FRAMEWORK_ACE_M


def report_legs(framework: str) -> tuple[str, ...]:
    return REPORT_LEGS_WITH_L if framework == FRAMEWORK_ACE_M_L else REPORT_LEGS


def loss_streak_multiplier(closed_returns_oldest_first: Any) -> float:
    """连亏降仓：最近连续 LOSS_STREAK_TRIGGER 笔已平仓交易都亏损（收益≤0）时返回 0.3，否则 1.0。"""

    values = [float(x) for x in list(closed_returns_oldest_first or [])]
    if len(values) < LOSS_STREAK_TRIGGER:
        return 1.0
    return LOSS_STREAK_MULTIPLIER if all(v <= 0 for v in values[-LOSS_STREAK_TRIGGER:]) else 1.0


def l_target_pct(*, ace_mode: str, l_self_allowed: bool, streak_mult: float = 1.0) -> float:
    """L 的目标仓位（相对总资产，未扣其它腿占用）：ACE 停手期 60%，ACE 正常期 25%；L 自身停手 ×20/82.5；再乘连亏降仓倍数。

    ACE 判定 fail-closed（BLOCKED）时按正常期 25% 处理（和研究一致：只有方案甲明确停手才算停手期）。
    """

    scale = 1.0 if l_self_allowed else L_SELF_STOP_SCALE
    if ace_mode == GATE_REDUCED:
        base = L_PCT_WHEN_ACE_HALTED * scale
    else:
        base = min(L_PCT_WHEN_ACE_NORMAL, L_PCT_WHEN_ACE_HALTED * scale)
    return base * float(streak_mult)


def l_budget(
    *,
    total_asset: float,
    available_cash: float,
    target_pct: float,
    ace_value: float,
    m_value: float,
    ace_unfilled_target: float,
    cash_buffer: float,
    ace_frozen_pending: float = 0.0,
) -> dict[str, float]:
    """L 本次可用金额（元）：min(目标比例, 99.5% − ACE − M − ACE 未成交目标, 现金 − 缓冲 − ACE 未冻结的预留)。"""

    ta = max(float(total_asset or 0.0), 0.0)
    by_pct = ta * max(float(target_pct or 0.0), 0.0)
    reserve = max(float(ace_unfilled_target or 0.0), 0.0)
    by_total_cap = ta * TOTAL_POSITION_CAP_PCT - max(float(ace_value or 0.0), 0.0) - max(float(m_value or 0.0), 0.0) - reserve
    cash_reserve = max(reserve - max(float(ace_frozen_pending or 0.0), 0.0), 0.0)
    by_cash = float(available_cash or 0.0) - max(float(cash_buffer or 0.0), 0.0) - cash_reserve
    amount = max(0.0, min(by_pct, by_total_cap, by_cash))
    return {"pct": target_pct, "by_pct": by_pct, "by_total_cap": by_total_cap, "by_cash": by_cash, "amount": amount}


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


def ace_limits(mode: str, total_asset: float, m_value: float, *, l_value: float = 0.0,
               streak_mult: float = 1.0) -> dict[str, float]:
    """返回 ACE 本次开仓的目标/硬顶/验收比例（相对总资产）与对应金额。

    m_value：M 当前持仓（本系统 M 仓）的市值；ACE 不得挤占 M 已用资金。
    l_value / streak_mult：新框架下 L 当前持仓市值与连亏降仓倍数；现行框架恒为 0 / 1.0（结果与原来逐位相同）。
    """

    base = ace_base_target_pct(mode) * float(streak_mult)
    ta = max(float(total_asset or 0.0), 0.0)
    m_pct = (max(float(m_value or 0.0), 0.0) + max(float(l_value or 0.0), 0.0)) / ta if ta > 0 else 0.0
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


def m_position_pct(ace_active_today: bool, framework: str = FRAMEWORK_ACE_M) -> float:
    beside = M_PCT_BESIDE_ACE_L_FRAMEWORK if framework == FRAMEWORK_ACE_M_L else M_PCT_BESIDE_ACE
    return beside if ace_active_today else M_PCT_WHEN_ACE_IDLE


def m_budget(
    *,
    total_asset: float,
    available_cash: float,
    ace_active_today: bool,
    ace_value: float,
    ace_unfilled_target: float,
    cash_buffer: float,
    ace_frozen_pending: float = 0.0,
    framework: str = FRAMEWORK_ACE_M,
    l_value: float = 0.0,
    streak_mult: float = 1.0,
) -> dict[str, float]:
    """M 本次可用金额（元）。

    total_asset：09:20 开仓前总资产快照（与 ACE 同一基数）；
    ace_value：ACE 当前持仓市值；ace_unfilled_target：ACE 今日目标尚未成交的金额（给 ACE 预留）；
    ace_frozen_pending：ACE 今日已挂、还没成交的买单冻结的钱（2026-11-18）。这部分已经从可用资金里扣掉了，
    按现金算 M 能用多少时不再给 ACE 重复预留；总仓位上限照旧按 ACE 整个目标预留。
    """

    ta = max(float(total_asset or 0.0), 0.0)
    pct = m_position_pct(ace_active_today, framework) * float(streak_mult)
    by_pct = ta * pct
    reserve = max(float(ace_unfilled_target or 0.0), 0.0)
    by_total_cap = ta * TOTAL_POSITION_CAP_PCT - max(float(ace_value or 0.0), 0.0) - max(float(l_value or 0.0), 0.0) - reserve
    cash_reserve = max(reserve - max(float(ace_frozen_pending or 0.0), 0.0), 0.0)
    by_cash = float(available_cash or 0.0) - max(float(cash_buffer or 0.0), 0.0) - cash_reserve
    amount = max(0.0, min(by_pct, by_total_cap, by_cash))
    return {"pct": pct, "by_pct": by_pct, "by_total_cap": by_total_cap, "by_cash": by_cash, "amount": amount}
