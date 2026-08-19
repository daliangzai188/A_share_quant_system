#!/usr/bin/env python3
"""用D真实退出日分钟行情回放容量型卖出POV，并复算完整组合。

研究边界：

* 只研究当前组合中18笔普通D（T+2收盘退出）；
* D接力已经全关，不存在T+1集合竞价让路卖出；
* 每个POV信号只能使用当时已经完成的bar，不使用未来收盘价决定是否卖；
* POV均价替换D原收盘价后，重新计算D腿复利、174笔总复利和最大回撤；
* 未全部成交的场景不得把残仓按收盘价伪装成成交，直接判定不可认证。

先在Windows盘后采集数据，再运行：

    py -3.11 scripts\research_strategy_d_exit_fetch.py
    py -3.11 scripts\research_strategy_d_exit_pov.py

输出：

    reports/strategy_d/exit_pov/d_exit_pov_detail.csv
    reports/strategy_d/exit_pov/d_exit_pov_summary.csv
    reports/strategy_d/exit_pov/d_exit_pov_gates.csv
    reports/strategy_d/exit_pov/d_exit_pov_report.md
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_current_executable_portfolio import (  # noqa: E402
    D_FILL_STRESS,
    D_ROUND_TRIP_COST,
    POSITION_PCT,
    daily_close,
)
from scripts.research_exit_pov_scan import (  # noqa: E402
    Scenario,
    _load_bars,
    _replay_one,
)


DEFAULT_5M_PATH = (
    PROJECT_ROOT / "data" / "processed" / "research_strategy_d_exit_5m.csv"
)
DEFAULT_1M_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "research_strategy_d_exit_1m_tail.csv"
)
PORTFOLIO_TRADES_PATH = (
    PROJECT_ROOT
    / "reports"
    / "current_portfolio_alignment"
    / "portfolio_trades.csv"
)
D_TRADES_PATH = PROJECT_ROOT / "reports" / "strategy_d" / "d_daily_candidates.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d" / "exit_pov"
# 2026-08-19按N双分支逐日资金占用重放后的D>A>M>E>C>N发布标尺；容量仍未认证。
EXPECTED_D_COUNT = 18
EXPECTED_PORTFOLIO_COUNT = 174
EXPECTED_PORTFOLIO_MULTIPLE = 9508.426795072035
EXPECTED_PORTFOLIO_DRAWDOWN = -0.22480568875722184
EXPECTED_D_MULTIPLE = 2.9157517119286513
EPSILON = 1e-9


@dataclass(frozen=True)
class DExitVariant:
    """预先定义的少量可解释变体，避免在17笔样本上无限调参。"""

    name: str
    description: str
    start_floor_hhmm: str = ""


VARIANTS = (
    DExitVariant(
        "dynamic_current",
        "复用现有容量倒推起点；容量越紧越早启动",
    ),
    DExitVariant(
        "not_before_1430",
        "即使容量不足也不早于14:30启动，减少提前卖出的时间成本",
        start_floor_hhmm="1430",
    ),
    DExitVariant(
        "tail_only_1445",
        "只允许14:45容量复查后的1分钟尾段POV",
        start_floor_hhmm="1445",
    ),
)


def normalize_date(value: Any) -> str:
    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def parse_amounts(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in str(raw).split(",") if item.strip())
    if not values or any(value <= 0 for value in values):
        raise ValueError("仓位金额必须是逗号分隔的正数")
    return values


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + pd.to_numeric(returns, errors="raise")).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def compound(returns: pd.Series) -> float:
    if returns.empty:
        return 1.0
    return float((1.0 + pd.to_numeric(returns, errors="raise")).prod())


def load_trade_metadata() -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载174笔当前组合及18笔普通D的买入价、原退出价。"""

    portfolio = pd.read_csv(
        PORTFOLIO_TRADES_PATH,
        dtype={"signal_date": str, "exit_date": str, "ts_code": str},
        low_memory=False,
    )
    if len(portfolio) != EXPECTED_PORTFOLIO_COUNT:
        raise ValueError(f"当前组合必须为{EXPECTED_PORTFOLIO_COUNT}笔，实际{len(portfolio)}笔")
    portfolio["signal_date"] = portfolio["signal_date"].map(normalize_date)
    portfolio["exit_date"] = portfolio["exit_date"].map(normalize_date)
    portfolio["account_return"] = pd.to_numeric(
        portfolio["account_return"], errors="raise"
    )
    base_multiple = compound(portfolio["account_return"])
    base_drawdown = max_drawdown(portfolio["account_return"])
    if abs(base_multiple - EXPECTED_PORTFOLIO_MULTIPLE) > 1e-8:
        raise ValueError(f"组合复利基准漂移：{base_multiple}")
    if abs(base_drawdown - EXPECTED_PORTFOLIO_DRAWDOWN) > 1e-8:
        raise ValueError(f"组合最大回撤基准漂移：{base_drawdown}")

    ordinary = portfolio[
        portfolio["strategy_leg"].astype(str).str.upper().eq("D")
    ].copy()
    if len(ordinary) != EXPECTED_D_COUNT:
        raise ValueError(f"普通D必须为{EXPECTED_D_COUNT}笔，实际{len(ordinary)}笔")
    ordinary["key"] = ordinary["ts_code"].astype(str) + "|" + ordinary["exit_date"]
    ordinary = ordinary.sort_values(["exit_date", "ts_code"]).reset_index(drop=True)
    split_index = max(1, int(len(ordinary) * 2 / 3))
    ordinary["sample_split"] = "validation"
    ordinary.loc[ordinary.index < split_index, "sample_split"] = "development"

    historical_d = pd.read_csv(
        D_TRADES_PATH,
        dtype={"signal_date": str, "ts_code": str},
        low_memory=False,
    )
    historical_d["signal_date"] = historical_d["signal_date"].map(normalize_date)
    historical_d = historical_d.drop_duplicates("signal_date", keep="last")
    fields = historical_d[["signal_date", "limit_close", "exit_close"]].copy()
    fields["limit_close"] = pd.to_numeric(fields["limit_close"], errors="coerce")
    fields["exit_close"] = pd.to_numeric(fields["exit_close"], errors="coerce")
    ordinary = ordinary.merge(fields, on="signal_date", how="left", validate="one_to_one")

    # 完整逐日D候选原则上已经带T+2退出价；若旧数据迁移或停牌顺延造成字段缺失，
    # 与正式认证的d_t2_candidate保持一致，按组合记录的实际退出日读日线回补。
    missing_exit = ordinary["exit_close"].isna() | ordinary["exit_close"].le(0)
    if missing_exit.any():
        ordinary.loc[missing_exit, "exit_close"] = [
            daily_close(str(row.exit_date), str(row.ts_code))
            for row in ordinary.loc[missing_exit].itertuples()
        ]

    invalid = (
        ordinary["limit_close"].isna()
        | ordinary["limit_close"].le(0)
        | ordinary["exit_close"].isna()
        | ordinary["exit_close"].le(0)
    )
    if invalid.any():
        raise ValueError(
            "普通D缺少有效买入价或退出价："
            + ",".join(ordinary.loc[invalid, "key"].tolist())
        )

    calculated = (
        ordinary["exit_close"] / ordinary["limit_close"]
        - 1.0
        - D_ROUND_TRIP_COST
    ) * D_FILL_STRESS * POSITION_PCT
    if not np.allclose(
        calculated,
        ordinary["account_return"],
        atol=1e-10,
        rtol=0,
    ):
        raise ValueError("普通D组合收益与买入价/T+2收盘价无法逐笔复现")
    if abs(compound(ordinary["account_return"]) - EXPECTED_D_MULTIPLE) > 1e-8:
        raise ValueError("普通D复利基准漂移")
    return portfolio.reset_index(drop=True), ordinary


