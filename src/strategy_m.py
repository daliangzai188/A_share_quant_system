"""策略M（补位腿）共用规则。

M 是补位腿，当前腿序为 D>A>M>E>C：只有账户空仓且D/A均未占用时
才进入M判断；M会排在E/C之前。M没有通过全部分段回撤非劣门禁，但用户基于
26笔样本、平均收益和组合复利明确接受风险，当前配置恢复``live_order_enabled=true``。
这不是门禁通过；10%回撤保护、82.5%仓位及全部成交/风控门禁继续生效。

当前冻结回放为481个信号日、M实际入选26笔；M只吃满足“深市主板情绪偏弱”且
没有被D/A占用的日期。

本模块只做无副作用计算，不读账户、不连 QMT、不提交委托。实盘信号脚本与历史
认证脚本必须共同调用这里，避免同一条规则在两处手写后漂移。

⚠️ 过拟合风险（必须与收益数字一起阅读）：
    M 的规则是从约 1053 个候选方案中按组合复利挑出的第一名。参数邻域检验显示
    "流通市值最小"这一排序显著优于其余 8 种（次优仅 1.13 倍），T+2 亦显著优于
    T+3——**单点最优、邻域塌陷是过拟合的典型指纹**。两个随机对照（各 500 次）
    500次对照中虽然没有超过观测值，但经验p值不能写成0；按plus-one口径上界约
    为1/501=0.001996，且仍未校正1053次方案搜索。样本外仅3笔，必须小资金前向验证。
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd


M_VERSION = "M_gap_day_sz_main_weak_min_circ_mv_v1"

# 历史回放锁定值（reports/current_portfolio_alignment 口径，仓位82.5%）。
# 任何改动后若这些数字变化，说明输入或规则漂移，必须先查清原因。
M_RESEARCH_AUDIT = {
    "window": "20240520~20260514",
    "m_trade_count": 26,
    "m_avg_account_return": 0.061991,
    "m_median_account_return": 0.020878,
    "m_win_rate": 0.538462,
    "m_standalone_multiple": 4.163881,
    "portfolio_without_m": 7677.946823,
    "portfolio_with_m_dd_guard": 29388.980134,
    "portfolio_max_drawdown_without_m": -0.235124,
    "portfolio_max_drawdown_with_m_dd_guard": -0.235585,
    "downside_if_m_expectation_zero": 7058.074525,
    "random_stock_control_p_upper_bound": 0.001996,
    "random_day_control_p_upper_bound": 0.001996,
    "out_of_sample_trades": 3,
    "live_order_enabled": True,
    "overfit_warning": (
        "规则来自1053个方案的样本内最优；排序与持有期均为单点最优、邻域塌陷。"
        "倍数不可作为实盘预期，只可作为方向性证据。"
    ),
}

DEFAULT_SPEC: dict[str, Any] = {
    "enabled": False,
    "live_order_enabled": False,
    "sentiment_column": "sz_main_market_sentiment_level",
    "sentiment_required": "weak",
    "rank_column": "circ_mv",
    "rank_ascending": True,
    "exit_hold_offset": 2,
    "position_pct": 0.825,
    "drawdown_guard_pct": 0.10,
    "require_all_legs_idle": True,
}


def load_m_spec(config: Mapping[str, Any]) -> dict[str, Any]:
    """从完整 config.json 或单独的 strategy_m 段读取规则，缺项用默认值。"""

    raw = config.get("strategy_m") if isinstance(config.get("strategy_m"), Mapping) else config
    spec = dict(DEFAULT_SPEC)
    for key in DEFAULT_SPEC:
        if key in raw:
            spec[key] = raw[key]
    return spec


def _bool_series(frame: pd.DataFrame, column: str, default: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="bool")
    return frame[column].astype(str).str.lower().isin(["true", "1"])


def apply_base_filters(pool: pd.DataFrame) -> pd.DataFrame:
    """与 E 实盘一致的数据质量、成交可靠性与 ST 过滤。

    这几条是全系统共同的可买性底线，不是 M 自己的选股条件。
    """

    if pool.empty:
        return pool
    result = pool.copy()
    if "limit_data_quality" in result.columns:
        result = result[result["limit_data_quality"].fillna("").astype(str).eq("full")]
    result = result[_bool_series(result, "strategy_compatible", False)]
    result = result[_bool_series(result, "allow_buy_reliable", False)]
    result = result[_bool_series(result, "is_fill_score_reliable", False)]
    result = result[~_bool_series(result, "is_st", False)]
    if "name" in result.columns:
        names = result["name"].fillna("").astype(str).str.upper()
        result = result[~(names.str.contains("ST", regex=False) | names.str.contains("退", regex=False))]
    return result.copy()


def sentiment_gate_passed(day_rows: pd.DataFrame, spec: Mapping[str, Any]) -> tuple[bool, str]:
    """判断当日市场情绪是否允许 M 触发。

    字段缺失时按安全口径拒绝——宁可不做，不做没有依据的交易。
    """

    column = str(spec.get("sentiment_column", DEFAULT_SPEC["sentiment_column"]))
    required = str(spec.get("sentiment_required", DEFAULT_SPEC["sentiment_required"]))
    if day_rows.empty:
        return False, "当日涨停池为空"
    if column not in day_rows.columns:
        return False, f"缺少情绪字段{column}，按安全口径拒绝"
    values = day_rows[column].dropna().astype(str).unique().tolist()
    if not values:
        return False, f"{column}全为空，按安全口径拒绝"
    actual = values[0]
    if actual != required:
        return False, f"{column}={actual}，需要{required}"
    return True, f"{column}={actual}"


def drawdown_guard_passed(
    current_equity: float,
    peak_equity: float,
    spec: Mapping[str, Any],
) -> tuple[bool, str]:
    """回撤保护：账户处于深度回撤时暂停 M。

    只门控 M 这一条补位腿。**绝不可推广到 A/C/D/E**——若所有腿都被净值门控，
    回撤中将没有任何腿能创造修复净值的机会，系统会锁死在回撤里。
    """

    guard = float(spec.get("drawdown_guard_pct", DEFAULT_SPEC["drawdown_guard_pct"]))
    if guard <= 0:
        return True, "未启用回撤保护"
    if peak_equity <= 0 or current_equity <= 0:
        return False, "净值数据缺失，按安全口径暂停"
    drawdown = current_equity / peak_equity - 1.0
    if drawdown <= -guard:
        return False, f"当前回撤{drawdown:.2%}超过{guard:.0%}，M暂停"
    return True, f"当前回撤{drawdown:.2%}"


def select_m_candidate(pool: pd.DataFrame, spec: Mapping[str, Any]) -> pd.DataFrame:
    """在已通过基础过滤的当日候选中选出 M 的唯一标的。

    只使用信号日收盘后即可确定的字段，不读取任何次日价格或成交结果。
    """

    if pool.empty:
        return pool
    column = str(spec.get("rank_column", DEFAULT_SPEC["rank_column"]))
    ascending = bool(spec.get("rank_ascending", DEFAULT_SPEC["rank_ascending"]))
    if column not in pool.columns:
        return pool.iloc[0:0]
    result = pool.copy()
    result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result[result[column].notna()]
    if result.empty:
        return result
    sort_columns = [column, "ts_code"] if "ts_code" in result.columns else [column]
    ascending_flags = [ascending] + ([True] if "ts_code" in result.columns else [])
    return result.sort_values(sort_columns, ascending=ascending_flags).head(1).reset_index(drop=True)


def build_m_candidate(
    day_rows: pd.DataFrame,
    spec: Mapping[str, Any],
) -> tuple[pd.DataFrame, str]:
    """完整选股链：基础过滤 → 情绪门禁 → 选票。

    返回 (候选DataFrame, 原因说明)。候选为空时原因说明为何不触发。
    调用方仍需自行确认"排在M前面的腿(D/A)全空 + 无持仓 + 回撤保护通过"三个前提。
    （2026-08-07 腿序重排前这里写的是"五腿全空"；E 和 C 已排到 M 之后。）
    """

    if day_rows.empty:
        return day_rows, "当日涨停池为空"
    passed, reason = sentiment_gate_passed(day_rows, spec)
    if not passed:
        return day_rows.iloc[0:0], reason
    filtered = apply_base_filters(day_rows)
    if filtered.empty:
        return filtered, f"{reason}；基础过滤后无可买候选"
    picked = select_m_candidate(filtered, spec)
    if picked.empty:
        return picked, f"{reason}；缺少{spec.get('rank_column')}有效值"
    return picked, f"{reason}；候选{len(filtered)}只取流通市值最小"


def resolve_exit_offset(spec: Mapping[str, Any]) -> int:
    """M 统一 T+2 收盘退出（相对信号日的交易日偏移）。"""

    return int(spec.get("exit_hold_offset", DEFAULT_SPEC["exit_hold_offset"]))
