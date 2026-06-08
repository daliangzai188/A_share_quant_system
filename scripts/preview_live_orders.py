"""
A+B+C 计划单转 QMT 实盘预览。

文件作用：
1. 读取每日操作台 planned_orders.csv。
2. 连接 QMT 获取账户资金、当日委托和实时行情。
3. 检查涨停买不到、跌停卖不出、资金不足、重复委托、单笔金额上限。
4. 输出实盘预览文件。
5. 不提交真实委托。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live_order_gateway import LiveOrderGateway


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="预览 A+B+C 计划单是否满足 QMT 实盘执行条件。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument(
        "--planned-orders",
        default="latest",
        help="planned_orders.csv 路径；使用 latest 自动选择最新文件。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/qmt_live_order",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gateway = LiveOrderGateway(args.config)
    paths = gateway.preview(args.planned_orders, args.output_prefix)
    print("QMT 实盘计划单预览完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()

