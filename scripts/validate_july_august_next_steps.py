#!/usr/bin/env python3
"""验证7月盈利、8月连亏后提出的 A/L/C 调整假设。

只做离线研究和审计，不修改策略配置、不连接券商、不下单。验证内容：

* A：是否应排除 turnover_rate_bucket=6_10；
* L：是否应排除 first_time_detail_bucket=after_1430；
* C：当前排序与“题材主线优先、换手率上限、资金流优先”候选排序；
* 8月实际成交：从 positions.json 的真实成交账汇总毛收益。

所有方案同时输出整体、前半段、后半段和自然年结果，避免仅凭8月4笔亏损
反向拟合规则。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import build_ac_daily_candidates as ac_builder  # noqa: E402
from scripts.certify_strategy_l_live_execution import (  # noqa: E402
    VARIANTS as L_VARIANTS,
    apply_filter as apply_l_filter,
    load_l_source,
)
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    configured_c_conditions,
    reject_strategy_risk_mask,
)
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "reports" / "july_august_review"
A_SOURCE = PROJECT_ROOT / "reports" / "a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv"
AC_DAILY_SOURCE = PROJECT_ROOT / "reports" / "ac_daily_candidates" / "ac_daily_candidates.csv"
POSITIONS_SOURCE = PROJECT_ROOT / "data" / "processed" / "positions.json"
STRATEGY_CONFIG = PROJECT_ROOT / "config" / "strategy_config.json"
RAW_CANDIDATES = PROJECT_ROOT / "data" / "processed" / "next_day_premium_trades_2y.csv"


def _numeric(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").dropna().astype(float)


def _max_consecutive_losses(values: pd.Series) -> int:
    current = result = 0
    for value in values:
        if float(value) < 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def _metrics(values: pd.Series) -> dict[str, Any]:
    returns = _numeric(values)
    if returns.empty:
        return {
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "profit_loss_ratio": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
        }
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    profit_loss_ratio = (
        float(gains.mean() / abs(losses.mean())) if not gains.empty and not losses.empty else 0.0
    )
    return {
        "sample_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "equity_multiple": float(equity.iloc[-1]),
        "max_drawdown": float(drawdown.min()),
        "profit_loss_ratio": profit_loss_ratio,
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": _max_consecutive_losses(returns),
    }


def _segments(data: pd.DataFrame, date_column: str) -> list[tuple[str, pd.DataFrame]]:
    ordered = data.sort_values(date_column).reset_index(drop=True)
    split = len(ordered) // 2
    result = [
        ("overall", ordered),
        ("front_half", ordered.iloc[:split]),
        ("back_half", ordered.iloc[split:]),
    ]
    years = ordered[date_column].astype(str).str[:4]
    for year in sorted(value for value in years.dropna().unique() if value):
        result.append((f"year_{year}", ordered[years.eq(year)]))
    return result


def _metric_rows(
    *, strategy: str, variant: str, data: pd.DataFrame, date_column: str, return_column: str,
    description: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for segment, group in _segments(data, date_column):
        rows.append(
            {
                "strategy": strategy,
                "variant": variant,
                "segment": segment,
                "description": description,
                "start_date": str(group[date_column].min()) if len(group) else "",
                "end_date": str(group[date_column].max()) if len(group) else "",
                **_metrics(group[return_column] if return_column in group else pd.Series(dtype=float)),
            }
        )
    return rows


def validate_a() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = pd.read_csv(A_SOURCE, dtype={"trade_date": str}, low_memory=False)
    data = data.sort_values("trade_date").reset_index(drop=True)
    data["analysis_return"] = pd.to_numeric(data["dynamic_account_return"], errors="coerce")
    filtered = data[data["turnover_rate_bucket"].fillna("").astype(str).ne("6_10")].copy()
    rows = _metric_rows(
        strategy="A",
        variant="current",
        data=data,
        date_column="trade_date",
        return_column="analysis_return",
        description="当前A审计成交，动态仓位账户收益口径",
    )
    rows += _metric_rows(
        strategy="A",
        variant="exclude_turnover_6_10",
        data=filtered,
        date_column="trade_date",
        return_column="analysis_return",
        description="拟议排除换手率分桶6_10",
    )
    removed = data[data["turnover_rate_bucket"].fillna("").astype(str).eq("6_10")]
    decisions = [
        {
            "strategy": "A",
            "hypothesis": "排除turnover_rate_bucket=6_10",
            "decision": "REJECT",
            "reason": f"仅移除{len(removed)}笔，样本过少；后半段/年度稳定性必须同时改善才允许上线。",
        }
    ]
    return rows, decisions


def validate_l() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = load_l_source()
    l2 = next(variant for variant in L_VARIANTS if variant.name == "L2")
    current = apply_l_filter(source, l2.filter_expr).drop_duplicates("trade_date", keep="first")
    late_mask = current["first_time_detail_bucket"].fillna("").astype(str).eq("after_1430")
    filtered = current[~late_mask].copy()
    rows = _metric_rows(
        strategy="L",
        variant="current_L2",
        data=current,
        date_column="trade_date",
        return_column="l_account_return",
        description="当前L2理论账户收益口径",
    )
    rows += _metric_rows(
        strategy="L",
        variant="exclude_after_1430",
        data=filtered,
        date_column="trade_date",
        return_column="l_account_return",
        description="拟议排除14:30以后首次涨停",
    )
    late_stats = _metrics(current.loc[late_mask, "l_account_return"])
    decisions = [
        {
            "strategy": "L",
            "hypothesis": "排除first_time_detail_bucket=after_1430",
            "decision": "REJECT",
            "reason": (
                f"晚板组{late_stats['sample_count']}笔，胜率{late_stats['win_rate']:.2%}、"
                f"平均收益{late_stats['avg_return']:.2%}，不是历史亏损源；排除后需警惕反向过拟合。"
            ),
        }
    ]
    return rows, decisions


def _bool_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data:
        return pd.Series(False, index=data.index)
    return data[column].astype(str).str.lower().isin({"1", "1.0", "true", "yes"})


def _number_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data:
        return pd.Series(0.0, index=data.index)
    return pd.to_numeric(data[column], errors="coerce").fillna(0.0)


def _rank_current(generator: PaperCandidateGenerator, data: pd.DataFrame) -> pd.DataFrame:
    return generator.rank_candidates(data.copy())


def _rank_theme_first(generator: PaperCandidateGenerator, data: pd.DataFrame) -> pd.DataFrame:
    ranked = generator.rank_candidates(data.copy())
    ranked["_theme_mainline"] = _bool_series(ranked, "theme_is_mainline").astype(int)
    ranked["_theme_limit_count"] = _number_series(ranked, "theme_limit_count")
    ranked["_theme_heat_score"] = _number_series(ranked, "theme_heat_score")
    ranked = ranked.sort_values(
        ["_theme_mainline", "_theme_limit_count", "_theme_heat_score", "profit_source_score", "turnover_rate", "amount"],
        ascending=[False, False, False, False, False, False],
    )
    return ranked.reset_index(drop=True)


def _rank_turnover_cap25(generator: PaperCandidateGenerator, data: pd.DataFrame) -> pd.DataFrame:
    kept = data[_number_series(data, "turnover_rate").le(25.0)].copy()
    return generator.rank_candidates(kept) if len(kept) else kept


def _rank_moneyflow_first(generator: PaperCandidateGenerator, data: pd.DataFrame) -> pd.DataFrame:
    ranked = generator.rank_candidates(data.copy())
    ranked["_moneyflow"] = _number_series(ranked, "sector_moneyflow_score")
    return ranked.sort_values(
        ["_moneyflow", "profit_source_score", "turnover_rate", "amount"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def _build_c_variant(
    generator: PaperCandidateGenerator,
    config: dict[str, Any],
    groups: dict[str, pd.DataFrame],
    a_dates: set[str],
    ranker: Callable[[PaperCandidateGenerator, pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for signal_date in sorted(groups):
        if signal_date in a_dates:
            continue
        ranked = ranker(generator, groups[signal_date].copy())
        if ranked.empty:
            continue
        risk_mask = reject_strategy_risk_mask(ranked, config, "c_strategy")
        ranked = ranked[~pd.Series(risk_mask.values, index=ranked.index)]
        if ranked.empty:
            continue
        pick = ranked.iloc[0]
        status, buy_date, exit_date, stock_return = ac_builder.trade_return(
            signal_date, str(pick["ts_code"]), 3
        )
        rows.append(
            {
                "signal_date": signal_date,
                "ts_code": str(pick["ts_code"]),
                "name": str(pick.get("name", "")),
                "status": status,
                "buy_date": buy_date,
                "exit_date": exit_date,
                "stock_return": stock_return,
            }
        )
    return pd.DataFrame(rows)


def validate_c() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    config = load_json_config(STRATEGY_CONFIG)

    def make_generator(conditions: list[dict[str, Any]] | None, label: str) -> PaperCandidateGenerator:
        strategy = condition_strategy_config(config, conditions, label) if conditions else config
        generator = PaperCandidateGenerator(STRATEGY_CONFIG, input_trades_path=RAW_CANDIDATES)
        generator.config = strategy
        generator.paper_config = strategy.get("paper_candidate", {})
        generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
        return generator

    ga = make_generator(None, "A")
    gc = make_generator(configured_c_conditions(config), "C")
    all_candidates = ga.load_all_candidates()
    ac_daily = pd.read_csv(AC_DAILY_SOURCE, dtype={"signal_date": str})
    window_start = str(ac_daily["signal_date"].min())
    window_end = str(ac_daily["signal_date"].max())
    a_pool = ga.apply_strategy_filters(all_candidates)
    c_pool = gc.apply_strategy_filters(all_candidates)
    a_pool = a_pool[a_pool["trade_date"].astype(str).between(window_start, window_end)].copy()
    c_pool = c_pool[c_pool["trade_date"].astype(str).between(window_start, window_end)].copy()
    a_dates = set(a_pool["trade_date"].astype(str))
    c_groups = {str(day): group.copy() for day, group in c_pool.groupby("trade_date")}

    moneyflow = _number_series(c_pool, "sector_moneyflow_score")
    moneyflow_coverage = float(moneyflow.ne(0).mean()) if len(moneyflow) else 0.0
    theme_coverage = float(_bool_series(c_pool, "theme_data_available").mean()) if len(c_pool) else 0.0

    rankers: dict[str, Callable[[PaperCandidateGenerator, pd.DataFrame], pd.DataFrame]] = {
        "current": _rank_current,
        "theme_mainline_first": _rank_theme_first,
        "turnover_cap_25": _rank_turnover_cap25,
    }
    if moneyflow_coverage >= 0.5:
        rankers["moneyflow_first"] = _rank_moneyflow_first

    rows: list[dict[str, Any]] = []
    variants: dict[str, pd.DataFrame] = {}
    for name, ranker in rankers.items():
        detail = _build_c_variant(gc, config, c_groups, a_dates, ranker)
        detail = detail[detail["status"].eq("OK")].copy() if not detail.empty else detail
        variants[name] = detail
        rows += _metric_rows(
            strategy="C",
            variant=name,
            data=detail,
            date_column="signal_date",
            return_column="stock_return",
            description="C在A无候选日、T+1开盘买/T+3收盘卖的个股净收益口径",
        )

    current_expected = ac_daily[(ac_daily["leg"].eq("C")) & (ac_daily["status"].eq("OK"))]
    current_got = variants.get("current", pd.DataFrame())
    expected_map = current_expected.set_index("signal_date")["ts_code"].astype(str).to_dict()
    got_map = current_got.set_index("signal_date")["ts_code"].astype(str).to_dict() if len(current_got) else {}
    current_match = sum(got_map.get(day) == code for day, code in expected_map.items())

    decisions = [
        {
            "strategy": "C",
            "hypothesis": "资金流优先排序",
            "decision": "DATA_BLOCKED" if moneyflow_coverage < 0.5 else "REVIEW",
            "reason": f"历史资金流有效覆盖率{moneyflow_coverage:.2%}；低于50%时禁止据此改排序。",
        },
        {
            "strategy": "C",
            "hypothesis": "题材主线优先/换手率<=25%",
            "decision": "REVIEW_AFTER_SPLIT",
            "reason": "只有整体和后半段、2025、2026同时不恶化才可保留；8月单笔结果不能作为上线依据。",
        },
    ]
    diagnostics = {
        "window_start": window_start,
        "window_end": window_end,
        "raw_c_rows": int(len(c_pool)),
        "raw_c_dates": int(c_pool["trade_date"].nunique()),
        "theme_data_coverage": theme_coverage,
        "moneyflow_nonzero_coverage": moneyflow_coverage,
        "current_rebuild_match": int(current_match),
        "current_rebuild_expected": int(len(expected_map)),
    }
    return rows, decisions, diagnostics


def august_actual_trades() -> pd.DataFrame:
    positions = json.loads(POSITIONS_SOURCE.read_text(encoding="utf-8"))
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for position in positions:
        sell_date = str(position.get("sell_date", "")).replace("-", "")[:8]
        if not sell_date.startswith("202608"):
            continue
        key = (
            str(position.get("buy_date", "")),
            str(position.get("ts_code", "")),
            str(position.get("strategy_leg", "")),
            sell_date,
        )
        groups.setdefault(key, []).append(position)
    rows: list[dict[str, Any]] = []
    for (buy_date, ts_code, strategy, sell_date), group in sorted(groups.items()):
        entry_qty = sum(int(row.get("entry_shares", row.get("shares", 0)) or 0) for row in group)
        entry_amount = sum(
            int(row.get("entry_shares", row.get("shares", 0)) or 0)
            * float(row.get("buy_price", 0) or 0)
            for row in group
        )
        exit_qty = 0
        exit_amount = 0.0
        for row in group:
            ledger = row.get("exit_fills_by_date", {})
            if not isinstance(ledger, dict):
                continue
            for value in ledger.values():
                if isinstance(value, dict):
                    exit_qty += int(value.get("qty", 0) or 0)
                    exit_amount += float(value.get("amount", 0) or 0)
        rows.append(
            {
                "buy_date": buy_date,
                "sell_date": sell_date,
                "strategy": strategy,
                "ts_code": ts_code,
                "name": next((str(row.get("name", "")) for row in group if row.get("name")), ""),
                "order_count": len(group),
                "entry_qty": entry_qty,
                "exit_qty": exit_qty,
                "buy_vwap": entry_amount / entry_qty if entry_qty else 0.0,
                "sell_vwap": exit_amount / exit_qty if exit_qty else 0.0,
                "gross_return": exit_amount / entry_amount - 1.0 if entry_amount > 0 else np.nan,
                "gross_pnl": exit_amount - entry_amount,
                "data_complete": bool(entry_qty > 0 and exit_qty == entry_qty and exit_amount > 0),
            }
        )
    return pd.DataFrame(rows)


def _markdown_table(data: pd.DataFrame, columns: list[str]) -> str:
    if data.empty:
        return "无数据。"
    view = data[columns].copy()
    for column in columns:
        if column in {"win_rate", "avg_return", "median_return", "max_drawdown", "max_profit", "max_loss", "gross_return"}:
            view[column] = pd.to_numeric(view[column], errors="coerce").map(
                lambda value: f"{value:.2%}" if pd.notna(value) else ""
            )
        elif column in {"equity_multiple", "profit_loss_ratio", "buy_vwap", "sell_vwap"}:
            view[column] = pd.to_numeric(view[column], errors="coerce").map(
                lambda value: f"{value:.4f}" if pd.notna(value) else ""
            )
        elif column == "gross_pnl":
            view[column] = pd.to_numeric(view[column], errors="coerce").map(
                lambda value: f"{value:,.2f}" if pd.notna(value) else ""
            )
        else:
            view[column] = view[column].fillna("").astype(str)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist())
    return "\n".join(lines)


def _decision_from_metrics(metrics: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    result = decisions.copy()
    # 将C的两项候选调整在统一样本外规则下自动收敛为接受/拒绝。
    c = metrics[metrics["strategy"].eq("C")]
    current = c[c["variant"].eq("current")].set_index("segment")
    for variant in ["theme_mainline_first", "turnover_cap_25"]:
        proposed = c[c["variant"].eq(variant)].set_index("segment")
        required = ["overall", "back_half", "year_2025", "year_2026"]
        stable = all(
            segment in proposed.index
            and segment in current.index
            and float(proposed.loc[segment, "equity_multiple"])
            >= float(current.loc[segment, "equity_multiple"])
            for segment in required
        )
        result = pd.concat(
            [
                result,
                pd.DataFrame(
                    [
                        {
                            "strategy": "C",
                            "hypothesis": variant,
                            "decision": "ACCEPT" if stable else "REJECT",
                            "reason": "通过整体、后半段、2025和2026一致性门槛。"
                            if stable
                            else "未同时改善整体、后半段、2025和2026，拒绝上线。",
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    return result


def main() -> int:
    if not AC_DAILY_SOURCE.exists():
        raise FileNotFoundError("缺少A/C每日候选，请先运行 scripts/build_ac_daily_candidates.py")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    metric_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    a_rows, a_decisions = validate_a()
    l_rows, l_decisions = validate_l()
    c_rows, c_decisions, c_diagnostics = validate_c()
    metric_rows.extend(a_rows + l_rows + c_rows)
    decision_rows.extend(a_decisions + l_decisions + c_decisions)

    metrics = pd.DataFrame(metric_rows)
    decisions = _decision_from_metrics(metrics, pd.DataFrame(decision_rows))
    actual = august_actual_trades()
    metrics.to_csv(OUTPUT_DIR / "hypothesis_metrics.csv", index=False, encoding="utf-8-sig")
    decisions.to_csv(OUTPUT_DIR / "hypothesis_decisions.csv", index=False, encoding="utf-8-sig")
    actual.to_csv(OUTPUT_DIR / "august_actual_trades.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "data_diagnostics.json").write_text(
        json.dumps(c_diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    overall = metrics[metrics["segment"].isin(["overall", "back_half", "year_2025", "year_2026"])]
    report = [
        "# 7月盈利、8月连亏：下一步验证报告",
        "",
        "## 8月实际成交账",
        "",
        _markdown_table(
            actual,
            ["strategy", "ts_code", "name", "order_count", "entry_qty", "exit_qty", "buy_vwap", "sell_vwap", "gross_return", "gross_pnl", "data_complete"],
        ),
        "",
        "> 毛收益未扣佣金、印花税和过户费；净亏损会略大。4条委托记录合并为3笔策略交易。",
        "",
        "## 原因归类",
        "",
        "- **主要原因：策略收益分布与近期行情。** 三笔真实成交分别来自A、L、C，不是同一条代码路径连续选错；8月恰好集中落入各策略的亏损尾部。",
        "- **C近期退化最明显。** 当前C在2026年8个历史样本中胜率50%、平均收益-0.07%、复利0.9746，华之杰亏损与这段样本外走弱方向一致。",
        "- **存在代码问题，但不是经济亏损原因。** 14:58幽灵持仓同步先写closed，15:00:30成交回报只查询open/sell_pending，导致卖出价记为0；券商委托本身已真实成交，修复的是审计账和后续复盘。",
        "- **样本口径容易误读。** 用户看到的4条持仓委托中，华之杰是同一策略交易的两片成交，因此统计上是3笔独立策略交易，不足以据此新增硬过滤。",
        "",
        "## 假设验证（整体、后半段及近年）",
        "",
        _markdown_table(
            overall,
            ["strategy", "variant", "segment", "sample_count", "win_rate", "avg_return", "median_return", "equity_multiple", "max_drawdown", "profit_loss_ratio", "max_profit", "max_loss", "max_consecutive_losses"],
        ),
        "",
        "## 决策",
        "",
        _markdown_table(decisions, ["strategy", "hypothesis", "decision", "reason"]),
        "",
        "## 数据质量",
        "",
        f"- C候选窗口：{c_diagnostics['window_start']}–{c_diagnostics['window_end']}。",
        f"- C原始过滤池：{c_diagnostics['raw_c_rows']}行/{c_diagnostics['raw_c_dates']}日。",
        f"- 题材数据覆盖率：{c_diagnostics['theme_data_coverage']:.2%}。",
        f"- 资金流非零覆盖率：{c_diagnostics['moneyflow_nonzero_coverage']:.2%}。",
        f"- 当前C重建一致：{c_diagnostics['current_rebuild_match']}/{c_diagnostics['current_rebuild_expected']}。",
        "",
        "## 结论",
        "",
        "8月连亏是真实发生的，但样本只有3笔策略交易，不能据此新增硬过滤。A换手率过滤和L晚板过滤均未通过稳定性门槛；C的题材/换手率替代排序也必须在后半段和2025/2026同时改善才可上线。资金流历史数据覆盖不足，当前不能用于排序认证。",
        "",
        "本报告仅用于回测和模拟盘决策，不构成收益承诺。任何实盘调整都应先经过样本外与模拟盘验证，并使用小资金。",
    ]
    (OUTPUT_DIR / "validation_report.md").write_text("\n".join(report), encoding="utf-8")

    print(f"指标:{OUTPUT_DIR / 'hypothesis_metrics.csv'}")
    print(f"决策:{OUTPUT_DIR / 'hypothesis_decisions.csv'}")
    print(f"报告:{OUTPUT_DIR / 'validation_report.md'}")
    print(decisions[["strategy", "hypothesis", "decision"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
