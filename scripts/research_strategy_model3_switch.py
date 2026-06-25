"""
研究 model=3：ABCDE2/D 与 L 龙头策略的市场环境自动切换。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

设计目标：
  - mode=1：继续使用现有 ABCDE2/D 组合收益曲线。
  - mode=2：独立 L 龙头策略。
  - mode=3：T 日收盘后，用当日已经可见的市场/题材/涨停环境字段，
            决定 T+1 使用 mode=1 还是 mode=2。

关键约束：
  - 只使用 trade_date 当天已经存在的字段，不使用未来收益作为切换条件。
  - L 分支重新做实盘约束回放：同一资金不重叠、涨停开盘买不到、跌停卖不出顺延、费用滑点。
  - 这只是第一版规则搜索，目的是筛出值得进一步样本外验证的切换条件。

输出：
  reports/strategy_model3/model3_switch_summary.csv
  reports/strategy_model3/model3_switch_daily.csv
  reports/strategy_model3/model3_switch_yearly.csv
  reports/strategy_model3/model3_switch_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
import sys
from typing import Any, Callable

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_strategy_l_live_execution import (
    BUY_SLIPPAGE_RATE,
    COMMISSION_RATE,
    INITIAL_EQUITY,
    LVariant,
    SELL_SLIPPAGE_RATE,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
    VARIANTS,
    apply_filter,
    has_day,
    is_limit_down_day,
    is_limit_up_one_word,
    load_l_source,
    max_consecutive_losses,
    max_drawdown,
    planned_exit_offset,
    to_numeric,
)


BASELINE_PATH = PROJECT_ROOT / "reports" / "strategy_expansion" / "abcd_expansion_selected_e2_equity_curve.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3"


@dataclass(frozen=True)
class SwitchAtom:
    """一个可解释的切换条件原子。

    例：market_emotion_state_bucket != retreat。
    多个原子可以 AND 组合成一条 model=3 切换规则。
    """

    name: str
    predicate: Callable[[pd.Series], bool]


def load_baseline_daily() -> pd.DataFrame:
    if not BASELINE_PATH.exists():
        raise FileNotFoundError(f"找不到 mode=1 基准收益曲线: {BASELINE_PATH}")
    daily = pd.read_csv(BASELINE_PATH, dtype={"date": str}, low_memory=False)
    daily = daily.sort_values("date").reset_index(drop=True)
    daily["mode1_return"] = to_numeric(daily["combined_return"])
    daily["mode1_operation_status"] = daily.get("operation_status", pd.Series("", index=daily.index)).fillna("").astype(str)
    return daily


def selected_l2_source() -> pd.DataFrame:
    """加载已选 L2 的理论信号源。

    注意：这里使用 leader_strategy_trades 的原始行情字段，不使用 l_live_cert_detail。
    原因是 l_live_cert_detail 中 position_skip 行已经被“纯L模式持仓占用”处理过；
    model=3 选择 L 的日期不同，必须重新回放持仓占用。
    """
    trades = load_l_source()
    l2_variant = next(v for v in VARIANTS if v.name == "L2")
    selected = apply_filter(trades, l2_variant.filter_expr)
    selected = (
        selected.sort_values(["trade_date", "l_account_return"], ascending=[True, False])
        .drop_duplicates("trade_date", keep="first")
        .reset_index(drop=True)
    )
    return selected


def build_l_lookup(l_source: pd.DataFrame) -> dict[str, pd.Series]:
    return {str(row["trade_date"]): row for _, row in l_source.iterrows()}


def _text(row: pd.Series, column: str) -> str:
    return str(row.get(column, "") if column in row.index else "")


def _num(row: pd.Series, column: str, default: float = 0.0) -> float:
    value = pd.to_numeric(row.get(column, default), errors="coerce")
    return default if pd.isna(value) else float(value)


def build_switch_atoms() -> list[SwitchAtom]:
    """候选切换条件。

    这些字段都来自 T 日涨停/市场/题材统计，是收盘后可见字段。
    第一版刻意保持可解释，避免直接用收益排序做黑箱。
    """
    return [
        SwitchAtom("market_segment!=sh_main", lambda r: _text(r, "market_segment") != "sh_main"),
        SwitchAtom("market_segment!=star", lambda r: _text(r, "market_segment") != "star"),
        SwitchAtom("market_segment!=bj", lambda r: _text(r, "market_segment") != "bj"),
        SwitchAtom("market_segment=chi_next", lambda r: _text(r, "market_segment") == "chi_next"),
        SwitchAtom("theme_name!=半导体", lambda r: _text(r, "theme_name") != "半导体"),
        SwitchAtom("market_emotion_state_bucket!=retreat", lambda r: _text(r, "market_emotion_state_bucket") != "retreat"),
        SwitchAtom("market_emotion_state_bucket!=ice_point", lambda r: _text(r, "market_emotion_state_bucket") != "ice_point"),
        SwitchAtom("market_emotion_state_bucket in mixed/warming/main_rise", lambda r: _text(r, "market_emotion_state_bucket") in {"mixed", "warming", "main_rise"}),
        SwitchAtom("segment_retreat_state_bucket!=weak_below_3", lambda r: _text(r, "segment_retreat_state_bucket") != "weak_below_3"),
        SwitchAtom("segment_retreat_state_bucket in neutral/warming_2day", lambda r: _text(r, "segment_retreat_state_bucket") in {"neutral", "warming_2day"}),
        SwitchAtom("market_chain_count_bucket!=lt_3", lambda r: _text(r, "market_chain_count_bucket") != "lt_3"),
        SwitchAtom("market_chain_count_bucket in 8_15/15_30/gte_30", lambda r: _text(r, "market_chain_count_bucket") in {"8_15", "15_30", "gte_30"}),
        SwitchAtom("market_limit_down_count_bucket in lt_5/5_15", lambda r: _text(r, "market_limit_down_count_bucket") in {"lt_5", "5_15"}),
        SwitchAtom("market_limit_down_count_bucket!=gte_60", lambda r: _text(r, "market_limit_down_count_bucket") != "gte_60"),
        SwitchAtom("first_time_detail_bucket!=after_1430", lambda r: _text(r, "first_time_detail_bucket") != "after_1430"),
        SwitchAtom("first_time_detail_bucket in before_1000/1000_1100/1100_1330", lambda r: _text(r, "first_time_detail_bucket") in {"before_1000", "1000_1100", "1100_1330"}),
        SwitchAtom("open_times_bucket!=gte_4", lambda r: _text(r, "open_times_bucket") != "gte_4"),
        SwitchAtom("open_times_bucket in 0/1", lambda r: _text(r, "open_times_bucket") in {"0", "1"}),
        SwitchAtom("theme_limit_count>=2", lambda r: _num(r, "theme_limit_count") >= 2),
        SwitchAtom("theme_limit_count>=5", lambda r: _num(r, "theme_limit_count") >= 5),
        SwitchAtom("theme_limit_count<=20", lambda r: _num(r, "theme_limit_count") <= 20),
        SwitchAtom("same_theme_limit_count>=2", lambda r: _num(r, "same_theme_limit_count") >= 2),
        SwitchAtom("same_theme_limit_count>=5", lambda r: _num(r, "same_theme_limit_count") >= 5),
    ]


def l_trade_return(row: pd.Series) -> tuple[bool, float, str, str]:
    """按 L 实盘认证口径计算单笔收益。

    返回：(是否成功买入卖出, 账户收益, 退出日期, 状态)
    """
    if not has_day(row, 1):
        return False, 0.0, str(row.get("trade_date", "")), "L_SKIP_MISSING_BUY_DAY"
    if is_limit_up_one_word(row):
        return False, 0.0, str(row.get("d1_trade_date", row.get("trade_date", ""))).replace(".0", ""), "L_SKIP_LIMIT_UP_UNBUYABLE"

    buy_price = float(row["d1_open"]) * (1.0 + BUY_SLIPPAGE_RATE)
    start_offset = planned_exit_offset(row)
    exit_date = ""
    exit_price = 0.0
    blocked_days = 0
    for offset in range(start_offset, 6):
        if not has_day(row, offset):
            break
        if is_limit_down_day(row, offset):
            blocked_days += 1
            continue
        exit_date = str(row[f"d{offset}_trade_date"]).replace(".0", "")
        exit_price = float(row[f"d{offset}_close"]) * (1.0 - SELL_SLIPPAGE_RATE)
        break
    if not exit_date:
        return False, 0.0, "99991231", "L_SELL_UNRESOLVED"

    gross_return = exit_price / buy_price - 1.0
    fee_rate = COMMISSION_RATE + TRANSFER_FEE_RATE + COMMISSION_RATE + TRANSFER_FEE_RATE + STAMP_TAX_RATE
    net_return = gross_return - fee_rate
    account_return = net_return * 0.8
    status = "L_EXECUTED" if blocked_days == 0 else f"L_EXECUTED_LIMIT_DOWN_DELAY_{blocked_days}"
    return True, account_return, exit_date, status


def infer_mode1_position_until(daily: pd.DataFrame, index: int) -> str:
    """从 mode=1 日收益曲线推断持仓占用到哪天释放。

    mode=1 明细在持仓期间会出现 POSITION_OCCUPIED_SKIP。
    如果当前日期产生了非零收益，后续连续 POSITION_OCCUPIED_SKIP 视作资金占用期；
    直到第一天非占用日，才允许 model=3 重新做选择。
    """
    if abs(float(daily.loc[index, "mode1_return"])) <= 1e-12:
        return ""
    j = index + 1
    while j < len(daily) and str(daily.loc[j, "mode1_operation_status"]) == "POSITION_OCCUPIED_SKIP":
        j += 1
    if j < len(daily):
        return str(daily.loc[j, "date"])
    return "99991231"


def calc_metrics(name: str, daily: pd.DataFrame, return_column: str, op_column: str) -> dict[str, Any]:
    returns = to_numeric(daily[return_column])
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    # 交易统计只看真正产生收益的开仓记录。
    # POSITION_OCCUPIED / NO_TRADE / L_SKIP 都是状态行，不应进入胜率和中位数统计。
    active = daily[returns.abs() > 1e-12].copy()
    active_returns = to_numeric(active[return_column])
    op_text = active[op_column].astype(str)
    return {
        "scenario": name,
        "day_count": int(len(daily)),
        "trade_count": int(len(active)),
        "l_trade_count": int(op_text.eq("L").sum()),
        "mode1_trade_count": int(op_text.eq("MODE1").sum()),
        "win_rate": float((active_returns > 0).mean()) if len(active_returns) else 0.0,
        "avg_account_return": float(active_returns.mean()) if len(active_returns) else 0.0,
        "median_account_return": float(active_returns.median()) if len(active_returns) else 0.0,
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(active_returns.max()) if len(active_returns) else 0.0,
        "max_loss": float(active_returns.min()) if len(active_returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(active_returns),
    }


def replay_model3(
    baseline: pd.DataFrame,
    l_lookup: dict[str, pd.Series],
    atoms: tuple[SwitchAtom, ...],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """按给定切换规则回放 model=3。

    规则触发且当天有 L2 信号时，选择 L；否则使用 mode=1。
    一旦某个分支开仓，直到该分支推断的退出日之前都不允许新开仓。
    """
    rows: list[dict[str, Any]] = []
    occupied_until = ""
    occupied_by = ""

    for i, row in baseline.iterrows():
        date = str(row["date"])
        if occupied_until and date < occupied_until:
            rows.append({
                **row.to_dict(),
                "model3_return": 0.0,
                "model3_op": f"POSITION_OCCUPIED_BY_{occupied_by}",
                "model3_reason": f"上一笔{occupied_by}持仓到{occupied_until}释放",
            })
            continue

        l_row = l_lookup.get(date)
        choose_l = bool(l_row is not None and all(atom.predicate(l_row) for atom in atoms))

        if choose_l and l_row is not None:
            ok, account_return, exit_date, status = l_trade_return(l_row)
            if ok:
                occupied_until = exit_date
                occupied_by = "L"
                rows.append({
                    **row.to_dict(),
                    "model3_return": account_return,
                    "model3_op": "L",
                    "model3_reason": " AND ".join(atom.name for atom in atoms) or "HAS_L_SIGNAL",
                    "l_ts_code": l_row.get("ts_code", ""),
                    "l_name": l_row.get("name", ""),
                    "l_exit_date": exit_date,
                    "l_status": status,
                })
            else:
                rows.append({
                    **row.to_dict(),
                    "model3_return": 0.0,
                    "model3_op": status,
                    "model3_reason": "L规则触发但实盘约束未成交/未卖出",
                    "l_ts_code": l_row.get("ts_code", ""),
                    "l_name": l_row.get("name", ""),
                    "l_exit_date": exit_date,
                    "l_status": status,
                })
            continue

        mode1_return = float(row["mode1_return"])
        op = "MODE1" if abs(mode1_return) > 1e-12 else "NO_TRADE"
        if op == "MODE1":
            occupied_until = infer_mode1_position_until(baseline, i)
            occupied_by = "MODE1"
        rows.append({
            **row.to_dict(),
            "model3_return": mode1_return,
            "model3_op": op,
            "model3_reason": "L条件不触发，使用mode=1",
        })

    daily = pd.DataFrame(rows)
    metrics = calc_metrics(
        "model3:" + (" AND ".join(atom.name for atom in atoms) if atoms else "HAS_L_SIGNAL"),
        daily,
        "model3_return",
        "model3_op",
    )
    metrics["rule"] = " AND ".join(atom.name for atom in atoms) if atoms else "HAS_L_SIGNAL"
    metrics["switch_l_signal_count"] = int((daily["model3_op"] == "L").sum())
    metrics["blocked_l_count"] = int(daily["model3_op"].astype(str).str.startswith("L_SKIP").sum())
    return daily, metrics


def generate_rules(atoms: list[SwitchAtom], max_size: int = 3) -> list[tuple[SwitchAtom, ...]]:
    rules: list[tuple[SwitchAtom, ...]] = [tuple()]
    for size in range(1, max_size + 1):
        rules.extend(tuple(combo) for combo in combinations(atoms, size))
    return rules


def yearly_metrics(daily: pd.DataFrame, return_column: str, op_column: str, scenario: str) -> pd.DataFrame:
    data = daily.copy()
    data["year"] = data["date"].astype(str).str.slice(0, 4)
    rows = []
    for year, group in data.groupby("year"):
        metrics = calc_metrics(scenario, group, return_column, op_column)
        metrics["year"] = year
        rows.append(metrics)
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, best_daily: pd.DataFrame, yearly: pd.DataFrame) -> None:
    report_path = OUTPUT_DIR / "model3_switch_report.md"
    best = summary.iloc[0]
    lines = [
        "# model=3 自动切换研究",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 最优规则：{best['rule']}",
        f"- 复利：{best['equity_multiple']:.2f}倍",
        f"- 最大回撤：{best['max_drawdown']:.2%}",
        f"- L交易数：{int(best['l_trade_count'])}",
        f"- mode=1交易数：{int(best['mode1_trade_count'])}",
        "",
        "## 规则搜索前20",
        "",
        summary.head(20).to_markdown(index=False),
        "",
        "## 最优规则年度表现",
        "",
        yearly.to_markdown(index=False),
        "",
        "## 最优规则最近50行",
        "",
        best_daily.tail(50).to_markdown(index=False),
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_source = selected_l2_source()
    l_lookup = build_l_lookup(l_source)
    atoms = build_switch_atoms()

    summary_rows: list[dict[str, Any]] = []
    best_daily: pd.DataFrame | None = None
    best_score = -10**18

    # 基准行：mode=1 原始曲线，便于直接对比。
    baseline_for_metrics = baseline.copy()
    baseline_for_metrics["mode1_op"] = baseline_for_metrics["mode1_return"].where(
        baseline_for_metrics["mode1_return"].abs() > 1e-12,
        0.0,
    )
    base_metrics = calc_metrics("mode=1 ABCDE2/D", baseline_for_metrics, "mode1_return", "mode1_operation_status")
    base_metrics["rule"] = "MODE1_ONLY"
    base_metrics["switch_l_signal_count"] = 0
    base_metrics["blocked_l_count"] = 0
    summary_rows.append(base_metrics)

    for rule in generate_rules(atoms, max_size=3):
        daily, metrics = replay_model3(baseline, l_lookup, rule)
        # 样本太少的规则容易过拟合，先保留但排序时降低优先级。
        sample_penalty = 0.0 if metrics["l_trade_count"] >= 20 else 1000.0
        score = metrics["equity_multiple"] - sample_penalty
        summary_rows.append(metrics)
        if score > best_score:
            best_score = score
            best_daily = daily

    summary = pd.DataFrame(summary_rows)
    summary = summary.sort_values(
        ["equity_multiple", "max_drawdown", "trade_count"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    if best_daily is None:
        raise RuntimeError("未生成 model=3 回放结果")
    best_rule = str(summary.iloc[0]["rule"])
    if best_rule != "MODE1_ONLY":
        # 重新回放排序第一的规则，保证 daily 与 summary 第一行一致。
        name_to_atom = {atom.name: atom for atom in atoms}
        rule_atoms = tuple(name_to_atom[name.strip()] for name in best_rule.split(" AND ") if name.strip() in name_to_atom)
        best_daily, _ = replay_model3(baseline, l_lookup, rule_atoms)
    else:
        best_daily = baseline.copy()
        best_daily["model3_return"] = best_daily["mode1_return"]
        best_daily["model3_op"] = best_daily["mode1_operation_status"]
        best_daily["model3_reason"] = "MODE1_ONLY"

    yearly = yearly_metrics(best_daily, "model3_return", "model3_op", str(summary.iloc[0]["scenario"]))

    summary.to_csv(OUTPUT_DIR / "model3_switch_summary.csv", index=False, encoding="utf-8-sig")
    best_daily.to_csv(OUTPUT_DIR / "model3_switch_daily.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "model3_switch_yearly.csv", index=False, encoding="utf-8-sig")
    write_report(summary, best_daily, yearly)

    print("model=3 自动切换研究完成")
    print(summary.head(20).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
