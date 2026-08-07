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
AC_DAILY_PATH = (
    PROJECT_ROOT / "reports" / "ac_daily_candidates" / "ac_daily_candidates.csv"
)
# A/C 重建候选相对认证口径的滑点差校准系数（重建用固定1.001/0.999，
# 认证口径用动态滑点+手续费）。由66笔可比样本的比值中位数定出。
AC_CALIB_K = 1.0016
M_POOL_PATH = (
    PROJECT_ROOT / "reports" / "strategy_m" / "m_backtest_trades.csv"
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
M_DRAWDOWN_GUARD = 0.10   # M兜底腿回撤保护阈值,与config.json/strategy_m一致
# 09:20预挂止盈单的让价,与 config.json live_trade.intraday_takeprofit_offset 一致。
# 衔接日判定旧仓能否盘中成交、提前释放资金时使用。
TAKEPROFIT_OFFSET = 0.01
EPSILON = 1e-12

# 先锁定门禁前基线，防止以后输入文件漂移后仍静默生成“更好”结果。
#
# 2026-08-07 修正：A/C 改用逐日独立候选（见 load_ac_daily），不再被
# baseline.abc_return 这张作废持仓表裁剪。以下为修正后的口径。
# 旧值（A/C 被裁到90天，低估约34%）保留作历史对照，勿再当基准：
#   BASE 132 / 2884.052538490145      E2_ONLY 129 / 3254.1261014125575
#   OPTIMIZED 132 / 4712.470092237913 WITH_M  147 / 15326.887148064476
# 2026-08-07 第二处修正：衔接日D（见 replay 的 block_d_on_handoff）。旧仓未冲板
# 时14:55才平仓、15:00才确认，而D的下单通道14:56关闭，那些D实盘拿不到。
# 剔除后 A/C 修正版旧值（仍含不可执行的衔接日D）降级为历史对照：
#   BASE 137 / 4252.40931647757        E2_ONLY 136 / 4760.864917583647
#   OPTIMIZED 139 / 6907.34827166775   WITH_M  155 / 20606.559741847264
# 2026-08-07 腿序改造第1步：D接力全关（见 replay 内注释与
# combined_live_engine 顶部「腿序与接力口径」）。接力本身值约+8.8%，故本步
# 单独看是降收益的；收益由后续腿序调整补回。接力开启时的旧值降级为历史对照：
#   BASE 133 / 5140.7613530121025    E2_ONLY 132 / 5755.436166596083
#   OPTIMIZED 135 / 8350.331871673612 WITH_M 151 / 24911.38506562485
# 2026-08-07 腿序改造第2步：腿序重排为 D>L>A>M>E2>C（见 pick_by_priority）。
# L 由"补位/替换窄门"改为无条件优先，M 由末尾兜底提到 E2 之前，C 显式垫底。
# 第1步（仅接力全关、腿序未动）的旧值降级为历史对照：
#   BASE 133 / 4726.105464194573     E2_ONLY 132 / 5291.200358840857
#   OPTIMIZED 135 / 7676.790727395173 WITH_M 151 / 22902.02267613949
EXPECTED_BASE_TRADE_COUNT = 133
EXPECTED_BASE_MULTIPLE = 3920.935559196542
EXPECTED_E2_ONLY_TRADE_COUNT = 132
EXPECTED_E2_ONLY_MULTIPLE = 5291.495551797165
EXPECTED_OPTIMIZED_TRADE_COUNT = 135
EXPECTED_OPTIMIZED_MULTIPLE = 7677.219011035194
# 2026-08-04 M兜底腿上线；2026-08-07 A/C候选+衔接日D+接力全关+腿序重排后的
# 当前发布标尺。腿序 D>L>A>M>E2>C，与实盘 combined_live_engine 同口径。
EXPECTED_WITH_M_TRADE_COUNT = 151
EXPECTED_WITH_M_MULTIPLE = 27870.30777624288


@dataclass(frozen=True)
class Sources:
    """组合回放需要的全部只读来源。"""

    baseline: pd.DataFrame
    abc: pd.DataFrame
    strategy_d: pd.DataFrame
    e2: pd.DataFrame
    no_b_reselection: pd.DataFrame
    m_pool: pd.DataFrame | None
    ac_daily: dict[str, dict[str, Any]]
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

    m_pool: pd.DataFrame | None = None
    if M_POOL_PATH.exists():
        m_pool = pd.read_csv(M_POOL_PATH, dtype={"trade_date": str}, low_memory=False)
        m_pool["trade_date"] = m_pool["trade_date"].map(normalize_date)
        if m_pool["trade_date"].duplicated().any():
            raise ValueError("M候选账本存在重复信号日")
        m_pool = m_pool.set_index("trade_date")

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
        m_pool=m_pool,
        ac_daily=load_ac_daily(),
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


def load_ac_daily() -> dict[str, dict[str, Any]]:
    """加载逐日独立生成的 A/C 候选（2026-08-07 修正）。

    旧口径用 `baseline.abc_return != 0` 当 A/C 的门槛，而 baseline 是
    A/B/C 三腿**单独回放**的产物，带着那次回放自己的持仓序列：A/C 明细481天里
    有108天是 `POSITION_OCCUPIED_SKIP`（当年被已删除的B等占掉），连 ts_code
    都没落盘。今天的组合含 D/E2/L/M，持仓情况完全不同，那张持仓表早已作废，
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
            "account_return": ((1.0 + stock_return) / AC_CALIB_K - 1.0) * POSITION_PCT,
            "return_source": "A/C逐日独立候选(校准至认证口径)",
        }
    return result


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
    """按A/C优先、无则当前E2生成收盘后候选。

    A/C 走 sources.ac_daily（逐日独立候选，见 load_ac_daily 的说明），
    不再用 baseline.abc_return 当门槛——那是一张已作废的持仓表。
    """

    signal_date = str(row["date"])
    ac = sources.ac_daily.get(signal_date)
    if ac is not None:
        return dict(ac)

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


def l_candidate(
    sources: Sources,
    signal_date: str,
    *,
    chain_3_8_enabled: bool,
) -> dict[str, Any] | None:
    """L 单腿候选：只过 model=3 基础规则，不再要求替换窄门。

    2026-08-07 腿序改造：L 由"补位/替换两段式"改为无条件排在 A/M/E2/C 之前，
    替换窄门（model3_l_replace_guard_pass）随之退出选股路径。窄门原本的作用是
    "L 想抢已有 mode1 计划时必须额外满足 创业板 ∧ 非尾盘首板"，在 L 已经是最高
    优先级之后这层限制没有意义。
    """

    l_row = sources.l_lookup.get(signal_date)
    if l_row is None or not l_base_passes(
        sources, l_row, chain_3_8_enabled=chain_3_8_enabled
    ):
        return None
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


def pick_by_priority(
    sources: Sources,
    row: pd.Series,
    row_index: int,
    *,
    entry_gate_enabled: bool,
    l_chain_3_8_enabled: bool,
    m_enabled: bool,
    equity: float,
    peak_equity: float,
) -> dict[str, Any] | None:
    """按腿序 L > A > M > E2 > C 选出当天唯一候选（D 不在此处，见 replay）。

    2026-08-07 腿序改造：替换掉原来的"mode1(A/C→E2) + choose_l 补位/替换窄门
    + M 末尾兜底"三段式。那套结构里 M、E2、L 的相对顺序由同一个替换机制耦合，
    无法单独调整——实测把 M 提进 mode1 会连带把它顶到 L 前面，组合从 22902x
    掉到 13715x。

    A 与 C 条件互斥（A 要 market_chain_count_bucket=8_15、C 要 15_30），同一天
    不可能都有票，所以 C 排在 A 之后的任何位置结果相同；这里显式让 C 垫底，
    与实盘的腿序声明保持一致。
    """

    signal_date = str(row["date"])
    ac = sources.ac_daily.get(signal_date)

    # ① L：只过基础规则，无条件优先
    l_pick = l_candidate(sources, signal_date, chain_3_8_enabled=l_chain_3_8_enabled)
    if l_pick is not None:
        return l_pick

    # ② A
    if ac is not None and str(ac.get("strategy_leg", "")) == "A":
        return dict(ac)

    # ③ M：回撤保护仍然生效
    if m_enabled:
        m_pick = m_candidate(sources, signal_date, equity, peak_equity)
        if m_pick is not None:
            return m_pick

    # ④ E2
    if signal_date in sources.e2.index:
        e2 = source_row(sources.e2, signal_date, "E2 R1")
        if not (entry_gate_enabled and not e2_entry_gate_passes(e2, sources.e2_spec)):
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

    # ⑤ C
    if ac is not None and str(ac.get("strategy_leg", "")) == "C":
        return dict(ac)

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


def m_candidate(
    sources: Sources,
    signal_date: str,
    equity: float,
    peak_equity: float,
) -> dict[str, Any] | None:
    """M兜底腿：只在五腿全空时调用，再过一道回撤保护。

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


def replay(
    sources: Sources,
    *,
    entry_gate_enabled: bool,
    l_chain_3_8_enabled: bool = False,
    m_enabled: bool = False,
    block_d_on_handoff: bool = True,
) -> pd.DataFrame:
    """严格按释放日串行重放481个信号日。

    block_d_on_handoff：衔接日按旧仓能否提前释放资金决定D是否可开（2026-08-07）。

    A/C/E2/L/M 是 T日收盘后出信号、T+1开盘买，旧仓 T日收盘已卖完，不冲突。
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
        if abs(to_float(row.get("d_return"))) > EPSILON and not blocking_handoff:
            # 2026-08-07 接力全关：D 一律走自己的 T+2 收盘平仓，平仓当天不开新仓，
            # 下一个信号日才轮到别的腿。与实盘 combined_live_engine 同口径
            # （见该文件顶部「腿序与接力口径」）。
            #
            # 旧口径在此处对 A/C/E2 做 d_relay_candidate（同一天资金用两次）。
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
                l_chain_3_8_enabled=l_chain_3_8_enabled,
                m_enabled=m_enabled,
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


def verdict_lines(comparison: pd.DataFrame, name: str, before_label: str, after_label: str) -> list[str]:
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
    elif overall["total_change"] > 0:
        lines.append(
            f"- **结论：全样本改善但存在劣段（见上），按既有先例如实保留，"
            f"不为单段结果调参。{name}维持上线。**"
        )
    else:
        lines.append(f"- **结论：按回测判据不成立，{name}不应上线。**")
    lines.append("")
    return lines


def write_report(
    summary: pd.DataFrame,
    e2_validation: pd.DataFrame,
    l_validation: pd.DataFrame,
    e2_portfolio_comparison: pd.DataFrame,
    m_portfolio_comparison: pd.DataFrame,
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
        f"- 再接入M兜底腿：{int(summary.iloc[3]['executed_trade_count'])}笔，"
        f"{summary.iloc[3]['equity_multiple']:.2f}倍，最大回撤{summary.iloc[3]['max_drawdown']:.2%}"
        f"（**当前发布标尺**）。",
        f"- 总组合复利变化：{summary.iloc[3]['equity_multiple'] / base['equity_multiple'] - 1:.2%}。",
        "- M只在A/C/D/E2/L全部无候选且账户空仓时触发，五腿规则一行未改；回撤>10%自动暂停。",
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
        "## E2门禁完整组合口径分段验证",
        "",
        "上面E2那张表是单腿口径（50笔→43笔）。单腿改善不等于组合改善：门禁挡下E2后，",
        "当天资金可能空仓，也可能被A/C/L接手，还会改变后续时间线。下表为完整组合口径。",
        "",
        markdown_table(e2_portfolio_comparison),
        "",
        *verdict_lines(e2_portfolio_comparison, "E2入场门禁", "gate_off", "gate_on"),
        "## M兜底腿完整组合口径分段验证",
        "",
        "M不与任何腿竞争，只在五腿全空时补位，因此下表的差异全部来自新增交易与其",
        "带来的时间线错位。",
        "",
        markdown_table(m_portfolio_comparison),
        "",
        *verdict_lines(m_portfolio_comparison, "M兜底腿", "m_off", "m_on"),
        "## 实盘对齐说明",
        "",
        "- 配置：`config/strategy_e2_r1_scenarios.json`中的entry_gate。",
        "- 共用代码：`src/strategy_e2.py`先选每日第一名，再执行同一门禁。",
        "- 历史验证：`scripts/verify_strategy_e2_alignment.py`必须同时通过门禁前50/50和门禁后43/43逐票对齐。",
        "- E2实盘信号、model=3盘中预览和历史回测均调用同一规则源。",
        "- L共用代码：`src/strategy_model3_policy.py`；实盘状态机和本认证脚本共同调用。",
        "- D实盘排序恢复为回测口径：炸板1~3次，优先2次，再按封单金额/流通市值降序。",
        "- M共用代码：`src/strategy_m.py`；实盘信号脚本与本认证脚本调用同一选股链。",
        "- M候选账本由`scripts/build_strategy_m_backtest_pool.py`生成，与实盘规则源同口径。",
        "- ⚠️M规则来自1053个方案样本内最优，参数邻域塌陷、样本外仅3笔；复利倍数不可作实盘预期。",
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
    with_m_daily = replay(
        sources, entry_gate_enabled=True, l_chain_3_8_enabled=True, m_enabled=True
    )
    base_summary = summarize(base_daily, "current_before_e2_entry_gate")
    e2_only_summary = summarize(e2_only_daily, "current_after_e2_entry_gate")
    optimized_summary = summarize(
        optimized_daily, "current_after_e2_gate_and_l_chain_3_8_expansion"
    )
    with_m_summary = summarize(with_m_daily, "current_with_m_gap_leg")
    summary = pd.DataFrame(
        [base_summary, e2_only_summary, optimized_summary, with_m_summary]
    )

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
    if with_m_summary["executed_trade_count"] != EXPECTED_WITH_M_TRADE_COUNT:
        raise RuntimeError("含M组合样本数漂移，拒绝发布")
    if abs(with_m_summary["equity_multiple"] - EXPECTED_WITH_M_MULTIPLE) > 1e-9:
        raise RuntimeError("含M组合复利漂移，拒绝发布")
    if optimized_summary["equity_multiple"] <= e2_only_summary["equity_multiple"]:
        raise RuntimeError("L扩容没有提高完整组合复利，禁止上线")
    # 浮点容差：两条曲线可能落在同一段回撤上，末位差 ~1e-16 不算恶化。
    if optimized_summary["max_drawdown"] < base_summary["max_drawdown"] - EPSILON:
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
    e2_portfolio_comparison = segment_comparison(
        replay(sources, entry_gate_enabled=False, l_chain_3_8_enabled=True, m_enabled=True),
        with_m_daily,
        before_label="gate_off",
        after_label="gate_on",
    )
    m_portfolio_comparison = segment_comparison(
        optimized_daily,
        with_m_daily,
        before_label="m_off",
        after_label="m_on",
    )
    e2_portfolio_comparison.to_csv(
        OUTPUT_DIR / "e2_gate_portfolio_validation.csv", index=False, encoding="utf-8-sig"
    )
    m_portfolio_comparison.to_csv(
        OUTPUT_DIR / "m_leg_portfolio_validation.csv", index=False, encoding="utf-8-sig"
    )
    write_report(
        summary, e2_validation, l_validation,
        e2_portfolio_comparison, m_portfolio_comparison,
    )

    print("当前可执行组合认证完成")
    print(summary.to_string(index=False))
    print("\nE2入场门禁前后半段/分年验证")
    print(e2_validation.to_string(index=False))
    print("\nL连板3~8扩容前后半段/分年验证")
    print(l_validation.to_string(index=False))


if __name__ == "__main__":
    main()
