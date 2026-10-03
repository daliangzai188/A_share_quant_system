"""方案甲临时复核触发器：所有“需要临时复核”的条件在这里统一判定和推送。

失败条件表（docs/equity_curve_stop.md第九节第6小节）原来分散在周报里，只在每周最后一个
开市日检查一次，提醒埋在周报末尾；另有几条完全没有自动检查：实盘连续3个月亏损、实盘
与正式规则逐笔对不上（漏单、多出的单、停手日开仓）、监控本身没算成。本模块每个交易日由
收盘流水线⑬调用一次：
- 任何一条命中，单独推送“请发起甲·临时复核”，写明每一条的原因；
- 同一组条件持续存在时，每隔若干个交易日再提醒一次；新增条件立即推送；
- 全部解除时推送一次解除，与提醒成对；
- 推送失败不写状态，下一个交易日自动重发；某项检查自己算失败，也作为一条推送，
  不允许“没算成”被当成“没问题”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

REVIEW_ACTION = "请在Claude发送：甲·临时复核"


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


def alignment_triggers(
    replay: pd.DataFrame,
    live: pd.DataFrame,
    *,
    stop_dates: Iterable[str],
    start: str,
    end: str,
) -> list[ReviewTrigger]:
    """实盘逐笔与正式规则（停手过滤后的单账户回放）对照。

    replay：回放明细，需要action_date、ts_code、strategy_leg、position_opened；
    live：实盘真实买入，需要entry_date、ts_code、strategy_leg。
    D是盘中腿，不在影子账里；只检查它是否在停手日或A/C/E已有计划的日子开仓。
    """

    stops = {str(date) for date in stop_dates}
    opened = replay[
        replay["action_date"].astype(str).between(start, end)
        & replay["position_opened"].eq(True)
    ]
    planned = {(str(r.action_date), str(r.ts_code)): str(r.strategy_leg) for r in opened.itertuples()}
    planned_days = {date for date, _code in planned}
    window = live[live["entry_date"].astype(str).between(start, end)]
    actual = {(str(r.entry_date), str(r.ts_code)): str(r.strategy_leg).upper() for r in window.itertuples()}

    triggers: list[ReviewTrigger] = []
    for (date, code), leg in sorted(planned.items()):
        if (date, code) not in actual:
            triggers.append(ReviewTrigger(
                f"alignment:missing:{date}:{code}", "实盘执行",
                f"{date}正式规则应开仓{leg}腿{code}，实盘没有买入（漏单或没成交）",
            ))
    for (date, code), leg in sorted(actual.items()):
        if date in stops:
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
    return triggers


def check_failure_trigger(name: str, error: BaseException | str) -> ReviewTrigger:
    """某项检查自己算失败：等于这一项今天没有被检查，必须让用户知道。"""

    return ReviewTrigger(
        f"check_failed:{name}", "检查失效",
        f"{name}没算成（{error}），这一项今天没有被检查",
        action="请在Claude发送：甲·临时复核（先修好这项检查）",
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
