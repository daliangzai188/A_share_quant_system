"""开仓计划通知的只读展示：合并ACE/M（ACE+M+L新框架再加L）标题，并用既有账户快照预估M预算。"""
from __future__ import annotations

import math
from typing import Any

from src.market_rules import is_st_name, normalize_ts_code
from src.portfolio_ace_m import (
    FRAMEWORK_ACE_M,
    FRAMEWORK_ACE_M_L,
    GATE_FULL,
    GATE_REDUCED,
    ace_limits,
    fund_family,
    l_budget,
    l_target_pct,
    m_budget,
    m_position_pct,
)
from src.strategy_m import m_sort_for_config, m_version_for


def ace_notice_has_plan(plan: dict[str, Any], gate_mode: str) -> bool:
    """M按正式ACE计划预留；竞价零股/广播窗口结束不会提前释放原计划资金。"""
    if not plan.get("final_buy") or gate_mode not in (GATE_FULL, GATE_REDUCED):
        return False
    return not any(plan.get(key) for key in (
        "execution_block_reason", "equity_curve_stop_reason", "ace_blocked_by_holdings",
    ))


def ace_notice_executable(plan: dict[str, Any], gate_mode: str) -> bool:
    """仅统计未被已知门禁拦截的ACE计划，不把保留供解释的旧候选计为开仓。"""
    if not ace_notice_has_plan(plan, gate_mode) or plan.get("execution_expired"):
        return False
    live = plan.get("live_sizing")
    if live is not None:
        if int(live.get("shares", 0) or 0) <= 0:
            return False
        if live.get("ace_gate_mode") and live["ace_gate_mode"] != gate_mode:
            return False
    return True


def combined_plan_title(title: str, label: str, plan: dict[str, Any], gate_mode: str, m_first: str,
                        l_first: str = "") -> str:
    """ACE最多一只、M最多一只（新框架L也最多一只）；保留原有阻断/过期告警，候选数量不参与计数。"""
    if l_first:
        parts = []
        if ace_notice_executable(plan, gate_mode):
            parts.append(f"{plan['final_buy'].get('strategy', '')} {plan['final_buy'].get('name', '')}")
        parts.append(f"L {l_first}")
        if m_first:
            parts.append(f"M {m_first}")
        if plan.get("final_buy") and not ace_notice_executable(plan, gate_mode):
            return title + f"｜另有开仓计划（预计{len(parts)}只）:" + "＋".join(parts)
        return f"📋 {label}开仓计划（预计{len(parts)}只）:" + "＋".join(parts)
    if ace_notice_executable(plan, gate_mode):
        buy = plan["final_buy"]
        if m_first:
            return f"📋 {label}开仓计划（预计2只）:{buy.get('strategy', '')} {buy.get('name', '')}＋M {m_first}"
        return title + "（预计1只）"
    if m_first:
        if plan.get("final_buy"):
            return title + f"｜另有M开仓计划（预计1只）:{m_first}"
        return f"📋 {label}开仓计划:M {m_first}（预计1只）"
    return title


def _m_budget_preview(plan, gate_mode, ace_active, positions, config, *, framework=FRAMEWORK_ACE_M,
                      l_reserve_pct=0.0, streak_mult=1.0):
    """只消费已有定仓快照；没有可靠快照就返回未知，绝不查券商或回退初始资金。
    新框架（ACE+M+L）：给L今天的规则目标（总资产×l_reserve_pct）留出来，再乘连亏降仓倍数，与执行时同口径。"""
    live = plan.get("live_sizing") or {}
    if live.get("ace_gate_mode") and live["ace_gate_mode"] != gate_mode:
        return None
    equity = float(live.get("total_asset", 0.0) or 0.0)
    cash = float(live.get("available_cash", 0.0) or 0.0)
    if not math.isfinite(equity) or equity <= 0 or not math.isfinite(cash) or cash < 0:
        return None
    held_ace = [p for p in positions if p.get("status") in {"open", "sell_pending"}
                and fund_family(p.get("strategy_leg", "")) == "ACE"]
    if held_ace and "ace_market_value" not in live:
        return None
    ace_value = float(live.get("ace_market_value", 0.0) or 0.0)
    m_value = float(live.get("m_market_value", 0.0) or 0.0)
    if not math.isfinite(m_value) or m_value < 0:
        return None
    # 与M执行的_m_ace_context一致：预留ACE规则目标，不拿受容量缩小的预演金额替代。
    target = ace_limits(gate_mode, equity, m_value)["target_amount"] if ace_notice_has_plan(plan, gate_mode) else 0.0
    bought = sum(float(p.get("shares", 0) or 0) * float(p.get("buy_price", 0) or 0)
                 for p in held_ace if str(p.get("buy_date", "")) == str(plan.get("action_date", "")))
    buffer = float(config.get("live_trade", {}).get("cash_buffer_amount", 1000))
    if any(not math.isfinite(x) or x < 0 for x in (ace_value, target, bought, buffer)):
        return None
    extra = {}
    if framework == FRAMEWORK_ACE_M_L:
        extra = {"framework": framework, "l_unfilled_target": equity * float(l_reserve_pct),
                 "streak_mult": float(streak_mult)}
    budget = m_budget(total_asset=equity, available_cash=cash, ace_active_today=ace_active,
                      ace_value=ace_value, ace_unfilled_target=max(target - bought, 0.0), cash_buffer=buffer, **extra)
    return {**budget, "total_asset": equity}


