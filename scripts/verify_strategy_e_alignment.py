"""验证E实盘规则与无前视、单账户、入场门禁回测基准是否逐票一致。

验证内容：
1. 配置固定为40条R1规则，且选股条件/排序不含未来成交和收益字段；
2. 使用与实盘相同的 ``src.strategy_e`` 构造历史候选宇宙；
3. 对门禁前102个完整候选日逐日核对股票与退出规则；
4. 每日第一名确定后排除13:30~14:30首次涨停组，且禁止回补第二名；
5. 对门禁后82个完整候选日复算候选序列指标；单账户资金占用由组合认证脚本验证。

运行：
    python scripts/verify_strategy_e_alignment.py

输出：
    reports/strategy_e_alignment/e_live_alignment_verification.csv
    reports/strategy_e_alignment/e_live_alignment_compare.csv
"""
from __future__ import annotations

import argparse
import sys
import tempfile
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
from src.strategy_optimizer import StrategyConditionOptimizer  # noqa: E402


HIST_POOL_PATH = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored_asof.csv"
LOCKED_TRADES_PATH = PROJECT_ROOT / "reports" / "strategy_e_samples" / "e_r1_daily_candidates_full.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_e_alignment"
POSITION_PCT = 0.825
EXPECTED_PRE_GATE_TRADE_COUNT = 102
EXPECTED_ENTRY_GATE_TRADE_COUNT = 82
EXPECTED_EQUITY_MULTIPLE = 9.64571212266072
EXPECTED_MAX_DRAWDOWN = -0.2766138432983575