def validate_bar_inputs(
    five: pd.DataFrame,
    one: pd.DataFrame,
    ordinary: pd.DataFrame,
) -> None:
    """要求18笔普通D全部有完整5分钟和尾盘1分钟数据。"""

    expected = set(ordinary["key"])
    five_keys = set(five["key"])
    one_keys = set(one["key"])
    if five_keys != expected:
        raise ValueError(
            f"D 5分钟样本不完整：缺少{sorted(expected-five_keys)}，多出{sorted(five_keys-expected)}"
        )
    if one_keys != expected:
        raise ValueError(
            f"D 1分钟样本不完整：缺少{sorted(expected-one_keys)}，多出{sorted(one_keys-expected)}"
        )
    five_counts = five.groupby("key").size()
    one_counts = one.groupby("key").size()
    if not five_counts.eq(48).all() or not one_counts.eq(16).all():
        raise ValueError(
            "D分钟bar数量不完整："
            f"5m={five_counts.to_dict()}；1m={one_counts.to_dict()}"
        )


def portfolio_metrics_with_replacements(
    portfolio: pd.DataFrame,
    replacements: dict[tuple[str, str], float],
) -> tuple[float, float]:
    """只替换普通D收益，其余115笔组合交易保持不变。"""

    returns = portfolio["account_return"].copy()
    for index, row in portfolio.iterrows():
        key = (str(row["signal_date"]), str(row["strategy_leg"]).upper())
        if key in replacements:
            returns.iloc[index] = replacements[key]
    return compound(returns), max_drawdown(returns)


