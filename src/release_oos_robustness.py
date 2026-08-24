"""冻结发布版本的样本外稳健性评估。

只消费影子账本、发布清单和真实成交完成汇总；所有结论均为报告状态，绝不
返回或修改实盘门禁。发布编号与样本外起点不匹配的记录会被直接排除。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.live_performance import completed_live_trades
from src.shadow_candidate_ledger import LEGS, _date, _read_csv, _read_json, load_release


REPORT_SCHEMA_VERSION = 1


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _maximum_consecutive_losses(values: pd.Series) -> int:
    current = maximum = 0
    for value in values:
        if float(value) < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def return_metrics(frame: pd.DataFrame, segment: str, return_column: str) -> dict[str, Any]:
    ordered = frame.copy()
    date_column = "signal_date"
    if "planned_buy_date" in ordered and ordered["planned_buy_date"].map(_date).ne("").any():
        date_column = "planned_buy_date"
    if date_column in ordered:
        ordered = ordered.sort_values(
            [date_column, "priority_rank"]
            if "priority_rank" in ordered
            else [date_column]
        )
    returns = pd.to_numeric(ordered.get(return_column, pd.Series(dtype=float)), errors="coerce").dropna()
    if returns.empty:
        return {
            "segment": segment, "sample_count": 0, "win_rate": 0.0,
            "avg_return": 0.0, "median_return": 0.0, "total_return_sum": 0.0,
            "compound_multiple": 1.0, "max_drawdown": 0.0, "profit_loss_ratio": 0.0,
            "max_profit": 0.0, "max_loss": 0.0, "max_consecutive_losses": 0,
            "sample_start": "", "sample_end": "",
        }
    curve = (1.0 + returns).cumprod()
    peak = curve.cummax().clip(lower=1.0)
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    dates = (
        ordered.loc[returns.index, date_column].map(_date)
        if date_column in ordered
        else pd.Series(dtype=str)
    )
    return {
        "segment": segment,
        "sample_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "total_return_sum": float(returns.sum()),
        "compound_multiple": float(curve.iloc[-1]),
        "max_drawdown": float((curve / peak - 1.0).min()),
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": _maximum_consecutive_losses(returns),
        "sample_start": str(dates.min()) if len(dates) else "",
        "sample_end": str(dates.max()) if len(dates) else "",
    }


def load_release_ledger(root: Path, release: Mapping[str, Any]) -> pd.DataFrame:
    ledger = _read_csv(root / "reports" / "oos_shadow" / "shadow_candidates.csv")
    if ledger.empty:
        return ledger
    required = {"release_id", "signal_date", "strategy_leg", "candidate_status", "counterfactual_status"}
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise ValueError("影子账本缺少字段：" + "、".join(missing))
    ledger["signal_date"] = ledger["signal_date"].map(_date)
    if "planned_buy_date" in ledger:
        ledger["planned_buy_date"] = ledger["planned_buy_date"].map(_date)
    return ledger[
        ledger["release_id"].astype(str).eq(str(release["release_id"]))
        & ledger["signal_date"].ge(_date(release["oos_start_date"]))
        & ledger["strategy_leg"].astype(str).isin(LEGS)
    ].copy()


def coverage_metrics(ledger: pd.DataFrame) -> pd.DataFrame:
    days = sorted(ledger["signal_date"].astype(str).unique()) if not ledger.empty else []
    expected = len(days) * len(LEGS)
    evaluated = ledger[~ledger["candidate_status"].astype(str).eq("NOT_OBSERVED")] if not ledger.empty else ledger
    rows = [{
        "segment": "发布版本整体",
        "signal_day_count": len(days),
        "expected_leg_rows": expected,
        "observed_leg_rows": len(ledger),
        "state_row_coverage": len(ledger) / expected if expected else 0.0,
        "evaluated_leg_rows": len(evaluated),
        "evaluated_coverage": len(evaluated) / expected if expected else 0.0,
        "candidate_count": int(ledger["candidate_status"].astype(str).eq("CANDIDATE").sum()) if not ledger.empty else 0,
        "resolved_count": int(ledger["counterfactual_status"].astype(str).eq("RESOLVED").sum()) if not ledger.empty else 0,
        "entry_unfillable_count": int(ledger["counterfactual_status"].astype(str).eq("ENTRY_UNFILLABLE").sum()) if not ledger.empty else 0,
        "not_observed_count": int(ledger["candidate_status"].astype(str).eq("NOT_OBSERVED").sum()) if not ledger.empty else 0,
    }]
    for leg in LEGS:
        group = ledger[ledger["strategy_leg"].astype(str).eq(leg)] if not ledger.empty else ledger
        evaluated_group = group[~group["candidate_status"].astype(str).eq("NOT_OBSERVED")] if not group.empty else group
        rows.append({
            "segment": f"策略{leg}",
            "signal_day_count": len(days),
            "expected_leg_rows": len(days),
            "observed_leg_rows": len(group),
            "state_row_coverage": len(group) / len(days) if days else 0.0,
            "evaluated_leg_rows": len(evaluated_group),
            "evaluated_coverage": len(evaluated_group) / len(days) if days else 0.0,
            "candidate_count": int(group["candidate_status"].astype(str).eq("CANDIDATE").sum()) if not group.empty else 0,
            "resolved_count": int(group["counterfactual_status"].astype(str).eq("RESOLVED").sum()) if not group.empty else 0,
            "entry_unfillable_count": int(group["counterfactual_status"].astype(str).eq("ENTRY_UNFILLABLE").sum()) if not group.empty else 0,
            "not_observed_count": int(group["candidate_status"].astype(str).eq("NOT_OBSERVED").sum()) if not group.empty else 0,
        })
    return pd.DataFrame(rows)


def priority_pair_metrics(resolved: pd.DataFrame, minimum_pairs: int) -> pd.DataFrame:
    columns = [
        "challenger_leg", "paired_sample_count", "challenger_better_rate",
        "avg_return_delta", "median_return_delta", "challenger_compound_multiple",
        "winner_compound_multiple", "priority_change_evidence",
    ]
    if resolved.empty:
        return pd.DataFrame(columns=columns)
    paired_source = resolved.copy()
    paired_source["action_date"] = (
        paired_source["planned_buy_date"].map(_date)
        if "planned_buy_date" in paired_source
        else paired_source["signal_date"].map(_date)
    )
    winner_flags = paired_source.get(
        "account_empty_winner", pd.Series(False, index=paired_source.index)
    )
    winners = paired_source[
        winner_flags
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
    ].copy()
    winners = winners[winners["action_date"].ne("")]
    if winners.empty:
        return pd.DataFrame(columns=columns)
    winner_view = winners[["action_date", "strategy_leg", "account_net_return"]].rename(columns={
        "strategy_leg": "winner_leg", "account_net_return": "winner_return",
    })
    rows: list[dict[str, Any]] = []
    for leg in LEGS:
        challengers = paired_source[
            paired_source["strategy_leg"].astype(str).eq(leg)
            & paired_source["action_date"].ne("")
        ][["action_date", "account_net_return"]].rename(
            columns={"account_net_return": "challenger_return"}
        )
        paired = challengers.merge(winner_view, on="action_date", how="inner")
        paired = paired[paired["winner_leg"].astype(str).ne(leg)].copy()
        if paired.empty:
            continue
        challenger_return = pd.to_numeric(paired["challenger_return"], errors="coerce")
        winner_return = pd.to_numeric(paired["winner_return"], errors="coerce")
        delta = challenger_return - winner_return
        enough = len(paired) >= minimum_pairs
        rows.append({
            "challenger_leg": leg,
            "paired_sample_count": len(paired),
            "challenger_better_rate": float(delta.gt(0).mean()),
            "avg_return_delta": float(delta.mean()),
            "median_return_delta": float(delta.median()),
            "challenger_compound_multiple": float((1 + challenger_return).prod()),
            "winner_compound_multiple": float((1 + winner_return).prod()),
            "priority_change_evidence": "REVIEW" if enough and delta.mean() > 0 and delta.median() > 0 else "INSUFFICIENT_OR_NO_EDGE",
        })
    return pd.DataFrame(rows, columns=columns)


def _live_report_config(root: Path) -> dict[str, Any]:
    config = _read_json(root / "config" / "config.json", {})
    report = dict(config.get("live_performance_report", {}))
    analysis = config.get("analysis", {})
    for key in (
        "commission_rate", "stamp_tax_rate", "stamp_tax_schedule",
        "transfer_fee_rate", "minimum_commission",
    ):
        report.setdefault(key, analysis.get(key))
    return report


def actual_release_trades(root: Path, release: Mapping[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = root / "reports" / "execution_tracking" / "trade_completion_summary.csv"
    raw = _read_csv(source)
    if raw.empty:
        return pd.DataFrame(), {"active_trade_rows": 0, "complete_trade_rows": 0, "incomplete_trade_rows": 0, "data_complete_rate": 0.0}
    trades, quality = completed_live_trades(raw, _live_report_config(root))
    if trades.empty:
        return trades, quality
    entry_dates = trades["entry_date"].map(_date)
    return trades[entry_dates.ge(_date(release["oos_start_date"]))].copy(), quality


def _historical_reference(root: Path) -> dict[str, Any]:
    cert = _read_json(root / "reports" / "current_portfolio_alignment" / "live_certification.json", {})
    metrics = cert.get("metrics", cert)
    return {
        "status": cert.get("status", ""),
        "executed_trade_count": int(metrics.get("executed_trade_count", cert.get("executed_trade_count", 0)) or 0),
        "equity_multiple": float(metrics.get("equity_multiple", cert.get("equity_multiple", 0.0)) or 0.0),
        "fixed_initial_notional_multiple": float(metrics.get("fixed_initial_notional_multiple", cert.get("fixed_initial_notional_multiple", 0.0)) or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown", cert.get("max_drawdown", 0.0)) or 0.0),
    }


def _markdown_table(frame: pd.DataFrame, percent_columns: set[str] | None = None) -> str:
    if frame.empty:
        return "暂无数据。"
    percent_columns = percent_columns or set()
    view = frame.copy()
    for column in view.columns:
        if column in percent_columns:
            view[column] = pd.to_numeric(view[column], errors="coerce").map(lambda value: f"{value:.2%}")
        elif pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(lambda value: f"{value:.4f}")
    headers = list(view.columns)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.fillna("").values.tolist())
    return "\n".join(lines)


def evaluate_release_oos(root: Path) -> dict[str, Any]:
    release = load_release(root)
    ledger = load_release_ledger(root, release)
    resolved = ledger[ledger["counterfactual_status"].astype(str).eq("RESOLVED")].copy() if not ledger.empty else ledger
    candidates = ledger[ledger["candidate_status"].astype(str).eq("CANDIDATE")].copy() if not ledger.empty else ledger
    winners = resolved[resolved.get("account_empty_winner", pd.Series(False, index=resolved.index)).astype(str).str.lower().isin(["true", "1"])] if not resolved.empty else resolved
    live_shadow = resolved[resolved.get("live_selected", pd.Series(False, index=resolved.index)).astype(str).str.lower().isin(["true", "1"])] if not resolved.empty else resolved
    config = _live_report_config(root)
    minimum = int(config.get("minimum_samples_for_decision", 20))

    overall = pd.DataFrame([
        return_metrics(resolved, "全部影子候选（诊断层，候选会重叠）", "account_net_return"),
        return_metrics(winners, "账户空仓时按冻结优先级胜出", "account_net_return"),
        return_metrics(live_shadow, "影子候选中实际进入实盘计划", "account_net_return"),
    ])
    by_leg = pd.DataFrame([
        return_metrics(resolved[resolved["strategy_leg"].astype(str).eq(leg)] if not resolved.empty else resolved, f"策略{leg}", "account_net_return")
        for leg in LEGS
    ])
    pairs = priority_pair_metrics(resolved, minimum)
    coverage = coverage_metrics(ledger)
    actual, quality = actual_release_trades(root, release)
    actual_for_metrics = actual.rename(columns={"entry_date": "signal_date", "net_return": "actual_net_return"}) if not actual.empty else actual
    actual_metrics = return_metrics(actual_for_metrics, "样本外真实完整成交（按入场日绑定）", "actual_net_return")

    winner_count = int(overall.loc[overall["segment"].eq("账户空仓时按冻结优先级胜出"), "sample_count"].iloc[0])
    actual_count = int(actual_metrics["sample_count"])
    if winner_count == 0 and actual_count == 0:
        status = "NO_SAMPLE"
        reason = "发布版本尚无已完成的优先级影子收益或真实成交，只建立口径，不评价策略优劣。"
    elif winner_count < minimum or actual_count < minimum:
        status = "EARLY_OBSERVATION"
        reason = f"优先级影子完整样本{winner_count}笔、真实完整成交{actual_count}笔，任一少于{minimum}笔；只观察，不改策略。"
    else:
        status = "REVIEW_READY"
        reason = f"优先级影子与真实完整成交均达到{minimum}笔，可进入人工稳健性复核；仍不自动改策略。"
    pair_review = int(pairs["priority_change_evidence"].astype(str).eq("REVIEW").sum()) if not pairs.empty else 0
    decision = "HOLD_RELEASE" if status != "REVIEW_READY" or pair_review == 0 else "MANUAL_REVIEW_ONLY"
    return {
        "release": release,
        "ledger": ledger,
        "candidates": candidates,
        "resolved": resolved,
        "overall": overall,
        "by_leg": by_leg,
        "pairs": pairs,
        "coverage": coverage,
        "actual_metrics": actual_metrics,
        "actual_quality": quality,
        "historical_reference": _historical_reference(root),
        "status": status,
        "reason": reason,
        "optimization_decision": decision,
        "minimum_samples": minimum,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_release_oos_report(root: Path) -> dict[str, Any]:
    result = evaluate_release_oos(root)
    output = root / "reports" / "oos_evaluation"
    _atomic_csv(output / "release_oos_metrics.csv", result["overall"])
    _atomic_csv(output / "release_oos_by_leg.csv", result["by_leg"])
    _atomic_csv(output / "release_oos_priority_pairs.csv", result["pairs"])
    _atomic_csv(output / "release_oos_coverage.csv", result["coverage"])
    release = result["release"]
    historical = result["historical_reference"]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": result["status"],
        "reason": result["reason"],
        "optimization_decision": result["optimization_decision"],
        "release_id": release["release_id"],
        "oos_start_date": release["oos_start_date"],
        "minimum_samples_for_review": result["minimum_samples"],
        "signal_day_count": int(result["ledger"]["signal_date"].nunique()) if not result["ledger"].empty else 0,
        "candidate_count": len(result["candidates"]),
        "resolved_candidate_count": len(result["resolved"]),
        "priority_winner_resolved_count": int(result["overall"].iloc[1]["sample_count"]),
        "actual_complete_trade_count": int(result["actual_metrics"]["sample_count"]),
        "evaluated_leg_rows": int(result["coverage"].iloc[0]["evaluated_leg_rows"]),
        "evaluated_coverage": float(result["coverage"].iloc[0]["evaluated_coverage"]),
        "priority_winner_metrics": result["overall"].iloc[1].to_dict(),
        "actual_metrics": result["actual_metrics"],
        "by_leg_metrics": result["by_leg"].to_dict(orient="records"),
        "priority_pair_metrics": result["pairs"].to_dict(orient="records"),
        "historical_reference": historical,
        "actual_data_quality": result["actual_quality"],
        "generated_at": result["generated_at"],
        "live_gate_enforced": False,
        "note": "报告只读且不接入下单。全部影子候选层可能重叠；优先级胜出与成对反事实均按真实planned_buy_date比较，D当日盘中与前一晚A/C/E计划处于同一action_date时才构成资金竞争。真实成交按入场日>=OOS起点绑定，旧成交不混入。",
    }
    _atomic_text(output / "release_oos_status.json", json.dumps(payload, ensure_ascii=False, indent=2))
    report = [
        "# 发布版本样本外稳健性评估",
        "",
        f"- 发布：`{release['release_id']}`",
        f"- 样本外起点：`{release['oos_start_date']}`",
        f"- 状态：**{result['status']}**",
        f"- 结论：{result['reason']}",
        f"- 优化动作：**{result['optimization_decision']}**（报告不接入下单门禁）",
        "- 固定规则：优先级影子样本和真实完整成交均至少20笔，才允许进入人工复核；任何优先级替换还需同一真实开仓日成对样本至少20笔，且平均差、中位数差均为正。",
        "",
        "## 数据覆盖",
        "",
        _markdown_table(result["coverage"], {"state_row_coverage", "evaluated_coverage"}),
        "",
        "## 发布版本整体",
        "",
        _markdown_table(result["overall"], {"win_rate", "avg_return", "median_return", "total_return_sum", "max_drawdown", "max_profit", "max_loss"}),
        "",
        "## 分策略",
        "",
        _markdown_table(result["by_leg"], {"win_rate", "avg_return", "median_return", "total_return_sum", "max_drawdown", "max_profit", "max_loss"}),
        "",
        "## 优先级反事实成对比较",
        "",
        _markdown_table(result["pairs"], {"challenger_better_rate", "avg_return_delta", "median_return_delta"}),
        "",
        "## 真实成交与历史参照",
        "",
        f"- 样本外真实完整成交：{result['actual_metrics']['sample_count']}笔；平均净收益{result['actual_metrics']['avg_return']:.2%}；最大回撤{result['actual_metrics']['max_drawdown']:.2%}。",
        f"- 冻结历史认证仅作参照：{historical['executed_trade_count']}笔，理论复利{historical['equity_multiple']:.4f}倍，固定初始名义金额倍数{historical['fixed_initial_notional_multiple']:.4f}倍，最大回撤{historical['max_drawdown']:.2%}。",
        "- 历史理论倍数不能当作未来收益预期，也不能与少量样本外交易直接等同比较。",
        "- 当前报告不会关闭策略、调腿序、改仓位或改变实盘下单。",
    ]
    _atomic_text(output / "release_oos_report.md", "\n".join(report))
    return payload
