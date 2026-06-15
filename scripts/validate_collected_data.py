from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_validator import DataValidator
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验本地采集的日线、每日基本面和涨停池 CSV 数据质量。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。不填则校验所有已采集日期。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。不填则校验所有已采集日期。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--output-dir", default="reports", help="报告输出目录。")
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

    validator = DataValidator(config_path=args.config)
    reports = validator.validate(
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
    )

    print("数据质量检查完成，报告文件：")
    for name, path in reports.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
