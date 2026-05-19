from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一次输入 Token，连续采集日线、每日基本面和涨停池数据。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的本地 CSV。")
    parser.add_argument("--no-daily-basic", action="store_true", help="只采集日线和涨停池，不采集 daily_basic。")
    parser.add_argument("--skip-daily", action="store_true", help="跳过日线和 daily_basic，只采集涨停池。")
    parser.add_argument("--skip-limit", action="store_true", help="跳过涨停池，只采集日线和 daily_basic。")
    return parser.parse_args()


def ensure_tushare_token(config: dict) -> None:
    token_env = config.get("data_source", {}).get("token_env", "TUSHARE_TOKEN")
    if os.getenv(token_env):
        return
    token = getpass.getpass("请输入 Tushare Pro Token（不会显示，且不会保存到本地）: ").strip()
    if not token:
        raise RuntimeError("Tushare Token 不能为空。")
    os.environ[token_env] = token


def yesterday_yyyymmdd() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


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


if __name__ == "__main__":
    main()
