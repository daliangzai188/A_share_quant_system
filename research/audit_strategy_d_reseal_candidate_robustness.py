#!/usr/bin/env python3
"""审计D回封组合候选的边界、收益来源和正式D仲裁敏感性。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.research_strategy_d_explosion_features import (  # noqa: E402
    build_current_other_legs,
    combo_replay,
    executed_metrics,
    replay_d_only,
)
from scripts.research_strategy_d_full_window_features_and_gates import (  # noqa: E402
    BASELINE_ACDE_MULTIPLE,
    BASELINE_D_MULTIPLE,
    END,
    FIRST_12M_END,
    SECOND_12M_START,
    START,
    TOLERANCE,
    assert_formal_baseline,
)
from scripts.research_strategy_d_reseal_combinations import (  # noqa: E402
    MIN_PROFITABLE_TRADE_COUNT,
    OutcomeCache,
    basic_metrics,
    load_cached_reseal_events,
    merge_picks_with_baseline,
    numeric,
    outcome_frame_from_picks,
    prepare_baseline_signal_picks,
)
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


OUTPUT_DIR = ROOT / "reports/strategy_d_reseal_combinations"
EVENTS_PATH = OUTPUT_DIR / "all_reseal_signal_events.csv"


@dataclass(frozen=True)
class Perturbation:
    name: str
    description: str
    predicate: Callable[[pd.DataFrame], pd.Series]


def perturbations() -> list[Perturbation]:
    def rule(
        name: str,
        description: str,
        *,
        time_low: int = 930,
        time_high: int = 1000,
        open_low: int = 1,
        open_high: int = 1,
        seal_low: float = 0.40,
        seal_high: float = 0.60,
        speed_low: int | None = None,
        speed_high: int | None = None,
    ) -> Perturbation:
        def predicate(frame: pd.DataFrame) -> pd.Series:
            mask = (
                numeric(frame["signal_hhmm"]).between(time_low, time_high)
                & numeric(frame["open_times_at_signal"]).between(open_low, open_high)
                & numeric(frame["market_seal_rate"]).between(seal_low, seal_high)
            )
            if speed_low is not None:
                mask &= numeric(frame["last_break_to_signal_minutes"]).ge(speed_low)
            if speed_high is not None:
                mask &= numeric(frame["last_break_to_signal_minutes"]).le(speed_high)
            return mask

        return Perturbation(name, description, predicate)

    return [
        rule("selected", "09:30~10:00、第一次回封、封住率40%~60%"),
        rule("time_end_0955", "结束时间提前5分钟", time_high=955),
        rule("time_start_0935", "开始时间推迟5分钟", time_low=935),
        rule("time_end_1005", "结束时间放宽5分钟", time_high=1005),
        rule("seal_35_60", "封住率下界放宽到35%", seal_low=0.35),
        rule("seal_40_65", "封住率上界放宽到65%", seal_high=0.65),
        rule("seal_45_60", "封住率下界收紧到45%", seal_low=0.45),
        rule("seal_40_55", "封住率上界收紧到55%", seal_high=0.55),
        rule("seal_35_65", "封住率双边各放宽5%", seal_low=0.35, seal_high=0.65),
        rule("open_1_2", "允许第1~2次炸板后的回封", open_high=2),
        rule("speed_le5", "增加最后炸板后5分钟内回封", speed_high=5),
        rule("speed_6_10", "限制最后炸板后6~10分钟回封", speed_low=6, speed_high=10),
    ]


def select_daily(events: pd.DataFrame, spec: Perturbation) -> pd.DataFrame:
    selected = events[spec.predicate(events).fillna(False)].copy()
    selected["_open2_priority"] = numeric(selected["open_times_at_signal"]).eq(2).astype(int)
    selected["_recent_amount_rank"] = numeric(
        selected["signal_recent_5m_amount_vs_prev_day"]
    ).fillna(float("-inf"))
    return (
        selected.sort_values(
            [
                "trade_date", "signal_hhmm", "_open2_priority",
                "_recent_amount_rank", "ts_code", "event_id",
            ],
            ascending=[True, True, False, False, True, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )


def flatten(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_{key}": value
        for key, value in metrics.items()
        if key not in {"leg_counts", "compound_standard_id"}
    }


def robustness_metrics(detail: pd.DataFrame) -> dict[str, Any]:
    trades = detail[detail["status"].eq("EXECUTED")].copy()
    values = pd.to_numeric(trades["account_return"], errors="raise").tolist()
    without_best = values.copy()
    without_worst = values.copy()
    if without_best:
        without_best.pop(max(range(len(without_best)), key=without_best.__getitem__))
        without_worst.pop(min(range(len(without_worst)), key=without_worst.__getitem__))
    return {
        "multiple_without_best_trade": basic_metrics(without_best)["equity_multiple"],
        "multiple_without_worst_trade": basic_metrics(without_worst)["equity_multiple"],
    }


def main() -> int:
    events, cache_audit = load_cached_reseal_events(EVENTS_PATH)
    daily_data = strict.daily_data()
    source, source_audit = strict.source_audit()
    if not bool(source_audit.get("passed")):
        raise RuntimeError("严格as-of源审计失败")
    baseline_outcomes = strict.build_d(source, daily_data)
    baseline_detail = replay_d_only(baseline_outcomes, START, END)
    baseline_metrics = executed_metrics(baseline_detail)
    other_legs = build_current_other_legs()
    baseline_combo_detail, baseline_combo_metrics = combo_replay(
        baseline_outcomes, other_legs
    )
    assert_formal_baseline(baseline_metrics, baseline_combo_metrics)
    baseline_picks, mapping_audit = prepare_baseline_signal_picks(
        baseline_outcomes, source, events
    )

    rows: list[dict[str, Any]] = []
    selected_standalone = pd.DataFrame()
    for spec in perturbations():
        picks = select_daily(events, spec)
        outcomes = outcome_frame_from_picks(picks)
        standalone = replay_d_only(outcomes, START, END)
        independent = executed_metrics(standalone)
        first = executed_metrics(
            standalone[standalone["signal_date"].between(START, FIRST_12M_END)]
        )
        second = executed_metrics(
            standalone[standalone["signal_date"].between(SECOND_12M_START, END)]
        )

        merged_outcomes, chosen = merge_picks_with_baseline(picks, baseline_picks)
        merged_detail = replay_d_only(merged_outcomes, START, END)
        merged = executed_metrics(merged_detail)
        _, combo = combo_replay(merged_outcomes, other_legs)

        # 极端保守仲裁：只要正式D当天存在候选，新子策略即使更早也不允许占用。
        baseline_dates = set(baseline_picks["trade_date"].astype(str))
        reserve_picks = picks[~picks["trade_date"].astype(str).isin(baseline_dates)].copy()
        reserve_outcomes, _ = merge_picks_with_baseline(reserve_picks, baseline_picks)
        reserve_d_detail = replay_d_only(reserve_outcomes, START, END)
        reserve_d = executed_metrics(reserve_d_detail)
        _, reserve_combo = combo_replay(reserve_outcomes, other_legs)

        rows.append(
            {
                "variant": spec.name,
                "description": spec.description,
                "candidate_day_count": len(picks),
                "price_confirmed_count": int(picks["queue_price_confirmed"].sum()),
                **flatten("independent", independent),
                "first_12m_trade_count": int(first["trade_count"]),
                "first_12m_multiple": float(first["equity_multiple"]),
                "second_12m_trade_count": int(second["trade_count"]),
                "second_12m_multiple": float(second["equity_multiple"]),
                **robustness_metrics(standalone),
                **flatten("merged_d", merged),
                **flatten("acde", combo),
                "candidate_wins_by_signal_time_day_count": int(chosen["source_priority"].eq(1).sum()),
                "main_triple_gate_passed": bool(
                    int(independent["trade_count"]) >= MIN_PROFITABLE_TRADE_COUNT
                    and float(independent["equity_multiple"]) > 1.0 + TOLERANCE
                    and float(merged["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
                    and float(combo["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
                ),
                "baseline_reserve_d_multiple": float(reserve_d["equity_multiple"]),
                "baseline_reserve_acde_multiple": float(reserve_combo["equity_multiple"]),
                "baseline_reserve_dual_gate_passed": bool(
                    float(reserve_d["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
                    and float(reserve_combo["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
                ),
            }
        )
        if spec.name == "selected":
            selected_standalone = standalone

    result = pd.DataFrame(rows)
    selected = result[result["variant"].eq("selected")].iloc[0].to_dict()
    payload = {
        "schema_version": 1,
        "protocol": STRICT_DISCOVERY,
        "window": f"{START}~{END}",
        "candidate": "09:30~10:00第一次炸板后的回封，信号时全市场首板封住率40%~60%",
        "baseline": {"d": baseline_metrics, "acde": baseline_combo_metrics},
        "input_audit": {
            **cache_audit,
            "strict_source_audit_passed": True,
            "formal_d_signal_mapping": mapping_audit,
        },
        "selected_result": selected,
        "neighbor_variant_count": len(result) - 1,
        "neighbor_main_gate_pass_count": int(result[~result["variant"].eq("selected")]["main_triple_gate_passed"].sum()),
        "formal_strategy_modified": False,
        "release_eligible": False,
        "warning": "候选来自5944条同窗口组合搜索，样本仅25笔且平均收益bootstrap下界为负；邻域结果只作稳健性审计。",
    }
    result.to_csv(
        OUTPUT_DIR / "selected_candidate_perturbation_audit.csv",
        index=False,
        encoding="utf-8-sig",
    )
    selected_standalone.to_csv(
        OUTPUT_DIR / "selected_candidate_independent_detail.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (OUTPUT_DIR / "selected_candidate_robustness_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
