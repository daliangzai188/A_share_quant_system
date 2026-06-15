from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_cleaner import DataCleaner
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清洗并合并本地采集的 A股日线、每日指标和涨停池数据。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。不填则清洗全部已采集日期。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。不填则清洗全部已采集日期。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--no-overwrite", action="store_true", help="如果输出文件已存在则报错，不覆盖。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    cleaner = DataCleaner(config_path=args.config)
    outputs = cleaner.clean(
        start_date=args.start_date,
        end_date=args.end_date,
        overwrite=not args.no_overwrite,
    )

    print("数据清洗完成，输出文件：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
