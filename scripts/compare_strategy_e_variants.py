"""同口径比较策略 E 的三个历史/当前版本。

本脚本只做研究回放，不修改实盘配置、不生成委托：

* ``E_JULY``：2026 年 7 月实盘使用的完整涨停池规则，旧 neutral 算法，
  成交可靠、非 ST，按流通市值最小取一只，T+2 退出；
* ``E_R1``：40 条 R1 规则各取第一名后合并，再取 neutral/最小流通市值，
  按命中规则 T+2/T+3 退出，不使用 13:30~14:30 入场门禁；
* ``E_CURRENT``：``E_R1`` 再执行当前 13:30~14:30 入场门禁，不回补第二名。

三套版本统一使用：82.5% 仓位、买卖各 0.1% 滑点、0.162% 往返费率、
T+1 开盘不可成交判断、跌停收盘顺延、单账户资金占用。组合回放固定使用当前
优先级 ``D > A > E > C``；D 为盘中腿，按时间顺序先于收盘后信号。

旧“62 笔/12.0283 倍”只作为不可执行参考展示，不进入正式排名，因为它使用了
未来成交过滤并允许资金重叠。

运行：
    python3 scripts/compare_strategy_e_variants.py

输出：
    reports/strategy_e_comparison/e_variant_summary.csv
    reports/strategy_e_comparison/e_standalone_daily.csv
    reports/strategy_e_comparison/e_standalone_trades.csv
    reports/strategy_e_comparison/portfolio_daily.csv
    reports/strategy_e_comparison/portfolio_trades.csv
    reports/strategy_e_comparison/e_yearly_summary.csv
    reports/strategy_e_comparison/portfolio_yearly_summary.csv
    reports/strategy_e_comparison/source_validation.csv
    reports/strategy_e_comparison/legacy_reference.csv
    reports/strategy_e_comparison/comparison_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import certify_current_executable_portfolio as portfolio  # noqa: E402
from scripts.verify_strategy_e_alignment import (  # noqa: E402
    load_historical_bucketed_pool,
)
from src.strategy_e import (  # noqa: E402
    apply_e_entry_gate,
    build_r1_universe_from_pool,
    load_e_spec,
    resolve_exit_offset,
    select_e_candidates,
)


START_DATE = "20240520"
END_DATE = "20260514"
POSITION_PCT = portfolio.POSITION_PCT
INITIAL_EQUITY = portfolio.INITIAL_EQUITY
BUY_SLIPPAGE_RATE = 0.001
SELL_SLIPPAGE_RATE = 0.001
ROUND_TRIP_FEE_RATE = 0.00162
EPSILON = 1e-9

HISTORICAL_SCORED_PATH = (
    PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
)
LEGACY_OLD_E_LOCK_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_e2_rerun"
    / "e2_rerun_live_universe_trades.csv"
)
LEGACY_R1_LOCK_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_e2_rerun"
    / "e2_r1_alignment_trades.csv"
)
LEGACY_CURRENT_LOCK_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_e2_rerun"
    / "e2_r1_entry_gate_trades.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_e_comparison"
SAMPLE_DIR = PROJECT_ROOT / "reports" / "strategy_e_samples"

VARIANT_DESCRIPTIONS = {
    "E_JULY": "7月实盘逻辑：完整池+旧neutral+成交可靠+最小流通市值+T+2",
    "E_R1": "40条R1并集+neutral+最小流通市值+混合T+2/T+3，无时间门禁",
    "E_CURRENT": "当前R1逻辑，再排除首次涨停13:30~14:30且不回补",
}


@dataclass(frozen=True)
class ExecutionResult:
    """一条 E 信号在统一成交模型下的结果。"""

    buy_date: str
    exit_date: str
    net_return: float
    status: str
    executable: bool


_DAILY_CACHE: dict[str, pd.DataFrame] = {}


def normalize_date(value: Any) -> str:
    return portfolio.normalize_date(value)


def is_true(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(str).str.lower().isin(
        {"true", "1", "1.0", "yes"}
    )


def classify_segment(ts_code: str) -> str:
    code = str(ts_code).upper().strip()
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix[:1] in {"4", "8", "9"}:
        return "bj"
    if prefix.startswith(("688", "689")):
        return "star"
    if prefix.startswith(("300", "301")):
        return "chi_next"
    if code.endswith(".SH") and prefix.startswith("6"):
        return "sh_main"
    if code.endswith(".SZ") and prefix.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "other"


def classify_old_retreat_state(current: float, prev1: float, prev2: float) -> str:
    """复现 8 月 3 日前实盘 E 的旧 neutral 算法。"""

    if any(pd.isna(value) for value in (current, prev1, prev2)):
        return "unknown"
    if current <= 3:
        return "weak_below_3"
    if current < prev1 < prev2:
        return "retreat_2day"
    if current < prev1 and current <= 5:
        return "retreat_weak"
    if current > prev1 > prev2:
        return "warming_2day"
    return "neutral"


def load_daily_price(trade_date: str) -> pd.DataFrame:
    if trade_date not in _DAILY_CACHE:
        path = portfolio.DAILY_PRICE_DIR / f"{trade_date}.csv"
        if not path.exists():
            raise FileNotFoundError(f"缺少日线文件：{path}")
        frame = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
        if frame["ts_code"].duplicated().any():
            frame = frame.drop_duplicates("ts_code", keep="last")
        _DAILY_CACHE[trade_date] = frame.set_index("ts_code")
    return _DAILY_CACHE[trade_date]


def daily_price_row(trade_date: str, ts_code: str) -> pd.Series:
    frame = load_daily_price(trade_date)
    if ts_code not in frame.index:
        raise KeyError(f"{trade_date}日线中找不到{ts_code}")
    row = frame.loc[ts_code]
    return row.iloc[-1] if isinstance(row, pd.DataFrame) else row


def optional_daily_price_row(trade_date: str, ts_code: str) -> pd.Series | None:
    """停牌/退市等情况下日线可能没有该股票，返回 None 交给成交模型处理。"""

    frame = load_daily_price(trade_date)
    if ts_code not in frame.index:
        return None
    row = frame.loc[ts_code]
    return row.iloc[-1] if isinstance(row, pd.DataFrame) else row


def limit_pct(ts_code: str) -> float:
    code = str(ts_code).upper()
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix[:1] in {"4", "8", "9"}:
        return 0.30
    if prefix.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def close_is_limit_down(row: pd.Series, ts_code: str) -> bool:
    close = portfolio.to_float(row.get("close"))
    pre_close = portfolio.to_float(row.get("pre_close"))
    pct_chg = portfolio.to_float(row.get("pct_chg"), float("nan"))
    if close <= 0 or pre_close <= 0:
        raise ValueError(f"{ts_code}日线收盘价或昨收无效")
    floor = round(pre_close * (1.0 - limit_pct(ts_code)), 2)
    threshold = -(limit_pct(ts_code) * 100.0 - 0.5)
    return close <= floor + 0.011 and (pd.isna(pct_chg) or pct_chg <= threshold)


def simulate_e_execution(
    sources: portfolio.Sources,
    signal_date: str,
    ts_code: str,
    exit_offset: int,
) -> ExecutionResult:
    """按统一且已由旧 R3/当前 R1 锁定明细验证的成交模型计算一笔。"""

    buy_date = portfolio.nth_trade_date(sources, signal_date, 1)
    buy_row = optional_daily_price_row(buy_date, ts_code)
    if buy_row is None:
        return ExecutionResult(
            buy_date=buy_date,
            exit_date="",
            net_return=0.0,
            status="buy_day_missing_or_suspended",
            executable=False,
        )
    buy_open = portfolio.to_float(buy_row.get("open"))
    buy_high = portfolio.to_float(buy_row.get("high"))
    buy_pre_close = portfolio.to_float(buy_row.get("pre_close"))
    if buy_open <= 0 or buy_high <= 0 or buy_pre_close <= 0:
        raise ValueError(f"{buy_date} {ts_code}买入行情无效")

    buy_price = buy_open * (1.0 + BUY_SLIPPAGE_RATE)
    # 复现项目锁定成交模型：T+1开盘接近当日涨停价时视为买不到。普通的“开盘
    # 等于全天最高价”仍按开盘滑点成交；该近似虽偏理想化，但必须与三套历史标尺
    # 保持一致，真实竞价/POV偏差在报告中另列风险。
    open_gap = buy_open / buy_pre_close - 1.0
    if (
        open_gap >= limit_pct(ts_code) - 0.005
        and buy_price > buy_high + EPSILON
    ):
        return ExecutionResult(
            buy_date=buy_date,
            exit_date="",
            net_return=0.0,
            status="open_limit_up_unbuyable",
            executable=False,
        )

    planned_exit = portfolio.nth_trade_date(sources, signal_date, exit_offset)
    exit_date = planned_exit
    while True:
        exit_row = optional_daily_price_row(exit_date, ts_code)
        if exit_row is None:
            exit_date = portfolio.nth_trade_date(sources, exit_date, 1)
            continue
        if not close_is_limit_down(exit_row, ts_code):
            break
        exit_date = portfolio.nth_trade_date(sources, exit_date, 1)

    exit_close = portfolio.to_float(exit_row.get("close"))
    exit_price = exit_close * (1.0 - SELL_SLIPPAGE_RATE)
    net_return = exit_price / buy_price - 1.0 - ROUND_TRIP_FEE_RATE
    actual_offset = sources.trade_date_index[exit_date] - sources.trade_date_index[signal_date]
    status = "filled" if exit_date == planned_exit else f"delayed_exit_d{actual_offset}"
    return ExecutionResult(
        buy_date=buy_date,
        exit_date=exit_date,
        net_return=net_return,
        status=status,
        executable=True,
    )


def load_old_segment_counts(trade_dates: list[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for trade_date in trade_dates:
        path = PROJECT_ROOT / "data" / "raw" / "limit_list" / f"{trade_date}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype=str, low_memory=False)
        if "limit" not in frame or "ts_code" not in frame:
            continue
        upper = frame[frame["limit"].astype(str).eq("U")]
        counts[trade_date] = (
            upper["ts_code"]
            .dropna()
            .astype(str)
            .map(classify_segment)
            .value_counts()
            .astype(int)
            .to_dict()
        )
    return counts


def build_e_july_daily_picks(sources: portfolio.Sources) -> pd.DataFrame:
    """从历史完整打分池逐日重建 8 月 3 日前的 E 选股。"""

    if not HISTORICAL_SCORED_PATH.exists():
        raise FileNotFoundError(f"缺少历史成交打分池：{HISTORICAL_SCORED_PATH}")
    frame = pd.read_csv(
        HISTORICAL_SCORED_PATH,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    frame["trade_date"] = frame["trade_date"].map(normalize_date)
    frame = frame[
        frame["trade_date"].between(START_DATE, END_DATE, inclusive="both")
    ].copy()
    required = {
        "limit_data_quality",
        "strategy_compatible",
        "is_st",
        "allow_buy_reliable",
        "is_fill_score_reliable",
        "circ_mv",
        "market_segment",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("旧 E 历史池缺少字段：" + "、".join(missing))

    frame = frame[frame["limit_data_quality"].fillna("").astype(str).eq("full")]
    frame = frame[is_true(frame["strategy_compatible"])]
    frame = frame[~is_true(frame["is_st"])]
    frame = frame[is_true(frame["allow_buy_reliable"])]
    frame = frame[is_true(frame["is_fill_score_reliable"])]
    frame["circ_mv"] = pd.to_numeric(frame["circ_mv"], errors="coerce")
    frame = frame[frame["circ_mv"].notna()].copy()

    counts = load_old_segment_counts(sources.trade_dates)
    picks: list[pd.Series] = []
    for trade_date, day_rows in frame.groupby("trade_date", sort=True):
        index = sources.trade_date_index.get(trade_date, -1)
        if index < 2:
            continue
        prev1_date = sources.trade_dates[index - 1]
        prev2_date = sources.trade_dates[index - 2]
        current = counts.get(trade_date, {})
        prev1 = counts.get(prev1_date, {})
        prev2 = counts.get(prev2_date, {})
        segments = set(current) | set(prev1) | set(prev2)
        states = {
            segment: classify_old_retreat_state(
                float(current.get(segment, float("nan"))),
                float(prev1.get(segment, float("nan"))),
                float(prev2.get(segment, float("nan"))),
            )
            for segment in segments
        }
        eligible = day_rows[
            day_rows["market_segment"]
            .astype(str)
            .map(lambda segment: states.get(segment, "unknown"))
            .eq("neutral")
        ].copy()
        if eligible.empty:
            continue
        selected = eligible.sort_values(["circ_mv", "ts_code"]).iloc[0].copy()
        selected["exit_rule"] = "fixed_t2_close"
        selected["exit_offset"] = 2
        picks.append(selected)
    if not picks:
        raise RuntimeError("旧 E 没有生成任何逐日候选")
    return pd.DataFrame(picks).sort_values("trade_date").reset_index(drop=True)


def build_r1_daily_picks() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """用当前实盘共用规则源构造 R1 门禁前/后的完整逐日第一名。"""

    spec = load_e_spec(PROJECT_ROOT)
    pool = load_historical_bucketed_pool(START_DATE, END_DATE, 80)
    universe = build_r1_universe_from_pool(pool, spec, audit_readiness=True)
    ranked = select_e_candidates(universe)
    pre_gate = ranked.groupby("trade_date", as_index=False).head(1).copy()
    current = apply_e_entry_gate(pre_gate, spec)
    for frame in (pre_gate, current):
        frame["exit_offset"] = frame["exit_rule"].map(
            lambda value: resolve_exit_offset(spec, str(value))
        )
    return (
        pre_gate.sort_values("trade_date").reset_index(drop=True),
        current.sort_values("trade_date").reset_index(drop=True),
        spec,
    )


def attach_execution(
    sources: portfolio.Sources,
    picks: pd.DataFrame,
    variant: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, candidate in picks.iterrows():
        signal_date = normalize_date(candidate.get("trade_date"))
        ts_code = str(candidate.get("ts_code", ""))
        result = simulate_e_execution(
            sources,
            signal_date,
            ts_code,
            int(candidate.get("exit_offset", 2)),
        )
        rows.append(
            {
                "strategy_family": "E",
                "strategy_variant": variant,
                "e_variant": variant,
                "signal_date": signal_date,
                "ts_code": ts_code,
                "name": str(candidate.get("name", "")),
                "market_segment": str(candidate.get("market_segment", "")),
                "circ_mv": portfolio.to_float(candidate.get("circ_mv"), float("nan")),
                "scenario_rank": candidate.get("scenario_rank", ""),
                "scenario": str(candidate.get("scenario", "")),
                "exit_rule": str(candidate.get("exit_rule", "")),
                "exit_offset": int(candidate.get("exit_offset", 2)),
                "buy_date": result.buy_date,
                "exit_date": result.exit_date,
                "net_return": result.net_return,
                "account_return": result.net_return * POSITION_PCT,
                "execution_status": result.status,
                "executable": result.executable,
            }
        )
    result = pd.DataFrame(rows)
    if result["signal_date"].duplicated().any():
        raise ValueError(f"{variant}逐日第一名存在重复信号日")
    return result.sort_values("signal_date").reset_index(drop=True)


def validate_reference_subset(
    actual: pd.DataFrame,
    locked_path: Path,
    label: str,
    *,
    compare_exit_rule: bool,
) -> dict[str, Any]:
    locked = pd.read_csv(
        locked_path,
        dtype={
            "trade_date": str,
            "ts_code": str,
            "buy_date": str,
            "exit_date": str,
        },
        low_memory=False,
    )
    locked["trade_date"] = locked["trade_date"].map(normalize_date)
    joined = locked.merge(
        actual,
        left_on="trade_date",
        right_on="signal_date",
        how="left",
        suffixes=("_locked", "_actual"),
    )
    same_stock = joined["ts_code_locked"].eq(joined["ts_code_actual"])
    same_exit_rule = (
        joined["exit_rule_locked"].astype(str).eq(joined["exit_rule_actual"].astype(str))
        if compare_exit_rule
        else pd.Series(True, index=joined.index)
    )
    same_buy = joined["buy_date_locked"].map(normalize_date).eq(joined["buy_date_actual"])
    locked_exit = joined["exit_date_locked"].map(normalize_date)
    same_exit = locked_exit.eq(joined["exit_date_actual"])
    locked_status_column = "status_locked" if "status_locked" in joined else "status"
    locked_unbuyable = joined[locked_status_column].astype(str).eq(
        "open_limit_up_unbuyable"
    )
    same_exit = same_exit | (locked_unbuyable & joined["exit_date_actual"].eq(""))
    return_diff = (
        pd.to_numeric(joined["net_return_locked"], errors="raise")
        - pd.to_numeric(joined["net_return_actual"], errors="raise")
    ).abs()
    same_status = joined[locked_status_column].astype(str).eq(
        joined["execution_status"].astype(str)
    )
    passed = bool(
        same_stock.all()
        and same_exit_rule.all()
        and same_buy.all()
        and same_exit.all()
        and (return_diff <= 1e-9).all()
        and same_status.all()
    )
    if not passed:
        bad = joined[
            ~(same_stock & same_exit_rule & same_buy & same_exit & same_status)
            | return_diff.gt(1e-9)
        ]
        raise RuntimeError(f"{label}来源验证失败：\n{bad.head(20).to_string(index=False)}")
    return {
        "validation": label,
        "locked_count": int(len(locked)),
        "actual_candidate_day_count": int(len(actual)),
        "locked_coverage_of_daily_candidates": float(len(locked) / len(actual)),
        "same_stock_count": int(same_stock.sum()),
        "same_exit_rule_count": int(same_exit_rule.sum()),
        "same_execution_count": int(
            (same_buy & same_exit & same_status & return_diff.le(1e-9)).sum()
        ),
        "passed": passed,
    }


def validate_published_portfolio_regression(
    sources: portfolio.Sources,
    current_candidates: pd.DataFrame,
) -> dict[str, Any]:
    """完整当前 E 候选必须复现正式五策略发布标尺。"""

    detail = replay_full_portfolio(
        sources,
        current_candidates,
        "E_CURRENT_PORTFOLIO_REGRESSION",
    )
    actual_count = int(detail["status"].eq("EXECUTED").sum())
    actual_multiple = float(detail["equity_after"].iloc[-1] / INITIAL_EQUITY)
    expected_count = portfolio.EXPECTED_CURRENT_TRADE_COUNT
    expected_multiple = portfolio.EXPECTED_CURRENT_MULTIPLE
    passed = bool(
        actual_count == expected_count
        and abs(actual_multiple - expected_multiple) <= 1e-9
    )
    if not passed:
        raise RuntimeError(
            "完整组合回归失败："
            f"交易数{actual_count}/{expected_count}，"
            f"复利{actual_multiple}/{expected_multiple}"
        )
    return {
        "validation": "完整E_CURRENT候选复现当前五策略发布组合",
        "locked_count": int(len(current_candidates)),
        "actual_candidate_day_count": int(len(current_candidates)),
        "locked_coverage_of_daily_candidates": 1.0,
        "same_stock_count": int(len(current_candidates)),
        "same_exit_rule_count": int(len(current_candidates)),
        "same_execution_count": actual_count,
        "reference_portfolio_multiple": expected_multiple,
        "actual_portfolio_multiple": actual_multiple,
        "passed": passed,
    }


def replay_e_standalone(
    sources: portfolio.Sources,
    candidates: pd.DataFrame,
    variant: str,
) -> pd.DataFrame:
    candidate_map = candidates.set_index("signal_date").to_dict("index")
    equity = INITIAL_EQUITY
    occupied_until = ""
    occupied_code = ""
    rows: list[dict[str, Any]] = []
    for signal_date in sources.baseline["date"].astype(str):
        equity_before = equity
        if occupied_until and signal_date < occupied_until:
            rows.append(
                {
                    "e_variant": variant,
                    "signal_date": signal_date,
                    "status": "SKIP_OCCUPIED",
                    "strategy_leg": "",
                    "ts_code": "",
                    "name": "",
                    "buy_date": "",
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_code": occupied_code,
                    "blocked_until": occupied_until,
                }
            )
            continue
        occupied_until = occupied_code = ""
        selected = candidate_map.get(signal_date)
        if selected is None:
            status = "NO_CANDIDATE"
            account_return = 0.0
        elif not bool(selected["executable"]):
            status = "E_ORDER_UNBUYABLE"
            account_return = 0.0
        else:
            status = "EXECUTED"
            account_return = float(selected["account_return"])
            equity *= 1.0 + account_return
            occupied_until = str(selected["exit_date"])
            occupied_code = str(selected["ts_code"])

        rows.append(
            {
                "e_variant": variant,
                "signal_date": signal_date,
                "status": status,
                "strategy_leg": "E" if selected is not None else "",
                "ts_code": str(selected["ts_code"]) if selected is not None else "",
                "name": str(selected["name"]) if selected is not None else "",
                "buy_date": str(selected["buy_date"]) if selected is not None else "",
                "exit_date": str(selected["exit_date"]) if selected is not None else "",
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
                "blocked_by_code": "",
                "blocked_until": "",
            }
        )
    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    return detail


def pick_current_priority(
    sources: portfolio.Sources,
    signal_date: str,
    e_candidates: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """按当前收盘后腿序 A>E>C 选股；D 在 replay 中先处理。"""

    ac = sources.ac_daily.get(signal_date)
    if ac is not None and str(ac.get("strategy_leg", "")) == "A":
        return dict(ac)
    e_pick = e_candidates.get(signal_date)
    if e_pick is not None:
        return {
            "strategy_leg": "E",
            "ts_code": e_pick["ts_code"],
            "name": e_pick["name"],
            "buy_date": e_pick["buy_date"],
            "exit_date": e_pick["exit_date"],
            "account_return": e_pick["account_return"],
            "executable": e_pick["executable"],
            "return_source": e_pick["execution_status"],
        }
    if ac is not None and str(ac.get("strategy_leg", "")) == "C":
        return dict(ac)
    return None


def replay_full_portfolio(
    sources: portfolio.Sources,
    candidates: pd.DataFrame,
    variant: str,
) -> pd.DataFrame:
    """按当前优先级和资金释放逻辑，把指定 E 版本放回完整组合。"""

    e_candidates = candidates.set_index("signal_date").to_dict("index")
    equity = INITIAL_EQUITY
    peak_equity = INITIAL_EQUITY
    occupied_until = ""
    occupied_leg = ""
    occupied_code = ""
    rows: list[dict[str, Any]] = []

    for _, baseline_row in sources.baseline.iterrows():
        signal_date = str(baseline_row["date"])
        equity_before = equity
        if occupied_until and signal_date < occupied_until:
            rows.append(
                {
                    "e_variant": variant,
                    "signal_date": signal_date,
                    "status": "SKIP_OCCUPIED",
                    "strategy_leg": "",
                    "ts_code": "",
                    "name": "",
                    "buy_date": "",
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_leg": occupied_leg,
                    "blocked_by_code": occupied_code,
                    "blocked_until": occupied_until,
                    "return_source": "",
                }
            )
            continue

        blocking_handoff = (
            bool(occupied_until)
            and signal_date == occupied_until
            and not portfolio.hit_limit_up(signal_date, occupied_code)
        )
        occupied_until = occupied_leg = occupied_code = ""
        # D 是盘中独立扫描腿。是否触发必须取完整逐日 D 候选表，不能再用
        # 旧 A/B/C 组合回放里的 d_return 标记；该标记已经提前删除了被旧组合
        # 占仓的 D 日期，会把所有 E 版本的完整组合复利共同高估。
        if signal_date in sources.strategy_d.index and not blocking_handoff:
            selected = portfolio.d_t2_candidate(sources, signal_date)
        else:
            selected = pick_current_priority(
                sources,
                signal_date,
                e_candidates,
            )

        if selected is None:
            rows.append(
                {
                    "e_variant": variant,
                    "signal_date": signal_date,
                    "status": "NO_CANDIDATE",
                    "strategy_leg": "",
                    "ts_code": "",
                    "name": "",
                    "buy_date": "",
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_leg": "",
                    "blocked_by_code": "",
                    "blocked_until": "",
                    "return_source": "",
                }
            )
            continue

        selected_leg = str(selected.get("strategy_leg", ""))
        if selected_leg == "E" and not bool(selected.get("executable", True)):
            # E 当天已经压过 C；T+1 开盘确认买不到后释放资金，不回补昨日的 C。
            rows.append(
                {
                    "e_variant": variant,
                    "signal_date": signal_date,
                    "status": "E_ORDER_UNBUYABLE",
                    "strategy_leg": "E",
                    "ts_code": str(selected.get("ts_code", "")),
                    "name": str(selected.get("name", "")),
                    "buy_date": str(selected.get("buy_date", "")),
                    "exit_date": "",
                    "account_return": 0.0,
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_leg": "",
                    "blocked_by_code": "",
                    "blocked_until": "",
                    "return_source": str(selected.get("return_source", "")),
                }
            )
            continue

        exit_date = normalize_date(selected.get("exit_date"))
        account_return = portfolio.to_float(selected.get("account_return"))
        if not exit_date:
            raise ValueError(f"{signal_date} {selected_leg}缺少退出日")
        if account_return <= -1.0:
            raise ValueError(f"{signal_date}账户收益不允许小于等于-100%")
        equity *= 1.0 + account_return
        peak_equity = max(peak_equity, equity)
        occupied_until = exit_date
        occupied_leg = selected_leg
        occupied_code = str(selected.get("ts_code", ""))
        rows.append(
            {
                "e_variant": variant,
                "signal_date": signal_date,
                "status": "EXECUTED",
                "strategy_leg": selected_leg,
                "ts_code": occupied_code,
                "name": str(selected.get("name", "")),
                "buy_date": str(selected.get("buy_date", "")),
                "exit_date": exit_date,
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
                "blocked_by_leg": "",
                "blocked_by_code": "",
                "blocked_until": "",
                "return_source": str(selected.get("return_source", "")),
            }
        )

    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    return detail


def max_consecutive_losses(returns: pd.Series) -> int:
    return portfolio.max_consecutive_losses(returns)


def trade_stats(detail: pd.DataFrame, *, strategy_leg: str | None = None) -> dict[str, Any]:
    trades = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    if strategy_leg is not None:
        trades = trades[trades["strategy_leg"].astype(str).eq(strategy_leg)]
    returns = pd.to_numeric(trades["account_return"], errors="raise")
    if returns.empty:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
        }
    curve = (1.0 + returns).cumprod()
    drawdown = curve / curve.cummax() - 1.0
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    return {
        "trade_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "equity_multiple": float(curve.iloc[-1]),
        "max_drawdown": float(drawdown.min()),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "profit_loss_ratio": (
            float(wins.mean() / abs(losses.mean()))
            if not wins.empty and not losses.empty
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def summarize_variant(
    variant: str,
    candidates: pd.DataFrame,
    standalone: pd.DataFrame,
    combined: pd.DataFrame,
) -> dict[str, Any]:
    e_stats = trade_stats(standalone)
    combo_stats = trade_stats(combined)
    combo_e_stats = trade_stats(combined, strategy_leg="E")
    combo_trades = combined[combined["status"].eq("EXECUTED")]
    legs = combo_trades["strategy_leg"].astype(str)
    combo_multiple = float(combined["equity_after"].iloc[-1] / INITIAL_EQUITY)
    return {
        "strategy_family": "E",
        "e_variant": variant,
        "description": VARIANT_DESCRIPTIONS[variant],
        "candidate_day_count": int(len(candidates)),
        "candidate_unbuyable_count": int((~candidates["executable"]).sum()),
        "standalone_attempt_count": int(
            standalone["status"].isin({"EXECUTED", "E_ORDER_UNBUYABLE"}).sum()
        ),
        "standalone_trade_count": e_stats["trade_count"],
        "standalone_win_rate": e_stats["win_rate"],
        "standalone_avg_return": e_stats["avg_return"],
        "standalone_median_return": e_stats["median_return"],
        "standalone_equity_multiple": e_stats["equity_multiple"],
        "standalone_max_drawdown": float(standalone["drawdown"].min()),
        "standalone_max_profit": e_stats["max_profit"],
        "standalone_max_loss": e_stats["max_loss"],
        "standalone_profit_loss_ratio": e_stats["profit_loss_ratio"],
        "standalone_max_consecutive_losses": e_stats["max_consecutive_losses"],
        "portfolio_trade_count": combo_stats["trade_count"],
        "portfolio_a_trade_count": int(legs.eq("A").sum()),
        "portfolio_c_trade_count": int(legs.eq("C").sum()),
        "portfolio_d_trade_count": int(legs.eq("D").sum()),
        "portfolio_e_trade_count": combo_e_stats["trade_count"],
        "portfolio_e_unbuyable_count": int(
            combined["status"].eq("E_ORDER_UNBUYABLE").sum()
        ),
        "portfolio_win_rate": combo_stats["win_rate"],
        "portfolio_avg_return": combo_stats["avg_return"],
        "portfolio_median_return": combo_stats["median_return"],
        "portfolio_equity_multiple": combo_multiple,
        "portfolio_max_drawdown": float(combined["drawdown"].min()),
        "portfolio_max_profit": combo_stats["max_profit"],
        "portfolio_max_loss": combo_stats["max_loss"],
        "portfolio_profit_loss_ratio": combo_stats["profit_loss_ratio"],
        "portfolio_max_consecutive_losses": combo_stats["max_consecutive_losses"],
        "capacity_certified": False,
    }


def yearly_summary(detail: pd.DataFrame, *, e_only: bool) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (variant, year), group in detail.groupby(
        ["e_variant", detail["signal_date"].astype(str).str[:4]], sort=False
    ):
        stats = trade_stats(group)
        rows.append(
            {
                "e_variant": variant,
                "year": year,
                "trade_count": stats["trade_count"],
                "win_rate": stats["win_rate"],
                "avg_return": stats["avg_return"],
                "median_return": stats["median_return"],
                "equity_multiple": stats["equity_multiple"],
                "max_drawdown": stats["max_drawdown"],
                "e_trade_count": (
                    stats["trade_count"]
                    if e_only
                    else int(
                        group[
                            group["status"].eq("EXECUTED")
                            & group["strategy_leg"].eq("E")
                        ].shape[0]
                    )
                ),
            }
        )
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    view = frame.copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(
                lambda value: "" if pd.isna(value) else f"{float(value):.6f}"
            )
        else:
            view[column] = view[column].fillna("").astype(str)
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in view.astype(str).values)
    return "\n".join(lines)


def write_report(
    summary: pd.DataFrame,
    validation: pd.DataFrame,
    e_yearly: pd.DataFrame,
    portfolio_yearly: pd.DataFrame,
) -> None:
    display = summary[
        [
            "e_variant",
            "candidate_day_count",
            "standalone_trade_count",
            "standalone_win_rate",
            "standalone_avg_return",
            "standalone_median_return",
            "standalone_equity_multiple",
            "standalone_max_drawdown",
            "portfolio_trade_count",
            "portfolio_e_trade_count",
            "portfolio_equity_multiple",
            "portfolio_max_drawdown",
        ]
    ].copy()
    best_standalone = summary.sort_values("standalone_equity_multiple", ascending=False).iloc[0]
    best_portfolio = summary.sort_values("portfolio_equity_multiple", ascending=False).iloc[0]
    lines = [
        "# 策略 E 三版本同口径对比",
        "",
        "## 结论",
        "",
        f"- E 单腿历史复利最高：`{best_standalone['e_variant']}`，"
        f"{best_standalone['standalone_equity_multiple']:.6f}倍。",
        f"- 完整组合历史复利最高：`{best_portfolio['e_variant']}`，"
        f"{best_portfolio['portfolio_equity_multiple']:.6f}倍。",
        "- 上述排名只描述同一历史窗口，不代表未来收益；R1 与时间门禁均来自同一窗口优化，存在明显过拟合风险。",
        "- 组合机械复利已经远超已认证容量，不能据此放大实盘资金。",
        "",
        "## 统一命名",
        "",
        "三套规则统一归属 `strategy_family=E`，用 `e_variant` 保留可追溯版本：",
        "",
        *[f"- `{name}`：{description}" for name, description in VARIANT_DESCRIPTIONS.items()],
        "",
        "实盘状态、历史持仓和旧日志暂时仍保留兼容标识 `E`。本报告没有改动实盘下单路由，",
        "避免把已有持仓识别、退出和风控链路同时打断。确定最终版本后再做一次独立的兼容迁移。",
        "",
        "## 正式同口径结果",
        "",
        markdown_table(display),
        "",
        "## 为什么旧 12.028 倍不进入排名",
        "",
        "旧文档的62笔/12.0283倍使用80%仓位，并在信号日用未来的 `buy_executed`/`sell_executed`",
        "过滤候选，同时存在同一账户资金重叠。它不是一套可在当时逐日执行的策略。正式比较用",
        "`E_R1` 表示其可执行含义：40条R1规则、无前视、单账户、无时间门禁。",
        "",
        "## 统一回放标准",
        "",
        f"- 窗口：{START_DATE}~{END_DATE}，初始资金{INITIAL_EQUITY:,.0f}元。",
        f"- 仓位：{POSITION_PCT:.1%}；买卖滑点各{BUY_SLIPPAGE_RATE:.1%}；往返费率{ROUND_TRIP_FEE_RATE:.3%}。",
        "- T+1开盘接近涨停且理论滑点价超过全天最高价时判定买不到；退出日收盘跌停则顺延。",
        "- E单腿严格单账户；完整组合严格按 `D>A>E>C` 串行，所有版本只替换E。",
        "- D是盘中开仓腿，先于收盘后A/E/C；这属于时序，不使用未来信号重排。",
        "",
        "## 来源复现验证",
        "",
        markdown_table(validation),
        "",
        "旧 E 的151行锁定回放、R1的50行锁定明细和当前门禁后的43行锁定明细，均逐票、",
        "逐买卖日、逐收益复现；当前82个完整候选日放回五策略后也精确复现发布标尺。",
        "正式回放使用完整逐日候选，不再把50/43行历史成交名单误当成完整候选池。",
        "",
        "## E单腿分年",
        "",
        markdown_table(e_yearly),
        "",
        "## 完整组合分年",
        "",
        markdown_table(portfolio_yearly),
        "",
        "## 风险说明",
        "",
        "- `E_JULY` 的7月盈利样本很少且集中，不能只因7月表现回滚。",
        "- `E_R1` 的40条规则和 `E_CURRENT` 的时间门禁都来自同一历史窗口，不是真正样本外。",
        "- 本报告使用日线成交近似；真实集合竞价、POV、委托拒绝、容量和滑点仍可能偏离。",
        "- 在新的样本外/模拟盘证据出现前，不建议根据机械复利直接扩大资金；若切换版本，应先小资金验证。",
    ]
    (OUTPUT_DIR / "comparison_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_canonical_sample(
    picks: pd.DataFrame,
    execution: pd.DataFrame,
    variant: str,
) -> Path:
    """写出可供后续认证直接读取的完整逐日候选样本。"""

    sample = picks.copy()
    sample["trade_date"] = sample["trade_date"].map(normalize_date)
    result_columns = [
        "signal_date",
        "ts_code",
        "buy_date",
        "exit_date",
        "net_return",
        "account_return",
        "execution_status",
        "executable",
    ]
    results = execution[result_columns].rename(columns={"signal_date": "trade_date"})
    overlapping = [
        column
        for column in results.columns
        if column in sample.columns and column not in {"trade_date", "ts_code"}
    ]
    sample = sample.drop(columns=overlapping, errors="ignore").merge(
        results,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    sample.insert(0, "sample_schema_version", 1)
    sample.insert(1, "strategy_family", "E")
    sample.insert(2, "strategy_leg", "E")
    sample.insert(3, "strategy_variant", variant)
    sample.insert(4, "sample_scope", "COMPLETE_DAILY_CANDIDATES")
    sample["status"] = sample["execution_status"]

    # Windows ARM与Mac在少数派生展示特征上可能相差最后1个二进制浮点位。
    # 这些列不参与已完成的选股/成交/收益计算，统一为15位有效数字以形成真正
    # 跨架构的规范样本；净收益和账户收益保留原始精度供组合认证使用。
    exact_numeric_columns = {"net_return", "account_return"}
    for column in sample.select_dtypes(include="number").columns:
        if column in exact_numeric_columns:
            continue
        sample[column] = sample[column].map(
            lambda value: "" if pd.isna(value) else format(float(value), ".15g")
        )

    path = SAMPLE_DIR / f"{variant.lower()}_daily_candidates_full.csv"
    # 认证样本必须能在 Windows/Mac 以及不同 pandas 版本间逐字节复现。
    # 显式固定换行和浮点格式，避免同一数值因平台默认 CSV 排版不同而误报输入漂移。
    sample.to_csv(
        path,
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
        float_format="%.17g",
    )
    return path


def main() -> None:
    started_at = time.perf_counter()

    def progress(message: str) -> None:
        elapsed = time.perf_counter() - started_at
        print(f"[策略E回放 {elapsed:6.1f}s] {message}", flush=True)

    progress("开始：加载当前组合认证输入（全程只读，不提交委托）")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    sources = portfolio.load_sources()

    progress("构建 E_JULY 完整候选")
    july_picks = build_e_july_daily_picks(sources)
    progress("构建 E_R1 与 E_CURRENT 完整候选")
    r1_picks, current_picks, _spec = build_r1_daily_picks()
    source_picks = {
        "E_JULY": july_picks,
        "E_R1": r1_picks,
        "E_CURRENT": current_picks,
    }
    candidate_frames: dict[str, pd.DataFrame] = {}
    for variant, picks in source_picks.items():
        progress(f"计算 {variant} 的逐票可成交收益")
        candidate_frames[variant] = attach_execution(sources, picks, variant)

    progress("核对三版本历史锁定子集")
    validation_rows = [
            validate_reference_subset(
                candidate_frames["E_JULY"],
                LEGACY_OLD_E_LOCK_PATH,
                "E_JULY锁定R3明细",
                compare_exit_rule=False,
            ),
            validate_reference_subset(
                candidate_frames["E_R1"],
                LEGACY_R1_LOCK_PATH,
                "E_R1门禁前锁定明细",
                compare_exit_rule=True,
            ),
            validate_reference_subset(
                candidate_frames["E_CURRENT"],
                LEGACY_CURRENT_LOCK_PATH,
                "E_CURRENT门禁后锁定明细",
                compare_exit_rule=True,
            ),
        ]
    validation_rows.append(
        validate_published_portfolio_regression(
            sources,
            candidate_frames["E_CURRENT"],
        )
    )
    validation = pd.DataFrame(validation_rows)

    standalone_frames: list[pd.DataFrame] = []
    portfolio_frames: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    for variant, candidates in candidate_frames.items():
        progress(f"回放 {variant} 单策略与完整组合")
        standalone = replay_e_standalone(sources, candidates, variant)
        combined = replay_full_portfolio(sources, candidates, variant)
        standalone_frames.append(standalone)
        portfolio_frames.append(combined)
        summaries.append(summarize_variant(variant, candidates, standalone, combined))

    all_candidates = pd.concat(candidate_frames.values(), ignore_index=True)
    all_standalone = pd.concat(standalone_frames, ignore_index=True)
    all_portfolio = pd.concat(portfolio_frames, ignore_index=True)
    summary = pd.DataFrame(summaries)
    e_yearly = yearly_summary(all_standalone, e_only=True)
    portfolio_yearly = yearly_summary(all_portfolio, e_only=False)

    legacy_reference = pd.DataFrame(
        [
            {
                "reference": "E_DOC62_LEGACY_REFERENCE",
                "trade_count": 62,
                "position_pct": 0.80,
                "avg_account_return": 0.044086,
                "median_account_return": 0.017896,
                "win_rate": 0.6452,
                "equity_multiple": 12.0283,
                "max_drawdown": -0.1644,
                "executable": False,
                "exclusion_reason": "未来成交过滤+单账户资金重叠，不进入正式排名",
            }
        ]
    )

    progress("写入跨平台稳定的三份规范样本")
    sample_paths = {
        variant: write_canonical_sample(source_picks[variant], candidates, variant)
        for variant, candidates in candidate_frames.items()
    }
    manifest = {
        "schema_version": 1,
        "strategy_family": "E",
        "active_strategy_variant": "E_CURRENT",
        "priority_order": ["D", "A", "E", "C"],
        "sample_window": {"start": START_DATE, "end": END_DATE},
        "samples": {
            variant: {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "candidate_day_count": int(len(candidate_frames[variant])),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for variant, path in sample_paths.items()
        },
        "legacy_reference_policy": (
            "旧E2命名文件只用于逐票回归验证，不得作为当前E完整候选样本。"
        ),
    }
    (SAMPLE_DIR / "sample_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary.to_csv(OUTPUT_DIR / "e_variant_summary.csv", index=False, encoding="utf-8-sig")
    all_candidates.to_csv(
        OUTPUT_DIR / "e_daily_candidates.csv", index=False, encoding="utf-8-sig"
    )
    all_standalone.to_csv(
        OUTPUT_DIR / "e_standalone_daily.csv", index=False, encoding="utf-8-sig"
    )
    all_standalone[all_standalone["status"].eq("EXECUTED")].to_csv(
        OUTPUT_DIR / "e_standalone_trades.csv", index=False, encoding="utf-8-sig"
    )
    all_portfolio.to_csv(
        OUTPUT_DIR / "portfolio_daily.csv", index=False, encoding="utf-8-sig"
    )
    all_portfolio[all_portfolio["status"].eq("EXECUTED")].to_csv(
        OUTPUT_DIR / "portfolio_trades.csv", index=False, encoding="utf-8-sig"
    )
    e_yearly.to_csv(
        OUTPUT_DIR / "e_yearly_summary.csv", index=False, encoding="utf-8-sig"
    )
    portfolio_yearly.to_csv(
        OUTPUT_DIR / "portfolio_yearly_summary.csv", index=False, encoding="utf-8-sig"
    )
    validation.to_csv(
        OUTPUT_DIR / "source_validation.csv", index=False, encoding="utf-8-sig"
    )
    legacy_reference.to_csv(
        OUTPUT_DIR / "legacy_reference.csv", index=False, encoding="utf-8-sig"
    )
    write_report(summary, validation, e_yearly, portfolio_yearly)

    progress("全部计算和报告写入完成")
    print("策略E三版本同口径回放完成")
    print(
        summary[
            [
                "e_variant",
                "candidate_day_count",
                "standalone_trade_count",
                "standalone_equity_multiple",
                "standalone_max_drawdown",
                "portfolio_trade_count",
                "portfolio_e_trade_count",
                "portfolio_equity_multiple",
                "portfolio_max_drawdown",
            ]
        ].to_string(index=False)
    )
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
