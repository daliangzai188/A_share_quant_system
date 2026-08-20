"""按严格as-of口径检查D/A/E/C/N。

本脚本只写研究报告，不修改实盘开关、发布冻结或券商状态。核心约束：

1. 历史成交概率只读逐日as-of评分，训练截止日必须早于信号日；
2. A/C/E/N按T+1开盘买，D按信号日涨停价买；统一处理前复权、涨跌停、
   卖出延期、双边滑点和日期化费用；
3. 按D>A>E>C>N顺序做单账户串行回放；
4. 对每条腿做“有该腿/删除该腿”组合边际比较；
5. 同时报告Wilson胜率区间、bootstrap平均收益区间、样本外状态和容量缺口。

运行：
    python3 scripts/validate_other_live_strategies_strict.py
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

from scripts import certify_current_executable_portfolio as cert  # noqa: E402
from scripts.backtest_strategy_d import build_daily_candidate_ledger  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    configured_c_conditions,
    reject_strategy_risk_mask,
)
from scripts.verify_strategy_e_alignment import load_historical_bucketed_pool  # noqa: E402
from src.adjusted_returns import linked_forward_adjusted_return  # noqa: E402
from src.market_rules import (  # noqa: E402
    fixed_close_sell_executable,
    listing_trade_day_number,
    price_limit_pct,
)
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strategy_d_spec import historical_candidate_mask  # noqa: E402
from src.strategy_e import (  # noqa: E402
    apply_e_entry_gate,
    build_r1_universe_from_pool,
    load_e_spec,
    resolve_exit_offset,
    select_e_candidates,
    select_e_daily_picks,
)
from src.trading_fees import account_return_after_fees  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


START = "20240520"
END = "20260514"
STRICT_SOURCE = ROOT / "data" / "processed" / "limit_up_fill_scored_asof.csv"
OLD_AC = ROOT / "reports" / "ac_daily_candidates" / "ac_daily_candidates.csv"
OLD_D = ROOT / "reports" / "strategy_d" / "d_daily_candidates.csv"
OLD_E = ROOT / "reports" / "strategy_e_samples" / "e_r1_daily_candidates_full.csv"
N_POOL = ROOT / "reports" / "strategy_n_v4" / "n_backtest_candidates.csv"
STRATEGY_CONFIG = ROOT / "config" / "strategy_config.json"
POSITIONS = ROOT / "data" / "processed" / "positions.json"
OUT = ROOT / "reports" / "strict_live_strategy_audit"

POSITION_PCT = float(cert.POSITION_PCT)
D_FILL_STRESS = 0.80
COMMISSION_RATE = float(cert._ANALYSIS_CONFIG.get("commission_rate", 0.0003))
TRANSFER_FEE_RATE = float(cert._ANALYSIS_CONFIG.get("transfer_fee_rate", 0.00001))
STAMP_TAX_SCHEDULE = cert._ANALYSIS_CONFIG.get("stamp_tax_schedule")
EXPECTED_FILL_METHOD = "asof_turnover_space_proxy_v2"
BOOTSTRAP_SAMPLES = 20_000
BOOTSTRAP_SEED = 20260820


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def source_audit() -> tuple[pd.DataFrame, dict[str, Any]]:
    source = pd.read_csv(STRICT_SOURCE, low_memory=False)
    for column in ("trade_date", "as_of_date", "model_training_end_date"):
        source[column] = date_text(source[column])
    reliable = truthy(source["is_fill_score_reliable"])
    audit = {
        "path": str(STRICT_SOURCE.relative_to(ROOT)),
        "row_count": int(len(source)),
        "trade_date_count": int(source["trade_date"].nunique()),
        "first_date": str(source["trade_date"].min()),
        "last_date": str(source["trade_date"].max()),
        "duplicate_key_count": int(source.duplicated(["trade_date", "ts_code"]).sum()),
        "as_of_date_mismatch_count": int(source["as_of_date"].ne(source["trade_date"]).sum()),
        "reliable_method_bad_count": int(
            source.loc[reliable, "fill_probability_method"].ne(EXPECTED_FILL_METHOD).sum()
        ),
        "reliable_training_not_prior_count": int(
            source.loc[reliable, "model_training_end_date"]
            .ge(source.loc[reliable, "trade_date"])
            .sum()
        ),
    }
    audit["passed"] = not any(
        audit[key]
        for key in (
            "duplicate_key_count",
            "as_of_date_mismatch_count",
            "reliable_method_bad_count",
            "reliable_training_not_prior_count",
        )
    )
    if not audit["passed"]:
        raise RuntimeError(f"严格as-of源审计失败：{audit}")
    return source, audit


def wilson_interval(wins: int, count: int) -> dict[str, float]:
    if count <= 0:
        return {"lower": 0.0, "upper": 0.0}
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return {"lower": center - radius, "upper": center + radius}


def max_consecutive_losses(values: np.ndarray) -> int:
    maximum = current = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def return_metrics(returns: pd.Series, *, seed_offset: int = 0) -> dict[str, Any]:
    values = pd.to_numeric(returns, errors="raise").dropna().to_numpy(dtype=float)
    if len(values) == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
            "win_rate_wilson_95_lower": 0.0,
            "win_rate_wilson_95_upper": 0.0,
            "avg_return_bootstrap_95_lower": 0.0,
            "avg_return_bootstrap_95_upper": 0.0,
        }
    wins = int((values > 0).sum())
    curve = np.cumprod(1 + values)
    drawdown = curve / np.maximum.accumulate(curve) - 1
    positive = values[values > 0]
    negative = values[values < 0]
    rng = np.random.default_rng(BOOTSTRAP_SEED + seed_offset)
    indexes = rng.integers(0, len(values), size=(BOOTSTRAP_SAMPLES, len(values)))
    bootstrap = values[indexes].mean(axis=1)
    low, high = np.quantile(bootstrap, [0.025, 0.975])
    interval = wilson_interval(wins, len(values))
    return {
        "trade_count": int(len(values)),
        "win_rate": float(wins / len(values)),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "equity_multiple": float(curve[-1]),
        "max_drawdown": float(drawdown.min()),
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(values),
        "win_rate_wilson_95_lower": float(interval["lower"]),
        "win_rate_wilson_95_upper": float(interval["upper"]),
        "avg_return_bootstrap_95_lower": float(low),
        "avg_return_bootstrap_95_upper": float(high),
    }


def account_return(stock_return: float, exit_date: str, position_pct: float = POSITION_PCT) -> float:
    return account_return_after_fees(
        stock_return_before_fees=float(stock_return),
        exit_date=str(exit_date),
        position_pct=float(position_pct),
        commission_rate=COMMISSION_RATE,
        transfer_fee_rate=TRANSFER_FEE_RATE,
        stamp_tax_schedule=STAMP_TAX_SCHEDULE,
    )


def build_ac(source_path: Path) -> pd.DataFrame:
    config = load_json_config(STRATEGY_CONFIG)

    def generator(conditions: list[dict[str, Any]] | None, label: str) -> PaperCandidateGenerator:
        selected = condition_strategy_config(config, conditions, label) if conditions else config
        item = PaperCandidateGenerator(STRATEGY_CONFIG, input_trades_path=source_path)
        item.config = selected
        item.paper_config = selected.get("paper_candidate", {})
        item.risk_thresholds = item.paper_config.get("risk_thresholds", {})
        return item

    strategy_a = generator(None, "A")
    strategy_c = generator(configured_c_conditions(config), "C")
    all_candidates = strategy_a.load_all_candidates()
    a_filtered = strategy_a.apply_strategy_filters(all_candidates)
    c_filtered = strategy_c.apply_strategy_filters(all_candidates)
    a_filtered = a_filtered[a_filtered["trade_date"].between(START, END)]
    c_filtered = c_filtered[c_filtered["trade_date"].between(START, END)]
    a_by_date = {date: rows for date, rows in a_filtered.groupby("trade_date")}
    c_by_date = {date: rows for date, rows in c_filtered.groupby("trade_date")}
    rows: list[dict[str, Any]] = []
    for signal_date in sorted(set(a_by_date) | set(c_by_date)):
        leg = ""
        picked: pd.Series | None = None
        if signal_date in a_by_date:
            ranked = strategy_a.rank_candidates(a_by_date[signal_date].copy()).reset_index(drop=True)
            if len(ranked):
                leg, picked = "A", ranked.iloc[0]
        if picked is None and signal_date in c_by_date:
            ranked = strategy_c.rank_candidates(c_by_date[signal_date].copy()).reset_index(drop=True)
            rejected = reject_strategy_risk_mask(ranked, config, "c_strategy")
            ranked = ranked[~pd.Series(rejected.values, index=ranked.index)]
            if len(ranked):
                leg, picked = "C", ranked.iloc[0]
        if picked is None:
            continue
        hold = 2 if leg == "A" else 3
        execution = trade_return_details(
            str(signal_date), str(picked["ts_code"]), hold, name=str(picked.get("name", ""))
        )
        value = None
        if execution.status == "OK" and execution.stock_return is not None:
            value = account_return(execution.stock_return, execution.exit_date)
        rows.append(
            {
                "signal_date": str(signal_date),
                "strategy_leg": leg,
                "ts_code": str(picked["ts_code"]),
                "name": str(picked.get("name", "")),
                "status": execution.status,
                "buy_date": execution.buy_date,
                "exit_date": execution.exit_date,
                "stock_return_before_fees": execution.stock_return,
                "account_return": value,
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


@dataclass
class DailyData:
    trade_dates: list[str]
    index: dict[str, int]
    stock_basic: pd.DataFrame
    cache: dict[str, pd.DataFrame]

    def day(self, date: str) -> pd.DataFrame:
        if date not in self.cache:
            path = ROOT / "data" / "raw" / "daily" / f"{date}.csv"
            self.cache[date] = (
                pd.read_csv(path, dtype={"ts_code": str}, low_memory=False).set_index("ts_code")
                if path.exists()
                else pd.DataFrame()
            )
        return self.cache[date]


def daily_data() -> DailyData:
    calendar = pd.read_csv(ROOT / "data" / "raw" / "trade_calendar.csv", dtype=str)
    calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True"})]
    dates = sorted(calendar["cal_date"].astype(str))
    basic = pd.read_csv(
        ROOT / "data" / "raw" / "stock_basic" / "stock_basic_all.csv",
        dtype=str,
        low_memory=False,
    ).drop_duplicates("ts_code", keep="last").set_index("ts_code")
    return DailyData(dates, {date: index for index, date in enumerate(dates)}, basic, {})


def d_execution(row: pd.Series, data: DailyData) -> dict[str, Any]:
    signal_date = str(row["signal_date"])
    ts_code = str(row["ts_code"])
    signal_index = data.index.get(signal_date)
    if signal_index is None:
        return {"status": "NO_CALENDAR"}
    buy_price = float(row["limit_close"])
    name = str(row.get("name", ""))
    list_date = ""
    if ts_code in data.stock_basic.index:
        basic = data.stock_basic.loc[ts_code]
        name = name or str(basic.get("name", ""))
        list_date = str(basic.get("list_date", "")).replace(".0", "")
    last_status = "SELL_UNRESOLVED"
    for offset in range(2, 6):
        if signal_index + offset >= len(data.trade_dates):
            break
        exit_date = data.trade_dates[signal_index + offset]
        frame = data.day(exit_date)
        if frame.empty or ts_code not in frame.index:
            last_status = "NO_PRICE"
            continue
        exit_row = frame.loc[ts_code]
        pre_close = float(exit_row.get("pre_close", 0) or 0)
        close = float(exit_row.get("close", 0) or 0)
        listing_day = listing_trade_day_number(list_date, exit_date, data.trade_dates)
        limit_pct = price_limit_pct(
            ts_code, name=name, trade_date=exit_date, listing_day_number=listing_day
        )
        if pre_close > 0 and not fixed_close_sell_executable(
            pre_close=pre_close, close_price=close, limit_pct=limit_pct
        ):
            last_status = "LIMIT_DOWN_DELAY"
            continue
        if close <= 0:
            last_status = "BAD_PRICE"
            continue
        try:
            stock_return = linked_forward_adjusted_return(
                ts_code=ts_code,
                buy_date=signal_date,
                buy_price=buy_price,
                sell_date=exit_date,
                sell_price=close * 0.999,
                trade_dates=data.trade_dates,
                daily_loader=data.day,
            )
        except ValueError:
            last_status = "NO_ADJUSTED_PRICE"
            continue
        return {
            "status": "OK",
            "buy_date": signal_date,
            "exit_date": exit_date,
            "stock_return_before_fees": stock_return,
            "account_return": account_return(
                stock_return, exit_date, POSITION_PCT * D_FILL_STRESS
            ),
        }
    return {"status": last_status}


def build_d(source: pd.DataFrame, data: DailyData) -> pd.DataFrame:
    pool = source[
        historical_candidate_mask(
            source,
            min_fill_probability=0.80,
            allowed_segments={"sh_main", "sz_main", "chi_next", "star", "bj"},
        )
        & source["trade_date"].between(START, END)
    ].copy()
    picks = build_daily_candidate_ledger(pool)
    rows: list[dict[str, Any]] = []
    for _, row in picks.iterrows():
        execution = d_execution(row, data)
        rows.append(
            {
                "signal_date": str(row["signal_date"]),
                "strategy_leg": "D",
                "ts_code": str(row["ts_code"]),
                "name": str(row.get("name", "")),
                **execution,
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def add_fixed_open_outcomes(picks: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in picks.sort_values("trade_date").iterrows():
        hold = resolve_exit_offset(spec, str(row["exit_rule"]))
        execution = trade_return_details(
            str(row["trade_date"]),
            str(row["ts_code"]),
            hold,
            name=str(row.get("name", "")),
        )
        value = None
        if execution.status == "OK" and execution.stock_return is not None:
            value = account_return(execution.stock_return, execution.exit_date)
        rows.append(
            {
                "signal_date": str(row["trade_date"]),
                "strategy_leg": "E",
                "ts_code": str(row["ts_code"]),
                "name": str(row.get("name", "")),
                "exit_rule": str(row["exit_rule"]),
                "status": execution.status,
                "buy_date": execution.buy_date,
                "exit_date": execution.exit_date,
                "stock_return_before_fees": execution.stock_return,
                "account_return": value,
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def build_e() -> tuple[pd.DataFrame, pd.DataFrame]:
    spec = load_e_spec(ROOT)
    pool = load_historical_bucketed_pool("20240523", "20260512", 80)
    universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
    ranked = select_e_candidates(universe)
    pre_gate = ranked.groupby("trade_date", as_index=False).head(1).copy()
    post_gate = select_e_daily_picks(universe, spec)
    return add_fixed_open_outcomes(pre_gate, spec), add_fixed_open_outcomes(post_gate, spec)


def build_n() -> pd.DataFrame:
    frame = pd.read_csv(N_POOL, dtype=str, low_memory=False)
    if not frame["fill_probability_method"].eq(EXPECTED_FILL_METHOD).all():
        raise RuntimeError("N候选池不是严格as-of成交评分")
    rows: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        status = str(row.execution_status)
        value = None
        if status == "OK":
            value = account_return(float(row.stock_return_before_fees), str(row.exit_date))
        rows.append(
            {
                "signal_date": str(row.trade_date),
                "strategy_leg": "N",
                "ts_code": str(row.ts_code),
                "name": str(row.name),
                "status": status,
                "buy_date": str(row.buy_date),
                "exit_date": str(row.exit_date).replace("nan", ""),
                "account_return": value,
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def candidate_map(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    valid = frame[frame["status"].astype(str).eq("OK")].copy()
    return {
        str(row["signal_date"]): row.to_dict()
        for _, row in valid.drop_duplicates("signal_date", keep="last").iterrows()
    }


def baseline_dates() -> list[str]:
    frame = pd.read_csv(cert.BASELINE_PATH, dtype={"date": str}, low_memory=False)
    return date_text(frame["date"]).tolist()


def replay(
    maps: dict[str, dict[str, dict[str, Any]]], enabled: set[str]
) -> pd.DataFrame:
    equity = 1.0
    occupied_until = occupied_leg = occupied_code = occupied_name = ""
    rows: list[dict[str, Any]] = []
    priority = ("A", "E", "C", "N")
    for signal_date in baseline_dates():
        if occupied_until and signal_date < occupied_until:
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "SKIP_OCCUPIED",
                    "strategy_leg": "",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue
        blocking_handoff = bool(
            occupied_until
            and signal_date == occupied_until
            and not cert.hit_limit_up(signal_date, occupied_code, occupied_name)
        )
        occupied_until = occupied_leg = occupied_code = occupied_name = ""
        selected: dict[str, Any] | None = None
        if "D" in enabled and signal_date in maps["D"] and not blocking_handoff:
            selected = maps["D"][signal_date]
        else:
            for leg in priority:
                if leg in enabled and signal_date in maps[leg]:
                    selected = maps[leg][signal_date]
                    break
        if selected is None:
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "NO_CANDIDATE",
                    "strategy_leg": "",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue
        value = float(selected["account_return"])
        equity *= 1 + value
        occupied_until = str(selected["exit_date"])
        occupied_leg = str(selected["strategy_leg"])
        occupied_code = str(selected["ts_code"])
        occupied_name = str(selected.get("name", ""))
        rows.append(
            {
                "signal_date": signal_date,
                "status": "EXECUTED",
                "strategy_leg": occupied_leg,
                "ts_code": occupied_code,
                "name": occupied_name,
                "exit_date": occupied_until,
                "account_return": value,
                "equity_after": equity,
            }
        )
    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1
    return detail


def combo_metrics(detail: pd.DataFrame, low: str = START, high: str = END) -> dict[str, Any]:
    sample = detail[detail["signal_date"].between(low, high)].copy()
    trades = sample[sample["status"].eq("EXECUTED")]
    result = return_metrics(trades["account_return"], seed_offset=99)
    result["leg_counts"] = trades["strategy_leg"].value_counts().sort_index().to_dict()
    return result


def identity_comparison(
    leg: str,
    old: pd.DataFrame,
    strict: pd.DataFrame,
    old_date: str,
    old_code: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    left = old[[old_date, old_code]].rename(
        columns={old_date: "signal_date", old_code: "ts_code_old"}
    )
    left["signal_date"] = date_text(left["signal_date"])
    right = strict[["signal_date", "ts_code"]].rename(columns={"ts_code": "ts_code_strict"})
    merged = left.merge(right, on="signal_date", how="outer", indicator=True)
    merged["same_stock"] = merged["ts_code_old"].eq(merged["ts_code_strict"])
    merged.insert(0, "strategy_leg", leg)
    summary = {
        "strategy_leg": leg,
        "old_candidate_count": int(len(left)),
        "strict_candidate_count": int(len(right)),
        "same_stock_count": int(merged["same_stock"].sum()),
        "changed_or_missing_count": int((~merged["same_stock"]).sum()),
        "old_only_count": int(merged["_merge"].eq("left_only").sum()),
        "strict_only_count": int(merged["_merge"].eq("right_only").sum()),
    }
    return summary, merged[~merged["same_stock"]].copy()


def local_live_counts() -> dict[str, int]:
    result = {leg: 0 for leg in ("D", "A", "E", "C", "N")}
    if not POSITIONS.exists():
        return result
    payload = json.loads(POSITIONS.read_text(encoding="utf-8-sig"))
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        leg = str(item.get("strategy_leg", "")).upper()
        if leg in result and str(item.get("status", "open")).lower() != "closed":
            result[leg] += 1
    return result


def markdown_metric(metrics: dict[str, Any]) -> str:
    return (
        f"{metrics['trade_count']}笔，胜率{metrics['win_rate']:.2%}，"
        f"平均{metrics['avg_account_return']:.2%}，中位{metrics['median_account_return']:.2%}，"
        f"复利{metrics['equity_multiple']:.6f}倍，最大回撤{metrics['max_drawdown']:.2%}，"
        f"最大盈利{metrics['max_profit']:.2%}，最大亏损{metrics['max_loss']:.2%}，"
        f"盈亏比{metrics['profit_loss_ratio']:.3f}，最长连亏{metrics['max_consecutive_losses']}笔"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    source, audit = source_audit()
    data = daily_data()
    ac = build_ac(STRICT_SOURCE)
    strategy_d = build_d(source, data)
    e_pre, strategy_e = build_e()
    strategy_n = build_n()

    strategy_a = ac[ac["strategy_leg"].eq("A")].copy()
    strategy_c = ac[ac["strategy_leg"].eq("C")].copy()
    legs = {"D": strategy_d, "A": strategy_a, "E": strategy_e, "C": strategy_c, "N": strategy_n}
    for leg, frame in legs.items():
        frame.to_csv(OUT / f"strict_{leg.lower()}_candidates.csv", index=False, encoding="utf-8-sig")
    e_pre.to_csv(OUT / "strict_e_pre_gate_candidates.csv", index=False, encoding="utf-8-sig")

    metric_rows: list[dict[str, Any]] = []
    for offset, (leg, frame) in enumerate(legs.items()):
        executed = frame[frame["status"].astype(str).eq("OK")]
        metric_rows.append(
            {"strategy_leg": leg, "sample_scope": "strict_all_candidates", **return_metrics(
                executed["account_return"], seed_offset=offset
            )}
        )
        for label, low, high in (
            ("first_half", START, "20250602"),
            ("second_half", "20250603", END),
        ):
            sample = executed[executed["signal_date"].between(low, high)]
            metric_rows.append(
                {"strategy_leg": leg, "sample_scope": label, **return_metrics(
                    sample["account_return"], seed_offset=10 + offset
                )}
            )
    metrics = pd.DataFrame(metric_rows)
    metrics.to_csv(OUT / "strict_leg_metrics.csv", index=False, encoding="utf-8-sig")

    e_gate_rows = []
    for label, frame in (("E_PRE_GATE", e_pre), ("E_CURRENT_GATE", strategy_e)):
        executed = frame[frame["status"].eq("OK")]
        e_gate_rows.append({"variant": label, **return_metrics(executed["account_return"])})
    e_gate = pd.DataFrame(e_gate_rows)
    e_gate.to_csv(OUT / "e_gate_strict_comparison.csv", index=False, encoding="utf-8-sig")

    old_ac = pd.read_csv(OLD_AC, dtype=str, low_memory=False)
    old_ac = old_ac[old_ac["leg"].isin({"A", "C"})]
    old_d = pd.read_csv(OLD_D, dtype=str, low_memory=False)
    old_d = old_d[date_text(old_d["signal_date"]).between(START, END)]
    old_e = pd.read_csv(OLD_E, dtype=str, low_memory=False)
    identity_rows: list[dict[str, Any]] = []
    identity_details: list[pd.DataFrame] = []
    for args in (
        ("A/C", old_ac, ac, "signal_date", "ts_code"),
        ("D", old_d, strategy_d, "signal_date", "ts_code"),
        ("E_PRE_GATE", old_e, e_pre, "trade_date", "ts_code"),
    ):
        summary, detail = identity_comparison(*args)
        identity_rows.append(summary)
        identity_details.append(detail)
    identity = pd.DataFrame(identity_rows)
    identity.to_csv(OUT / "candidate_identity_summary.csv", index=False, encoding="utf-8-sig")
    pd.concat(identity_details, ignore_index=True).to_csv(
        OUT / "candidate_identity_changes.csv", index=False, encoding="utf-8-sig"
    )

    maps = {leg: candidate_map(frame) for leg, frame in legs.items()}
    all_enabled = set(legs)
    full = replay(maps, all_enabled)
    full.to_csv(OUT / "strict_portfolio_daily.csv", index=False, encoding="utf-8-sig")
    full_metrics = combo_metrics(full)
    marginal_rows: list[dict[str, Any]] = []
    for leg in legs:
        without = replay(maps, all_enabled - {leg})
        without_metrics = combo_metrics(without)
        marginal_rows.append(
            {
                "strategy_leg": leg,
                "without_leg_trade_count": without_metrics["trade_count"],
                "with_leg_trade_count": full_metrics["trade_count"],
                "without_leg_multiple": without_metrics["equity_multiple"],
                "with_leg_multiple": full_metrics["equity_multiple"],
                "with_vs_without_change": (
                    full_metrics["equity_multiple"] / without_metrics["equity_multiple"] - 1
                ),
                "without_leg_max_drawdown": without_metrics["max_drawdown"],
                "with_leg_max_drawdown": full_metrics["max_drawdown"],
                "compound_improved": full_metrics["equity_multiple"] > without_metrics["equity_multiple"],
                "drawdown_noninferior": full_metrics["max_drawdown"] >= without_metrics["max_drawdown"],
            }
        )
        for split, low, high in (
            ("first_half", START, "20250602"),
            ("second_half", "20250603", END),
        ):
            with_split = combo_metrics(full, low, high)
            without_split = combo_metrics(without, low, high)
            marginal_rows.append(
                {
                    "strategy_leg": leg,
                    "split": split,
                    "without_leg_trade_count": without_split["trade_count"],
                    "with_leg_trade_count": with_split["trade_count"],
                    "without_leg_multiple": without_split["equity_multiple"],
                    "with_leg_multiple": with_split["equity_multiple"],
                    "with_vs_without_change": (
                        with_split["equity_multiple"] / without_split["equity_multiple"] - 1
                    ),
                    "without_leg_max_drawdown": without_split["max_drawdown"],
                    "with_leg_max_drawdown": with_split["max_drawdown"],
                    "compound_improved": with_split["equity_multiple"] > without_split["equity_multiple"],
                    "drawdown_noninferior": with_split["max_drawdown"] >= without_split["max_drawdown"],
                }
            )
    marginal = pd.DataFrame(marginal_rows)
    marginal["split"] = marginal.get("split", pd.Series(index=marginal.index)).fillna("full")
    marginal.to_csv(OUT / "strict_portfolio_leg_marginal.csv", index=False, encoding="utf-8-sig")

    all_scope = metrics[metrics["sample_scope"].eq("strict_all_candidates")].set_index("strategy_leg")
    second_half = metrics[metrics["sample_scope"].eq("second_half")].set_index("strategy_leg")
    full_marginal = marginal[marginal["split"].eq("full")].set_index("strategy_leg")
    identity_by_leg = identity.set_index("strategy_leg")
    live_counts = local_live_counts()
    release_facts = {
        "D": {"untouched_oos": False, "existing_release_gate": "FAIL_SAMPLE_LT_50"},
        "A": {"untouched_oos": False, "existing_release_gate": "INVALIDATED_BY_B_REMOVAL"},
        "E": {"untouched_oos": False, "existing_release_gate": "ALIGNMENT_FAIL_AND_GATE_NONINFERIOR_FAIL"},
        "C": {"untouched_oos": False, "existing_release_gate": "INVALIDATED_BY_B_REMOVAL"},
        "N": {"untouched_oos": False, "existing_release_gate": "TEST_OOS_NONINFERIOR_FAIL"},
    }
    verdicts = {
        "D": "DO_NOT_LIVE_CURRENT_VERSION",
        "A": "PAUSE_PENDING_STRICT_RELEASE",
        "E": "DO_NOT_LIVE_CURRENT_VERSION",
        "C": "PAUSE_PENDING_STRICT_RELEASE",
        "N": "DO_NOT_LIVE_CURRENT_VERSION",
    }
    gate_rows: list[dict[str, Any]] = []
    for leg in legs:
        metric = all_scope.loc[leg]
        marginal_row = full_marginal.loc[leg]
        identity_key = "A/C" if leg in {"A", "C"} else ("E_PRE_GATE" if leg == "E" else leg)
        changed = int(identity_by_leg.loc[identity_key, "changed_or_missing_count"]) if identity_key in identity_by_leg.index else 0
        gates = {
            "source_asof_passed": bool(audit["passed"]),
            "existing_strategy_release_gate_passed": False,
            "invalid_certification_blocks_this_buy_path": leg != "D",
            "candidate_identity_unchanged": changed == 0,
            "sample_count_at_least_50": int(metric["trade_count"]) >= 50,
            "win_rate_point_above_50pct": float(metric["win_rate"]) > 0.50,
            "win_rate_wilson_lower_above_50pct": float(metric["win_rate_wilson_95_lower"]) > 0.50,
            "mean_return_bootstrap_lower_positive": float(metric["avg_return_bootstrap_95_lower"]) > 0,
            "second_half_equity_multiple_above_1": float(second_half.loc[leg, "equity_multiple"]) > 1,
            "full_combo_compound_improved": bool(marginal_row["compound_improved"]),
            "full_combo_drawdown_noninferior": bool(marginal_row["drawdown_noninferior"]),
            "untouched_oos_available": bool(release_facts[leg]["untouched_oos"]),
            "capacity_certified": False,
        }
        if leg == "E":
            pre_gate_row = e_gate[e_gate["variant"].eq("E_PRE_GATE")].iloc[0]
            current_gate_row = e_gate[e_gate["variant"].eq("E_CURRENT_GATE")].iloc[0]
            gates["strict_entry_gate_noninferior"] = bool(
                float(current_gate_row["equity_multiple"])
                >= float(pre_gate_row["equity_multiple"])
                and float(current_gate_row["max_drawdown"])
                >= float(pre_gate_row["max_drawdown"])
            )
        if leg == "N":
            gates["known_test_oos_noninferiority_passed"] = False
        for gate, passed in gates.items():
            gate_rows.append(
                {
                    "strategy_leg": leg,
                    "gate": gate,
                    "passed": passed,
                    "verdict": verdicts[leg],
                    "existing_release_gate": release_facts[leg]["existing_release_gate"],
                }
            )
    gate_frame = pd.DataFrame(gate_rows)
    gate_frame.to_csv(OUT / "strict_release_gates.csv", index=False, encoding="utf-8-sig")

    result = {
        "audit_date": "2026-08-20",
        "window": f"{START}~{END}",
        "source_audit": audit,
        "methodology": {
            "selection": "固定当前规则，不重新调参；历史成交评分逐日as-of",
            "execution": "A/C/E/N为T+1开盘，D为信号日涨停价；涨停买不到、跌停卖出延期",
            "returns": "前复权链接、买卖各0.1%滑点（D买入为涨停价）、日期化费用",
            "position": POSITION_PCT,
            "d_fill_stress": D_FILL_STRESS,
            "portfolio": "D>A>E>C>N；单账户串行占仓",
        },
        "strict_leg_metrics": metric_rows,
        "strict_combo": full_metrics,
        "candidate_identity": identity_rows,
        "marginal": marginal.to_dict("records"),
        "release_verdicts": verdicts,
        "local_open_position_counts": live_counts,
        "live_execution_gate_audit": {
            "A_C_E_N": "真实BUY入口调用LiveOrderGateway；认证失效会拒绝新增BUY",
            "D": (
                "monitor_strategy_d_intraday.StrategyDMonitor通过SharedQMTBrokerProxy下单；"
                "该代理在BUY唯一落点调用同一LiveOrderGateway认证门禁"
            ),
            "sell": "LiveOrderGateway仅对BUY检查发布认证，已有持仓SELL不受认证失效影响",
        },
        "global_buy_gate_note": "A/C/D/E/N所有真实BUY路径均受同一发布认证门禁约束",
        "causal_limits": [
            "D/A/C/E规则都在同一2024~2026窗口研究或精修，没有未查看的真正样本外发布段。",
            "N所谓TEST_OOS已被v4研究查看且非劣门禁失败，不能再当未触碰样本。",
            "机械复利没有资金增长后的盘口容量约束，不是可实现资金预测。",
            "组合删除腿对照固定其它规则与腿序，可用于边际判断，但仍继承各腿规则发现偏差。",
        ],
    }
    (OUT / "strict_audit_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# D/A/E/C/N严格实盘资格审计",
        "",
        "> 结论：当前没有一条其它策略满足完整严格发布标准。"
        "A/C暂停新增实盘并重建发布验证；D/E/N当前版本不允许新增实盘。",
        "",
        "## 严格单腿结果",
        "",
    ]
    for leg in ("D", "A", "E", "C", "N"):
        lines.append(f"- {leg}：{markdown_metric(all_scope.loc[leg].to_dict())}；判定 `{verdicts[leg]}`。")
    lines.extend(
        [
            "",
            "## 关键问题",
            "",
            f"- A/C旧候选与严格候选有{int(identity_by_leg.loc['A/C', 'changed_or_missing_count'])}日换票或消失。",
            f"- D旧候选与严格候选有{int(identity_by_leg.loc['D', 'changed_or_missing_count'])}日换票或消失，"
            "且现有D发布门禁已明确因样本少于50笔失败。",
            "- D盘中BUY通过SharedQMTBrokerProxy下单；代理在BUY落点调用LiveOrderGateway，"
            "与其它腿使用同一发布认证门禁。",
            f"- E门禁前旧样本与严格样本有{int(identity_by_leg.loc['E_PRE_GATE', 'changed_or_missing_count'])}日换票、消失或新增；"
            "现有逐票对齐验证失败。",
            "- A/C发布配置仍明确标记为B删除后认证失效；没有新的有效发布证书。",
            "- N测试段组合相对不含N仅0.930732，最大回撤也更差；所谓测试段已被v4研究查看。",
            f"- E严格门禁前为{float(e_gate.loc[e_gate['variant'].eq('E_PRE_GATE'), 'equity_multiple'].iloc[0]):.6f}倍、"
            f"回撤{float(e_gate.loc[e_gate['variant'].eq('E_PRE_GATE'), 'max_drawdown'].iloc[0]):.2%}；"
            f"当前门禁后降至{float(e_gate.loc[e_gate['variant'].eq('E_CURRENT_GATE'), 'equity_multiple'].iloc[0]):.6f}倍、"
            f"回撤{float(e_gate.loc[e_gate['variant'].eq('E_CURRENT_GATE'), 'max_drawdown'].iloc[0]):.2%}，"
            "收益和回撤同时恶化。",
            "- 发布版本截至本次审计的前向影子/真实完成交易均为0笔，容量认证全部缺失。",
            "",
            "## 严格组合",
            "",
            f"- 全组合：{markdown_metric(full_metrics)}。",
            "- 该机械复利只允许做相同口径的相对比较，不代表资金可按该倍数实盘增长。",
            "",
            "## 逐腿组合边际",
            "",
        ]
    )
    for leg in ("D", "A", "E", "C", "N"):
        row = full_marginal.loc[leg]
        lines.append(
            f"- {leg}：全段含该腿相对删除该腿复利变化"
            f"{float(row['with_vs_without_change']):+.2%}；"
            f"回撤{float(row['without_leg_max_drawdown']):.2%} → "
            f"{float(row['with_leg_max_drawdown']):.2%}。"
        )
    lines.extend(["", "## 发布判定", ""])
    for leg in ("D", "A", "E", "C", "N"):
        failed = gate_frame[(gate_frame["strategy_leg"].eq(leg)) & ~gate_frame["passed"]]["gate"].tolist()
        lines.append(f"- {leg} `{verdicts[leg]}`：不通过 {', '.join(failed)}。")
    lines.extend(
        [
            "",
            "当前发布认证已经失效，A/C/D/E/N新BUY均会fail-closed。"
            "本报告不阻断已有持仓的SELL、撤单和成交回写。",
            "",
        ]
    )
    (OUT / "strict_audit_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
