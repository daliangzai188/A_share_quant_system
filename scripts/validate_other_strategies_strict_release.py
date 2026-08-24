#!/usr/bin/env python3
"""用统一发布门禁验证 D/A/E/C，禁止回溯点估计冒充实盘证书。

输入均来自严格 as-of 固定规则审计和五年逐年嵌套外层 OOS。脚本只读研究
产物，不修改实盘配置、不连接券商、不生成订单。最终报告会分别展示组合内
回溯点估计与只用过去选规则的外层 OOS，并核验组合边际、untouched OOS、
容量认证和正式发布状态。
"""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEGS = ("D", "A", "E", "C")
BOOTSTRAP_SEED = 20260821
BOOTSTRAP_SAMPLES = 50_000
OUTPUT_DIR = ROOT / "reports" / "other_strategies_strict_validation"
STRICT_SUMMARY = ROOT / "reports" / "strict_live_strategy_audit" / "strict_audit_summary.json"
STRICT_PORTFOLIO = ROOT / "reports" / "strict_live_strategy_audit" / "strict_portfolio_daily.csv"
STRICT_RELEASE_GATES = ROOT / "reports" / "strict_live_strategy_audit" / "strict_release_gates.csv"
NESTED_LEGS = ROOT / "reports" / "five_year_walk_forward" / "leg_oos_metrics.csv"
NESTED_MARGINAL = ROOT / "reports" / "five_year_walk_forward" / "leg_marginal_impact.csv"
NESTED_CAPACITY = ROOT / "reports" / "five_year_walk_forward" / "capacity_proxy.csv"
FORWARD_FREEZE = ROOT / "reports" / "five_year_walk_forward" / "forward_freeze_candidates.json"
PUBLICATION_GATE = ROOT / "reports" / "five_year_walk_forward" / "publication_gate.json"
LIVE_CERTIFICATION = ROOT / "reports" / "current_portfolio_alignment" / "live_certification.json"
LIVE_FREEZE = ROOT / "config" / "strategy_release_freeze.json"

