from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集 A股涨停池数据 limit_list_d。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的本地 CSV。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    start_date = args.start_date or config["data"]["start_date"]
    end_date = args.end_date or config["data"]["end_date"]

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    data_source = TushareDataSource(config_path=args.config)
    collector = DataCollector(data_source=data_source, config_path=args.config)
    collector.collect_limit_data(start_date=start_date, end_date=end_date, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
