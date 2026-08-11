from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config
from src.secret_config import ensure_tushare_token
from src.utils.time_utils import yesterday_beijing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一次输入 Token，连续采集日线、每日基本面和涨停池数据。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的本地 CSV。")
    parser.add_argument("--no-daily-basic", action="store_true", help="只采集日线和涨停池，不采集 daily_basic。")
    parser.add_argument("--skip-daily", action="store_true", help="跳过日线和 daily_basic，只采集涨停池。")
    parser.add_argument("--skip-limit", action="store_true", help="跳过涨停池，只采集日线和 daily_basic。")
    parser.add_argument(
        "--require-end-date-limit",
        action="store_true",
        help="要求 end-date 的涨停池 CSV 必须存在且有数据行；守护进程收盘流水线使用。",
    )
    return parser.parse_args()


def yesterday_yyyymmdd() -> str:
    return yesterday_beijing().strftime("%Y%m%d")


def csv_has_data_row(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open(encoding="utf-8-sig") as f:
            f.readline()
            return bool(f.readline().strip())
    except OSError:
        return False


def csv_has_full_limit_data(path: Path) -> bool:
    if not csv_has_data_row(path):
        return False
    try:
        import pandas as pd

        header = pd.read_csv(path, nrows=0).columns.tolist()
        if "limit_data_quality" not in header:
            return True
        sample = pd.read_csv(path, usecols=["limit_data_quality"], nrows=20)
        return bool(sample["limit_data_quality"].fillna("full").astype(str).eq("full").all())
    except Exception:
        return False


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    start_date = args.start_date or config["data"]["start_date"]
    end_date = args.end_date or yesterday_yyyymmdd()
    ensure_tushare_token(config)

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    data_source = TushareDataSource(config_path=args.config)
    collector = DataCollector(data_source=data_source, config_path=args.config)

    if not args.skip_daily:
        collector.collect_daily_data(
            start_date=start_date,
            end_date=end_date,
            overwrite=args.overwrite,
            include_daily_basic=not args.no_daily_basic,
        )

    if not args.skip_limit:
        collector.collect_limit_data(start_date=start_date, end_date=end_date, overwrite=args.overwrite)
        if args.require_end_date_limit:
            limit_path = collector.limit_list_dir / f"{end_date}.csv"
            if not csv_has_full_limit_data(limit_path):
                raise RuntimeError(
                    f"{end_date} 未采集到 limit_list_d 完整涨停池数据: {limit_path}。"
                    "收盘流水线停止，不使用 stk_limit 基础口径，也不使用旧信号。"
                )


if __name__ == "__main__":
    main()
