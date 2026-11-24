"""方案甲策略日报（2026-11-14起，用户要求）：QMT外检查每个交易日收盘后只推这一条。

只管策略级别的提醒：临时复核、中级、终极，以及因子健康、各腿表现、逐笔对账、滑点、研究池覆盖率、信号断流
等数值。以前这些分成单独推送、每5个交易日重复提醒、周五因子健康周报和终端横幅几种，现在合成一份格式固定的
日报：标题直接写级别，要处理的问题排在最前，已登记复核的列为“观察中”，后面是各项数值。每个交易日都推，
没收到日报就是检查出了问题。早盘通知、盘中实时告警、QMT内发出的推送都不归这里管，照旧。
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Sequence

from src.portfolio_ace_m import REPORT_LEGS

LEVELS = ("ok", "note", "review", "middle", "final")


def _mmdd(day: Any) -> str:
    text = str(day or "")
    return "%s-%s" % (text[4:6], text[6:8]) if len(text) >= 8 else text


def _pct(value: float, digits: int = 1) -> str:
    return "%+.*f%%" % (digits, float(value) * 100)


def report_level(pending: Sequence[Any]) -> str:
    """ok=一切正常、note=只有需记录事项、review=临时复核、middle=中级、final=终极。"""

    tiers = [int(getattr(t, "tier", 1)) for t in pending]
    if any(tier >= 3 for tier in tiers):
        return "final"
    if any(tier == 2 for tier in tiers):
        return "middle"
    if any(getattr(t, "requires_review", True) for t in pending):
        return "review"
    return "note" if pending else "ok"


def build_daily_report(
    day: str,
    triggers: Sequence[Any],
    acknowledged: Mapping[str, str],
    recheck: Mapping[str, str],
    first_seen: Mapping[str, str],
    value_lines: Sequence[str],
    *,
    final_standard: str = "",
) -> dict[str, Any]:
    """日报的标题、正文和级别；first_seen是上次记下的各条件首次出现日，返回值里带上今天之后的新记录。"""

    acknowledged = dict(acknowledged or {})
    seen = {str(k): str(v) for k, v in dict(first_seen or {}).items()}
    unique: dict[str, Any] = {}
    for trigger in triggers:
        unique.setdefault(trigger.key, trigger)
    pending = [t for t in unique.values() if t.key not in acknowledged]
    watched = [t for t in unique.values() if t.key in acknowledged]
    level = report_level(pending)
    ordered = sorted(pending, key=lambda t: (-int(getattr(t, "tier", 1)), not getattr(t, "requires_review", True),
                                             t.key in seen, t.key))
    top = max([int(getattr(t, "tier", 1)) for t in ordered] or [1])
    scopes = "、".join(dict.fromkeys(str(getattr(t, "scope", "")) for t in ordered
                                     if int(getattr(t, "tier", 1)) == top and getattr(t, "scope", "")))
    mmdd = _mmdd(day)
    titles = {
        "ok": "✅ 方案甲策略日报 %s：一切正常" % mmdd,
        "note": "📝 方案甲策略日报 %s：%d项需记录" % (mmdd, len(pending)),
        "review": "🚨 方案甲策略日报 %s：需要临时复核（%d项）" % (mmdd, len(pending)),
        "middle": "⚠️ 方案甲策略日报 %s：中级警告，%s需要策略复核" % (mmdd, scopes),
        "final": "⛔ 方案甲策略日报 %s：终极警告，建议停止交易、优化策略（%s）" % (mmdd, scopes),
    }
    lines: list[str] = []
    if ordered:
        lines.append("要处理：")
        for index, trigger in enumerate(ordered, 1):
            tag = ("自%s起" % _mmdd(seen[trigger.key])) if trigger.key in seen else "新"
            lines.append("%d.【%s·%s】%s" % (index, trigger.category, tag, trigger.detail))
        actions: list[str] = []
        for trigger in ordered:
            if trigger.action and trigger.action not in actions:
                actions.append(trigger.action)
        lines.append("需要你做的：" + "；".join(actions))
        if level == "final" and final_standard:
            lines.append("%s。系统不会自己停止交易，停不停由你决定。" % final_standard)
    else:
        lines.append("要处理：无")
    if watched:
        items = []
        for trigger in watched:
            label = str(trigger.detail).split("（")[0]
            when = str(dict(recheck or {}).get(trigger.key, "") or "")
            items.append("%s（%s复查）" % (label, when) if when else label)
        lines.append("观察中（已复核，不用处理）：" + "；".join(items))
    lines.extend(str(line) for line in value_lines if line)
    return {
        "signal_date": str(day),
        "level": level,
        "title": titles[level],
        "lines": lines,
        "body": "\n".join(lines),
        "first_seen": {t.key: seen.get(t.key, str(day)) for t in pending},
    }


def _slippage(name: str, stats: Mapping[str, Any] | None, error: str) -> str:
    if error:
        return "%s滑点没算成" % name
    if stats is None:
        return ""
    done, need = int(stats.get("completed", 0) or 0), int(stats.get("trades", 10) or 10)
    limit, average = float(stats.get("limit_pct", 0) or 0), stats.get("average_pct")
    if not done:
        return "%s滑点：还没有已平仓的" % name
    if done < need:
        return "%s滑点：已平仓%d笔，平均双边%+.2f%%（满%d笔判断，线%.1f%%）" % (name, done, average, need, limit)
    return "%s滑点：最近%d笔平均双边%+.2f%%（线%.1f%%）" % (name, need, average, limit)


def _health_day(value: Any) -> str:
    text = str(value or "")
    try:
        return dt.datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d") if len(text) == 8 else ""
    except ValueError:
        return ""


def _health_leg_state(leg: str, item: Any, *, day: str, minimum: int) -> tuple[str, str]:
    """只解释现有规则回放的结果；缺数据不得伪装成零笔或健康。"""
    if not isinstance(item, Mapping) or not {"trades", "value", "as_of"}.issubset(item):
        return "error", "%s：样本数据缺失，健康状态未知。" % leg
    try:
        raw_count = item["trades"]
        count = float(raw_count)
        if isinstance(raw_count, bool) or not math.isfinite(count) or count < 0 or not count.is_integer():
            raise ValueError("invalid sample count")
        n = int(count)
    except (TypeError, ValueError, OverflowError):
        return "error", "%s：样本数无法核对，健康状态未知。" % leg
    if n == 0:
        return "empty", "%s：有效收益0笔，暂无法评估；是否无信号、结果未到期或缺数据，原因待核对。" % leg
    last = _health_day(item.get("as_of"))
    suffix = "（最近样本行动日%s）" % last if last else ""
    if not last or str(item["as_of"]) > day:
        return "error", "%s：有效收益%d笔，样本日期缺失或超出核对日，健康状态未知。" % (leg, n)
    if n < minimum:
        return "insufficient", "%s：有效收益%d笔／最低%d笔，样本不足，暂不判级%s。" % (leg, n, minimum, suffix)
    try:
        raw_value = item.get("value")
        value = float(raw_value)
        if isinstance(raw_value, bool) or not math.isfinite(value):
            raise ValueError("invalid return")
    except (TypeError, ValueError, OverflowError):
        return "error", "%s：有效收益%d笔，收益未算成，健康状态未知%s。" % (leg, n, suffix)
    return "available", "%s：规则复利%s，有效收益%d笔%s。" % (leg, _pct(value), n, suffix)


def _m_health_gap(warnings: Mapping[str, Any]) -> str:
    replay = warnings.get("replay") or {}
    if not isinstance(replay, Mapping):
        return "M候选重算完整性资料无法核对"
    errors = replay.get("m_reference_error_days", 0)
    try:
        count = float(errors)
        if isinstance(errors, bool) or not math.isfinite(count) or count < 0 or not count.is_integer():
            raise ValueError("invalid reference error count")
    except (TypeError, ValueError, OverflowError):
        return "M历史候选重算失败天数无法核对"
    if not count:
        return ""
    text = "M历史候选重算有%d天失败，回放完整性未确认" % int(count)
    recent = replay.get("m_reference_errors_recent") or []
    if isinstance(recent, (list, tuple)) and recent:
        text += "（最近失败日期%s）" % "、".join(str(d) for d in recent[-5:])
    return text


def strategy_health_data_issues(values: Mapping[str, Any]) -> list[str]:
    """与正文共用同一校验，只把统计失败并入既有日报，不把正常少样本报成故障。"""
    context = values.get("strategy_health_context") or {}
    if not context.get("enabled") or values.get("warnings_error"):
        return []
    day = str(context.get("signal_date", ""))
    if not _health_day(day):
        return ["策略体检核对日期缺失或无效"]
    warnings = values.get("warnings")
    if not isinstance(warnings, Mapping) or not isinstance(warnings.get("legs"), Mapping):
        return ["未取得当前规则回放结果，健康状态未知"]
    try:
        minimum, window = int(context["min_trades"]), int(context["window_days"])
        if minimum <= 0 or window <= 0:
            raise ValueError("invalid sample settings")
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["策略体检样本要求或统计窗口缺失"]
    issues = []
    for leg in REPORT_LEGS:
        status, text = _health_leg_state(leg, warnings["legs"].get(leg), day=day, minimum=minimum)
        if status == "error":
            issues.append(text)
    gap = _m_health_gap(warnings)
    if gap:
        issues.append(gap)
    return issues


def strategy_health_details(values: Mapping[str, Any]) -> list[str]:
    """把旧周报迁入已有日报，复用现成数值，不重新回放、不新增判级门槛。"""
    context = values.get("strategy_health_context") or {}
    day = str(context.get("signal_date", ""))
    date = _health_day(day)
    if not date:
        return ["策略体检：核对日期缺失或无效，健康状态未知。"]
    if not context.get("enabled", False):
        return ["策略体检（核对日%s）：规则回放检查未启用，未评估健康状态。" % date]
    header = "策略体检（核对日%s）" % date
    if values.get("warnings_error"):
        return [header + "：统计失败，健康状态未知；不能解释为零笔或策略正常。"]
    warnings = values.get("warnings")
    if not isinstance(warnings, Mapping) or not isinstance(warnings.get("legs"), Mapping):
        return [header + "：规则回放结果缺失，健康状态未知。"]
    try:
        minimum, window = int(context["min_trades"]), int(context["window_days"])
        if minimum <= 0 or window <= 0:
            raise ValueError("invalid sample settings")
    except (KeyError, TypeError, ValueError, OverflowError):
        return [header + "：样本要求或统计窗口缺失，暂无法评估。"]
    lines = [header + "：窗口%d天，最低%d笔；各腿窗口截至其最近样本行动日。" % (window, minimum)]
    as_of = _health_day((warnings.get("signals") or {}).get("as_of"))
    if as_of:
        lines[0] += " 数据重算截至%s。" % as_of
    for leg in REPORT_LEGS:
        _status, line = _health_leg_state(leg, warnings["legs"].get(leg), day=day, minimum=minimum)
        if leg == "M":
            gap = _m_health_gap(warnings)
            if gap:
                line = line.rstrip("。") + "；" + gap + "，健康状态未知。"
        lines.append(line)
    lines.append("口径：A/C/E各腿独立规则回放，M为ACE+M组合内回放，均非实盘成交收益；D不在本项统计范围。"
                 "本项供复核，不自动调整仓位。")
    return lines


def value_lines(values: Mapping[str, Any]) -> list[str]:
    """日报后半段的各项数值（只读QMT外检查已经算好的结果，不重新判断）。"""

    lines: list[str] = []
    parts = []
    if values.get("alignment_error"):
        parts.append("逐笔对账没算成")
    elif values.get("alignment") is None:
        parts.append("逐笔对账未启用")
    elif values["alignment"]:
        parts.append("逐笔对账%d处不一致" % len(values["alignment"]))
    else:
        parts.append("逐笔对账一致")
    for name, key in (("M", "m_slippage"), ("A/C/E", "ace_slippage")):
        text = _slippage(name, values.get(key), str(values.get(key + "_error") or ""))
        if text:
            parts.append(text)
    loss = values.get("live_loss")
    if values.get("live_loss_error"):
        parts.append("连续亏损没算成")
    elif loss is not None:
        parts.append("账户连续亏损%d个月（%d个月报警）" % (int(loss["months"]), int(loss["need"])))
    lines.append("实盘：" + "；".join(parts))

    parts = []
    coverage = values.get("coverage")
    if values.get("coverage_error"):
        parts.append("研究池覆盖率没算成")
    elif coverage is not None:
        rows, limit_up = int(coverage.get("pool_rows", 0) or 0), coverage.get("limit_up_count")
        if not limit_up:
            parts.append("没有全市场涨停统计，覆盖率未判断")
        else:
            parts.append("研究池覆盖率%.0f%%（%d/%d只，低于%.0f%%报警）" % (
                100.0 * rows / float(limit_up), rows, int(float(limit_up)), 100.0 * float(coverage.get("min_ratio", 0.3))))
    signals = values.get("signals") or {}
    if signals.get("gap") is not None:
        last = signals.get("last") or {}
        parts.append("最近一次选出股票%s（A %s、C %s、E %s、M %s），之后%d个行动日没有新的（超过%d天报警）" % (
            _mmdd(signals.get("last_any")) or "无", _mmdd(last.get("A")) or "无", _mmdd(last.get("C")) or "无",
            _mmdd(last.get("E")) or "无", _mmdd(last.get("M")) or "无", int(signals["gap"]), int(signals.get("line", 22))))
    if parts:
        lines.append("数据：" + "；".join(parts))

    if values.get("health_error"):
        lines.append("因子健康：没算成")
    elif values.get("health"):
        parts = []
        for leg, item in sorted(values["health"].items()):
            if not item or item.get("value") is None:
                parts.append("%s样本不足" % leg)
                continue
            below = int(item.get("months_below", 0) or 0)
            parts.append("%s %+.2f%%（线%+.2f%%%s）" % (leg, float(item["value"]) * 100, float(item["line"]) * 100,
                                                  "，连续%d个月在线下" % below if below else ""))
        lines.append("因子健康：" + "；".join(parts))

    warnings = values.get("warnings")
    if values.get("strategy_health_context") is not None:
        lines.extend(strategy_health_details(values))
    elif values.get("warnings_error"):
        lines.append("规则回放：没算成")
    if warnings and not values.get("warnings_error"):
        legs = warnings.get("legs") or {}

        def leg_text(leg: str) -> str:
            value = (legs.get(leg) or {}).get("value")
            return "%s %s" % (leg, "不足10笔" if value is None else _pct(value))

        if values.get("strategy_health_context") is None:
            lines.append("各腿近12个月（按规则）：" + "、".join(leg_text(leg) for leg in REPORT_LEGS))
        parts = []
        thresholds = values.get("thresholds") or {}
        combo = (warnings.get("combo_drawdown") or {}).get("value")
        if combo is not None:
            parts.append("ACE+M回撤%.1f%%（中级%.1f%%、终极%.1f%%）" % (
                combo * 100, float(thresholds.get("combo_middle", -0.315)) * 100,
                float(thresholds.get("combo_final", -0.5)) * 100))
        relative = (warnings.get("combo_relative") or {}).get("value")
        if relative is not None:
            parts.append("近12个月比只用ACE%s" % _pct(relative))
        if parts:
            lines.append("组合（按规则）：" + "；".join(parts))
        recent = warnings.get("m_recent")
        if recent:
            window = int(recent.get("window_trades", 10))
            value = recent.get("window_return")
            lines.append("M近况（按规则）：连亏%d笔（上线前最长%d笔，超过就提醒）；最近%d笔%s（上线前最差%s，低于就提醒）" % (
                int(recent.get("streak", 0) or 0), int(recent.get("max_loss_streak", 0) or 0), window,
                "不足%d笔" % window if value is None else _pct(value), _pct(float(recent.get("worst_window_return", -1.0)))))

    parts = []
    shadow = values.get("shadow")
    if values.get("shadow_error"):
        parts.append("影子账没算成")
    elif shadow:
        windows = ["%s%s（线%s）" % (label, _pct(shadow[key]), _pct(shadow[fail]))
                   for label, key, fail in (("近3个月", "r3", "fail_3m"), ("近6个月", "r6", "fail_6m"))
                   if shadow.get(key) is not None]
        if windows:
            parts.append("、".join(windows))
    stall = values.get("stall")
    if values.get("stall_error"):
        parts.append("正常行情连续不赚钱没算成")
    elif stall:
        parts.append("正常行情连续不赚钱%d/%d个月" % (int(stall.get("streak", 0) or 0), int(stall.get("need_months", 3) or 3)))
    if parts:
        lines.append("ACE影子账：" + "；".join(parts))
    return lines


def terminal_lines(report: Mapping[str, Any] | None) -> list[str]:
    """终端显示同一份日报（start_windows.py的日志窗口和状态显示）。"""

    if not report or not report.get("title"):
        return []
    return ["=" * 60, str(report["title"])] + [str(line) for line in report.get("lines") or []] + ["=" * 60]
