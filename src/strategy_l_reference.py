"""L 首选的只读参照，给 QMT 外的“实盘逐笔对账”用（ACE+M+L，2026-11-26）。

L 的计划在 QMT 外由 ``scripts/run_strategy_l_signal.py`` 算出，历史首选（每个买入日按规则排好序的前 3 个代码）
存在 ``reports/strategy_l/l_picks_cache.json``；这里只读它，再从原始日线取买入日的开盘价和涨停价，
判断“首选开盘有没有封涨停”。本模块只读不写，不参与实盘信号生成。

QMT 里的收盘流水线⑬读不到这个缓存（2026-10-23 起 QMT 内不能读写普通文件），所以只有 QMT 外的每日检查用它。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.ace_m_replay import RawPrices
from src.market_rules import limit_up_price, price_limit_pct
from src.strategy_l import L_VERSION, NameHistory


def _read_csv(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str)


def load_name_history(stock_basic_path: Path | str, namechange_path: Path | str) -> tuple[NameHistory, dict[str, str]]:
    """逐日名称（判断当时是不是 ST，用 Tushare 名称变更记录）和上市日；晚间信号脚本和三级警告回放共用。"""

    basic = _read_csv(Path(stock_basic_path))
    current = dict(zip(basic["ts_code"], basic["name"])) if not basic.empty else {}
    list_dates = dict(zip(basic["ts_code"], basic["list_date"])) if not basic.empty else {}
    changes = _read_csv(Path(namechange_path))
    return NameHistory(changes if not changes.empty else None, current=current), list_dates


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


class LReference:
    """``reference(buy_date)`` 返回 {"status": "OK"/"ERROR", "error", "first": {"ts_code", "buy_open", "buy_limit_up"}或None}，
    从不抛异常；``buy_days(start, end)`` 列出缓存里在这段时间的全部买入日（含当天没有 L 计划的日子）。"""

    def __init__(self, project_root: Path | str, raw_dir: Path | str | None = None) -> None:
        root = Path(project_root)
        self.cache_path = root / "reports" / "strategy_l" / "l_picks_cache.json"
        self.prices = RawPrices(Path(raw_dir) if raw_dir is not None else root / "data" / "raw")
        self._picks: dict[str, list[str]] | None = None
        self._error = ""

    def _load(self) -> None:
        if self._picks is not None or self._error:
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._error = "L首选缓存不存在（%s）" % self.cache_path
            return
        except (OSError, ValueError) as exc:
            self._error = "L首选缓存读不了：%s" % exc
            return
        if str(payload.get("l_version", "")) != L_VERSION:
            self._error = "L首选缓存版本%s与正式版本%s不一致" % (payload.get("l_version", ""), L_VERSION)
            return
        self._picks = {str(day): [str(c) for c in (codes or [])] for day, codes in dict(payload.get("picks") or {}).items()}

    def all_picks(self) -> dict[str, list[str]]:
        """全部历史首选 {买入日: [按规则排好序的前3个代码]}；缓存缺失或版本不符时抛错（三级警告记“没算成”）。"""
        self._load()
        if self._error:
            raise RuntimeError(self._error)
        return dict(self._picks or {})

    def buy_days(self, start: str, end: str) -> list[str]:
        self._load()
        return sorted(day for day in (self._picks or {}) if str(start) <= day <= str(end))

    def __call__(self, buy_date: str) -> dict[str, Any]:
        try:
            return self._build(str(buy_date))
        except Exception as exc:  # 参照出错只让这一天“没有被检查”，不能让整个对账中断
            return {"status": "ERROR", "error": "L首选参照出错：%s" % exc, "first": None}

    def _build(self, day: str) -> dict[str, Any]:
        self._load()
        if self._error:
            return {"status": "ERROR", "error": self._error, "first": None}
        assert self._picks is not None
        if day not in self._picks:
            return {"status": "ERROR", "error": "L首选缓存里没有%s（晚间信号没算这一天）" % day, "first": None}
        codes = self._picks[day]
        if not codes:
            return {"status": "OK", "error": "", "first": None}
        code = codes[0]
        bar = self.prices.bar(day, code) or {}
        pre_close = _positive(bar.get("pre_close"))
        return {"status": "OK", "error": "", "first": {
            "ts_code": code,
            "buy_open": _positive(bar.get("open")),
            "buy_limit_up": limit_up_price(pre_close, price_limit_pct(code)) if pre_close is not None else None,
        }}
