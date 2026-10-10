"""用现有行情权限核对无后续报价的样本；只保存隔离证据。"""
from pathlib import Path
import datetime as dt
import hashlib
import json
import os

import pandas as pd
import requests
from dotenv import dotenv_values

OUT = Path(__file__).resolve().parent
CODE = OUT.parent / "2026-10-10_继续补齐复核数据/code"


def capture_suspensions(codes: list[str], stem: str = "suspend_d") -> pd.DataFrame:
    """HTTPS查询官方每日停复牌信息，分页并缓存，不保存凭据。"""
    path = OUT / f"{stem}_raw.csv"
    receipt_path = OUT / f"{stem}_receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        assert receipt["params"]["ts_code"] == ",".join(codes)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]
        return pd.read_csv(path, dtype=str).fillna("")
    env_path = os.environ.get("A_SYSTEM_MARKET_CREDENTIAL_FILE", "")
    if not env_path:
        env_path = "/Users/eass/A_System_Rebuild/2026-10-08/pipeline_repair/data_retention_export/.env"
    token = os.environ.get("TUSHARE_TOKEN") or dotenv_values(env_path).get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("现有行情凭据不可用")
    query = {"ts_code": ",".join(codes), "start_date": "20250603", "end_date": "20261009"}
    frames = []
    with requests.Session() as session:
        session.trust_env = False
        for page in range(10):
            for attempt in range(2):
                try:
                    response = session.post("https://api.tushare.pro", json={
                        "api_name": "suspend_d", "token": token,
                        "params": {**query, "offset": page * 5000, "limit": 5000},
                        "fields": "ts_code,trade_date,suspend_timing,suspend_type"}, timeout=(20, 35))
                    break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                    if attempt == 1:
                        raise
                    print("SUSPEND_D_TRANSIENT_NETWORK_RETRY", flush=True)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(str(payload.get("msg", "停牌核对请求失败")).replace(token, "[REDACTED]"))
            data = payload.get("data") or {}
            frame = pd.DataFrame(data.get("items", []), columns=data.get("fields", []))
            assert list(frame.columns) == ["ts_code", "trade_date", "suspend_timing", "suspend_type"]
            frames.append(frame)
            if len(frame) < 5000:
                break
        else:
            raise RuntimeError("停复牌分页未完结")
    result = pd.concat(frames, ignore_index=True).fillna("")
    assert result.ts_code.isin(codes).all()
    assert result.trade_date.astype(str).between(query["start_date"], query["end_date"]).all()
    assert result.suspend_type.isin(["S", "R"]).all()
    result.to_csv(path, index=False, encoding="utf-8-sig")
    receipt = {"api": "suspend_d", "endpoint": "https://api.tushare.pro", "params": query,
               "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "rows": len(result), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "credentials_saved": False, "new_paid_purchase": False}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return result.astype(str)


def main():
    excluded = pd.read_csv(OUT / "factor_exclusion_context.csv", dtype={"trade_date": str, "ts_code": str,
                                                                          "delist_date": str}).fillna("")
    calendar = pd.read_csv(CODE / "data/raw/trade_calendar.csv", dtype={"cal_date": str})
    dates = sorted(calendar[calendar.is_open.eq(1)].cal_date.unique())
    indices = {date: index for index, date in enumerate(dates)}
    suspensions = capture_suspensions(sorted(excluded.ts_code.unique()))
    full_day = suspensions[suspensions.suspend_type.eq("S") & suspensions.suspend_timing.eq("")]
    suspended_keys = set(zip(full_day.ts_code, full_day.trade_date))
    # 北交所官方同一发行人的公告核验历史839680与后续920680；仅作报价缺失解释，
    # 不更改冻结行情、收益或当前策略市场范围。
    alias_documentation = [
        "https://www.bse.cn/disclosure/2025/2025-07-23/1493e8a1141c41758be3d7fc313a752f.pdf",
        "https://www.bse.cn/disclosure/2025/2025-12-26/c0744890832f4ba98ac837c26dc05087.pdf",
    ]
    historic_keys = set()
    if (OUT / "suspend_d_historic_code_receipt.json").exists():
        historic = capture_suspensions(["839680.BJ"], stem="suspend_d_historic_code")
        historic_full = historic[historic.suspend_type.eq("S") & historic.suspend_timing.eq("")]
        historic_keys = set(zip(historic_full.ts_code, historic_full.trade_date))
    notice = json.loads((OUT / "bse_historic_code_exclusion_evidence.json").read_text())
    notice_interval = dates[indices["20250704"]:indices["20250704"] + 5]
    assert set(notice["quote_dates_explained"]).issubset(set(notice_interval))
    quotes = {}
    rows = []
    for item in excluded.to_dict("records"):
        for role, offset in (("entry", 1), ("exit", 2)):
            date = dates[indices[item["trade_date"]] + offset]
            if date not in quotes:
                daily = pd.read_csv(CODE / "data/raw/daily" / f"{date}.csv", dtype={"ts_code": str})
                quotes[date] = set(daily.ts_code)
            if item["ts_code"] in quotes[date]:
                classification = "QUOTE_PRESENT"
            elif (item["ts_code"], date) in suspended_keys:
                classification = "OFFICIAL_FULL_DAY_SUSPENSION"
            elif item["ts_code"] == "920680.BJ" and ("839680.BJ", date) in historic_keys:
                classification = "OFFICIAL_SUSPENSION_UNDER_HISTORIC_CODE"
            elif item["ts_code"] == notice["dataset_code"] and date in notice["quote_dates_explained"]:
                classification = "BSE_NOTICE_AND_CALENDAR_SUSPENSION_INFERENCE"
            elif item["delist_date"] and date >= item["delist_date"]:
                classification = "AFTER_RECORDED_DELISTING"
            else:
                classification = "NO_QUOTE_NOT_YET_EXPLAINED"
            rows.append({**item, "quote_role": role, "quote_date": date, "classification": classification})
    checks = pd.DataFrame(rows)
    checks.to_csv(OUT / "factor_unpriced_classification.csv", index=False, encoding="utf-8-sig")
    unknown = checks[checks.classification.eq("NO_QUOTE_NOT_YET_EXPLAINED")]
    summary = {"samples": len(excluded), "checked_stock_days": len(checks),
               "counts": checks.classification.value_counts().to_dict(),
               "unexplained_stock_days": len(unknown), "unknown_rows": unknown.to_dict("records"),
               "status": "ALL_NO_QUOTES_EXPLAINED" if unknown.empty else "PARTIAL_NO_QUOTES_EXPLAINED",
               "official_documentation": ["https://tushare.pro/document/2?doc_id=214",
                                          "https://tushare.pro/document/2?doc_id=27"],
               "historic_code_primary_evidence": alias_documentation,
               "historic_code_mapping_used_only_for_exclusion_classification": True,
               "notice_inference_rows_explicitly_not_api_returned": int(checks.classification.eq(
                   "BSE_NOTICE_AND_CALENDAR_SUSPENSION_INFERENCE").sum()),
               "formal_raw_replaced": False, "invented_prices": False, "production_writes": False}
    (OUT / "factor_unpriced_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    main()
