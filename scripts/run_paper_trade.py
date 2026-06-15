"""
运行本地模拟盘账本。

文件作用：
1. 读取 config/strategy_config.json 中的当前候选策略。
2. 调用 src.paper_trader.PaperTradeSimulator。
3. 生成信号、委托、成交、持仓、资金曲线、风险事件和汇总报告。

本脚本只做本地模拟盘，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paper_trader import PaperTradeSimulator
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行本地模拟盘账本。")
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

    outputs = PaperTradeSimulator(strategy_config_path=args.strategy_config).run()
    print("本地模拟盘账本生成完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
