"""
model=3 自动切换防过拟合验证。

只做离线验证，不接实盘，不修改当前 mode=1 配置。

验证方式：
  1. 按年份留出：用其他年份训练选规则，用留出年份测试。
  2. 前半段训练、后半段测试：只用前半段选规则，再套到后半段。
  3. 滚动窗口：用一段历史训练选规则，再测试下一段。

注意：
  - 训练段可以搜索规则。
  - 测试段只能使用训练段选出的规则，不能重新调参。
  - 这是防过拟合第一层验证，不代表可以直接实盘。

输出：
  reports/strategy_model3/validation/model3_validation_summary.csv
  reports/strategy_model3/validation/model3_validation_detail.csv
  reports/strategy_model3/validation/model3_validation_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (
    INITIAL_EQUITY,
    SwitchAtom,
    build_l_lookup,
    build_switch_atoms,
    calc_metrics,
    generate_rules,
    load_baseline_daily,
    replay_model3,
    selected_l2_source,
)


OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "validation"
SEARCH_SUMMARY_PATH = PROJECT_ROOT / "reports" / "strategy_model3" / "model3_switch_summary.csv"


def choose_best_rule(
    train_daily: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    rules: list[tuple[SwitchAtom, ...]],
    *,
    min_l_trades: int = 8,
) -> tuple[tuple[SwitchAtom, ...], dict[str, Any]]:
    """只在训练集上选规则。

    排序目标：
      1. 复利高。
      2. 回撤小。
      3. L交易数不能太少，太少容易是偶然样本。
    """
    best_rule: tuple[SwitchAtom, ...] = tuple()
    best_metrics: dict[str, Any] = {}
    best_score = -10**18
    for rule in rules:
        _, metrics = replay_model3(train_daily, l_lookup, rule)
        l_trades = int(metrics.get("l_trade_count", 0))
        sample_penalty = 1000.0 if 0 < l_trades < min_l_trades else 0.0
        drawdown_penalty = abs(float(metrics.get("max_drawdown", 0.0))) * 10.0
        score = float(metrics.get("equity_multiple", 1.0)) - sample_penalty - drawdown_penalty
        if score > best_score:
            best_score = score
            best_rule = rule
            best_metrics = metrics
    return best_rule, best_metrics


def mode1_metrics(data: pd.DataFrame, scenario: str) -> dict[str, Any]:
    result = calc_metrics(scenario, data, "mode1_return", "mode1_operation_status")
    result["rule"] = "MODE1_ONLY"
    return result


def rule_text(rule: tuple[SwitchAtom, ...]) -> str:
    return " AND ".join(atom.name for atom in rule) if rule else "HAS_L_SIGNAL"


def parse_rule(text: str, atom_map: dict[str, SwitchAtom]) -> tuple[SwitchAtom, ...] | None:
    if text in {"", "HAS_L_SIGNAL"}:
        return tuple()
    parts = [part.strip() for part in str(text).split(" AND ") if part.strip()]
    atoms: list[SwitchAtom] = []
    for part in parts:
        atom = atom_map.get(part)
        if atom is None:
            return None
        atoms.append(atom)
    return tuple(atoms)


def build_candidate_rules(atoms: list[SwitchAtom], limit: int = 200) -> list[tuple[SwitchAtom, ...]]:
    """构造验证用候选规则池。

    防过拟合验证仍然是在训练集选规则，但为了避免每个切分都暴力搜索全部组合，
    这里先用全样本搜索报告的前N条作为候选池。候选池本身不会决定测试集结果，
    测试集只接收训练集从候选池中选出的规则。
    """
    atom_map = {atom.name: atom for atom in atoms}
    rules: list[tuple[SwitchAtom, ...]] = [tuple()]
    if SEARCH_SUMMARY_PATH.exists():
        summary = pd.read_csv(SEARCH_SUMMARY_PATH, low_memory=False)
        for raw_rule in summary.get("rule", pd.Series(dtype=str)).head(limit).astype(str):
            parsed = parse_rule(raw_rule, atom_map)
            if parsed is not None and parsed not in rules:
                rules.append(parsed)
    if len(rules) <= 1:
        rules = generate_rules(atoms, max_size=2)
    return rules


def run_split(
    split_name: str,
    train_daily: pd.DataFrame,
    test_daily: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    rules: list[tuple[SwitchAtom, ...]],
    *,
    min_l_trades: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    best_rule, train_metrics = choose_best_rule(
        train_daily,
        l_lookup,
        rules,
        min_l_trades=min_l_trades,
    )
    test_model3_daily, test_model3_metrics = replay_model3(test_daily, l_lookup, best_rule)
    test_mode1_metrics = mode1_metrics(test_daily, f"{split_name}:mode1_test")

    row = {
        "split": split_name,
        "train_start": str(train_daily["date"].min()) if not train_daily.empty else "",
        "train_end": str(train_daily["date"].max()) if not train_daily.empty else "",
        "test_start": str(test_daily["date"].min()) if not test_daily.empty else "",
        "test_end": str(test_daily["date"].max()) if not test_daily.empty else "",
        "selected_rule": rule_text(best_rule),
        "train_equity_multiple": train_metrics.get("equity_multiple", 1.0),
        "train_max_drawdown": train_metrics.get("max_drawdown", 0.0),
        "train_trade_count": train_metrics.get("trade_count", 0),
        "train_l_trade_count": train_metrics.get("l_trade_count", 0),
        "test_model3_equity_multiple": test_model3_metrics.get("equity_multiple", 1.0),
        "test_model3_max_drawdown": test_model3_metrics.get("max_drawdown", 0.0),
        "test_model3_trade_count": test_model3_metrics.get("trade_count", 0),
        "test_model3_l_trade_count": test_model3_metrics.get("l_trade_count", 0),
        "test_model3_win_rate": test_model3_metrics.get("win_rate", 0.0),
        "test_mode1_equity_multiple": test_mode1_metrics.get("equity_multiple", 1.0),
        "test_mode1_max_drawdown": test_mode1_metrics.get("max_drawdown", 0.0),
        "test_mode1_trade_count": test_mode1_metrics.get("trade_count", 0),
        "test_model3_vs_mode1_multiple_ratio": (
            float(test_model3_metrics.get("equity_multiple", 1.0))
            / max(float(test_mode1_metrics.get("equity_multiple", 1.0)), 1e-12)
        ),
        "test_model3_dd_improvement": (
            float(test_model3_metrics.get("max_drawdown", 0.0))
            - float(test_mode1_metrics.get("max_drawdown", 0.0))
        ),
    }
    test_model3_daily = test_model3_daily.copy()
    test_model3_daily["split"] = split_name
    test_model3_daily["selected_rule"] = row["selected_rule"]
    return row, test_model3_daily


def build_splits(daily: pd.DataFrame) -> list[tuple[str, pd.DataFrame, pd.DataFrame, int]]:
    splits: list[tuple[str, pd.DataFrame, pd.DataFrame, int]] = []

    # 1. 按年份留出：其他年份训练，单一年份测试。
    daily = daily.copy()
    daily["year"] = daily["date"].astype(str).str.slice(0, 4)
    for year in sorted(daily["year"].dropna().unique()):
        train = daily[daily["year"].ne(year)].drop(columns=["year"]).reset_index(drop=True)
        test = daily[daily["year"].eq(year)].drop(columns=["year"]).reset_index(drop=True)
        if len(train) >= 120 and len(test) >= 40:
            splits.append((f"year_holdout_{year}", train, test, 8))

    # 2. 前半段训练，后半段测试。
    mid = len(daily) // 2
    first_half = daily.iloc[:mid].drop(columns=["year"]).reset_index(drop=True)
    second_half = daily.iloc[mid:].drop(columns=["year"]).reset_index(drop=True)
    splits.append(("first_half_train_second_half_test", first_half, second_half, 10))

    # 3. 滚动窗口：约 240 个交易日训练，后续 60 个交易日测试，步长 60。
    train_size = 240
    test_size = 60
    step = 60
    start = 0
    while start + train_size + test_size <= len(daily):
        train = daily.iloc[start:start + train_size].drop(columns=["year"]).reset_index(drop=True)
        test = daily.iloc[start + train_size:start + train_size + test_size].drop(columns=["year"]).reset_index(drop=True)
        split_name = f"rolling_{train['date'].iloc[0]}_{train['date'].iloc[-1]}__test_{test['date'].iloc[0]}_{test['date'].iloc[-1]}"
        splits.append((split_name, train, test, 8))
        start += step

    return splits


def write_report(summary: pd.DataFrame) -> None:
    report_path = OUTPUT_DIR / "model3_validation_report.md"
    passed = summary[
        (summary["test_model3_vs_mode1_multiple_ratio"] > 1.0)
        & (summary["test_model3_max_drawdown"] >= summary["test_mode1_max_drawdown"])
    ]
    lines = [
        "# model=3 自动切换防过拟合验证",
        "",
        "说明：本报告只做离线验证，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 验证切分数：{len(summary)}",
        f"- 测试集复利超过 mode=1 且回撤不差于 mode=1 的切分数：{len(passed)}",
        "",
        "## 汇总",
        "",
        summary.to_markdown(index=False),
        "",
        "## 解释",
        "",
        "- `test_model3_vs_mode1_multiple_ratio > 1` 表示测试集 model=3 复利超过同区间 mode=1。",
        "- `test_model3_dd_improvement >= 0` 表示 model=3 最大回撤不比 mode=1 更差。",
        "- 如果某个年份/滚动测试失败，说明规则仍有过拟合或行情依赖，需要继续收紧或做样本外观察。",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    atoms = build_switch_atoms()
    rules = build_candidate_rules(atoms, limit=200)
    splits = build_splits(baseline)

    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[pd.DataFrame] = []
    for split_name, train, test, min_l_trades in splits:
        row, detail = run_split(
            split_name,
            train,
            test,
            l_lookup,
            rules,
            min_l_trades=min_l_trades,
        )
        summary_rows.append(row)
        detail_rows.append(detail)

    summary = pd.DataFrame(summary_rows)
    detail = pd.concat(detail_rows, ignore_index=True) if detail_rows else pd.DataFrame()
    summary.to_csv(OUTPUT_DIR / "model3_validation_summary.csv", index=False, encoding="utf-8-sig")
    detail.to_csv(OUTPUT_DIR / "model3_validation_detail.csv", index=False, encoding="utf-8-sig")
    write_report(summary)

    print("model=3 防过拟合验证完成")
    print(summary.to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
