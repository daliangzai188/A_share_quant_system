#!/usr/bin/env python3
"""断点续传采集Tushare复权因子。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.secret_config import ensure_tushare_token  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402
from src.utils.time_utils import yesterday_beijing  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集A股复权因子")
    parser.add_argument("--start-date", help="开始日期，格式YYYYMMDD")
    parser.add_argument("--end-date", help="结束日期，格式YYYYMMDD")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有完整CSV")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    start_date = args.start_date or config["data"]["start_date"]
    end_date = args.end_date or yesterday_beijing().strftime("%Y%m%d")
    ensure_tushare_token(config)

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    source = TushareDataSource(config_path=args.config)
    collector = DataCollector(data_source=source, config_path=args.config)
    stats = collector.collect_adj_factor_data(
        start_date=start_date,
        end_date=end_date,
        overwrite=args.overwrite,
    )
    print(stats)


if __name__ == "__main__":
    main()
