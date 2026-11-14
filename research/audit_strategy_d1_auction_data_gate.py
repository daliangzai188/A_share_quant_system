#!/usr/bin/env python3
"""审计D1竞价抢筹是否具备严格as-of历史研究数据。

只检查数据与接口权限，不计算收益、不连接券商、不下单。09:30一分钟bar是09:25
最终竞价成交代理，不能倒推09:24挂单时已知的竞价量比、虚拟涨幅和虚拟金额。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
import urllib.request

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.secret_config import load_tushare_token  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


FEATURE_PATH = ROOT / "data/processed/auction_features.csv"
OUTPUT_PATH = ROOT / "reports/strategy_d1_auction/data_gate_summary.json"


def probe(token: str, api_name: str) -> dict[str, Any]:
    payload = {
        "api_name": api_name,
        "token": token,
        "params": {"trade_date": "20260630"},
        "fields": "",
    }
    request = urllib.request.Request(
        "http://api.tushare.pro",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    data = result.get("data") or {}
    return {
        "api_name": api_name,
        "code": int(result.get("code", -1)),
        "message": str(result.get("msg", "")),
        "row_count": int(len(data.get("items") or [])),
        "fields": list(data.get("fields") or []),
        "access_granted": int(result.get("code", -1)) == 0,
    }


def main() -> int:
    frame = pd.read_csv(
        FEATURE_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False
    )
    required = {"trade_date", "ts_code", "data_available", "unavailable_reason"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"竞价占位文件缺少字段：{missing}")
    available = frame["data_available"].astype(str).str.lower().isin({"true", "1", "yes"})
    config = load_json_config(ROOT / "config/config.json")
    token = load_tushare_token(config, project_root=ROOT)
    if not token:
        raise RuntimeError("缺少Tushare Token，无法复核付款后的竞价权限")
    probes = [probe(token, api_name) for api_name in ("stk_auction", "stk_auction_o")]
    strict_ready = bool(available.any() and all(item["access_granted"] for item in probes))
    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "protocol": STRICT_DISCOVERY,
        "strategy_school": "D1_CALL_AUCTION_GRABBING_0915_0925",
        "formal_strategy_modified": False,
        "local_feature_path": str(FEATURE_PATH.relative_to(ROOT)),
        "local_row_count": int(len(frame)),
        "local_trade_day_count": int(frame["trade_date"].nunique()),
        "local_available_row_count": int(available.sum()),
        "local_unavailable_row_count": int((~available).sum()),
        "local_file_is_placeholder_only": bool(not available.any()),
        "permission_probes": probes,
        "strict_asof_two_year_research_ready": strict_ready,
        "formal_decision": (
            "DATA_READY_RESEARCH_MAY_START"
            if strict_ready
            else "REJECT_D1_NO_0924_ASOF_AUCTION_HISTORY_KEEP_OUT_OF_FORMAL_D"
        ),
        "why_0930_minute_is_not_substitute": (
            "09:30分钟bar最多代理09:25最终成交价量；D1在09:24前下单时不能知道最终匹配结果，"
            "用它筛选会形成未来函数。"
        ),
        "required_fields_before_research": [
            "09:24及以前逐时点虚拟开盘价",
            "09:24及以前虚拟匹配量/金额",
            "买卖未匹配量或可复现竞价量比的原始字段",
            "覆盖完整2024-06-30~2026-06-30候选分母",
        ],
        "release_eligible": False,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
