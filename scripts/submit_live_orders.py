"""
提交 QMT 真实委托。

文件作用：
1. 读取 preview_live_orders.py 生成的预览文件。
2. 只提交 validation_status=PASS 的订单。
3. 必须同时满足 config/config.json 的实盘开关和命令行确认文本。
4. 默认配置下无法真实下单。
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
    parser = argparse.ArgumentParser(description="提交 QMT 真实委托。默认配置会拒绝执行。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument("--preview", required=True, help="preview_live_orders.py 生成的 preview.csv。")
    parser.add_argument("--confirm", required=True, help="真实下单确认文本。")
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/qmt_live_order",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gateway = LiveOrderGateway(args.config)
    paths = gateway.submit(args.preview, args.confirm, args.output_prefix)
    print("QMT 真实委托提交完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()

