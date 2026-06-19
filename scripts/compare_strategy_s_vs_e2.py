"""
对比当前 E2 与创业板 20CM 策略 S 的资金占用择优结果。

固定口径：
  - A/B/C/D 继续优先，占用资金时 E2/S 都不能开仓。
  - E2 使用当前实盘口径：segment_retreat_state_bucket=neutral，按 circ_mv 升序。
  - S 使用创业板20CM独立上限里较优且事前可见的规则：
    segment_retreat_state_bucket=neutral
    segment_limit_height_rank_bucket=rank_1
    market_segment=chi_next
    limit_pct_bucket=20cm
    按 circ_mv 升序。
  - 同一资金不允许重叠占用，T日信号，T+1开盘买，T+2收盘卖。
  - 本脚本只做历史模拟对比，不接入实盘。

输出：
  reports/strategy_s/e2_vs_s/e2_vs_s_summary.csv
  reports/strategy_s/e2_vs_s/e2_vs_s_trades.csv
  reports/strategy_s/e2_vs_s/e2_vs_s_equity_curve.csv
  reports/strategy_s/e2_vs_s/e2_vs_s_yearly.csv
  reports/strategy_s/e2_vs_s/e2_vs_s_report.md

用法：
  .venv/bin/python scripts/compare_strategy_s_vs_e2.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]

START_DATE = "20240520"
END_DATE = "20260514"
INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.8
D_FILL_RATE = 0.8

OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_s" / "e2_vs_s"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
CURRENT_COMBO_CURVE_PATH = (
    PROJECT_ROOT / "reports" / "strategy_expansion" / "abcd_expansion_selected_e2_equity_curve.csv"
)
SOURCE_PATH = PROJECT_ROOT / "reports" / "recent_2y_full_strategy_exclude_st_exclude_amount_ratio_top500_detail.csv"

SCENARIOS = {
    "current_e2_only": "A/B/C/D空闲时只用E2",
    "s_only": "A/B/C/D空闲时只用S",
    "s_first_then_e2": "A/B/C/D空闲时S优先，否则E2",
    "e2_first_then_s": "A/B/C/D空闲时E2优先，否则S",
}


def clean_date(value: Any) -> str:
    text = str(value).strip()
    return text.replace(".0", "") if text.endswith(".0") else text


def is_true_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "1.0"])


def load_open_dates() -> list[str]:
    calendar = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    if "is_open" in calendar.columns:
        calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    return sorted(calendar["cal_date"].astype(str).tolist())


def next_trade_day(date_str: str, n: int, open_dates: list[str]) -> str:
    future = [date for date in open_dates if date > date_str]
    return future[n - 1] if len(future) >= n else date_str


def max_consecutive_losses(returns: pd.Series) -> int:
    max_count = 0
    current = 0
    for value in returns:
        if value <= 0:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def equity_stats(returns: pd.Series) -> dict[str, Any]:
    equity = INITIAL_EQUITY * (1 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1
    return {
        "final_equity": float(equity.iloc[-1]),
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY),
        "max_drawdown": float(drawdown.min()),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def load_base_curve() -> pd.DataFrame:
    curve = pd.read_csv(CURRENT_COMBO_CURVE_PATH, dtype={"date": str})
    curve["date"] = curve["date"].map(clean_date)
    curve = curve[(curve["date"] >= START_DATE) & (curve["date"] <= END_DATE)].copy()
    for column in ["abc_return", "d_return", "base_return", "combined_return"]:
        curve[column] = pd.to_numeric(curve[column], errors="coerce").fillna(0.0)
    curve["abcd_busy"] = curve["operation_status"].astype(str).isin(
        ["HISTORICAL_SIM_FILLED", "POSITION_OCCUPIED_SKIP"]
    ) | (curve["base_return"].abs() > 0)
    return curve[["date", "abc_return", "d_return", "base_return", "combined_return", "operation_status", "abcd_busy"]]


def load_reliable_source() -> pd.DataFrame:
    data = pd.read_csv(SOURCE_PATH, low_memory=False)
    data["trade_date"] = data["trade_date"].map(clean_date)
    data = data[(data["trade_date"] >= START_DATE) & (data["trade_date"] <= END_DATE)].copy()
    data = data.drop_duplicates(["trade_date", "ts_code"]).copy()

    for column in [
        "net_return",
        "amount",
        "turnover_rate",
        "volume_ratio",
        "circ_mv",
        "fd_amount_to_circ_mv",
        "fill_probability",
        "market_leader_rank",
        "segment_market_leader_rank",
    ]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    data = data[data["net_return"].notna()].copy()
    if "is_st" in data.columns:
        data = data[~is_true_series(data["is_st"])].copy()
    if "allow_buy_reliable" in data.columns:
        data = data[is_true_series(data["allow_buy_reliable"])].copy()
    if "is_fill_score_reliable" in data.columns:
        data = data[is_true_series(data["is_fill_score_reliable"])].copy()
    if "buy_executed" in data.columns:
        data = data[is_true_series(data["buy_executed"])].copy()
    if "sell_executed" in data.columns:
        data = data[is_true_series(data["sell_executed"])].copy()
    return data


def candidate_maps(data: pd.DataFrame) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    e2 = data[data["segment_retreat_state_bucket"].astype(str) == "neutral"].copy()
    e2 = e2.sort_values(["trade_date", "circ_mv"], ascending=[True, True])
    e2_map = {date: group.iloc[0] for date, group in e2.groupby("trade_date", sort=True)}

    s = data[
        (data["market_segment"].astype(str) == "chi_next")
        & (data["limit_pct_bucket"].astype(str) == "20cm")
        & (data["segment_retreat_state_bucket"].astype(str) == "neutral")
        & (data["segment_limit_height_rank_bucket"].astype(str) == "rank_1")
    ].copy()
    s = s.sort_values(["trade_date", "circ_mv"], ascending=[True, True])
    s_map = {date: group.iloc[0] for date, group in s.groupby("trade_date", sort=True)}
    return e2_map, s_map


def row_exit_date(row: pd.Series, signal_date: str, open_dates: list[str]) -> str:
    exit_date = clean_date(row.get("exit_trade_date", next_trade_day(signal_date, 2, open_dates)))
    if not exit_date or exit_date == "nan":
        return next_trade_day(signal_date, 2, open_dates)
    return exit_date


def choose_candidate(
    scenario: str,
    signal_date: str,
    e2_map: dict[str, pd.Series],
    s_map: dict[str, pd.Series],
) -> tuple[str, pd.Series | None]:
    e2 = e2_map.get(signal_date)
    s = s_map.get(signal_date)
    if scenario == "current_e2_only":
        return ("E2", e2) if e2 is not None else ("", None)
    if scenario == "s_only":
        return ("S", s) if s is not None else ("", None)
    if scenario == "s_first_then_e2":
        if s is not None:
            return "S", s
        return ("E2", e2) if e2 is not None else ("", None)
    if scenario == "e2_first_then_s":
        if e2 is not None:
            return "E2", e2
        return ("S", s) if s is not None else ("", None)
    return "", None


def simulate_scenario(
    scenario: str,
    base: pd.DataFrame,
    e2_map: dict[str, pd.Series],
    s_map: dict[str, pd.Series],
    open_dates: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    expansion_occupied_until = ""

    for _, base_row in base.sort_values("date").iterrows():
        signal_date = str(base_row["date"])
        base_return = float(base_row["base_return"])
        expansion_leg = ""
        expansion_return = 0.0
        ts_code = ""
        name = ""
        exit_date = ""
        skip_reason = ""

        if bool(base_row["abcd_busy"]):
            skip_reason = "ABCD_BUSY"
        elif expansion_occupied_until and signal_date <= expansion_occupied_until:
            skip_reason = "EXPANSION_POSITION_OCCUPIED"
        else:
            e2_candidate = e2_map.get(signal_date)
            s_candidate = s_map.get(signal_date)
            expansion_leg, candidate = choose_candidate(scenario, signal_date, e2_map, s_map)
            if candidate is None:
                skip_reason = "NO_EXPANSION_CANDIDATE"
            else:
                expansion_return = float(candidate["net_return"]) * POSITION_PCT
                ts_code = str(candidate.get("ts_code", ""))
                name = str(candidate.get("name", ""))
                exit_date = row_exit_date(candidate, signal_date, open_dates)
                expansion_occupied_until = exit_date
                trades.append(
                    {
                        "scenario": scenario,
                        "strategy_leg": expansion_leg,
                        "signal_date": signal_date,
                        "exit_date": exit_date,
                        "ts_code": ts_code,
                        "name": name,
                        "account_return": expansion_return,
                        "net_return": float(candidate["net_return"]),
                        "market_segment": candidate.get("market_segment", ""),
                        "limit_pct_bucket": candidate.get("limit_pct_bucket", ""),
                        "segment_retreat_state_bucket": candidate.get("segment_retreat_state_bucket", ""),
                        "segment_limit_height_rank_bucket": candidate.get("segment_limit_height_rank_bucket", ""),
                        "same_as_e2_candidate": bool(
                            expansion_leg == "S"
                            and e2_candidate is not None
                            and str(candidate.get("ts_code", "")) == str(e2_candidate.get("ts_code", ""))
                        ),
                        "same_as_s_candidate": bool(
                            expansion_leg == "E2"
                            and s_candidate is not None
                            and str(candidate.get("ts_code", "")) == str(s_candidate.get("ts_code", ""))
                        ),
                    }
                )

        combined_return = (1 + base_return) * (1 + expansion_return) - 1
        rows.append(
            {
                "scenario": scenario,
                "date": signal_date,
                "base_return": base_return,
                "expansion_leg": expansion_leg,
                "expansion_return": expansion_return,
                "combined_return": combined_return,
                "ts_code": ts_code,
                "name": name,
                "exit_date": exit_date,
                "skip_reason": skip_reason,
                "operation_status": base_row["operation_status"],
            }
        )

    curve = pd.DataFrame(rows)
    curve["equity"] = INITIAL_EQUITY * (1 + curve["combined_return"]).cumprod()
    curve["peak_equity"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["peak_equity"] - 1
    trades_df = pd.DataFrame(trades)

    stats = equity_stats(curve["combined_return"])
    expansion_returns = trades_df["account_return"] if not trades_df.empty else pd.Series(dtype=float)
    stats.update(
        {
            "scenario": scenario,
            "description": SCENARIOS[scenario],
            "expansion_trades": int(len(trades_df)),
            "e2_trades": int((trades_df["strategy_leg"] == "E2").sum()) if not trades_df.empty else 0,
            "s_trades": int((trades_df["strategy_leg"] == "S").sum()) if not trades_df.empty else 0,
            "expansion_win_rate": float((expansion_returns > 0).mean()) if len(expansion_returns) else 0.0,
            "expansion_avg_account_return": float(expansion_returns.mean()) if len(expansion_returns) else 0.0,
            "expansion_median_account_return": float(expansion_returns.median()) if len(expansion_returns) else 0.0,
            "expansion_max_profit": float(expansion_returns.max()) if len(expansion_returns) else 0.0,
            "expansion_max_loss": float(expansion_returns.min()) if len(expansion_returns) else 0.0,
            "expansion_max_consecutive_losses": max_consecutive_losses(expansion_returns)
            if len(expansion_returns)
            else 0,
            "s_same_as_e2_candidate": int(trades_df.get("same_as_e2_candidate", pd.Series(dtype=bool)).sum())
            if not trades_df.empty
            else 0,
            "e2_same_as_s_candidate": int(trades_df.get("same_as_s_candidate", pd.Series(dtype=bool)).sum())
            if not trades_df.empty
            else 0,
        }
    )
    return curve, trades_df, stats


def write_yearly(all_curves: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scenario, group in all_curves.groupby("scenario", sort=False):
        prev_equity = INITIAL_EQUITY
        group = group.copy()
        group["year"] = group["date"].astype(str).str[:4]
        for year, year_group in group.groupby("year"):
            last_equity = float(year_group["equity"].iloc[-1])
            rows.append(
                {
                    "scenario": scenario,
                    "year": year,
                    "year_return": last_equity / prev_equity - 1,
                    "active_days": int((year_group["combined_return"] != 0).sum()),
                    "expansion_days": int((year_group["expansion_return"] != 0).sum()),
                }
            )
            prev_equity = last_equity
    yearly = pd.DataFrame(rows)
    yearly.to_csv(OUTPUT_DIR / "e2_vs_s_yearly.csv", index=False, encoding="utf-8-sig")
    return yearly


def write_report(summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    best = summary.iloc[0]
    lines = [
        "# E2 与创业板20CM策略S择优研究报告",
        "",
        "## 研究口径",
        f"- 时间范围：{START_DATE} 至 {END_DATE}",
        "- A/B/C/D 优先；A/B/C/D 占用资金时，E2/S 均跳过。",
        "- E2规则：segment_retreat_state_bucket=neutral，按 circ_mv 升序。",
        "- S规则：创业板20CM，segment_retreat_state_bucket=neutral，segment_limit_height_rank_bucket=rank_1，按 circ_mv 升序。",
        "- 择优规则不使用未来收益，只比较固定优先级场景。",
        "- 本报告只是历史模拟研究，不代表可以直接实盘。",
        "",
        "## 场景汇总",
        summary.to_markdown(index=False),
        "",
        "## 最优场景",
        f"- 场景：{best['scenario']}（{best['description']}）",
        f"- 复利倍数：{float(best['equity_multiple']):.2f}x",
        f"- 最大回撤：{float(best['max_drawdown']):.2%}",
        f"- 扩展交易数：{int(best['expansion_trades'])}",
        f"- E2交易数：{int(best['e2_trades'])}",
        f"- S交易数：{int(best['s_trades'])}",
        f"- S与同日E2候选重合次数：{int(best['s_same_as_e2_candidate'])}",
        f"- E2与同日S候选重合次数：{int(best['e2_same_as_s_candidate'])}",
        f"- 扩展胜率：{float(best['expansion_win_rate']):.2%}",
        "",
        "## 年度拆分",
        yearly.to_markdown(index=False),
        "",
        "## 结论",
        "- 如果 S 优先场景没有明显超过当前 E2，则不应替换当前实盘 E2。",
        "- 若 S 与 E2 同日候选高度重合，说明 S 不是新增收益来源，只是给 E2 的创业板20CM子集贴标签。",
        "- 如果 S 只在少数日期提升组合，下一步应做小资金模拟认证，而不是直接实盘。",
        "- 实盘前仍需验证单笔5万元上限、账户余额、涨停买不到、跌停卖不出、滑点手续费和QMT委托拒单日志。",
    ]
    (OUTPUT_DIR / "e2_vs_s_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    open_dates = load_open_dates()
    base = load_base_curve()
    source = load_reliable_source()
    e2_map, s_map = candidate_maps(source)

    curves: list[pd.DataFrame] = []
    trades: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        curve, scenario_trades, stats = simulate_scenario(scenario, base, e2_map, s_map, open_dates)
        curves.append(curve)
        if not scenario_trades.empty:
            trades.append(scenario_trades)
        summaries.append(stats)

    all_curves = pd.concat(curves, ignore_index=True)
    all_trades = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    summary = pd.DataFrame(summaries).sort_values(
        ["equity_multiple", "max_drawdown", "expansion_trades"],
        ascending=[False, False, False],
    )
    summary.to_csv(OUTPUT_DIR / "e2_vs_s_summary.csv", index=False, encoding="utf-8-sig")
    all_curves.to_csv(OUTPUT_DIR / "e2_vs_s_equity_curve.csv", index=False, encoding="utf-8-sig")
    all_trades.to_csv(OUTPUT_DIR / "e2_vs_s_trades.csv", index=False, encoding="utf-8-sig")
    yearly = write_yearly(all_curves)
    write_report(summary, yearly)

    print(summary.to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
