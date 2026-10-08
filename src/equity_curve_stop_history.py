"""方案甲每日全历史重算的输入覆盖检查；不修补数据、不更改停手规则。"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.historical_limit_counts import load_historical_limit_count


def audit_history(
    project_root: Path, config: Mapping[str, Any], start: str, end: str,
) -> dict[str, Any]:
    calendar_path = project_root / "data/raw/trade_calendar.csv"
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str})
    dates = sorted(set(calendar.loc[
        pd.to_numeric(calendar["is_open"], errors="coerce").eq(1), "cal_date"
    ].astype(str)))
    eligible = [date for date in dates[1:] if str(start) <= date <= str(end)]
    if not eligible or eligible[-1] != str(end):
        raise RuntimeError(f"方案甲历史检查：交易日历未覆盖收盘日{end}")
    # 首个行动日的市场门禁还需要它的前一交易日。
    first = dates[dates.index(eligible[0]) - 1]
    expected = [date for date in dates if first <= date <= str(end)]
    data_config = config.get("data", {})
    limit_start = str(config.get("cleaning", {}).get("limit_list_start_date", "20191128"))
    schemas = {
        "daily": {"trade_date", "ts_code", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"},
        "daily_basic": {"trade_date", "ts_code", "turnover_rate", "volume_ratio", "circ_mv"},
        "limit_list": {"trade_date", "ts_code", "first_time", "last_time", "open_times", "fd_amount", "limit_times"},
    }
    groups: dict[str, Any] = {}
    for kind, required in schemas.items():
        folder = project_root / str(data_config.get(kind + "_dir", "data/raw/" + kind))
        selected = [date for date in expected if kind != "limit_list" or date >= limit_start]
        problems: dict[str, str] = {}
        for date in selected:
            path = folder / f"{date}.csv"
            try:
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    missing = sorted(required - set(reader.fieldnames or []))
                    row = next(reader, None)
                if missing:
                    problems[date] = "缺少字段：" + ",".join(missing)
                elif row is None:
                    problems[date] = "没有数据行（不能把缺失当作0涨停）"
                elif row.get("trade_date") != date:
                    problems[date] = "数据日期与文件名不符"
                elif kind == "limit_list" and row.get("limit_data_quality", "full") not in {"full", "untradable_source_placeholder"}:
                    problems[date] = "不是完整涨停明细口径"
            except (OSError, UnicodeError, csv.Error) as exc:
                problems[date] = type(exc).__name__
        groups[kind] = {"path": str(folder), "expected": len(selected), "available": len(selected) - len(problems), "problems": problems}
    early_dates = [date for date in expected if date < limit_start]
    problems = {}
    for date in early_dates:
        try:
            load_historical_limit_count(project_root, date)
        except (OSError, UnicodeError, ValueError, RuntimeError, csv.Error) as exc:
            problems[date] = str(exc)
    groups["historical_limit_counts"] = {
        "path": str(project_root / "data/raw/market_limit_counts.csv"),
        "expected": len(early_dates), "available": len(early_dates) - len(problems),
        "problems": problems,
    }
    return {
        "status": "PASS" if not any(group["problems"] for group in groups.values()) else "INCOMPLETE",
        "first_signal_date": first, "last_signal_date": str(end),
        "expected_trade_days": len(expected), "groups": groups,
        "limit_list_start_date": limit_start,
        "note": "早期涨停数须有原始榜单和哈希证据；不补造早期炸板、封单或成交模型字段。",
    }


def require_history(project_root: Path, config: Mapping[str, Any], start: str, end: str) -> dict[str, Any]:
    audit = audit_history(project_root, config, start, end)
    if audit["status"] != "PASS":
        missing = [
            f"{kind}缺少或无效{len(group['problems'])}天（首日{min(group['problems'])}）"
            for kind, group in audit["groups"].items() if group["problems"]
        ]
        raise RuntimeError(
            f"方案甲全历史输入尚未就绪：{'；'.join(missing)}。"
            "新机仅采集最近3天不能重建2019年起的影子账；须补齐历史后重跑⑬，继续fail-closed不开新仓。"
        )
    return audit
