"""
搜索 model=3 安全切换方案。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

背景：
  已有稳健规则能提升全样本收益，但逐日冲突复盘发现：
  - L 在 mode=1 空闲日补位的贡献较好。
  - L 替换 mode=1 有交易的日期时，平均改善很小，并且存在大幅拖累案例。

本脚本把“补位”和“替换”拆开回放，验证更安全的 model=3 方案：
  1. 只允许 L 在 mode=1 空闲日补位。
  2. 空闲日补位 + 替换时增加更强过滤。
  3. 对每个方案做全样本收益、回撤、冲突贡献、分切分稳健性统计。

输出：
  reports/strategy_model3/safe_modes/model3_safe_modes_summary.csv
  reports/strategy_model3/safe_modes/model3_safe_modes_detail.csv
  reports/strategy_model3/safe_modes/model3_safe_modes_conflicts.csv
  reports/strategy_model3/safe_modes/model3_safe_modes_tests.csv
  reports/strategy_model3/safe_modes/model3_safe_modes_report.md
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
    build_switch_atoms,
    calc_metrics,
    infer_mode1_position_until,
    l_trade_return,
    load_baseline_daily,
    selected_l2_source,
)
from scripts.validate_strategy_model3_switch import (  # noqa: E402
    build_splits,
    mode1_metrics,
    parse_rule,
)


ROBUST_RULES_PATH = PROJECT_ROOT / "reports" / "strategy_model3" / "robust" / "model3_robust_rules.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "safe_modes"


@dataclass(frozen=True)
class SafePolicy:
    """model=3 安全切换策略。

    base_rule:
      L 必须先通过的稳健基础条件。
    allow_idle:
      mode=1 当天没有交易收益且状态是 NO_CANDIDATE 时，是否允许 L 补位。
    allow_other_no_trade:
      mode=1 当天没有交易收益但状态不是 NO_CANDIDATE 时，是否允许 L 切换。
      只有 robust_all 用来复现旧口径，安全版默认关闭。
    replace_predicate:
      mode=1 当天本来有交易时，是否允许 L 替换。
      这里不能使用未来收益，只能用 T 日 L 信号行里已经可见的市场、板块、题材字段。
    """

    name: str
    description: str
    base_rule: tuple[SwitchAtom, ...]
    allow_idle: bool
    allow_other_no_trade: bool
    replace_predicate: Callable[[pd.Series], bool]


def _text(row: pd.Series, column: str) -> str:
    return str(row.get(column, "") if column in row.index else "")


def _always(_row: pd.Series) -> bool:
    return True


def _never(_row: pd.Series) -> bool:
    return False


def _not_sh_main(row: pd.Series) -> bool:
    return _text(row, "market_segment") != "sh_main"


def _not_ice_point(row: pd.Series) -> bool:
    return _text(row, "market_emotion_state_bucket") != "ice_point"


def _chi_next(row: pd.Series) -> bool:
    return _text(row, "market_segment") == "chi_next"


def _chi_next_not_ice_point(row: pd.Series) -> bool:
    return _chi_next(row) and _not_ice_point(row)


def _not_sh_main_not_ice_point(row: pd.Series) -> bool:
    return _not_sh_main(row) and _not_ice_point(row)


def _main_rise_or_warming(row: pd.Series) -> bool:
    return _text(row, "market_emotion_state_bucket") in {"warming", "main_rise"}


def load_robust_rule() -> tuple[str, tuple[SwitchAtom, ...]]:
    if not ROBUST_RULES_PATH.exists():
        raise FileNotFoundError(f"找不到稳健规则结果: {ROBUST_RULES_PATH}")
    rules = pd.read_csv(ROBUST_RULES_PATH, low_memory=False)
    if rules.empty:
        raise ValueError("稳健规则结果为空")
    text = str(rules.iloc[0]["rule"])
    atom_map = {atom.name: atom for atom in build_switch_atoms()}
    parsed = parse_rule(text, atom_map)
    if parsed is None:
        raise ValueError(f"无法解析稳健规则: {text}")
    return text, parsed


def build_policies(base_rule: tuple[SwitchAtom, ...]) -> list[SafePolicy]:
    return [
        SafePolicy(
            name="mode1_only",
            description="只使用当前 mode=1，不启用 L。",
            base_rule=tuple(),
            allow_idle=False,
            allow_other_no_trade=False,
            replace_predicate=_never,
        ),
        SafePolicy(
            name="robust_all",
            description="稳健规则全量切换：通过基础条件就允许 L 替换或补位。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=True,
            replace_predicate=_always,
        ),
        SafePolicy(
            name="idle_only",
            description="安全补位版：只有 mode=1 当天无候选/无收益时，才允许 L 补位。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_never,
        ),
        SafePolicy(
            name="idle_plus_replace_not_sh_main",
            description="补位 + 替换时排除沪市主板。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_not_sh_main,
        ),
        SafePolicy(
            name="idle_plus_replace_not_ice_point",
            description="补位 + 替换时排除冰点环境。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_not_ice_point,
        ),
        SafePolicy(
            name="idle_plus_replace_chi_next",
            description="补位 + 替换时只允许创业板。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_chi_next,
        ),
        SafePolicy(
            name="idle_plus_replace_chi_next_not_ice_point",
            description="补位 + 替换时只允许创业板且不是冰点环境。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_chi_next_not_ice_point,
        ),
        SafePolicy(
            name="idle_plus_replace_not_sh_main_not_ice_point",
            description="补位 + 替换时排除沪市主板且排除冰点环境。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_not_sh_main_not_ice_point,
        ),
        SafePolicy(
            name="idle_plus_replace_warming_main_rise",
            description="补位 + 替换时只允许 warming/main_rise 情绪。",
            base_rule=base_rule,
            allow_idle=True,
            allow_other_no_trade=False,
            replace_predicate=_main_rise_or_warming,
        ),
    ]


def _base_rule_ok(l_row: pd.Series | None, rule: tuple[SwitchAtom, ...]) -> bool:
    return bool(l_row is not None and all(atom.predicate(l_row) for atom in rule))


def _mode1_has_trade(row: pd.Series) -> bool:
    return abs(float(row.get("mode1_return", 0.0) or 0.0)) > 1e-12


def _mode1_is_idle(row: pd.Series) -> bool:
    status = str(row.get("mode1_operation_status", ""))
    return not _mode1_has_trade(row) and status == "NO_CANDIDATE"


def replay_safe_policy(
    baseline: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    policy: SafePolicy,
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
                "safe_policy": policy.name,
            })
            continue

        l_row = l_lookup.get(date)
        base_ok = _base_rule_ok(l_row, policy.base_rule)
        mode1_has_trade = _mode1_has_trade(row)
        idle = _mode1_is_idle(row)
        allow_supplement = policy.allow_idle and idle
        allow_other_no_trade = policy.allow_other_no_trade and not mode1_has_trade and not idle
        allow_replace = mode1_has_trade and l_row is not None and policy.replace_predicate(l_row)
        choose_l = base_ok and (allow_supplement or allow_other_no_trade or allow_replace)

        if choose_l and l_row is not None:
            ok, account_return, exit_date, status = l_trade_return(l_row)
            if mode1_has_trade:
                conflict_type = "L_REPLACE_MODE1_RETURN_DAY"
            elif idle:
                conflict_type = "L_SUPPLEMENT_MODE1_IDLE"
            else:
                conflict_type = "L_OTHER_NO_TRADE_STATUS"
            if ok:
                occupied_until = exit_date
                occupied_by = "L"
                rows.append({
                    **row.to_dict(),
                    "model3_return": account_return,
                    "model3_op": "L",
                    "model3_reason": policy.description,
                    "safe_policy": policy.name,
                    "conflict_type": conflict_type,
                    "l_ts_code": l_row.get("ts_code", ""),
                    "l_name": l_row.get("name", ""),
                    "l_exit_date": exit_date,
                    "l_status": status,
                    "mode1_vs_model3_return_diff": account_return - float(row["mode1_return"]),
                })
            else:
                rows.append({
                    **row.to_dict(),
                    "model3_return": 0.0,
                    "model3_op": status,
                    "model3_reason": "L规则触发但实盘约束未成交/未卖出",
                    "safe_policy": policy.name,
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
            "model3_reason": "不满足安全切换条件，使用mode=1",
            "safe_policy": policy.name,
            "conflict_type": "NOT_L_DAY",
            "mode1_vs_model3_return_diff": 0.0,
        })

    daily = pd.DataFrame(rows)
    metrics = calc_metrics(policy.name, daily, "model3_return", "model3_op")
    metrics["policy"] = policy.name
    metrics["description"] = policy.description
    metrics["l_skip_count"] = int(daily["model3_op"].astype(str).str.startswith("L_SKIP").sum())
    metrics["l_supplement_count"] = int((daily["conflict_type"] == "L_SUPPLEMENT_MODE1_IDLE").sum())
    metrics["l_replace_count"] = int((daily["conflict_type"] == "L_REPLACE_MODE1_RETURN_DAY").sum())
    metrics["total_return_diff"] = float(pd.to_numeric(daily["mode1_vs_model3_return_diff"], errors="coerce").fillna(0.0).sum())
    return daily, metrics


def summarize_conflicts(daily: pd.DataFrame) -> pd.DataFrame:
    l_days = daily[daily["model3_op"].astype(str).eq("L")].copy()
    if l_days.empty:
        return pd.DataFrame(columns=[
            "safe_policy",
            "conflict_type",
            "count",
            "win_rate",
            "avg_l_return",
            "median_l_return",
            "total_return_diff",
            "avg_return_diff",
            "worst_l_return",
            "best_l_return",
        ])
    return (
        l_days.groupby(["safe_policy", "conflict_type"], dropna=False)
        .agg(
            count=("date", "count"),
            win_rate=("model3_return", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            avg_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            median_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").median())),
            total_return_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").sum())),
            avg_return_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            worst_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").min())),
            best_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").max())),
        )
        .reset_index()
    )


def evaluate_policy_on_splits(
    policy: SafePolicy,
    splits: list[tuple[str, pd.DataFrame, pd.DataFrame, int]],
    l_lookup: dict[str, pd.Series],
) -> tuple[dict[str, Any], pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for split_name, _train, test, _min_l_trades in splits:
        model3_daily, model3 = replay_safe_policy(test, l_lookup, policy)
        mode1 = mode1_metrics(test, f"{split_name}:mode1")
        ratio = float(model3["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12)
        dd_improvement = float(model3["max_drawdown"]) - float(mode1["max_drawdown"])
        rows.append({
            "policy": policy.name,
            "split": split_name,
            "test_start": str(test["date"].min()),
            "test_end": str(test["date"].max()),
            "model3_equity_multiple": model3["equity_multiple"],
            "mode1_equity_multiple": mode1["equity_multiple"],
            "multiple_ratio": ratio,
            "model3_max_drawdown": model3["max_drawdown"],
            "mode1_max_drawdown": mode1["max_drawdown"],
            "dd_improvement": dd_improvement,
            "model3_trade_count": model3["trade_count"],
            "model3_l_trade_count": model3["l_trade_count"],
            "model3_win_rate": model3["win_rate"],
        })
    detail = pd.DataFrame(rows)
    summary = {
        "policy": policy.name,
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


def markdown_table(df: pd.DataFrame) -> str:
    """不依赖 tabulate 的简易 Markdown 表格。

    本项目运行环境不一定安装 pandas 的可选依赖 tabulate，
    研究脚本报告应尽量自包含。
    """
    if df.empty:
        return "无结果。"
    view = df.copy()
    for col in view.columns:
        if pd.api.types.is_float_dtype(view[col]):
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.6g}")
        else:
            view[col] = view[col].fillna("").astype(str)
    headers = [str(col) for col in view.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in view.iterrows():
        values = [str(row[col]).replace("\n", " ") for col in view.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_report(
    rule_text: str,
    summary: pd.DataFrame,
    conflicts: pd.DataFrame,
    tests: pd.DataFrame,
    detail: pd.DataFrame,
) -> None:
    best = summary.iloc[0] if len(summary) else {}
    top_cols = [
        "policy",
        "description",
        "equity_multiple",
        "max_drawdown",
        "trade_count",
        "l_trade_count",
        "l_supplement_count",
        "l_replace_count",
        "pass_both_count",
        "min_multiple_ratio",
        "total_return_diff",
    ]
    top_cols = [col for col in top_cols if col in summary.columns]
    l_days = detail[detail["model3_op"].astype(str).eq("L")].copy()
    show_cols = [
        "date",
        "safe_policy",
        "l_ts_code",
        "l_name",
        "model3_return",
        "mode1_return",
        "mode1_vs_model3_return_diff",
        "conflict_type",
    ]
    show_cols = [col for col in show_cols if col in l_days.columns]
    lines = [
        "# model=3 安全切换方案搜索",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 使用的 L 基础稳健规则：{rule_text}",
    ]
    if len(summary):
        lines.extend([
            f"- 当前综合排序第一：{best['policy']}",
            f"- 全样本复利：{float(best['equity_multiple']):.2f}倍",
            f"- 最大回撤：{float(best['max_drawdown']):.2%}",
            f"- L交易数：{int(best['l_trade_count'])}",
            f"- 分切分复利和回撤同时通过：{int(best['pass_both_count'])}/{int(best['split_count'])}",
            "",
        ])
    lines.extend([
        "## 方案汇总",
        "",
        markdown_table(summary[top_cols]) if len(summary) else "无结果。",
        "",
        "## L冲突贡献",
        "",
        markdown_table(conflicts) if len(conflicts) else "无L交易。",
        "",
        "## 分切分稳健性",
        "",
        markdown_table(tests) if len(tests) else "无验证结果。",
        "",
        "## L切换拖累最大前20",
        "",
        markdown_table(l_days.sort_values("mode1_vs_model3_return_diff", ascending=True).head(20)[show_cols])
        if len(l_days) else "无L交易。",
        "",
        "## L切换改善最大前20",
        "",
        markdown_table(l_days.sort_values("mode1_vs_model3_return_diff", ascending=False).head(20)[show_cols])
        if len(l_days) else "无L交易。",
    ])
    (OUTPUT_DIR / "model3_safe_modes_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rule_text, base_rule = load_robust_rule()
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    splits = build_splits(baseline)
    policies = build_policies(base_rule)

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    conflict_frames: list[pd.DataFrame] = []
    test_summary_rows: list[dict[str, Any]] = []
    test_detail_frames: list[pd.DataFrame] = []

    for policy in policies:
        daily, metrics = replay_safe_policy(baseline, l_lookup, policy)
        split_summary, split_detail = evaluate_policy_on_splits(policy, splits, l_lookup)
        metrics.update(split_summary)
        summary_rows.append(metrics)
        detail_frames.append(daily)
        conflict_frames.append(summarize_conflicts(daily))
        test_summary_rows.append(split_summary)
        test_detail_frames.append(split_detail)

    summary = pd.DataFrame(summary_rows)
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    conflicts = pd.concat(conflict_frames, ignore_index=True) if conflict_frames else pd.DataFrame()
    tests = pd.DataFrame(test_summary_rows)
    test_detail = pd.concat(test_detail_frames, ignore_index=True) if test_detail_frames else pd.DataFrame()

    summary = summary.sort_values(
        [
            "pass_both_count",
            "pass_multiple_count",
            "pass_drawdown_count",
            "min_multiple_ratio",
            "equity_multiple",
            "max_drawdown",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)

    summary.to_csv(OUTPUT_DIR / "model3_safe_modes_summary.csv", index=False, encoding="utf-8-sig")
    detail.to_csv(OUTPUT_DIR / "model3_safe_modes_detail.csv", index=False, encoding="utf-8-sig")
    conflicts.to_csv(OUTPUT_DIR / "model3_safe_modes_conflicts.csv", index=False, encoding="utf-8-sig")
    tests.to_csv(OUTPUT_DIR / "model3_safe_modes_tests.csv", index=False, encoding="utf-8-sig")
    test_detail.to_csv(OUTPUT_DIR / "model3_safe_modes_test_detail.csv", index=False, encoding="utf-8-sig")
    write_report(rule_text, summary, conflicts, tests, detail)

    print("model=3 安全切换方案搜索完成")
    print(f"基础稳健规则: {rule_text}")
    print(summary[[
        "policy",
        "equity_multiple",
        "max_drawdown",
        "l_trade_count",
        "l_supplement_count",
        "l_replace_count",
        "pass_both_count",
        "min_multiple_ratio",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
