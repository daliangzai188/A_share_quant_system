"""
运行多日模拟盘批量流程。

文件作用：
1. 输入日期区间，批量运行每日模拟盘流程。
2. 输出每日状态、候选、计划委托、成交更新、持仓更新、资金曲线和风险事件。
3. 用于模拟盘连续运行验证。

本脚本只读写本地文件，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paper_batch_flow import PaperBatchFlowRunner
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行多日模拟盘批量流程。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument(
        "--runtime-config",
        default="config/config.json",
        help="运行时通用配置文件路径，仅用于日志配置。",
    )
    parser.add_argument("--start-date", default=None, help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", default=None, help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--top-n", type=int, default=None, help="每日候选输出数量，不传则读取配置。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime_config = load_json_config(args.runtime_config)
    logging_config = runtime_config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    outputs = PaperBatchFlowRunner(strategy_config_path=args.strategy_config).run(
        start_date=args.start_date,
        end_date=args.end_date,
        top_n=args.top_n,
    )
    print("多日模拟盘批量流程完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