def m_plan_notice(signal, *, action_date, ace_active, plan, positions, config, gate_mode, has_m_position,
                  window_closed=False, framework=FRAMEWORK_ACE_M, l_reserve_pct=0.0, streak_mult=1.0, l_codes=()):
    """返回(M正文, 首个可展示的M计划名称)；不写状态、不修改信号、不调用交易接口。
    新框架（ACE+M+L）：ACE当天有计划或持仓时M不开；M不买L的股票；预算给L留出今天的规则目标。"""
    if not signal:
        return "", ""
    if has_m_position:
        return "M：仍有M持仓（当天收盘卖出），M同一时间最多1只，不开新M。", ""
    if window_closed:
        return "M：原计划开仓窗口已结束，不新增M持仓。", ""
    if signal.get("planned_buy_date") and str(signal["planned_buy_date"]) != str(action_date):
        return "M：信号执行日期不符，不开新M。", ""
    if not bool((config.get("strategy_m") or {}).get("enabled", True)):
        return "M：策略已停用，不开新M。", ""
    if plan.get("execution_block_reason"):
        return f"M不开新仓：新增BUY安全门禁阻断（{plan['execution_block_reason']}）。", ""
    if str(signal.get("m_version", "")) != m_version_for(m_sort_for_config(config)):   # 跟着配置的M排序方式（2026-11-27）
        return "M：信号版本不符，不开新M。", ""
    cands = sorted(signal.get("candidates") or [], key=lambda c: int(c.get("rank", 999)))
    if not cands:
        diag = signal.get("diagnostics") or {}
        mood = diag.get("mood")
        mood_text = f"{float(mood) * 100:.2f}%" if mood is not None and math.isfinite(float(mood)) else "无"
        return f"M：无候选（昨日涨停今日平均{mood_text}，20cm涨停{int(diag.get('n20_limit_up') or 0)}只）。", ""
    if str(signal.get("status", "")) != "SIGNAL_READY":
        return "M：尚无有效开仓信号，不开新M。", ""
    ace_codes = {normalize_ts_code(p.get("ts_code", "")) for p in positions
                 if p.get("status") in {"open", "sell_pending"}
                 and fund_family(p.get("strategy_leg", "")) == "ACE"}
    if ace_notice_has_plan(plan, gate_mode):
        ace_codes.add(normalize_ts_code(plan["final_buy"].get("ts_code", "")))
    new = framework == FRAMEWORK_ACE_M_L
    if new and (ace_active or ace_codes):
        return "M：ACE+M+L下ACE今天有开仓计划或持仓，M不开仓。", ""
    avoid = ace_codes | {normalize_ts_code(c) for c in l_codes}
    cands = [c for c in cands if str(c.get("ts_code", ""))
             and normalize_ts_code(c["ts_code"]) not in avoid and not is_st_name(c.get("name", ""))]
    if not cands:
        return ("M：候选均与ACE/L同票或命中ST/退市过滤，不开新M。" if new
                else "M：候选均与ACE同票或命中ST/退市过滤，不开新M。"), ""
    ace_active = ace_active or bool(ace_codes)
    budget = _m_budget_preview(plan, gate_mode, ace_active, positions, config, framework=framework,
                               l_reserve_pct=l_reserve_pct, streak_mult=streak_mult)
    if budget is not None and budget["amount"] <= 0:
        return "M：按现有账户快照扣除ACE预留及现金缓冲后，预估可用预算0元，不开新M。", ""
    amount = "预估金额待执行前定仓（尚无可用的账户定仓快照）"
    if budget is not None:
        pct = f"{budget['amount'] / budget['total_asset'] * 100:.2f}".rstrip("0").rstrip(".")
        amount = f"预估开仓约{budget['amount'] / 10000:.2f}万（占总资产{pct}%，执行前重算）"
    rule_pct = f"{m_position_pct(ace_active, framework) * 100:.2f}".rstrip("0").rstrip(".")
    names = "、".join(f"{c.get('name') or c['ts_code']}({c['ts_code']})" for c in cands[:3])
    funding = "，仅用闲置资金" if ace_active else ""
    return (f"M计划：{names}｜{amount}；规则仓位上限为总资产{rule_pct}%{funding}"
            "（预算受可用现金、ACE预留及账户总仓位上限约束）。按顺序最多买1只，"
            "开盘涨停则换下一只，跳过ACE同票；09:20按涨停价-0.01预挂、余量POV补足"
            "（≤开盘+2%），买入后下一个交易日收盘卖出。", str(cands[0].get("name") or cands[0]["ts_code"]))


