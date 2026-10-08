"""Verified early market breadth; no reconstructed fill-model fields."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class HistoricalLimitCount:
    count: int
    codes: tuple[str, ...]
    source: str = "kpl_list"


@lru_cache(maxsize=8)
def _count_table(path: str, modified: int, size: int) -> dict[str, dict]:
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            date = row.get("trade_date", "")
            if date in result:
                raise RuntimeError("早期涨停统计日期重复：" + date)
            result[date] = row
    return result


def load_historical_limit_count(project_root: Path, date: str) -> HistoricalLimitCount:
    if len(date) != 8 or not date.isdigit():
        raise ValueError("历史涨停数日期必须为YYYYMMDD")
    table_path = project_root / "data/raw/market_limit_counts.csv"
    if not table_path.exists():
        raise RuntimeError("早期涨停数来源文件缺失；请重新采集开盘啦历史榜单，不可按0处理")
    stat = table_path.stat()
    row = _count_table(str(table_path), stat.st_mtime_ns, stat.st_size).get(date)
    expected_relative = f"data/raw/kpl_limit_list/{date}.csv"
    if not row or row.get("source") != "kpl_list" or row.get("scope") != "SH_SZ_NON_ST" or row.get("raw_file") != expected_relative:
        raise RuntimeError("早期涨停数来源或统计范围不完整：" + date)
    path = project_root / expected_relative
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row.get("raw_sha256"):
        raise RuntimeError("早期涨停榜单缺失或哈希不符：" + date)
    seen, codes, source_rows = set(), [], 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for item in csv.DictReader(stream):
            code, name = item.get("ts_code", ""), item.get("name", "")
            if not code or not name or item.get("trade_date") != date or item.get("tag") != "涨停" or code in seen:
                raise RuntimeError("早期涨停榜单日期、类别或主键不正确：" + date)
            seen.add(code)
            source_rows += 1
            if code.endswith((".SH", ".SZ")) and "ST" not in name.upper():
                codes.append(code)
    if not source_rows or source_rows != int(row.get("source_row_count", -1)) or len(codes) != int(row.get("limit_up_count", -1)):
        raise RuntimeError("早期涨停数与原始榜单不一致：" + date)
    return HistoricalLimitCount(len(codes), tuple(codes))
