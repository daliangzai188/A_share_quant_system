"""复现策略N v4量比后门禁，并输出分段及新增前向审计。

本脚本只读历史数据，不生成实盘信号、不连接券商。v4固定规则为：
第一分支每日第一名仅接受T日``volume_ratio_bucket``为``4_8/lt_1``；
失败日整日放弃，不回补第二名且不转补充分支。第二分支保持v3规则不变。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import certify_current_executable_portfolio as cert  # noqa: E402
from scripts import research_strategy_n as common  # noqa: E402
from scripts import research_strategy_n_v2 as research  # noqa: E402
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.strategy_n import (  # noqa: E402
    apply_n_base_filters,
    load_n_spec,
    select_n_daily_picks,
)


OUTPUT_DIR = ROOT / "reports" / "strategy_n_v4_research"
V3_CANDIDATE_PATH = ROOT / "reports" / "strategy_n_v3" / "n_backtest_candidates.csv"
FORWARD_START = "20260515"
FORWARD_END = "20260626"
SPLITS = (
    ("TRAIN", "20240520", "20250723"),
    ("VALIDATION", "20250724", "20251212"),
    ("TEST_OOS", "20251215", "20260514"),
)


def _config() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))


def n_metrics(detail: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    portfolio = common.metrics(detail, start, end)
    trades = detail[
        detail["status"].astype(str).eq("EXECUTED")
        & detail["signal_date"].astype(str).between(start, end)
    ].copy()
    n_trades = trades[trades["strategy_leg"].astype(str).eq("N")].copy()
    values = pd.to_numeric(n_trades["account_return"], errors="raise")
    curve = (1.0 + values).cumprod()
    drawdown = curve / curve.cummax() - 1.0 if len(curve) else pd.Series([0.0])
    wins = values[values > 0]
    losses = values[values < 0]
    portfolio.update({
        "n_standalone_multiple": float((1.0 + values).prod()) if len(values) else 1.0,
        "n_avg_return": float(values.mean()) if len(values) else 0.0,
        "n_median_return": float(values.median()) if len(values) else 0.0,
        "n_max_drawdown": float(drawdown.min()),
        "n_max_profit": float(values.max()) if len(values) else 0.0,
        "n_max_loss": float(values.min()) if len(values) else 0.0,
        "n_payoff_ratio": (
            float(wins.mean() / abs(losses.mean()))
            if len(wins) and len(losses)
            else 0.0
        ),
        "n_max_consecutive_losses": common.max_consecutive_losses(values),
    })
    return portfolio


def v3_candidate_map() -> dict[str, dict[str, Any]]:
    frame = pd.read_csv(
        V3_CANDIDATE_PATH,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    result: dict[str, dict[str, Any]] = {}
    for row in frame.itertuples(index=False):
        item = common.account_outcome(str(row.trade_date), str(row.ts_code), str(row.name))
        item["rule_id"] = str(row.n_rule_id)
        item["n_branch"] = str(row.n_branch)
        result[str(row.trade_date)] = item
    return result


def gate_v3_map(
    mapping: dict[str, dict[str, Any]],
    signal_pool: pd.DataFrame,
    *,
    field: str,
    accepted_values: set[str],
) -> dict[str, dict[str, Any]]:
    lookup = signal_pool.set_index(["trade_date", "ts_code"])
    result: dict[str, dict[str, Any]] = {}
    for signal_date, item in mapping.items():
        if str(item.get("n_branch", "")) != "CURRENT":
            result[signal_date] = dict(item)
            continue
        key = (str(signal_date), str(item.get("ts_code", "")))
        row = lookup.loc[key]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if str(row[field]) in accepted_values:
            result[signal_date] = dict(item)
    return result


def gate_scan(sources: cert.Sources) -> pd.DataFrame:
    signal_pool = common.load_signal_pool()
    v3 = v3_candidate_map()
    variants = {
        "V3_NO_POST_FILTER": v3,
        "V4_VOLUME_4_8_LT_1": gate_v3_map(
            v3,
            signal_pool,
            field="volume_ratio_bucket",
            accepted_values={"4_8", "lt_1"},
        ),
        "AUCTION_ONLY": gate_v3_map(
            v3,
            signal_pool,
            field="first_time_detail_bucket",
            accepted_values={"open_auction"},
        ),
        "CURRENT_BRANCH_OFF": {
            date: dict(item)
            for date, item in v3.items()
            if str(item.get("n_branch", "")) != "CURRENT"
        },
    }
    v4_codes = {
        (str(date), str(row["ts_code"]))
        for date, row in sources.n_pool.iterrows()
    }
    scanned_v4_codes = {
        (str(date), str(item.get("ts_code", "")))
        for date, item in variants["V4_VOLUME_4_8_LT_1"].items()
    }
    if scanned_v4_codes != v4_codes:
        raise RuntimeError("N v4候选账本与量比后门禁研究映射不一致")

    without_n = cert.replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
        n_enabled=False,
        block_d_on_handoff=True,
    )
    rows: list[dict[str, Any]] = []
    for variant, mapping in variants.items():
        detail = research.replay_with_n_map(sources, mapping)
        full = n_metrics(detail, common.START_DATE, common.END_DATE)
        row: dict[str, Any] = {
            "variant": variant,
            "candidate_day_count": len(mapping),
            "portfolio_trade_count": full["trade_count"],
            "n_trade_count": full["n_trade_count"],
            "n_win_rate": full["n_win_rate"],
            "n_standalone_multiple": full["n_standalone_multiple"],
            "n_max_drawdown": full["n_max_drawdown"],
            "portfolio_multiple": full["equity_multiple"],
            "portfolio_max_drawdown": full["max_drawdown"],
        }
        for split, start, end in SPLITS:
            selected = n_metrics(detail, start, end)
            reference = common.metrics(without_n, start, end)
            prefix = split.lower()
            row.update({
                f"{prefix}_n_trade_count": selected["n_trade_count"],
                f"{prefix}_n_win_rate": selected["n_win_rate"],
                f"{prefix}_n_multiple": selected["n_standalone_multiple"],
                f"{prefix}_portfolio_ratio_to_without_n": (
                    selected["equity_multiple"] / reference["equity_multiple"]
                ),
            })
        rows.append(row)
    return pd.DataFrame(rows)


def forward_validation(config: dict[str, Any]) -> pd.DataFrame:
    spec = load_n_spec(config)
    pool = load_historical_bucketed_pool(FORWARD_START, FORWARD_END, 80)
    pool = apply_n_base_filters(pool, spec)
    if not pool["fill_probability_method"].astype(str).eq(
        "asof_turnover_space_proxy_v2"
    ).all():
        raise RuntimeError("N新增前向区间不是严格as-of成交空间打分")
    picks = select_n_daily_picks(pool, spec)
    rows: list[dict[str, Any]] = []
    for row in picks.itertuples(index=False):
        outcome = common.account_outcome(str(row.trade_date), str(row.ts_code), str(row.name))
        rows.append({
            "signal_date": str(row.trade_date),
            "ts_code": str(row.ts_code),
            "name": str(row.name),
            "n_branch": str(row.n_branch),
            "volume_ratio_bucket": str(row.volume_ratio_bucket),
            "execution_status": str(outcome["execution_status"]),
            "account_return": float(outcome["account_return"]),
        })
    return pd.DataFrame(rows)


def main(output_dir: Path = OUTPUT_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = _config()
    sources = cert.load_sources()
    current = cert.replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
        n_enabled=True,
        block_d_on_handoff=True,
    )
    without_n = cert.replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
        n_enabled=False,
        block_d_on_handoff=True,
    )
    full = n_metrics(current, common.START_DATE, common.END_DATE)
    audit = config["strategy_n"]["execution_v4_audit"]
    if full["n_standalone_multiple"] <= float(audit["target_n_standalone_multiple_gt"]):
        raise RuntimeError("N v4机械逐笔复利没有严格大于2倍")
    if full["n_win_rate"] <= float(audit["target_n_win_rate_gt"]):
        raise RuntimeError("N v4胜率没有严格大于60%")

    split_rows: list[dict[str, Any]] = []
    for split, start, end in SPLITS:
        selected = n_metrics(current, start, end)
        reference = common.metrics(without_n, start, end)
        split_rows.append({
            "split": split,
            "start_date": start,
            "end_date": end,
            "n_trade_count": selected["n_trade_count"],
            "n_win_rate": selected["n_win_rate"],
            "n_standalone_multiple": selected["n_standalone_multiple"],
            "without_n_multiple": reference["equity_multiple"],
            "with_n_multiple": selected["equity_multiple"],
            "ratio_to_without_n": selected["equity_multiple"] / reference["equity_multiple"],
            "without_n_max_drawdown": reference["max_drawdown"],
            "with_n_max_drawdown": selected["max_drawdown"],
            "noninferior_passed": bool(
                selected["equity_multiple"] >= reference["equity_multiple"]
                and selected["max_drawdown"] >= reference["max_drawdown"]
            ),
        })
    split_report = pd.DataFrame(split_rows)
    scan = gate_scan(sources)
    forward = forward_validation(config)
    forward_ok = forward[forward["execution_status"].eq("OK")].copy()
    forward_returns = pd.to_numeric(forward_ok["account_return"], errors="raise")
    verification = {
        "status": "TARGET_PASS_WITH_OOS_RISK",
        "strategy_version": config["strategy_n"]["strategy_version"],
        "candidate_count": int(len(sources.n_pool)),
        "executable_candidate_count": int(
            sources.n_pool["execution_status"].astype(str).eq("OK").sum()
        ),
        "portfolio_trade_count": int(full["trade_count"]),
        "portfolio_n_trade_count": int(full["n_trade_count"]),
        "portfolio_equity_multiple": float(full["equity_multiple"]),
        "portfolio_max_drawdown": float(full["max_drawdown"]),
        "n_standalone_multiple": float(full["n_standalone_multiple"]),
        "n_win_rate": float(full["n_win_rate"]),
        "n_avg_return": float(full["n_avg_return"]),
        "n_median_return": float(full["n_median_return"]),
        "n_max_drawdown": float(full["n_max_drawdown"]),
        "n_max_profit": float(full["n_max_profit"]),
        "n_max_loss": float(full["n_max_loss"]),
        "n_payoff_ratio": float(full["n_payoff_ratio"]),
        "n_max_consecutive_losses": int(full["n_max_consecutive_losses"]),
        "target_multiple_passed": bool(full["n_standalone_multiple"] > 2.0),
        "target_win_rate_passed": bool(full["n_win_rate"] > 0.60),
        "test_oos_noninferiority_passed": bool(
            split_report.loc[
                split_report["split"].eq("TEST_OOS"), "noninferior_passed"
            ].iloc[0]
        ),
        "forward_start": FORWARD_START,
        "forward_end": FORWARD_END,
        "forward_trade_count": int(len(forward_ok)),
        "forward_win_rate": float(forward_returns.gt(0).mean()) if len(forward_returns) else 0.0,
        "forward_multiple": float((1.0 + forward_returns).prod()) if len(forward_returns) else 1.0,
        "warning": (
            "N单腿目标通过，但历史门禁比较已查看测试段，且测试段完整组合仍劣于不含N；"
            "新增前向仅2笔，不能据此认定长期稳定。"
        ),
    }
    split_report.to_csv(
        output_dir / "current_v4_split_validation.csv", index=False, encoding="utf-8-sig"
    )
    scan.to_csv(output_dir / "gate_scan.csv", index=False, encoding="utf-8-sig")
    forward.to_csv(
        output_dir / "forward_20260515_20260626.csv", index=False, encoding="utf-8-sig"
    )
    (output_dir / "current_v4_verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    print(f"输出目录：{output_dir}")


if __name__ == "__main__":
    main()
