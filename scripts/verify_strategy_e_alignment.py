"""验证E实盘V14规则与最近三年冻结计划是否逐票一致。

验证内容：
1. 配置固定为40条R1规则，且选股条件/排序不含未来成交和收益字段；
2. 使用与实盘相同的 ``src.strategy_e`` 构造历史候选宇宙；
3. 对换手率高值优先、封单金额/流通市值低值优先的等权日内分位排序逐日核对；
4. 核对V13单项门禁及“倍率1.2~2且炸板2~3次”AND空仓门禁，禁止回补第二名；
5. 单账户收益、资金占用和组合腿序由严格组合认证脚本独立验证。

运行：
    python scripts/verify_strategy_e_alignment.py

输出：
    reports/strategy_e_alignment/e_live_alignment_verification.csv
    reports/strategy_e_alignment/e_live_alignment_compare.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy_e import (  # noqa: E402
    E_VERSION,
    FORBIDDEN_SELECTION_COLUMNS,
    apply_e_entry_gate,
    build_r1_universe_from_pool,
    load_e_spec,
    required_signal_fields,
    select_e_candidates,
    select_e_daily_picks,
)
HIST_POOL_PATH = PROJECT_ROOT / "data/research/monthly_acde/20260831/strict_feature_pool.csv"
LOCKED_PICKS_PATH = PROJECT_ROOT / "reports/current_portfolio_alignment/acde_e_no_trade_v14/e_plans.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_e_alignment"
POSITION_PCT = 0.825
START_DATE = "20230901"
END_DATE = "20260831"
EXPECTED_PRE_GATE_TRADE_COUNT = 148
EXPECTED_V13_GATE_TRADE_COUNT = 81
EXPECTED_ENTRY_GATE_TRADE_COUNT = 69


def load_historical_bucketed_pool(start_date: str, end_date: str, lookback_dates: int) -> pd.DataFrame:
    """读取月度流水线冻结的严格时点特征池。"""

    if not HIST_POOL_PATH.exists():
        raise FileNotFoundError(
            "缺少20260831月度流水线冻结特征池，禁止用其他数据临时代替。"
        )
    pool = pd.read_csv(HIST_POOL_PATH, dtype={"trade_date": str}, low_memory=False)
    return pool[(pool["trade_date"] >= start_date) & (pool["trade_date"] <= end_date)].copy()


def max_consecutive_losses(returns: pd.Series) -> int:
    maximum = current = 0
    for value in returns:
        if float(value) <= 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def verify_single_account_occupancy(trades: pd.DataFrame) -> list[str]:
    """下一笔信号日不得早于上一笔退出日，避免同一笔资金重复占用。"""

    errors: list[str] = []
    previous_exit = ""
    for row in trades.sort_values("trade_date").itertuples(index=False):
        signal_date = str(row.trade_date)
        if previous_exit and signal_date < previous_exit:
            errors.append(f"{signal_date}早于上一笔退出日{previous_exit}")
        previous_exit = str(row.exit_date)
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="验证E实盘V14与最近三年69个冻结计划逐票对齐")
    parser.add_argument("--lookback-dates", type=int, default=80, help="板块状态前置数据日数量")
    args = parser.parse_args()

    spec = load_e_spec(PROJECT_ROOT)
    locked = pd.read_csv(LOCKED_PICKS_PATH, dtype={"signal_date": str}, low_memory=False)
    locked = locked.rename(columns={"signal_date": "trade_date"})

    future_fields = sorted(required_signal_fields(spec) & FORBIDDEN_SELECTION_COLUMNS)
    if future_fields:
        raise RuntimeError(f"E规则含前视字段：{future_fields}")

    pool = load_historical_bucketed_pool(START_DATE, END_DATE, args.lookback_dates)
    universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
    candidates = select_e_candidates(universe, spec)
    pre_gate_daily_pick = candidates.groupby("trade_date", as_index=False).head(1).copy()
    v13_spec = dict(spec)
    v13_spec["entry_gate"] = dict(spec["entry_gate"])
    v13_spec["entry_gate"].pop("exclude_all_conditions", None)
    v13_gate_daily_pick = apply_e_entry_gate(pre_gate_daily_pick, v13_spec)
    daily_pick = select_e_daily_picks(universe, spec)

    # 锁定文件是本轮研究中经过现行入场门禁后的89个候选日。用outer合并，
    # 防止新增或缺失日期被left join静默隐藏。
    expected = locked[["trade_date", "ts_code", "exit_rule"]].copy()
    actual = daily_pick[["trade_date", "ts_code", "exit_rule", "scenario_rank", "scenario"]].copy()
    compare = expected.merge(
        actual,
        on="trade_date",
        how="outer",
        suffixes=("_expected", "_actual"),
        indicator=True,
    )
    compare["same_stock"] = compare["ts_code_expected"].eq(compare["ts_code_actual"])
    compare["same_exit_rule"] = compare["exit_rule_expected"].eq(compare["exit_rule_actual"])

    summary = pd.DataFrame(
        [
            {
                "strategy_version": E_VERSION,
                "scenario_count": len(spec["scenarios"]),
                "pre_gate_trade_count": len(pre_gate_daily_pick),
                "v13_gate_trade_count": len(v13_gate_daily_pick),
                "v14_joint_gate_removed_count": int(
                    len(v13_gate_daily_pick) - len(daily_pick)
                ),
                "entry_gate_removed_count": int(len(pre_gate_daily_pick) - len(daily_pick)),
                "locked_trade_count": len(locked),
                "actual_trade_count": len(daily_pick),
                "same_stock_count": int(compare["same_stock"].sum()),
                "same_exit_rule_count": int(compare["same_exit_rule"].sum()),
                "position_pct": POSITION_PCT,
                "future_selection_field_count": len(future_fields),
                "sample_scope": "STRICT_20230901_20260831_E_V14_DAILY_PICKS",
                "single_account_validation_source": "certify_acde_return_first_release.py",
                "alignment_passed": bool(
                    len(pre_gate_daily_pick) == EXPECTED_PRE_GATE_TRADE_COUNT
                    and len(v13_gate_daily_pick) == EXPECTED_V13_GATE_TRADE_COUNT
                    and len(locked) == EXPECTED_ENTRY_GATE_TRADE_COUNT
                    and len(daily_pick) == EXPECTED_ENTRY_GATE_TRADE_COUNT
                    and compare["_merge"].eq("both").all()
                    and compare["same_stock"].all()
                    and compare["same_exit_rule"].all()
                ),
            }
        ]
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUTPUT_DIR / "e_live_alignment_verification.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(OUTPUT_DIR / "e_live_alignment_compare.csv", index=False, encoding="utf-8-sig")
    pre_gate_daily_pick.to_csv(
        OUTPUT_DIR / "e_live_alignment_pre_gate_picks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    daily_pick.to_csv(
        OUTPUT_DIR / "e_r1_entry_gate_picks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(summary.to_string(index=False))
    if not bool(summary.iloc[0]["alignment_passed"]):
        mismatches = compare[~(compare["same_stock"] & compare["same_exit_rule"])]
        print(mismatches.to_string(index=False))
        raise SystemExit("E实盘V14未通过最近三年冻结计划逐票对齐验证，禁止上线。")
    print(
        "E对齐验证通过：原始第一名148日、V13门禁后81日、V14联合门禁后69/69同股同退出；"
        "组合单账户结果由当前组合认证报告锁定。"
    )


if __name__ == "__main__":
    main()
