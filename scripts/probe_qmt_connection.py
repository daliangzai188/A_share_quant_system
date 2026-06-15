from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读探测 QMT XtQuantTrader 连接路径和 session。")
    parser.add_argument("--output", default="reports/live_trade/qmt_connection_probe.csv", help="输出 CSV 路径。")
    return parser.parse_args()


def mask_account(account_id: str) -> str:
    account_id = account_id.strip()
    if len(account_id) <= 4:
        return "****"
    return account_id[:2] + "*" * (len(account_id) - 4) + account_id[-2:]


def existing_path(path: str | Path) -> str | None:
    candidate = Path(path).expanduser()
    return str(candidate) if candidate.exists() else None


def build_candidate_paths(qmt_path: str) -> list[str]:
    candidates: list[str] = []
    if qmt_path:
        base = Path(qmt_path)
        candidates.extend(
            [
                str(base),
                str(base / "userdata_mini"),
                str(base / "userdata"),
                str(base.parent),
                str(base.parent / "userdata_mini"),
                str(base.parent / "userdata"),
            ]
        )

    common_roots = [
        Path.home() / "国金证券QMT交易端",
        Path.home() / "国金证券QMT交易端" / "userdata",
        Path.home() / "国金证券QMT交易端" / "userdata_mini",
        Path.home() / "国金证券QMT交易端" / "bin.x64",
    ]
    candidates.extend(str(path) for path in common_roots)

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = existing_path(candidate)
        if resolved and resolved.lower() not in seen:
            result.append(resolved)
            seen.add(resolved.lower())
    return result


def object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    data: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if isinstance(attr, (str, int, float, bool, type(None))):
            data[name] = attr
    return data


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    account_id = os.getenv("QMT_ACCOUNT_ID", "").strip()
    account_type = os.getenv("QMT_ACCOUNT_TYPE", "STOCK").strip() or "STOCK"
    qmt_path = os.getenv("QMT_PATH", "").strip()
    configured_session_id = int(os.getenv("QMT_SESSION_ID", "1001") or "1001")
    session_ids = list(dict.fromkeys([configured_session_id, 1001, 1002, 10001, 20001, 31001]))
    paths = build_candidate_paths(qmt_path)

    from xtquant import xttrader, xttype  # type: ignore

    rows: list[dict[str, Any]] = []
    for path in paths:
        for session_id in session_ids:
            trader = None
            row: dict[str, Any] = {
                "qmt_path": path,
                "session_id": session_id,
                "account_id": mask_account(account_id),
                "account_type": account_type,
                "connect_result": "",
                "subscribe_result": "",
                "asset_query": "",
                "status": "FAIL",
                "error": "",
            }
            try:
                trader = xttrader.XtQuantTrader(path, session_id)
                account = xttype.StockAccount(account_id, account_type)
                trader.start()
                connect_result = trader.connect()
                row["connect_result"] = connect_result
                if connect_result not in {0, True, None}:
                    row["error"] = f"connect returned {connect_result}"
                    rows.append(row)
                    continue
                if hasattr(trader, "subscribe"):
                    row["subscribe_result"] = trader.subscribe(account)
                asset = trader.query_stock_asset(account)
                row["asset_query"] = bool(object_to_dict(asset) or asset is not None)
                row["status"] = "PASS"
                rows.append(row)
            except Exception as exc:
                row["error"] = f"{exc.__class__.__name__}: {exc}"
                rows.append(row)
            finally:
                if trader is not None and hasattr(trader, "stop"):
                    try:
                        trader.stop()
                    except Exception:
                        pass

    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = pd.DataFrame(rows)
    report.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"QMT connection probe saved: {output}")
    if report.empty:
        print("No existing QMT path candidates found.")
    else:
        print(report.to_string(index=False))


if __name__ == "__main__":
    main()
