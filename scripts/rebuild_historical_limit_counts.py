"""Recollect unavailable early limit-up lists without inventing execution fields."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import pandas as pd
import tushare as ts

FIELDS = "ts_code,name,trade_date,lu_time,ld_time,open_time,last_time,lu_desc,tag,theme,net_change,bid_amount,status,bid_change,bid_turnover,lu_bid_vol,pct_chg,bid_pct_chg,rt_pct_chg,limit_order,amount,turnover_rate,free_float,lu_limit_order"


def validate_list(frame: pd.DataFrame, date: str) -> None:
    required = {"ts_code", "name", "trade_date", "tag"}
    if required - set(frame.columns) or frame.empty:
        raise ValueError("KPL historical source is unavailable or incomplete")
    if not frame.trade_date.astype(str).eq(date).all():
        raise ValueError("KPL returned another trade date")
    if frame[list(required)].isna().any().any():
        raise ValueError("KPL is missing code/name/date/tag")
    if frame.duplicated(["trade_date", "ts_code", "tag"]).any():
        raise ValueError("Duplicate historical source keys")
    if not frame.tag.eq("涨停").all():
        raise ValueError("KPL returned another board category")


def count_row(frame: pd.DataFrame, date: str, raw_path: Path) -> dict:
    validate_list(frame, date)
    listed = frame.ts_code.astype(str).str.endswith((".SH", ".SZ"))
    non_st = ~frame.name.astype(str).str.contains("ST", case=False, regex=False)
    return {"trade_date": date, "limit_up_count": int((listed & non_st).sum()),
            "source_row_count": len(frame), "excluded_st_count": int((listed & ~non_st).sum()),
            "source": "kpl_list", "source_tag": "涨停", "scope": "SH_SZ_NON_ST",
            "raw_file": f"data/raw/kpl_limit_list/{date}.csv",
            "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "execution_fields_reconstructed": False}


def main():
    parser = argparse.ArgumentParser(description="采集早期涨停原始榜单及有来源的涨停数；不补造炸板次数或成交模型字段")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-date", default="20190101")
    parser.add_argument("--end-date", default="20191127")
    args = parser.parse_args()
    root, output = args.project_root.absolute(), args.output.absolute()
    if output == root or root in output.parents:
        raise ValueError("Output must be outside production")
    sys.path.insert(0, str(root))
    from src.secret_config import load_tushare_token
    cfg = json.loads((root / "config/config.json").read_text())
    token = load_tushare_token(cfg, project_root=root)
    if not token:
        raise RuntimeError("TUSHARE_TOKEN missing")
    calendar = pd.read_csv(root / "data/raw/trade_calendar.csv", dtype={"cal_date": str})
    dates = sorted(set(calendar.loc[pd.to_numeric(calendar.is_open).eq(1), "cal_date"]))
    dates = [d for d in dates if args.start_date <= d <= args.end_date]
    if not dates or dates[-1] != args.end_date:
        raise ValueError("Calendar does not cover requested range")
    folder = output / "data/raw/kpl_limit_list"
    folder.mkdir(parents=True, exist_ok=True)
    api = ts.pro_api(token, timeout=30)
    rows, failures = [], {}
    state = {"status": "RUNNING", "expected_days": len(dates), "checked": 0,
             "source": "kpl_list", "broker_calls_sent": False, "production_writes": False}
    def save():
        state.update(saved_days=len(rows), failed=failures)
        temp = output / "early_history_status.json.tmp"
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        temp.replace(output / "early_history_status.json")
    for date in dates:
        path = folder / (date + ".csv")
        for attempt in range(3):
            try:
                if path.exists():
                    frame = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})
                else:
                    time.sleep(0.5)
                    frame = api.query("kpl_list", trade_date=date, tag="涨停", fields=FIELDS, limit=8000, offset=0)
                    validate_list(frame, date)
                    temp = path.with_suffix(".csv.tmp")
                    frame.to_csv(temp, index=False, encoding="utf-8-sig")
                    temp.replace(path)
                rows.append(count_row(frame, date, path))
                break
            except Exception as exc:
                message = str(exc).replace(token, "[REDACTED]")[:350]
                if attempt == 2:
                    failures[date] = message
                else:
                    time.sleep(2 * (attempt + 1))
        state["checked"] += 1
        if state["checked"] % 10 == 0 or date in failures:
            save()
            print("EARLY_LIMIT_HISTORY", state["checked"], "/", len(dates), "failed", len(failures), flush=True)
    if not failures:
        pd.DataFrame(rows).sort_values("trade_date").to_csv(output / "data/raw/market_limit_counts.csv", index=False, encoding="utf-8-sig")
        (folder / "manifest.json").write_text(json.dumps({r["trade_date"]: r for r in rows}, ensure_ascii=False, indent=2) + "\n")
    state["status"] = "COUNTS_COLLECTED_NEEDS_VALIDATION" if not failures else "INCOMPLETE"
    save()
    print(state["status"], flush=True)


if __name__ == "__main__":
    main()
