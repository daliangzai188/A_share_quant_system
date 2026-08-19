"""在当前正式N之上研究一个可选的补充分支，绝不修改实盘配置。

研究口径：
1. 固定当前组合 ``D>A>M>E>C>N`` 和当前N第一分支；
2. 只有当前N当天没有候选时，才允许研究规则提供N补充候选；
3. 所有候选只使用信号日字段，T+1开盘买、T+2收盘卖；
4. 训练段生成有限的一/二条件规则，验证段锁定唯一挑战者，测试段最后揭盲；
5. 研究时挑战者必须优于冻结的N单分支7108.62倍，且真正增加N样本或提高N胜率；
6. 本脚本只写 ``reports/strategy_n_v2_research``，不会生成实盘信号。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.certify_current_executable_portfolio as cert  # noqa: E402
import scripts.research_strategy_n as legacy  # noqa: E402


OUTPUT_DIR = ROOT / "reports" / "strategy_n_v2_research"
MAX_TOP_ATOMIC_CONDITIONS = 22
MAX_VALIDATION_FINALISTS = 240
MIN_TRAIN_EXTRA_TRADES = 3
MIN_VALIDATION_EXTRA_TRADES = 1
MAX_SPLIT_DRAWDOWN_WORSENING = 0.03
EPSILON = 1e-12

CONDITION_FIELDS = tuple(legacy.CONDITION_FIELDS)
FORBIDDEN_EXACT = set(legacy.FORBIDDEN_EXACT)
FORBIDDEN_TOKENS = tuple(legacy.FORBIDDEN_TOKENS)

RANKERS: dict[str, tuple[list[str], list[bool]]] = {
    **legacy.RANKERS,
    "circ_mv_desc": (["circ_mv", "ts_code"], [False, True]),
    "first_time_desc": (["first_time_minutes", "circ_mv", "ts_code"], [False, True, True]),
    "fill_probability_desc": (["fill_probability", "circ_mv", "ts_code"], [False, True, True]),
    "open_times_desc": (["open_times", "circ_mv", "ts_code"], [False, True, True]),
    "volume_ratio_desc": (["volume_ratio", "circ_mv", "ts_code"], [False, True, True]),
}


@dataclass(frozen=True)
class Rule:
    conditions: tuple[tuple[str, str], ...]
    ranker: str
    origin: str

    @property
    def rule_id(self) -> str:
        conditions = ";".join(f"{field}={value}" for field, value in self.conditions)
        return f"{conditions}|{self.ranker}"


def assert_signal_only(rule: Rule, pool: pd.DataFrame) -> None:
    fields = {field for field, _ in rule.conditions}
    fields.update(RANKERS[rule.ranker][0])
    missing = sorted(fields.difference(pool.columns))
    if missing:
        raise RuntimeError(f"N补充规则缺少字段：{missing}")
    forbidden = sorted(
        field
        for field in fields
        if field in FORBIDDEN_EXACT
        or any(token in field.lower() for token in FORBIDDEN_TOKENS)
    )
    if forbidden:
        raise RuntimeError(f"N补充规则使用未来字段：{forbidden}")


def current_n_daily(sources: cert.Sources) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for signal_date in sources.n_pool.index.astype(str):
        selected = cert.n_candidate(sources, signal_date)
        if selected is None:
            continue
        selected = dict(selected)
        selected["rule_id"] = "CURRENT_N"
        selected["n_branch"] = "CURRENT"
        result[signal_date] = selected
    return result


def select_daily(rule: Rule, pool: pd.DataFrame) -> dict[str, dict[str, Any]]:
    assert_signal_only(rule, pool)
    selected = pool
    for field, value in rule.conditions:
        selected = selected[selected[field].astype(str).eq(value)]
    if selected.empty:
        return {}
    columns, ascending = RANKERS[rule.ranker]
    selected = (
        selected.sort_values(
            ["trade_date", *columns],
            ascending=[True, *ascending],
            na_position="last",
        )
        .groupby("trade_date", as_index=False)
        .head(1)
    )
    result: dict[str, dict[str, Any]] = {}
    for row in selected.itertuples(index=False):
        signal_date = str(row.trade_date)
        item = legacy.account_outcome(signal_date, str(row.ts_code), str(row.name))
        item["rule_id"] = rule.rule_id
        item["n_branch"] = "SUPPLEMENT"
        result[signal_date] = item
    return result


def combine_n_daily(
    current: dict[str, dict[str, Any]],
    supplement: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """当前N优先；补充分支只填当前N没有候选的日期。"""

    result = {date: dict(item) for date, item in supplement.items()}
    result.update({date: dict(item) for date, item in current.items()})
    return result


def replay_with_n_map(
    sources: cert.Sources,
    n_daily: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """复刻正式单账户时序，但把N候选源替换为研究映射。"""

    equity = cert.INITIAL_EQUITY
    peak_equity = cert.INITIAL_EQUITY
    occupied_until = ""
    occupied_leg = ""
    occupied_code = ""
    rows: list[dict[str, Any]] = []

    for row_index, row in sources.baseline.iterrows():
        signal_date = str(row["date"])
        equity_before = equity
        if occupied_until and signal_date < occupied_until:
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "SKIP_OCCUPIED",
                    "strategy_leg": "",
                    "ts_code": "",
                    "name": "",
                    "buy_date": "",
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity,
                    "equity_after": equity,
                    "blocked_by_leg": occupied_leg,
                    "blocked_by_code": occupied_code,
                    "blocked_until": occupied_until,
                    "return_source": "",
                    "n_rule_id": "",
                    "n_branch": "",
                }
            )
            continue

        blocking_handoff = (
            bool(occupied_until)
            and signal_date == occupied_until
            and not legacy.cached_hit_limit_up(signal_date, occupied_code)
        )
        occupied_until = occupied_leg = occupied_code = ""

        if signal_date in sources.strategy_d.index and not blocking_handoff:
            selected = cert.d_t2_candidate(sources, signal_date)
        else:
            selected = cert.pick_by_priority(
                sources,
                row,
                row_index,
                entry_gate_enabled=True,
                m_enabled=True,
                n_enabled=False,
                equity=equity,
                peak_equity=peak_equity,
            )
            if selected is None:
                selected = n_daily.get(signal_date)

        if selected is None:
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "NO_CANDIDATE",
                    "strategy_leg": "",
                    "ts_code": "",
                    "name": "",
                    "buy_date": "",
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity,
                    "equity_after": equity,
                    "blocked_by_leg": "",
                    "blocked_by_code": "",
                    "blocked_until": "",
                    "return_source": "",
                    "n_rule_id": "",
                    "n_branch": "",
                }
            )
            continue

        selected_leg = str(selected.get("strategy_leg", ""))
        if selected_leg == "N" and selected.get("execution_status", "OK") != "OK":
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "N_NOT_FILLED",
                    "strategy_leg": "N",
                    "ts_code": str(selected.get("ts_code", "")),
                    "name": str(selected.get("name", "")),
                    "buy_date": str(selected.get("buy_date", "")),
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity,
                    "equity_after": equity,
                    "blocked_by_leg": "",
                    "blocked_by_code": "",
                    "blocked_until": "",
                    "return_source": str(selected.get("return_source", "")),
                    "n_rule_id": str(selected.get("rule_id", "")),
                    "n_branch": str(selected.get("n_branch", "")),
                }
            )
            continue

        exit_date = cert.normalize_date(selected.get("exit_date"))
        account_return = cert.to_float(selected.get("account_return"), float("nan"))
        if not exit_date or math.isnan(account_return):
            raise ValueError(f"{signal_date} {selected_leg} 缺少可执行退出日或收益")
        if account_return <= -1.0:
            raise ValueError(f"{signal_date} {selected_leg} 账户收益不允许小于等于-100%")

        equity *= 1.0 + account_return
        peak_equity = max(peak_equity, equity)
        occupied_until = exit_date
        occupied_leg = selected_leg
        occupied_code = str(selected.get("ts_code", ""))
        rows.append(
            {
                "signal_date": signal_date,
                "status": "EXECUTED",
                "strategy_leg": selected_leg,
                "ts_code": occupied_code,
                "name": str(selected.get("name", "")),
                "buy_date": cert.normalize_date(selected.get("buy_date")),
                "exit_date": exit_date,
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
                "blocked_by_leg": "",
                "blocked_by_code": "",
                "blocked_until": "",
                "return_source": str(selected.get("return_source", "")),
                "n_rule_id": str(selected.get("rule_id", "")),
                "n_branch": str(selected.get("n_branch", "")),
            }
        )

    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    return detail


def period_metrics(detail: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    result = legacy.metrics(detail, start, end)
    trades = detail[
        detail["status"].eq("EXECUTED") & detail["signal_date"].between(start, end)
    ].copy()
    n_trades = trades[trades["strategy_leg"].eq("N")]
    branch = n_trades.get(
        "n_branch",
        pd.Series("CURRENT", index=n_trades.index, dtype="object"),
    ).astype(str)
    extra = n_trades[branch.eq("SUPPLEMENT")]
    result.update(
        {
            "extra_trade_count": int(len(extra)),
            "extra_multiple": float((1.0 + extra["account_return"]).prod()) if len(extra) else 1.0,
            "extra_win_rate": float((extra["account_return"] > 0).mean()) if len(extra) else 0.0,
            "n_total_win_rate": float((n_trades["account_return"] > 0).mean()) if len(n_trades) else 0.0,
        }
    )
    return result


def atomic_conditions(train_pool: pd.DataFrame) -> list[tuple[str, str]]:
    train_days = train_pool["trade_date"].nunique()
    result: list[tuple[str, str]] = []
    for field in CONDITION_FIELDS:
        if field not in train_pool.columns:
            continue
        counts = train_pool.groupby(train_pool[field].astype(str))["trade_date"].nunique()
        for value, count in counts.items():
            if 12 <= int(count) <= int(train_days * 0.95):
                result.append((field, str(value)))
    return sorted(set(result))


def evaluate_rule(
    rule: Rule,
    *,
    pool: pd.DataFrame,
    sources: cert.Sources,
    current_daily: dict[str, dict[str, Any]],
    baseline: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    supplement = select_daily(rule, pool)
    detail = replay_with_n_map(sources, combine_n_daily(current_daily, supplement))
    base = period_metrics(baseline, start, end)
    selected = period_metrics(detail, start, end)
    row = {
        "rule_id": rule.rule_id,
        "origin": rule.origin,
        "conditions": json.dumps(rule.conditions, ensure_ascii=False),
        "ranker": rule.ranker,
        "candidate_day_count": int(len(supplement)),
        "candidate_unfilled_count": int(
            sum(item.get("execution_status") != "OK" for item in supplement.values())
        ),
        "trade_count": selected["trade_count"],
        "n_trade_count": selected["n_trade_count"],
        "extra_trade_count": selected["extra_trade_count"],
        "extra_multiple": selected["extra_multiple"],
        "extra_win_rate": selected["extra_win_rate"],
        "n_total_win_rate": selected["n_total_win_rate"],
        "portfolio_multiple": selected["equity_multiple"],
        "portfolio_ratio_to_base": selected["equity_multiple"] / base["equity_multiple"],
        "max_drawdown": selected["max_drawdown"],
        "base_max_drawdown": base["max_drawdown"],
        "ulcer_index": selected["ulcer_index"],
        "base_ulcer_index": base["ulcer_index"],
    }
    return row, detail


def rule_score(row: dict[str, Any]) -> float:
    return (
        math.log(max(float(row["portfolio_ratio_to_base"]), 1e-12))
        + 0.006 * float(row["extra_trade_count"])
        + 0.08 * (float(row["n_total_win_rate"]) - 0.50)
        - 0.50 * max(0.0, float(row["ulcer_index"]) - float(row["base_ulcer_index"]))
    )


def selection_gate(row: dict[str, Any], minimum_extra: int) -> bool:
    return bool(
        int(row["extra_trade_count"]) >= minimum_extra
        and float(row["extra_multiple"]) > 1.0
        and float(row["portfolio_ratio_to_base"]) > 1.0
        and float(row["max_drawdown"])
        >= float(row["base_max_drawdown"]) - MAX_SPLIT_DRAWDOWN_WORSENING
    )


def returns_summary(detail: pd.DataFrame) -> dict[str, Any]:
    trades = detail[detail["status"].eq("EXECUTED")].copy()
    n = trades[trades["strategy_leg"].eq("N")].copy()
    extra = n[n["n_branch"].eq("SUPPLEMENT")].copy()

    def summarize(frame: pd.DataFrame) -> dict[str, Any]:
        values = pd.to_numeric(frame["account_return"], errors="raise")
        curve = (1.0 + values).cumprod()
        drawdown = curve / curve.cummax() - 1.0 if len(curve) else pd.Series([0.0])
        wins = values[values > 0]
        losses = values[values < 0]
        return {
            "trade_count": int(len(values)),
            "win_rate": float((values > 0).mean()) if len(values) else 0.0,
            "avg_return": float(values.mean()) if len(values) else 0.0,
            "median_return": float(values.median()) if len(values) else 0.0,
            "multiple": float(curve.iloc[-1]) if len(curve) else 1.0,
            "max_drawdown": float(drawdown.min()),
            "max_profit": float(values.max()) if len(values) else 0.0,
            "max_loss": float(values.min()) if len(values) else 0.0,
            "payoff_ratio": (
                float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else 0.0
            ),
            "max_consecutive_losses": legacy.max_consecutive_losses(values),
        }

    return {"n_total": summarize(n), "supplement": summarize(extra)}


def bootstrap_mean(returns: pd.Series, *, samples: int = 20_000) -> dict[str, float]:
    values = pd.to_numeric(returns, errors="raise").to_numpy(float)
    if not len(values):
        return {"mean_p025": 0.0, "mean_p50": 0.0, "mean_p975": 0.0, "prob_mean_positive": 0.0}
    rng = np.random.default_rng(20260819 + len(values))
    sampled = rng.choice(values, size=(samples, len(values)), replace=True)
    means = sampled.mean(axis=1)
    return {
        "mean_p025": float(np.quantile(means, 0.025)),
        "mean_p50": float(np.quantile(means, 0.50)),
        "mean_p975": float(np.quantile(means, 0.975)),
        "prob_mean_positive": float((means > 0).mean()),
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = cert.load_sources()
    baseline = cert.replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
        n_enabled=True,
        block_d_on_handoff=True,
    )
    baseline_full = period_metrics(baseline, legacy.START_DATE, legacy.END_DATE)
    if baseline_full["trade_count"] != cert.EXPECTED_CURRENT_TRADE_COUNT:
        raise RuntimeError("当前N组合样本数漂移，禁止继续研究")
    if abs(baseline_full["equity_multiple"] - cert.EXPECTED_CURRENT_MULTIPLE) > 1e-9:
        raise RuntimeError("当前N组合复利漂移，禁止继续研究")

    pool = legacy.load_signal_pool()
    current_daily = current_n_daily(sources)
    reproduced = replay_with_n_map(sources, current_daily)
    reproduced_full = period_metrics(reproduced, legacy.START_DATE, legacy.END_DATE)
    if abs(reproduced_full["equity_multiple"] - baseline_full["equity_multiple"]) > 1e-9:
        raise RuntimeError("研究回放未能逐笔复现当前N组合，禁止搜索")

    dates = sources.baseline["date"].astype(str).tolist()
    train_count = int(len(dates) * 0.60)
    validation_count = int(len(dates) * 0.20)
    train_start, train_end = dates[0], dates[train_count - 1]
    validation_start = dates[train_count]
    validation_end = dates[train_count + validation_count - 1]
    test_start, test_end = dates[train_count + validation_count], dates[-1]
    train_pool = pool[pool["trade_date"].between(train_start, train_end)].copy()

    print(
        f"当前基线：{baseline_full['trade_count']}笔/{baseline_full['n_trade_count']}笔N/"
        f"{baseline_full['equity_multiple']:.6f}倍；候选池{len(pool)}行"
    )
    print(
        f"训练={train_start}~{train_end}；验证={validation_start}~{validation_end}；"
        f"测试={test_start}~{test_end}"
    )

    atoms = atomic_conditions(train_pool)
    atomic_rules = [Rule((atom,), ranker, "ATOMIC_TRAIN") for atom in atoms for ranker in RANKERS]
    atomic_rows: list[dict[str, Any]] = []
    atomic_lookup: dict[str, Rule] = {}
    for index, rule in enumerate(atomic_rules, start=1):
        row, _ = evaluate_rule(
            rule,
            pool=pool,
            sources=sources,
            current_daily=current_daily,
            baseline=baseline,
            start=train_start,
            end=train_end,
        )
        row["train_gate"] = selection_gate(row, MIN_TRAIN_EXTRA_TRADES)
        row["selection_score"] = rule_score(row)
        atomic_rows.append(row)
        atomic_lookup[rule.rule_id] = rule
        if index % 100 == 0 or index == len(atomic_rules):
            print(f"N原子规则训练回放：{index}/{len(atomic_rules)}")

    atomic_report = pd.DataFrame(atomic_rows).sort_values(
        ["train_gate", "selection_score", "portfolio_ratio_to_base"],
        ascending=[False, False, False],
    )
    atomic_report.to_csv(OUTPUT_DIR / "atomic_train_search.csv", index=False, encoding="utf-8-sig")
    eligible_atomic = atomic_report[atomic_report["train_gate"]].copy()

    top_conditions: list[tuple[str, str]] = []
    for rule_id in eligible_atomic["rule_id"]:
        condition = atomic_lookup[str(rule_id)].conditions[0]
        if condition not in top_conditions:
            top_conditions.append(condition)
        if len(top_conditions) >= MAX_TOP_ATOMIC_CONDITIONS:
            break

    pair_rules: list[Rule] = []
    for left_index, left in enumerate(top_conditions):
        for right in top_conditions[left_index + 1 :]:
            if left[0] == right[0]:
                continue
            conditions = tuple(sorted((left, right)))
            pair_rules.extend(Rule(conditions, ranker, "PAIR_FROM_TRAIN") for ranker in RANKERS)

    candidate_lookup: dict[str, Rule] = {
        str(rule_id): atomic_lookup[str(rule_id)]
        for rule_id in eligible_atomic.head(MAX_VALIDATION_FINALISTS)["rule_id"]
    }
    train_rows = atomic_report[atomic_report["rule_id"].isin(candidate_lookup)].to_dict("records")
    for index, rule in enumerate(pair_rules, start=1):
        row, _ = evaluate_rule(
            rule,
            pool=pool,
            sources=sources,
            current_daily=current_daily,
            baseline=baseline,
            start=train_start,
            end=train_end,
        )
        row["train_gate"] = selection_gate(row, MIN_TRAIN_EXTRA_TRADES)
        row["selection_score"] = rule_score(row)
        if row["train_gate"]:
            candidate_lookup[rule.rule_id] = rule
            train_rows.append(row)
        if index % 200 == 0 or index == len(pair_rules):
            print(f"N双条件规则训练回放：{index}/{len(pair_rules)}")

    train_report = pd.DataFrame(train_rows).drop_duplicates("rule_id", keep="first")
    train_report = train_report.sort_values(
        ["train_gate", "selection_score", "portfolio_ratio_to_base"],
        ascending=[False, False, False],
    ).head(MAX_VALIDATION_FINALISTS)
    train_report.to_csv(OUTPUT_DIR / "train_finalists.csv", index=False, encoding="utf-8-sig")

    validation_rows: list[dict[str, Any]] = []
    detail_lookup: dict[str, pd.DataFrame] = {}
    for index, train_row in enumerate(train_report.itertuples(index=False), start=1):
        rule = candidate_lookup[str(train_row.rule_id)]
        validation_row, detail = evaluate_rule(
            rule,
            pool=pool,
            sources=sources,
            current_daily=current_daily,
            baseline=baseline,
            start=validation_start,
            end=validation_end,
        )
        validation_row["validation_gate"] = selection_gate(
            validation_row, MIN_VALIDATION_EXTRA_TRADES
        )
        validation_row["validation_score"] = rule_score(validation_row)
        validation_row.update(
            {
                "train_extra_trade_count": int(train_row.extra_trade_count),
                "train_portfolio_ratio_to_base": float(train_row.portfolio_ratio_to_base),
                "train_extra_multiple": float(train_row.extra_multiple),
                "train_n_total_win_rate": float(train_row.n_total_win_rate),
                "combined_selection_score": (
                    float(validation_row["validation_score"])
                    + 0.40 * float(train_row.selection_score)
                ),
            }
        )
        validation_rows.append(validation_row)
        detail_lookup[rule.rule_id] = detail
        if index % 50 == 0 or index == len(train_report):
            print(f"N入围规则验证回放：{index}/{len(train_report)}")

    validation_report = pd.DataFrame(validation_rows).sort_values(
        ["validation_gate", "combined_selection_score", "portfolio_ratio_to_base"],
        ascending=[False, False, False],
    )
    validation_report.to_csv(
        OUTPUT_DIR / "train_validation_selection.csv", index=False, encoding="utf-8-sig"
    )
    passing = validation_report[validation_report["validation_gate"]]

    # 对所有验证段过关者做测试段审计，只用于判断搜索族是否普遍失效；
    # 不能用这张表回头改选第二名，否则测试段就被污染成新的验证段。
    passer_audit_rows: list[dict[str, Any]] = []
    for validation_row in passing.itertuples(index=False):
        detail = detail_lookup[str(validation_row.rule_id)]
        test_metrics = period_metrics(detail, test_start, test_end)
        base_test_metrics = period_metrics(baseline, test_start, test_end)
        full_metrics = period_metrics(detail, legacy.START_DATE, legacy.END_DATE)
        passer_audit_rows.append(
            {
                "rule_id": str(validation_row.rule_id),
                "validation_extra_trade_count": int(validation_row.extra_trade_count),
                "validation_ratio_to_base": float(validation_row.portfolio_ratio_to_base),
                "test_extra_trade_count": int(test_metrics["extra_trade_count"]),
                "test_extra_multiple": float(test_metrics["extra_multiple"]),
                "test_ratio_to_base": (
                    float(test_metrics["equity_multiple"])
                    / float(base_test_metrics["equity_multiple"])
                ),
                "test_max_drawdown": float(test_metrics["max_drawdown"]),
                "base_test_max_drawdown": float(base_test_metrics["max_drawdown"]),
                "full_n_trade_count": int(full_metrics["n_trade_count"]),
                "full_n_win_rate": float(full_metrics["n_total_win_rate"]),
                "full_multiple": float(full_metrics["equity_multiple"]),
                "full_max_drawdown": float(full_metrics["max_drawdown"]),
                "test_audit_passed": bool(
                    int(test_metrics["extra_trade_count"]) >= 1
                    and float(test_metrics["extra_multiple"]) > 1.0
                    and float(test_metrics["equity_multiple"])
                    >= float(base_test_metrics["equity_multiple"])
                    and float(test_metrics["max_drawdown"])
                    >= float(base_test_metrics["max_drawdown"])
                    - MAX_SPLIT_DRAWDOWN_WORSENING
                ),
            }
        )
    passer_audit = pd.DataFrame(passer_audit_rows)
    if not passer_audit.empty:
        passer_audit = passer_audit.sort_values(
            ["test_audit_passed", "test_ratio_to_base", "full_multiple"],
            ascending=[False, False, False],
        )
    passer_audit.to_csv(
        OUTPUT_DIR / "validation_passers_test_audit.csv",
        index=False,
        encoding="utf-8-sig",
    )

    locked_row = passing.iloc[0] if not passing.empty else validation_report.iloc[0]
    locked_rule = candidate_lookup[str(locked_row["rule_id"])]
    locked_detail = detail_lookup[locked_rule.rule_id]

    # 规则到此锁定；下面才读取测试段和全样本结果，不再换第二名。
    split_rows: list[dict[str, Any]] = []
    for split, start, end in (
        ("TRAIN", train_start, train_end),
        ("VALIDATION", validation_start, validation_end),
        ("TEST_OOS", test_start, test_end),
    ):
        base = period_metrics(baseline, start, end)
        selected = period_metrics(locked_detail, start, end)
        split_rows.append(
            {
                "split": split,
                "start_date": start,
                "end_date": end,
                "base_trade_count": base["trade_count"],
                "selected_trade_count": selected["trade_count"],
                "base_n_trade_count": base["n_trade_count"],
                "selected_n_trade_count": selected["n_trade_count"],
                "extra_trade_count": selected["extra_trade_count"],
                "extra_multiple": selected["extra_multiple"],
                "base_multiple": base["equity_multiple"],
                "selected_multiple": selected["equity_multiple"],
                "ratio_to_base": selected["equity_multiple"] / base["equity_multiple"],
                "base_max_drawdown": base["max_drawdown"],
                "selected_max_drawdown": selected["max_drawdown"],
                "base_n_win_rate": base["n_total_win_rate"],
                "selected_n_win_rate": selected["n_total_win_rate"],
            }
        )
    split_report = pd.DataFrame(split_rows)
    full = period_metrics(locked_detail, legacy.START_DATE, legacy.END_DATE)
    test = split_report[split_report["split"].eq("TEST_OOS")].iloc[0]
    full_gate = bool(
        not passing.empty
        and float(test["ratio_to_base"]) >= 1.0
        and int(test["extra_trade_count"]) >= 1
        and float(test["selected_max_drawdown"])
        >= float(test["base_max_drawdown"]) - MAX_SPLIT_DRAWDOWN_WORSENING
        and float(full["equity_multiple"]) >= float(baseline_full["equity_multiple"]) - EPSILON
        and (
            int(full["n_trade_count"]) > int(baseline_full["n_trade_count"])
            or float(full["n_total_win_rate"]) > float(baseline_full["n_total_win_rate"])
        )
        and float(full["max_drawdown"]) >= float(baseline_full["max_drawdown"]) - EPSILON
    )

    selected_trades = locked_detail[locked_detail["status"].eq("EXECUTED")].copy()
    extra_trades = selected_trades[
        selected_trades["strategy_leg"].eq("N")
        & selected_trades["n_branch"].eq("SUPPLEMENT")
    ].copy()
    payload = {
        "research_only": True,
        "live_changed": False,
        "priority": "D>A>M>E>C>N",
        "baseline": baseline_full,
        "locked_rule": {
            "rule_id": locked_rule.rule_id,
            "conditions": [list(item) for item in locked_rule.conditions],
            "ranker": locked_rule.ranker,
            "origin": locked_rule.origin,
        },
        "train_validation_rule_count": int(len(validation_report)),
        "validation_passing_count": int(len(passing)),
        "full_metrics": full,
        "return_statistics": returns_summary(locked_detail),
        "supplement_bootstrap": bootstrap_mean(extra_trades["account_return"]),
        "promotion_gate_passed": full_gate,
        "warning": (
            "当前N本身曾使用完整历史挑选；本轮仅补充分支做到训练/验证锁定、测试揭盲，"
            "仍不等同于未来实盘样本外。"
        ),
    }

    (OUTPUT_DIR / "locked_candidate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    split_report.to_csv(OUTPUT_DIR / "locked_split_validation.csv", index=False, encoding="utf-8-sig")
    locked_detail.to_csv(OUTPUT_DIR / "locked_portfolio_daily.csv", index=False, encoding="utf-8-sig")
    selected_trades.to_csv(OUTPUT_DIR / "locked_portfolio_trades.csv", index=False, encoding="utf-8-sig")
    extra_trades.to_csv(OUTPUT_DIR / "locked_supplement_trades.csv", index=False, encoding="utf-8-sig")

    print("\n锁定挑战者：", locked_rule.rule_id)
    print(split_report.to_string(index=False))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
