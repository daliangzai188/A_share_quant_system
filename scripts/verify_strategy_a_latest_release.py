#!/usr/bin/env python3
"""核对最新策略A从研究结果到生产配置和正式严格证书的逐笔一致性。

本脚本只读代码、配置和历史数据，只写审计报告；不连接QMT、不提交委托。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import certify_strict_asof_portfolio as certifier  # noqa: E402
from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from src.mechanical_compound import mechanical_compound_frame  # noqa: E402
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.trading_fees import stamp_tax_rate_for_date  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


START = "20240630"
END = "20260630"
EXPECTED_RELEASE_ID = "A_FD_1_2_NON_ONE_WORD_FALLBACK_20260630_V1"
EXPECTED_FALLBACK_ID = "A_FD_1PCT_2PCT_NON_ONE_WORD_FALLBACK"
EXPECTED_A_TRADES = 82
EXPECTED_A_MULTIPLE = 94.39844282719737
EXPECTED_COMBO_TRADES = 143
EXPECTED_COMBO_MULTIPLE = 2280.9020459698163
TOLERANCE = 1e-12

RESEARCH_DIR = ROOT / "reports" / "strategy_a_current_window" / "20260630_ac_ed"
RESEARCH_PICKS = RESEARCH_DIR / "posthoc_robust_observation_picks.csv"
RESEARCH_COMBO = RESEARCH_DIR / "posthoc_robust_observation_trades.csv"
OUTPUT_DIR = ROOT / "reports" / "strategy_a_release_verification" / "20260824"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
REPORT_PATH = OUTPUT_DIR / "report.md"
IDENTITY_PATH = OUTPUT_DIR / "candidate_identity.csv"
FEE_PATH = OUTPUT_DIR / "a_fee_recalculation.csv"
COMBO_IDENTITY_PATH = OUTPUT_DIR / "combo_identity.csv"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def rank_daily(
    pool: pd.DataFrame,
    generator: PaperCandidateGenerator,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for _, daily in pool.groupby("trade_date", sort=True):
        ranked = generator.rank_candidates(daily.copy()).reset_index(drop=True)
        if not ranked.empty:
            rows.append(ranked.head(1))
    if not rows:
        return pd.DataFrame(columns=pool.columns)
    return pd.concat(rows, ignore_index=True).sort_values("trade_date").reset_index(drop=True)


def identity_frame(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    date_column: str,
) -> pd.DataFrame:
    left = expected[[date_column, "ts_code"]].copy().rename(
        columns={"ts_code": "expected_ts_code"}
    )
    right = actual[[date_column, "ts_code"]].copy().rename(
        columns={"ts_code": "actual_ts_code"}
    )
    merged = left.merge(right, on=date_column, how="outer", indicator=True)
    merged["identity_equal"] = (
        merged["_merge"].eq("both")
        & merged["expected_ts_code"].astype(str).eq(
            merged["actual_ts_code"].astype(str)
        )
    )
    return merged.sort_values(date_column).reset_index(drop=True)


def verify_config(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    filters = config.get("candidate_filters", {})
    fallback = filters.get("fallback_when_primary_empty", {})
    conditions = {
        str(item.get("column", "")): str(item.get("value", ""))
        for item in fallback.get("conditions", [])
    }
    excludes = {
        (str(item.get("column", "")), str(item.get("value", "")))
        for item in fallback.get("exclude_conditions", [])
    }
    checks = {
        "release_id": config.get("release_id") == EXPECTED_RELEASE_ID,
        "fallback_enabled": fallback.get("enabled") is True,
        "fallback_id": fallback.get("fallback_id") == EXPECTED_FALLBACK_ID,
        "same_trade_date_only": fallback.get("same_trade_date_only") is True,
        "inherit_primary_ranking": fallback.get("inherit_primary_ranking") is True,
        "fallback_conditions": conditions
        == {
            "segment_limit_up_count_bucket": "lt_5",
            "market_chain_count_bucket": "8_15",
            "fd_ratio_bucket": "1pct_2pct",
        },
        "fallback_excludes_one_word": ("board_type", "one_word") in excludes,
        "priority_a_c_e_d": runtime.get("portfolio_certification", {}).get(
            "strategy_priority_order"
        )
        == ["A", "C", "E", "D"],
        "runtime_live_mode": str(runtime.get("trade_mode", "")).lower() == "live",
        "real_order_enabled": runtime.get("live_trade", {}).get("real_order_enabled")
        is True,
        "risk_gateway_enabled": all(
            runtime.get("live_trade", {}).get(key) is True
            for key in (
                "enforce_trading_time",
                "reject_limit_up_buy",
                "duplicate_order_check",
                "fill_confirm_enabled",
            )
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"最新A生产配置检查失败: {failed}")
    return checks


def fee_recalculation(a_outcomes: pd.DataFrame) -> pd.DataFrame:
    commission = float(strict.COMMISSION_RATE)
    transfer = float(strict.TRANSFER_FEE_RATE)
    rows: list[dict[str, Any]] = []
    for _, row in a_outcomes[a_outcomes["status"].eq("OK")].iterrows():
        stock_return = float(row["stock_return_before_fees"])
        exit_date = str(row["exit_date"])
        stamp = stamp_tax_rate_for_date(exit_date, strict.STAMP_TAX_SCHEDULE)
        independent = (
            stock_return
            - commission
            - transfer
            - (1.0 + stock_return) * (commission + transfer + stamp)
        ) * float(strict.POSITION_PCT)
        actual = float(row["account_return"])
        rows.append(
            {
                "signal_date": str(row["signal_date"]),
                "ts_code": str(row["ts_code"]),
                "exit_date": exit_date,
                "stock_return_before_fees": stock_return,
                "commission_rate": commission,
                "transfer_fee_rate": transfer,
                "stamp_tax_rate": stamp,
                "position_pct": float(strict.POSITION_PCT),
                "independent_account_return": independent,
                "certified_account_return": actual,
                "absolute_error": abs(independent - actual),
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def verify_combo_identity(expected: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    columns = ["action_date", "signal_date", "strategy_leg", "ts_code", "account_return"]
    left = expected[columns].copy().rename(
        columns={
            "signal_date": "expected_signal_date",
            "strategy_leg": "expected_strategy_leg",
            "ts_code": "expected_ts_code",
            "account_return": "expected_account_return",
        }
    )
    right = actual[actual["status"].eq("EXECUTED")][columns].copy().rename(
        columns={
            "signal_date": "actual_signal_date",
            "strategy_leg": "actual_strategy_leg",
            "ts_code": "actual_ts_code",
            "account_return": "actual_account_return",
        }
    )
    merged = left.merge(right, on="action_date", how="outer", indicator=True)
    merged["identity_equal"] = (
        merged["_merge"].eq("both")
        & merged["expected_signal_date"].astype(str).eq(
            merged["actual_signal_date"].astype(str)
        )
        & merged["expected_strategy_leg"].astype(str).eq(
            merged["actual_strategy_leg"].astype(str)
        )
        & merged["expected_ts_code"].astype(str).eq(
            merged["actual_ts_code"].astype(str)
        )
        & np.isclose(
            pd.to_numeric(merged["expected_account_return"], errors="coerce"),
            pd.to_numeric(merged["actual_account_return"], errors="coerce"),
            atol=TOLERANCE,
            rtol=0,
        )
    )
    return merged.sort_values("action_date").reset_index(drop=True)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_json_config(ROOT / "config" / "strategy_config.json")
    runtime = load_json_config(ROOT / "config" / "config.json")
    config_checks = verify_config(config, runtime)

    generator = PaperCandidateGenerator(
        ROOT / "config" / "strategy_config.json",
        input_trades_path=strict.STRICT_SOURCE,
    )
    all_candidates = generator.load_all_candidates()
    pool = generator.apply_strategy_filters(all_candidates)
    pool = pool[pool["trade_date"].astype(str).between(START, END)].copy()
    production_picks = rank_daily(pool, generator)
    research_picks = pd.read_csv(
        RESEARCH_PICKS,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    identity = identity_frame(
        research_picks,
        production_picks,
        date_column="trade_date",
    )
    identity.to_csv(IDENTITY_PATH, index=False, encoding="utf-8-sig")
    if not identity["identity_equal"].all():
        raise RuntimeError("最新A生产候选与研究候选逐日身份不一致")

    fallback_picks = production_picks[
        production_picks.get(
            "matched_condition_profile_ids",
            pd.Series("", index=production_picks.index),
        )
        .fillna("")
        .astype(str)
        .eq(EXPECTED_FALLBACK_ID)
    ].copy()
    primary_dates = set(
        production_picks.loc[
            ~production_picks.index.isin(fallback_picks.index), "trade_date"
        ].astype(str)
    )
    fallback_checks = {
        "fallback_pick_count": int(len(fallback_picks)),
        "fallback_all_fd_1_2": bool(
            fallback_picks["fd_ratio_bucket"].astype(str).eq("1pct_2pct").all()
        ),
        "fallback_all_non_one_word": bool(
            ~fallback_picks["board_type"].astype(str).eq("one_word").any()
        ),
        "fallback_never_competes_with_primary": bool(
            not fallback_picks["trade_date"].astype(str).isin(primary_dates).any()
        ),
    }
    if not all(
        value for key, value in fallback_checks.items() if key != "fallback_pick_count"
    ):
        raise RuntimeError(f"A补位语义检查失败: {fallback_checks}")

    _source_audit, official_daily, legs = certifier.build_strict_snapshot()
    a_outcomes = legs["A"].copy()
    a_standalone = strict.replay_by_action_date({"A": a_outcomes}, ("A",))
    a_metrics = strict.combo_metrics(a_standalone)
    combo_metrics = strict.combo_metrics(official_daily)
    mechanical = mechanical_compound_frame(
        official_daily[official_daily["status"].eq("EXECUTED")]
    )
    metric_checks = {
        "a_trade_count": int(a_metrics["trade_count"]) == EXPECTED_A_TRADES,
        "a_equity_multiple": abs(
            float(a_metrics["equity_multiple"]) - EXPECTED_A_MULTIPLE
        )
        <= TOLERANCE,
        "combo_trade_count": int(combo_metrics["trade_count"])
        == EXPECTED_COMBO_TRADES,
        "combo_equity_multiple": abs(
            float(combo_metrics["equity_multiple"]) - EXPECTED_COMBO_MULTIPLE
        )
        <= TOLERANCE,
        "mechanical_compound_matches": abs(
            mechanical.equity_multiple - float(combo_metrics["equity_multiple"])
        )
        <= TOLERANCE,
    }
    if not all(metric_checks.values()):
        raise RuntimeError(f"最新A正式指标锚点检查失败: {metric_checks}")

    fees = fee_recalculation(a_outcomes)
    fees.to_csv(FEE_PATH, index=False, encoding="utf-8-sig")
    maximum_fee_error = float(fees["absolute_error"].max()) if not fees.empty else 0.0
    if maximum_fee_error > TOLERANCE:
        raise RuntimeError(f"A逐笔费用独立复算不一致: max_error={maximum_fee_error}")

    expected_combo = pd.read_csv(
        RESEARCH_COMBO,
        dtype={"action_date": str, "signal_date": str, "ts_code": str},
        low_memory=False,
    )
    combo_identity = verify_combo_identity(expected_combo, official_daily)
    combo_identity.to_csv(COMBO_IDENTITY_PATH, index=False, encoding="utf-8-sig")
    if not combo_identity["identity_equal"].all():
        raise RuntimeError("研究组合逐笔与正式证书逐笔不一致")

    freeze = load_json_config(ROOT / "config" / "strategy_release_freeze.json")
    summary = {
        "schema_version": 1,
        "verification_status": "PASS",
        "release_id": EXPECTED_RELEASE_ID,
        "window": f"{START}~{END}",
        "config_checks": config_checks,
        "candidate_identity_count": int(len(identity)),
        "candidate_identity_mismatch_count": int((~identity["identity_equal"]).sum()),
        "fallback_checks": fallback_checks,
        "fee_recalculation_count": int(len(fees)),
        "maximum_fee_recalculation_error": maximum_fee_error,
        "combo_identity_count": int(len(combo_identity)),
        "combo_identity_mismatch_count": int(
            (~combo_identity["identity_equal"]).sum()
        ),
        "a_metrics": a_metrics,
        "combo_metrics": combo_metrics,
        "mechanical_compound": mechanical.to_dict(),
        "formal_release_certification": {
            "status": freeze.get("status"),
            "certification_status": freeze.get("certification_status"),
            "capacity_certified": freeze.get("capacity_certified"),
        },
        "conclusion": (
            "研究候选、生产候选、费用公式、真实开仓日组合逐笔和机械复利全部一致；"
            "未发现收益计算错误。正式样本外与容量认证状态仍保持未通过，不伪造。"
        ),
    }
    atomic_json(SUMMARY_PATH, summary)
    REPORT_PATH.write_text(
        "\n".join(
            [
                "# 最新策略A落地逐笔核验",
                "",
                "- 核验结论：PASS，未发现收益计算错误。",
                f"- 生产/研究候选逐日一致：{len(identity)}日，差异0日。",
                f"- A补位候选：{len(fallback_picks)}日；全部为1%-2%封单、非一字板，且未与原A同日竞争。",
                f"- A逐笔费用复算：{len(fees)}笔，最大绝对误差{maximum_fee_error:.3e}。",
                f"- 研究/正式组合逐笔一致：{len(combo_identity)}笔，差异0笔。",
                f"- A独立：{int(a_metrics['trade_count'])}笔，{float(a_metrics['equity_multiple']):.12f}倍，最大回撤{float(a_metrics['max_drawdown']):.4%}。",
                f"- A>C>E>D：{int(combo_metrics['trade_count'])}笔，{float(combo_metrics['equity_multiple']):.12f}倍，最大回撤{float(combo_metrics['max_drawdown']):.4%}。",
                "- 费用口径：双边佣金、双边过户费、日期化印花税；价格收益已含买卖各0.1%滑点；仓位82.5%。",
                "- 资金口径：按真实buy_date执行，退出日收盘后才释放资金，固定A>C>E>D。",
                "- 风险状态：STRICT_DISCOVERY；未触碰样本外与容量认证仍未通过，不代表未来收益。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
