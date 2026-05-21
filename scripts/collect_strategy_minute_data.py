from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_source import TushareDataSource
from src.minute_data_collector import StrategyMinuteDataCollector
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按当前策略信号采集所需分钟 K 线。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--start-date", default="", help="信号开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", default="", help="信号结束日期，格式 YYYYMMDD。")
    parser.add_argument("--limit", type=int, default=0, help="限制采集目标数量，用于先小样本测试。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的原始分钟 K 文件。")
    parser.add_argument("--no-import", action="store_true", help="只保存原始分钟 K，不合并到标准 minute_bars.csv。")
    parser.add_argument("--dry-run-targets", action="store_true", help="只生成采集目标预览，不请求 Tushare。")
    return parser.parse_args()


def ensure_tushare_token(config: dict) -> None:
    token_env = config.get("data_source", {}).get("token_env", "TUSHARE_TOKEN")
    if os.getenv(token_env):
        return
    token = getpass.getpass("请输入 Tushare Pro Token（不会显示，且不会保存到本地）: ").strip()
    if not token:
        raise RuntimeError("未输入 Tushare Pro Token。")
    os.environ[token_env] = token


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    if args.dry_run_targets:
        collector = StrategyMinuteDataCollector(data_source=None, config_path=args.config)
        output_path = collector.write_target_preview(
            start_date=args.start_date or None,
            end_date=args.end_date or None,
            limit=args.limit or None,
        )
        print(f"分钟 K 采集目标预览已生成: {output_path}")
        return

    ensure_tushare_token(config)

    data_source = TushareDataSource(config_path=args.config)
    collector = StrategyMinuteDataCollector(data_source=data_source, config_path=args.config)
    outputs = collector.collect_for_strategy(
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        limit=args.limit or None,
        overwrite=args.overwrite,
        import_after_collect=not args.no_import,
    )

    print("策略分钟 K 采集完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
