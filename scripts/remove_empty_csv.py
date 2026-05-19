from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除指定日期范围内只有表头、没有数据行的本地 CSV。")
    parser.add_argument("--start-date", required=True, help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", required=True, help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--dry-run", action="store_true", help="只打印将删除的文件，不实际删除。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    data_config = config["data"]
    directories = [
        PROJECT_ROOT / data_config.get("daily_dir", "data/raw/daily"),
        PROJECT_ROOT / data_config.get("daily_basic_dir", "data/raw/daily_basic"),
        PROJECT_ROOT / data_config.get("limit_list_dir", "data/raw/limit_list"),
    ]

    targets = []
    for directory in directories:
        for path in sorted(directory.glob("*.csv")):
            trade_date = path.stem
            if args.start_date <= trade_date <= args.end_date and is_empty_csv(path):
                targets.append(path)

    if not targets:
        print("没有发现空 CSV。")
        return

    for path in targets:
        if args.dry_run:
            print(f"[DRY-RUN] 将删除: {path}")
        else:
            path.unlink()
            print(f"已删除: {path}")


def is_empty_csv(path: Path) -> bool:
    try:
        data = pd.read_csv(path, nrows=1)
    except pd.errors.EmptyDataError:
        return True
    return data.empty


if __name__ == "__main__":
    main()
