"""
本地模拟实盘预览。

文件作用：
1. 不连接 QMT。
2. 读取 A+B+C planned_orders.csv。
3. 用计划单参考价构造模拟行情。
4. 调用 LiveOrderGateway 的实盘风控预览逻辑。
5. 验证计划单到实盘预览的本地流程是否可运行。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.broker_adapter import QuoteSnapshot
from src.live_order_gateway import LiveOrderGateway
from src.qmt_adapter import tushare_to_qmt_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地模拟 A+B+C 实盘预览，不连接 QMT。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument("--planned-orders", default="latest", help="planned_orders.csv 路径，或 latest。")
    parser.add_argument("--available-cash", type=float, default=50000.0, help="模拟可用资金。")
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/mock_qmt_live_order",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def build_mock_quotes(planned_orders: pd.DataFrame) -> dict[str, QuoteSnapshot]:
    quotes: dict[str, QuoteSnapshot] = {}
    for _, row in planned_orders.iterrows():
        ts_code = str(row.get("ts_code", "")).strip().upper()
        if not ts_code:
            continue
        reference_price = pd.to_numeric(row.get("reference_price", 0.0), errors="coerce")
        price = 0.0 if pd.isna(reference_price) else float(reference_price)
        broker_code = tushare_to_qmt_code(ts_code)
        quotes[ts_code] = QuoteSnapshot(
            ts_code=ts_code,
            broker_code=broker_code,
            last_price=price,
            open_price=price,
            high_price=price,
            low_price=price,
            pre_close=price / 1.02 if price > 0 else 0.0,
            upper_limit=price * 1.1 if price > 0 else 0.0,
            lower_limit=price * 0.9 if price > 0 else 0.0,
            bid_prices=[price] * 5,
            ask_prices=[price] * 5,
            bid_volumes=[10000] * 5,
            ask_volumes=[10000] * 5,
            suspended=False,
            raw={"source": "mock_live_order_preview"},
        )
    return quotes


def main() -> None:
    args = parse_args()
    gateway = LiveOrderGateway(args.config)
    planned_path, planned_orders = gateway.load_planned_orders(args.planned_orders)
    quote_map = build_mock_quotes(planned_orders)
    preview = gateway.validate_planned_orders(
        planned_orders=planned_orders,
        account_cash=args.available_cash,
        open_orders=[],
        quote_map=quote_map,
    )

    output_prefix = Path(args.output_prefix)
    if not output_prefix.is_absolute():
        output_prefix = PROJECT_ROOT / output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    preview_path = output_prefix.with_name(output_prefix.name + "_preview.csv")
    preview.to_csv(preview_path, index=False, encoding="utf-8-sig")

    print("本地模拟实盘预览完成：")
    print(f"- source_planned_orders: {planned_path}")
    print(f"- preview: {preview_path}")
    print(preview.to_string(index=False))


if __name__ == "__main__":
    main()

