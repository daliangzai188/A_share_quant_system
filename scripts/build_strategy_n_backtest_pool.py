"""用N唯一规则源重建完整历史候选账本并验证已研究组合入选明细。"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.strategy_n import load_n_spec, select_n_daily_picks  # noqa: E402

START_DATE = "20240520"
END_DATE = "20260514"
EXPECTED_PORTFOLIO_N_TRADES = 35
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_n_v3"
OUTPUT_PATH = OUTPUT_DIR / "n_backtest_candidates.csv"
LOCKED_EXECUTED_PATH = (
    PROJECT_ROOT / "reports" / "strategy_n_v2_research" / "locked_portfolio_trades.csv"
)


def main() -> None:
    config = json.loads((PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8"))
    spec = load_n_spec(config)
    live_config = config.get("live_trade", {})
    takeprofit_enabled = bool(live_config.get("intraday_takeprofit_enabled", True))
    takeprofit_offset = float(live_config.get("intraday_takeprofit_offset", 0.01))
    pool = load_historical_bucketed_pool(START_DATE, END_DATE, 80)
    picks = select_n_daily_picks(pool, spec)
    if picks["trade_date"].duplicated().any() or len(picks) < 80:
        raise RuntimeError(f"N修复版候选日异常：唯一日={picks['trade_date'].nunique()}，总行数={len(picks)}")

    rows: list[dict[str, object]] = []
    for row in picks.itertuples(index=False):
        outcome = trade_return_details(
            str(row.trade_date),
            str(row.ts_code),
            2,
            name=str(row.name),
            use_intraday_takeprofit=takeprofit_enabled,
            takeprofit_offset=takeprofit_offset,
        )
        rows.append({
            "trade_date": str(row.trade_date),
            "ts_code": str(row.ts_code),
            "name": str(row.name),
            "market_segment": str(getattr(row, "market_segment", "")),
            "segment_limit_max_height_bucket": str(row.segment_limit_max_height_bucket),
            "segment_retreat_state_bucket": str(row.segment_retreat_state_bucket),
            "market_chain_count_bucket": str(getattr(row, "market_chain_count_bucket", "")),
            "market_emotion_state_bucket": str(getattr(row, "market_emotion_state_bucket", "")),
            "n_branch": str(getattr(row, "n_branch", "")),
            "n_rule_id": str(getattr(row, "n_rule_id", "")),
            "first_time": str(getattr(row, "first_time", "")),
            "first_time_minutes": float(row.first_time_minutes),
            "circ_mv": float(row.circ_mv),
            "limit_close": float(row.limit_close),
            "fill_probability": float(row.fill_probability),
            "fill_space_ratio": float(getattr(row, "fill_space_ratio", row.fill_probability)),
            "fill_probability_method": str(getattr(row, "fill_probability_method", "")),
            "model_training_end_date": str(getattr(row, "model_training_end_date", "")),
            "execution_status": outcome.status,
            "buy_date": outcome.buy_date,
            "exit_date": outcome.exit_date,
            "exit_rule": outcome.exit_rule,
            "stock_return_before_fees": outcome.stock_return,
            "strategy_version": str(config["strategy_n"]["strategy_version"]),
            "sample_scope": "COMPLETE_DAILY_CANDIDATES",
        })
    result = pd.DataFrame(rows)

    locked = pd.read_csv(
        LOCKED_EXECUTED_PATH,
        dtype={"signal_date": str, "ts_code": str},
        low_memory=False,
    )
    locked = locked[locked["strategy_leg"].astype(str).eq("N")].copy()
    if len(locked) != EXPECTED_PORTFOLIO_N_TRADES:
        raise RuntimeError("N双分支研究组合入选明细不是35笔")
    compare = locked[["signal_date", "ts_code"]].merge(
        result[["trade_date", "ts_code"]],
        left_on="signal_date",
        right_on="trade_date",
        how="left",
        suffixes=("_expected", "_actual"),
    )
    matched = int(compare["ts_code_expected"].eq(compare["ts_code_actual"]).sum())
    changed = compare[~compare["ts_code_expected"].eq(compare["ts_code_actual"])].copy()
    # v3修复了历史成交打分前视；候选变化是预期审计结果，必须披露，不能为了
    # 维持旧35笔而继续引用被污染的旧候选。
    if not changed.empty:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        changed.to_csv(
            OUTPUT_DIR / "locked_v2_executed_candidate_changes.csv",
            index=False,
            encoding="utf-8-sig",
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(
        f"N完整候选账本已生成：{len(result)}个候选日；"
        f"旧组合35笔中{matched}笔候选仍一致、{len(changed)}笔变化；"
        f"状态={result['execution_status'].value_counts().to_dict()}；输出={OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
