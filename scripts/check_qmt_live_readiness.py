"""
QMT / miniQMT 实盘接入准备度检查。

文件作用：
1. 检查 config/config.json 的券商和实盘开关。
2. 检查 .env 是否配置 QMT 必要变量，但不打印真实账号。
3. 检查 xtquant 是否能被当前 Python 环境导入。
4. 检查 QMT_PATH 是否存在。
5. 检查是否已经有 A+B+C planned_orders.csv 和 live preview。
6. 输出本地 CSV/Markdown 报告，不连接券商、不下单。
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config


PLACEHOLDER_MARKERS = {"", "your_qmt_account_id_here", "/path/to/your/qmt/userdata_mini"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 QMT / miniQMT 实盘接入准备度。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    parser.add_argument(
        "--output-prefix",
        default="reports/live_trade/qmt_live_readiness",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def latest_file(pattern: str) -> Path | None:
    files = [Path(path) for path in glob.glob(str(PROJECT_ROOT / pattern))]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def status_row(item: str, status: str, detail: str, blocking: bool) -> dict[str, Any]:
    return {
        "item": item,
        "status": status,
        "blocking": blocking,
        "detail": detail,
    }


def check_env_value(env_name: str) -> tuple[str, str, bool]:
    value = os.getenv(env_name, "").strip()
    if value in PLACEHOLDER_MARKERS:
        return "FAIL", f"{env_name} 未配置或仍是占位值。", True
    return "PASS", f"{env_name} 已配置，未打印真实值。", False


def build_report(config: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    broker_config = config.get("broker", {})
    live_config = config.get("live_trade", {})

    rows.append(
        status_row(
            "trade_mode",
            "PASS" if str(config.get("trade_mode", "")).lower() in {"paper", "simulation", "dry_run", "research"} else "WARN",
            f"当前 trade_mode={config.get('trade_mode')}; 未正式测试前建议保持 paper。",
            False,
        )
    )
    rows.append(
        status_row(
            "broker_adapter_enabled",
            "PASS" if bool(config.get("broker_adapter_enabled", False)) else "WARN",
            f"broker_adapter_enabled={config.get('broker_adapter_enabled')}; 审批通过后只读检查需要改为 true。",
            False,
        )
    )
    rows.append(
        status_row(
            "qmt_enabled",
            "PASS" if bool(config.get("qmt_enabled", False)) else "WARN",
            f"qmt_enabled={config.get('qmt_enabled')}; 审批通过后只读检查需要改为 true。",
            False,
        )
    )
    rows.append(
        status_row(
            "broker.enabled",
            "PASS" if bool(broker_config.get("enabled", False)) else "WARN",
            f"broker.enabled={broker_config.get('enabled')}; 审批通过后只读检查需要改为 true。",
            False,
        )
    )
    rows.append(
        status_row(
            "real_order_gate",
            "PASS"
            if not bool(live_config.get("enabled", False)) and not bool(live_config.get("real_order_enabled", False))
            else "WARN",
            (
                f"live_trade.enabled={live_config.get('enabled')}, "
                f"real_order_enabled={live_config.get('real_order_enabled')}; "
                "未完成 100 股测试前必须保持 false。"
            ),
            False,
        )
    )

    for env_name in [
        str(broker_config.get("account_id_env", "QMT_ACCOUNT_ID")),
        str(broker_config.get("account_type_env", "QMT_ACCOUNT_TYPE")),
        str(broker_config.get("qmt_path_env", "QMT_PATH")),
        str(broker_config.get("session_id_env", "QMT_SESSION_ID")),
    ]:
        status, detail, blocking = check_env_value(env_name)
        rows.append(status_row(env_name, status, detail, blocking))

    qmt_path = os.getenv(str(broker_config.get("qmt_path_env", "QMT_PATH")), "").strip()
    rows.append(
        status_row(
            "QMT_PATH_EXISTS",
            "PASS" if qmt_path and Path(qmt_path).expanduser().exists() else "FAIL",
            "QMT_PATH 路径存在。" if qmt_path and Path(qmt_path).expanduser().exists() else "QMT_PATH 不存在或未配置。",
            True,
        )
    )

    xtquant_spec = importlib.util.find_spec("xtquant")
    rows.append(
        status_row(
            "xtquant_import",
            "PASS" if xtquant_spec is not None else "FAIL",
            "当前 Python 环境可以导入 xtquant。" if xtquant_spec is not None else "当前 Python 环境不能导入 xtquant。",
            True,
        )
    )

    planned = latest_file("reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")
    rows.append(
        status_row(
            "latest_planned_orders",
            "PASS" if planned is not None else "WARN",
            str(planned) if planned is not None else "未找到 planned_orders.csv，先运行每日操作台。",
            False,
        )
    )

    preview = latest_file("reports/live_trade/*_preview.csv")
    rows.append(
        status_row(
            "latest_live_preview",
            "PASS" if preview is not None else "WARN",
            str(preview) if preview is not None else "未找到 live preview，先运行 preview_live_orders.py 或 mock_live_order_preview.py。",
            False,
        )
    )
    return pd.DataFrame(rows)


def write_markdown(path: Path, report: pd.DataFrame) -> None:
    fail_count = int((report["status"] == "FAIL").sum())
    warn_count = int((report["status"] == "WARN").sum())
    blocking_count = int(((report["status"] == "FAIL") & report["blocking"]).sum())
    content = f"""# QMT / miniQMT 实盘接入准备度检查

本报告不连接券商、不提交委托、不打印真实账号。

## 汇总

| 项目 | 数量 |
|---|---:|
| FAIL | {fail_count} |
| WARN | {warn_count} |
| 阻断项 | {blocking_count} |

## 明细

{report.to_markdown(index=False)}

## 结论

- 阻断项为 0 前，不进入真实下单。
- 第一次通过后也只做只读检查和实盘预览。
- 真实下单前先做 100 股小资金测试。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_json_config(args.config)
    report = build_report(config)
    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_prefix.with_name(output_prefix.name + ".csv")
    md_path = output_prefix.with_name(output_prefix.name + ".md")
    report.to_csv(csv_path, index=False, encoding="utf-8-sig")
    write_markdown(md_path, report)

    print("QMT / miniQMT 实盘接入准备度检查完成：")
    print(f"- csv: {csv_path}")
    print(f"- markdown: {md_path}")
    print(report.to_string(index=False))


if __name__ == "__main__":
    main()

