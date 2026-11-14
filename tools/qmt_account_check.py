"""
QMT / miniQMT 账户只读连接检查。

文件作用：
1. 连接 QMT 客户端。
2. 查询资金、持仓、当日委托、当日成交。
3. 保存本地报告。
4. 不提交任何真实委托。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live_order_gateway import LiveOrderGateway


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QMT / miniQMT 账户只读连接检查。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/qmt_account_check",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gateway = LiveOrderGateway(args.config)
    paths = gateway.account_check(args.output_prefix)
    print("QMT 账户只读检查完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()

