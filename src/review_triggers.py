"""方案甲临时复核触发器：所有“需要临时复核”的条件在这里统一判定和推送。

失败条件表（docs/equity_curve_stop.md第九节第6小节）原来分散在周报里，只在每周最后一个
开市日检查一次，提醒埋在周报末尾；另有几条完全没有自动检查：实盘连续3个月亏损、实盘
与正式规则逐笔对不上（漏单、多出的单、停手日开仓；ACE+M起还包括M买错、D在不该启动的
日子开仓）、M实盘滑点超过报警线（ACE+M起）、监控本身没算成。本模块每个交易日由收盘流水线⑬调用一次：
- 任何一条命中，单独推送“请发起甲·临时复核”，写明每一条的原因；
- 同一组条件持续存在时，每隔若干个交易日再提醒一次；新增条件立即推送；
- 全部解除时推送一次解除，与提醒成对；
- 推送失败不写状态，下一个交易日自动重发；某项检查自己算失败，也作为一条推送，
  不允许“没算成”被当成“没问题”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from src.market_rules import is_st_name

REVIEW_ACTION = "请在Claude发送：甲·临时复核"
CHECK_REPAIR_ACTION = "请在Claude发送：甲·临时复核（先修好这项检查）"
M_LEG = "M"


@dataclass(frozen=True)
class ReviewTrigger:
    key: str
    category: str
    detail: str
    requires_review: bool = True
    action: str = REVIEW_ACTION


@dataclass(frozen=True)
class PushPlan:
    kind: str  # "alert" / "reminder" / "cleared" / ""
    title: str
    body: str
    state: dict[str, Any] = field(default_factory=dict)


def shadow_triggers(
    values: Sequence[float],
    *,
    window_3m: int,
    window_6m: int,
    fail_3m: float,
    fail_6m: float,
    stop_episode: Mapping[str, Any] | None,
    worst_missed_gain: float,
) -> list[ReviewTrigger]:
    """影子账滚动收益跌破失败线；停手期间踏空超过历史最差（只记入年度复核）。"""

    triggers: list[ReviewTrigger] = []
    count = len(values)

    def _change(offset: int) -> float | None:
        return float(values[-1] / values[-1 - offset] - 1.0) if count > offset else None

    return_3m = _change(window_3m)
    return_6m = _change(window_6m)
    if return_3m is not None and return_3m <= fail_3m:
        triggers.append(ReviewTrigger(
            "shadow_3m", "策略逻辑",
            f"影子账近3个月{return_3m:+.1%}，跌破失败线{fail_3m:+.1%}（选股逻辑可能失效）",
        ))
    if return_6m is not None and return_6m <= fail_6m:
        triggers.append(ReviewTrigger(
            "shadow_6m", "策略逻辑",
            f"影子账近6个月{return_6m:+.1%}，跌破失败线{fail_6m:+.1%}（选股逻辑可能失效）",
        ))
    if stop_episode and float(stop_episode["shadow_return"]) >= worst_missed_gain:
        triggers.append(ReviewTrigger(
            f"stop_missed_gain:{stop_episode['start_date']}", "停手参数",
            f"本段停手踏空{float(stop_episode['shadow_return']):+.1%}，超过历史最差{worst_missed_gain:+.1%}"
            "（停手参数留待年度复核，当年不改）",
            requires_review=False,
            action="记入复核记录，作为年度复核输入；停手参数当年不改",
        ))
    return triggers


def regime_stall_triggers(stall: Mapping[str, Any] | None) -> list[ReviewTrigger]:
    if not stall or not stall.get("triggered"):
        return []
    months = "、".join(str(month) for month in stall.get("months", []))
    return [ReviewTrigger(
        "regime_stall", "策略逻辑",
        f"行情正常（涨停≥{float(stall['min_limit_up']):.0f}）却连续{int(stall['streak'])}个月不赚钱：{months}"
        "（选股规则可能已钝化）",
    )]


def factor_health_triggers(health: Mapping[str, Mapping[str, Any]] | None) -> list[ReviewTrigger]:
    triggers: list[ReviewTrigger] = []
    for leg, item in sorted((health or {}).items()):
        if item and item.get("triggered"):
            triggers.append(ReviewTrigger(
                f"factor_health:{leg}", "策略逻辑",
                f"{leg}腿条件集滚动12个月优势{float(item['value']):+.2%}，"
                f"连续{int(item['months_below'])}个月跌破历史最低{float(item['line']):+.2%}"
                f"（{int(item['samples'])}个样本，选股土壤可能已退化）",
            ))
    return triggers


def monthly_realized_pnl(completed: pd.DataFrame, months: Sequence[str]) -> list[tuple[str, float, int]]:
    """按卖出月份汇总真实已实现盈亏；没有成交的月份记0笔0元（不算亏损月）。"""

    if completed.empty:
        return [(month, 0.0, 0) for month in months]
    frame = completed.assign(_month=completed["exit_date"].astype(str).str[:6])
    grouped = frame.groupby("_month")["net_pnl"].agg(["sum", "size"])
    return [
        (month, float(grouped.loc[month, "sum"]) if month in grouped.index else 0.0,
         int(grouped.loc[month, "size"]) if month in grouped.index else 0)
        for month in months
    ]


def live_loss_streak_triggers(
    monthly: Sequence[tuple[str, float, int]], *, need_months: int = 3
) -> list[ReviewTrigger]:
    """实盘账户连续need_months个已结束自然月净亏损（全部腿的真实成交）。"""

    streak: list[tuple[str, float, int]] = []
    for month, pnl, count in reversed(list(monthly)):
        if count > 0 and pnl < 0:
            streak.append((month, pnl, count))
        else:
            break
    if len(streak) < need_months:
        return []
    streak.reverse()
    text = "、".join(f"{month}月{pnl:+,.0f}元（{count}笔）" for month, pnl, count in streak)
    return [ReviewTrigger(
        f"live_loss_streak:{streak[0][0]}", "账户",
        f"实盘账户连续{len(streak)}个月亏损：{text}",
    )]


def _text(value: Any) -> str:
    """空值（None、NaN）返回空串，其余转成去掉首尾空白的文本。"""

    if value is None or value != value:
        return ""
    return str(value).strip()


def _date_text(value: Any) -> str:
    """把20261113、20261113.0、"2026-11-13"都规整成"20261113"；空值返回空串。"""

    if isinstance(value, float) and value == value:
        value = int(value)
    text = _text(value)
    if text.lower() in ("", "nan", "none", "nat"):
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    return text.replace("-", "")[:8]


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _codes_text(codes: Sequence[str], limit: int = 5) -> str:
    if not codes:
        return "无"
    shown = "、".join(codes[:limit])
    return f"{shown}等{len(codes)}只" if len(codes) > limit else shown


def _live_buys(live: pd.DataFrame) -> list[dict[str, Any]]:
    """实盘每笔买入规整成统一字段；可选列（名称、平仓状态、成交额）缺失时留空。"""

    buys = []
    for record in live.to_dict("records"):
        buys.append({
            "date": _date_text(record.get("entry_date")),
            "code": _text(record.get("ts_code")),
            "leg": _text(record.get("strategy_leg")).upper(),
            "name": _text(record.get("name")),
            "status": _text(record.get("execution_status")),
            "exit_date": _date_text(record.get("exit_date")),
            "fill_amount": _positive(record.get("entry_fill_amount")),
        })
    return buys


def _held_from_before(buys: Sequence[Mapping[str, Any]], day: str) -> list[Mapping[str, Any]]:
    """day之前买入、day当天开盘时仍在账户里的买入记录。

    只认能确定的：还没平完的（持仓中、平仓中），或已平仓但最后一笔卖出不早于day的。
    汇总表没有平仓状态时一笔都不认，相关检查自然不报警。
    """

    held = []
    for buy in buys:
        if not buy["date"] or buy["date"] >= day:
            continue
        if buy["status"] in ("持仓中", "平仓中") or (buy["status"] == "已平仓" and buy["exit_date"] >= day):
            held.append(buy)
    return held


def _m_reference_for(m_reference: Callable[[str], Mapping[str, Any] | None] | None, day: str) -> tuple[list, str]:
    """取day的M候选单参照；返回（候选列表，错误说明）。错误说明非空表示这天无法检查。"""

    if m_reference is None:
        return [], "没有提供M候选单参照"
    reference = m_reference(day)
    if not reference or str(reference.get("status", "")) != "OK":
        return [], _text((reference or {}).get("error")) or "M候选单参照不可用"
    return list(reference.get("candidates") or []), ""


def _first_buyable_ahead(
    candidates: Sequence[Mapping[str, Any]], bought: str, *, excused: set[str], budget: float | None
) -> str:
    """排在已买股票前面、且能确定“当天本该先买它”的第一只候选，没有则返回空串。

    实盘按排名跳过这几类：ACE的股票、ST、停牌/无行情、开盘就涨停、钱不够买一手。
    只有逐项都能确认不属于这几类才算；任何一项无法确认（没有开盘价或涨停价、
    实盘成交额未知）都不报警。钱够不够用已买股票的实际成交额作下限来判断。
    """

    for candidate in candidates:
        code = _text(candidate.get("ts_code"))
        if code == bought:
            return ""
        if not code or code in excused or is_st_name(_text(candidate.get("name"))):
            continue
        open_price = _positive(candidate.get("buy_open"))
        limit_up = _positive(candidate.get("buy_limit_up"))
        if open_price is None or limit_up is None or budget is None:
            continue
        if open_price >= limit_up - 1e-6:
            continue
        lot = 200 if code.startswith(("688", "689")) else 100
        if lot * (limit_up - 0.01) <= budget + 1e-6:
            return code
    return ""


_RECOMPUTED_NOTE = "候选单是当晚用Tushare数据重算的，可能是实盘做错，也可能是QMT与Tushare数据不一致，需要复核"


def _d_leg_trigger(
    day: str,
    code: str,
    *,
    stops: set[str],
    planned_days: set[str],
    m_held: Sequence[str],
    m_bought: Sequence[str],
    m_reference: Callable[[str], Mapping[str, Any] | None] | None,
) -> ReviewTrigger | None:
    """ACE+M起D腿只在这样的日子启动：不停手、A/C/E没有计划、账户里没有M、M当天也没有计划。"""

    if day in stops:
        return ReviewTrigger(
            f"alignment:stop_day:{day}:{code}", "实盘执行",
            f"{day}是停手日，D腿不启动，实盘却买入了D腿{code}",
        )
    if day in planned_days:
        return ReviewTrigger(
            f"alignment:d_conflict:{day}:{code}", "实盘执行",
            f"{day}A/C/E已有开仓计划，实盘D腿却买入了{code}",
        )
    m_codes = sorted(set(m_held) | set(m_bought))
    if m_codes:
        listed = _codes_text(m_codes)
        return ReviewTrigger(
            f"alignment:d_m_position:{day}:{code}", "实盘执行",
            f"{day}账户里有M的持仓或当天买入（{listed}），D腿不启动，实盘却买入了D腿{code}",
        )
    candidates, error = _m_reference_for(m_reference, day)
    if error:
        return ReviewTrigger(
            f"alignment:d_unchecked:{day}:{code}", "检查失效",
            f"{day}实盘买入了D腿{code}，但M候选单没重算出来（{error}），"
            "“M当天有计划时D腿不启动”这一条没有被检查",
            action=CHECK_REPAIR_ACTION,
        )
    if candidates:
        listed = _codes_text([_text(c.get("ts_code")) for c in candidates])
        return ReviewTrigger(
            f"alignment:d_m_plan:{day}:{code}", "实盘执行",
            f"{day}M当天有开仓候选（{listed}），D腿不启动，实盘却买入了D腿{code}；{_RECOMPUTED_NOTE}",
        )
    return None


def _m_buy_triggers(
    buy: Mapping[str, Any],
    *,
    ace_codes: set[str],
    excused: set[str],
    m_held: Sequence[str],
    m_reference: Callable[[str], Mapping[str, Any] | None] | None,
) -> list[ReviewTrigger]:
    """一笔M买入对照M的规则：不买ST、只买候选单里排最前的可买候选、不买ACE的股票、同时最多一只。"""

    day, code, name = buy["date"], buy["code"], buy["name"]
    triggers: list[ReviewTrigger] = []
    if is_st_name(name):
        triggers.append(ReviewTrigger(
            f"alignment:m_st:{day}:{code}", "实盘执行",
            f"{day}M买入了{code}{name}，名称带ST或退市标记；M不允许买这类股票",
        ))
    else:
        candidates, error = _m_reference_for(m_reference, day)
        ranked = [_text(c.get("ts_code")) for c in candidates]
        if error:
            triggers.append(ReviewTrigger(
                f"alignment:m_unchecked:{day}:{code}", "检查失效",
                f"{day}M买入了{code}，但M候选单没重算出来（{error}），这一笔买得对不对没有被检查",
                action=CHECK_REPAIR_ACTION,
            ))
        elif code not in ranked:
            listed = _codes_text(ranked)
            triggers.append(ReviewTrigger(
                f"alignment:m_extra:{day}:{code}", "实盘执行",
                f"{day}M买入了{code}，不在当天的M候选单里（候选：{listed}）；{_RECOMPUTED_NOTE}",
            ))
        else:
            ahead = _first_buyable_ahead(candidates, code, excused=excused, budget=buy["fill_amount"])
            if ahead:
                rank = ranked.index(code) + 1
                triggers.append(ReviewTrigger(
                    f"alignment:m_order:{day}:{code}", "实盘执行",
                    f"{day}M买入了候选第{rank}名{code}，但排在前面的{ahead}当天开盘没封涨停、"
                    f"钱也够买一手，应当先买它；{_RECOMPUTED_NOTE}",
                ))
    if code in ace_codes:
        triggers.append(ReviewTrigger(
            f"alignment:m_ace_same:{day}:{code}", "实盘执行",
            f"{day}M买入了{code}，但方案甲当天也买了或还持有这只；M应当跳过方案甲的股票",
        ))
    if m_held:
        listed = _codes_text(sorted(set(m_held)))
        triggers.append(ReviewTrigger(
            f"alignment:m_overlap:{day}:{code}", "实盘执行",
            f"{day}M买入了{code}，但之前买的M（{listed}）当天还没卖完；M同一时间最多持有一只",
        ))
    return triggers


def alignment_triggers(
    replay: pd.DataFrame,
    live: pd.DataFrame,
    *,
    stop_dates: Iterable[str],
    start: str,
    end: str,
    ace_m_start: str = "",
    m_reference: Callable[[str], Mapping[str, Any] | None] | None = None,
    ace_plan_codes: Mapping[str, Iterable[str]] | None = None,
) -> list[ReviewTrigger]:
    """实盘逐笔与正式规则（单账户回放）对照。

    replay：回放明细，需要action_date、ts_code、strategy_leg、position_opened；
    live：实盘真实买入，需要entry_date、ts_code、strategy_leg；ACE+M的检查还会用到
    name、execution_status、exit_date、entry_fill_amount（缺哪列，用到它的检查就不报警）。
    D是盘中腿，不在影子账里，只检查它是否在不该启动的日子开仓。

    ace_m_start留空：全程按ACE+M之前的口径——停手日任何腿都不该开仓，replay应是
    停手过滤后的回放。
    ace_m_start有值：从这个行动日起按ACE+M口径，replay从这天起不应再剔除停手日计划：
    - A/C/E：停手日照常开仓（只是小仓），所以停手日也按回放逐笔对照；
    - D：停手日、A/C/E有计划、账户里有M、M当天有候选，这四种日子都不该开仓；
    - M：不买ST；买的必须在m_reference(买入日)给出的候选单里，且排在它前面的候选
      都有不买的理由（ACE的股票、开盘就涨停、停牌、钱不够一手）；不买ACE当天买入或
      持有的股票；前一笔M没卖完不买；一天最多买一只。M停手日照常买，不算违规。
      M“该买没买”不检查。
    m_reference(买入日)返回 {"status": "OK"或"ERROR", "error": 原因,
    "candidates": [{"ts_code", "name", "buy_open", "buy_limit_up"}, ...按排名先后]}；
    拿不到候选单时，相关的那一笔报“没有被检查”，不当成没问题。
    ace_plan_codes：每个行动日A/C/E各腿计划过的全部股票（M会回避，不能据此说M买错了顺序）。
    """

    stops = {str(date) for date in stop_dates}
    rule_start = str(ace_m_start or "")
    opened = replay[
        replay["action_date"].astype(str).between(start, end)
        & replay["position_opened"].eq(True)
    ]
    planned = {(str(r.action_date), str(r.ts_code)): str(r.strategy_leg) for r in opened.itertuples()}
    planned_days = {date for date, _code in planned}
    window = live[live["entry_date"].astype(str).between(start, end)]

    def ace_m_rules(date: str) -> bool:
        return bool(rule_start) and date >= rule_start

    actual: dict[tuple[str, str], str] = {}
    m_buys: dict[str, list[dict[str, Any]]] = {}
    for buy in _live_buys(window):
        if buy["leg"] == M_LEG and ace_m_rules(buy["date"]):
            m_buys.setdefault(buy["date"], []).append(buy)
        else:
            actual[(buy["date"], buy["code"])] = buy["leg"]
    history = _live_buys(live) if rule_start and "execution_status" in live.columns else []

    triggers: list[ReviewTrigger] = []
    for (date, code), leg in sorted(planned.items()):
        if (date, code) not in actual:
            reason = "漏单、没成交，或当天可用额度不够买一手" if ace_m_rules(date) else "漏单或没成交"
            triggers.append(ReviewTrigger(
                f"alignment:missing:{date}:{code}", "实盘执行",
                f"{date}正式规则应开仓{leg}腿{code}，实盘没有买入（{reason}）",
            ))
    for (date, code), leg in sorted(actual.items()):
        if ace_m_rules(date):
            if leg == "D":
                trigger = _d_leg_trigger(
                    date, code, stops=stops, planned_days=planned_days,
                    m_held=[b["code"] for b in _held_from_before(history, date) if b["leg"] == M_LEG],
                    m_bought=[b["code"] for b in m_buys.get(date, [])],
                    m_reference=m_reference,
                )
                if trigger is not None:
                    triggers.append(trigger)
            elif (date, code) not in planned:
                triggers.append(ReviewTrigger(
                    f"alignment:extra:{date}:{code}", "实盘执行",
                    f"{date}实盘买入了{leg}腿{code}，正式规则当天没有这笔",
                ))
        elif date in stops:
            triggers.append(ReviewTrigger(
                f"alignment:stop_day:{date}:{code}", "实盘执行",
                f"{date}是停手日，实盘却买入了{leg}腿{code}",
            ))
        elif leg == "D":
            if date in planned_days:
                triggers.append(ReviewTrigger(
                    f"alignment:d_conflict:{date}:{code}", "实盘执行",
                    f"{date}A/C/E已有开仓计划，实盘D腿却买入了{code}",
                ))
        elif (date, code) not in planned:
            triggers.append(ReviewTrigger(
                f"alignment:extra:{date}:{code}", "实盘执行",
                f"{date}实盘买入了{leg}腿{code}，正式规则当天没有这笔",
            ))

    for date in sorted(m_buys):
        held = _held_from_before(history, date)
        held_ace = {b["code"] for b in held if b["leg"] != M_LEG}
        bought_ace = {code for (day, code), leg in actual.items() if day == date and leg != "D"}
        planned_ace = {str(code) for code in (ace_plan_codes or {}).get(date, [])}
        rows = sorted(m_buys[date], key=lambda b: b["code"])
        bought_m = {b["code"] for b in rows}  # 一天买了多只另报m_multi，不再互相算作“该先买”
        for buy in rows:
            triggers.extend(_m_buy_triggers(
                buy,
                ace_codes=held_ace | bought_ace,
                excused=held_ace | bought_ace | planned_ace | bought_m,
                m_held=[b["code"] for b in held if b["leg"] == M_LEG],
                m_reference=m_reference,
            ))
        codes = sorted({b["code"] for b in rows})
        if len(codes) > 1:
            listed = _codes_text(codes)
            triggers.append(ReviewTrigger(
                f"alignment:m_multi:{date}", "实盘执行",
                f"{date}M一天买了{len(codes)}只（{listed}）；M一天最多买一只",
            ))
    return triggers


def m_slippage_stats(live: pd.DataFrame, *, start: str, trades: int, limit_pct: float) -> dict[str, Any]:
    """M最近trades笔已平仓交易的实盘平均双边滑点（%），即成交汇总的total_slippage_bps/100：
    买入均价比开盘价高多少加上卖出均价比收盘价低多少，正数表示比回测基准差。

    只算start（ACE+M生效日）起、已平仓、买卖均价和开盘/收盘基准都齐全的M（缺基准时汇总里
    滑点记为0，不能算进去）；满trades笔才判断是否超过limit_pct。
    """

    done = []
    if start and len(live):
        for record in live.to_dict("records"):
            if _text(record.get("strategy_leg")).upper() != M_LEG:
                continue
            day = _date_text(record.get("entry_date"))
            if not day or day < str(start) or _text(record.get("execution_status")) != "已平仓":
                continue
            prices = [_positive(record.get(c)) for c in ("entry_vwap", "benchmark_open", "exit_vwap", "benchmark_close")]
            total = _number(record.get("total_slippage_bps"))
            if None in prices or total is None:
                continue
            done.append((day, _text(record.get("ts_code")), total / 100.0))
    done.sort()
    window = done[-int(trades):] if int(trades) > 0 else []
    average = sum(pct for _day, _code, pct in window) / len(window) if window else None
    return {
        "completed": len(done),
        "trades": int(trades),
        "limit_pct": float(limit_pct),
        "average_pct": average,
        "window": [{"entry_date": day, "ts_code": code, "slippage_pct": round(pct, 4)} for day, code, pct in window],
        "triggered": bool(int(trades) > 0 and len(window) >= int(trades) and average > float(limit_pct)),
    }


def m_slippage_triggers(stats: Mapping[str, Any] | None) -> list[ReviewTrigger]:
    """M实盘滑点超过报警线：回测按双边0.2%，双边约3%~4%时加M就不如只用ACE。"""

    if not stats or not stats.get("triggered"):
        return []
    window = list(stats.get("window") or [])
    return [ReviewTrigger(
        "m_slippage", "实盘执行",
        "M最近%d笔（%s~%s买入）实盘平均双边滑点%+.2f%%，超过报警线%.1f%%；回测按双边0.2%%，"
        "双边约3%%~4%%时加M就不如只用ACE，请查竞价和补单的成交价" % (
            len(window), window[0]["entry_date"], window[-1]["entry_date"],
            float(stats["average_pct"]), float(stats["limit_pct"])),
    )]


def check_failure_trigger(name: str, error: BaseException | str) -> ReviewTrigger:
    """某项检查自己算失败：等于这一项今天没有被检查，必须让用户知道。"""

    return ReviewTrigger(
        f"check_failed:{name}", "检查失效",
        f"{name}没算成（{error}），这一项今天没有被检查",
        action=CHECK_REPAIR_ACTION,
    )


def _open_days_between(open_dates: Sequence[str], after: str, through: str) -> int:
    return sum(1 for date in open_dates if after < str(date) <= through)


def active_acknowledgements(entries: Iterable[Mapping[str, Any]] | None, today: str) -> dict[str, str]:
    """配置里登记的“已复核、仍在观察”条件；过了recheck_by日期自动失效、恢复提醒。"""

    result: dict[str, str] = {}
    today_compact = str(today).replace("-", "")
    for entry in entries or []:
        key = str(entry.get("key", "") or "")
        recheck_by = str(entry.get("recheck_by", "") or "").replace("-", "")
        if not key or not recheck_by or today_compact >= recheck_by:
            continue
        result[key] = (
            f"{entry.get('reviewed_on', '')}已复核：{entry.get('conclusion', '')}"
            f"（{entry.get('recheck_by', '')}前不重复提醒）"
        )
    return result


def plan_push(
    triggers: Sequence[ReviewTrigger],
    state: Mapping[str, Any],
    *,
    today: str,
    open_dates: Sequence[str],
    remind_every_open_days: int,
    acknowledged: Mapping[str, str] | None = None,
) -> PushPlan:
    """决定今天推什么；返回的state只在推送成功（或无需推送）后才应写回。

    已登记复核的条件不单独提醒，但其他条件推送时附在末尾；新增条件立即推送，
    持续中的条件每remind_every_open_days个交易日重复一次，全部解除时推送一次。
    """

    acknowledged = dict(acknowledged or {})
    previous = {str(key) for key in state.get("active_keys", [])}
    first_seen = {str(k): str(v) for k, v in (state.get("first_seen", {}) or {}).items()}
    pending = [trigger for trigger in triggers if trigger.key not in acknowledged]
    watched = [trigger for trigger in triggers if trigger.key in acknowledged]
    keys = {trigger.key for trigger in pending}
    new_keys = keys - previous
    last_alert = str(state.get("last_alert_date", "") or "")
    next_first_seen = {key: first_seen.get(key, today) for key in keys}
    base_state = {"active_keys": sorted(keys), "first_seen": next_first_seen, "last_alert_date": last_alert}

    if not keys:
        if previous:
            return PushPlan(
                "cleared",
                "✅ 方案甲临时复核条件已全部解除",
                f"截至{today}，此前提醒的{len(previous)}项条件都已解除或已登记复核；周报照常跟踪。",
                {"active_keys": [], "first_seen": {}, "last_alert_date": last_alert},
            )
        return PushPlan("", "", "", base_state)

    due = not last_alert or _open_days_between(open_dates, last_alert, today) >= remind_every_open_days
    if not new_keys and not due:
        return PushPlan("", "", "", base_state)

    ordered = sorted(pending, key=lambda t: (not t.requires_review, t.key not in new_keys, t.key))
    lines = []
    for index, trigger in enumerate(ordered, 1):
        tag = "新" if trigger.key in new_keys else f"自{next_first_seen[trigger.key]}起"
        lines.append(f"{index}.【{trigger.category}·{tag}】{trigger.detail}")
    actions = []
    for trigger in ordered:
        if trigger.action not in actions:
            actions.append(trigger.action)
    needs_review = any(trigger.requires_review for trigger in pending)
    if needs_review:
        title = (
            f"🚨 请发起甲·临时复核：{len(keys)}项触发"
            if new_keys
            else f"🚨 仍需甲·临时复核：{len(keys)}项条件持续中"
        )
    else:
        title = f"📝 方案甲需记录事项：{len(keys)}项"
    body = "；".join(lines) + "。处理：" + "；".join(actions) + "。"
    if watched:
        body += "另有已复核、仍在观察：" + "；".join(
            f"{trigger.detail}（{acknowledged[trigger.key]}）" for trigger in watched
        ) + "。"
    return PushPlan(
        "alert" if new_keys else "reminder",
        title,
        body,
        {**base_state, "last_alert_date": today},
    )