def run_replay(
    *,
    five: pd.DataFrame,
    one: pd.DataFrame,
    portfolio: pd.DataFrame,
    ordinary: pd.DataFrame,
    position_amounts: tuple[float, ...],
    base_participation: float,
    late_participation: float,
    runway_buffer: float,
    capacity_haircut: float,
    trigger_pct: float,
    pm_extrapolate: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    five_groups = {key: group.copy() for key, group in five.groupby("key")}
    one_groups = {key: group.copy() for key, group in one.groupby("key")}
    metadata = ordinary.set_index("key")
    scenario = Scenario(
        base_participation,
        late_participation,
        runway_buffer,
        capacity_haircut,
    )
    detail_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []

    for amount in position_amounts:
        for variant in VARIANTS:
            rows: list[dict[str, Any]] = []
            for key, meta in metadata.iterrows():
                result = _replay_one(
                    five_groups[key],
                    one_groups[key],
                    target_amount=amount,
                    scenario=scenario,
                    trigger_pct=trigger_pct,
                    pm_extrapolate=pm_extrapolate,
                    backtest_slippage={"D": 0.0},
                    start_floor_hhmm=variant.start_floor_hhmm,
                )
                close_from_bar = float(result["close_1500"])
                if abs(close_from_bar - float(meta["exit_close"])) > 0.011:
                    raise ValueError(
                        f"{key} 分钟收盘价{close_from_bar}与D账本{meta['exit_close']}不一致"
                    )
                complete = bool(result["complete_final_1500"])
                neutral_price = (
                    float(result["neutral_realized_avg_price"])
                    if complete
                    else np.nan
                )
                stress_price = (
                    float(result["stress_realized_avg_price"])
                    if complete
                    else np.nan
                )
                neutral_account_return = (
                    (
                        neutral_price / float(meta["limit_close"])
                        - 1.0
                        - D_ROUND_TRIP_COST
                    )
                    * D_FILL_STRESS
                    * POSITION_PCT
                    if complete
                    else np.nan
                )
                stress_account_return = (
                    (
                        stress_price / float(meta["limit_close"])
                        - 1.0
                        - D_ROUND_TRIP_COST
                    )
                    * D_FILL_STRESS
                    * POSITION_PCT
                    if complete
                    else np.nan
                )
                rows.append(
                    {
                        **result,
                        "variant": variant.name,
                        "variant_description": variant.description,
                        "position_amount": amount,
                        "account_equity_at_82_5pct": amount / POSITION_PCT,
                        "signal_date": str(meta["signal_date"]),
                        "name": str(meta["name"]),
                        "sample_split": str(meta["sample_split"]),
                        "limit_close": float(meta["limit_close"]),
                        "baseline_account_return": float(meta["account_return"]),
                        "neutral_account_return": neutral_account_return,
                        "stress_account_return": stress_account_return,
                    }
                )
            case = pd.DataFrame(rows)
            detail_rows.extend(case.to_dict("records"))
            complete_all = bool(case["complete_final_1500"].all())
            replacements: dict[tuple[str, str], float] = {}
            stress_replacements: dict[tuple[str, str], float] = {}
            if complete_all:
                replacements = {
                    (str(row.signal_date), "D"): float(row.neutral_account_return)
                    for row in case.itertuples(index=False)
                }
                stress_replacements = {
                    (str(row.signal_date), "D"): float(row.stress_account_return)
                    for row in case.itertuples(index=False)
                }
                total_multiple, total_drawdown = portfolio_metrics_with_replacements(
                    portfolio, replacements
                )
                stress_multiple, stress_drawdown = portfolio_metrics_with_replacements(
                    portfolio, stress_replacements
                )
                d_multiple = compound(case["neutral_account_return"])
                d_stress_multiple = compound(case["stress_account_return"])
            else:
                total_multiple = total_drawdown = np.nan
                stress_multiple = stress_drawdown = np.nan
                d_multiple = d_stress_multiple = np.nan

            for split in ("ALL", "development", "validation"):
                subset = case if split == "ALL" else case[case["sample_split"].eq(split)]
                split_complete = bool(subset["complete_final_1500"].all())
                baseline_d = compound(subset["baseline_account_return"])
                neutral_d = compound(subset["neutral_account_return"]) if split_complete else np.nan
                summary_rows.append(
                    {
                        "variant": variant.name,
                        "position_amount": amount,
                        "account_equity_at_82_5pct": amount / POSITION_PCT,
                        "sample_split": split,
                        "samples": len(subset),
                        "trigger_1300_rate": float(subset["trigger_1300"].mean()),
                        "trigger_1430_rate": float(subset["trigger_1430"].mean()),
                        "trigger_1445_rate": float(subset["trigger_1445"].mean()),
                        "complete_final_rate": float(subset["complete_final_1500"].mean()),
                        "complete_before_auction_rate": float(
                            subset["complete_before_auction_1457"].mean()
                        ),
                        "mean_neutral_vs_close_pct": float(
                            subset["neutral_close_mark_vs_close_pct"].mean()
                        ),
                        "mean_stress_vs_close_pct": float(
                            subset["stress_close_mark_vs_close_pct"].mean()
                        ),
                        "baseline_d_multiple": baseline_d,
                        "neutral_d_multiple": neutral_d,
                        "neutral_d_change": (
                            neutral_d / baseline_d - 1.0
                            if split_complete
                            else np.nan
                        ),
                        "portfolio_multiple": total_multiple if split == "ALL" else np.nan,
                        "portfolio_change": (
                            total_multiple / EXPECTED_PORTFOLIO_MULTIPLE - 1.0
                            if split == "ALL" and complete_all
                            else np.nan
                        ),
                        "portfolio_max_drawdown": total_drawdown if split == "ALL" else np.nan,
                        "stress_d_multiple": d_stress_multiple if split == "ALL" else np.nan,
                        "stress_portfolio_multiple": stress_multiple if split == "ALL" else np.nan,
                        "stress_portfolio_max_drawdown": stress_drawdown if split == "ALL" else np.nan,
                    }
                )

            all_splits = [case]
            all_splits.extend(
                case[case["sample_split"].eq(label)]
                for label in ("development", "validation")
            )
            split_noninferior = all(
                bool(split_case["complete_final_1500"].all())
                and compound(split_case["neutral_account_return"])
                >= compound(split_case["baseline_account_return"]) - EPSILON
                for split_case in all_splits
            )
            gate_rows.append(
                {
                    "variant": variant.name,
                    "position_amount": amount,
                    "account_equity_at_82_5pct": amount / POSITION_PCT,
                    "all_17_fully_sold": complete_all,
                    "d_full_and_both_splits_noninferior": split_noninferior,
                    "portfolio_noninferior": bool(
                        complete_all
                        and total_multiple >= EXPECTED_PORTFOLIO_MULTIPLE - EPSILON
                    ),
                    "drawdown_noninferior": bool(
                        complete_all
                        and total_drawdown >= EXPECTED_PORTFOLIO_DRAWDOWN - EPSILON
                    ),
                    "certification_passed": bool(
                        complete_all
                        and split_noninferior
                        and total_multiple >= EXPECTED_PORTFOLIO_MULTIPLE - EPSILON
                        and total_drawdown >= EXPECTED_PORTFOLIO_DRAWDOWN - EPSILON
                    ),
                }
            )

    return (
        pd.DataFrame(detail_rows),
        pd.DataFrame(summary_rows),
        pd.DataFrame(gate_rows),
    )


def markdown_table(frame: pd.DataFrame) -> str:
    view = frame.copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(
                lambda value: "" if pd.isna(value) else f"{float(value):.6f}"
            )
        else:
            view[column] = view[column].fillna("").astype(str)
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in view.astype(str).values)
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, gates: pd.DataFrame) -> None:
    all_summary = summary[summary["sample_split"].eq("ALL")].copy()
    passed = gates[gates["certification_passed"]]
    lines = [
        "# D普通退出容量型POV认证",
        "",
        "## 固定边界",
        "",
        f"- 当前完整组合基准：{EXPECTED_PORTFOLIO_COUNT}笔/{EXPECTED_PORTFOLIO_MULTIPLE:.2f}倍/最大回撤{EXPECTED_PORTFOLIO_DRAWDOWN:.2%}。",
        f"- 普通D基准：{EXPECTED_D_COUNT}笔/{EXPECTED_D_MULTIPLE:.6f}倍。",
        "- D接力退出不参与POV，继续按T+1集合竞价卖出释放A/C/E2资金。",
        "- 未全部成交的场景不计算组合复利，不能把残仓按收盘价伪装成成交。",
        "",
        "## 全样本结果",
        "",
        markdown_table(all_summary),
        "",
        "## 上线门禁",
        "",
        markdown_table(gates),
        "",
        "## 结论",
        "",
        (
            f"- 有{len(passed)}个金额/执行组合同时通过D腿、完整组合、回撤及前后段非劣门禁。"
            if not passed.empty
            else "- 当前没有变体同时通过全部门禁，禁止直接修改D实盘退出。"
        ),
        "- 历史分钟K只有成交量和OHLC，不等同于Level-2真实买盘；压力价结果必须同时查看。",
        "- 样本仅17笔，结果用于筛除明显不合格方案，不能承诺未来收益。",
    ]
    (OUTPUT_DIR / "d_exit_pov_report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回放普通D容量型卖出POV并复算132笔组合")
    parser.add_argument("--five", type=Path, default=DEFAULT_5M_PATH)
    parser.add_argument("--one", type=Path, default=DEFAULT_1M_PATH)
    parser.add_argument(
        "--position-amounts",
        default="250000,500000,1000000,3000000,5000000,10000000",
        help="待验证的D单笔仓位金额（元）",
    )
    parser.add_argument("--base-participation", type=float, default=0.25)
    parser.add_argument("--late-participation", type=float, default=0.35)
    parser.add_argument("--runway-buffer", type=float, default=1.2)
    parser.add_argument("--capacity-haircut", type=float, default=0.5)
    parser.add_argument("--trigger-pct", type=float, default=0.01)
    parser.add_argument("--pm-extrapolate", type=float, default=0.44)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    portfolio, ordinary = load_trade_metadata()
    five, _five_stats = _load_bars(args.five, require_leg=True)
    one, _one_stats = _load_bars(args.one, require_leg=False)
    validate_bar_inputs(five, one, ordinary)
    detail, summary, gates = run_replay(
        five=five,
        one=one,
        portfolio=portfolio,
        ordinary=ordinary,
        position_amounts=parse_amounts(args.position_amounts),
        base_participation=args.base_participation,
        late_participation=args.late_participation,
        runway_buffer=args.runway_buffer,
        capacity_haircut=args.capacity_haircut,
        trigger_pct=args.trigger_pct,
        pm_extrapolate=args.pm_extrapolate,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT_DIR / "d_exit_pov_detail.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_DIR / "d_exit_pov_summary.csv", index=False, encoding="utf-8-sig")
    gates.to_csv(OUTPUT_DIR / "d_exit_pov_gates.csv", index=False, encoding="utf-8-sig")
    write_report(summary, gates)
    print("D普通退出POV研究完成")
    print(summary[summary["sample_split"].eq("ALL")].to_string(index=False))
    print("\n上线门禁")
    print(gates.to_string(index=False))


if __name__ == "__main__":
    main()
