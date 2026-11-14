"""D盘中监控的策略参数、数据结构、配置读取和辅助函数（2026-11-15 项目优化：从 scripts/monitor_strategy_d_intraday.py 原样搬来，逻辑未改）。

scripts/monitor_strategy_d_intraday.py 把这里的名字全部导入回去，守护进程和测试照旧从那里取用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

from src.portfolio_ace_m import ACE_FULL_HARD_CAP_PCT, ACE_FULL_TARGET_PCT
from src.utils.time_utils import now_beijing, today_beijing
from src.market_rules import market_segment, price_limit_pct
from src.strategy_d_spec import (
    D_CHECKPOINT_MAX_AGE_SECONDS,
    D_FIRST_TIME_BUCKETS,
    D_MAX_OPEN_TIMES,
    D_MIN_FILL_PROBABILITY,
    D_MIN_OPEN_TIMES,
    D_ORDER_CANCEL_HHMM,
    D_PREFERRED_OPEN_TIMES,
    D_SIGNAL_START_HHMM,
    D_TAIL_RESEAL_HHMM,
    D_TRACKING_START_HHMM,
)

PROJECT_ROOT = Path(__file__).absolute().parents[2]


# ── 策略参数 ──────────────────────────────────────────────────────────────────
SENTIMENT_STRONG_MIN = 88    # 默认实时strong代理下界，最终读取config.strategy_d
SENTIMENT_STRONG_MAX = 132   # 默认实时strong代理上界；历史very_strong不属于D样本
WATCH_START_HHMM = 935       # 完整路径必须09:30开始；09:35起才允许发WATCH提醒
SIGNAL_START_HHMM = D_SIGNAL_START_HHMM  # 回测last_time下限；必须在此后真实回封
CANCEL_HHMM = D_ORDER_CANCEL_HHMM        # 冻结的D实盘撤单边界
POLL_BATCH_SIZE = 500        # 每次 get_full_tick 的股票数量
POLL_INTERVAL_SEC = 30       # 每批轮询间隔（秒）
ORDER_FILL_POLL_INTERVAL_SEC = 2  # D活动委托成交事实轮询；不参与策略信号判断
MONITOR_START_HHMM = D_TRACKING_START_HHMM  # 完整路径从连续竞价开始跟踪
D_POSITION_PCT = ACE_FULL_TARGET_PCT  # 默认目标仓位82.5%，优先使用 config.json/strategy_d/position_pct
D_RETRY_TOP_N = 1            # 严格对齐D原始回测口径：只尝试排序第1名，失败不补偿
MIN_D_VALID_LIMIT_PRICE = 1.0  # D只做正常A股涨停价；QMT异常行情可能返回0.x，必须本地拦截
MAX_D_INVALID_PRICE_TICKS = 3  # 连续3轮仍异常才判定为行情源有问题，避免单帧脏数据误伤
STRATEGY_REMARK = "D_FIRST_BOARD"
DEFAULT_ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj", "other"}


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class StockState:
    ts_code: str
    name: str = ""
    market_segment: str = ""
    upper_limit: float = 0.0
    was_sealed: bool = False       # 上次轮询时是否涨停
    ever_sealed: bool = False      # 今日曾涨停过
    open_times_today: int = 0      # 今日炸板次数
    first_seal_hhmm: int = 0       # 首次封板时间
    last_seal_hhmm: int = 0        # 最后一次封板时间（每次重封更新）
    bid_vol: int = 0               # 当前涨停封单量（股）
    circ_mv: float = 0.0           # 流通市值，单位：万元
    # 两档信号状态
    watch_alerted: bool = False    # 观察提醒已发出
    buy_signaled: bool = False     # 买入信号已发出
    order_id: str = ""
    last_order_fail_reason: str = ""
    invalid_price_ticks: int = 0   # 连续异常涨停价轮数，用于过滤QMT偶发脏行情
    last_price: float = 0.0        # 最近一次行情现价（封板时=真实涨停价，下单价自洽校验用）
    st_suspect: bool = False       # ST/风险警示嫌疑（名字带ST/退 或 QMT涨停幅≈5%）
    st_suspect_logged: bool = False  # 排除日志只打一次
    fill_probability: float = 0.0
    fill_reliable: bool = False
    fill_matched_source: str = "none"
    fill_sample_count: int = 0
    fill_min_group_samples: int = 0
    fill_reject_reason: str = "尚未计算成交概率"
    pre_close: float = 0.0
    open_price: float = 0.0
    session_high_price: float = 0.0
    session_low_price: float = 0.0
    cumulative_amount: float = 0.0
    previous_day_amount_yuan: float = 0.0
    last_break_hhmm: int = 0
    last_break_price: float = 0.0
    previous_seal_to_break_minutes: int = 0
    last_reseal_scan_round: int = 0
    matched_factor_profile_ids: str = ""
    factor_values_json: str = ""
    fill_planned_buy_amount: float = 0.0
    fill_estimated_turnover_amount: float = 0.0
    fill_current_queue_amount: float = 0.0


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def now_hhmm() -> int:
    t = now_beijing().time()
    return t.hour * 100 + t.minute


def hhmm_to_str(hhmm: int) -> str:
    return f"{hhmm // 100:02d}:{hhmm % 100:02d}"


def _read_open_dates_before_today() -> list[str]:
    calendar_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
    today_str = today_beijing().strftime("%Y%m%d")
    if not calendar_path.exists():
        return []
    try:
        calendar = pd.read_csv(calendar_path, dtype={"cal_date": str})
    except Exception:
        return []
    if "cal_date" not in calendar.columns:
        return []
    if "is_open" in calendar.columns:
        calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    dates = calendar["cal_date"].astype(str)
    return sorted(date for date in dates if date < today_str)


def _resolve_previous_limit_file(limit_dir: Path) -> Path | None:
    """按交易日解析昨日涨停文件，禁止直接取目录最新文件。"""
    today_str = today_beijing().strftime("%Y%m%d")
    open_dates = _read_open_dates_before_today()
    if open_dates:
        for trade_date in reversed(open_dates):
            candidate = limit_dir / f"{trade_date}.csv"
            if candidate.exists():
                return candidate

    dated_files = []
    for path in limit_dir.glob("*.csv"):
        if path.stem.isdigit() and len(path.stem) == 8 and path.stem < today_str:
            dated_files.append(path)
    return max(dated_files, key=lambda path: path.stem) if dated_files else None


def load_yesterday_limit_codes() -> set[str]:
    """加载昨日涨停股票代码集合，用于排除2板+。"""
    limit_dir = PROJECT_ROOT / "data" / "raw" / "limit_list"
    path = _resolve_previous_limit_file(limit_dir)
    if path is None:
        print("[警告] 未找到昨日涨停文件，首板过滤将不可用。")
        return set()
    try:
        df = pd.read_csv(path, dtype={"ts_code": str})
        if df.empty:
            print(f"[警告] 昨日涨停文件为空: {path}")
            return set()
        df = df[df["limit"].astype(str).str.upper() == "U"]
        codes = set(df["ts_code"].tolist())
        print(f"[昨日涨停] 使用文件: {path.name}，涨停数={len(codes)}")
        return codes
    except Exception as e:
        print(f"[警告] 加载昨日涨停数据失败: {path}, error={e}")
        return set()


def load_stock_universe() -> list[str]:
    """加载全市场股票代码列表。

    D 是盘中实时扫描，开盘时今日/未来日线文件可能只有表头。
    股票池只需要代码清单，应该使用今天及以前最近一个非空日线文件。
    """
    daily_dir = PROJECT_ROOT / "data" / "raw" / "daily"
    today_str = today_beijing().strftime("%Y%m%d")
    files = sorted(
        path
        for path in daily_dir.glob("*.csv")
        if path.stem.isdigit() and len(path.stem) == 8 and path.stem <= today_str
    )
    if not files:
        print("[警告] 未找到今天及以前的日线文件，D股票池为空。")
        return []
    last_error = ""
    for path in reversed(files):
        try:
            df = pd.read_csv(path, dtype={"ts_code": str})
        except Exception as e:
            last_error = f"{path.name}: {e}"
            continue
        if "ts_code" not in df.columns:
            last_error = f"{path.name}: 缺少 ts_code 字段"
            continue
        codes = df["ts_code"].dropna().astype(str).str.strip()
        codes = [code for code in codes.tolist() if code]
        if codes:
            print(f"[股票池] 使用日线文件: {path.name}，股票数={len(codes)}")
            return codes
        last_error = f"{path.name}: 空文件或无有效 ts_code"
    print(f"[警告] 今天及以前没有可用的非空日线股票池，最后错误: {last_error}")
    return []


def load_latest_circ_mv_map() -> dict[str, float]:
    """加载最近可用 daily_basic 流通市值，用于D原始口径 fd_amount_to_circ_mv 排序。"""
    basic_dir = PROJECT_ROOT / "data" / "raw" / "daily_basic"
    today_str = today_beijing().strftime("%Y%m%d")
    files = sorted(
        path
        for path in basic_dir.glob("*.csv")
        if path.stem.isdigit() and len(path.stem) == 8 and path.stem <= today_str
    )
    for path in reversed(files):
        try:
            data = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
        except Exception:
            continue
        if {"ts_code", "circ_mv"}.issubset(data.columns) and not data.empty:
            data["circ_mv"] = pd.to_numeric(data["circ_mv"], errors="coerce").fillna(0.0)
            result = dict(zip(data["ts_code"].astype(str), data["circ_mv"].astype(float)))
            print(f"[流通市值] 使用 daily_basic: {path.name}，股票数={len(result)}")
            return result
    print("[警告] 未找到可用 daily_basic 流通市值，D将只能按封单金额近似排序。")
    return {}


def load_previous_daily_amount_map() -> dict[str, float]:
    """加载今天之前最近交易日成交额，统一转换为人民币元。"""

    daily_dir = PROJECT_ROOT / "data" / "raw" / "daily"
    today_str = today_beijing().strftime("%Y%m%d")
    files = sorted(
        path
        for path in daily_dir.glob("*.csv")
        if path.stem.isdigit() and len(path.stem) == 8 and path.stem < today_str
    )
    for path in reversed(files):
        try:
            data = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
        except Exception:
            continue
        if {"ts_code", "amount"}.issubset(data.columns) and not data.empty:
            amount = pd.to_numeric(data["amount"], errors="coerce").fillna(0.0) * 1000.0
            result = dict(zip(data["ts_code"].astype(str), amount.astype(float)))
            print(f"[前日成交额] 使用日线: {path.name}，股票数={len(result)}")
            return result
    print("[警告] 未找到前一交易日成交额；使用成交额因子的D发布条件将fail-closed。")
    return {}


def classify_market_segment(ts_code: object) -> str:
    """板块（规则在 src.market_rules.market_segment）。"""
    return market_segment(ts_code)


def load_strategy_d_config(config: dict[str, Any]) -> dict[str, Any]:
    strategy_config = config.get("strategy_d", {})
    return strategy_config if isinstance(strategy_config, dict) else {}


def configured_factor_release_path(config: dict[str, Any]) -> Path:
    strategy_config = load_strategy_d_config(config)
    raw = str(
        strategy_config.get(
            "factor_release_path", "config/strategy_d_factor_release.json"
        )
    )
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def configured_allowed_segments(config: dict[str, Any]) -> set[str]:
    strategy_config = load_strategy_d_config(config)
    values = strategy_config.get("allowed_market_segments", sorted(DEFAULT_ALLOWED_SEGMENTS))
    if not isinstance(values, list):
        return set(DEFAULT_ALLOWED_SEGMENTS)
    result = {str(item).strip() for item in values if str(item).strip()}
    return result or set(DEFAULT_ALLOWED_SEGMENTS)


def configured_position_pct(config: dict[str, Any]) -> float:
    strategy_config = load_strategy_d_config(config)
    try:
        value = float(strategy_config.get("position_pct", D_POSITION_PCT))
    except (TypeError, ValueError):
        return D_POSITION_PCT
    if value <= 0:
        return D_POSITION_PCT
    return min(value, 1.0)


def configured_max_open_times(config: dict[str, Any]) -> int:
    """读取D炸板次数硬上限；显式偏离历史认证值时拒绝启动。"""

    strategy_config = load_strategy_d_config(config)
    try:
        value = int(strategy_config.get("max_open_times", D_MAX_OPEN_TIMES))
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.max_open_times无效") from exc
    if value != D_MAX_OPEN_TIMES:
        raise ValueError(
            f"config.strategy_d.max_open_times={value}偏离D回测值{D_MAX_OPEN_TIMES}"
        )
    return value


def configured_min_open_times(config: dict[str, Any]) -> int:
    strategy_config = load_strategy_d_config(config)
    try:
        value = int(strategy_config.get("min_open_times", D_MIN_OPEN_TIMES))
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.min_open_times无效") from exc
    if value != D_MIN_OPEN_TIMES:
        raise ValueError(
            f"config.strategy_d.min_open_times={value}偏离D回测值{D_MIN_OPEN_TIMES}"
        )
    return value


def configured_preferred_open_times(config: dict[str, Any]) -> int:
    """读取D排序优先炸板次数；异常配置回退到历史认证值2。"""

    strategy_config = load_strategy_d_config(config)
    try:
        value = int(
            strategy_config.get(
                "preferred_open_times", D_PREFERRED_OPEN_TIMES
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.preferred_open_times无效") from exc
    if value != D_PREFERRED_OPEN_TIMES:
        raise ValueError(
            "config.strategy_d.preferred_open_times="
            f"{value}偏离D回测值{D_PREFERRED_OPEN_TIMES}"
        )
    return value


def configured_min_fill_probability(config: dict[str, Any]) -> float:
    strategy_config = load_strategy_d_config(config)
    try:
        value = float(
            strategy_config.get(
                "min_fill_probability", D_MIN_FILL_PROBABILITY
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.min_fill_probability无效") from exc
    if abs(value - D_MIN_FILL_PROBABILITY) > 1e-12:
        raise ValueError(
            "config.strategy_d.min_fill_probability="
            f"{value}偏离D回测值{D_MIN_FILL_PROBABILITY}"
        )
    return value


def configured_first_time_buckets(config: dict[str, Any]) -> frozenset[str]:
    strategy_config = load_strategy_d_config(config)
    values = strategy_config.get(
        "first_time_buckets", sorted(D_FIRST_TIME_BUCKETS)
    )
    configured = frozenset(str(value) for value in values)
    if configured != D_FIRST_TIME_BUCKETS:
        raise ValueError(
            "config.strategy_d.first_time_buckets="
            f"{sorted(configured)}偏离D回测值{sorted(D_FIRST_TIME_BUCKETS)}"
        )
    return configured


def configured_tail_reseal_hhmm(config: dict[str, Any]) -> int:
    strategy_config = load_strategy_d_config(config)
    try:
        value = int(strategy_config.get("tail_reseal_hhmm", D_TAIL_RESEAL_HHMM))
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.tail_reseal_hhmm无效") from exc
    if value != D_TAIL_RESEAL_HHMM:
        raise ValueError(
            f"config.strategy_d.tail_reseal_hhmm={value}偏离D回测值"
            f"{D_TAIL_RESEAL_HHMM}"
        )
    return value


def configured_sentiment_bounds(config: dict[str, Any]) -> tuple[int, int]:
    strategy_config = load_strategy_d_config(config)
    try:
        minimum = int(
            strategy_config.get(
                "sentiment_current_sealed_min", SENTIMENT_STRONG_MIN
            )
        )
        maximum = int(
            strategy_config.get(
                "sentiment_current_sealed_max", SENTIMENT_STRONG_MAX
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d实时strong代理区间无效") from exc
    expected = (SENTIMENT_STRONG_MIN, SENTIMENT_STRONG_MAX)
    if (minimum, maximum) != expected:
        raise ValueError(
            "config.strategy_d实时strong代理区间="
            f"{minimum}~{maximum}偏离D认证值{expected[0]}~{expected[1]}"
        )
    return minimum, maximum


def configured_checkpoint_max_age_sec(config: dict[str, Any]) -> int:
    """D重启只允许恢复一轮扫描附近的短断点，禁止放宽成长时间缺口。"""

    strategy_config = load_strategy_d_config(config)
    try:
        value = int(
            strategy_config.get(
                "checkpoint_max_age_sec", D_CHECKPOINT_MAX_AGE_SECONDS
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("config.strategy_d.checkpoint_max_age_sec无效") from exc
    if value != D_CHECKPOINT_MAX_AGE_SECONDS:
        raise ValueError(
            "config.strategy_d.checkpoint_max_age_sec="
            f"{value}偏离D执行冻结值{D_CHECKPOINT_MAX_AGE_SECONDS}"
        )
    return value


def validate_configured_execution_clock(config: dict[str, Any]) -> None:
    """D实盘时钟必须与回测事件定义一致，任何漂移都拒绝启动。"""

    strategy_config = load_strategy_d_config(config)
    expected = {
        "tracking_start_hhmm": D_TRACKING_START_HHMM,
        "signal_start_hhmm": D_SIGNAL_START_HHMM,
        "cancel_hhmm": D_ORDER_CANCEL_HHMM,
    }
    for key, frozen_value in expected.items():
        try:
            configured_value = int(strategy_config.get(key, frozen_value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.strategy_d.{key}无效") from exc
        if configured_value != frozen_value:
            raise ValueError(
                f"config.strategy_d.{key}={configured_value}偏离D回测/执行冻结值"
                f"{frozen_value}"
            )


def filter_universe_by_segments(universe: list[str], allowed_segments: set[str]) -> list[str]:
    return [code for code in universe if classify_market_segment(code) in allowed_segments]


def limit_up_pct(ts_code: str, name: object | None = None) -> float:
    """涨停幅度（现行交易规则，规则在 src.market_rules.price_limit_pct）。

    2026-11-15 前这里另写一份，ST/退一律按5%；现行规则主板风险警示股是10%。只影响ST股票的估算，
    ST股票在后面的风险警示过滤里一律排除，D的候选和下单不变。
    """
    stock_name = "" if name is None or pd.isna(name) else str(name)
    return float(price_limit_pct(ts_code, name=stock_name) or 0.10)


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def estimate_upper_limit(ts_code: str, pre_close: float, name: object | None = None) -> float:
    if pre_close <= 0:
        return 0.0
    return round_price(pre_close * (1 + limit_up_pct(ts_code, name)))


def check_strategy_position_occupied(broker: Any | None = None) -> tuple[bool, str]:
    """检查券商是否仍实际持有本系统旧策略仓。

    本地open/sell_pending记录只负责识别策略身份，券商volume>0负责确认真实持仓；
    因此打新中签、债券和人工仓不会误阻断D。今日到期、逾期、待卖以及D自身旧仓
    都属于旧策略仓，实际清空前禁止D再次开仓。
    """
    pos_file = PROJECT_ROOT / "data" / "processed" / "positions.json"
    if not pos_file.exists():
        return False, ""
    try:
        import json
        positions = json.loads(pos_file.read_text(encoding="utf-8"))
        open_pos = [
            p for p in positions
            if str(p.get("status", "")).lower() in {"open", "sell_pending"}
        ]
        if not open_pos:
            return False, ""
        if broker is not None:
            try:
                broker_codes = {
                    str(getattr(p, "ts_code", "")).split(".")[0]
                    for p in (broker.query_positions() or [])
                    if int(getattr(p, "volume", 0) or 0) > 0
                }
            except Exception as exc:
                return True, f"券商持仓查询失败，按安全口径禁止D开仓({exc})"
            open_pos = [
                p for p in open_pos
                if str(p.get("ts_code", "")).split(".")[0] in broker_codes
            ]
            if not open_pos:
                return False, ""
        desc = ", ".join(f"{p['ts_code']}({p.get('strategy_leg','?')})" for p in open_pos)
        return True, desc
    except Exception as e:
        return True, f"读取持仓失败，按安全口径禁止D开仓({e})"


def load_stock_names() -> dict[str, str]:
    try:
        df = pd.read_csv(
            PROJECT_ROOT / "data" / "processed" / "limit_up_merged.csv",
            dtype={"ts_code": str},
            low_memory=False,
        )
        if "name" in df.columns:
            return dict(zip(df["ts_code"], df["name"]))
    except Exception:
        pass  # 可以忽略：股票名称只用于显示，读不了返回空表
    return {}


def calc_shares_below_target_amount(target_amount: float, price: float) -> int:
    """按已经完成全部资金与仓位风控的目标金额向下取整到100股。"""
    if target_amount <= 0 or price <= 0:
        return 0
    return max(int((target_amount - 0.01) / price / 100) * 100, 0)


@dataclass(frozen=True)
class DOrderCapacity:
    """D成交门和真实下单共用的同一账户金额快照。"""

    available_cash: float
    total_asset: float
    target_amount: float
    shares: int
    actual_amount: float


def calculate_d_order_capacity(
    account: Any,
    *,
    price: float,
    position_pct: float,
    live_config: dict[str, Any],
) -> DOrderCapacity:
    """按正式仓位上限计算D真实拟下单金额，供成交门和委托共同使用。"""

    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
    max_order_amount = float(live_config.get("max_single_order_amount", 0) or 0)
    max_position_pct = float(live_config.get("max_position_pct", ACE_FULL_HARD_CAP_PCT))
    max_total_position_pct = float(
        live_config.get("max_total_position_pct", ACE_FULL_TARGET_PCT)
    )
    cash_buffer = float(live_config.get("cash_buffer_amount", 1000) or 0)
    target_amount = min(
        max(available_cash - cash_buffer, 0.0),
        total_asset * float(position_pct),
        total_asset * max_total_position_pct,
        total_asset * max_position_pct,
    )
    if max_order_amount > 0:
        target_amount = min(target_amount, max_order_amount)
    shares = calc_shares_below_target_amount(target_amount, price)
    return DOrderCapacity(
        available_cash=available_cash,
        total_asset=total_asset,
        target_amount=target_amount,
        shares=shares,
        actual_amount=float(shares) * float(price),
    )


def is_d_market_context_eligible(
    state: StockState,
    *,
    yesterday_limit_codes: set[str],
    allowed_segments: set[str],
) -> bool:
    """判定单票是否属于D回测广度的非ST首板母池。"""

    return bool(
        not state.st_suspect
        and state.ts_code not in yesterday_limit_codes
        and state.market_segment in allowed_segments
        and state.upper_limit >= MIN_D_VALID_LIMIT_PRICE
    )


def next_trade_day(date_str: str, n: int = 1) -> str:
    calendar_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
    if calendar_path.exists():
        try:
            calendar = pd.read_csv(calendar_path, dtype={"cal_date": str})
            if "is_open" in calendar.columns:
                calendar = calendar[calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
            dates = sorted(calendar["cal_date"].astype(str).tolist())
            future = [date for date in dates if date > date_str]
            if len(future) >= n:
                return future[n - 1]
        except Exception:
            pass  # 可以忽略：交易日历读不了走下面按工作日推算的兜底

    current = datetime.strptime(date_str, "%Y%m%d").date()
    count = 0
    while count < n:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return current.strftime("%Y%m%d")


def load_position_records() -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "processed" / "positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_position_records(records: list[dict[str, Any]]) -> None:
    path = PROJECT_ROOT / "data" / "processed" / "positions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
