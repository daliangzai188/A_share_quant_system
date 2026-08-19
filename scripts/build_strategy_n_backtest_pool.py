"""用N唯一规则源重建完整历史候选账本并验证已研究组合入选明细。"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_ac_daily_candidates import trade_return  # noqa: E402
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.strategy_n import load_n_spec, select_n_daily_picks  # noqa: E402

START_DATE = "20240520"
END_DATE = "20260514"
EXPECTED_CANDIDATE_DAYS = 46
EXPECTED_PORTFOLIO_N_TRADES = 16
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_n"
OUTPUT_PATH = OUTPUT_DIR / "n_backtest_candidates.csv"
LOCKED_EXECUTED_PATH = PROJECT_ROOT / "reports" / "strategy_n_expansion" / "selected_n_trades.csv"


def main() -> None:
    config = json.loads((PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8"))
    spec = load_n_spec(config)
    pool = load_historical_bucketed_pool(START_DATE, END_DATE, 80)
    picks = select_n_daily_picks(pool, spec)
    if len(picks) != EXPECTED_CANDIDATE_DAYS or picks["trade_date"].duplicated().any():
        raise RuntimeError(
            f"N完整候选必须为{EXPECTED_CANDIDATE_DAYS}个唯一信号日，当前{len(picks)}"
        )

    rows: list[dict[str, object]] = []
    for row in picks.itertuples(index=False):
        status, buy_date, exit_date, stock_return = trade_return(
            str(row.trade_date), str(row.ts_code), 2
        )
        rows.append({
            "trade_date": str(row.trade_date),
            "ts_code": str(row.ts_code),
            "name": str(row.name),
            "market_segment": str(getattr(row, "market_segment", "")),
            "segment_limit_max_height_bucket": str(row.segment_limit_max_height_bucket),
            "segment_retreat_state_bucket": str(row.segment_retreat_state_bucket),
            "first_time": str(getattr(row, "first_time", "")),
            "first_time_minutes": float(row.first_time_minutes),
            "circ_mv": float(row.circ_mv),
            "limit_close": float(row.limit_close),
            "fill_probability": float(row.fill_probability),
            "execution_status": status,
            "buy_date": buy_date,
            "exit_date": exit_date,
            "stock_return_before_fees": stock_return,
            "strategy_version": str(config["strategy_n"]["strategy_version"]),
            "sample_scope": "COMPLETE_DAILY_CANDIDATES",
        })
    result = pd.DataFrame(rows)
    if not result["execution_status"].eq("OK").all():
        bad = result[~result["execution_status"].eq("OK")]
        raise RuntimeError("N锁定候选出现不可成交：\n" + bad.to_string(index=False))

    locked = pd.read_csv(
        LOCKED_EXECUTED_PATH,
        dtype={"signal_date": str, "ts_code": str},
        low_memory=False,
    )
    if len(locked) != EXPECTED_PORTFOLIO_N_TRADES:
        raise RuntimeError("N研究组合入选明细不是16笔")
    compare = locked[["signal_date", "ts_code"]].merge(
        result[["trade_date", "ts_code"]],
        left_on="signal_date",
        right_on="trade_date",
        how="left",
        suffixes=("_expected", "_actual"),
    )
    if not compare["ts_code_expected"].eq(compare["ts_code_actual"]).all():
        raise RuntimeError("N唯一规则源与研究组合16笔逐票不一致：\n" + compare.to_string(index=False))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(
        f"N完整候选账本已生成：{len(result)}个候选日；"
        f"与组合入选{len(compare)}笔逐票一致；输出={OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
