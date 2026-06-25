"""
搜索 model=3 稳健切换规则。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

和 validate_strategy_model3_switch.py 的区别：
  - validate 脚本模拟“训练集选规则 -> 测试集验证”。
  - 本脚本把候选规则固定下来，逐条跑所有年份/滚动测试，
    看哪条规则跨区间更稳健。

筛选目标不再是全样本最高复利，而是：
  - 多数测试切分复利不低于 mode=1。
  - 多数测试切分回撤不差于 mode=1。
  - 最差测试复利比不能太低。
  - L 切换次数不能太少，避免偶然样本。

输出：
  reports/strategy_model3/robust/model3_robust_rules.csv
  reports/strategy_model3/robust/model3_robust_rule_tests.csv
  reports/strategy_model3/robust/model3_robust_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (  # noqa: E402
    SwitchAtom,
    build_l_lookup,
    build_switch_atoms,
    calc_metrics,
    load_baseline_daily,
    replay_model3,
    selected_l2_source,
)
from scripts.validate_strategy_model3_switch import (  # noqa: E402
    build_candidate_rules,
    build_splits,
    mode1_metrics,
    rule_text,
)


OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "robust"


def evaluate_rule_on_splits(
    rule: tuple[SwitchAtom, ...],
    splits: list[tuple[str, pd.DataFrame, pd.DataFrame, int]],
    l_lookup: dict[str, pd.Series],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """固定一条规则，跑所有测试切分。

    注意：这里只在每个 split 的 test 段上评价，不重新训练、不重新选规则。
    """
    rows: list[dict[str, Any]] = []
    text = rule_text(rule)
    for split_name, _train, test, _min_l_trades in splits:
        model3_daily, model3_metrics = replay_model3(test, l_lookup, rule)
        mode1 = mode1_metrics(test, f"{split_name}:mode1")
        ratio = float(model3_metrics["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12)
        dd_improvement = float(model3_metrics["max_drawdown"]) - float(mode1["max_drawdown"])
        rows.append({
            "rule": text,
            "split": split_name,
            "test_start": str(test["date"].min()),
            "test_end": str(test["date"].max()),
            "model3_equity_multiple": model3_metrics["equity_multiple"],
            "mode1_equity_multiple": mode1["equity_multiple"],
            "multiple_ratio": ratio,
            "model3_max_drawdown": model3_metrics["max_drawdown"],
            "mode1_max_drawdown": mode1["max_drawdown"],
            "dd_improvement": dd_improvement,
            "model3_trade_count": model3_metrics["trade_count"],
            "model3_l_trade_count": model3_metrics["l_trade_count"],
            "model3_win_rate": model3_metrics["win_rate"],
        })
    detail = pd.DataFrame(rows)
    summary = {
        "rule": text,
        "split_count": int(len(detail)),
        "pass_multiple_count": int((detail["multiple_ratio"] > 1.0).sum()),
        "pass_drawdown_count": int((detail["dd_improvement"] >= 0.0).sum()),
        "pass_both_count": int(((detail["multiple_ratio"] > 1.0) & (detail["dd_improvement"] >= 0.0)).sum()),
        "avg_multiple_ratio": float(detail["multiple_ratio"].mean()),
        "median_multiple_ratio": float(detail["multiple_ratio"].median()),
        "min_multiple_ratio": float(detail["multiple_ratio"].min()),
        "avg_dd_improvement": float(detail["dd_improvement"].mean()),
        "min_dd_improvement": float(detail["dd_improvement"].min()),
        "total_l_trade_count": int(detail["model3_l_trade_count"].sum()),
        "avg_l_trade_count": float(detail["model3_l_trade_count"].mean()),
    }
    return summary, detail


def full_sample_metrics(
    rule: tuple[SwitchAtom, ...],
    baseline: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
) -> dict[str, Any]:
    model3_daily, model3 = replay_model3(baseline, l_lookup, rule)
    mode1 = mode1_metrics(baseline, "mode1_full")
    return {
        "full_model3_equity_multiple": model3["equity_multiple"],
        "full_model3_max_drawdown": model3["max_drawdown"],
        "full_model3_trade_count": model3["trade_count"],
        "full_model3_l_trade_count": model3["l_trade_count"],
        "full_mode1_equity_multiple": mode1["equity_multiple"],
        "full_mode1_max_drawdown": mode1["max_drawdown"],
        "full_multiple_ratio": float(model3["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12),
        "full_dd_improvement": float(model3["max_drawdown"]) - float(mode1["max_drawdown"]),
    }


def write_report(summary: pd.DataFrame, detail: pd.DataFrame) -> None:
    report_path = OUTPUT_DIR / "model3_robust_report.md"
    top = summary.iloc[0] if not summary.empty else {}
    lines = [
        "# model=3 稳健切换规则搜索",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
    ]
    if len(summary):
        lines.extend([
            f"- 当前最稳健规则：{top['rule']}",
            f"- 测试切分通过复利次数：{int(top['pass_multiple_count'])}/{int(top['split_count'])}",
            f"- 测试切分通过回撤次数：{int(top['pass_drawdown_count'])}/{int(top['split_count'])}",
            f"- 复利和回撤同时通过次数：{int(top['pass_both_count'])}/{int(top['split_count'])}",
            f"- 最差测试复利比：{float(top['min_multiple_ratio']):.2f}",
            f"- 全样本复利：{float(top['full_model3_equity_multiple']):.2f}倍",
            f"- 全样本最大回撤：{float(top['full_model3_max_drawdown']):.2%}",
            "",
        ])
    lines.extend([
        "## 稳健规则前20",
        "",
        summary.head(20).to_markdown(index=False) if not summary.empty else "无结果。",
        "",
        "## 第一名规则分切分表现",
        "",
    ])
    if len(summary):
        best_rule = str(summary.iloc[0]["rule"])
        lines.append(detail[detail["rule"].astype(str).eq(best_rule)].to_markdown(index=False))
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    atoms = build_switch_atoms()
    rules = build_candidate_rules(atoms, limit=200)
    splits = build_splits(baseline)

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    for rule in rules:
        row, detail = evaluate_rule_on_splits(rule, splits, l_lookup)
        row.update(full_sample_metrics(rule, baseline, l_lookup))
        summary_rows.append(row)
        detail_frames.append(detail)

    summary = pd.DataFrame(summary_rows)
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

    # 稳健排序：先看同时通过次数，再看复利通过次数、最差复利比、全样本复利。
    summary = summary.sort_values(
        [
            "pass_both_count",
            "pass_multiple_count",
            "pass_drawdown_count",
            "min_multiple_ratio",
            "avg_multiple_ratio",
            "full_model3_equity_multiple",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)

    summary.to_csv(OUTPUT_DIR / "model3_robust_rules.csv", index=False, encoding="utf-8-sig")
    detail.to_csv(OUTPUT_DIR / "model3_robust_rule_tests.csv", index=False, encoding="utf-8-sig")
    write_report(summary, detail)

    print("model=3 稳健规则搜索完成")
    print(summary.head(20).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
