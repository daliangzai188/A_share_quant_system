"""
验证 model=3 安全候选规则二选一是否过拟合。

只做离线验证，不接实盘，不修改当前 mode=1 配置。

候选规则：
  - idle_plus_replace_not_sh_main
  - idle_plus_replace_chi_next

验证方式：
  每个切分先只看训练集，按“复利优先、回撤其次、L交易数再次”选择候选规则；
  然后把训练集选出的候选规则原样套到测试集。

这样可以回答：
  1. 如果不看测试集，训练阶段会选哪个候选规则？
  2. 被选中的规则在测试集是否仍然优于 mode=1？
  3. 两个候选规则是否实质等价，还是某个规则只靠全样本偶然样本胜出？

输出：
  reports/strategy_model3/safe_candidate_validation/model3_safe_candidate_choice_summary.csv
  reports/strategy_model3/safe_candidate_validation/model3_safe_candidate_choice_detail.csv
  reports/strategy_model3/safe_candidate_validation/model3_safe_candidate_choice_report.md
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
    build_l_lookup,
    calc_metrics,
    load_baseline_daily,
    selected_l2_source,
)
from scripts.search_strategy_model3_safe_modes import (  # noqa: E402
    SafePolicy,
    build_policies,
    load_robust_rule,
    markdown_table,
    replay_safe_policy,
)
from scripts.validate_strategy_model3_switch import build_splits, mode1_metrics  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "safe_candidate_validation"
CANDIDATE_NAMES = {
    "idle_plus_replace_not_sh_main",
    "idle_plus_replace_chi_next",
}


def candidate_policies() -> list[SafePolicy]:
    _rule_text, base_rule = load_robust_rule()
    return [policy for policy in build_policies(base_rule) if policy.name in CANDIDATE_NAMES]


def policy_score(metrics: dict[str, Any]) -> tuple[float, float, int]:
    """训练集选择规则的排序分。

    排序只使用训练集指标：
      1. 复利越高越好。
      2. 最大回撤越接近 0 越好。
      3. L交易数多一点更有统计意义。
    """
    return (
        float(metrics.get("equity_multiple", 1.0)),
        float(metrics.get("max_drawdown", 0.0)),
        int(metrics.get("l_trade_count", 0)),
    )


def choose_policy_on_train(
    train: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    policies: list[SafePolicy],
) -> tuple[SafePolicy, dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    best_policy = policies[0]
    best_metrics: dict[str, Any] = {}
    best_score: tuple[float, float, int] | None = None
    for policy in policies:
        _daily, metrics = replay_safe_policy(train, l_lookup, policy)
        row = {
            "policy": policy.name,
            "train_equity_multiple": metrics["equity_multiple"],
            "train_max_drawdown": metrics["max_drawdown"],
            "train_trade_count": metrics["trade_count"],
            "train_l_trade_count": metrics["l_trade_count"],
            "train_win_rate": metrics["win_rate"],
        }
        rows.append(row)
        score = policy_score(metrics)
        if best_score is None or score > best_score:
            best_score = score
            best_policy = policy
            best_metrics = metrics
    return best_policy, best_metrics, rows


def evaluate_all_candidates_on_test(
    split_name: str,
    test: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    policies: list[SafePolicy],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    mode1 = mode1_metrics(test, f"{split_name}:mode1")
    for policy in policies:
        _daily, metrics = replay_safe_policy(test, l_lookup, policy)
        rows.append({
            "split": split_name,
            "policy": policy.name,
            "test_equity_multiple": metrics["equity_multiple"],
            "test_max_drawdown": metrics["max_drawdown"],
            "test_trade_count": metrics["trade_count"],
            "test_l_trade_count": metrics["l_trade_count"],
            "test_win_rate": metrics["win_rate"],
            "test_mode1_equity_multiple": mode1["equity_multiple"],
            "test_mode1_max_drawdown": mode1["max_drawdown"],
            "test_vs_mode1_multiple_ratio": float(metrics["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12),
            "test_dd_improvement": float(metrics["max_drawdown"]) - float(mode1["max_drawdown"]),
        })
    return pd.DataFrame(rows)


def run_validation() -> tuple[pd.DataFrame, pd.DataFrame]:
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    policies = candidate_policies()
    splits = build_splits(baseline)

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    for split_name, train, test, _min_l_trades in splits:
        selected, train_metrics, train_rows = choose_policy_on_train(train, l_lookup, policies)
        selected_daily, selected_test = replay_safe_policy(test, l_lookup, selected)
        mode1 = mode1_metrics(test, f"{split_name}:mode1")
        all_test = evaluate_all_candidates_on_test(split_name, test, l_lookup, policies)
        all_test["selected_policy"] = selected.name
        detail_frames.append(all_test)

        summary_rows.append({
            "split": split_name,
            "train_start": str(train["date"].min()),
            "train_end": str(train["date"].max()),
            "test_start": str(test["date"].min()),
            "test_end": str(test["date"].max()),
            "selected_policy": selected.name,
            "selected_train_equity_multiple": train_metrics["equity_multiple"],
            "selected_train_max_drawdown": train_metrics["max_drawdown"],
            "selected_train_l_trade_count": train_metrics["l_trade_count"],
            "test_equity_multiple": selected_test["equity_multiple"],
            "test_max_drawdown": selected_test["max_drawdown"],
            "test_trade_count": selected_test["trade_count"],
            "test_l_trade_count": selected_test["l_trade_count"],
            "test_win_rate": selected_test["win_rate"],
            "test_mode1_equity_multiple": mode1["equity_multiple"],
            "test_mode1_max_drawdown": mode1["max_drawdown"],
            "test_vs_mode1_multiple_ratio": float(selected_test["equity_multiple"]) / max(float(mode1["equity_multiple"]), 1e-12),
            "test_dd_improvement": float(selected_test["max_drawdown"]) - float(mode1["max_drawdown"]),
            "candidate_train_rows": str(train_rows),
        })

        selected_daily = selected_daily.copy()
        selected_daily["split"] = split_name
        selected_daily["selected_policy"] = selected.name
        selected_daily.to_csv(
            OUTPUT_DIR / f"detail_{split_name}.csv",
            index=False,
            encoding="utf-8-sig",
        )

    summary = pd.DataFrame(summary_rows)
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    return summary, detail


def write_report(summary: pd.DataFrame, detail: pd.DataFrame) -> None:
    pass_multiple = int((summary["test_vs_mode1_multiple_ratio"] > 1.0).sum())
    pass_dd = int((summary["test_dd_improvement"] >= 0.0).sum())
    pass_both = int(((summary["test_vs_mode1_multiple_ratio"] > 1.0) & (summary["test_dd_improvement"] >= 0.0)).sum())
    selected_counts = summary["selected_policy"].value_counts().reset_index()
    selected_counts.columns = ["selected_policy", "count"]

    summary_cols = [
        "split",
        "selected_policy",
        "test_start",
        "test_end",
        "test_equity_multiple",
        "test_mode1_equity_multiple",
        "test_vs_mode1_multiple_ratio",
        "test_max_drawdown",
        "test_mode1_max_drawdown",
        "test_dd_improvement",
        "test_l_trade_count",
    ]
    detail_cols = [
        "split",
        "policy",
        "test_equity_multiple",
        "test_mode1_equity_multiple",
        "test_vs_mode1_multiple_ratio",
        "test_max_drawdown",
        "test_mode1_max_drawdown",
        "test_dd_improvement",
        "test_l_trade_count",
        "selected_policy",
    ]
    lines = [
        "# model=3 安全候选规则二选一样本外验证",
        "",
        "说明：本报告只做离线验证，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 验证切分数：{len(summary)}",
        f"- 测试集复利超过 mode=1 的次数：{pass_multiple}/{len(summary)}",
        f"- 测试集回撤不差于 mode=1 的次数：{pass_dd}/{len(summary)}",
        f"- 复利和回撤同时通过次数：{pass_both}/{len(summary)}",
        "",
        "## 训练集选择次数",
        "",
        markdown_table(selected_counts),
        "",
        "## 被选规则测试集表现",
        "",
        markdown_table(summary[summary_cols]),
        "",
        "## 两个候选在每个测试集的并列表现",
        "",
        markdown_table(detail[detail_cols]),
        "",
        "## 解释",
        "",
        "- 如果某个候选在训练集中经常被选中，但测试集表现不稳定，说明它更可能有过拟合风险。",
        "- 如果两个候选在大部分切分表现完全相同，应优先选择限制更窄、解释更简单的规则。",
    ]
    (OUTPUT_DIR / "model3_safe_candidate_choice_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary, detail = run_validation()
    summary.to_csv(
        OUTPUT_DIR / "model3_safe_candidate_choice_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    detail.to_csv(
        OUTPUT_DIR / "model3_safe_candidate_choice_detail.csv",
        index=False,
        encoding="utf-8-sig",
    )
    write_report(summary, detail)

    print("model=3 安全候选规则二选一样本外验证完成")
    print(summary[[
        "split",
        "selected_policy",
        "test_vs_mode1_multiple_ratio",
        "test_dd_improvement",
        "test_l_trade_count",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
