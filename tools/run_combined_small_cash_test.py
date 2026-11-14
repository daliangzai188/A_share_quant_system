"""
A+B+C+D 组合小资金 100 股测试。

流程：
1. 生成组合状态机计划。
2. 将组合计划单数量强制裁剪到配置的 small_cash_test_max_shares。
3. 连接 QMT 做实盘预览。
4. 只提交预览 PASS 且仍满足小资金限制的订单。

不需要命令行确认文本，但必须在 config/config.json 中显式开启：
- trade_mode=live
- broker_adapter_enabled=true
- qmt_enabled=true
- broker.enabled=true
- live_trade.enabled=true
- live_trade.small_cash_test_enabled=true
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.combined_live_engine import CombinedLiveEngine
from src.live_order_gateway import LiveOrderGateway


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 A+B+C+D 组合 100 股小资金测试。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/combined/combined_small_cash_test",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def cap_orders_for_small_test(orders: pd.DataFrame, max_shares: int) -> pd.DataFrame:
    if orders.empty:
        return orders.copy()
    capped = orders.copy()
    if "round_lot_shares" in capped.columns:
        capped["round_lot_shares"] = pd.to_numeric(capped["round_lot_shares"], errors="coerce").fillna(0).astype(int)
        capped["round_lot_shares"] = capped["round_lot_shares"].clip(lower=0, upper=max_shares)
    if "estimated_shares" in capped.columns:
        capped["estimated_shares"] = pd.to_numeric(capped["estimated_shares"], errors="coerce").fillna(0).astype(int)
        capped["estimated_shares"] = capped["estimated_shares"].clip(lower=0, upper=max_shares)
    if "planned_amount_by_equity" in capped.columns:
        capped["planned_amount_by_equity"] = 0.0
    capped["small_cash_test_forced_shares"] = max_shares
    return capped


def main() -> None:
    args = parse_args()
    engine = CombinedLiveEngine(args.config)
    plan_paths = engine.write_plan()
    gateway = LiveOrderGateway(args.config)
    max_shares = int(gateway.live_config.get("small_cash_test_max_shares", 100))

    orders = pd.read_csv(plan_paths["planned_orders"], low_memory=False)
    capped_orders = cap_orders_for_small_test(orders, max_shares=max_shares)

    output_prefix = Path(args.output_prefix)
    if not output_prefix.is_absolute():
        output_prefix = PROJECT_ROOT / output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    capped_path = output_prefix.with_name(output_prefix.name + "_orders.csv")
    capped_orders.to_csv(capped_path, index=False, encoding="utf-8-sig")

    preview_paths = gateway.preview(capped_path, output_prefix)
    submit_paths = gateway.submit_small_cash_test(preview_paths["preview"], output_prefix)

    print("A+B+C+D 组合小资金测试流程完成：")
    print(f"- combined_state: {plan_paths['state']}")
    print(f"- combined_decisions: {plan_paths['decisions']}")
    print(f"- small_test_orders: {capped_path}")
    print(f"- preview: {preview_paths['preview']}")
    print(f"- submitted_orders: {submit_paths['orders']}")


if __name__ == "__main__":
    main()