SOURCE_PATHS = (
    STRICT_SUMMARY,
    STRICT_PORTFOLIO,
    STRICT_RELEASE_GATES,
    NESTED_LEGS,
    NESTED_MARGINAL,
    NESTED_CAPACITY,
    FORWARD_FREEZE,
    PUBLICATION_GATE,
    LIVE_CERTIFICATION,
    LIVE_FREEZE,
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def wilson_interval(wins: int, count: int) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 0.0
    z = 1.959963984540054
    rate = wins / count
    denominator = 1.0 + z * z / count
    center = (rate + z * z / (2.0 * count)) / denominator
    half = z * math.sqrt(
        rate * (1.0 - rate) / count + z * z / (4.0 * count * count)
    ) / denominator
    return center - half, center + half


def max_consecutive_losses(values: np.ndarray) -> int:
    longest = current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def return_metrics(values: np.ndarray) -> dict[str, Any]:
    if values.size == 0:
        raise RuntimeError("组合实际成交样本为空")
    wins = int((values > 0).sum())
    lower, upper = wilson_interval(wins, int(values.size))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    bootstrap_means = rng.choice(
        values,
        size=(BOOTSTRAP_SAMPLES, int(values.size)),
        replace=True,
    ).mean(axis=1)
    curve = np.cumprod(1.0 + values)
    drawdown = curve / np.maximum.accumulate(curve) - 1.0
    positive = values[values > 0]
    negative = values[values < 0]
    return {
        "trade_count": int(values.size),
        "win_count": wins,
        "win_rate": float(wins / values.size),
        "win_rate_wilson_95_lower": float(lower),
        "win_rate_wilson_95_upper": float(upper),
        "avg_account_return": float(values.mean()),
        "avg_return_bootstrap_95_lower": float(np.quantile(bootstrap_means, 0.025)),
        "avg_return_bootstrap_95_upper": float(np.quantile(bootstrap_means, 0.975)),
        "median_account_return": float(np.median(values)),
        "equity_multiple": float(np.prod(1.0 + values)),
        "max_drawdown": float(drawdown.min()),
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if positive.size and negative.size
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(values),
    }


def bool_value(value: Any) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed"}
    return bool(value) if not isinstance(value, np.bool_) else bool(value.item())


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON根节点不是对象：{path}")
    return payload


def scalar(value: Any) -> Any:
    return value.item() if isinstance(value, np.generic) else value


def record(frame: pd.DataFrame, *, label: str, leg: str) -> dict[str, Any]:
    rows = frame[frame["strategy_leg"].astype(str).eq(leg)]
    if len(rows) != 1:
        raise RuntimeError(f"{label}中{leg}不是唯一一行：{len(rows)}")
    return {key: scalar(value) for key, value in rows.iloc[0].to_dict().items()}


def gate(name: str, passed: Any, actual: Any, required: str, evidence: Path) -> dict[str, Any]:
    return {
        "gate": name,
        "passed": bool_value(passed),
        "actual": scalar(actual),
        "required": required,
        "evidence": str(evidence.relative_to(ROOT)),
    }


def capacity_status(capacity: pd.DataFrame, leg: str) -> dict[str, bool]:
    rows = capacity[capacity["strategy_leg"].astype(str).eq(leg)]
    if rows.empty:
        raise RuntimeError(f"容量代理中没有{leg}记录")
    return {
        "capacity_certified": all(bool_value(value) for value in rows["capacity_certified"]),
        "minute_orderbook_verified": all(bool_value(value) for value in rows["minute_orderbook_verified"]),
        "real_fill_verified": all(bool_value(value) for value in rows["real_fill_verified"]),
    }


def decision(nested: dict[str, Any], freeze_status: str) -> str:
    if not bool_value(nested["combination_compound_non_decreasing_passed"]):
        return "RETIRE_BY_OUTER_OOS_COMPOUND_RULE"
    if freeze_status == "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY":
        return "PAPER_OOS_ONLY_NOT_LIVE"
    return "NOT_LIVE_NO_FORWARD_CANDIDATE"


def build_report(payload: dict[str, Any]) -> str:
    lines = [
        "# D/A/E/C 统一严格发布验证",
        "",
        f"- 组合发布状态：`{payload['status']}`",
        "- 所有腿均按固定规则点估计、逐年嵌套外层OOS、组合边际、untouched OOS、容量和正式证书分层验证。",
        "- 回溯点估计不得覆盖外层OOS结论。",
        "",
        "## 核心交叉对比",
        "",
        "| 腿 | 回溯组合笔数 | 回溯胜率 | 回溯复利 | 外层OOS笔数 | 外层OOS胜率 | 外层OOS复利 | 外层OOS回撤 | 含腿/删腿复利 | 结论 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for leg in LEGS:
        item = payload["legs"][leg]
        fixed = item["fixed_rule_combo_discovery_metrics"]
        nested = item["nested_outer_oos_metrics"]
        marginal = item["nested_combination_marginal"]
        lines.append(
            f"| {leg} | {fixed['trade_count']} | {fixed['win_rate']:.2%} | "
            f"{fixed['equity_multiple']:.4f}x | {int(nested['trade_count'])} | "
            f"{float(nested['win_rate']):.2%} | {float(nested['equity_multiple']):.4f}x | "
            f"{float(nested['max_drawdown']):.2%} | "
            f"{float(marginal['with_leg_equity_multiple']):.4f}x / "
            f"{float(marginal['without_leg_equity_multiple']):.4f}x | {item['decision']} |"
        )
    lines.extend(["", "## 各腿失败门禁", ""])
    for leg in LEGS:
        item = payload["legs"][leg]
        lines.extend(
            [
                f"### {leg}",
                "",
                f"- 发布结论：`{item['status']}`；研究处置：`{item['decision']}`。",
                f"- 失败门禁：{', '.join(item['failed_gates'])}。",
                "",
                "| 门禁 | 结果 | 实际值 | 要求 |",
                "|---|---|---|---|",
            ]
        )
        for row in item["gates"]:
            lines.append(
                f"| {row['gate']} | {'通过' if row['passed'] else '不通过'} | "
                f"{json.dumps(row['actual'], ensure_ascii=False)} | {row['required']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 安全边界",
            "",
            "- 本报告没有修改任何策略开关或发布文件。",
            "- 当前全局BUY认证门禁继续关闭；SELL、撤单和已有持仓回写不受影响。",
            "- 容量代理只验证日级成交空间，缺少分钟盘口和对应策略真实成交，不能认证容量。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    missing = [str(path) for path in SOURCE_PATHS if not path.exists()]
    if missing:
        raise RuntimeError("严格验证缺少输入文件：" + "、".join(missing))

    strict_summary = read_json(STRICT_SUMMARY)
    publication = read_json(PUBLICATION_GATE)
    forward = read_json(FORWARD_FREEZE)
    live_certification = read_json(LIVE_CERTIFICATION)
    live_freeze = read_json(LIVE_FREEZE)
    strict_source_passed = bool(strict_summary.get("source_audit", {}).get("passed", False))

    portfolio = pd.read_csv(STRICT_PORTFOLIO, dtype={"signal_date": str}, low_memory=False)
    release_gates = pd.read_csv(STRICT_RELEASE_GATES)
    nested_legs = pd.read_csv(NESTED_LEGS)
    nested_marginal = pd.read_csv(NESTED_MARGINAL)
    capacity = pd.read_csv(NESTED_CAPACITY)
    freeze_by_leg = {
        str(row.get("strategy_leg")): row
        for row in forward.get("candidates", [])
        if isinstance(row, dict)
    }

    all_candidates = {
        str(row["strategy_leg"]): row
        for row in strict_summary.get("strict_leg_metrics", [])
        if row.get("sample_scope") == "strict_all_candidates"
    }
    result: dict[str, Any] = {}
    for leg in LEGS:
        selected = portfolio[
            portfolio["status"].astype(str).eq("EXECUTED")
            & portfolio["strategy_leg"].astype(str).eq(leg)
        ]
        fixed = return_metrics(
            pd.to_numeric(selected["account_return"], errors="raise").to_numpy(float)
        )
        if leg not in all_candidates:
            raise RuntimeError(f"严格固定规则报告缺少{leg}全候选指标")
        nested = record(nested_legs, label="嵌套外层OOS", leg=leg)
        marginal = record(nested_marginal, label="嵌套组合边际", leg=leg)
        freeze_status = str(freeze_by_leg.get(leg, {}).get("status", "MISSING"))
        capacity_check = capacity_status(capacity, leg)

        existing_rows = release_gates[
            release_gates["strategy_leg"].astype(str).eq(leg)
            & release_gates["gate"].astype(str).eq("existing_strategy_release_gate_passed")
        ]
        if len(existing_rows) != 1:
            raise RuntimeError(f"现有发布门禁中{leg}不是唯一一行")
        existing_release_passed = bool_value(existing_rows.iloc[0]["passed"])

        gates = [
            gate("strict_asof_source", strict_source_passed, strict_source_passed, "必须通过", STRICT_SUMMARY),
            gate("fixed_combo_discovery_multiple_gt_1", fixed["equity_multiple"] > 1.0, fixed["equity_multiple"], "> 1.0x", STRICT_PORTFOLIO),
            gate("fixed_combo_discovery_wilson_lower_gt_50pct", fixed["win_rate_wilson_95_lower"] > 0.50, fixed["win_rate_wilson_95_lower"], "> 0.50", STRICT_PORTFOLIO),
            gate("fixed_combo_discovery_mean_bootstrap_lower_gt_0", fixed["avg_return_bootstrap_95_lower"] > 0.0, fixed["avg_return_bootstrap_95_lower"], "> 0", STRICT_PORTFOLIO),
            gate("existing_strategy_release_gate", existing_release_passed, existing_release_passed, "必须通过", STRICT_RELEASE_GATES),
            gate("nested_outer_oos_min_30_trades", int(nested["trade_count"]) >= 30, int(nested["trade_count"]), ">= 30", NESTED_LEGS),
            gate("nested_outer_oos_positive_years_at_least_3", int(nested["positive_years"]) >= 3, int(nested["positive_years"]), ">= 3", NESTED_LEGS),
            gate("nested_outer_oos_wilson_lower_gt_50pct", float(nested["win_rate_wilson_95_lower"]) > 0.50, float(nested["win_rate_wilson_95_lower"]), "> 0.50", NESTED_LEGS),
            gate("nested_outer_oos_mean_bootstrap_lower_gt_0", float(nested["avg_return_bootstrap_95_lower"]) > 0.0, float(nested["avg_return_bootstrap_95_lower"]), "> 0", NESTED_LEGS),
            gate("nested_outer_oos_drawdown_not_worse_than_25pct", float(nested["max_drawdown"]) >= -0.25, float(nested["max_drawdown"]), ">= -0.25", NESTED_LEGS),
            gate("nested_combination_compound_non_decreasing", bool_value(nested["combination_compound_non_decreasing_passed"]), bool_value(nested["combination_compound_non_decreasing_passed"]), "含腿复利不得低于删腿", NESTED_MARGINAL),
            gate("frozen_for_future_paper_oos", freeze_status == "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY", freeze_status, "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY", FORWARD_FREEZE),
            gate("untouched_oos_after_freeze", bool(publication.get("untouched_oos_passed", False)), int(publication.get("real_forward_samples_after_freeze", 0) or 0), "冻结后真实前向样本且门禁通过", PUBLICATION_GATE),
            gate("capacity_minute_and_real_fill_certified", all(capacity_check.values()), capacity_check, "三项均为true", NESTED_CAPACITY),
            gate("formal_live_certification", live_certification.get("current_executable") is True, {"status": live_certification.get("status"), "current_executable": live_certification.get("current_executable")}, "认证通过且current_executable=true", LIVE_CERTIFICATION),
            gate("formal_release_frozen", live_freeze.get("status") == "FROZEN", live_freeze.get("status"), "FROZEN", LIVE_FREEZE),
        ]
        failed = [row["gate"] for row in gates if not row["passed"]]
        result[leg] = {
            "status": "LIVE_RELEASEABLE" if not failed else "NOT_LIVE_RELEASEABLE",
            "decision": decision(nested, freeze_status),
            "fixed_rule_combo_discovery_metrics": fixed,
            "fixed_rule_all_candidate_discovery_metrics": all_candidates[leg],
            "nested_outer_oos_metrics": nested,
            "nested_combination_marginal": marginal,
            "forward_freeze_status": freeze_status,
            "capacity": capacity_check,
            "gates": gates,
            "failed_gates": failed,
        }

    all_releaseable = all(item["status"] == "LIVE_RELEASEABLE" for item in result.values())
    payload = {
        "schema_version": 1,
        "status": "LIVE_RELEASEABLE" if all_releaseable else "NOT_LIVE_RELEASEABLE",
        "validation_scope": "A_C_E_D_action_date_strict_asof_nested_outer_oos_untouched_capacity_release",
        "legs": result,
        "source_sha256": {
            str(path.relative_to(ROOT)): file_sha256(path) for path in SOURCE_PATHS
        },
        "safety": {
            "live_files_modified": False,
            "broker_connected": False,
            "orders_generated": False,
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "validation.json"
    report_path = OUTPUT_DIR / "report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_report(payload), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "legs": {
            leg: {
                "status": result[leg]["status"],
                "decision": result[leg]["decision"],
                "failed_gate_count": len(result[leg]["failed_gates"]),
            }
            for leg in LEGS
        },
        "json": str(json_path),
        "report": str(report_path),
    }, ensure_ascii=False, indent=2))
    if not all_releaseable:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
