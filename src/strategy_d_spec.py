"""策略D回测与实盘共用的不可变候选规范。

这里仅放两端都能逐字段复现的规则。任何放宽都必须先修改本文件、重跑D回测和
完整组合认证，禁止实盘脚本自行增加候选。
"""

from __future__ import annotations

from typing import Any

import pandas as pd


D_SENTIMENT_LEVEL = "strong"
D_BOARD_TYPE = "multi_open"
D_MIN_OPEN_TIMES = 2
D_MAX_OPEN_TIMES = 3
D_PREFERRED_OPEN_TIMES = 2
D_FIRST_TIME_BUCKETS = frozenset({"midday", "afternoon", "late"})
D_TAIL_RESEAL_HHMM = 1400
D_MIN_FILL_PROBABILITY = 0.80
# 历史D候选依赖完整的首次封板、炸板次数和最后回封路径。正常实盘从连续竞价
# 开始跟踪；正式FACTOR_UNION晚启动时可以用QMT完整1m历史回补，禁止用当前快照
# 代替早盘路径。LEGACY模式仍必须从起点连续跟踪。
D_TRACKING_START_HHMM = 930
D_SIGNAL_START_HHMM = D_TAIL_RESEAL_HHMM
D_ORDER_CANCEL_HHMM = 1455
D_LATEST_COMPLETE_HISTORY_START_HHMM = D_TRACKING_START_HHMM
# D扫描正常每30秒完成一轮。75秒覆盖一次keeper检测+进程/QMT短重连，
# 但不允许把数分钟甚至午后长时间断档误当成连续路径。
D_CHECKPOINT_MAX_AGE_SECONDS = 75


def classify_first_time_bucket_hhmm(value: int) -> str:
    """把实盘HHMM首次封板时间映射到历史清洗的first_time_bucket。"""

    if value <= 0:
        return "unknown"
    if value <= 931:
        return "open_limit"
    if value < 1000:
        return "early_morning"
    if value < 1400:
        return "midday"
    if value < 1430:
        return "afternoon"
    return "late"


def common_candidate_rejection_reason(
    *,
    open_times: int,
    first_seal_hhmm: int,
    last_seal_hhmm: int | None = None,
    require_tail_reseal: bool = False,
) -> str:
    """返回回测/实盘共有字段的首个拒绝原因；空字符串表示通过。"""

    if open_times < D_MIN_OPEN_TIMES:
        return f"炸板次数{open_times}低于回测multi_open下限{D_MIN_OPEN_TIMES}"
    if open_times > D_MAX_OPEN_TIMES:
        return f"炸板次数{open_times}超过回测上限{D_MAX_OPEN_TIMES}"
    bucket = classify_first_time_bucket_hhmm(first_seal_hhmm)
    if bucket not in D_FIRST_TIME_BUCKETS:
        return f"首次封板时段{bucket}不在回测允许范围{sorted(D_FIRST_TIME_BUCKETS)}"
    if require_tail_reseal and int(last_seal_hhmm or 0) < D_TAIL_RESEAL_HHMM:
        return (
            f"最后回封时间{int(last_seal_hhmm or 0):04d}早于回测下限"
            f"{D_TAIL_RESEAL_HHMM:04d}"
        )
    return ""


def historical_candidate_mask(
    data: pd.DataFrame,
    *,
    min_fill_probability: float = D_MIN_FILL_PROBABILITY,
    allowed_segments: set[str] | None = None,
) -> pd.Series:
    """生成D历史候选布尔掩码，供所有D回测脚本复用。"""

    required = {
        "limit_times",
        "is_st",
        "market_sentiment_level",
        "board_type",
        "open_times",
        "first_time_bucket",
        "last_time",
        "fill_probability",
        "is_fill_score_reliable",
        "market_segment",
    }
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError(f"D候选数据缺少字段: {','.join(missing)}")

    is_st = data["is_st"].astype(str).str.lower().isin({"true", "1", "yes"})
    fill_reliable = (
        data["is_fill_score_reliable"]
        .astype(str)
        .str.lower()
        .isin({"true", "1", "yes"})
    )
    mask = (
        pd.to_numeric(data["limit_times"], errors="coerce").eq(1)
        & ~is_st
        & data["market_sentiment_level"].astype(str).eq(D_SENTIMENT_LEVEL)
        & data["board_type"].astype(str).eq(D_BOARD_TYPE)
        & pd.to_numeric(data["open_times"], errors="coerce").between(
            D_MIN_OPEN_TIMES, D_MAX_OPEN_TIMES
        )
        & data["first_time_bucket"].astype(str).isin(D_FIRST_TIME_BUCKETS)
        & pd.to_numeric(data["last_time"], errors="coerce").ge(
            D_TAIL_RESEAL_HHMM * 100
        )
        & pd.to_numeric(data["fill_probability"], errors="coerce").ge(
            min_fill_probability
        )
        & fill_reliable
    )
    if allowed_segments:
        mask &= data["market_segment"].astype(str).isin(allowed_segments)
    return mask.fillna(False)


def d_rank_key(
    *, open_times: int, fd_amount_to_circ_mv: float, ts_code: str
) -> tuple[int, float, str]:
    """D唯一排序：优先炸板2次，再按封单市值比和股票代码稳定排序。"""

    return (
        int(int(open_times) == D_PREFERRED_OPEN_TIMES),
        float(fd_amount_to_circ_mv),
        str(ts_code),
    )


def live_sentiment_is_historical_strong(
    sealed_count: int,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    """实时封板数代理历史`strong`桶；上下界都必须满足。"""

    return int(minimum) <= int(sealed_count) <= int(maximum)


def intraday_history_is_complete(session_start_hhmm: int) -> bool:
    """判断内存路径是否从09:30开始；False表示必须检查点恢复或QMT 1m回补。"""

    return int(session_start_hhmm) <= D_LATEST_COMPLETE_HISTORY_START_HHMM


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = pd.to_numeric(value, errors="coerce")
        return default if pd.isna(number) else float(number)
    except (TypeError, ValueError):
        return default
