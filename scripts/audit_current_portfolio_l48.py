"""只审计已作废的旧154笔组合中的48笔L，不扩展为L独立策略研究。"""
from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_strategy_l_full_universe import (  # noqa: E402
    VARIANTS,
    build_trade_rows,
    load_complete_pool,
    load_config,
    load_price_table,
    metrics,
    open_trade_dates,
    safe_mask,
    select_variant,
)


CURRENT_TRADES_PATH = (
    PROJECT_ROOT / "reports" / "current_portfolio_alignment" / "portfolio_trades.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l" / "current_portfolio_l48_audit"
ARCHIVED_L48_DETAIL_PATH = OUTPUT_DIR / "current_l48_detail.csv"


def percent(value: float) -> str:
    return f"{value:.2%}"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    current = pd.read_csv(
        CURRENT_TRADES_PATH,
        dtype={
            "signal_date": str,
            "ts_code": str,
            "buy_date": str,
            "exit_date": str,
        },
        low_memory=False,
    )
    frozen_l = current[current["strategy_leg"].astype(str).eq("L")].copy()
    if len(current) != 154 or len(frozen_l) != 48:
        if not ARCHIVED_L48_DETAIL_PATH.exists():
            raise RuntimeError("当前已切换为无L组合，且缺少旧48笔L审计快照")
        archived = pd.read_csv(
            ARCHIVED_L48_DETAIL_PATH,
            dtype={"signal_date": str, "ts_code": str, "buy_date": str, "exit_date": str},
            low_memory=False,
        )
        source_columns = [
            "signal_date", "status", "strategy_leg", "ts_code", "name", "buy_date",
            "exit_date", "account_return", "equity_before", "equity_after",
            "blocked_by_leg", "blocked_by_code", "blocked_until", "return_source",
            "peak_equity", "drawdown", "entry_gate_enabled", "l_chain_3_8_enabled",
        ]
        frozen_l = archived[[column for column in source_columns if column in archived.columns]].copy()
        if len(frozen_l) != 48 or not frozen_l["strategy_leg"].astype(str).eq("L").all():
            raise RuntimeError("旧L审计快照不是48笔纯L样本，拒绝复算")
    legacy_total_count = 154

    pool = load_complete_pool()
    pool["safe_full_pool"] = safe_mask(pool)
    pool["theme_heat_top3"] = pool["theme_heat_rank"].le(3)
    pool["theme_leader_first"] = pool["theme_leader_rank"].eq(1)
    pool["l2_body_pass"] = (
        pool["safe_full_pool"]
        & pool["theme_heat_top3"]
        & pool["theme_leader_first"]
        & pool["segment_retreat_state_bucket"].ne("retreat_2day")
        & pool["segment_limit_down_count_bucket"].ne("3_8")
        & pool["theme_limit_count"].ne(30)
    )
    pool["model3_base_pass"] = (
        pool["market_segment"].astype(str).ne("star")
        & pool["segment_retreat_state_bucket"].isin({"neutral", "warming_2day"})
        & pool["market_chain_count_bucket"].isin({"3_8", "8_15", "15_30", "gte_30"})
    )

    l2_variant = next(variant for variant in VARIANTS if variant.name == "L2_CURRENT")
    full_selected = select_variant(pool, l2_variant)[["trade_date", "ts_code", "name"]].rename(
        columns={"ts_code": "full_selected_ts_code", "name": "full_selected_name"}
    )

    pool_columns = [
        "trade_date",
        "ts_code",
        "theme_name",
        "theme_heat_rank",
        "theme_leader_rank",
        "theme_limit_count",
        "segment_retreat_state_bucket",
        "segment_limit_down_count_bucket",
        "market_chain_count_bucket",
        "safe_full_pool",
        "theme_heat_top3",
        "theme_leader_first",
        "l2_body_pass",
        "model3_base_pass",
    ]
    detail = frozen_l.merge(
        pool[pool_columns],
        left_on=["signal_date", "ts_code"],
        right_on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    ).merge(
        full_selected,
        left_on="signal_date",
        right_on="trade_date",
        how="left",
        suffixes=("", "_selected"),
        validate="many_to_one",
    )
    detail["full_pool_selected_same_stock"] = detail["ts_code"].eq(
        detail["full_selected_ts_code"]
    )

    replay_input = frozen_l.rename(columns={"signal_date": "trade_date"}).copy()
    replay_input["variant"] = "FROZEN_CURRENT_L48"
    replay = build_trade_rows(
        replay_input,
        load_price_table(set(frozen_l["ts_code"].astype(str))),
        open_trade_dates(),
        load_config(),
        "open_limit_unbuyable",
    )
    replay = replay[[
        "signal_date",
        "ts_code",
        "status",
        "buy_date",
        "exit_date",
        "account_return",
    ]].rename(
        columns={
            "status": "current_rule_replay_status",
            "buy_date": "current_rule_buy_date",
            "exit_date": "current_rule_exit_date",
            "account_return": "current_rule_account_return",
        }
    )
    detail = detail.merge(replay, on=["signal_date", "ts_code"], how="left", validate="one_to_one")
    detail["exit_rule_aligned"] = detail["exit_date"].astype(str).eq(
        detail["current_rule_exit_date"].astype(str)
    )
    detail["account_return_diff"] = (
        pd.to_numeric(detail["account_return"], errors="coerce")
        - pd.to_numeric(detail["current_rule_account_return"], errors="coerce")
    )

    certified_metrics = metrics(frozen_l)
    replay_metrics = metrics(
        replay[replay["current_rule_replay_status"].astype(str).eq("EXECUTABLE")].rename(
            columns={"current_rule_account_return": "account_return"}
        )
    )
    metric_rows = pd.DataFrame([
        {"scenario": "FROZEN_CERTIFIED_L48", **certified_metrics},
        {"scenario": "SAME_48_CURRENT_T2_RECALCULATED", **replay_metrics},
    ])

    detail.to_csv(OUTPUT_DIR / "current_l48_detail.csv", index=False, encoding="utf-8-sig")
    metric_rows.to_csv(OUTPUT_DIR / "current_l48_return_metrics.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# 已作废的旧154笔组合中L的48笔专项审计",
        "",
        "## 审计对象",
        "",
        f"- 已作废的旧冻结组合交易：{legacy_total_count}笔。",
        f"- 其中旧L实际入选：{len(frozen_l)}笔。",
        f"- 信号区间：{frozen_l['signal_date'].min()}~{frozen_l['signal_date'].max()}。",
        "",
        "## 候选生成核对",
        "",
        f"- 48笔在完整涨停池中均能找到：{int(detail['trade_date'].notna().sum())}/48。",
        f"- 通过当前model=3基础环境门：{int(detail['model3_base_pass'].fillna(False).sum())}/48。",
        f"- 在完整池中仍满足当前L2本体：{int(detail['l2_body_pass'].fillna(False).sum())}/48。",
        f"- 与完整池当前L2每日第一名同股：{int(detail['full_pool_selected_same_stock'].sum())}/48。",
        f"- 题材热度仍在前三：{int(detail['theme_heat_top3'].fillna(False).sum())}/48。",
        f"- 题材内仍为龙头第一：{int(detail['theme_leader_first'].fillna(False).sum())}/48。",
        "",
        "## 收益与退出规则核对",
        "",
        f"- 当前认证退出日与当前T+2规则一致：{int(detail['exit_rule_aligned'].sum())}/48。",
        f"- 退出日不一致：{int((~detail['exit_rule_aligned']).sum())}/48。",
        f"- 冻结认证：胜率{percent(certified_metrics['win_rate'])}，平均{percent(certified_metrics['avg_account_return'])}，复利{certified_metrics['equity_multiple']:.6f}倍。",
        f"- 同48只按当前T+2重算：胜率{percent(replay_metrics['win_rate'])}，平均{percent(replay_metrics['avg_account_return'])}，复利{replay_metrics['equity_multiple']:.6f}倍。",
        "",
        "## 结论",
        "",
        "旧154/48组合的下游持仓排序可以复现，但48笔L的上游候选不是由完整涨停池按当前L2定义产生；",
        "同时12笔退出日不符合当前实盘T+2口径。因此旧154笔账户路径及其24175倍复利均已作废。",
    ]
    (OUTPUT_DIR / "current_l48_audit_report.md").write_text("\n".join(lines), encoding="utf-8")

    print("已作废旧154/48组合中的L专项审计完成")
    print(
        f"L2本体仍有效={int(detail['l2_body_pass'].fillna(False).sum())}/48，"
        f"每日第一名同股={int(detail['full_pool_selected_same_stock'].sum())}/48，"
        f"退出规则一致={int(detail['exit_rule_aligned'].sum())}/48"
    )
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
