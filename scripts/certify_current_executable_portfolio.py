"""认证当前A/C/D/E2/L组合的可执行逐日资金曲线。

本脚本把此前散落的一次性计算固化成可重复基线，并比较E2门禁与L扩容前后：

1. B已删除；历史B日只保留按当前A→C重选后真实存在的A/C候选；
2. E2使用无前视、单账户R1明细，旧E2候选和旧收益不得混入；
3. 普通首仓按82.5%，旧策略仓未释放前不做尾盘衔接；
4. 保留D面对A/C/E2时的09:23先卖后买接力，D不为L接力；
5. L使用当前model=3基础条件和替换窄门；
6. E2入场门禁在每日第一名确定后执行，被排除时不回补第二名；
7. model=3的L基础环境新增全市场连板数3~8档，历史与实盘调用同一规则；
8. 所有收益按同一账户、同一时间顺序连乘，禁止把各腿复利直接相乘。

该报告仍是历史回放，不承诺未来收益。实盘还会受到涨停无法成交、POV追价上限、
滑点和容量约束影响；放大资金前必须继续小资金核验真实成交。

运行：
    python scripts/certify_current_executable_portfolio.py

输出：
    reports/current_portfolio_alignment/portfolio_summary.csv
    reports/current_portfolio_alignment/portfolio_trades.csv
    reports/current_portfolio_alignment/portfolio_daily.csv
    reports/current_portfolio_alignment/e2_entry_gate_validation.csv
    reports/current_portfolio_alignment/l_chain_expansion_validation.csv
    reports/current_portfolio_alignment/portfolio_report.md
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (  # noqa: E402
    build_l_lookup,
    l_trade_return,
    selected_l2_source,
)
from src.strategy_e2 import load_e2_spec  # noqa: E402
from src.strategy_model3_policy import (  # noqa: E402
    model3_l_base_rule_pass,
    model3_l_replace_guard_pass,
)


BASELINE_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_expansion"
    / "abcd_expansion_selected_e2_equity_curve.csv"
)
ABC_PATH = (
    PROJECT_ROOT
    / "reports"
    / "paper_trade"
    / "backup_strategy_c"
    / "current_config_c_exit_refine_exit5_20240520_20260514_481d_best_abc_detail.csv"
)
D_PATH = PROJECT_ROOT / "reports" / "strategy_d" / "d_trades.csv"
E2_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_e2_rerun"
    / "e2_r1_alignment_trades.csv"
)
NO_B_RESELECTION_PATH = (
    PROJECT_ROOT
    / "config"
    / "strategy_no_b_historical_reselection.csv"
)
TRADE_CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
DAILY_PRICE_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "current_portfolio_alignment"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"

INITIAL_EQUITY = 500_000.0
OLD_POSITION_PCT = 0.80
POSITION_PCT = 0.825
D_FILL_STRESS = 0.80
D_ROUND_TRIP_COST = 0.0015
EPSILON = 1e-12

# 先锁定门禁前基线，防止以后输入文件漂移后仍静默生成“更好”结果。
EXPECTED_BASE_TRADE_COUNT = 132
EXPECTED_BASE_MULTIPLE = 2884.052538490145
EXPECTED_E2_ONLY_TRADE_COUNT = 129
EXPECTED_E2_ONLY_MULTIPLE = 3254.1261014125575
EXPECTED_OPTIMIZED_TRADE_COUNT = 132
EXPECTED_OPTIMIZED_MULTIPLE = 4712.470092237913


@dataclass(frozen=True)
class Sources:
    """组合回放需要的全部只读来源。"""

    baseline: pd.DataFrame
    abc: pd.DataFrame
    strategy_d: pd.DataFrame
    e2: pd.DataFrame
    no_b_reselection: pd.DataFrame
    l_lookup: dict[str, pd.Series]
    model3_config: dict[str, Any]
    e2_spec: dict[str, Any]
    trade_dates: list[str]
    trade_date_index: dict[str, int]


def normalize_date(value: Any) -> str:
    """把日期统一成YYYYMMDD；空值返回空字符串。"""

    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def to_float(value: Any, default: float = 0.0) -> float:
    """安全转换浮点数。"""

    number = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(number) else float(number)


def to_bool(value: Any) -> bool:
    """兼容CSV中的布尔文本。"""

    return str(value).strip().lower() in {"true", "1", "yes"}


def load_sources() -> Sources:
    """加载并严格校验来源；缺失或日期漂移时直接失败。"""

    required = [
        BASELINE_PATH,
        ABC_PATH,
        D_PATH,
        E2_PATH,
        NO_B_RESELECTION_PATH,
        TRADE_CALENDAR_PATH,
        RUNTIME_CONFIG_PATH,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("组合认证缺少来源：" + "；".join(missing))

    baseline = pd.read_csv(BASELINE_PATH, dtype={"date": str}, low_memory=False)
    baseline["date"] = baseline["date"].map(normalize_date)
    baseline = baseline.sort_values("date").reset_index(drop=True)
    if baseline["date"].eq("").any() or baseline["date"].duplicated().any():
        raise ValueError("组合基准日期为空或重复")
    for column in ("abc_return", "d_return", "expansion_return"):
        baseline[column] = pd.to_numeric(
            baseline.get(column, 0.0), errors="coerce"
        ).fillna(0.0)

    abc = pd.read_csv(
        ABC_PATH,
        dtype={"signal_date": str, "buy_trade_date": str, "exit_trade_date": str},
        low_memory=False,
    )
    abc = abc[abc["scenario"].astype(str).eq("A_plus_B_plus_C_refined")].copy()
    abc["signal_date"] = abc["signal_date"].map(normalize_date)
    abc = abc.drop_duplicates("signal_date", keep="last").set_index("signal_date")

    strategy_d = pd.read_csv(D_PATH, dtype={"signal_date": str}, low_memory=False)
    strategy_d["signal_date"] = strategy_d["signal_date"].map(normalize_date)
    strategy_d = strategy_d.drop_duplicates("signal_date", keep="last").set_index(
        "signal_date"
    )

    e2 = pd.read_csv(E2_PATH, dtype={"trade_date": str}, low_memory=False)
    e2["trade_date"] = e2["trade_date"].map(normalize_date)
    if len(e2) != 50 or e2["trade_date"].duplicated().any():
        raise ValueError("E2门禁前锁定明细必须恰好50个唯一信号日")
    e2 = e2.set_index("trade_date")

    no_b = pd.read_csv(
        NO_B_RESELECTION_PATH,
        dtype={
            "signal_date": str,
            "ts_code": str,
            "buy_trade_date": str,
            "exit_trade_date": str,
        },
        low_memory=False,
    )
    no_b["signal_date"] = no_b["signal_date"].map(normalize_date)
    if no_b["signal_date"].eq("").any() or no_b["signal_date"].duplicated().any():
        raise ValueError("无B重选账本日期为空或重复")
    no_b = no_b.set_index("signal_date")
    old_b_dates = set(
        abc.loc[abc["strategy_leg"].astype(str).str.upper().eq("B")].index
    )
    if set(no_b.index) != old_b_dates:
        raise ValueError("无B重选账本没有逐日覆盖全部历史B日")

    calendar = pd.read_csv(TRADE_CALENDAR_PATH, dtype={"cal_date": str})
    trade_dates = sorted(
        calendar.loc[
            pd.to_numeric(calendar["is_open"], errors="coerce").eq(1),
            "cal_date",
        ]
        .map(normalize_date)
        .loc[lambda values: values.ne("")]
        .unique()
        .tolist()
    )
    trade_date_index = {date: index for index, date in enumerate(trade_dates)}

    l_source = selected_l2_source().copy()
    l_source["trade_date"] = l_source["trade_date"].map(normalize_date)
    l_source = l_source.drop_duplicates("trade_date", keep="last")
    runtime_config = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    model3_config = runtime_config.get("strategy_model3", {})
    if not isinstance(model3_config, dict):
        raise ValueError("config.json缺少有效strategy_model3配置")

    return Sources(
        baseline=baseline,
        abc=abc,
        strategy_d=strategy_d,
        e2=e2,
        no_b_reselection=no_b,
        l_lookup=build_l_lookup(l_source),
        model3_config=model3_config,
        e2_spec=load_e2_spec(PROJECT_ROOT),
        trade_dates=trade_dates,
        trade_date_index=trade_date_index,
    )


def nth_trade_date(sources: Sources, date: str, offset: int) -> str:
    """返回指定日期后的第offset个交易日。"""

    if date not in sources.trade_date_index:
        raise KeyError(f"交易日历中找不到{date}")
    target = sources.trade_date_index[date] + offset
    if target >= len(sources.trade_dates):
        raise IndexError(f"{date}之后缺少第{offset}个交易日")
    return sources.trade_dates[target]


def source_row(table: pd.DataFrame, date: str, source: str) -> pd.Series:
    """读取唯一来源行。"""

    if date not in table.index:
        raise KeyError(f"{source}来源中找不到{date}")
    row = table.loc[date]
    return row.iloc[-1] if isinstance(row, pd.DataFrame) else row


def infer_abc_release_date(baseline: pd.DataFrame, row_index: int) -> str:
    """从历史占仓状态推断A/C退出释放日。"""

    index = row_index + 1
    while (
        index < len(baseline)
        and str(baseline.loc[index, "operation_status"]) == "POSITION_OCCUPIED_SKIP"
    ):
        index += 1
    return str(baseline.loc[index, "date"]) if index < len(baseline) else "99991231"


def e2_entry_gate_passes(row: pd.Series, spec: dict[str, Any]) -> bool:
    """只用信号日字段执行配置化E2入场门禁。"""

    for column, values in spec.get("entry_gate", {}).get("exclude_values", {}).items():
        if column not in row.index:
            raise RuntimeError(f"E2锁定明细缺少入场门禁字段：{column}")
        if str(row.get(column, "")) in {str(value) for value in values}:
            return False
    return True


def no_b_candidate(sources: Sources, signal_date: str) -> dict[str, Any] | None:
    """历史B日只接受重新选出的A/C；旧E2重选结果全部废止。

    E2现在由新的50笔R1信号日集合独立判断。旧账本里的E2属于已废止候选口径，
    若继续使用会把前视旧E2重新混回当前组合。
    """

    row = source_row(sources.no_b_reselection, signal_date, "无B重选")
    leg = str(row.get("replacement_leg", "NONE")).upper()
    if leg not in {"A", "C"} or not to_bool(row.get("buy_executed")):
        return None
    exit_date = normalize_date(row.get("exit_trade_date"))
    if not exit_date:
        raise ValueError(f"{signal_date} 无B重选{leg}已成交但缺少退出日")
    return {
        "strategy_leg": leg,
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "buy_date": normalize_date(row.get("buy_trade_date")),
        "exit_date": exit_date,
        "account_return": to_float(row.get("return_at_80pct"))
        * POSITION_PCT
        / OLD_POSITION_PCT,
        "return_source": "原B日删除B后按当前A→C重选",
    }


def mode1_candidate(
    sources: Sources,
    row: pd.Series,
    row_index: int,
    *,
    entry_gate_enabled: bool,
) -> dict[str, Any] | None:
    """按A/C优先、无则当前E2生成收盘后候选。"""

    signal_date = str(row["date"])
    if abs(to_float(row.get("abc_return"))) > EPSILON:
        abc = source_row(sources.abc, signal_date, "A/C/B")
        leg = str(abc.get("strategy_leg", "")).upper()
        if leg == "B":
            replacement = no_b_candidate(sources, signal_date)
            if replacement is not None:
                return replacement
        elif leg in {"A", "C"}:
            buy_date = normalize_date(abc.get("buy_trade_date")) or nth_trade_date(
                sources, signal_date, 1
            )
            exit_date = normalize_date(abc.get("exit_trade_date")) or infer_abc_release_date(
                sources.baseline, row_index
            )
            return {
                "strategy_leg": leg,
                "ts_code": str(abc.get("ts_code", "")),
                "name": str(abc.get("name", "")),
                "buy_date": buy_date,
                "exit_date": exit_date,
                "account_return": to_float(abc.get("account_return"))
                * POSITION_PCT
                / OLD_POSITION_PCT,
                "return_source": str(abc.get("return_source", "A/C历史执行收益")),
            }

    if signal_date not in sources.e2.index:
        return None
    e2 = source_row(sources.e2, signal_date, "E2 R1")
    if entry_gate_enabled and not e2_entry_gate_passes(e2, sources.e2_spec):
        return None
    return {
        "strategy_leg": "E2",
        "ts_code": str(e2.get("ts_code", "")),
        "name": str(e2.get("name", "")),
        "buy_date": normalize_date(e2.get("buy_date")),
        "exit_date": normalize_date(e2.get("exit_date")),
        "account_return": to_float(e2.get("net_return")) * POSITION_PCT,
        "return_source": (
            f"E2_R1:{e2.get('scenario_rank', '')};"
            f"first_time={e2.get('first_time_detail_bucket', '')}"
        ),
    }


def l_base_passes(
    sources: Sources,
    row: pd.Series | None,
    *,
    chain_3_8_enabled: bool,
) -> bool:
    """调用实盘共用规则，并允许认证脚本复现扩容前旧口径。"""

    if row is None:
        return False
    model3_config = copy.deepcopy(sources.model3_config)
    chain_values = list(
        model3_config.get("base_l_rule", {}).get(
            "market_chain_count_bucket", []
        )
    )
    if chain_3_8_enabled and "3_8" not in chain_values:
        chain_values.insert(0, "3_8")
    if not chain_3_8_enabled:
        chain_values = [value for value in chain_values if value != "3_8"]
    model3_config.setdefault("base_l_rule", {})[
        "market_chain_count_bucket"
    ] = chain_values
    passed, _reason = model3_l_base_rule_pass(row.to_dict(), model3_config)
    return passed


def l_replace_guard_passes(sources: Sources, row: pd.Series) -> bool:
    """L替换A/C/E2必须通过实盘共用窄门。"""

    passed, _reason = model3_l_replace_guard_pass(
        row.to_dict(), sources.model3_config
    )
    return passed


def choose_l(
    sources: Sources,
    signal_date: str,
    mode1: dict[str, Any] | None,
    *,
    chain_3_8_enabled: bool,
) -> dict[str, Any] | None:
    """按当前model=3规则执行L补位或替换。"""

    l_row = sources.l_lookup.get(signal_date)
    if not l_base_passes(
        sources, l_row, chain_3_8_enabled=chain_3_8_enabled
    ) or l_row is None:
        return mode1
    if mode1 is not None and not l_replace_guard_passes(sources, l_row):
        return mode1

    ok, old_account_return, exit_date, status = l_trade_return(l_row)
    if not ok:
        return None
    return {
        "strategy_leg": "L",
        "ts_code": str(l_row.get("ts_code", "")),
        "name": str(l_row.get("name", "")),
        "buy_date": normalize_date(l_row.get("d1_trade_date")),
        "exit_date": normalize_date(exit_date),
        "account_return": old_account_return * POSITION_PCT / OLD_POSITION_PCT,
        "return_source": status,
    }


def daily_close(trade_date: str, ts_code: str) -> float:
    """D历史末笔缺少退出价时读取本地日线。"""

    path = DAILY_PRICE_DIR / f"{trade_date}.csv"
    daily = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
    rows = daily[daily["ts_code"].astype(str).str.upper().eq(ts_code.upper())]
    if rows.empty:
        raise KeyError(f"{trade_date}日线中找不到{ts_code}")
    close = to_float(rows.iloc[-1].get("close"))
    if close <= 0:
        raise ValueError(f"{trade_date} {ts_code}收盘价无效")
    return close


def d_t2_candidate(sources: Sources, signal_date: str) -> dict[str, Any]:
    """D无接力时按T+2收盘并保留80%成交压力折扣。"""

    row = source_row(sources.strategy_d, signal_date, "D")
    exit_date = nth_trade_date(sources, signal_date, 2)
    exit_close = to_float(row.get("exit_close"))
    if exit_close <= 0:
        exit_close = daily_close(exit_date, str(row.get("ts_code", "")))
    net_return = (
        exit_close / to_float(row.get("limit_close"))
        - 1.0
        - D_ROUND_TRIP_COST
    )
    return {
        "strategy_leg": "D",
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "buy_date": signal_date,
        "exit_date": exit_date,
        "account_return": net_return * D_FILL_STRESS * POSITION_PCT,
        "return_source": "D_T2_CLOSE;净收益×80%成交压力",
    }


def d_relay_candidate(
    sources: Sources,
    signal_date: str,
    next_candidate: dict[str, Any],
) -> dict[str, Any]:
    """组合D的T+1接力收益与A/C/E2下一腿收益。"""

    d_row = source_row(sources.strategy_d, signal_date, "D")
    d_return = (
        to_float(d_row.get("account_return"))
        * POSITION_PCT
        / OLD_POSITION_PCT
    )
    next_return = to_float(next_candidate.get("account_return"))
    next_leg = str(next_candidate.get("strategy_leg", ""))
    return {
        **next_candidate,
        "strategy_leg": f"D→{next_leg}",
        "account_return": (1.0 + d_return) * (1.0 + next_return) - 1.0,
        "return_source": f"D_T1_RELAY({d_return:.6f})+{next_leg}({next_return:.6f})",
    }


def replay(
    sources: Sources,
    *,
    entry_gate_enabled: bool,
    l_chain_3_8_enabled: bool = False,
) -> pd.DataFrame:
    """严格按释放日串行重放481个信号日。"""

    equity = INITIAL_EQUITY
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
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_leg": occupied_leg,
                    "blocked_by_code": occupied_code,
                    "blocked_until": occupied_until,
                    "return_source": "",
                }
            )
            continue

        occupied_until = occupied_leg = occupied_code = ""
        mode1 = mode1_candidate(
            sources,
            row,
            row_index,
            entry_gate_enabled=entry_gate_enabled,
        )

        # D在信号日盘中发生，早于收盘后A/C/E2/L计划。只有A/C/E2可接力；
        # L不参与D接力，保持当前实盘行为。
        if abs(to_float(row.get("d_return"))) > EPSILON:
            if mode1 is not None:
                selected = d_relay_candidate(sources, signal_date, mode1)
            else:
                selected = d_t2_candidate(sources, signal_date)
        else:
            selected = choose_l(
                sources,
                signal_date,
                mode1,
                chain_3_8_enabled=l_chain_3_8_enabled,
            )

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
                    "equity_before": equity_before,
                    "equity_after": equity,
                    "blocked_by_leg": "",
                    "blocked_by_code": "",
                    "blocked_until": "",
                    "return_source": "",
                }
            )
            continue

        exit_date = normalize_date(selected.get("exit_date"))
        account_return = to_float(selected.get("account_return"))
        if not exit_date:
            raise ValueError(f"{signal_date} {selected.get('strategy_leg')}缺少退出日")
        if account_return <= -1.0:
            raise ValueError(f"{signal_date}账户收益不允许小于等于-100%")

        equity *= 1.0 + account_return
        occupied_until = exit_date
        occupied_leg = str(selected.get("strategy_leg", ""))
        occupied_code = str(selected.get("ts_code", ""))
        rows.append(
            {
                "signal_date": signal_date,
                "status": "EXECUTED",
                "strategy_leg": occupied_leg,
                "ts_code": occupied_code,
                "name": selected.get("name", ""),
                "buy_date": selected.get("buy_date", ""),
                "exit_date": exit_date,
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
                "blocked_by_leg": "",
                "blocked_by_code": "",
                "blocked_until": "",
                "return_source": selected.get("return_source", ""),
            }
        )

    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    detail["entry_gate_enabled"] = entry_gate_enabled
    detail["l_chain_3_8_enabled"] = l_chain_3_8_enabled
    return detail


def max_consecutive_losses(returns: pd.Series) -> int:
    """计算最大连续亏损笔数。"""

    maximum = current = 0
    for value in returns:
        if float(value) <= 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def summarize(detail: pd.DataFrame, scenario: str) -> dict[str, Any]:
    """汇总单一组合场景。"""

    trades = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    returns = pd.to_numeric(trades["account_return"], errors="raise")
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    multiple = float(detail["equity_after"].iloc[-1] / INITIAL_EQUITY)
    legs = trades["strategy_leg"].astype(str)
    return {
        "scenario": scenario,
        "signal_day_count": int(len(detail)),
        "executed_trade_count": int(len(trades)),
        "a_trade_count": int(legs.eq("A").sum()),
        "c_trade_count": int(legs.eq("C").sum()),
        "d_trade_count": int(legs.eq("D").sum()),
        "d_to_a_trade_count": int(legs.eq("D→A").sum()),
        "d_to_c_trade_count": int(legs.eq("D→C").sum()),
        "d_to_e2_trade_count": int(legs.eq("D→E2").sum()),
        "e2_trade_count": int(legs.eq("E2").sum()),
        "l_trade_count": int(legs.eq("L").sum()),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "equity_multiple": multiple,
        "total_compound_return": multiple - 1.0,
        "max_drawdown": float(detail["drawdown"].min()),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "profit_loss_ratio": (
            float(wins.mean() / abs(losses.mean()))
            if len(wins) and len(losses)
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def e2_entry_gate_validation(sources: Sources) -> pd.DataFrame:
    """分别在前后半段和自然年验证被排除组方向。"""

    trades = sources.e2.reset_index().sort_values("trade_date").reset_index(drop=True)
    trades["account_return"] = pd.to_numeric(trades["net_return"], errors="raise") * POSITION_PCT
    gate_pass = trades.apply(
        lambda row: e2_entry_gate_passes(row, sources.e2_spec), axis=1
    )
    split_date = str(trades.iloc[len(trades) // 2]["trade_date"])
    groups: list[tuple[str, pd.DataFrame]] = [
        ("全部", trades),
        (f"训练半段<{split_date}", trades[trades["trade_date"] < split_date]),
        (f"测试半段>={split_date}", trades[trades["trade_date"] >= split_date]),
    ]
    groups.extend(
        (f"自然年{year}", group)
        for year, group in trades.groupby(trades["trade_date"].str[:4])
    )
    rows: list[dict[str, Any]] = []
    for label, group in groups:
        current_gate = gate_pass.reindex(group.index)
        kept = group[current_gate]
        removed = group[~current_gate]
        base_returns = group["account_return"]
        kept_returns = kept["account_return"]
        rows.append(
            {
                "split": label,
                "base_count": int(len(group)),
                "kept_count": int(len(kept)),
                "removed_count": int(len(removed)),
                "removed_avg_return": (
                    float(removed["account_return"].mean()) if len(removed) else 0.0
                ),
                "removed_win_rate": (
                    float((removed["account_return"] > 0).mean()) if len(removed) else 0.0
                ),
                "base_equity_multiple": float((1.0 + base_returns).prod()),
                "optimized_equity_multiple": float((1.0 + kept_returns).prod()),
                "optimized_vs_base": float(
                    (1.0 + kept_returns).prod() / (1.0 + base_returns).prod() - 1.0
                ),
            }
        )
    return pd.DataFrame(rows)


def l_chain_expansion_validation(
    before: pd.DataFrame,
    after: pd.DataFrame,
) -> pd.DataFrame:
    """比较L连板3~8扩容在全段、前后半段和自然年的组合表现。"""

    groups = [
        ("全部", "", ""),
        ("前半段", "", "20250520"),
        ("后半段", "20250520", ""),
        ("自然年2024", "20240101", "20250101"),
        ("自然年2025", "20250101", "20260101"),
        ("自然年2026", "20260101", "20270101"),
    ]
    rows: list[dict[str, Any]] = []
    for label, start, end in groups:
        before_group = before.copy()
        after_group = after.copy()
        if start:
            before_group = before_group[before_group["signal_date"].ge(start)]
            after_group = after_group[after_group["signal_date"].ge(start)]
        if end:
            before_group = before_group[before_group["signal_date"].lt(end)]
            after_group = after_group[after_group["signal_date"].lt(end)]
        before_trades = before_group[before_group["status"].eq("EXECUTED")]
        after_trades = after_group[after_group["status"].eq("EXECUTED")]
        before_returns = pd.to_numeric(
            before_trades["account_return"], errors="raise"
        )
        after_returns = pd.to_numeric(
            after_trades["account_return"], errors="raise"
        )
        before_l_returns = pd.to_numeric(
            before_trades.loc[
                before_trades["strategy_leg"].eq("L"), "account_return"
            ],
            errors="raise",
        )
        after_l_returns = pd.to_numeric(
            after_trades.loc[
                after_trades["strategy_leg"].eq("L"), "account_return"
            ],
            errors="raise",
        )
        before_multiple = float((1.0 + before_returns).prod())
        after_multiple = float((1.0 + after_returns).prod())
        before_l_multiple = float((1.0 + before_l_returns).prod())
        after_l_multiple = float((1.0 + after_l_returns).prod())
        rows.append(
            {
                "split": label,
                "before_trade_count": int(len(before_trades)),
                "after_trade_count": int(len(after_trades)),
                "before_l_trade_count": int(len(before_l_returns)),
                "after_l_trade_count": int(len(after_l_returns)),
                "before_total_multiple": before_multiple,
                "after_total_multiple": after_multiple,
                "total_change": after_multiple / before_multiple - 1.0,
                "before_l_multiple": before_l_multiple,
                "after_l_multiple": after_l_multiple,
                "l_change": after_l_multiple / before_l_multiple - 1.0,
            }
        )
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    """生成不依赖tabulate的Markdown表格。"""

    view = frame.copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(lambda value: f"{float(value):.6f}")
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
    e2_validation: pd.DataFrame,
    l_validation: pd.DataFrame,
) -> None:
    """写出中文认证报告。"""

    base = summary.iloc[0]
    e2_only = summary.iloc[1]
    optimized = summary.iloc[2]
    lines = [
        "# 当前可执行组合、E2门禁与L扩容认证",
        "",
        "## 结论",
        "",
        f"- 原可执行基线：{int(base['executed_trade_count'])}笔，{base['equity_multiple']:.2f}倍，最大回撤{base['max_drawdown']:.2%}。",
        f"- 只接入E2门禁：{int(e2_only['executed_trade_count'])}笔，{e2_only['equity_multiple']:.2f}倍，最大回撤{e2_only['max_drawdown']:.2%}。",
        f"- 再接入L连板3~8扩容：{int(optimized['executed_trade_count'])}笔，{optimized['equity_multiple']:.2f}倍，最大回撤{optimized['max_drawdown']:.2%}。",
        f"- 总组合复利变化：{optimized['equity_multiple'] / base['equity_multiple'] - 1:.2%}。",
        "- E2门禁字段只来自信号日首次涨停时间；每日第一名被排除后直接空仓，不回补第二名。",
        "- L扩容只增加T日已知的market_chain_count_bucket=3_8；选股、买卖时间、成交约束和替换窄门均不改变。",
        "- 2026短窗口的组合复利略低于扩容前，已在分段表中保留，不再为单笔历史结果继续调参。",
        "- 该结果是历史回放，不是收益承诺；实盘仍须小资金验证成交、滑点、POV和容量。",
        "",
        "## 组合新旧对照",
        "",
        markdown_table(summary),
        "",
        "## E2前后半段及分年验证",
        "",
        markdown_table(e2_validation),
        "",
        "## L扩容前后半段及分年验证",
        "",
        markdown_table(l_validation),
        "",
        "## 实盘对齐说明",
        "",
        "- 配置：`config/strategy_e2_r1_scenarios.json`中的entry_gate。",
        "- 共用代码：`src/strategy_e2.py`先选每日第一名，再执行同一门禁。",
        "- 历史验证：`scripts/verify_strategy_e2_alignment.py`必须同时通过门禁前50/50和门禁后43/43逐票对齐。",
        "- E2实盘信号、model=3盘中预览和历史回测均调用同一规则源。",
        "- L共用代码：`src/strategy_model3_policy.py`；实盘状态机和本认证脚本共同调用。",
        "- D实盘排序恢复为回测口径：炸板1~3次，优先2次，再按封单金额/流通市值降序。",
    ]
    (OUTPUT_DIR / "portfolio_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    base_daily = replay(
        sources, entry_gate_enabled=False, l_chain_3_8_enabled=False
    )
    e2_only_daily = replay(
        sources, entry_gate_enabled=True, l_chain_3_8_enabled=False
    )
    optimized_daily = replay(
        sources, entry_gate_enabled=True, l_chain_3_8_enabled=True
    )
    base_summary = summarize(base_daily, "current_before_e2_entry_gate")
    e2_only_summary = summarize(e2_only_daily, "current_after_e2_entry_gate")
    optimized_summary = summarize(
        optimized_daily, "current_after_e2_gate_and_l_chain_3_8_expansion"
    )
    summary = pd.DataFrame([base_summary, e2_only_summary, optimized_summary])

    if base_summary["executed_trade_count"] != EXPECTED_BASE_TRADE_COUNT:
        raise RuntimeError("门禁前组合样本数漂移，拒绝发布")
    if abs(base_summary["equity_multiple"] - EXPECTED_BASE_MULTIPLE) > 1e-9:
        raise RuntimeError("门禁前组合复利漂移，拒绝发布")
    if e2_only_summary["executed_trade_count"] != EXPECTED_E2_ONLY_TRADE_COUNT:
        raise RuntimeError("E2门禁后组合样本数漂移，拒绝发布")
    if abs(e2_only_summary["equity_multiple"] - EXPECTED_E2_ONLY_MULTIPLE) > 1e-9:
        raise RuntimeError("E2门禁后组合复利漂移，拒绝发布")
    if optimized_summary["executed_trade_count"] != EXPECTED_OPTIMIZED_TRADE_COUNT:
        raise RuntimeError("L扩容后组合样本数漂移，拒绝发布")
    if abs(optimized_summary["equity_multiple"] - EXPECTED_OPTIMIZED_MULTIPLE) > 1e-9:
        raise RuntimeError("门禁后组合复利漂移，拒绝发布")
    if optimized_summary["equity_multiple"] <= e2_only_summary["equity_multiple"]:
        raise RuntimeError("L扩容没有提高完整组合复利，禁止上线")
    if optimized_summary["max_drawdown"] < base_summary["max_drawdown"]:
        raise RuntimeError("E2门禁恶化完整组合最大回撤，禁止上线")

    e2_validation = e2_entry_gate_validation(sources)
    required_splits = e2_validation[e2_validation["split"].ne("全部")]
    if bool((required_splits["removed_avg_return"] >= 0).any()):
        raise RuntimeError("E2被排除组未在全部前后半段/自然年保持负均值，禁止上线")
    if bool((required_splits["optimized_vs_base"] <= 0).any()):
        raise RuntimeError("E2门禁未在全部前后半段/自然年提高单腿复利，禁止上线")

    l_validation = l_chain_expansion_validation(e2_only_daily, optimized_daily)
    required_l_splits = l_validation[
        l_validation["split"].isin({"全部", "前半段", "后半段"})
    ]
    if bool((required_l_splits["total_change"] <= 0).any()):
        raise RuntimeError("L扩容未在全段及前后半段提高组合复利，禁止上线")
    if bool((required_l_splits["l_change"] <= 0).any()):
        raise RuntimeError("L扩容未在全段及前后半段提高L分支复利，禁止上线")

    base_daily.to_csv(
        OUTPUT_DIR / "portfolio_daily_before_gate.csv",
        index=False,
        encoding="utf-8-sig",
    )
    e2_only_daily.to_csv(
        OUTPUT_DIR / "portfolio_daily_after_e2_gate.csv",
        index=False,
        encoding="utf-8-sig",
    )
    optimized_daily.to_csv(
        OUTPUT_DIR / "portfolio_daily.csv", index=False, encoding="utf-8-sig"
    )
    optimized_daily[
        optimized_daily["status"].astype(str).eq("EXECUTED")
    ].to_csv(OUTPUT_DIR / "portfolio_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_DIR / "portfolio_summary.csv", index=False, encoding="utf-8-sig")
    e2_validation.to_csv(
        OUTPUT_DIR / "e2_entry_gate_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    l_validation.to_csv(
        OUTPUT_DIR / "l_chain_expansion_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    write_report(summary, e2_validation, l_validation)

    print("当前可执行组合认证完成")
    print(summary.to_string(index=False))
    print("\nE2入场门禁前后半段/分年验证")
    print(e2_validation.to_string(index=False))
    print("\nL连板3~8扩容前后半段/分年验证")
    print(l_validation.to_string(index=False))


if __name__ == "__main__":
    main()
