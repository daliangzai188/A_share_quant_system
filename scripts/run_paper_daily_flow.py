"""
运行单日模拟盘流程。

文件作用：
1. 按指定 signal_date 生成每日模拟盘候选。
2. 生成计划委托、历史模拟成交更新、持仓更新和资金更新。
3. 输出单日流程报告。

本脚本只读写本地文件，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paper_daily_flow import PaperDailyFlowRunner
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行单日模拟盘流程。")
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
    parser.add_argument(
        "--signal-date",
        default=None,
        help="信号日期，格式 YYYYMMDD。不传则使用当前策略过滤后有候选的最新日期。",
    )
    parser.add_argument("--top-n", type=int, default=None, help="候选输出数量，不传则读取配置。")
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

    outputs = PaperDailyFlowRunner(strategy_config_path=args.strategy_config).run(
        signal_date=args.signal_date,
        top_n=args.top_n,
    )
    print("单日模拟盘流程完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
