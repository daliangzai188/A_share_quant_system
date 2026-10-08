#!/usr/bin/env python3
"""行情 Git 备份清单与克隆验收：无网络、无密钥读取、无券商调用。"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = Path("config/market_data_backup.json")
LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"


def safe_path(root: Path, relative: str) -> Path:
    """清单中的路径必须指向项目内的行情目录，不能越界或跟随外部链接。"""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not relative.startswith("data/"):
        raise ValueError(f"非法行情路径：{relative}")
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"行情路径越界：{relative}")
    return result


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("rb") as stream:
        if stream.read(len(LFS_PREFIX)) == LFS_PREFIX:
            raise ValueError("只有 Git LFS 指针，请先运行 git lfs pull")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def file_record(path: Path, relative: str, required: list[str], date: str | None = None) -> dict:
    """逐日文件检查字段、日期与重复键，并以流式 SHA-256 保留原始字节。"""
    before = path.stat()
    with path.open("rb") as stream:
        if stream.read(len(LFS_PREFIX)) == LFS_PREFIX:
            raise ValueError("只有 Git LFS 指针，请先运行 git lfs pull")
    row_count = None
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = list(reader.fieldnames or [])
            missing = set(required) - set(columns)
            if missing:
                raise ValueError("缺少字段：" + ",".join(sorted(missing)))
            keys = set()
            row_count = 0
            for row in reader:
                row_count += 1
                if date:
                    if row.get("trade_date") != date or not row.get("ts_code"):
                        raise ValueError("交易日期不符或股票代码缺失")
                    key = row["trade_date"], row["ts_code"]
                    if key in keys:
                        raise ValueError("交易日期/股票代码重复")
                    keys.add(key)
            if not row_count:
                raise ValueError("没有数据行，不能把空文件当作已备份数据")
    else:
        json.loads(path.read_text(encoding="utf-8-sig"))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("校验期间文件发生变化，请等待同步完成后再试")
    return {"path": relative, "bytes": after.st_size, "sha256": digest.hexdigest(), "rows": row_count}


def audit(root: Path, start: str, end: str) -> dict:
    """备份范围由独立清单限定，不遍历账户运行状态。"""
    dt.datetime.strptime(start, "%Y%m%d")
    dt.datetime.strptime(end, "%Y%m%d")
    if start > end:
        raise ValueError("开始日期晚于结束日期")
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    calendar_path = safe_path(root, "data/raw/trade_calendar.csv")
    columns, rows = read_rows(calendar_path)
    if not {"cal_date", "is_open"}.issubset(columns):
        raise ValueError("交易日历缺少 cal_date/is_open")
    if len({row["cal_date"] for row in rows}) != len(rows):
        raise ValueError("交易日历日期重复")
    calendar_dates = sorted(row["cal_date"] for row in rows)
    if not calendar_dates or calendar_dates[0] > start or calendar_dates[-1] < end:
        raise ValueError("交易日历未覆盖请求日期范围")
    dates = sorted(row["cal_date"] for row in rows
                   if start <= row["cal_date"] <= end and row["is_open"] in {"1", "1.0", "true", "True"})
    if not dates:
        raise ValueError("请求范围没有开市日")
    files, problems, groups = {}, {}, {}

    def add(relative: str, required: list[str] | None = None, date: str | None = None) -> None:
        if relative in files or relative in problems:
            return
        try:
            files[relative] = file_record(safe_path(root, relative), relative, required or [], date)
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            problems[relative] = str(exc)

    for kind, group in policy["daily_groups"].items():
        selected = [date for date in dates if date >= group.get("start_date", start)]
        for date in selected:
            add(f"{group['path']}/{date}.csv", group["required_columns"], date)
        groups[kind] = {"expected_dates": len(selected), "valid_dates": sum(
            f"{group['path']}/{date}.csv" in files for date in selected)}
    for relative in policy["required_files"]:
        add(relative)
    for pattern in policy["optional_market_patterns"]:
        # 先验证配置模式目录，避免错误策略扫描项目外或运行账本。
        safe_path(root, pattern)
        for path in sorted(root.glob(pattern)):
            add(path.relative_to(root).as_posix())

    # 原始涨停起点晚于2019；缺少早期有来源的市场涨停计数时不能宣称换机输入完整。
    sentiment_relative = "data/research/five_year_strict/market_sentiment.csv"
    if sentiment_relative in files:
        sentiment_columns, sentiment_rows = read_rows(safe_path(root, sentiment_relative))
        seen = {}
        try:
            if not {"trade_date", "limit_up_count"}.issubset(sentiment_columns):
                raise ValueError("历史市场情绪表缺少日期或涨停数")
            for row in sentiment_rows:
                date = row["trade_date"]
                if date in seen:
                    raise ValueError("历史市场情绪表日期重复")
                value = float(row["limit_up_count"])
                if value < 0 or not value.is_integer():
                    raise ValueError("历史涨停数不是非负整数")
                seen[date] = value
            missing = [date for date in dates if date not in seen]
            if missing:
                raise ValueError(f"历史涨停计数缺少{len(missing)}天，首日{missing[0]}")
        except ValueError as exc:
            problems[sentiment_relative] = str(exc)
    return {
        "schema_version": 1, "status": "BACKUP_COMPLETE" if not problems else "INCOMPLETE",
        "start_date": start, "end_date": end, "expected_trade_days": len(dates),
        "groups": groups, "file_count": len(files),
        "total_bytes": sum(record["bytes"] for record in files.values()),
        "files": [files[key] for key in sorted(files)], "problems": problems,
        "note": "只验证行情备份；不代表数据源口径、策略认证、交易账本迁移或交易许可已通过。",
    }


def verify(root: Path) -> dict:
    """克隆后检查完整覆盖、实际 LFS 文件及清单字节哈希。"""
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    saved = json.loads(safe_path(root, policy["manifest_path"]).read_text(encoding="utf-8"))
    if saved.get("schema_version") != 1 or saved.get("status") != "BACKUP_COMPLETE":
        raise ValueError("备份清单不是完整验收结果")
    current = audit(root, saved["start_date"], saved["end_date"])
    expected = {}
    for record in saved["files"]:
        path = record["path"]
        safe_path(root, path)
        if path in expected:
            raise ValueError("备份清单路径重复")
        expected[path] = record
    actual = {record["path"]: record for record in current["files"]}
    for path in set(expected) | set(actual):
        if path not in expected:
            current["problems"][path] = "文件未登记在备份清单，请更新并提交清单"
        elif path not in actual or any(expected[path].get(key) != actual[path].get(key)
                                        for key in ("bytes", "sha256", "rows")):
            current["problems"][path] = current["problems"].get(path, "文件哈希、字节数或行数与备份清单不一致")
    current["status"] = "RESTORE_VERIFIED" if not current["problems"] else "INCOMPLETE"
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["audit", "create", "verify"])
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args(argv)
    try:
        root = args.project_root.resolve()
        policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
        if args.operation == "verify":
            result = verify(root)
        else:
            if not args.end_date:
                parser.error("audit/create 必须指定 --end-date")
            result = audit(root, args.start_date or policy["start_date"], args.end_date)
            if args.operation == "create" and result["status"] == "BACKUP_COMPLETE":
                result["created_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
                path = safe_path(root, policy["manifest_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(path)
        # 终端只展示概况和问题，完整文件清单写入受控的行情备份清单。
        summary = {key: value for key, value in result.items() if key != "files"}
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        return 0 if result["status"] in {"BACKUP_COMPLETE", "RESTORE_VERIFIED"} else 1
    except (OSError, UnicodeError, ValueError, KeyError, csv.Error) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
