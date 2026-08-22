#!/usr/bin/env python3
"""生成D逐笔队列研究的数据源覆盖审计与正式认证闸门。

本脚本只读本地报告和清单，不连接QMT、不访问账户、不请求网络。它会明确区分：

* 5分钟近似路径；
* 1分钟价格路径；
* 全市场逐笔委托/成交/盘口队列（正式认证必需）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_strategy_d_intraday_tushare_1m import (  # noqa: E402
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    build_cluster_jobs,
    load_open_dates,
    load_targets,
)
from src.strategy_d_strict_intraday import (  # noqa: E402
    REQUIRED_D_SCAN_COLUMNS,
    REQUIRED_EXCHANGES,
    REQUIRED_L2_EVENT_COLUMNS,
    REQUIRED_L2_MANIFEST_COLUMNS,
    strict_l2_manifest_gate,
)


WINDOW_START = "20240630"
WINDOW_END = "20260630"
L2_MANIFEST_PATH = ROOT / "data/research/strategy_d_intraday/strict_l2_daily_manifest.csv"
TUSHARE_STATUS_PATH = ROOT / "data/research/strategy_d_intraday/tushare_1m_status.csv"
BAOSTOCK_REPORT_PATH = ROOT / "reports/strategy_d_intraday_research/baostock_5m_collection.json"
QMT_REPORT_PATH = ROOT / "reports/strategy_d_intraday_research/qmt_depth_probe.json"
OUTPUT_PATH = ROOT / "reports/strategy_d_intraday_research/strict_source_audit.json"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path: Path, *, dtype: dict[str, Any] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=dtype, low_memory=False)


def count_complete_tushare_targets(status: pd.DataFrame) -> int:
    if status.empty or not {"target_key", "status"}.issubset(status.columns):
        return 0
    latest = status.drop_duplicates("target_key", keep="last")
    return int(latest["status"].astype(str).eq("COMPLETE_1M_NO_QUEUE_DEPTH").sum())


def build_audit(*, l2_manifest_path: Path = L2_MANIFEST_PATH) -> dict[str, Any]:
    targets = load_targets()
    all_open_dates = load_open_dates()
    window_open_dates = [date for date in all_open_dates if WINDOW_START <= date <= WINDOW_END]
    jobs = build_cluster_jobs(targets, all_open_dates)
    tushare_status = read_csv(
        TUSHARE_STATUS_PATH,
        dtype={"target_key": str, "trade_date": str, "ts_code": str},
    )
    l2_manifest = read_csv(
        l2_manifest_path,
        dtype={"trade_date": str, "exchange": str},
    )
    l2_gate = strict_l2_manifest_gate(
        l2_manifest,
        required_open_dates=window_open_dates,
        required_exchanges=REQUIRED_EXCHANGES,
    )
    qmt = read_json(QMT_REPORT_PATH)
    baostock = read_json(BAOSTOCK_REPORT_PATH)
    tushare_complete = count_complete_tushare_targets(tushare_status)
    qmt_tick = int(qmt.get("tick_available_count", 0) or 0)
    qmt_book = int(qmt.get("historical_book_available_count", 0) or 0)
    strict_passed = bool(l2_gate["passed"])
    blockers = []
    if not strict_passed:
        if tushare_complete < len(targets):
            blockers.append(
                f"Tushare一分钟路径仅完成{tushare_complete}/{len(targets)}个冻结目标"
            )
        blockers.append(
            "缺少整个24个月窗口、沪深京全市场、09:30~14:55完整逐笔委托/成交/盘口文件"
        )
        if qmt_tick == 0 or qmt_book == 0:
            blockers.append("QMT抽样未返回两年历史tick或买一队列字段")
        blockers.append(
            "现有6,848母池只覆盖最终收盘strong的56日，不能单独倒推出所有交易日信号时点情绪"
        )
    return {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "research_protocol": "STRICT_DISCOVERY",
        "strategy": "D",
        "window": f"{WINDOW_START}~{WINDOW_END}",
        "formal_rule_modified": False,
        "current_formal_baselines_frozen": {
            "d_standalone_trade_count": 39,
            "d_standalone_compound": 2.0261239235922566,
            "acde_trade_count": 132,
            "acde_compound": 327.72671897548867,
            "leg_counts": {"D": 22, "A": 44, "E": 49, "C": 17},
            "priority": "D>A>E>C",
            "position_ratio": 0.825,
        },
        "frozen_touch_target_count": int(len(targets)),
        "window_open_day_count": int(len(window_open_dates)),
        "source_layers": {
            "baostock_5m": {
                "terminal_target_count": int(baostock.get("terminal_target_count", 0) or 0),
                "target_count": int(baostock.get("target_count", 0) or 0),
                "role": "APPROXIMATE_PATH_ONLY",
                "certification_eligible": False,
            },
            "tushare_1m": {
                "complete_target_count": tushare_complete,
                "target_count": int(len(targets)),
                "clustered_request_count": int(len(jobs)),
                "request_interval_seconds": DEFAULT_REQUEST_INTERVAL_SECONDS,
                "estimated_hours_at_current_rate": round(
                    len(jobs) * DEFAULT_REQUEST_INTERVAL_SECONDS / 3600, 2
                ),
                "role": "PRICE_PATH_CROSS_CHECK_NO_QUEUE",
                "certification_eligible": False,
            },
            "qmt_probe": {
                "sample_target_count": int(qmt.get("target_count", 0) or 0),
                "one_minute_available_count": int(qmt.get("one_minute_available_count", 0) or 0),
                "tick_available_count": qmt_tick,
                "historical_book_available_count": qmt_book,
                "read_only_xtdata": bool(qmt.get("read_only_xtdata", False)),
                "certification_eligible": False,
            },
            "strict_full_market_l2": {
                "manifest_path": str(l2_manifest_path.relative_to(ROOT)),
                **l2_gate,
                "required_exchanges": list(REQUIRED_EXCHANGES),
                "role": "FORMAL_QUEUE_SENTIMENT_RANKING_CERTIFICATION",
                "certification_eligible": strict_passed,
            },
        },
        "standard_l2_contract": {
            "event_required_columns": sorted(REQUIRED_L2_EVENT_COLUMNS),
            "synchronized_d_scan_required_columns": sorted(REQUIRED_D_SCAN_COLUMNS),
            "daily_manifest_required_columns": sorted(REQUIRED_L2_MANIFEST_COLUMNS),
            "required_scope": "每个交易日SSE/SZSE/BSE全市场，不是只给最终候选；09:30前开始，至少覆盖到14:55",
            "required_units": "价格=元，数量=股(SHARE)，时间至少到秒且保留原始sequence",
            "required_semantics": "逐笔委托新增/撤单、逐笔成交、盘口快照；供应方声明序列完整",
        },
        "certification": {
            "first_eligible_reseal_reconstructable": strict_passed,
            "signal_time_sentiment_reconstructable": strict_passed,
            "same_day_ranking_reconstructable": strict_passed,
            "queue_fill_and_1455_cancel_reconstructable": strict_passed,
            "current_d_reproduction_allowed": strict_passed,
            "d_standalone_compound_comparison_allowed": strict_passed,
            "acde_one_leg_replacement_allowed": strict_passed,
            "release_eligible": False,
            "status": (
                "DATA_READY_REPLAY_NOT_YET_RUN"
                if strict_passed
                else "BLOCKED_HISTORICAL_FULL_MARKET_L2_REQUIRED"
            ),
        },
        "blockers": blockers,
        "next_authorized_action": (
            "导入已有合规历史L2数据并运行逐日重放"
            if not strict_passed
            else "按冻结D规则先复现当前D，再运行候选变体和ACDE双门槛"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计D逐笔队列严格研究数据源")
    parser.add_argument("--l2-manifest", type=Path, default=L2_MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_audit(l2_manifest_path=args.l2_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # 数据缺口是研究状态，不是脚本故障；报告成功落盘即返回0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
