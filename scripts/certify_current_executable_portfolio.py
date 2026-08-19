"""认证当前D/A/M/E/C/N组合的可执行逐日资金曲线。

本脚本把此前散落的一次性计算固化成可重复基线，并比较E门禁与M启用前后：

1. B已删除；历史B日只保留按当前A→C重选后真实存在的A/C候选；
2. E使用无前视、单账户R1明细，旧E候选和旧收益不得混入；
3. 普通首仓按82.5%，旧策略仓未释放前不做尾盘衔接；
4. **D接力全关**：D一律T+2收盘平仓，确认清仓后的下一个信号日才轮到别的腿
   （2026-08-07；旧接力口径已经作废）；
5. E入场门禁在每日第一名确定后执行，被排除时不回补第二名；
6. 所有收益按同一账户、同一时间顺序连乘，禁止把各腿复利直接相乘。

当前有效腿序 **D > A > M > E > C > N**（2026-08-19 定稿），见 pick_by_priority。
D 不在 pick_by_priority 里：它在信号日盘中就买了，位置由时序锁死，见 replay。

⚠️ **本脚本是实盘的对照基准，两侧必须同时正确。** 实盘一侧分两层，缺一层就是空转：
      上游门（信号生不生成）run_strategy_m_signal.higher_priority_leg_has_signal
                            run_strategy_e_signal.has_ac_planned_order
      下游腿序（生成了怎么挑）combined_live_engine.build_plan

被腿序改造废弃的旧规则函数已删除，不再留在文件里等人误接回去；
需要查旧口径请看 git 历史。

该报告仍是历史回放，不承诺未来收益。实盘还会受到涨停无法成交、POV追价上限、
滑点和容量约束影响；放大资金前必须继续小资金核验真实成交。

运行：
    python scripts/certify_current_executable_portfolio.py

输出：
    reports/current_portfolio_alignment/live_certification.json
    reports/current_portfolio_alignment/input_manifest.json
    reports/current_portfolio_alignment/portfolio_summary.csv
    reports/current_portfolio_alignment/portfolio_trades.csv
    reports/current_portfolio_alignment/portfolio_daily.csv
    reports/current_portfolio_alignment/e_entry_gate_validation.csv
    reports/current_portfolio_alignment/portfolio_report.md
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy_e import load_e_spec  # noqa: E402
from src.live_certification import (  # noqa: E402
    certification_file_size,
    certification_file_sha256,
    certification_config_sha256,
    certification_files_sha256,
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
D_PATH = PROJECT_ROOT / "reports" / "strategy_d" / "d_daily_candidates.csv"
E_PATH = (
    PROJECT_ROOT
    / "reports"
    / "strategy_e_samples"
    / "e_r1_daily_candidates_full.csv"
)
NO_B_RESELECTION_PATH = (
    PROJECT_ROOT
    / "config"
    / "strategy_no_b_historical_reselection.csv"
)
AC_DAILY_PATH = (
    PROJECT_ROOT / "reports" / "ac_daily_candidates" / "ac_daily_candidates.csv"
)
M_POOL_PATH = (
    PROJECT_ROOT / "reports" / "strategy_m" / "m_backtest_trades.csv"
)
N_POOL_PATH = PROJECT_ROOT / "reports" / "strategy_n" / "n_backtest_candidates.csv"
E_SPEC_PATH = PROJECT_ROOT / "config" / "strategy_e_r1_scenarios.json"
TRADE_CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
DAILY_PRICE_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "current_portfolio_alignment"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"

CODE_CERTIFICATION_FILES = [
    "scripts/build_ac_daily_candidates.py",
    "scripts/backtest_strategy_d.py",
    "scripts/certify_current_executable_portfolio.py",
    "scripts/monitor_strategy_d_intraday.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/run_strategy_m_signal.py",
    "scripts/run_strategy_n_signal.py",
    "scripts/build_strategy_n_backtest_pool.py",
    "scripts/trading_daemon.py",
    "src/combined_live_engine.py",
    "src/live_certification.py",
    "src/strategy_e.py",
    "src/strategy_identity.py",
    "src/strategy_equity_ledger.py",
    "src/strategy_m.py",
    "src/strategy_n.py",
    "src/strategy_d_spec.py",
]

_RUNTIME_CONFIG = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
_CERTIFICATION_CONFIG = _RUNTIME_CONFIG.get("portfolio_certification", {})
_ANALYSIS_CONFIG = _RUNTIME_CONFIG.get("analysis", {})

INITIAL_EQUITY = float(_CERTIFICATION_CONFIG.get("initial_equity", 500_000.0))
POSITION_PCT = float(_CERTIFICATION_CONFIG.get("position_pct", 0.825))
D_FILL_STRESS = 0.80
D_ROUND_TRIP_COST = 0.0015
M_DRAWDOWN_GUARD = float(
    _RUNTIME_CONFIG.get("strategy_m", {}).get("drawdown_guard_pct", 0.10)
)
AC_BUY_FEE_RATE = float(_ANALYSIS_CONFIG.get("commission_rate", 0.0003)) + float(
    _ANALYSIS_CONFIG.get("transfer_fee_rate", 0.00001)
)
AC_SELL_FEE_RATE = (
    float(_ANALYSIS_CONFIG.get("commission_rate", 0.0003))
    + float(_ANALYSIS_CONFIG.get("transfer_fee_rate", 0.00001))
    + float(_ANALYSIS_CONFIG.get("stamp_tax_rate", 0.001))
)
# 09:20预挂止盈单的让价,与 config.json live_trade.intraday_takeprofit_offset 一致。
# 衔接日判定旧仓能否盘中成交、提前释放资金时使用。
TAKEPROFIT_OFFSET = 0.01
EPSILON = 1e-12

# 先锁定门禁前基线，防止以后输入文件漂移后仍静默生成“更好”结果。
#
# 2026-08-07 修正：A/C 改用逐日独立候选（见 load_ac_daily），不再被
# baseline.abc_return 这张作废持仓表裁剪。以下为修正后的口径。
# 旧值（A/C 被裁到90天，低估约34%）保留作历史对照，勿再当基准：
#   BASE 132 / 2884.052538490145      E_ONLY 129 / 3254.1261014125575
#   OPTIMIZED 132 / 4712.470092237913 WITH_M  147 / 15326.887148064476
# 2026-08-07 第二处修正：衔接日D（见 replay 的 block_d_on_handoff）。旧仓未冲板
# 时14:55才平仓、15:00才确认，而D的下单通道14:56关闭，那些D实盘拿不到。
# 剔除后 A/C 修正版旧值（仍含不可执行的衔接日D）降级为历史对照：
#   BASE 137 / 4252.40931647757        E_ONLY 136 / 4760.864917583647
#   OPTIMIZED 139 / 6907.34827166775   WITH_M  155 / 20606.559741847264
# 2026-08-07 腿序改造第1步：D接力全关（见 replay 内注释与
# combined_live_engine 顶部「腿序与接力口径」）。接力本身值约+8.8%，故本步
# 单独看是降收益的；收益由后续腿序调整补回。接力开启时的旧值降级为历史对照：
#   BASE 133 / 5140.7613530121025    E_ONLY 132 / 5755.436166596083
#   OPTIMIZED 135 / 8350.331871673612 WITH_M 151 / 24911.38506562485
# 当前正式组合的冻结回归锚点；任何输入、顺序或成交口径漂移都必须显式审查。
EXPECTED_CURRENT_TRADE_COUNT = 174
EXPECTED_CURRENT_MULTIPLE = 9508.426795072035
EXPECTED_D_DAILY_CANDIDATE_COUNT = 45
EXPECTED_N_DAILY_CANDIDATE_COUNT = 106


@dataclass(frozen=True)
class Sources:
    """组合回放需要的全部只读来源。"""

    baseline: pd.DataFrame
    abc: pd.DataFrame
    strategy_d: pd.DataFrame
    e: pd.DataFrame
    no_b_reselection: pd.DataFrame
    m_pool: pd.DataFrame | None
    n_pool: pd.DataFrame
    ac_daily: dict[str, dict[str, Any]]
    e_spec: dict[str, Any]
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


def load_sources() -> Sources:
    """加载并严格校验来源；缺失或日期漂移时直接失败。"""

    required = [
        BASELINE_PATH,
        ABC_PATH,
        D_PATH,
        E_PATH,
        NO_B_RESELECTION_PATH,
        TRADE_CALENDAR_PATH,
        RUNTIME_CONFIG_PATH,
        N_POOL_PATH,
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
    if strategy_d["signal_date"].eq("").any() or strategy_d["signal_date"].duplicated().any():
        raise ValueError("D完整逐日候选账本日期为空或重复")
    if len(strategy_d) != EXPECTED_D_DAILY_CANDIDATE_COUNT:
        raise ValueError(
            "D完整逐日候选账本必须恰好"
            f"{EXPECTED_D_DAILY_CANDIDATE_COUNT}个唯一信号日，当前{len(strategy_d)}"
        )
    required_d_columns = {"ts_code", "limit_close", "exit_close"}
    missing_d_columns = sorted(required_d_columns.difference(strategy_d.columns))
    if missing_d_columns:
        raise ValueError("D完整逐日候选账本缺少字段：" + ",".join(missing_d_columns))
    strategy_d = strategy_d.drop_duplicates("signal_date", keep="last").set_index(
        "signal_date"
    )

    e = pd.read_csv(E_PATH, dtype={"trade_date": str}, low_memory=False)
    e["trade_date"] = e["trade_date"].map(normalize_date)
    if len(e) != 102 or e["trade_date"].duplicated().any():
        raise ValueError("E_R1完整逐日候选样本必须恰好102个唯一信号日")
    if not e.get("strategy_variant", pd.Series(index=e.index, dtype=str)).astype(str).eq("E_R1").all():
        raise ValueError("E组合认证只允许读取strategy_variant=E_R1的完整门禁前样本")
    if not e.get("sample_scope", pd.Series(index=e.index, dtype=str)).astype(str).eq("COMPLETE_DAILY_CANDIDATES").all():
        raise ValueError("E组合认证拒绝读取历史成交子集，必须使用完整逐日候选样本")
    e = e.set_index("trade_date")

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

    m_pool: pd.DataFrame | None = None
    if M_POOL_PATH.exists():
        m_pool = pd.read_csv(M_POOL_PATH, dtype={"trade_date": str}, low_memory=False)
        m_pool["trade_date"] = m_pool["trade_date"].map(normalize_date)
        if m_pool["trade_date"].duplicated().any():
            raise ValueError("M候选账本存在重复信号日")
        m_pool = m_pool.set_index("trade_date")

    n_pool = pd.read_csv(N_POOL_PATH, dtype={"trade_date": str}, low_memory=False)
    n_pool["trade_date"] = n_pool["trade_date"].map(normalize_date)
    if (
        len(n_pool) != EXPECTED_N_DAILY_CANDIDATE_COUNT
        or n_pool["trade_date"].duplicated().any()
    ):
        raise ValueError(
            "N完整候选账本必须恰好"
            f"{EXPECTED_N_DAILY_CANDIDATE_COUNT}个唯一信号日"
        )
    if not n_pool["sample_scope"].astype(str).eq("COMPLETE_DAILY_CANDIDATES").all():
        raise ValueError("N认证只允许完整逐日候选样本")
    if not n_pool["execution_status"].astype(str).eq("OK").all():
        raise ValueError("N候选账本含不可执行候选")
    n_pool = n_pool.set_index("trade_date")

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

    return Sources(
        baseline=baseline,
        abc=abc,
        strategy_d=strategy_d,
        e=e,
        no_b_reselection=no_b,
        m_pool=m_pool,
        n_pool=n_pool,
        ac_daily=load_ac_daily(),
        e_spec=load_e_spec(PROJECT_ROOT),
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


def e_entry_gate_passes(row: pd.Series, spec: dict[str, Any]) -> bool:
    """只用信号日字段执行配置化E入场门禁。"""

    for column, values in spec.get("entry_gate", {}).get("exclude_values", {}).items():
        if column not in row.index:
            raise RuntimeError(f"E锁定明细缺少入场门禁字段：{column}")
        if str(row.get(column, "")) in {str(value) for value in values}:
            return False
    return True


def load_ac_daily() -> dict[str, dict[str, Any]]:
    """加载逐日独立生成的 A/C 候选（2026-08-07 修正）。

    旧口径用 `baseline.abc_return != 0` 当 A/C 的门槛，而 baseline 是
    A/B/C 三腿**单独回放**的产物，带着那次回放自己的持仓序列：A/C 明细481天里
    有108天是 `POSITION_OCCUPIED_SKIP`（当年被已删除的B等占掉），连 ts_code
    都没落盘。今天的组合含 D/A/M/E/C/N，持仓情况完全不同，那张持仓表早已作废，
    却仍在挡 A/C —— A/C 可用天数被锁死在90天。

    实盘从不受此限制：run_paper_ab_filtered_daily_ops / combined_live_engine /
    paper_candidate_generator 三个文件都不读 baseline，每天独立跑选股规则。
    所以这不是放宽口径，是把回测对齐到实盘一直在做的事。

    本文件由 A/C 各自的认证规则逐日重算得出（A: candidate_filters+ranking，
    T+2收盘；C: c_strategy.conditions，T+3收盘；一字涨停买不到、跌停顺延），
    并已验证：与已知90天成交对比，69天 ts_code 完全一致，21天不一致全部是
    已删除的B腿；无B重选表里3天实际生效的C（融发核电/岭南股份/洛凯股份）
    全部命中同一只票。
    """

    if not AC_DAILY_PATH.exists():
        raise FileNotFoundError(f"找不到A/C逐日候选: {AC_DAILY_PATH}")
    frame = pd.read_csv(
        AC_DAILY_PATH,
        dtype={"signal_date": str, "ts_code": str, "buy_date": str, "exit_date": str},
    )
    result: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        leg = str(row.get("leg", "")).upper()
        if leg not in {"A", "C"} or str(row.get("status", "")) != "OK":
            continue
        stock_return = to_float(row.get("stock_return"), float("nan"))
        if stock_return != stock_return:      # NaN
            continue
        result[normalize_date(row.get("signal_date"))] = {
            "strategy_leg": leg,
            "ts_code": str(row.get("ts_code", "")),
            "name": str(row.get("name", "")),
            "buy_date": normalize_date(row.get("buy_date")),
            "exit_date": normalize_date(row.get("exit_date")),
            "account_return": (
                stock_return - AC_BUY_FEE_RATE - (1.0 + stock_return) * AC_SELL_FEE_RATE
            ) * POSITION_PCT,
            "return_source": "A/C逐日独立候选(显式扣佣金/过户费/印花税)",
        }
    return result


def pick_by_priority(
    sources: Sources,
    row: pd.Series,
    row_index: int,
    *,
    entry_gate_enabled: bool,
    m_enabled: bool,
    n_enabled: bool,
    equity: float,
    peak_equity: float,
) -> dict[str, Any] | None:
    """按当前有效腿序选出当天唯一候选（D 不在此处，见 replay）。

    A 与 C 条件互斥（A 要 market_chain_count_bucket=8_15、C 要 15_30），同一天
    不可能都有票，所以 C 排在 A 之后的任何位置结果相同；这里显式让 C 垫底，
    与实盘的腿序声明保持一致。
    """

    signal_date = str(row["date"])
    ac = sources.ac_daily.get(signal_date)

    # ① A
    if ac is not None and str(ac.get("strategy_leg", "")) == "A":
        return dict(ac)

    # ② M：回撤保护仍然生效
    if m_enabled:
        m_pick = m_candidate(sources, signal_date, equity, peak_equity)
        if m_pick is not None:
            return m_pick

    # ③ E
    if signal_date in sources.e.index:
        e = source_row(sources.e, signal_date, "E R1")
        if not (entry_gate_enabled and not e_entry_gate_passes(e, sources.e_spec)):
            return {
                "strategy_leg": "E",
                "ts_code": str(e.get("ts_code", "")),
                "name": str(e.get("name", "")),
                "buy_date": normalize_date(e.get("buy_date")),
                "exit_date": normalize_date(e.get("exit_date")),
                "account_return": to_float(e.get("net_return")) * POSITION_PCT,
                "return_source": (
                    f"E_R1:{e.get('scenario_rank', '')};"
                    f"first_time={e.get('first_time_detail_bucket', '')}"
                ),
            }

    # ④ C
    if ac is not None and str(ac.get("strategy_leg", "")) == "C":
        return dict(ac)

    # ⑤ N：只在前四个收盘后策略腿均空时补位
    if n_enabled:
        n_pick = n_candidate(sources, signal_date)
        if n_pick is not None:
            return n_pick

    return None


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


def hit_limit_up(trade_date: str, ts_code: str) -> bool:
    """当日是否冲到涨停（判断09:20预挂止盈单能否盘中成交）。

    实盘止盈单挂在"涨停价 - intraday_takeprofit_offset(0.01元)"，冲板即成交
    （trading_daemon:5121/5501「冲板即成交锁定强势」）。日线口径下用最高价是否
    触及该价位近似判定；取不到行情时保守返回 False（视为未提前释放资金）。
    """

    if not trade_date or not ts_code:
        return False
    path = DAILY_PRICE_DIR / f"{trade_date}.csv"
    if not path.exists():
        return False
    daily = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
    rows = daily[daily["ts_code"].astype(str).str.upper().eq(ts_code.upper())]
    if rows.empty:
        return False
    row = rows.iloc[-1]
    pre_close = to_float(row.get("pre_close"))
    high = to_float(row.get("high"))
    if pre_close <= 0 or high <= 0:
        return False
    limit_pct = 0.20 if ts_code[:3] in {"300", "301", "688"} else 0.10
    cap = round(pre_close * (1.0 + limit_pct), 2)
    return high >= cap - TAKEPROFIT_OFFSET - 1e-9


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


def m_candidate(
    sources: Sources,
    signal_date: str,
    equity: float,
    peak_equity: float,
) -> dict[str, Any] | None:
    """M补位腿：只在D/A均未占用时调用，再过一道回撤保护。

    与实盘 src/strategy_m.py 同口径：选股已在
    scripts/build_strategy_m_backtest_pool.py 固化，这里只判断回撤闸门。
    """

    if sources.m_pool is None or signal_date not in sources.m_pool.index:
        return None
    if peak_equity > 0 and equity / peak_equity - 1.0 <= -M_DRAWDOWN_GUARD:
        return None
    row = source_row(sources.m_pool, signal_date, "M候选池")
    return {
        "strategy_leg": "M",
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "buy_date": normalize_date(row.get("buy_date")),
        "exit_date": normalize_date(row.get("exit_date")),
        "account_return": to_float(row.get("net_return")) * POSITION_PCT,
        "return_source": f"M兜底:{row.get('sentiment','')};流通市值最小",
    }


def n_candidate(sources: Sources, signal_date: str) -> dict[str, Any] | None:
    """N最低优先级腿；候选已由src.strategy_n唯一规则源逐日固化。"""

    if signal_date not in sources.n_pool.index:
        return None
    row = source_row(sources.n_pool, signal_date, "N完整候选池")
    stock_return = to_float(row.get("stock_return_before_fees"), float("nan"))
    if pd.isna(stock_return):
        raise ValueError(f"{signal_date} N候选缺少收益结果")
    account_return = (
        stock_return - AC_BUY_FEE_RATE - (1.0 + stock_return) * AC_SELL_FEE_RATE
    ) * POSITION_PCT
    return {
        "strategy_leg": "N",
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "buy_date": normalize_date(row.get("buy_date")),
        "exit_date": normalize_date(row.get("exit_date")),
        "account_return": account_return,
        "return_source": (
            f"N双分支:{row.get('n_branch', '')}:{row.get('n_rule_id', '')};"
            "T+1开/T+2收;显式费用"
        ),
    }


def replay(
    sources: Sources,
    *,
    entry_gate_enabled: bool,
    m_enabled: bool = False,
    n_enabled: bool = True,
    block_d_on_handoff: bool = True,
) -> pd.DataFrame:
    """严格按释放日串行重放481个信号日。

    block_d_on_handoff：衔接日按旧仓能否提前释放资金决定D是否可开（2026-08-07）。

    A/C/E/M/N 是 T日收盘后出信号、T+1开盘买，旧仓 T日收盘已卖完，不冲突。
    D 不同——它是 T日**盘中**买入（trading_daemon:9326/12498 写死 14:00起BUY、
    14:56停止），旧仓何时释放资金决定它买不买得到：

    - 旧仓 T日冲到涨停：09:20 预挂的"涨停-0.01"止盈单盘中成交，资金早上回来，
      **D 可开**；
    - 旧仓 T日未冲板：走 14:55 主平仓、15:00 之后才确认成交，而 D 的下单通道
      14:56 已关闭，**D 不可开**。

    旧口径在衔接日一律放行D，混入了未冲板那部分实盘拿不到的交易。
    设 False 可复现旧口径做对照。

    注：13:00 起的 POV 分流卖出是另一条提前释放路径，但只在仓位超过实时容量
    门槛时启动（trading_daemon:18），当前资金量不触发，故不纳入；资金放大后
    需要重新评估。
    """

    equity = INITIAL_EQUITY
    peak_equity = INITIAL_EQUITY
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

        # 衔接日 = 今天正好是上一笔的退出日。旧仓冲板则09:20止盈单盘中成交、
        # 资金早上释放；未冲板则14:55才平仓，D的14:56下单通道等不到资金。
        blocking_handoff = (
            block_d_on_handoff
            and bool(occupied_until)
            and signal_date == occupied_until
            and not hit_limit_up(signal_date, occupied_code)
        )
        occupied_until = occupied_leg = occupied_code = ""
        # D在信号日盘中发生，早于收盘后其余各腿的计划——D 的位置由时序锁死，
        # 不是可优化项（"看到别的腿有票就不做D"需要预知几小时后的收盘结果）。
        # D必须直接读取完整逐日候选账本。旧写法用baseline.d_return作门，而该字段
        # 来自曾被旧A/B/C POSITION_OCCUPIED_SKIP裁剪的D交易表，会漏掉当前组合
        # 账户实际空闲日的D信号，造成组合资金曲线偏乐观。
        if signal_date in sources.strategy_d.index and not blocking_handoff:
            # 2026-08-07 接力全关：D 一律走自己的 T+2 收盘平仓，平仓当天不开新仓，
            # 下一个信号日才轮到别的腿。与实盘 combined_live_engine 同口径
            # （见该文件顶部「腿序与接力口径」）。
            #
            # 旧口径在此处对 A/C/E 做 d_relay_candidate（同一天资金用两次）。
            # 关闭依据：接力多出的收益超过一半来自口径不对称——接力的D走T+1竞价、
            # 不打成交压力折扣，而T+2退出的D要打80%折扣；同折扣口径下接力只值+7.8%，
            # 换来的却是五步成对POV链路。关闭后胜率反升、执行链路变成一条直线。
            selected = d_t2_candidate(sources, signal_date)
        else:
            selected = pick_by_priority(
                sources,
                row,
                row_index,
                entry_gate_enabled=entry_gate_enabled,
                m_enabled=m_enabled,
                n_enabled=n_enabled,
                equity=equity,
                peak_equity=peak_equity,
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
        peak_equity = max(peak_equity, equity)
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
    detail["m_enabled"] = m_enabled
    detail["n_enabled"] = n_enabled
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
    fixed_initial_notional_multiple = float(1.0 + returns.sum())
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
        "d_to_e_trade_count": int(legs.eq("D→E").sum()),
        "e_trade_count": int(legs.eq("E").sum()),
        "m_trade_count": int(legs.eq("M").sum()),
        "n_trade_count": int(legs.eq("N").sum()),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "equity_multiple": multiple,
        "fixed_initial_notional_multiple": fixed_initial_notional_multiple,
        "theoretical_ending_equity": float(INITIAL_EQUITY * multiple),
        "theoretical_next_order_amount": float(INITIAL_EQUITY * multiple * POSITION_PCT),
        "capacity_certified": False,
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


def e_entry_gate_validation(sources: Sources) -> pd.DataFrame:
    """分别在前后半段和自然年验证被排除组方向。"""

    # E完整特征样本列很多；先复制完成内存整理，避免Windows pandas在
    # reset_index插列时产生高度碎片化警告。这里只改变内存布局，不改变数据。
    trades = (
        sources.e.copy(deep=True)
        .reset_index()
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    trades["account_return"] = pd.to_numeric(trades["net_return"], errors="raise") * POSITION_PCT
    gate_pass = trades.apply(
        lambda row: e_entry_gate_passes(row, sources.e_spec), axis=1
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


SEGMENTS = [
    ("全部", "20240520", "20260514"),
    ("训练半段<20250603", "20240520", "20250602"),
    ("测试半段>=20250603", "20250603", "20260514"),
    ("自然年2024", "20240101", "20241231"),
    ("自然年2025", "20250101", "20251231"),
    ("自然年2026", "20260101", "20261231"),
]


def segment_comparison(
    before: pd.DataFrame,
    after: pd.DataFrame,
    *,
    before_label: str,
    after_label: str,
) -> pd.DataFrame:
    """完整组合口径的分段对照。

    单腿改善不等于组合改善：一条腿被挡下后，当天资金可能空仓，也可能被其它腿
    接手，还会改变后续时间线。因此每个开关都必须在完整组合上分段验证，而不是
    只看单腿数字。
    """

    rows: list[dict[str, Any]] = []
    for label, low, high in SEGMENTS:
        def window(detail: pd.DataFrame) -> pd.DataFrame:
            executed = detail[detail["status"].astype(str).eq("EXECUTED")]
            return executed[
                (executed["signal_date"].astype(str) >= low)
                & (executed["signal_date"].astype(str) <= high)
            ]

        before_window, after_window = window(before), window(after)
        before_equity = float((1.0 + before_window["account_return"]).prod())
        after_equity = float((1.0 + after_window["account_return"]).prod())

        def drawdown(frame: pd.DataFrame) -> float:
            if frame.empty:
                return 0.0
            curve = (1.0 + frame["account_return"]).cumprod()
            return float((curve / curve.cummax() - 1.0).min())

        rows.append(
            {
                "split": label,
                f"{before_label}_trade_count": len(before_window),
                f"{after_label}_trade_count": len(after_window),
                f"{before_label}_total_multiple": before_equity,
                f"{after_label}_total_multiple": after_equity,
                "total_change": after_equity / before_equity - 1.0 if before_equity else 0.0,
                f"{before_label}_max_drawdown": drawdown(before_window),
                f"{after_label}_max_drawdown": drawdown(after_window),
            }
        )
    return pd.DataFrame(rows)


def period_metrics(detail: pd.DataFrame) -> pd.DataFrame:
    """按冻结全段、前后半段和自然年汇总当前组合，避免只看总复利。"""

    rows: list[dict[str, Any]] = []
    for label, low, high in SEGMENTS:
        trades = detail[
            detail["status"].astype(str).eq("EXECUTED")
            & detail["signal_date"].astype(str).between(low, high)
        ].copy()
        returns = pd.to_numeric(trades["account_return"], errors="raise")
        wins = returns[returns > 0]
        losses = returns[returns < 0]
        curve = (1.0 + returns).cumprod()
        rows.append(
            {
                "split": label,
                "trade_count": int(len(returns)),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_return": float(returns.mean()) if len(returns) else 0.0,
                "median_return": float(returns.median()) if len(returns) else 0.0,
                "equity_multiple": float(curve.iloc[-1]) if len(curve) else 1.0,
                "fixed_initial_notional_multiple": float(1.0 + returns.sum()),
                "max_drawdown": (
                    float((curve / curve.cummax() - 1.0).min()) if len(curve) else 0.0
                ),
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "profit_loss_ratio": (
                    float(wins.mean() / abs(losses.mean()))
                    if len(wins) and len(losses)
                    else 0.0
                ),
                "max_consecutive_losses": max_consecutive_losses(returns),
            }
        )
    return pd.DataFrame(rows)


def verdict_lines(
    comparison: pd.DataFrame,
    name: str,
    before_label: str,
    after_label: str,
    *,
    risk_accepted: bool = False,
) -> list[str]:
    """按"非劣才改"判据逐段核对，并写出明确判定。"""

    worse = comparison[comparison["total_change"] < -1e-9]["split"].tolist()
    dd_worse = comparison[
        comparison[f"{after_label}_max_drawdown"]
        < comparison[f"{before_label}_max_drawdown"] - 1e-9
    ]["split"].tolist()
    overall = comparison.iloc[0]
    lines = [
        f"### 判定：{name}",
        "",
        f"- 全样本复利：{overall[f'{before_label}_total_multiple']:.2f}倍 → "
        f"{overall[f'{after_label}_total_multiple']:.2f}倍（{overall['total_change']:+.2%}）",
        f"- 复利分段检查：{'六段全部不劣' if not worse else '劣于对照的分段=' + '、'.join(worse)}",
        f"- 回撤分段检查：{'六段全部不劣' if not dd_worse else '变差的分段=' + '、'.join(dd_worse)}",
    ]
    if overall["total_change"] > 0 and not worse and not dd_worse:
        lines.append(f"- **结论：按回测判据成立，{name}维持上线。**")
    elif overall["total_change"] > 0 and risk_accepted:
        lines.append(
            f"- **结论：全样本改善但存在劣段（见上），非劣门禁判定仍为失败；"
            f"用户明确接受该风险，{name}按`PASS_WITH_RISK_ACCEPTANCE`恢复真实新开仓。**"
        )
    elif overall["total_change"] > 0:
        lines.append(
            f"- **结论：全样本改善但存在劣段（见上），未通过“分段收益与回撤均非劣”"
            f"门禁，{name}不得用于真实新开仓。**"
        )
    else:
        lines.append(f"- **结论：按回测判据不成立，{name}不应上线。**")
    lines.append("")
    return lines


def resolve_m_release_status(
    *,
    m_live_enabled: bool,
    m_noninferior: bool,
    risk_accepted: bool,
    noninferiority_reason: str,
) -> str:
    """决定M发布状态；风险接受只能豁免M门禁，不能伪造门禁通过。"""

    if not m_live_enabled or m_noninferior:
        return "PASS"
    if risk_accepted:
        return "PASS_WITH_RISK_ACCEPTANCE"
    raise RuntimeError(f"M完整组合非劣门禁未通过，禁止真实上线：{noninferiority_reason}")


def noninferiority_passes(
    comparison: pd.DataFrame,
    before_label: str,
    after_label: str,
) -> tuple[bool, str]:
    """严格执行“全样本改善，且所有分段收益/回撤均不劣”。"""

    if comparison.empty:
        return False, "没有可验证分段"
    overall = comparison.iloc[0]
    worse_return = comparison[comparison["total_change"] < -EPSILON]["split"].tolist()
    worse_drawdown = comparison[
        comparison[f"{after_label}_max_drawdown"]
        < comparison[f"{before_label}_max_drawdown"] - EPSILON
    ]["split"].tolist()
    if float(overall["total_change"]) <= EPSILON:
        return False, "全样本复利没有提高"
    if worse_return:
        return False, "收益劣段=" + "、".join(map(str, worse_return))
    if worse_drawdown:
        return False, "回撤劣段=" + "、".join(map(str, worse_drawdown))
    return True, "全样本改善且所有分段收益/回撤均非劣"


def write_certification(payload: dict[str, Any]) -> None:
    """原子写认证状态，避免进程中断后留下半个JSON或沿用旧PASS。"""

    path = OUTPUT_DIR / "live_certification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    return certification_file_sha256(path)


def lock_or_verify_input_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    refresh: bool,
) -> None:
    """验证锁定清单；只有显式refresh才原子更新。"""

    if path.exists() and not refresh:
        locked = json.loads(path.read_text(encoding="utf-8"))
        if locked != manifest:
            locked_files = {
                str(row.get("path", "")): row for row in locked.get("files", [])
            }
            current_files = {
                str(row.get("path", "")): row for row in manifest.get("files", [])
            }
            differences: list[str] = []
            for name in sorted(set(locked_files) | set(current_files)):
                before = locked_files.get(name)
                after = current_files.get(name)
                if before is None:
                    differences.append(f"新增：{name}")
                elif after is None:
                    differences.append(f"缺失：{name}")
                elif before != after:
                    differences.append(
                        f"变化：{name}；锁定size={before.get('size')} "
                        f"sha256={before.get('sha256')}；当前size={after.get('size')} "
                        f"sha256={after.get('sha256')}"
                    )
            if not differences:
                differences.append("清单元数据变化（schema/window/file_count）")
            detail = "\n".join(f"- {line}" for line in differences[:20])
            if len(differences) > 20:
                detail += f"\n- 其余{len(differences) - 20}项未显示"
            raise RuntimeError(
                "认证输入与锁定清单不一致，具体差异：\n"
                f"{detail}\n先查明数据变化，确认后使用"
                " --refresh-input-manifest 显式更新并单独审查差异。"
            )
        return
    if not path.exists() and not refresh:
        raise FileNotFoundError(
            "缺少认证输入锁定清单；首次建立必须使用 --refresh-input-manifest"
        )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_input_manifest(*, refresh: bool = False) -> Path:
    """默认核对锁定清单；只有显式refresh才允许接受新的输入版本。"""

    direct = [
        BASELINE_PATH,
        ABC_PATH,
        D_PATH,
        E_PATH,
        NO_B_RESELECTION_PATH,
        AC_DAILY_PATH,
        M_POOL_PATH,
        N_POOL_PATH,
        E_SPEC_PATH,
        TRADE_CALENDAR_PATH,
    ]
    daily = sorted(
        path
        for path in DAILY_PRICE_DIR.glob("????????.csv")
        if "20240520" <= path.stem <= "20260514"
    )
    files = direct + daily
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise FileNotFoundError("认证输入清单存在缺失文件：" + "；".join(missing))
    rows = []
    for path in files:
        rows.append(
            {
                # 清单是跨平台协议，不使用Windows反斜杠；否则同一文件会被
                # Mac记录为data/raw/...、Windows记录为data\raw\...并误判漂移。
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "size": certification_file_size(path),
                "sha256": _file_sha256(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "window": "20240520~20260514",
        "strategy_priority_order": ["D", "A", "M", "E", "C", "N"],
        "file_count": len(rows),
        "files": rows,
    }
    path = OUTPUT_DIR / "input_manifest.json"
    lock_or_verify_input_manifest(path, manifest, refresh=refresh)
    return path


def write_current_report(
    summary: pd.DataFrame,
    current_periods: pd.DataFrame,
    e_validation: pd.DataFrame,
    e_portfolio_comparison: pd.DataFrame,
    m_portfolio_comparison: pd.DataFrame,
    n_portfolio_comparison: pd.DataFrame,
    *,
    current_scenario: str,
    certification_status: str,
    e_gate_validation_passed: bool,
    e_gate_risk_accepted: bool,
    m_noninferior: bool,
    m_noninferior_reason: str,
    m_risk_accepted: bool,
) -> None:
    """写出当前正式组合报告。"""

    current = summary[summary["scenario"].eq(current_scenario)].iloc[0]
    without_m = summary[summary["scenario"].eq("with_e_gate_without_m")].iloc[0]
    lines = [
        "# 当前可执行组合认证",
        "",
        "## 正式结论",
        "",
        "- **当前唯一有效腿序：D > A > M > E > C > N。**",
        f"- 当前冻结窗口为481个信号日，共{int(current['executed_trade_count'])}笔；"
        f"A={int(current['a_trade_count'])}、C={int(current['c_trade_count'])}、"
        f"D={int(current['d_trade_count'])}、E={int(current['e_trade_count'])}、"
        f"M={int(current['m_trade_count'])}、N={int(current['n_trade_count'])}。",
        f"- 胜率{current['win_rate']:.2%}，平均账户收益{current['avg_return']:.2%}，"
        f"中位数{current['median_return']:.2%}，逐笔复利{current['equity_multiple']:.6f}倍，"
        f"最大回撤{current['max_drawdown']:.2%}。",
        f"- 不含M的同口径参考为{int(without_m['executed_trade_count'])}笔、"
        f"{without_m['equity_multiple']:.6f}倍、最大回撤{without_m['max_drawdown']:.2%}。",
        f"- 认证状态：`{certification_status}`。M非劣门禁："
        f"{'通过' if m_noninferior else '未通过（' + m_noninferior_reason + '）'}；"
        f"{'已按既有用户风险接受保留' if (not m_noninferior and m_risk_accepted) else '未使用风险豁免'}。",
        "- 该复利是假定每笔账户净值按82.5%仓位连续放大的机械历史结果，资金容量未认证，不能作为实盘收益预期。",
        "",
        "## 五个同口径场景",
        "",
        markdown_table(summary),
        "",
        f"## 当前{int(current['executed_trade_count'])}笔分段与分年结果",
        "",
        markdown_table(current_periods),
        "",
        "## E门禁：完整组合分段对照",
        "",
        markdown_table(e_portfolio_comparison),
        "",
        *verdict_lines(
            e_portfolio_comparison,
            "E入场门禁（完整组合）",
            "gate_off",
            "gate_on",
            risk_accepted=(not e_gate_validation_passed and e_gate_risk_accepted),
        ),
        "## M：完整组合分段对照",
        "",
        markdown_table(m_portfolio_comparison),
        "",
        *verdict_lines(
            m_portfolio_comparison,
            "M补位腿（完整组合）",
            "m_off",
            "m_on",
            risk_accepted=(not m_noninferior and m_risk_accepted),
        ),
        "## N：完整组合分段对照",
        "",
        markdown_table(n_portfolio_comparison),
        "",
        "## E每日候选门禁样本",
        "",
        markdown_table(e_validation),
        "",
        "## 口径锁定",
        "",
        "- 信号日范围：20240520～20260514；同一账户严格按退出日释放资金。",
        "- D为盘中腿，按真实时序优先；其余按A>M>E>C>N选择当天唯一候选。",
        "- A/C显式扣双边佣金、过户费及卖出印花税；普通腿仓位82.5%；D保留80%成交压力折扣。",
        f"- 输出`portfolio_trades.csv`是当前{int(current['executed_trade_count'])}笔唯一正式组合样本。",
    ]
    (OUTPUT_DIR / "portfolio_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def certify_current(*, refresh_input_manifest: bool = False) -> None:
    """按当前D>A>M>E>C>N从481个信号日重新认证。"""

    write_certification(
        {
            "schema_version": 1,
            "status": "RUNNING",
            "current_executable": False,
            "scenario": "current_d_a_m_e_c_n",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "note": "正在从481个信号日重建当前组合；完成前按fail-closed处理。",
        }
    )
    sources = load_sources()
    without_e_without_m = replay(
        sources,
        entry_gate_enabled=False,
        m_enabled=False,
    )
    with_e_without_m = replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=False,
    )
    without_e_with_m = replay(
        sources,
        entry_gate_enabled=False,
        m_enabled=True,
    )
    current_daily = replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
    )
    without_n = replay(
        sources,
        entry_gate_enabled=True,
        m_enabled=True,
        n_enabled=False,
    )
    current_scenario = "current_d_a_m_e_c_n"
    summary = pd.DataFrame(
        [
            summarize(without_e_without_m, "without_e_gate_without_m"),
            summarize(with_e_without_m, "with_e_gate_without_m"),
            summarize(without_e_with_m, "without_e_gate_with_m"),
            summarize(without_n, "current_without_n"),
            summarize(current_daily, current_scenario),
        ]
    )
    summary["is_current_executable"] = summary["scenario"].eq(current_scenario)
    current_summary = summary[summary["scenario"].eq(current_scenario)].iloc[0]

    if int(current_summary["executed_trade_count"]) != EXPECTED_CURRENT_TRADE_COUNT:
        raise RuntimeError(
            f"当前组合样本数不是冻结值{EXPECTED_CURRENT_TRADE_COUNT}，拒绝发布"
        )
    if (
        abs(float(current_summary["equity_multiple"]) - EXPECTED_CURRENT_MULTIPLE)
        > 1e-9
    ):
        raise RuntimeError(
            f"当前组合复利偏离冻结值{EXPECTED_CURRENT_MULTIPLE}，拒绝发布"
        )

    runtime_config = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    portfolio_config = runtime_config.get("portfolio_certification", {})
    if [str(value).upper() for value in portfolio_config.get("strategy_priority_order", [])] != [
        "D", "A", "M", "E", "C", "N"
    ]:
        raise RuntimeError("当前配置腿序不是D>A>M>E>C>N，拒绝发布")

    e_validation = e_entry_gate_validation(sources)
    required_e_splits = e_validation[e_validation["split"].ne("全部")]
    e_gate_validation_passed = not bool(
        (required_e_splits["removed_avg_return"] >= 0).any()
        or (required_e_splits["optimized_vs_base"] <= 0).any()
    )
    e_config = runtime_config.get("strategy_e", {})
    e_gate_risk_accepted = bool(e_config.get("full_sample_gate_risk_accepted", False))
    if not e_gate_validation_passed and not e_gate_risk_accepted:
        raise RuntimeError("E候选门禁验证未通过且未明确接受风险，拒绝发布")

    e_portfolio_comparison = segment_comparison(
        without_e_with_m,
        current_daily,
        before_label="gate_off",
        after_label="gate_on",
    )
    e_portfolio_noninferior, e_portfolio_reason = noninferiority_passes(
        e_portfolio_comparison, "gate_off", "gate_on"
    )
    if not e_portfolio_noninferior and not e_gate_risk_accepted:
        raise RuntimeError(f"E组合非劣门禁未通过：{e_portfolio_reason}")

    m_portfolio_comparison = segment_comparison(
        with_e_without_m,
        current_daily,
        before_label="m_off",
        after_label="m_on",
    )
    m_noninferior, m_noninferior_reason = noninferiority_passes(
        m_portfolio_comparison, "m_off", "m_on"
    )
    m_config = runtime_config.get("strategy_m", {})
    m_live_enabled = bool(m_config.get("enabled", False)) and bool(
        m_config.get("live_order_enabled", False)
    )
    m_risk_accepted = bool(m_config.get("live_noninferiority_override", False))
    certification_status = resolve_m_release_status(
        m_live_enabled=m_live_enabled,
        m_noninferior=m_noninferior,
        risk_accepted=m_risk_accepted,
        noninferiority_reason=m_noninferior_reason,
    )
    if (not e_gate_validation_passed or not e_portfolio_noninferior) and e_gate_risk_accepted:
        certification_status = "PASS_WITH_RISK_ACCEPTANCE"

    n_config = runtime_config.get("strategy_n", {})
    n_live_enabled = bool(n_config.get("enabled", False)) and bool(
        n_config.get("live_order_enabled", False)
    )
    n_risk_accepted = bool(n_config.get("live_research_risk_accepted", False))
    if not n_live_enabled:
        raise RuntimeError("N当前未同时开启enabled/live_order_enabled，拒绝发布新组合")
    if not n_risk_accepted:
        raise RuntimeError("N小样本研究风险尚未显式接受，拒绝发布")
    if not bool(n_config.get("supplement_enabled", False)):
        raise RuntimeError("N双分支挑战者未开启supplement_enabled，拒绝发布")
    if [str(value) for value in n_config.get("supplement_filter_columns", [])] != [
        "market_chain_count_bucket",
        "market_emotion_state_bucket",
    ]:
        raise RuntimeError("N补充分支筛选字段漂移，拒绝发布")
    if n_config.get("supplement_filter_values") != [["3_8"], ["mixed"]]:
        raise RuntimeError("N补充分支筛选值漂移，拒绝发布")
    if [str(value) for value in n_config.get("supplement_rank_columns", [])] != [
        "amount",
        "circ_mv",
        "ts_code",
    ]:
        raise RuntimeError("N补充分支排序字段漂移，拒绝发布")
    if [bool(value) for value in n_config.get("supplement_rank_ascending", [])] != [
        False,
        True,
        True,
    ]:
        raise RuntimeError("N补充分支排序方向漂移，拒绝发布")
    certification_status = "PASS_WITH_RISK_ACCEPTANCE"

    n_portfolio_comparison = segment_comparison(
        without_n,
        current_daily,
        before_label="n_off",
        after_label="n_on",
    )

    without_e_with_m.to_csv(
        OUTPUT_DIR / "portfolio_daily_before_gate.csv", index=False, encoding="utf-8-sig"
    )
    current_daily.to_csv(
        OUTPUT_DIR / "portfolio_daily_after_e_gate.csv", index=False, encoding="utf-8-sig"
    )
    current_daily.to_csv(
        OUTPUT_DIR / "portfolio_daily.csv", index=False, encoding="utf-8-sig"
    )
    current_trades = current_daily[current_daily["status"].astype(str).eq("EXECUTED")]
    current_trades.to_csv(
        OUTPUT_DIR / "portfolio_trades.csv", index=False, encoding="utf-8-sig"
    )
    current_periods = period_metrics(current_daily)
    current_periods.to_csv(
        OUTPUT_DIR / "portfolio_period_metrics.csv", index=False, encoding="utf-8-sig"
    )
    summary.to_csv(OUTPUT_DIR / "portfolio_summary.csv", index=False, encoding="utf-8-sig")
    e_validation.to_csv(
        OUTPUT_DIR / "e_entry_gate_validation.csv", index=False, encoding="utf-8-sig"
    )
    e_portfolio_comparison.to_csv(
        OUTPUT_DIR / "e_gate_portfolio_validation.csv", index=False, encoding="utf-8-sig"
    )
    m_portfolio_comparison.to_csv(
        OUTPUT_DIR / "m_leg_portfolio_validation.csv", index=False, encoding="utf-8-sig"
    )
    n_portfolio_comparison.to_csv(
        OUTPUT_DIR / "n_leg_portfolio_validation.csv", index=False, encoding="utf-8-sig"
    )
    write_current_report(
        summary,
        current_periods,
        e_validation,
        e_portfolio_comparison,
        m_portfolio_comparison,
        n_portfolio_comparison,
        current_scenario=current_scenario,
        certification_status=certification_status,
        e_gate_validation_passed=e_gate_validation_passed,
        e_gate_risk_accepted=e_gate_risk_accepted,
        m_noninferior=m_noninferior,
        m_noninferior_reason=m_noninferior_reason,
        m_risk_accepted=m_risk_accepted,
    )

    manifest_path = write_input_manifest(refresh=refresh_input_manifest)
    input_files = [manifest_path.relative_to(PROJECT_ROOT).as_posix()]
    certification = {
        "schema_version": 1,
        "status": certification_status,
        "current_executable": True,
        "scenario": current_scenario,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input_start_date": str(current_daily["signal_date"].min()),
        "input_end_date": str(current_daily["signal_date"].max()),
        "signal_day_count": int(current_summary["signal_day_count"]),
        "executed_trade_count": int(current_summary["executed_trade_count"]),
        "a_trade_count": int(current_summary["a_trade_count"]),
        "c_trade_count": int(current_summary["c_trade_count"]),
        "d_trade_count": int(current_summary["d_trade_count"]),
        "e_trade_count": int(current_summary["e_trade_count"]),
        "m_trade_count": int(current_summary["m_trade_count"]),
        "n_trade_count": int(current_summary["n_trade_count"]),
        "strategy_priority_order": ["D", "A", "M", "E", "C", "N"],
        "equity_multiple": float(current_summary["equity_multiple"]),
        "win_rate": float(current_summary["win_rate"]),
        "avg_return": float(current_summary["avg_return"]),
        "median_return": float(current_summary["median_return"]),
        "max_drawdown": float(current_summary["max_drawdown"]),
        "max_profit": float(current_summary["max_profit"]),
        "max_loss": float(current_summary["max_loss"]),
        "profit_loss_ratio": float(current_summary["profit_loss_ratio"]),
        "max_consecutive_losses": int(current_summary["max_consecutive_losses"]),
        "fixed_initial_notional_multiple": float(
            current_summary["fixed_initial_notional_multiple"]
        ),
        "theoretical_ending_equity": float(current_summary["theoretical_ending_equity"]),
        "theoretical_next_order_amount": float(
            current_summary["theoretical_next_order_amount"]
        ),
        "capacity_certified": False,
        "m_live_enabled": m_live_enabled,
        "m_noninferiority_passed": m_noninferior,
        "m_noninferiority_reason": m_noninferior_reason,
        "m_live_risk_accepted": m_risk_accepted,
        "m_live_risk_acceptance_note": m_config.get(
            "live_noninferiority_override_note", ""
        ),
        "n_live_enabled": n_live_enabled,
        "n_research_risk_accepted": n_risk_accepted,
        "n_research_risk_acceptance_note": n_config.get(
            "live_research_risk_acceptance_note", ""
        ),
        "e_strategy_leg": "E",
        "e_strategy_variant": str(e_config.get("strategy_variant", "E_CURRENT")),
        "e_complete_sample_candidate_count_before_gate": 102,
        "e_complete_sample_candidate_count_after_gate": 82,
        "e_gate_candidate_validation_passed": e_gate_validation_passed,
        "e_gate_portfolio_noninferiority_passed": e_portfolio_noninferior,
        "e_gate_portfolio_noninferiority_reason": e_portfolio_reason,
        "e_gate_risk_accepted": e_gate_risk_accepted,
        "e_gate_risk_acceptance_note": e_config.get(
            "full_sample_gate_risk_acceptance_note", ""
        ),
        "config_sha256": certification_config_sha256(runtime_config),
        "code_files": CODE_CERTIFICATION_FILES,
        "code_sha256": certification_files_sha256(PROJECT_ROOT, CODE_CERTIFICATION_FILES),
        "input_files": input_files,
        "input_sha256": certification_files_sha256(PROJECT_ROOT, input_files),
        "note": (
            "当前正式组合按D>A>M>E>C>N从完整481日逐日回放。"
            "M在自然年2025存在收益劣段，按用户既有风险接受保留。"
            "机械复利和82.5%仓位的资金容量未认证，不代表未来收益。"
        ),
    }
    write_certification(certification)
    print("当前可执行组合认证完成")
    print(summary.to_string(index=False))
    print("\nE组合分段验证")
    print(e_portfolio_comparison.to_string(index=False))
    print("\nM组合分段验证")
    print(m_portfolio_comparison.to_string(index=False))
    print("\nN组合分段验证")
    print(n_portfolio_comparison.to_string(index=False))


def main(*, refresh_input_manifest: bool = False) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    certify_current(refresh_input_manifest=refresh_input_manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="认证当前可执行组合")
    parser.add_argument(
        "--refresh-input-manifest",
        action="store_true",
        help="确认输入数据版本变化后，显式刷新锁定清单",
    )
    arguments = parser.parse_args()
    main(refresh_input_manifest=arguments.refresh_input_manifest)
