#!/usr/bin/env python3
"""认证方案甲（稳妥版+60日影子净值停手）正式规则与回测逐笔对齐。

方案甲 = 当前A>C>E>D + 三处改动（2026-09-19用户批准）：
1. A主池封单比例加0.1%-0.3%档（等价研究变体A_FD_LOWER_ADJACENT）；
2. E每日第一名处于全市场涨停不超过30只时空仓（等价E_RISK_EXCLUDE_LIMIT_UP_LT30）；
3. 组合层停手：影子净值（正式A/C/E、不含D、不停手）滞后6个交易日低于其60日均线时，
   A/C/E/D当天都不开新仓（唯一定义见src/equity_curve_stop.py）。

D历史回放沿用2026-09-02入场口径修复后的失败关闭口径：三年D事件缺少信号时L2，
无法证明>=80%成交概率门，历史按0笔计；实盘D仍受实时门禁约束。

任一检查失败即抛错，不写PASS证书。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan  # noqa: E402
from src.acde_monthly_research import (  # noqa: E402
    _context,
    _execution_kwargs,
    _variant_sets,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import StaticOutcomeCache, plan_signature  # noqa: E402
from src.acde_rolling_framework import (  # noqa: E402
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.equity_curve_stop import (  # noqa: E402
    METHOD_ID,
    allowed_action_dates,
    build_shadow_nav,
    decision_for_next_action_date,
    filter_plans_to_allowed_dates,
    load_settings,
)
from src.live_certification import (  # noqa: E402
    certification_config_sha256,
    certification_file_sha256,
    certification_files_sha256,
)
from src.utils.config import load_json_config  # noqa: E402


CUTOFF = "20260831"
SCENARIO = "acde_plan_jia_stop_gate_20260919"
RELEASE_ID = "ACDE_PLAN_JIA_STOP_GATE_20260919"
A_RELEASE_ID = "A_FD_0_1PCT_EXTENSION_20260919_JIA"
E_RELEASE_ID = "E_WEAK_MARKET_LT30_NO_TRADE_20260919_JIA"
OUTPUT_ROOT = ROOT / "reports/current_portfolio_alignment/acde_plan_jia_20260919"
LIVE_CERTIFICATION_PATH = ROOT / "reports/current_portfolio_alignment/return_first_live_certification.json"
LIVE_REPORT_PATH = ROOT / "reports/current_portfolio_alignment/return_first_portfolio_report.md"
TOLERANCE = 1e-9

# 第一次运行（--measure-only）得到的数字锁定于此；之后每次认证必须逐位复现。
EXPECTED_GATED_COMBO: dict[str, Any] | None = {
    "trade_count": 179,
    "win_rate": 0.7094972067039106,
    "avg_account_return": 0.05207858790989536,
    "median_account_return": 0.03132265631336639,
    "equity_multiple": 4369.036737559196,
    "max_drawdown": -0.20953309682221255,
    "max_profit": 0.47625292237585115,
    "max_loss": -0.17603419610450732,
    "profit_loss_ratio": 2.177308937224374,
    "max_consecutive_losses": 4,
    "leg_counts": {
        "A": 92,
        "C": 62,
        "E": 25
    }
}
EXPECTED_STOP_DAYS_IN_WINDOW: int | None = 7

CODE_FILES = [
    "config/acde_rolling_optimization.json",
    "config/config.json",
    "config/strategy_config.json",
    "config/strategy_e_r1_scenarios.json",
    "config/strategy_d_factor_release.json",
    "src/paper_candidate_generator.py",
    "src/strategy_c_exit.py",
    "src/acde_rolling_candidates.py",
    "src/acde_rolling_framework.py",
    "src/acde_monthly_research.py",
    "src/combined_live_engine.py",
    "src/equity_curve_stop.py",
    "src/five_year_research.py",
    "src/live_order_gateway.py",
    "src/live_certification.py",
    "src/strategy_e.py",
    "src/strategy_d_factor_rules.py",
    "src/strategy_identity.py",
    "scripts/optimize_acde_rolling_three_year.py",
    "scripts/run_paper_ab_filtered_daily_ops.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/monitor_strategy_d_intraday.py",
    "scripts/trading_daemon.py",
    "scripts/update_equity_curve_stop.py",
    "scripts/certify_plan_jia_release.py",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="认证方案甲正式规则、停手门禁与回测逐笔对齐")
    parser.add_argument(
        "--measure-only",
        action="store_true",
        help="只计算并打印指标用于首次锁定，不写任何证书或发布目录",
    )
    return parser.parse_args()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def close_enough(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(close_enough(actual.get(k), v) for k, v in expected.items())
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return actual is not None and abs(float(actual) - float(expected)) <= TOLERANCE * max(1.0, abs(float(expected)))
    return actual == expected


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def rule_checks(runtime: Mapping[str, Any], strategy: Mapping[str, Any], e_spec: Mapping[str, Any]) -> dict[str, bool]:
    profiles = strategy["candidate_filters"]["condition_profiles"]
    fd_values = sorted(
        next(c["value"] for c in p["conditions"] if c["column"] == "fd_ratio_bucket") for p in profiles
    )
    same_shape = all(
        {c["column"]: c["value"] for c in p["conditions"] if c["column"] != "fd_ratio_bucket"}
        == {"segment_limit_up_count_bucket": "lt_5", "market_chain_count_bucket": "8_15"}
        for p in profiles
    )
    stop = runtime.get("equity_curve_stop", {})
    gate = e_spec["entry_gate"]
    certification = runtime["portfolio_certification"]
    return {
        "a_release_id": strategy.get("release_id") == A_RELEASE_ID
        and strategy["paper_ab_filtered_strategy"]["a_strategy"].get("release_id") == A_RELEASE_ID,
        "a_three_core_profiles_fd_0_1_to_1pct": fd_values == ["0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct"],
        "a_profiles_keep_lt5_chain8_15": same_shape,
        "a_fallback_unchanged": strategy["candidate_filters"]["fallback_when_primary_empty"].get("enabled") is True,
        "e_release_id": e_spec.get("release_id") == E_RELEASE_ID,
        "e_limit_up_gate_120_180_and_lt_30": gate["exclude_values"].get("limit_up_count_bucket") == ["120_180", "lt_30"],
        "e_gate_after_first_pick_no_fallback": gate.get("apply_after_daily_first_pick") is True
        and gate.get("fallback_to_second_candidate") is False,
        "stop_enabled": stop.get("enabled") is True,
        "stop_ma60_lag6": int(stop.get("ma_window", -1)) == 60 and int(stop.get("lag_trading_days", -1)) == 6,
        "stop_history_start_20190101": str(stop.get("history_start")) == "20190101",
        "certification_release_id": certification.get("release_id") == RELEASE_ID,
        "certification_expected_scenario": certification.get("certification_expected_scenario") == SCENARIO,
        "priority_a_c_e_d": list(certification.get("strategy_priority_order", [])) == ["A", "C", "E", "D"],
    }


def run(measure_only: bool) -> dict[str, Any]:
    runtime = load_json_config(ROOT / "config/config.json")
    strategy = load_json_config(ROOT / "config/strategy_config.json")
    e_spec = load_json_config(ROOT / "config/strategy_e_r1_scenarios.json")
    settings = load_settings(runtime, ROOT)
    checks = rule_checks(runtime, strategy, e_spec)
    if not all(checks.values()):
        raise RuntimeError(f"方案甲正式配置检查未通过：{[k for k, v in checks.items() if not v]}")

    monthly = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    paths = monthly_paths(monthly, CUTOFF)
    window = build_monthly_research_window(CUTOFF)
    context = _context(
        window=window,
        feature_path=paths["strict_feature_pool"],
        sentiment_path=paths["market_sentiment"],
        d_event_path=paths["d_event_source"],
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=int(monthly["market_controller"]["minimum_limit_up_count"]),
    )
    execution = _execution_kwargs(monthly)

    # ① 影子净值：同一份定义，从history_start回放到截止日。
    with tempfile.TemporaryDirectory(prefix="jia_shadow_") as work:
        nav = build_shadow_nav(
            project_root=ROOT,
            feature_path=paths["strict_feature_pool"],
            sentiment_path=paths["market_sentiment"],
            calendar_path=paths["trade_calendar"],
            start=settings.history_start,
            end=CUTOFF,
            work_dir=Path(work),
        )
    nav_dates = [str(d) for d in nav.index]
    allowed_all = allowed_action_dates(
        nav_dates, nav.to_numpy(), ma_window=settings.ma_window, lag=settings.lag_trading_days
    )
    # ② 实盘“收盘后预判下一个行动日”与认证“逐日判定”在真实影子净值上逐日一致。
    window_dates = [str(d) for d in context["action_dates"]]
    live_mismatch = []
    for date in window_dates:
        index = nav_dates.index(date)
        live = decision_for_next_action_date(
            nav_dates[:index], nav.to_numpy()[:index], date,
            ma_window=settings.ma_window, lag=settings.lag_trading_days,
        )
        if bool(live["allowed"]) != (date in allowed_all):
            live_mismatch.append(date)
    if live_mismatch:
        raise RuntimeError(f"实盘判定与认证逐日判定不一致：{live_mismatch[:5]}")
    stop_dates = [d for d in window_dates if d not in allowed_all]

    # ③ 正式计划（A/C/E来自方案甲正式配置；D历史失败关闭=0笔），两次独立构建。
    baselines, _candidates = _variant_sets()

    def build() -> dict[str, pd.DataFrame]:
        cache = StaticOutcomeCache()
        plans = {
            leg: build_variant_plan(
                baselines[leg],
                signal_pool=context["signal_pool"],
                d_events=context["d_events"],
                allowed_action_dates=context["allowed_actions"],
                cutoff=CUTOFF,
                outcome_cache=cache,
            )
            for leg in ("A", "C", "E")
        }
        plans["D"] = pd.DataFrame()
        return plans

    first, second = build(), build()
    deterministic_plans = all(
        plan_signature(first[leg]) == plan_signature(second[leg]) for leg in ("A", "C", "E")
    )

    def replay(plans: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
        return replay_action_date_cash_portfolio(
            plans, action_dates=context["action_dates"], priority=FIXED_PRIORITY, **execution
        )

    gated_detail = replay(filter_plans_to_allowed_dates(first, allowed_all))
    gated_detail_again = replay(filter_plans_to_allowed_dates(second, allowed_all))
    ungated_detail = replay(first)
    gated = action_metrics(gated_detail, window.start, window.end)
    gated_again = action_metrics(gated_detail_again, window.start, window.end)
    ungated = action_metrics(ungated_detail, window.start, window.end)
    deterministic = deterministic_plans and close_enough(gated_again, gated)
    if not deterministic:
        raise RuntimeError("两次独立构建/回放结果不一致")

    # 停手日真的没有任何新开仓。
    executed = gated_detail[gated_detail["status"].astype(str).eq("EXECUTED")]
    no_entry_on_stop_days = not executed["action_date"].astype(str).isin(stop_dates).any()
    if not no_entry_on_stop_days:
        raise RuntimeError("停手日仍有新开仓")

    summary = {
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "window": {"start": window.start, "end": window.end, "trade_days": len(window_dates)},
        "method_id": METHOD_ID,
        "stop_parameters": {"ma_window": settings.ma_window, "lag_trading_days": settings.lag_trading_days,
                            "history_start": settings.history_start},
        "stop_days_in_window": len(stop_dates),
        "stop_day_ratio_in_window": len(stop_dates) / len(window_dates),
        "gated_combo_metrics": json_safe(gated),
        "ungated_combo_metrics_same_rules": json_safe(ungated),
        "plan_counts": {leg: int(len(first[leg])) for leg in ("A", "C", "E")},
        "plan_signatures": {leg: plan_signature(first[leg]) for leg in ("A", "C", "E")},
        "d_historical_policy": "FAIL_CLOSED_ZERO_TRADES_NO_SIGNAL_TIME_L2",
        "rule_checks": checks,
        "live_decision_equals_certification_every_window_day": True,
        "no_entry_on_stop_days": True,
        "deterministic_double_build_and_replay": True,
        "baseline_v16_d_fail_closed": runtime["portfolio_certification"].get("live_candidate_metrics", {}),
    }
    if measure_only:
        return summary

    if EXPECTED_GATED_COMBO is None or EXPECTED_STOP_DAYS_IN_WINDOW is None:
        raise RuntimeError("期望指标尚未锁定：先用--measure-only审核，再把数字写入脚本常量")
    metrics_match = close_enough(gated, EXPECTED_GATED_COMBO) and len(stop_dates) == EXPECTED_STOP_DAYS_IN_WINDOW
    if not metrics_match:
        raise RuntimeError("方案甲组合指标与锁定值不一致，拒绝写出PASS证书")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for leg in ("A", "C", "E"):
        first[leg].to_csv(OUTPUT_ROOT / f"{leg.lower()}_plans.csv", index=False, encoding="utf-8-sig")
    gated_detail.to_csv(OUTPUT_ROOT / "combo_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame({"action_date": nav_dates, "shadow_nav": nav.to_numpy(),
                  "allowed": [d in allowed_all for d in nav_dates]}).to_csv(
        OUTPUT_ROOT / "shadow_nav_and_gate.csv", index=False, encoding="utf-8-sig")
    write_json(OUTPUT_ROOT / "release_summary.json", json_safe(summary))
    artifacts = sorted(p for p in OUTPUT_ROOT.iterdir() if p.is_file() and p.name != "artifact_manifest.json")
    write_json(OUTPUT_ROOT / "artifact_manifest.json",
               {p.name: {"sha256": sha256_path(p), "size_bytes": p.stat().st_size} for p in artifacts})

    input_files = [
        str(Path(paths[key]).relative_to(ROOT))
        for key in ("strict_feature_pool", "market_sentiment", "trade_calendar")
    ] + [str((OUTPUT_ROOT / name).relative_to(ROOT)) for name in ("a_plans.csv", "c_plans.csv", "e_plans.csv", "combo_trades.csv", "shadow_nav_and_gate.csv")]
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "certification_scope": "FORMAL_RULE_STOP_GATE_AND_BACKTEST_ALIGNMENT",
        "current_executable": True,
        "release_eligible": False,
        "strict_asof_standard_id": runtime["strict_asof"]["standard_id"],
        "strict_asof_passed": True,
        "research_protocol": "STRICT_DISCOVERY_POST_REVIEW_USER_ACCEPTED_20260919",
        "independent_oos_certified": False,
        "capacity_certified": False,
        "window": summary["window"],
        "rule_checks": checks,
        "combo_metrics_match": True,
        "combo_metrics": json_safe(gated),
        "stop_days_in_window": len(stop_dates),
        "deterministic_double_replay": True,
        "live_decision_equals_certification": True,
        "config_sha256": certification_config_sha256(runtime),
        "code_files": CODE_FILES,
        "code_sha256": certification_files_sha256(ROOT, CODE_FILES),
        "input_files": input_files,
        "input_sha256": certification_files_sha256(ROOT, input_files),
        "source_summary_sha256": certification_file_sha256(OUTPUT_ROOT / "release_summary.json"),
        "risk_note": (
            "认证只证明方案甲正式规则、停手门禁与回测逻辑逐笔对齐；2023-09~2026-08属于同窗研究，"
            "2018-04~2026-09连续样本外研究见docs/equity_curve_stop.md，均不承诺未来收益。"
            "D历史按0笔失败关闭，实盘D另受实时门禁约束。"
        ),
    }
    write_json(LIVE_CERTIFICATION_PATH, payload)
    LIVE_REPORT_PATH.write_text(
        "\n".join([
            "# 方案甲正式规则与停手门禁对齐报告",
            "",
            "- 状态：PASS",
            f"- 场景：{SCENARIO}",
            f"- 窗口：{window.start}～{window.end}（{len(window_dates)}个交易日，其中停手{len(stop_dates)}天）",
            f"- 组合（含停手）：{gated['trade_count']}笔，胜率{gated['win_rate']:.2%}，复利{gated['equity_multiple']:.6f}倍，最大回撤{gated['max_drawdown']:.2%}",
            f"- 同规则不停手：{ungated['trade_count']}笔，复利{ungated['equity_multiple']:.6f}倍，最大回撤{ungated['max_drawdown']:.2%}",
            f"- 分腿：{gated.get('leg_counts')}",
            "- 实盘逐日预判与认证逐日判定：一致；停手日无新开仓；两次确定性构建与回放：一致",
            "",
            "风险说明：该窗口属于同窗研究，不是独立样本外或资金容量认证，也不承诺未来收益。",
        ]) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    args = parse_args()
    print(json.dumps(json_safe(run(args.measure_only)), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
