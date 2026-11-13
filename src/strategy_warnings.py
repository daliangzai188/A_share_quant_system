"""方案甲三级警告（2026-11-13用户要求）：策略什么时候算走坏、该优化哪一部分。

- 初级（临时复核）：src.review_triggers 里原有的条件，出现异常先查原因，交易照常；
- 中级（策略复核）：按规则回放确认某一部分变差、超出历史正常范围，要研究优化，交易照常；
- 终极（建议停止交易、优化策略）：比历史上最差还差，或现行版本已不满足自己的上线硬门槛，
  或中级警告60个交易日没有处理。只推送和在终端打印，**不自动停止任何程序**，停不停由用户决定。

每条警告写明范围：某个策略（A/C/E腿、M）、全部策略（两个及以上策略同时变差，或ACE选股整体）、
整体组合（仓位分配与ACE+M的组合方式）。所有判定都用规则回放（不受实盘成交影响）；阈值在
config/config.json 的 strategy_warnings，由2019-12-02~2026-11-10的回放算出后冻结
（方法见docs/equity_curve_stop.md第九节第8小节），换版时按新版本重算。
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from src.review_triggers import FINAL_STANDARD, ReviewTrigger

TIER_MIDDLE = 2
TIER_FINAL = 3
LEGS = ("A", "C", "E")
SCOPE_NAMES = {"A": "A腿", "C": "C腿", "E": "E腿", "M": "M", "ALL": "全部策略", "COMBO": "整体组合"}
MIDDLE_CATEGORY = "中级·策略复核"
FINAL_CATEGORY = "终极·建议停止交易"
MIDDLE_ACTION = "请在Claude发送：甲·策略复核（%s）"
FINAL_ACTION = "建议先暂停交易（虚拟机运行 py -3.11 stop_windows.py），再在Claude发送：甲·强制优化（%s）"


@dataclass(frozen=True)
class WarningSettings:
    enabled: bool = False
    replay_start: str = "20191202"
    window_days: int = 365
    min_trades: int = 10
    trade_cost: float = 0.002
    factor_health_months: int = 4
    relative_window: int = 252
    relative_days: int = 20
    escalate_open_days: int = 60
    middle: Mapping[str, float] = field(default_factory=dict)
    final: Mapping[str, float] = field(default_factory=dict)
    reference: Mapping[str, Any] = field(default_factory=dict)


def load_warning_settings(config: Mapping[str, Any]) -> WarningSettings:
    section = dict(config.get("strategy_warnings", {}) or {})
    return WarningSettings(
        enabled=bool(section.get("enabled", False)),
        replay_start=str(section.get("replay_start", "20191202")),
        window_days=int(section.get("window_days", 365)),
        min_trades=int(section.get("min_trades", 10)),
        trade_cost=float(section.get("trade_cost", 0.002)),
        factor_health_months=int(section.get("factor_health_months", 4)),
        relative_window=int(section.get("relative_window", 252)),
        relative_days=int(section.get("relative_days", 20)),
        escalate_open_days=int(section.get("escalate_open_days", 60)),
        middle={str(k): float(v) for k, v in dict(section.get("middle", {}) or {}).items()},
        final={str(k): float(v) for k, v in dict(section.get("final", {}) or {}).items()},
        reference=dict(section.get("reference", {}) or {}),
    )


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "1.0")


def leg_trades(legs: Mapping[str, pd.DataFrame], *, cost: float) -> dict[str, list[tuple[str, float]]]:
    """A/C/E各腿单独按规则做（每个成立、结果已知的计划都做，不管别的腿占不占用），每笔扣cost。"""

    result: dict[str, list[tuple[str, float]]] = {}
    for leg in LEGS:
        frame = legs.get(leg)
        rows: list[tuple[str, float]] = []
        if frame is not None and not frame.empty:
            for record in frame.to_dict("records"):
                if not (_truthy(record.get("position_opened")) and _truthy(record.get("outcome_observable"))
                        and str(record.get("status")) == "OK"):
                    continue
                value = float(record.get("stock_return_before_fees"))
                if value == value:
                    rows.append((str(record["action_date"]), value - float(cost)))
        result[leg] = sorted(rows)
    return result


def trailing_return(trades: Sequence[tuple[str, float]], *, window_days: int, min_trades: int) -> dict[str, Any]:
    """截至最后一笔的最近window_days天逐笔复利；不足min_trades笔时value为None。"""

    if not trades:
        return {"value": None, "trades": 0, "as_of": ""}
    ordered = sorted(trades)
    last = ordered[-1][0]
    since = (dt.datetime.strptime(last, "%Y%m%d") - dt.timedelta(days=int(window_days))).strftime("%Y%m%d")
    window = [ret for day, ret in ordered if since < day <= last]
    value = None
    if len(window) >= int(min_trades):
        value = 1.0
        for ret in window:
            value *= 1.0 + ret
        value -= 1.0
    return {"value": value, "trades": len(window), "as_of": last}


def drawdown(series: pd.Series) -> dict[str, Any]:
    """当前回撤（相对此前最高点）及最近一次到达最高点的日子。"""

    if series is None or series.empty:
        return {"value": None, "peak": "", "as_of": ""}
    values = series.astype(float)
    top = float(values.max())
    peak = values[values == top].index[-1]
    return {"value": float(values.iloc[-1] / top - 1), "peak": str(peak), "as_of": str(values.index[-1])}


def relative_behind(live: pd.Series, base: pd.Series, *, window: int) -> dict[str, Any]:
    """ACE+M与只用ACE的最近window个交易日收益之比；返回最新相对收益和末尾连续落后的交易日数。"""

    joined = pd.concat([live.astype(float), base.astype(float)], axis=1, keys=["live", "base"]).dropna()
    if len(joined) <= window:
        return {"value": None, "days_behind": 0, "as_of": ""}
    rel = (joined["live"] / joined["live"].shift(window)) / (joined["base"] / joined["base"].shift(window)) - 1
    rel = rel.dropna()
    behind = 0
    for value in reversed(rel.tolist()):
        if value < 0:
            behind += 1
        else:
            break
    return {"value": float(rel.iloc[-1]), "days_behind": behind, "as_of": str(rel.index[-1])}


def compute_metrics(
    shadow: Mapping[str, Any],
    health: Mapping[str, Mapping[str, Any]] | None,
    live_equity: pd.Series,
    ace_equity: pd.Series,
    live_trades: pd.DataFrame,
    settings: WarningSettings,
) -> dict[str, Any]:
    """三级警告用到的全部数值（也写进状态文件和周报）。"""

    legs = leg_trades(shadow["legs"], cost=settings.trade_cost)
    metrics: dict[str, Any] = {"legs": {}}
    for leg in LEGS:
        metrics["legs"][leg] = trailing_return(legs[leg], window_days=settings.window_days,
                                               min_trades=settings.min_trades)
    m_rows = []
    if live_trades is not None and not live_trades.empty:
        m = live_trades[live_trades["family"] == "M"]
        m_rows = sorted(zip(m["action_date"].astype(str), m["ret"].astype(float)))
    metrics["legs"]["M"] = trailing_return(m_rows, window_days=settings.window_days, min_trades=settings.min_trades)
    health = dict(health or {})
    metrics["factor_health"] = {
        leg: {"months_below": int((health.get(leg) or {}).get("months_below", 0) or 0),
              "value": (health.get(leg) or {}).get("value"), "line": (health.get(leg) or {}).get("line")}
        for leg in ("A", "C")
    }
    metrics["shadow_drawdown"] = drawdown(shadow["nav"])
    metrics["combo_drawdown"] = drawdown(live_equity)
    metrics["combo_relative"] = relative_behind(live_equity, ace_equity, window=settings.relative_window)
    return metrics


def _pct(value: float) -> str:
    return "%+.1f%%" % (value * 100)


def _trigger(tier: int, scope: str, key: str, detail: str) -> ReviewTrigger:
    name = SCOPE_NAMES.get(scope, scope)
    if tier >= TIER_FINAL:
        return ReviewTrigger(key, FINAL_CATEGORY, detail, action=FINAL_ACTION % name, tier=TIER_FINAL, scope=name)
    return ReviewTrigger(key, MIDDLE_CATEGORY, detail, action=MIDDLE_ACTION % name, tier=TIER_MIDDLE, scope=name)


def warning_triggers(metrics: Mapping[str, Any], settings: WarningSettings) -> list[ReviewTrigger]:
    """按冻结阈值判出中级、终极警告（不含“中级拖着不处理”的升级，见 escalate）。"""

    middle, final, reference = settings.middle, settings.final, settings.reference
    triggers: list[ReviewTrigger] = []
    middle_scopes: list[str] = []
    final_scopes: list[str] = []
    for leg in ("A", "C"):
        item = metrics["factor_health"][leg]
        if item["months_below"] >= settings.factor_health_months:
            middle_scopes.append(leg)
            triggers.append(_trigger(TIER_MIDDLE, leg, "warn2:leg:%s" % leg,
                "%s腿选股条件的优势已连续%d个月低于冻结线（中级线：连续%d个月；临时复核是2个月）" % (
                    leg, item["months_below"], settings.factor_health_months)))
    for leg in ("A", "C", "E", "M"):
        item = metrics["legs"][leg]
        value = item["value"]
        if value is None:
            continue
        label = "%s腿单独按规则" % leg if leg != "M" else "M按规则"
        if leg in final and value < final[leg]:
            final_scopes.append(leg)
            triggers.append(_trigger(TIER_FINAL, leg, "warn3:leg:%s" % leg,
                "%s最近12个月收益%s（%d笔，截至%s），低于历史最差%s" % (
                    label, _pct(value), item["trades"], item["as_of"], _pct(final[leg]))))
        elif leg in middle and value < middle[leg]:
            middle_scopes.append(leg)
            note = reference.get("middle_%s" % leg, "")
            triggers.append(_trigger(TIER_MIDDLE, leg, "warn2:leg:%s" % leg,
                "%s最近12个月收益%s（%d笔，截至%s），低于中级线%s%s" % (
                    label, _pct(value), item["trades"], item["as_of"], _pct(middle[leg]),
                    "（%s）" % note if note else "")))
    middle_scopes = sorted(set(middle_scopes), key=("A", "C", "E", "M").index)
    if len(middle_scopes) >= 2:
        names = "、".join(SCOPE_NAMES[s] for s in middle_scopes)
        triggers.append(_trigger(TIER_MIDDLE, "ALL", "warn2:all", "%s同时到中级警告" % names))
    if len(final_scopes) >= 2:
        names = "、".join(SCOPE_NAMES[s] for s in final_scopes)
        triggers.append(_trigger(TIER_FINAL, "ALL", "warn3:all", "%s同时到终极警告" % names))
    shadow = metrics["shadow_drawdown"]
    if shadow["value"] is not None and "shadow_drawdown" in final and shadow["value"] < final["shadow_drawdown"]:
        triggers.append(_trigger(TIER_FINAL, "ALL", "warn3:all:shadow",
            "ACE影子账（A/C/E选股、从不停手）自%s回撤%s，超过历史最深%s" % (
                shadow["peak"], _pct(shadow["value"]), _pct(final["shadow_drawdown"]))))
    combo = metrics["combo_drawdown"]
    if combo["value"] is not None:
        if "combo_drawdown" in final and combo["value"] < final["combo_drawdown"]:
            triggers.append(_trigger(TIER_FINAL, "COMBO", "warn3:combo",
                "ACE+M按现行规则和仓位回放，自%s回撤%s，超过现行版本的上线硬门槛%s" % (
                    combo["peak"], _pct(combo["value"]), _pct(final["combo_drawdown"]))))
        elif "combo_drawdown" in middle and combo["value"] < middle["combo_drawdown"]:
            triggers.append(_trigger(TIER_MIDDLE, "COMBO", "warn2:combo:drawdown",
                "ACE+M按现行规则和仓位回放，自%s回撤%s，超过历史最深%s" % (
                    combo["peak"], _pct(combo["value"]), _pct(middle["combo_drawdown"]))))
    relative = metrics["combo_relative"]
    if relative["value"] is not None and relative["days_behind"] >= settings.relative_days:
        triggers.append(_trigger(TIER_MIDDLE, "COMBO", "warn2:combo:relative",
            "ACE+M最近12个月收益比只用ACE（甲v1）低%.1f%%，已连续%d个交易日落后（中级线：连续%d个交易日%s）" % (
                abs(relative["value"]) * 100, relative["days_behind"], settings.relative_days,
                "；%s" % reference["relative_history"] if reference.get("relative_history") else "")))
    return triggers


def escalate(
    triggers: Sequence[ReviewTrigger],
    state: Mapping[str, Any],
    *,
    acknowledged: Mapping[str, str],
    today: str,
    open_dates: Sequence[str],
    open_days: int,
) -> list[ReviewTrigger]:
    """中级警告持续open_days个交易日仍未登记复核结论：另加一条同范围的终极警告。"""

    first_seen = {str(k): str(v) for k, v in (state.get("first_seen", {}) or {}).items()}
    dates = sorted(str(d) for d in open_dates)
    extra = []
    for trigger in triggers:
        if getattr(trigger, "tier", 1) != TIER_MIDDLE or trigger.key in acknowledged:
            continue
        since = first_seen.get(trigger.key, "")
        if not since:
            continue
        waited = sum(1 for d in dates if since < d <= str(today))
        if waited >= int(open_days):
            extra.append(ReviewTrigger(
                "warn3:escalated:%s" % trigger.key, FINAL_CATEGORY,
                "%s——这条中级警告自%s起已%d个交易日没有处理（没有登记复核结论），升为终极" % (
                    trigger.detail, since, waited),
                action=FINAL_ACTION % trigger.scope, tier=TIER_FINAL, scope=trigger.scope))
    return list(triggers) + extra


def warning_report(triggers: Sequence[ReviewTrigger], acknowledged: Mapping[str, str], metrics: Mapping[str, Any],
                   *, signal_date: str, generated_at: str) -> dict[str, Any]:
    """写给终端显示的状态：未处理的中级/终极警告（已登记复核的单列）。"""

    rows = []
    for trigger in sorted(triggers, key=lambda t: (-getattr(t, "tier", 1), t.key)):
        tier = getattr(trigger, "tier", 1)
        if tier < TIER_MIDDLE:
            continue
        rows.append({"tier": tier, "scope": getattr(trigger, "scope", ""), "key": trigger.key,
                     "detail": trigger.detail, "action": trigger.action,
                     "acknowledged": acknowledged.get(trigger.key, "")})
    return {"signal_date": str(signal_date), "generated_at": str(generated_at), "warnings": rows,
            "final_standard": FINAL_STANDARD if any(r["tier"] >= TIER_FINAL for r in rows) else "",
            "metrics": metrics}


def banner_lines(report: Mapping[str, Any] | None) -> list[str]:
    """终端横幅：终极警告醒目列出（建议停止交易），中级警告列出范围和处理口令；没有警告返回空列表。"""

    if not report:
        return []
    rows = [r for r in report.get("warnings", []) if not r.get("acknowledged")]
    if not rows:
        return []
    final_rows = [r for r in rows if int(r.get("tier", 0)) >= TIER_FINAL]
    middle_rows = [r for r in rows if int(r.get("tier", 0)) == TIER_MIDDLE]
    lines = ["=" * 60]
    if final_rows:
        scopes = "、".join(sorted({str(r.get("scope", "")) for r in final_rows}))
        lines.append("⛔ 终极警告（%s）：程序和策略已经走坏，建议停止交易、优化策略（%s）" % (
            report.get("signal_date", ""), scopes))
        for row in final_rows:
            lines.append("  - %s" % row.get("detail", ""))
        lines.append("  处理：%s" % final_rows[0].get("action", ""))
        if report.get("final_standard"):
            lines.append("  %s" % report["final_standard"])
        lines.append("  （系统不会自己停止交易，停不停由你决定）")
    if middle_rows:
        scopes = "、".join(sorted({str(r.get("scope", "")) for r in middle_rows}))
        lines.append("⚠️ 中级警告（%s）：%s需要策略复核" % (report.get("signal_date", ""), scopes))
        for row in middle_rows:
            lines.append("  - %s" % row.get("detail", ""))
        lines.append("  处理：%s" % middle_rows[0].get("action", ""))
    lines.append("=" * 60)
    return lines


def read_report(path: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


CACHE_VERSION = 1
REFRESH_OPEN_DAYS = 20


def _cached_m_inputs(reference: Callable[[str], Mapping[str, Any]], outcome: Callable[[str, str], Any],
                     open_days: Sequence[str], days: Sequence[str], cache_path: Path | str | None):
    """M候选单和每笔买卖结果：已算过的从缓存取，只重算新日子和最近REFRESH_OPEN_DAYS个交易日。"""

    cache: dict[str, Any] = {}
    path = Path(cache_path) if cache_path else None
    if path is not None and path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    if cache.get("version") != CACHE_VERSION:
        cache = {"version": CACHE_VERSION, "candidates": {}, "errors": {}, "outcomes": {}}
    recent = set(list(open_days)[-REFRESH_OPEN_DAYS:]) | set(list(days)[-REFRESH_OPEN_DAYS:])
    candidates: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for day in days:
        day = str(day)
        if day not in recent and day in cache["candidates"]:
            codes = list(cache["candidates"][day])
        elif day not in recent and day in cache["errors"]:
            errors[day] = str(cache["errors"][day])
            continue
        else:
            result = reference(day)
            if str(result.get("status", "")) != "OK":
                errors[day] = str(result.get("error", ""))
                cache["errors"][day] = errors[day]
                cache["candidates"].pop(day, None)
                continue
            codes = [str(c["ts_code"]) for c in result.get("candidates") or []]
            cache["candidates"][day] = codes
            cache["errors"].pop(day, None)
        if codes:
            candidates[day] = codes

    def cached_outcome(code: str, day: str):
        key = "%s|%s" % (code, day)
        if key in cache["outcomes"] and (cache["outcomes"][key] is not None or str(day) not in recent):
            value = cache["outcomes"][key]
            return None if value is None else (str(value[0]), float(value[1]), float(value[2]))
        value = outcome(code, day)
        cache["outcomes"][key] = None if value is None else [value[0], value[1], value[2]]
        return value

    def save() -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    return candidates, errors, cached_outcome, save


def run_strategy_warnings(
    shadow: Mapping[str, Any],
    health: Mapping[str, Mapping[str, Any]] | None,
    config: Mapping[str, Any],
    *,
    raw_dir: Path | str,
    cache_path: Path | str | None,
    today: str,
    ma_window: int,
    lag: int,
) -> tuple[list[ReviewTrigger], dict[str, Any]]:
    """ACE+M按现行规则和仓位回放、只用ACE回放、各腿单独，算出指标并判出中级/终极警告。"""

    settings = load_warning_settings(config)
    if not settings.enabled:
        return [], {}
    from src.ace_m_replay import RawPrices, ace_plans_by_day, make_m_outcome, replay_ace_m
    from src.equity_curve_stop import allowed_flags
    from src.strategy_m_reference import MReference

    nav = shadow["nav"]
    flags = allowed_flags(nav.to_numpy(), ma_window=ma_window, lag=lag)
    ace_allowed = {str(day): bool(ok) for day, ok in zip(nav.index, flags)}
    days = [str(d) for d in shadow["action_dates"] if settings.replay_start <= str(d) <= str(today)]
    reference = MReference(raw_dir)
    open_days = reference.open_days()
    candidates, errors, outcome, save = _cached_m_inputs(
        reference, make_m_outcome(RawPrices(raw_dir), open_days), open_days, days, cache_path)
    analysis = dict(config.get("analysis", {}) or {})
    fees = {
        "commission_rate": float(analysis.get("commission_rate", 0.0003)),
        "minimum_commission": float(analysis.get("minimum_commission", 5.0)),
        "transfer_rate": float(analysis.get("transfer_fee_rate", 0.00001)),
        "stamp_schedule": analysis.get("stamp_tax_schedule"),
    }
    plans = ace_plans_by_day(shadow["legs"])
    live_equity, live_trades = replay_ace_m(days, plans, ace_allowed, candidates, outcome, **fees)
    ace_equity, _ace_trades = replay_ace_m(days, plans, ace_allowed, {}, outcome, use_m=False,
                                           ace_halted_weight=0.0, **fees)
    save()
    metrics = compute_metrics(shadow, health, live_equity, ace_equity, live_trades, settings)
    metrics["replay"] = {
        "start": days[0] if days else "", "end": days[-1] if days else "",
        "ace_m_multiple": float(live_equity.iloc[-1] / live_equity.iloc[0]) if len(live_equity) else None,
        "ace_only_multiple": float(ace_equity.iloc[-1] / ace_equity.iloc[0]) if len(ace_equity) else None,
        "m_trades": int((live_trades["family"] == "M").sum()) if len(live_trades) else 0,
        "m_reference_error_days": len(errors), "m_reference_errors_recent": sorted(errors)[-5:],
    }
    return warning_triggers(metrics, settings), metrics