def load_historical_bucketed_pool(start_date: str, end_date: str, lookback_dates: int) -> pd.DataFrame:
    """用实盘同一特征链生成历史bucket，并为板块shift保留足够前置窗口。"""

    if not HIST_POOL_PATH.exists():
        raise FileNotFoundError(
            "历史研究禁止读取全样本成交打分表；请先运行 "
            "python scripts/score_limit_up_fill_probability.py --historical-asof "
            "--output-path data/processed/limit_up_fill_scored_asof.csv"
        )
    raw = pd.read_csv(HIST_POOL_PATH, low_memory=False)
    method = raw.get("fill_probability_method", pd.Series(dtype=str)).astype(str)
    if method.empty or not method.eq("asof_turnover_space_proxy_v2").all():
        raise RuntimeError("历史成交打分表缺少严格as-of方法标识，禁止用于研究/回测")
    raw["trade_date"] = raw["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    available = sorted(raw.loc[raw["trade_date"] <= end_date, "trade_date"].unique())
    start_index = available.index(start_date) if start_date in available else 0
    first_index = max(0, start_index - max(int(lookback_dates), 3))
    keep_dates = set(available[first_index:])
    raw = raw[raw["trade_date"].isin(keep_dates) & (raw["trade_date"] <= end_date)].copy()

    with tempfile.TemporaryDirectory(prefix="e_alignment_") as temp_dir:
        # 文件名以live_开头，强制optimizer读取逐日分片/原始日线；这与实盘入口一致，
        # 也避免误读已停止更新的巨大daily_merged单文件。
        input_path = Path(temp_dir) / "live_e_alignment_pool.csv"
        raw.to_csv(input_path, index=False, encoding="utf-8-sig")
        optimizer = StrategyConditionOptimizer(config_path="config/config.json")
        optimizer.input_trades_path = input_path
        pool = optimizer.load_trades(require_complete_exit=False)
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
    parser = argparse.ArgumentParser(description="验证E实盘与82日完整入场门禁候选样本逐票对齐")
    parser.add_argument("--lookback-dates", type=int, default=80, help="板块状态前置数据日数量")
    args = parser.parse_args()

    spec = load_e_spec(PROJECT_ROOT)
    locked = pd.read_csv(LOCKED_TRADES_PATH, dtype={"trade_date": str}, low_memory=False)
    start_date = str(locked["trade_date"].min())
    end_date = str(locked["trade_date"].max())

    future_fields = sorted(required_signal_fields(spec) & FORBIDDEN_SELECTION_COLUMNS)
    if future_fields:
        raise RuntimeError(f"E规则含前视字段：{future_fields}")

    pool = load_historical_bucketed_pool(start_date, end_date, args.lookback_dates)
    universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
    candidates = select_e_candidates(universe)
    pre_gate_daily_pick = candidates.groupby("trade_date", as_index=False).head(1).copy()
    daily_pick = select_e_daily_picks(universe, spec)

    # 门禁前必须保持102个完整候选日逐票一致；历史50行子集不得再替代完整样本。
    pre_gate_expected = locked[["trade_date", "ts_code", "exit_rule"]].copy()
    pre_gate_actual = pre_gate_daily_pick[
        ["trade_date", "ts_code", "exit_rule"]
    ].copy()
    pre_gate_compare = pre_gate_expected.merge(
        pre_gate_actual,
        on="trade_date",
        how="left",
        suffixes=("_expected", "_actual"),
    )
    pre_gate_compare["same_stock"] = pre_gate_compare["ts_code_expected"].eq(
        pre_gate_compare["ts_code_actual"]
    )
    pre_gate_compare["same_exit_rule"] = pre_gate_compare[
        "exit_rule_expected"
    ].eq(pre_gate_compare["exit_rule_actual"])

    eligible_locked = apply_e_entry_gate(locked, spec)
    expected = eligible_locked[["trade_date", "ts_code", "exit_rule"]].copy()
    actual = daily_pick[["trade_date", "ts_code", "exit_rule", "scenario_rank", "scenario"]].copy()
    compare = expected.merge(actual, on="trade_date", how="left", suffixes=("_expected", "_actual"))
    compare["same_stock"] = compare["ts_code_expected"].eq(compare["ts_code_actual"])
    compare["same_exit_rule"] = compare["exit_rule_expected"].eq(compare["exit_rule_actual"])

    returns = pd.to_numeric(eligible_locked["net_return"], errors="raise") * POSITION_PCT
    equity = (1 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1

    summary = pd.DataFrame(
        [
            {
                "strategy_version": E_VERSION,
                "scenario_count": len(spec["scenarios"]),
                "pre_gate_trade_count": len(locked),
                "pre_gate_same_stock_count": int(pre_gate_compare["same_stock"].sum()),
                "pre_gate_same_exit_rule_count": int(pre_gate_compare["same_exit_rule"].sum()),
                "entry_gate_removed_count": int(len(locked) - len(eligible_locked)),
                "locked_trade_count": len(eligible_locked),
                "same_stock_count": int(compare["same_stock"].sum()),
                "same_exit_rule_count": int(compare["same_exit_rule"].sum()),
                "position_pct": POSITION_PCT,
                "avg_account_return": float(returns.mean()),
                "median_account_return": float(returns.median()),
                "win_rate": float((returns > 0).mean()),
                "equity_multiple": float(equity.iloc[-1]),
                "max_drawdown": float(drawdown.min()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
                "max_consecutive_losses": max_consecutive_losses(returns),
                "future_selection_field_count": len(future_fields),
                "sample_scope": "COMPLETE_DAILY_CANDIDATES",
                "single_account_validation_source": "certify_strict_asof_portfolio.py",
                "alignment_passed": bool(
                    len(locked) == EXPECTED_PRE_GATE_TRADE_COUNT
                    and len(eligible_locked) == EXPECTED_ENTRY_GATE_TRADE_COUNT
                    and pre_gate_compare["same_stock"].all()
                    and pre_gate_compare["same_exit_rule"].all()
                    and compare["same_stock"].all()
                    and compare["same_exit_rule"].all()
                    and abs(float(equity.iloc[-1]) - EXPECTED_EQUITY_MULTIPLE) < 1e-9
                    and abs(float(drawdown.min()) - EXPECTED_MAX_DRAWDOWN) < 1e-9
                ),
            }
        ]
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUTPUT_DIR / "e_live_alignment_verification.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(OUTPUT_DIR / "e_live_alignment_compare.csv", index=False, encoding="utf-8-sig")
    pre_gate_compare.to_csv(
        OUTPUT_DIR / "e_live_alignment_pre_gate_compare.csv",
        index=False,
        encoding="utf-8-sig",
    )
    eligible_locked.to_csv(
        OUTPUT_DIR / "e_r1_entry_gate_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(summary.to_string(index=False))
    if not bool(summary.iloc[0]["alignment_passed"]):
        mismatches = compare[~(compare["same_stock"] & compare["same_exit_rule"])]
        print(mismatches.to_string(index=False))
        raise SystemExit("E实盘规则未通过102日完整候选样本逐票对齐验证，禁止上线。")
    print(
        "E对齐验证通过：门禁前102/102同股同退出，门禁后82/82同股同退出；"
        "组合单账户结果由当前组合认证报告锁定。"
    )


if __name__ == "__main__":
    main()