def _l_budget_preview(plan, gate_mode, positions, config, *, target_pct, streak_mult):
    """L 预估金额（2026-11-27，用户要求像 A 一样显示金额）：只用已有的账户定仓快照，先给 ACE 规则目标留够，
    没有可靠快照就返回 None（显示“待执行前定仓”），绝不查券商。"""
    live = (plan or {}).get("live_sizing") or {}
    if live.get("ace_gate_mode") and live["ace_gate_mode"] != gate_mode:
        return None
    equity = float(live.get("total_asset", 0.0) or 0.0)
    cash = float(live.get("available_cash", 0.0) or 0.0)
    if not math.isfinite(equity) or equity <= 0 or not math.isfinite(cash) or cash < 0:
        return None
    held_ace = [p for p in positions if p.get("status") in {"open", "sell_pending"}
                and fund_family(p.get("strategy_leg", "")) == "ACE"]
    if held_ace and "ace_market_value" not in live:
        return None
    ace_value = float(live.get("ace_market_value", 0.0) or 0.0)
    other_value = float(live.get("m_market_value", 0.0) or 0.0)
    target = (ace_limits(gate_mode, equity, other_value, streak_mult=streak_mult)["target_amount"]
              if ace_notice_has_plan(plan, gate_mode) else 0.0)
    bought = sum(float(p.get("shares", 0) or 0) * float(p.get("buy_price", 0) or 0)
                 for p in held_ace if str(p.get("buy_date", "")) == str((plan or {}).get("action_date", "")))
    buffer = float(config.get("live_trade", {}).get("cash_buffer_amount", 1000))
    if any(not math.isfinite(x) or x < 0 for x in (ace_value, other_value, target, bought, buffer)):
        return None
    budget = l_budget(total_asset=equity, available_cash=cash, target_pct=target_pct, ace_value=ace_value,
                      m_value=other_value, ace_unfilled_target=max(target - bought, 0.0), cash_buffer=buffer)
    return {**budget, "total_asset": equity}


def l_plan_notice(signal, *, occasion, action_date, gate_mode, has_l_position, streak_mult, ace_codes,
                  l_version, window_closed=False, plan=None, positions=(), config=None):
    """ACE+M+L新框架的L计划摘要（推送用），返回(正文, 首选名称)；只读，不调用交易接口。

    收盘推送时L计划通常还没算出（龙虎榜约17~18点才出全），晚上L计划算好后由虚拟机另推一条。
    """
    if not signal:
        if occasion == "收盘":
            return "L：明日L计划等晚上龙虎榜数据出全后算好单独推送。", ""
        return "L：没有收到今日L计划，今日L不开仓。", ""
    if signal.get("planned_buy_date") and str(signal["planned_buy_date"]) != str(action_date):
        return "L：计划执行日期不符，不开L。", ""
    if str(signal.get("l_version", "")) != str(l_version):
        return "L：计划版本不符，不开L。", ""
    if str(signal.get("status", "")) != "SIGNAL_READY" or not signal.get("ts_code"):
        return "L：龙虎榜没有符合L规则的股票，不开L。", ""
    if has_l_position:
        return "L：仍有L持仓（今天收盘卖出），L同一时间最多1只，不开新L。", ""
    if window_closed:
        return "L：原计划开仓窗口已结束，不新增L持仓。", ""
    code, name = str(signal["ts_code"]), str(signal.get("name") or signal["ts_code"])
    if is_st_name(name):
        return f"L：首选{name}({code})名称带ST，不开L（不递补）。", ""
    if normalize_ts_code(code) in {normalize_ts_code(c) for c in ace_codes}:
        return f"L：首选{name}({code})与ACE同票，不开L（不递补）。", ""
    allowed = bool(signal.get("l_allowed", False))
    pct = l_target_pct(ace_mode=gate_mode, l_self_allowed=allowed, streak_mult=streak_mult)
    reasons = ["方案甲停手期60%" if gate_mode == GATE_REDUCED else "方案甲正常期25%"]
    if not allowed:
        reasons.append("L自身停手×20/82.5")
    if float(streak_mult) < 1.0:
        reasons.append("连亏降仓×%.1f" % float(streak_mult))
    pct_text = f"{pct * 100:.2f}".rstrip("0").rstrip(".")
    amount = "预估金额待执行前定仓（尚无可用的账户定仓快照）"
    budget = (_l_budget_preview(plan, gate_mode, list(positions or []), config, target_pct=pct, streak_mult=streak_mult)
              if config is not None and plan is not None else None)
    if budget is not None:
        if budget["amount"] <= 0:
            return "L：按现有账户快照扣除ACE预留及现金缓冲后，预估可用预算0元，不开新L。", ""
        share = f"{budget['amount'] / budget['total_asset'] * 100:.2f}".rstrip("0").rstrip(".")
        amount = f"预估开仓约{budget['amount'] / 10000:.2f}万（占总资产{share}%，执行前重算）"
    return (f"L计划：{name}({code})｜{amount}；规则仓位上限为总资产{pct_text}%（{'，'.join(reasons)}；预算受可用现金、"
            "ACE预留及账户总仓位99.5%约束）。09:20按涨停价-0.01预挂，开盘涨停不买、不递补，余量POV补足"
            "（≤开盘+2%），买入后下一个交易日收盘卖出。", name)
