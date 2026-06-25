"""
搜索 L 龙头策略在现有组合中的条件化替换规则。

目标：
  - 不让 L 盲目全局优先
  - 只在历史冲突日里寻找 L 替换当前组合更有优势的条件
  - 默认要求组合复利提升，最大回撤不差于当前 ABCE2 审计口径

本脚本只做离线研究，不接实盘，不修改 ABC/E2/D。

输出：
  reports/strategy_l/leader_combo_replacement_rules.csv
  reports/strategy_l/leader_combo_replacement_report.md
"""
from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
OVERLAY_DAILY_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_combo_overlay_daily.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
INITIAL_EQUITY = 500_000.0
MAX_RULE_SIZE = 3
MIN_REPLACE_COUNT = 3

RULE_COLUMNS = [
    "baseline_strategy_leg",
    "market_segment",
    "theme_name",
    "segment_retreat_state_bucket",
    "theme_limit_count",
]


def to_numeric(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.fillna(0.0):
        if float(value) < 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def calc_equity(returns: pd.Series) -> pd.Series:
    return INITIAL_EQUITY * (1.0 + to_numeric(returns)).cumprod()


def calc_stats(returns: pd.Series, active_mask: pd.Series) -> dict[str, Any]:
    equity = calc_equity(returns)
    active_returns = to_numeric(returns[active_mask])
    return {
        "trade_count": int(active_mask.sum()),
        "win_rate": float((active_returns > 0).mean()) if len(active_returns) else 0.0,
        "avg_account_return": float(active_returns.mean()) if len(active_returns) else 0.0,
        "median_account_return": float(active_returns.median()) if len(active_returns) else 0.0,
        "final_equity": float(equity.iloc[-1]) if len(equity) else INITIAL_EQUITY,
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(active_returns.max()) if len(active_returns) else 0.0,
        "max_loss": float(active_returns.min()) if len(active_returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(active_returns),
    }


def load_daily() -> pd.DataFrame:
    if not OVERLAY_DAILY_PATH.exists():
        raise FileNotFoundError(f"找不到 L 组合叠加明细，请先运行 overlay 脚本: {OVERLAY_DAILY_PATH}")
    data = pd.read_csv(OVERLAY_DAILY_PATH, dtype={"signal_date": str}, low_memory=False)
    data["abce2_return"] = to_numeric(data["abce2_return"])
    data["l_return"] = to_numeric(data["l_return"])
    data["has_l_signal"] = data["has_l_signal"].fillna(False).astype(str).str.lower().isin({"true", "1"})
    data["is_idle_day"] = data["is_idle_day"].fillna(False).astype(str).str.lower().isin({"true", "1"})
    return data


def build_atoms(conflicts: pd.DataFrame) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for column in RULE_COLUMNS:
        if column not in conflicts.columns:
            continue
        values = conflicts[column].fillna("missing").astype(str)
        for value, group in conflicts.groupby(values):
            if len(group) < MIN_REPLACE_COUNT:
                continue
            diff = to_numeric(group["l_return"]) - to_numeric(group["abce2_return"])
            atoms.append({
                "column": column,
                "value": str(value),
                "label": f"{column}={value}",
                "replace_count": int(len(group)),
                "avg_diff": float(diff.mean()),
                "win_diff": float((diff > 0).mean()),
            })
    return atoms


def apply_rule(data: pd.DataFrame, combo: tuple[dict[str, Any], ...]) -> pd.Series:
    mask = pd.Series(True, index=data.index)
    for item in combo:
        mask &= data[item["column"]].fillna("missing").astype(str).eq(item["value"])
    return mask & data["has_l_signal"] & ~data["is_idle_day"]


def search_rules(data: pd.DataFrame) -> pd.DataFrame:
    base_returns = data["abce2_return"].copy()
    base_active = base_returns.ne(0)
    base_stats = calc_stats(base_returns, base_active)
    conflicts = data[data["has_l_signal"] & ~data["is_idle_day"]].copy()
    atoms = build_atoms(conflicts)
    rows: list[dict[str, Any]] = []

    for size in range(1, MAX_RULE_SIZE + 1):
        for combo in combinations(atoms, size):
            if len({item["column"] for item in combo}) != len(combo):
                continue
            replace_mask = apply_rule(data, combo)
            if int(replace_mask.sum()) < MIN_REPLACE_COUNT:
                continue
            returns = base_returns.copy()
            returns.loc[replace_mask] = data.loc[replace_mask, "l_return"]
            active_mask = base_active | replace_mask
            stats = calc_stats(returns, active_mask)
            diff = data.loc[replace_mask, "l_return"] - data.loc[replace_mask, "abce2_return"]
            if stats["equity_multiple"] <= base_stats["equity_multiple"]:
                continue
            rows.append({
                "rule": " AND ".join(item["label"] for item in combo),
                "rule_size": size,
                "replace_count": int(replace_mask.sum()),
                "replace_avg_diff": float(diff.mean()),
                "replace_win_diff": float((diff > 0).mean()),
                **stats,
                "equity_multiple_change": float(stats["equity_multiple"] - base_stats["equity_multiple"]),
                "drawdown_change": float(stats["max_drawdown"] - base_stats["max_drawdown"]),
                "drawdown_not_worse": bool(stats["max_drawdown"] >= base_stats["max_drawdown"]),
                "base_equity_multiple": base_stats["equity_multiple"],
                "base_max_drawdown": base_stats["max_drawdown"],
            })

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(
        ["drawdown_not_worse", "equity_multiple", "max_drawdown", "replace_count"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def write_report(result: pd.DataFrame) -> None:
    path = OUTPUT_DIR / "leader_combo_replacement_report.md"
    if result.empty:
        path.write_text("# L 条件化替换规则搜索\n\n没有找到复利高于基准的规则。\n", encoding="utf-8")
        return
    qualified = result[result["drawdown_not_worse"]].copy()
    best = qualified.iloc[0] if not qualified.empty else result.iloc[0]
    lines = [
        "# L 条件化替换规则搜索",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改 ABC/E2/D。",
        "",
        f"- 最优合格规则：{best['rule']}",
        f"- 替换次数：{int(best['replace_count'])}",
        f"- 复利：{best['equity_multiple']:.2f} 倍",
        f"- 最大回撤：{best['max_drawdown']:.2%}",
        f"- 基准复利：{best['base_equity_multiple']:.2f} 倍",
        f"- 基准最大回撤：{best['base_max_drawdown']:.2%}",
        "",
        "## 回撤不恶化候选前20",
        "",
        qualified.head(20).to_markdown(index=False) if not qualified.empty else "无",
        "",
        "## 收益最高候选前20",
        "",
        result.sort_values("equity_multiple", ascending=False).head(20).to_markdown(index=False),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    daily = load_daily()
    result = search_rules(daily)
    result.to_csv(OUTPUT_DIR / "leader_combo_replacement_rules.csv", index=False)
    write_report(result)

    print("L条件化替换规则搜索完成")
    if result.empty:
        print("未找到复利高于基准的替换规则")
        return
    print(result.head(20).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
