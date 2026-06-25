"""
搜索 model=3 的 L 替换持仓占用保护规则。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

背景：
  安全候选规则唯一失败窗口显示，model=3 跑输不是因为 L 单笔明显差，
  而是 L 持仓期间错过 mode=1 的正收益交易。

本脚本验证两类规则：
  1. oracle_no_missed_mode1：使用未来信息的理论上限，只用来确认问题是否可被解决。
  2. T 日可见字段代理规则：只用 L 信号当天已有字段过滤替换交易，避免实盘不可用。

输出：
  reports/strategy_model3/occupancy_guards/model3_occupancy_guard_summary.csv
  reports/strategy_model3/occupancy_guards/model3_occupancy_guard_tests.csv
  reports/strategy_model3/occupancy_guards/model3_occupancy_guard_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (  # noqa: E402
    SwitchAtom,
    build_l_lookup,
    calc_metrics,
    infer_mode1_position_until,
    l_trade_return,
    load_baseline_daily,
    selected_l2_source,
)
from scripts.search_strategy_model3_safe_modes import (  # noqa: E402
    SafePolicy,
    build_policies,
    load_robust_rule,
    markdown_table,
)
from scripts.validate_strategy_model3_switch import build_splits, mode1_metrics  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "occupancy_guards"


@dataclass(frozen=True)
class GuardRule:
    name: str
    description: str
    replace_predicate: Callable[[pd.Series], bool]
    oracle_no_missed_mode1: bool = False


def _text(row: pd.Series, column: str) -> str:
    return str(row.get(column, "") if column in row.index else "")


def _num(row: pd.Series, column: str, default: float = 0.0) -> float:
    value = pd.to_numeric(row.get(column, default), errors="coerce")
    return default if pd.isna(value) else float(value)


def _always(_row: pd.Series) -> bool:
    return True


def _main_rise(row: pd.Series) -> bool:
    return _text(row, "market_emotion_state_bucket") == "main_rise"


def _theme_limit_ge_2(row: pd.Series) -> bool:
    return _num(row, "theme_limit_count") >= 2


def _not_after_1430(row: pd.Series) -> bool:
    return _text(row, "first_time_detail_bucket") != "after_1430"


def _theme_ge_2_not_after_1430(row: pd.Series) -> bool:
    return _theme_limit_ge_2(row) and _not_after_1430(row)


def _market_chain_15_30(row: pd.Series) -> bool:
    return _text(row, "market_chain_count_bucket") == "15_30"


def _open_times_not_gte4(row: pd.Series) -> bool:
    return _text(row, "open_times_bucket") != "gte_4"


def _main_rise_or_theme_ge_2(row: pd.Series) -> bool:
    return _main_rise(row) or _theme_limit_ge_2(row)


def build_guard_rules() -> list[GuardRule]:
    return [
        GuardRule(
            name="chi_next_base",
            description="当前候选：L为创业板时允许替换。",
            replace_predicate=_always,
        ),
        GuardRule(
            name="oracle_no_missed_mode1",
            description="理论上限：若L持仓期会错过mode=1交易，则不替换。使用未来信息，不能实盘。",
            replace_predicate=_always,
            oracle_no_missed_mode1=True,
        ),
        GuardRule(
            name="replace_main_rise_only",
            description="实盘可用代理：只在市场情绪 main_rise 时允许替换。",
            replace_predicate=_main_rise,
        ),
        GuardRule(
            name="replace_theme_limit_ge_2",
            description="实盘可用代理：只在题材涨停数>=2时允许替换。",
            replace_predicate=_theme_limit_ge_2,
        ),
        GuardRule(
            name="replace_not_after_1430",
            description="实盘可用代理：排除尾盘首次涨停 after_1430。",
            replace_predicate=_not_after_1430,
        ),
        GuardRule(
            name="replace_theme_ge_2_not_after_1430",
            description="实盘可用代理：题材涨停数>=2且排除 after_1430。",
            replace_predicate=_theme_ge_2_not_after_1430,
        ),
        GuardRule(
            name="replace_market_chain_15_30",
            description="实盘可用代理：只在市场连板数量 15_30 时允许替换。",
            replace_predicate=_market_chain_15_30,
        ),
        GuardRule(
            name="replace_open_times_not_gte4",
            description="实盘可用代理：排除炸板次数 gte_4。",
            replace_predicate=_open_times_not_gte4,
        ),
        GuardRule(
            name="replace_main_rise_or_theme_ge_2",
            description="实盘可用代理：main_rise 或题材涨停数>=2 时允许替换。",
            replace_predicate=_main_rise_or_theme_ge_2,
        ),
    ]


def build_chi_next_base_rule() -> tuple[SwitchAtom, ...]:
    _rule_text, robust_rule = load_robust_rule()
    policies = build_policies(robust_rule)
    selected = [p for p in policies if p.name == "idle_plus_replace_chi_next"]
    if not selected:
        raise ValueError("找不到 idle_plus_replace_chi_next 候选策略")
    return selected[0].base_rule


def _base_rule_ok(l_row: pd.Series | None, rule: tuple[SwitchAtom, ...]) -> bool:
    return bool(l_row is not None and all(atom.predicate(l_row) for atom in rule))


def _mode1_has_trade(row: pd.Series) -> bool:
    return abs(float(row.get("mode1_return", 0.0) or 0.0)) > 1e-12


def _mode1_is_idle(row: pd.Series) -> bool:
    status = str(row.get("mode1_operation_status", ""))
    return not _mode1_has_trade(row) and status == "NO_CANDIDATE"


def _has_mode1_trade_between(baseline: pd.DataFrame, start_date: str, exit_date: str) -> bool:
    if not exit_date or exit_date == "99991231":
        return True
    mask = (
        baseline["date"].astype(str).gt(start_date)
        & baseline["date"].astype(str).lt(exit_date)
        & baseline["mode1_return"].abs().gt(1e-12)
    )
    return bool(mask.any())


def replay_guard(
    baseline: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    base_rule: tuple[SwitchAtom, ...],
    guard: GuardRule,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    occupied_until = ""
    occupied_by = ""

    for i, row in baseline.iterrows():
        date = str(row["date"])
        if occupied_until and date < occupied_until:
            rows.append({
                **row.to_dict(),
                "model3_return": 0.0,
                "model3_op": f"POSITION_OCCUPIED_BY_{occupied_by}",
                "model3_reason": f"上一笔{occupied_by}持仓到{occupied_until}释放",
                "guard_rule": guard.name,
                "conflict_type": "",
                "mode1_vs_model3_return_diff": None,
            })
            continue

        l_row = l_lookup.get(date)
        base_ok = _base_rule_ok(l_row, base_rule)
        mode1_has_trade = _mode1_has_trade(row)
        idle = _mode1_is_idle(row)
        replace_allowed = False
        if mode1_has_trade and l_row is not None:
            replace_allowed = _text(l_row, "market_segment") == "chi_next" and guard.replace_predicate(l_row)

        choose_l = base_ok and ((idle) or replace_allowed)
        if choose_l and l_row is not None:
            ok, account_return, exit_date, status = l_trade_return(l_row)
            if guard.oracle_no_missed_mode1 and mode1_has_trade and ok:
                if _has_mode1_trade_between(baseline, date, exit_date):
                    choose_l = False
            if choose_l and ok:
                occupied_until = exit_date
                occupied_by = "L"
                conflict_type = "L_REPLACE_MODE1_RETURN_DAY" if mode1_has_trade else "L_SUPPLEMENT_MODE1_IDLE"
                rows.append({
                    **row.to_dict(),
                    "model3_return": account_return,
                    "model3_op": "L",
                    "model3_reason": guard.description,
                    "guard_rule": guard.name,
                    "conflict_type": conflict_type,
                    "l_ts_code": l_row.get("ts_code", ""),
                    "l_name": l_row.get("name", ""),
                    "l_exit_date": exit_date,
                    "l_status": status,
                    "mode1_vs_model3_return_diff": account_return - float(row["mode1_return"]),
                })
                continue
            if choose_l and not ok:
                rows.append({
                    **row.to_dict(),
                    "model3_return": 0.0,
                    "model3_op": status,
                    "model3_reason": "L规则触发但实盘约束未成交/未卖出",
                    "guard_rule": guard.name,
                    "conflict_type": "L_SKIP",
                    "l_ts_code": l_row.get("ts_code", ""),
                    "l_name": l_row.get("name", ""),
                    "l_exit_date": exit_date,
                    "l_status": status,
                    "mode1_vs_model3_return_diff": -float(row["mode1_return"]),
                })
                continue

        mode1_return = float(row["mode1_return"])
        op = "MODE1" if abs(mode1_return) > 1e-12 else "NO_TRADE"
        if op == "MODE1":
            occupied_until = infer_mode1_position_until(baseline, i)
            occupied_by = "MODE1"
        rows.append({
            **row.to_dict(),
            "model3_return": mode1_return,
            "model3_op": op,
            "model3_reason": "不满足占用保护切换条件，使用mode=1",
            "guard_rule": guard.name,
            "conflict_type": "NOT_L_DAY",
            "mode1_vs_model3_return_diff": 0.0,
        })

    daily = pd.DataFrame(rows)
    metrics = calc_metrics(guard.name, daily, "model3_return", "model3_op")
    metrics["guard_rule"] = guard.name
    metrics["description"] = guard.description
    metrics["l_supplement_count"] = int((daily["conflict_type"] == "L_SUPPLEMENT_MODE1_IDLE").sum())
    metrics["l_replace_count"] = int((daily["conflict_type"] == "L_REPLACE_MODE1_RETURN_DAY").sum())
    metrics["total_return_diff"] = float(pd.to_numeric(daily["mode1_vs_model3_return_diff"], errors="coerce").fillna(0.0).sum())
    return daily, metrics


def evaluate_on_splits(
    guard: GuardRule,
    splits: list[tuple[str, pd.DataFrame, pd.DataFrame, int]],
    l_lookup: dict[str, pd.Series],
    base_rule: tuple[SwitchAtom, ...],
) -> tuple[dict[str, Any], pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for split_name, _train, test, _min_l_trades in splits:
        daily, metrics = replay_guard(test, l_lookup, base_rule, guard)
        mode1 = mode1_metrics(test, f"{split_name}:mode1")
        rows.append({
            "guard_rule": guard.name,
            "split": split_name,
            "test_start": str(test["date"].min()),
            "test_end": str(test["date"].max()),
            "model3_equity_multiple": metrics["equity_multiple"],
            "mode1_equity_multiple": mode1["equity_multiple"],
            "multiple_ratio": float(metrics["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12),
            "model3_max_drawdown": metrics["max_drawdown"],
            "mode1_max_drawdown": mode1["max_drawdown"],
            "dd_improvement": float(metrics["max_drawdown"]) - float(mode1["max_drawdown"]),
            "model3_l_trade_count": metrics["l_trade_count"],
            "model3_l_replace_count": metrics["l_replace_count"],
            "model3_l_supplement_count": metrics["l_supplement_count"],
        })
    detail = pd.DataFrame(rows)
    summary = {
        "guard_rule": guard.name,
        "split_count": int(len(detail)),
        "pass_multiple_count": int((detail["multiple_ratio"] > 1.0).sum()),
        "pass_drawdown_count": int((detail["dd_improvement"] >= 0.0).sum()),
        "pass_both_count": int(((detail["multiple_ratio"] > 1.0) & (detail["dd_improvement"] >= 0.0)).sum()),
        "avg_multiple_ratio": float(detail["multiple_ratio"].mean()),
        "median_multiple_ratio": float(detail["multiple_ratio"].median()),
        "min_multiple_ratio": float(detail["multiple_ratio"].min()),
        "avg_dd_improvement": float(detail["dd_improvement"].mean()),
        "min_dd_improvement": float(detail["dd_improvement"].min()),
    }
    return summary, detail


def write_report(summary: pd.DataFrame, tests: pd.DataFrame) -> None:
    cols = [
        "guard_rule",
        "description",
        "equity_multiple",
        "max_drawdown",
        "trade_count",
        "l_trade_count",
        "l_replace_count",
        "l_supplement_count",
        "pass_both_count",
        "min_multiple_ratio",
        "total_return_diff",
    ]
    cols = [col for col in cols if col in summary.columns]
    test_cols = [
        "guard_rule",
        "split",
        "multiple_ratio",
        "dd_improvement",
        "model3_l_trade_count",
        "model3_l_replace_count",
    ]
    test_cols = [col for col in test_cols if col in tests.columns]
    lines = [
        "# model=3 L替换持仓占用保护规则搜索",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
        "## 规则汇总",
        "",
        markdown_table(summary[cols]),
        "",
        "## 分切分表现",
        "",
        markdown_table(tests[test_cols]),
        "",
        "## 解释",
        "",
        "- `oracle_no_missed_mode1` 使用未来信息，只能作为理论上限，不能实盘。",
        "- 实盘可用代理规则只能使用 T 日已知字段；如果收益明显下降，说明占用风险暂时无法靠简单字段稳定解决。",
        "- 若没有实盘可用规则同时提高复利和降低失败切分风险，应保持 model=3 研究状态，不接实盘。",
    ]
    (OUTPUT_DIR / "model3_occupancy_guard_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    base_rule = build_chi_next_base_rule()
    splits = build_splits(baseline)

    summary_rows: list[dict[str, Any]] = []
    test_frames: list[pd.DataFrame] = []
    for guard in build_guard_rules():
        daily, metrics = replay_guard(baseline, l_lookup, base_rule, guard)
        split_summary, split_detail = evaluate_on_splits(guard, splits, l_lookup, base_rule)
        metrics.update(split_summary)
        summary_rows.append(metrics)
        test_frames.append(split_detail)
        daily.to_csv(OUTPUT_DIR / f"model3_occupancy_guard_{guard.name}_daily.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows).sort_values(
        ["pass_both_count", "min_multiple_ratio", "equity_multiple", "max_drawdown"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    tests = pd.concat(test_frames, ignore_index=True) if test_frames else pd.DataFrame()

    summary.to_csv(OUTPUT_DIR / "model3_occupancy_guard_summary.csv", index=False, encoding="utf-8-sig")
    tests.to_csv(OUTPUT_DIR / "model3_occupancy_guard_tests.csv", index=False, encoding="utf-8-sig")
    write_report(summary, tests)

    print("model=3 L替换持仓占用保护规则搜索完成")
    print(summary[[
        "guard_rule",
        "equity_multiple",
        "max_drawdown",
        "l_trade_count",
        "l_replace_count",
        "pass_both_count",
        "min_multiple_ratio",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
