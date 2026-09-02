#!/usr/bin/env python3
"""重算C/D开仓口径修复后的分腿与ACED组合结果。

本脚本不改写原V16认证目录，也不提交代码。它以V16冻结计划为旧基线，
分别回放：
1. OLD：D按66%仓位且未执行历史成交概率门；
2. POSITION_ONLY：D统一82.5%仓位，仅用于隔离金额差异；
3. ALIGNED_FAIL_CLOSED：D统一82.5%，并要求信号时成交概率>=80%。

现有三年D事件数据缺少信号时L2队列金额，因此第3组必须fail-closed。
这是数据可验证性结论，不得用事后价格穿透伪造成交概率。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acde_monthly_research import _execution_kwargs, load_monthly_config
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    replay_action_date_cash_portfolio,
)
from scripts.monitor_strategy_d_intraday import (
    StockState,
    is_d_market_context_eligible,
)
from scripts.run_paper_ab_filtered_daily_ops import resolve_ac_selected_leg


WINDOW_START = "20230901"
WINDOW_END = "20260831"
BASELINE_ROOT = ROOT / (
    "reports/current_portfolio_alignment/"
    "acde_c_third_branch_t2_22695_20260902_v16"
)
OUTPUT_ROOT = ROOT / "reports/entry_alignment_fix_20260902"
EXPECTED_OLD_COMBO_MULTIPLE = 22695.89224525786


def _read_plan(leg: str) -> pd.DataFrame:
    return pd.read_csv(
        BASELINE_ROOT / f"{leg.lower()}_plans.csv",
        dtype={
            "signal_date": str,
            "action_date": str,
            "buy_date": str,
            "exit_date": str,
            "position_open_until": str,
            "ts_code": str,
        },
        low_memory=False,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    raise TypeError(f"不支持的JSON类型: {type(value)!r}")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=_json_default,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(
    legs: Mapping[str, pd.DataFrame],
    action_dates: list[str],
    execution: Mapping[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    detail = replay_action_date_cash_portfolio(
        legs,
        action_dates=action_dates,
        priority=FIXED_PRIORITY,
        **execution,
    )
    return detail, action_metrics(detail, WINDOW_START, WINDOW_END)


def _standalone_metrics(
    legs: Mapping[str, pd.DataFrame],
    action_dates: list[str],
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for leg in FIXED_PRIORITY:
        detail = replay_action_date_cash_portfolio(
            {leg: legs[leg]},
            action_dates=action_dates,
            priority=(leg,),
            **execution,
        )
        result[leg] = action_metrics(detail, WINDOW_START, WINDOW_END)
    return result


def _pct(value: float) -> str:
    return f"{float(value) * 100:.2f}%"


def _metric_row(label: str, metrics: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {int(metrics['trade_count'])} | {_pct(metrics['win_rate'])} | "
        f"{_pct(metrics['avg_account_return'])} | {_pct(metrics['median_account_return'])} | "
        f"{float(metrics['equity_multiple']):.6f}倍 | {_pct(metrics['max_drawdown'])} | "
        f"{_pct(metrics['max_loss'])} | {_pct(metrics['max_profit'])} | "
        f"{float(metrics['profit_loss_ratio']):.4f} | "
        f"{int(metrics['max_consecutive_losses'])} |"
    )


def run() -> dict[str, Any]:
    old_legs = {leg: _read_plan(leg) for leg in FIXED_PRIORITY}
    old_combo_dates = pd.read_csv(
        BASELINE_ROOT / "combo_trades.csv",
        usecols=["action_date"],
        dtype={"action_date": str},
    )["action_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    action_dates = sorted(old_combo_dates.unique().tolist())
    monthly_config = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    execution = _execution_kwargs(monthly_config)

    position_only_legs = {leg: frame.copy() for leg, frame in old_legs.items()}
    position_only_legs["D"]["position_scale"] = 1.0

    aligned_legs = {leg: frame.copy() for leg, frame in old_legs.items()}
    aligned_d = aligned_legs["D"]
    aligned_d["position_scale"] = 1.0
    aligned_d["fill_gate_required"] = True
    aligned_d["fill_probability_threshold"] = 0.8
    aligned_d["fill_input_reliable"] = False
    aligned_d["estimated_turnover_amount"] = np.nan
    aligned_d["current_queue_amount"] = np.nan
    aligned_d["fill_probability_method"] = (
        "SIGNAL_TIME_AMOUNT_SPACE_OVER_ACTUAL_ORDER_GROSS"
    )

    old_detail, old_metrics = _metrics(old_legs, action_dates, execution)
    position_detail, position_metrics = _metrics(
        position_only_legs, action_dates, execution
    )
    aligned_detail, aligned_metrics = _metrics(aligned_legs, action_dates, execution)
    old_standalone = _standalone_metrics(old_legs, action_dates, execution)
    position_standalone = _standalone_metrics(
        position_only_legs, action_dates, execution
    )
    aligned_standalone = _standalone_metrics(aligned_legs, action_dates, execution)

    reproduced = bool(
        abs(old_metrics["equity_multiple"] - EXPECTED_OLD_COMBO_MULTIPLE) <= 1e-9
    )
    unverified_rows = aligned_detail[
        aligned_detail["status"].astype(str).eq(
            "PLAN_NOT_EXECUTED_FILL_GATE_UNVERIFIABLE"
        )
    ].copy()
    aligned_d_executed = int(aligned_metrics["leg_counts"].get("D", 0))
    d_source_columns = pd.read_csv(
        ROOT
        / "data/research/monthly_acde/20260831/strategy_d_three_year/"
        "all_reseal_signal_events.csv",
        nrows=0,
    ).columns.tolist()
    required_l2_columns = ["estimated_turnover_amount", "current_queue_amount"]
    missing_l2_columns = [
        column for column in required_l2_columns if column not in d_source_columns
    ]

    c_leg, _c_status = resolve_ac_selected_leg(
        None,
        pd.Series({"ts_code": "000001.SZ"}),
        pd.DataFrame([{"ts_code": "000002.SZ"}]),
    )
    eligible_state = StockState(
        ts_code="300001.SZ",
        market_segment="chi_next",
        upper_limit=11.0,
    )
    st_state = StockState(
        ts_code="300002.SZ",
        market_segment="chi_next",
        upper_limit=11.0,
        st_suspect=True,
    )
    prior_limit_state = StockState(
        ts_code="300003.SZ",
        market_segment="chi_next",
        upper_limit=11.0,
    )
    breadth_cases = [
        is_d_market_context_eligible(
            eligible_state,
            yesterday_limit_codes={prior_limit_state.ts_code},
            allowed_segments={"chi_next"},
        ),
        is_d_market_context_eligible(
            st_state,
            yesterday_limit_codes={prior_limit_state.ts_code},
            allowed_segments={"chi_next"},
        ),
        is_d_market_context_eligible(
            prior_limit_state,
            yesterday_limit_codes={prior_limit_state.ts_code},
            allowed_segments={"chi_next"},
        ),
    ]
    checks = {
        "old_v16_combo_exactly_reproduced": reproduced,
        "priority_is_a_c_e_d": tuple(FIXED_PRIORITY) == ("A", "C", "E", "D"),
        "c_fallback_regression_passed": c_leg == "C",
        "d_market_breadth_regression_passed": breadth_cases == [True, False, False],
        "d_position_scale_is_1": bool(aligned_d["position_scale"].eq(1.0).all()),
        "d_fill_gate_threshold_is_80pct": bool(
            aligned_d["fill_probability_threshold"].eq(0.8).all()
        ),
        "historical_d_l2_fields_available": not missing_l2_columns,
        "unverifiable_d_failed_closed": bool(
            len(unverified_rows) > 0 and aligned_d_executed == 0
        ),
        "aligned_replay_deterministic": aligned_metrics
        == _metrics(aligned_legs, action_dates, execution)[1],
    }

    decision = (
        "A_C_E_ACTIVE_D_NEW_BUY_PAUSED"
        if checks["unverifiable_d_failed_closed"]
        else "ALIGNMENT_REVIEW_REQUIRED"
    )
    summary = {
        "schema_version": 1,
        "decision": decision,
        "window": {
            "start": WINDOW_START,
            "end": WINDOW_END,
            "trade_days": len(action_dates),
        },
        "priority": list(FIXED_PRIORITY),
        "old_combo": old_metrics,
        "position_only_combo": position_metrics,
        "aligned_fail_closed_combo": aligned_metrics,
        "old_standalone": old_standalone,
        "position_only_standalone": position_standalone,
        "aligned_fail_closed_standalone": aligned_standalone,
        "d_data_audit": {
            "plan_count": int(len(aligned_d)),
            "source_missing_l2_columns": missing_l2_columns,
            "unverifiable_selected_plan_days_in_combo": int(len(unverified_rows)),
            "certified_executed_d_trades": aligned_d_executed,
            "runtime_new_buy_enabled": False,
            "reason": (
                "历史事件数据没有信号时L2队列金额，不能精确复现"
                "fill_probability>=80%；因此D样本不再计入已对齐收益。"
            ),
        },
        "c_alignment_note": (
            "C正式历史计划本来就是风险剔除后再排序，因此分腿样本不变；"
            "修复的是每日执行台错把c_rejected当成整日C禁买的分支。"
        ),
        "checks": checks,
    }

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    old_detail.to_csv(OUTPUT_ROOT / "old_combo_replay.csv", index=False)
    position_detail.to_csv(OUTPUT_ROOT / "position_only_combo_replay.csv", index=False)
    aligned_detail.to_csv(OUTPUT_ROOT / "aligned_fail_closed_combo_replay.csv", index=False)
    aligned_d.to_csv(OUTPUT_ROOT / "aligned_d_plans.csv", index=False)
    unverified_rows.to_csv(OUTPUT_ROOT / "unverified_d_plan_days.csv", index=False)
    _write_json(OUTPUT_ROOT / "summary.json", summary)

    table_header = (
        "| 口径 | 样本 | 胜率 | 平均每笔 | 中位每笔 | 复利 | 最大回撤 | "
        "最大单笔亏损 | 最大单笔盈利 | 盈亏比 | 最长连亏 |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    lines = [
        "# ACDE开仓口径修复后精确重算",
        "",
        f"窗口：`{WINDOW_START}~{WINDOW_END}`，交易日：{len(action_dates)}。",
        "",
        "## 组合对比",
        "",
        table_header,
        _metric_row("OLD（66% D，无历史>=80%门）", old_metrics),
        _metric_row("POSITION_ONLY（82.5% D，仅隔离仓位）", position_metrics),
        _metric_row("ALIGNED_FAIL_CLOSED（82.5%+成交门）", aligned_metrics),
        "",
        "## 新口径分腿",
        "",
        table_header,
        *[
            _metric_row(f"{leg}独立", aligned_standalone[leg])
            for leg in FIXED_PRIORITY
        ],
        "",
        "## 结论",
        "",
        f"- 旧V16复利精确复现：`{reproduced}`。",
        f"- D冻结计划：{len(aligned_d)}个；在组合实际轮到D的不可验证日：{len(unverified_rows)}个。",
        f"- 新口径D可认证实际成交：{aligned_d_executed}笔。",
        f"- D源数据缺少字段：`{','.join(missing_l2_columns)}`。",
        "- 因此A/C/E可继续按新回放结果评估；D在补齐历史信号时L2队列并重新认证前，禁止新BUY。",
        "- C修复不改变历史正式计划样本；它修正了未来每日执行中的漏买错误。",
    ]
    (OUTPUT_ROOT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_json(
        OUTPUT_ROOT / "artifact_manifest.json",
        {
            "schema_version": 1,
            "artifacts": {
                path.name: _sha256(path)
                for path in sorted(OUTPUT_ROOT.iterdir())
                if path.is_file() and path.name != "artifact_manifest.json"
            },
        },
    )
    return summary


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
