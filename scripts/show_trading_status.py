"""只读显示 A_System、QMT、账户资金和当日交易状态。

本工具不会下单、撤单或发送通知。所有账户输出均脱敏：姓名显示 ***，
资金账号只保留末两位。默认把同一份脱敏结果保存到
reports/live_trade/trading_status_latest.json。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.account_privacy import mask_account_id, public_account_data
from src.qmt_adapter import QMTBrokerAdapter, first_present, to_float, to_int
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读查看程序、QMT、资金、持仓、委托和成交状态。")
    parser.add_argument(
        "--output",
        default="reports/live_trade/trading_status_latest.json",
        help="脱敏 JSON 输出路径。",
    )
    parser.add_argument("--no-broker", action="store_true", help="只看本地程序状态，不连接 QMT。")
    return parser.parse_args()


def pid_running(pid_file: Path) -> tuple[bool, int]:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        if pid <= 0:
            return False, 0
        if os.name == "nt":
            import ctypes

            query = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(query, False, pid)
            if not handle:
                return False, pid
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True, pid
                return int(exit_code.value) == still_active, pid
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True, pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False, 0


def age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def local_runtime_status(config: dict[str, Any]) -> dict[str, Any]:
    daemon_running, daemon_pid = pid_running(PROJECT_ROOT / ".daemon_pid")
    keeper_running, keeper_pid = pid_running(PROJECT_ROOT / ".keeper_pid")
    manual_stop_file = PROJECT_ROOT / ".manual_stop.json"
    manual_stop = manual_stop_file.exists()
    heartbeat = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
    broker_health = PROJECT_ROOT / "logs" / "broker_health.json"
    notify_cfg = config.get("notify", {}) or {}
    bark_configured = bool((os.getenv("BARK_URL", "") or "").strip())
    notifications_disabled = (os.getenv("A_SYSTEM_DISABLE_NOTIFICATIONS", "") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return {
        "manual_stop": manual_stop,
        "daemon_running": daemon_running,
        "daemon_pid": daemon_pid if daemon_running else None,
        "keeper_running": keeper_running,
        "keeper_pid": keeper_pid if keeper_running else None,
        "daemon_heartbeat_age_sec": age_seconds(heartbeat),
        "broker_health_age_sec": age_seconds(broker_health),
        "log_file": str(PROJECT_ROOT / "logs" / "trading_daemon.log"),
        "notification": {
            "configured": bool(notify_cfg.get("enabled", False)) and bark_configured,
            "channel": str(notify_cfg.get("channel", "bark")),
            "environment_disabled": notifications_disabled,
            "active_now": (
                daemon_running
                and bool(notify_cfg.get("enabled", False))
                and bark_configured
                and not notifications_disabled
            ),
        },
    }


def safe_order(row: dict[str, Any]) -> dict[str, Any]:
    side = str(first_present(row, ["side", "order_side", "direction"], ""))
    return {
        "time": str(first_present(row, ["order_time", "m_strInsertTime", "time"], "")),
        "code": str(first_present(row, ["ts_code", "stock_code", "m_strInstrumentID"], "")),
        "name": str(first_present(row, ["stock_name", "name", "m_strInstrumentName"], "")),
        "side": side,
        "quantity": to_int(first_present(row, ["order_volume", "quantity", "m_nVolumeTotalOriginal"], 0)),
        "filled_quantity": to_int(first_present(row, ["traded_volume", "m_nVolumeTraded"], 0)),
        "price": to_float(first_present(row, ["price", "order_price", "m_dLimitPrice"], 0)),
        "status": str(first_present(row, ["status_text", "order_status", "m_nOrderStatus"], "")),
        "order_id": str(first_present(row, ["order_id", "order_sysid", "m_strOrderSysID"], "")),
        "strategy": str(first_present(row, ["strategy_name", "m_strSource"], "")),
        "remark": str(first_present(row, ["order_remark", "remark", "m_strRemark"], "")),
    }


def safe_trade(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": str(first_present(row, ["traded_time", "m_strTradeTime", "time"], "")),
        "code": str(first_present(row, ["ts_code", "stock_code", "m_strInstrumentID"], "")),
        "name": str(first_present(row, ["stock_name", "name", "m_strInstrumentName"], "")),
        "side": str(first_present(row, ["side", "order_side", "direction"], "")),
        "quantity": to_int(first_present(row, ["traded_volume", "quantity", "m_nVolume"], 0)),
        "price": to_float(first_present(row, ["traded_price", "price", "m_dPrice"], 0)),
        "trade_id": str(first_present(row, ["traded_id", "trade_id", "m_strTradeID"], "")),
        "order_id": str(first_present(row, ["order_id", "order_sysid", "m_strOrderSysID"], "")),
        "strategy": str(first_present(row, ["strategy_name", "m_strSource"], "")),
        "remark": str(first_present(row, ["order_remark", "remark", "m_strRemark"], "")),
    }


def broker_status(config: dict[str, Any]) -> dict[str, Any]:
    adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
    try:
        adapter.connect()
        account = adapter.query_account()
        positions = adapter.query_positions()
        orders = adapter.query_orders()
        trades = adapter.query_trades()
        server = dict(getattr(adapter, "server_info", {}) or {})
        mode = str(server.get("mode", "unknown"))
        return {
            "connected": True,
            "transport": "qmt_inner" if adapter.__class__.__name__ == "QMTInnerBrokerAdapter" else "miniQMT",
            "mode": mode,
            "financial_calls": False,
            "account": {
                "account_name": "***",
                "account_id": mask_account_id(account.account_id),
                "available_cash": account.available_cash,
                "frozen_cash": account.frozen_cash,
                "market_value": account.market_value,
                "total_asset": account.total_asset,
            },
            "positions": public_account_data([asdict(row) for row in positions]),
            "orders": [safe_order(dict(row)) for row in orders],
            "trades": [safe_trade(dict(row)) for row in trades],
        }
    finally:
        adapter.disconnect()


def money(value: Any) -> str:
    return f"{to_float(value):,.2f} 元（{to_float(value) / 10000:,.2f} 万）"


def print_report(report: dict[str, Any]) -> None:
    runtime = report["runtime"]
    broker = report.get("broker") or {}
    print("=" * 72)
    print("A_System / 国金 QMT 只读状态总览")
    print("检查时间：" + report["checked_at"])
    print("=" * 72)
    print("程序状态：" + ("运行中" if runtime["daemon_running"] else "已停止"))
    print("守护程序：" + ("运行中" if runtime["keeper_running"] else "已停止"))
    print("人工停机：" + ("是" if runtime["manual_stop"] else "否"))
    notice = runtime["notification"]
    print(
        "通知状态："
        + ("运行中" if notice["active_now"] else "当前不发送")
        + ("（配置完整）" if notice["configured"] else "（配置不完整或关闭）")
    )
    if report.get("broker_error"):
        print("QMT 连接：失败 - " + report["broker_error"])
        return
    if not broker:
        print("QMT 连接：本次按 --no-broker 跳过")
        return
    print("QMT 连接：正常")
    print(f"执行接口：{broker['transport']} / {broker['mode']}")
    account = broker["account"]
    print(f"账号名称：{account['account_name']}")
    print(f"资金账号：{account['account_id']}")
    print("可用资金：" + money(account["available_cash"]))
    print("冻结资金：" + money(account["frozen_cash"]))
    print("持仓市值：" + money(account["market_value"]))
    print("总资产：  " + money(account["total_asset"]))
    print(f"持仓数量：{len(broker['positions'])}；今日委托：{len(broker['orders'])}；今日成交：{len(broker['trades'])}")
    if broker["positions"]:
        print("\n当前持仓：")
        for row in broker["positions"]:
            print(
                f"  {row.get('ts_code', '')} {row.get('name', '')} "
                f"持有{row.get('volume', 0)}股 可用{row.get('can_use_volume', 0)}股 "
                f"成本{to_float(row.get('cost_price')):.3f} 市值{to_float(row.get('market_value')):,.2f}元"
            )
    if broker["orders"]:
        print("\n今日最近委托：")
        for row in broker["orders"][-10:]:
            print(
                f"  {row['time']} {row['code']} {row['name']} {row['side']} "
                f"{row['quantity']}股/已成{row['filled_quantity']}股 @{row['price']:.3f} 状态{row['status']}"
            )
    if broker["trades"]:
        print("\n今日最近成交：")
        for row in broker["trades"][-10:]:
            print(
                f"  {row['time']} {row['code']} {row['name']} {row['side']} "
                f"{row['quantity']}股 @{row['price']:.3f}"
            )
    print("\n完整运行日志：" + runtime["log_file"])
    print("脱敏状态文件：" + report["output"])
    print("说明：本工具全程只读，没有下单、撤单或发送通知。")


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    report: dict[str, Any] = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": local_runtime_status(config),
        "broker": None,
        "broker_error": "",
        "output": str(output),
    }
    if not args.no_broker:
        try:
            report["broker"] = broker_status(config)
        except Exception as exc:  # noqa: BLE001
            report["broker_error"] = f"{type(exc).__name__}: {exc}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    return 1 if report["broker_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
