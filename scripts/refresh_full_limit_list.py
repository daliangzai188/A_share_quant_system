from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config
from src.utils.logger import setup_logger
from src.secret_config import ensure_tushare_token


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只尝试恢复 Tushare limit_list_d 完整涨停池。")
    parser.add_argument("--trade-date", required=True, help="交易日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
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
    ensure_tushare_token(config)

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    source = TushareDataSource(config_path=args.config)
    collector = DataCollector(data_source=source, config_path=args.config)
    fields = config.get("collection", {}).get("limit_list_fields")
    limit_type = config.get("collection", {}).get("limit_type", "U")
    data, fetch_method = collector.fetch_limit_list_d_full(
        trade_date=args.trade_date,
        limit_type=limit_type,
        fields=fields,
    )
    if data.empty:
        raise RuntimeError(f"Tushare limit_list_d 仍未返回 {args.trade_date} 完整涨停池数据。")

    data["limit_data_source"] = "limit_list_d"
    data["limit_data_quality"] = "full"
    data["strategy_compatible"] = True
    output_path = collector.limit_list_dir / f"{args.trade_date}.csv"
    collector._save_dataframe(data, output_path)
    print(f"完整涨停池已恢复: {output_path}, 行数: {len(data)}, fetch_method={fetch_method}")


if __name__ == "__main__":
    main()
