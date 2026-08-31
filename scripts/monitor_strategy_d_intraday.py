"""
策略D盘中监控：首板回封信号检测 + 14:55自动撤单

【正式发布驱动】
  FACTOR_UNION模式：
    从09:30读取QMT一分钟K线；只用已完成分钟收盘重建封板、炸板和回封，
    每次新完成的分钟回封匹配已发布因子条件；
    旧D的14:00时间、strong情绪和2～3次炸板条件不参与因子版判断。

  LEGACY_FORMAL_D模式：
    09:35～14:00只观察；14:00后真实回封并满足旧D全部条件才允许BUY。

  两种模式都继续强制首板、非ST、当前封板、可靠成交概率、单日唯一候选和
  14:55撤单等公共安全门。

运行方式：
  python scripts/monitor_strategy_d_intraday.py              # 仅提醒，不下单
  python scripts/monitor_strategy_d_intraday.py --live-order # 已退役；实盘只能由daemon统一执行
  python scripts/monitor_strategy_d_intraday.py --dry-run    # 打印配置后退出

日志/输出：
  logs/strategy_d_monitor_YYYYMMDD.log
  reports/strategy_d/intraday_signals_YYYYMMDD.csv
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from dotenv import load_dotenv

from src.utils.logger import setup_logger
from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.notify import notify
from src.data_cleaner import DataCleaner
from src.fill_model import FillProbabilityEstimator
from src.strategy_d_checkpoint import (
    D_CHECKPOINT_STATUS_CLOSED,
    D_CHECKPOINT_STATUS_READY,
    D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
    block_strategy_d_checkpoint_recovery,
    clear_strategy_d_checkpoint_recovery_block,
    inspect_strategy_d_checkpoint,
    invalidate_strategy_d_checkpoint,
    strategy_d_checkpoint_path,
    strategy_d_checkpoint_recovery_block_path,
    strategy_d_machine_fingerprint,
    strategy_d_market_context_sha256,
    strategy_d_runtime_fingerprint,
    strategy_d_universe_sha256,
    write_strategy_d_checkpoint,
)
from src.strategy_d_factor_rules import (
    FACTOR_UNION_MODE,
    factor_values_from_raw,
    load_factor_release,
    matching_profile_ids,
    release_uses_factor_union,
    trading_minutes_between,
)
from src.strategy_d_minute_alignment import (
    StrictMinutePath,
    replay_completed_minute_path,
)
from src.qmt_adapter import MAX_SINGLE_QUOTE_SUBSCRIPTIONS
from src.strategy_d_spec import (
    D_CHECKPOINT_MAX_AGE_SECONDS,
    D_FIRST_TIME_BUCKETS,
    D_LATEST_COMPLETE_HISTORY_START_HHMM,
    D_MAX_OPEN_TIMES,
    D_MIN_FILL_PROBABILITY,
    D_MIN_OPEN_TIMES,
    D_ORDER_CANCEL_HHMM,
    D_PREFERRED_OPEN_TIMES,
    D_SIGNAL_START_HHMM,
    D_TAIL_RESEAL_HHMM,
    D_TRACKING_START_HHMM,
    classify_first_time_bucket_hhmm,
    common_candidate_rejection_reason,
    d_rank_key,
    intraday_history_is_complete,
    live_sentiment_is_historical_strong,
)

load_dotenv(PROJECT_ROOT / ".env")

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
D_POSITION_PCT = 0.825       # 默认目标仓位82.5%，优先使用 config.json/strategy_d/position_pct
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
    fill_reject_reason: str = "尚未计算成交概率"
    pre_close: float = 0.0
    open_price: float = 0.0
    session_low_price: float = 0.0
    cumulative_amount: float = 0.0
    previous_day_amount_yuan: float = 0.0
    last_break_hhmm: int = 0
    last_break_price: float = 0.0
    previous_seal_to_break_minutes: int = 0
    last_reseal_scan_round: int = 0
    matched_factor_profile_ids: str = ""
    factor_values_json: str = ""


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
    code = str(ts_code).strip().upper()
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
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
    stock_name = "" if name is None or pd.isna(name) else str(name).upper()
    code = str(ts_code).strip().upper()
    prefix = code.split(".")[0]
    if "ST" in stock_name or "退" in stock_name:
        return 0.05
    if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
        return 0.30
    if prefix.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


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
        pass
    return {}


def calc_shares_below_target_amount(target_amount: float, price: float) -> int:
    """按已经完成全部资金与仓位风控的目标金额向下取整到100股。"""
    if target_amount <= 0 or price <= 0:
        return 0
    return max(int((target_amount - 0.01) / price / 100) * 100, 0)


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
            pass

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


# ── 核心监控逻辑 ─────────────────────────────────────────────────────────────

class StrategyDMonitor:

    def __init__(self, broker, live_order: bool, logger, signal_csv: Path,
                 monitor_start_hhmm: int = MONITOR_START_HHMM,
                 allowed_segments: set[str] | None = None,
                 position_pct: float = D_POSITION_PCT,
                 config: dict[str, Any] | None = None,
                 position_recorder: Callable[[dict[str, Any]], None] | None = None,
                 entry_gate: Callable[[], tuple[bool, str]] | None = None,
                 tracking_gate: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.broker = broker
        self.live_order = live_order
        self.logger = logger
        self.signal_csv = signal_csv
        self.monitor_start_hhmm = monitor_start_hhmm
        self.allowed_segments = allowed_segments or set(DEFAULT_ALLOWED_SEGMENTS)
        self.position_pct = position_pct
        self.config = config or {}
        self.position_recorder = position_recorder
        # D的行情路径采集与实际入场必须解耦：上游候选仍在执行时继续从09:30
        # 维护完整日内路径，但每轮形成BUY候选前动态复核资金是否已经真正释放。
        # 独立研究/回测未传回调时保持原行为；daemon实盘入口必须传入组合门禁。
        self.entry_gate = entry_gate
        self.tracking_gate = tracking_gate
        self._last_entry_gate_reason = ""
        if self.live_order and self.position_recorder is None:
            raise RuntimeError("D实盘必须由daemon统一持仓记录器回写，禁止直写positions.json")
        self.factor_release_path = configured_factor_release_path(self.config)
        self.factor_release = load_factor_release(self.factor_release_path)
        self.factor_union_active = release_uses_factor_union(self.factor_release)
        self.factor_profiles = list(self.factor_release.get("profiles", []))
        self.factor_release_id = str(self.factor_release.get("release_id", ""))
        validate_configured_execution_clock(self.config)
        self.min_open_times = configured_min_open_times(self.config)
        self.max_open_times = configured_max_open_times(self.config)
        self.preferred_open_times = configured_preferred_open_times(self.config)
        self.min_fill_probability = configured_min_fill_probability(self.config)
        self.first_time_buckets = configured_first_time_buckets(self.config)
        self.tail_reseal_hhmm = configured_tail_reseal_hhmm(self.config)
        self.checkpoint_max_age_sec = configured_checkpoint_max_age_sec(self.config)
        (
            self.sentiment_current_min,
            self.sentiment_current_max,
        ) = configured_sentiment_bounds(self.config)
        self.default_fill_planned_amount = float(
            self.config.get("fill_model", {}).get(
                "default_planned_buy_amount", 100000
            )
        )

        self.yesterday_limit_codes: set[str] = set()
        self.universe: list[str] = []
        self.name_map: dict[str, str] = {}
        self.circ_mv_map: dict[str, float] = {}
        self.previous_day_amount_map: dict[str, float] = {}
        self.segment_stock_counts: dict[str, int] = {}
        self.states: dict[str, StockState] = {}
        self.fill_estimator: FillProbabilityEstimator | None = None
        self.fill_model_ready: bool = False
        self.scan_round = 0
        self.path_integrity_failed = False
        self.path_integrity_reason = ""
        self._path_failure_notified = False
        # 检查点 I/O 降级与行情路径缺失分开管理。前者可在后续完整扫描并成功写入
        # 新 READY 后恢复；后者涉及真实封板/炸板证据缺口，必须全天 fail-closed。
        self.checkpoint_io_degraded = False
        self.checkpoint_io_reason = ""
        self._last_checkpoint_io_error = ""
        self.checkpoint_path = strategy_d_checkpoint_path(
            PROJECT_ROOT, today_beijing().strftime("%Y%m%d")
        )
        self.checkpoint_machine_fingerprint = strategy_d_machine_fingerprint()
        self.checkpoint_runtime_fingerprint = ""
        self.universe_sha256 = ""
        self.market_context_sha256 = ""
        self.original_session_start_hhmm = 0
        self.first_complete_scan_at = ""
        self.last_complete_scan_at = ""
        self.last_scan_updated_count = 0
        self._restored_from_checkpoint = False

        # 本次会话下单记录 {order_id: ts_code}
        self.session_orders: dict[str, str] = {}
        self.session_order_details: dict[str, dict[str, Any]] = {}
        self.signal_records: list[dict] = []
        self.order_placed: bool = False   # 本会话已触发BUY，不再对其他股票下单
        self.order_locked_ts_code: str = ""
        self.factor_signal_consumed: bool = False
        self.factor_signal_locked_ts_code: str = ""
        self.position_opened: bool = False
        self.waiting_order_only: bool = False
        self.limit_price_fallback_logged: bool = False
        self._strong_notified: bool = False     # 情绪转强(≥阈值)只推送一次
        # 正式因子D的事件时钟必须来自“已完成QMT 1m收盘”，不能再使用30秒快照
        # 的瞬时涨停/炸板转换。缓存只覆盖当前分钟；下一分钟重新拉取完整路径。
        self.strict_minute_paths: dict[str, StrictMinutePath] = {}
        self.strict_minute_refresh_hhmm: int = 0
        self.strict_minute_refresh_error: str = ""

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def setup(self) -> None:
        self.logger.info("=== 策略D盘中监控启动 ===")
        self.yesterday_limit_codes = load_yesterday_limit_codes()
        full_universe = load_stock_universe()
        self.universe = filter_universe_by_segments(full_universe, self.allowed_segments)
        self.name_map = load_stock_names()
        self.circ_mv_map = load_latest_circ_mv_map()
        self.previous_day_amount_map = load_previous_daily_amount_map()
        segment_counts: dict[str, int] = {}
        for code in self.universe:
            segment = classify_market_segment(code)
            segment_counts[segment] = segment_counts.get(segment, 0) + 1
        self.segment_stock_counts = segment_counts
        self.universe_sha256 = strategy_d_universe_sha256(self.universe)
        self.market_context_sha256 = strategy_d_market_context_sha256(
            self.universe,
            self.yesterday_limit_codes,
            self.name_map,
            self.circ_mv_map,
            self.previous_day_amount_map,
        )
        if not self.checkpoint_runtime_fingerprint:
            self.checkpoint_runtime_fingerprint = strategy_d_runtime_fingerprint(
                PROJECT_ROOT, self.config
            )
        try:
            self.fill_estimator = FillProbabilityEstimator(
                PROJECT_ROOT / "config" / "config.json"
            )
            self.fill_model_ready = True
        except Exception as exc:
            self.fill_estimator = None
            self.fill_model_ready = False
            self.logger.error(
                "D成交概率模型初始化失败，按严格对齐口径禁止D开仓: %s", exc
            )
        self.logger.info(
            "宇宙: %d只(原始%d只) | 允许分段=%s | 分段数量=%s | 昨日涨停(排除首板): %d只 | 全市场扫描: %d批x%d只/轮，轮间隔%d秒",
            len(self.universe), len(full_universe), ",".join(sorted(self.allowed_segments)),
            segment_counts, len(self.yesterday_limit_codes),
            len(self._batches()), POLL_BATCH_SIZE, POLL_INTERVAL_SEC,
        )
        max_order_amount = float(self.config.get("live_trade", {}).get("max_single_order_amount", 50000))
        if self.factor_union_active:
            self.logger.warning(
                "D启用半年因子并集发布: release_id=%s profiles=%d | 任一if命中后进入候选 | "
                "封板/炸板/回封严格按已完成QMT 1m收盘重建 | "
                "首板/非ST/14:55前/成交概率>=%.0f%%且可靠仍是公共安全门",
                self.factor_release_id,
                len(self.factor_profiles),
                self.min_fill_probability * 100,
            )
        else:
            self.logger.info(
                "D最低开仓条件: 市场分段在%s | 首板(排除昨日涨停) | 当前封涨停 | 今日炸板%d~%d次(multi_open) | 首次封板时段=%s | 当前封板数=%d~%d(strong代理，不含very_strong) | 最后真实回封>=%s | 成交概率>=%.0f%%且可靠 | 实盘二次复核通过",
                ",".join(sorted(self.allowed_segments)),
                self.min_open_times,
                self.max_open_times,
                ",".join(sorted(self.first_time_buckets)),
                self.sentiment_current_min,
                self.sentiment_current_max,
                hhmm_to_str(self.tail_reseal_hhmm),
                self.min_fill_probability * 100,
            )
        self.logger.info(
            "D回测对齐时钟: %s开始持续记录完整封板/炸板路径；%s；%s停止并撤销未成交委托。",
            hhmm_to_str(MONITOR_START_HHMM),
            (
                "每次已完成1m收盘形成回封后按发布因子if判断"
                if self.factor_union_active
                else f"{hhmm_to_str(SIGNAL_START_HHMM)}后只有真实回封才允许BUY"
            ),
            hhmm_to_str(CANCEL_HHMM),
        )
        self.logger.info(
            "D重启恢复: 每轮完整扫描原子保存逐票路径；检查点最长%d秒，"
            "跨设备/跨交易日/代码配置变化/扫描缺批一律拒绝恢复。",
            self.checkpoint_max_age_sec,
        )
        self.logger.info(
            "D选票规则: 每天最多买1只，先优先炸板%d次，再按封单金额÷流通市值降序；"
            "只买排第1的那只，买不进当天就放弃、不用第2名递补。",
            self.preferred_open_times,
        )
        fixed_cap_text = f"{max_order_amount:.0f}元" if max_order_amount > 0 else "不设固定金额上限"
        self.logger.info(
            "D开仓参数: 目标仓位=%.1f%% 单票硬顶=85%% 固定单笔上限=%s 买入价=涨停价 14:55处理未成交/部分成交委托",
            self.position_pct * 100,
            fixed_cap_text,
        )

    def _batches(self) -> list[list[str]]:
        return [self.universe[i: i + POLL_BATCH_SIZE]
                for i in range(0, len(self.universe), POLL_BATCH_SIZE)]

    # ── 盘中路径检查点 ────────────────────────────────────────────────────────

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else 0.0
        if isinstance(value, dict):
            return {
                str(key): StrategyDMonitor._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [StrategyDMonitor._json_safe(item) for item in value]
        return value

    def _invalidate_checkpoint(self, status: str, reason: str) -> bool:
        try:
            invalidate_strategy_d_checkpoint(
                self.checkpoint_path,
                trade_date=today_beijing().strftime("%Y%m%d"),
                status=status,
                reason=reason,
                recorded_at=now_beijing(),
                machine_fingerprint=self.checkpoint_machine_fingerprint,
                runtime_fingerprint=self.checkpoint_runtime_fingerprint,
            )
            self._last_checkpoint_io_error = ""
            return True
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D路径检查点失效标记写入失败：%s", exc)
            return False

    def _enter_checkpoint_io_degraded(self, reason: str) -> bool:
        """隔离检查点I/O故障；不把完整的内存行情路径误判为永久缺失。"""

        self.checkpoint_io_degraded = True
        self.checkpoint_io_reason = str(reason)
        try:
            block_strategy_d_checkpoint_recovery(
                self.checkpoint_path,
                trade_date=today_beijing().strftime("%Y%m%d"),
                reason=self.checkpoint_io_reason,
                recorded_at=now_beijing(),
            )
        except Exception as exc:
            # 主检查点没能切换为 SCAN_IN_PROGRESS，恢复阻断标记也无法落盘时，
            # 新进程可能读取陈旧 READY。此时无法证明恢复唯一性，只能永久停开。
            self.path_integrity_failed = True
            self.path_integrity_reason = (
                "D检查点I/O失败且恢复阻断标记无法落盘，不能证明重启恢复路径唯一:"
                f"{exc}"
            )
            self.logger.error("D完整路径已永久失效：%s", self.path_integrity_reason)
            return False
        self.logger.warning(
            "D检查点进入I/O降级保护：%s；当前轮不判断BUY。若后续全市场扫描完整、"
            "新READY原子落盘且恢复阻断标记清除成功，将自动恢复D，不按全天路径缺失处理。",
            self.checkpoint_io_reason,
        )
        return True

    def _clear_checkpoint_io_degraded(self) -> bool:
        try:
            clear_strategy_d_checkpoint_recovery_block(self.checkpoint_path)
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D新READY已落盘，但恢复阻断标记清除失败：%s", exc)
            return False
        old_reason = self.checkpoint_io_reason
        self.checkpoint_io_degraded = False
        self.checkpoint_io_reason = ""
        self._last_checkpoint_io_error = ""
        self.logger.warning(
            "D检查点I/O已恢复：后续完整扫描已成功写入新READY并清除恢复阻断标记；"
            "D继续使用09:30起内存路径判断信号。原故障=%s",
            old_reason,
        )
        return True

    def _save_ready_checkpoint(self) -> bool:
        now = now_beijing()
        if not self.first_complete_scan_at:
            self.first_complete_scan_at = now.isoformat()
        self.last_complete_scan_at = now.isoformat()
        resume_allowed = bool(
            not self.path_integrity_failed
            and not self.order_placed
            and not self.session_orders
            and not self.position_opened
            and not self.waiting_order_only
        )
        # 未曾封板且当前也未封板的股票，其路径状态等价于默认值；股票宇宙摘要和
        # 完整扫描覆盖数已证明它们被观察过，无需每30秒重复写入数千条零状态。
        # 恢复时先按同一宇宙重建默认状态，再覆盖下面的非默认路径状态。
        path_states = {
            code: self._json_safe(asdict(state))
            for code, state in self.states.items()
            if (
                state.ever_sealed
                or state.was_sealed
                or state.open_times_today > 0
                or state.watch_alerted
                or state.buy_signaled
                or bool(state.order_id)
                or state.invalid_price_ticks > 0
            )
        }
        payload = {
            "status": D_CHECKPOINT_STATUS_READY,
            "resume_allowed": resume_allowed,
            "trade_date": today_beijing().strftime("%Y%m%d"),
            "recorded_at": now.isoformat(),
            "tracking_start_hhmm": D_TRACKING_START_HHMM,
            "original_session_start_hhmm": self.original_session_start_hhmm,
            "first_complete_scan_at": self.first_complete_scan_at,
            "last_complete_scan_at": self.last_complete_scan_at,
            "scan_round": self.scan_round,
            "last_scan_updated_count": self.last_scan_updated_count,
            "universe_size": len(self.universe),
            "universe_sha256": self.universe_sha256,
            "market_context_sha256": self.market_context_sha256,
            "state_count": len(path_states),
            "states": path_states,
            "path_integrity_failed": self.path_integrity_failed,
            "path_integrity_reason": self.path_integrity_reason,
            "machine_fingerprint": self.checkpoint_machine_fingerprint,
            "runtime_fingerprint": self.checkpoint_runtime_fingerprint,
            "signal_records": self._json_safe(self.signal_records),
            "strong_notified": self._strong_notified,
            "limit_price_fallback_logged": self.limit_price_fallback_logged,
            "order_placed": self.order_placed,
            "order_locked_ts_code": self.order_locked_ts_code,
            "factor_signal_consumed": self.factor_signal_consumed,
            "factor_signal_locked_ts_code": self.factor_signal_locked_ts_code,
            "session_orders": self._json_safe(self.session_orders),
        }
        try:
            write_strategy_d_checkpoint(self.checkpoint_path, payload)
            self._last_checkpoint_io_error = ""
            return True
        except Exception as exc:
            self._last_checkpoint_io_error = str(exc)
            self.logger.error("D完整路径检查点保存失败，本轮状态不可用于重启恢复：%s", exc)
            return False

    def _restore_ready_checkpoint(self) -> tuple[bool, str]:
        check = inspect_strategy_d_checkpoint(
            self.checkpoint_path,
            trade_date=today_beijing().strftime("%Y%m%d"),
            now=now_beijing(),
            max_age_seconds=self.checkpoint_max_age_sec,
            expected_tracking_start_hhmm=D_TRACKING_START_HHMM,
            expected_machine_fingerprint=self.checkpoint_machine_fingerprint,
            expected_runtime_fingerprint=self.checkpoint_runtime_fingerprint,
            expected_universe_sha256=self.universe_sha256,
            expected_universe_size=len(self.universe),
            expected_market_context_sha256=self.market_context_sha256,
        )
        if not check.ok:
            return False, check.reason
        payload = check.payload
        valid_field_names = {item.name for item in fields(StockState)}
        restored_states: dict[str, StockState] = {
            code: StockState(
                ts_code=code,
                name=self.name_map.get(code, ""),
                market_segment=classify_market_segment(code),
                circ_mv=float(self.circ_mv_map.get(code, 0.0) or 0.0),
            )
            for code in self.universe
        }
        try:
            for ts_code, raw_state in payload["states"].items():
                if set(raw_state).difference(valid_field_names):
                    raise ValueError(f"{ts_code}包含未知状态字段")
                if str(ts_code) not in restored_states:
                    raise ValueError(f"{ts_code}不在当前股票宇宙")
                restored_states[str(ts_code)] = StockState(**raw_state)
        except Exception as exc:
            return False, f"D逐票状态无法恢复:{exc}"
        self.states = restored_states
        self.scan_round = int(payload["scan_round"])
        self.original_session_start_hhmm = int(payload["original_session_start_hhmm"])
        self.first_complete_scan_at = str(payload["first_complete_scan_at"])
        self.last_complete_scan_at = str(payload["last_complete_scan_at"])
        self.last_scan_updated_count = int(payload["last_scan_updated_count"])
        records = payload.get("signal_records", [])
        self.signal_records = list(records) if isinstance(records, list) else []
        self._strong_notified = bool(payload.get("strong_notified", False))
        self.limit_price_fallback_logged = bool(
            payload.get("limit_price_fallback_logged", False)
        )
        self.factor_signal_consumed = bool(
            payload.get("factor_signal_consumed", False)
        )
        self.factor_signal_locked_ts_code = str(
            payload.get("factor_signal_locked_ts_code", "")
        )
        self._restored_from_checkpoint = True
        return True, check.reason

    # ── 状态更新 ──────────────────────────────────────────────────────────────

    @property
    def sealed_ever_count(self) -> int:
        """全市场【当前正封在涨停】的家数（瞬时快照，每轮刷新）。

        与回测口径一致：回测数的是收盘封住的涨停(limit==U)，临近收盘时"当前封板数"≈"收盘涨停数"。
        只看每只票最近一次轮询的封板状态(was_sealed)，炸板打开的不计、回封的计入。
        """
        return sum(1 for st in self.states.values() if st.was_sealed)

    def _sentiment_passes(self) -> bool:
        return live_sentiment_is_historical_strong(
            self.sealed_ever_count,
            minimum=self.sentiment_current_min,
            maximum=self.sentiment_current_max,
        )

    def _segment_sentiment_level(self, segment: str) -> str:
        stock_count = int(self.segment_stock_counts.get(segment, 0))
        limit_count = sum(
            1
            for state in self.states.values()
            if state.was_sealed and state.market_segment == segment
        )
        return DataCleaner.classify_segment_sentiment(
            limit_count, stock_count
        )

    @property
    def market_ever_sealed_count(self) -> int:
        return sum(1 for state in self.states.values() if state.ever_sealed)

    @property
    def market_break_event_count(self) -> int:
        return sum(int(state.open_times_today) for state in self.states.values())

    def _same_segment_reseal_context(self, segment: str) -> tuple[int, int, float]:
        same = [
            state for state in self.states.values()
            if state.market_segment == segment
        ]
        active = sum(1 for state in same if state.was_sealed)
        ever = sum(1 for state in same if state.ever_sealed)
        return active, ever, active / ever if ever > 0 else 0.0

    def _strict_path_for(self, st: StockState) -> StrictMinutePath | None:
        return self.strict_minute_paths.get(st.ts_code)

    def _refresh_strict_minute_paths(self) -> bool:
        """为当前可能入选的股票批量重建回测同口径分钟路径；失败即不产生BUY。"""

        current_hhmm = now_hhmm()
        if self.strict_minute_refresh_hhmm == current_hhmm:
            return not self.strict_minute_refresh_error

        targets = [
            state
            for state in self.states.values()
            if (
                state.was_sealed
                and not state.st_suspect
                and state.ts_code not in self.yesterday_limit_codes
                and state.market_segment in self.allowed_segments
                and state.upper_limit >= MIN_D_VALID_LIMIT_PRICE
            )
        ]
        self.strict_minute_paths = {}
        self.strict_minute_refresh_error = ""
        if not targets:
            self.strict_minute_refresh_hhmm = current_hhmm
            return True
        if self.broker is None or not hasattr(self.broker, "get_minute_bars"):
            self.strict_minute_refresh_error = "券商行情接口不支持QMT一分钟K线"
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        now = now_beijing()
        trade_date = today_beijing().strftime("%Y%m%d")
        codes = [state.ts_code for state in targets]
        if len(codes) > MAX_SINGLE_QUOTE_SUBSCRIPTIONS:
            self.strict_minute_refresh_error = (
                "严格D一分钟候选超过QMT单股订阅安全上限："
                f"{len(codes)}>{MAX_SINGLE_QUOTE_SUBSCRIPTIONS}"
            )
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False
        try:
            raw_paths = self.broker.get_minute_bars(
                codes,
                start_time=trade_date + "093000",
                end_time=now.strftime("%Y%m%d%H%M%S"),
            )
            if not isinstance(raw_paths, dict):
                raise RuntimeError(
                    f"一分钟K线返回非法类型{type(raw_paths).__name__}"
                )
            missing_codes = set(codes).difference(str(code) for code in raw_paths)
            if missing_codes:
                raise RuntimeError(
                    f"一分钟K线缺少股票:{sorted(missing_codes)[:5]}"
                )
            for state in targets:
                self.strict_minute_paths[state.ts_code] = replay_completed_minute_path(
                    raw_paths.get(state.ts_code, []),
                    limit_price=state.upper_limit,
                    current_hhmm=current_hhmm,
                )
        except Exception as exc:
            self.strict_minute_paths = {}
            self.strict_minute_refresh_error = f"QMT一分钟路径查询/重建失败:{exc}"
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        uncertified = [
            f"{code}:{path.reason}"
            for code, path in self.strict_minute_paths.items()
            if not path.certifiable
        ]
        if uncertified:
            self.strict_minute_refresh_error = (
                f"{len(uncertified)}只候选的一分钟路径不完整，"
                f"示例={uncertified[:3]}"
            )
            self.logger.error(
                "[D STRICT 1M BLOCK] %s，正式因子D本轮禁止开仓",
                self.strict_minute_refresh_error,
            )
            return False

        self.strict_minute_refresh_hhmm = current_hhmm
        self.logger.info(
            "[D STRICT 1M] 已按回测分钟收盘口径认证%d只当前封板候选，完成分钟=%s",
            len(self.strict_minute_paths),
            hhmm_to_str(current_hhmm),
        )
        return True

    def _factor_raw_values(
        self,
        st: StockState,
        strict_path: StrictMinutePath | None = None,
    ) -> dict[str, Any]:
        market_active = self.sealed_ever_count
        market_ever = self.market_ever_sealed_count
        segment_active, segment_ever, segment_rate = self._same_segment_reseal_context(
            st.market_segment
        )
        event_first_seal_hhmm = (
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        event_last_seal_hhmm = (
            strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
        )
        event_open_times = strict_path.open_times if strict_path else st.open_times_today
        event_last_break_hhmm = (
            strict_path.last_break_hhmm if strict_path else st.last_break_hhmm
        )
        event_last_break_price = (
            strict_path.last_break_close if strict_path else st.last_break_price
        )
        event_previous_hold = (
            strict_path.previous_seal_to_break_minutes
            if strict_path
            else st.previous_seal_to_break_minutes
        )
        event_low = (
            strict_path.pre_signal_low_price
            if strict_path and strict_path.pre_signal_low_price > 0
            else st.session_low_price
        )
        event_amount = (
            strict_path.signal_cumulative_amount
            if strict_path and strict_path.signal_cumulative_amount > 0
            else st.cumulative_amount
        )
        amount_ratio = (
            event_amount / st.previous_day_amount_yuan
            if st.previous_day_amount_yuan > 0
            else float("nan")
        )
        return {
            "signal_hhmm": event_last_seal_hhmm,
            "first_seal_hhmm": event_first_seal_hhmm,
            "open_times_at_signal": event_open_times,
            "first_to_signal_minutes": trading_minutes_between(
                event_first_seal_hhmm, event_last_seal_hhmm
            ),
            "last_break_to_signal_minutes": trading_minutes_between(
                event_last_break_hhmm, event_last_seal_hhmm
            ),
            "previous_seal_to_break_minutes": event_previous_hold,
            "last_break_close_depth_pct": (
                max(st.upper_limit / event_last_break_price - 1.0, 0.0)
                if st.upper_limit > 0 and event_last_break_price > 0
                else float("nan")
            ),
            "open_gap_pct": (
                st.open_price / st.pre_close - 1.0
                if st.open_price > 0 and st.pre_close > 0
                else float("nan")
            ),
            "pre_signal_min_return": (
                event_low / st.pre_close - 1.0
                if event_low > 0 and st.pre_close > 0
                else float("nan")
            ),
            "signal_cumulative_amount_vs_prev_day": amount_ratio,
            "market_ever_sealed_count": market_ever,
            "market_active_sealed_count": market_active,
            "market_seal_rate": market_active / market_ever if market_ever > 0 else 0.0,
            "market_break_event_rate": (
                self.market_break_event_count / market_ever if market_ever > 0 else 0.0
            ),
            "market_segment": st.market_segment,
            "same_segment_seal_rate": segment_rate,
            "same_segment_active_sealed_count": segment_active,
            "same_segment_ever_sealed_count": segment_ever,
        }

    def _factor_release_match(
        self, st: StockState, *, require_fresh_reseal: bool
    ) -> tuple[bool, str]:
        if not self.factor_union_active:
            return False, "当前D发布不是因子并集模式"
        if st.st_suspect:
            return False, "ST/风险警示"
        if st.market_segment not in self.allowed_segments:
            return False, f"市场分段{st.market_segment}不在允许范围"
        if st.ts_code in self.yesterday_limit_codes:
            return False, "昨日已涨停，非首板"
        if not st.was_sealed:
            return False, "当前不在涨停封板状态"
        strict_path = self._strict_path_for(st)
        if strict_path is None:
            return False, "尚未取得已完成QMT一分钟路径认证"
        if not strict_path.certifiable:
            return False, f"QMT一分钟路径不可认证:{strict_path.reason}"
        if not strict_path.has_reseal:
            return False, "回测分钟收盘口径尚未形成封板→炸板→回封"
        if not (
            MONITOR_START_HHMM <= strict_path.last_reseal_hhmm < CANCEL_HHMM
        ):
            return False, "回封时间不在可委托窗口"
        if require_fresh_reseal and not strict_path.has_fresh_reseal:
            return False, "不是最新完成分钟刚形成的回测口径回封事件"
        raw = self._factor_raw_values(st, strict_path)
        values = factor_values_from_raw(raw)
        matched = matching_profile_ids(values, self.factor_profiles)
        st.factor_values_json = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        st.matched_factor_profile_ids = ";".join(matched)
        if not matched:
            return False, "未命中任何已发布D因子if条件"
        return True, f"命中D因子条件:{','.join(matched)}"

    def _refresh_fill_gate(self, st: StockState) -> tuple[bool, str]:
        """按历史成交概率模型实时复算；模型或字段缺失时fail-closed。"""

        if not self.fill_model_ready or self.fill_estimator is None:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_reject_reason = "成交概率模型未就绪"
            return False, st.fill_reject_reason
        if st.circ_mv <= 0 or st.upper_limit <= 0 or st.bid_vol <= 0:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_reject_reason = "流通市值/涨停价/实时封单缺失"
            return False, st.fill_reject_reason

        strict_path = self._strict_path_for(st) if self.factor_union_active else None
        first_bucket = classify_first_time_bucket_hhmm(
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        current_queue_amount = float(st.bid_vol) * float(st.upper_limit)
        fd_ratio = self._fd_amount_to_circ_mv(st)
        abnormal_threshold = float(
            self.config.get("fill_model", {}).get(
                "fd_amount_abnormal_ratio_threshold", 1.0
            )
        )
        if fd_ratio > abnormal_threshold:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_reject_reason = (
                f"封单市值比{fd_ratio:.2%}超过异常阈值{abnormal_threshold:.2%}"
            )
            return False, st.fill_reject_reason

        try:
            result = self.fill_estimator.estimate(
                ts_code=st.ts_code,
                trade_date=today_beijing().strftime("%Y%m%d"),
                limit_times=1,
                board_type="multi_open",
                first_time_bucket=first_bucket,
                market_sentiment_level="strong",
                market_segment=st.market_segment,
                segment_market_sentiment_level=self._segment_sentiment_level(
                    st.market_segment
                ),
                circ_mv=float(st.circ_mv),
                current_queue_amount=current_queue_amount,
                planned_buy_amount=self.default_fill_planned_amount,
            )
        except Exception as exc:
            st.fill_probability = 0.0
            st.fill_reliable = False
            st.fill_matched_source = "none"
            st.fill_reject_reason = f"成交概率复算失败:{exc}"
            return False, st.fill_reject_reason

        source = str(result.get("matched_source", "none"))
        probability = float(result.get("fill_probability", 0.0) or 0.0)
        reliable = source != "none"
        st.fill_probability = probability
        st.fill_reliable = reliable
        st.fill_matched_source = source
        if not reliable:
            st.fill_reject_reason = "成交概率没有可靠历史匹配"
            return False, st.fill_reject_reason
        if probability < self.min_fill_probability:
            st.fill_reject_reason = (
                f"成交概率{probability:.1%}低于回测阈值"
                f"{self.min_fill_probability:.1%}"
            )
            return False, st.fill_reject_reason
        st.fill_reject_reason = ""
        return True, "通过回测同口径成交概率门"

    def _update_states(self, quotes: dict) -> None:
        hhmm = now_hhmm()
        fallback_count = 0
        qmt_limit_count = 0
        skipped_no_price = 0
        for ts_code, snap in quotes.items():
            name = self.name_map.get(ts_code, "")
            qmt_limit = float(snap.upper_limit or 0.0)
            est_limit = estimate_upper_limit(ts_code, float(snap.pre_close or 0.0), name)
            if qmt_limit > 0:
                qmt_limit_count += 1
            elif est_limit > 0:
                fallback_count += 1
            # 存储/下单用涨停价：优先 QMT（交易所口径），缺失时用估算
            upper_limit = qmt_limit if qmt_limit > 0 else est_limit
            if upper_limit <= 0 or snap.last_price <= 0:
                skipped_no_price += 1
                continue
            # 封板判定：现价贴近【QMT涨停价】或【昨收估算涨停价（已验证准确）】任一即算。
            # 不再单信 QMT——QMT 涨停价偶有缺失/滞后/不准，会漏掉真封板的票。
            at_limit = (
                (qmt_limit > 0 and abs(snap.last_price - qmt_limit) < 0.015)
                or (est_limit > 0 and abs(snap.last_price - est_limit) < 0.015)
            )

            if ts_code not in self.states:
                self.states[ts_code] = StockState(
                    ts_code=ts_code,
                    name=name,
                    market_segment=classify_market_segment(ts_code),
                    upper_limit=upper_limit,
                    circ_mv=float(self.circ_mv_map.get(ts_code, 0.0) or 0.0),
                    pre_close=float(snap.pre_close or 0.0),
                    open_price=float(getattr(snap, "open_price", 0.0) or 0.0),
                    session_low_price=float(
                        getattr(snap, "low_price", 0.0) or snap.last_price or 0.0
                    ),
                    cumulative_amount=float(getattr(snap, "amount", 0.0) or 0.0),
                    previous_day_amount_yuan=float(
                        self.previous_day_amount_map.get(ts_code, 0.0) or 0.0
                    ),
                )
            st = self.states[ts_code]
            st.upper_limit = upper_limit
            st.last_price = float(snap.last_price or 0.0)
            st.pre_close = float(snap.pre_close or st.pre_close or 0.0)
            st.open_price = float(
                getattr(snap, "open_price", 0.0) or st.open_price or 0.0
            )
            raw_low = float(
                getattr(snap, "low_price", 0.0) or st.last_price or 0.0
            )
            if raw_low > 0:
                st.session_low_price = (
                    raw_low
                    if st.session_low_price <= 0
                    else min(st.session_low_price, raw_low)
                )
            st.cumulative_amount = max(
                float(st.cumulative_amount or 0.0),
                float(getattr(snap, "amount", 0.0) or 0.0),
            )
            st.previous_day_amount_yuan = float(
                self.previous_day_amount_map.get(
                    ts_code, st.previous_day_amount_yuan
                ) or 0.0
            )
            # ST/风险警示识别（对齐回测口径 ~is_st，2026-07-23 春兴精工废单事故）：
            # ①名字带ST/退；②QMT真实涨停幅≈5%（名字缓存可能不带前缀，这次就栽在名字上，
            # 涨停幅是交易所口径、不会骗人）。命中任一即嫌疑。
            pre_c = float(snap.pre_close or 0.0)
            name_st = ("ST" in str(name).upper()) or ("退" in str(name))
            ratio_st = bool(qmt_limit > 0 and pre_c > 0 and 0.04 <= (qmt_limit / pre_c - 1.0) <= 0.06)
            st.st_suspect = name_st or ratio_st
            if upper_limit < MIN_D_VALID_LIMIT_PRICE:
                st.invalid_price_ticks += 1
                if st.invalid_price_ticks <= MAX_D_INVALID_PRICE_TICKS:
                    self.logger.warning(
                        "[PRICE GUARD] %s %s 涨停价异常%.2f，连续%d/%d轮，等待下一轮行情复核",
                        ts_code,
                        name,
                        upper_limit,
                        st.invalid_price_ticks,
                        MAX_D_INVALID_PRICE_TICKS,
                    )
            else:
                st.invalid_price_ticks = 0
            st.circ_mv = float(self.circ_mv_map.get(ts_code, st.circ_mv) or 0.0)

            if at_limit:
                # 当前封在涨停（炸板历史不影响：只看此刻是否封板）
                if not st.ever_sealed:
                    st.ever_sealed = True
                    st.first_seal_hhmm = hhmm
                if not st.was_sealed:
                    # 非涨停 → 涨停：记录重封时间
                    st.last_seal_hhmm = hhmm
                    st.last_reseal_scan_round = self.scan_round + 1
                st.was_sealed = True
                # 更新封单量（涨停买一量，单位：股）
                if snap.bid_volumes:
                    st.bid_vol = snap.bid_volumes[0]
            else:
                if st.was_sealed:
                    # 涨停 → 非涨停：炸板
                    st.previous_seal_to_break_minutes = trading_minutes_between(
                        st.last_seal_hhmm, hhmm
                    )
                    st.last_break_hhmm = hhmm
                    st.last_break_price = float(st.last_price or 0.0)
                    st.open_times_today += 1
                st.was_sealed = False
                st.bid_vol = 0

        if not self.limit_price_fallback_logged and quotes:
            self.limit_price_fallback_logged = True
            self.logger.info(
                "涨停价口径: QMT直接给出=%d，按昨收价估算=%d，缺价格跳过=%d",
                qmt_limit_count,
                fallback_count,
                skipped_no_price,
            )

    # ── 信号检测与分级触发 ────────────────────────────────────────────────────

    def _passes_base_filters(self, ts_code: str, st: StockState) -> bool:
        """通用过滤：首板 + 开板回封 + 当前涨停 + strong情绪 + 非ST。"""
        if st.st_suspect:  # 回测口径 ~is_st：ST/风险警示股从不入D候选池
            if not st.st_suspect_logged:
                st.st_suspect_logged = True
                self.logger.warning(
                    "[D过滤] %s %s 判定为ST/风险警示（名字带ST/退 或 QMT涨停幅≈5%%），"
                    "回测口径(~is_st)从不买这类票，排除出D候选。",
                    ts_code, st.name,
                )
            return False
        if ts_code in self.yesterday_limit_codes:  # 排除2板+
            return False
        if not st.was_sealed:                      # 当前不在涨停
            return False
        common_reason = common_candidate_rejection_reason(
            open_times=st.open_times_today,
            first_seal_hhmm=st.first_seal_hhmm,
        )
        if common_reason:
            return False
        if not self._sentiment_passes():           # 只允许历史strong桶
            return False
        return True

    def _fd_amount_to_circ_mv(self, st: StockState) -> float:
        """实时封单金额/流通市值，复刻D回测 pick_d_candidate 的排序字段。"""
        if st.circ_mv <= 0 or st.upper_limit <= 0:
            return 0.0
        fd_amount = float(st.bid_vol) * float(st.upper_limit)
        return fd_amount / (float(st.circ_mv) * 10000.0)

    def _rank_explain(self, st: StockState) -> str:
        return (
            f"fd_ratio={self._fd_amount_to_circ_mv(st):.4%}/"
            f"成交概率={st.fill_probability:.1%}/"
            f"封单{st.bid_vol / 10000:.1f}万股/"
            f"涨停价{st.upper_limit:.2f}/"
            f"流通市值{st.circ_mv / 10000:.2f}亿/"
            f"炸{st.open_times_today}/"
            f"重封{hhmm_to_str(st.last_seal_hhmm)}"
        )

    def _rank_key(self, st: StockState) -> tuple[int, float, str]:
        """调用D共享排序：先优先炸板2次，再比较实时封单比。"""

        return d_rank_key(
            open_times=st.open_times_today,
            fd_amount_to_circ_mv=self._fd_amount_to_circ_mv(st),
            ts_code=st.ts_code,
        )

    def _entry_gate_allows_buy(self) -> tuple[bool, str]:
        """动态复核D能否入场；门禁异常一律fail-closed但不停止路径采集。"""

        if self.entry_gate is None:
            return True, "未配置外部候选资金门禁"
        try:
            allowed, reason = self.entry_gate()
            normalized_reason = str(reason or ("动态门禁允许" if allowed else "动态门禁阻断"))
        except Exception as exc:
            allowed = False
            normalized_reason = f"动态门禁检查异常:{exc}"
        if not allowed and normalized_reason != self._last_entry_gate_reason:
            self.logger.info(
                "[D TRACKING ONLY] 继续维护09:30起完整路径，暂不产生BUY：%s",
                normalized_reason,
            )
        elif allowed and self._last_entry_gate_reason and normalized_reason != self._last_entry_gate_reason:
            self.logger.warning(
                "[D ENTRY UNLOCKED] 上游候选已不再占用资金；D从本轮起允许按正式条件形成BUY：%s",
                normalized_reason,
            )
        self._last_entry_gate_reason = normalized_reason
        return bool(allowed), normalized_reason

    def _tracking_gate_allows_monitor(self) -> tuple[bool, str]:
        """候选一旦真实成交便结束D只读扫描；异常时停止扫描并fail-closed。"""

        if self.tracking_gate is None:
            return True, "未配置外部路径继续门禁"
        try:
            allowed, reason = self.tracking_gate()
            return bool(allowed), str(reason or "路径继续门禁未说明原因")
        except Exception as exc:
            return False, f"路径继续门禁检查异常:{exc}"

    def _check_and_fire_factor_union(self) -> None:
        """用已完成1m路径执行半年发布因子；任一命中即进入唯一候选排序。"""

        if self.factor_signal_consumed:
            return
        if not self._refresh_strict_minute_paths():
            return
        candidates: list[StockState] = []
        for st in self.states.values():
            if st.buy_signaled:
                continue
            matched, reason = self._factor_release_match(
                st, require_fresh_reseal=True
            )
            if not matched:
                continue
            self.logger.info("[D因子命中] %s %s", st.ts_code, reason)
            candidates.append(st)

        if not candidates:
            return
        # 与历史因子并集一致：先取最早回封；同一轮/分钟优先炸板2次，再按代码稳定排序。
        ranked = sorted(
            candidates,
            key=lambda state: (
                -int(
                    bool(
                        self._strict_path_for(state)
                        and self._strict_path_for(state).open_times
                        == D_PREFERRED_OPEN_TIMES
                    )
                ),
                state.ts_code,
            ),
        )
        candidate = ranked[0]
        # 历史账本先选当日最早信号，再判断该挂单能否成交；即使成交门失败，
        # 也不能用事后结果补选更晚回封。因此选中即永久消耗今日D机会。
        self.factor_signal_consumed = True
        self.factor_signal_locked_ts_code = candidate.ts_code
        self.logger.warning(
            "[D FACTOR BUY] release=%s profiles=%s 本轮候选=%d，选择=%s",
            self.factor_release_id,
            candidate.matched_factor_profile_ids,
            len(ranked),
            candidate.ts_code,
        )
        fill_passed, fill_reason = self._refresh_fill_gate(candidate)
        if not fill_passed:
            self.logger.warning(
                "[D FACTOR NO FILL] %s 当日最早if信号未通过成交概率门，"
                "按无补选口径锁定今日D: %s",
                candidate.ts_code,
                fill_reason,
            )
            return
        valid, invalid_reason = self._validate_buy_candidate(candidate)
        if not valid:
            self.logger.warning(
                "[D FACTOR BUY SKIP] %s 二次复核失败: %s",
                candidate.ts_code,
                invalid_reason,
            )
            return
        self._fire_buy_signal(candidate)

    def _check_and_fire(self) -> None:
        if self.order_placed:
            return

        # 只拦截信号选择/下单，不拦截poll_once中的全市场行情更新和检查点保存。
        # 因此上游任意策略候选失败并过了补仓窗口后，D可以使用同一进程从
        # 09:30连续积累的真实路径继续判断，而不是从午后快照伪造路径。
        entry_allowed, _entry_reason = self._entry_gate_allows_buy()
        if not entry_allowed:
            return

        if self.factor_union_active:
            self._check_and_fire_factor_union()
            return

        hhmm = now_hhmm()
        buy_candidates: list[StockState] = []

        for ts_code, st in self.states.items():
            if st.buy_signaled:
                continue
            if not self._passes_base_filters(ts_code, st):
                continue

            # 14:00后必须发生真实回封。WATCH只提醒，不能绕过回测last_time>=14:00。
            if hhmm >= SIGNAL_START_HHMM:
                common_reason = common_candidate_rejection_reason(
                    open_times=st.open_times_today,
                    first_seal_hhmm=st.first_seal_hhmm,
                    last_seal_hhmm=st.last_seal_hhmm,
                    require_tail_reseal=True,
                )
                if common_reason:
                    continue
                fill_passed, fill_reason = self._refresh_fill_gate(st)
                if not fill_passed:
                    self.logger.info(
                        "[D过滤] %s %s 未通过成交概率门: %s",
                        st.ts_code,
                        st.name,
                        fill_reason,
                    )
                    continue
                buy_candidates.append(st)
                continue

            # ── 场景二：WATCH窗口 → 逐个发提醒 ──────────────────────────────
            if hhmm >= WATCH_START_HHMM and not st.watch_alerted:
                self._fire_watch_alert(st)

        # 有BUY候选：按原始D回测口径排序，只尝试第1名，失败不补偿。
        if not buy_candidates:
            if hhmm >= SIGNAL_START_HHMM:
                self._log_buy_empty_funnel(hhmm)
            return

        scored_all = sorted(buy_candidates, key=self._rank_key, reverse=True)
        scored = scored_all[:D_RETRY_TOP_N]
        rank_info = "  ".join(
            f"{idx + 1}.{s.ts_code}({self._rank_explain(s)})" for idx, s in enumerate(scored)
        )
        self.logger.info(
            "[BUY RANK] 本轮%d只候选，严格对齐D原始回测，只尝试第1名: %s",
            len(scored_all),
            rank_info,
        )
        if len(scored_all) > D_RETRY_TOP_N:
            self.logger.info(
                "[BUY RANK] 第%d名以后不尝试，原因=已取消D补偿机制，仅保留原始回测第1名口径；未尝试数量=%d",
                D_RETRY_TOP_N + 1,
                len(scored_all) - D_RETRY_TOP_N,
            )
        if len(scored) > 1:
            print(f"  [多候选排序] {rank_info}")

        for idx, candidate in enumerate(scored, start=1):
            fd_ratio = self._fd_amount_to_circ_mv(candidate)
            still_valid, invalid_reason = self._validate_buy_candidate(candidate)
            if not still_valid:
                self.logger.warning(
                    "[BUY SKIP] 第%d名 %s 不再符合D策略要求：%s",
                    idx,
                    candidate.ts_code,
                    invalid_reason,
                )
                print(f"  [跳过第{idx}名] {candidate.ts_code} 不再符合D策略要求：{invalid_reason}")
                continue
            self.logger.info(
                "[BUY TRY] 第%d名 fd_ratio=%.4f%%: %s %s  炸板%d次 重封%s 封单%.1f万股 流通市值%.2f亿",
                idx, fd_ratio * 100, candidate.ts_code, candidate.name, candidate.open_times_today,
                hhmm_to_str(candidate.last_seal_hhmm), candidate.bid_vol / 10000, candidate.circ_mv / 10000,
            )
            print(f"  [尝试第{idx}名] {candidate.ts_code} {candidate.name} fd_ratio={fd_ratio:.4%}")
            locked = self._fire_buy_signal(candidate)
            if locked:
                return
            fail_reason = str(getattr(candidate, "last_order_fail_reason", "") or "未形成有效委托")
            self.logger.warning(
                "[BUY FAIL] 第%d名失败：%s；已取消D补偿机制，本轮不再尝试其他候选。", idx, fail_reason
            )
            print(f"  [尝试第{idx}名失败] 原因={fail_reason}，已取消补偿，本轮结束")

        self.logger.warning("[BUY FAIL] 本轮第1名候选未形成有效委托，严格回测口径下不补偿。")
        print("  [本轮结束] 第1名未形成有效委托，严格回测口径下不补偿")

    def _log_buy_empty_funnel(self, hhmm: int) -> None:
        """14:00后无买入候选时打印D实时漏斗，说明卡在哪一层。"""

        current_sealed = [st for st in self.states.values() if st.was_sealed]
        first_board = [st for st in current_sealed if st.ts_code not in self.yesterday_limit_codes]
        opened_once = [st for st in first_board if st.open_times_today >= 1]
        open_times_ok = [
            st for st in opened_once
            if st.open_times_today <= self.max_open_times
        ]
        buy_time_ok = [
            st for st in open_times_ok
            if st.last_seal_hhmm >= self.tail_reseal_hhmm
        ]
        sample = "  ".join(
            f"{st.ts_code}(炸{st.open_times_today},重封{hhmm_to_str(st.last_seal_hhmm)})"
            for st in buy_time_ok[:5]
        ) or "无"
        self.logger.info(
            "[BUY FUNNEL] %s 无D买入候选：当前封板=%d 首板封板=%d 曾炸板回封=%d 炸板次数合规=%d 14:00后真实回封=%d 情绪=%d(允许%d~%d) 样例=%s",
            hhmm_to_str(hhmm),
            len(current_sealed),
            len(first_board),
            len(opened_once),
            len(open_times_ok),
            len(buy_time_ok),
            self.sealed_ever_count,
            self.sentiment_current_min,
            self.sentiment_current_max,
            sample,
        )

    def _validate_buy_candidate(self, st: StockState) -> tuple[bool, str]:
        """每次尝试下单前复核，确保重试候选仍符合D策略实时要求。"""

        segment = classify_market_segment(st.ts_code)
        if segment not in self.allowed_segments:
            return False, f"市场分段{segment}不在允许范围{','.join(sorted(self.allowed_segments))}"
        if st.ts_code in self.yesterday_limit_codes:
            return False, "昨日已涨停，非首板"
        if st.upper_limit < MIN_D_VALID_LIMIT_PRICE:
            if st.invalid_price_ticks < MAX_D_INVALID_PRICE_TICKS:
                return False, (
                    f"涨停价异常{st.upper_limit:.2f}元，连续{st.invalid_price_ticks}/{MAX_D_INVALID_PRICE_TICKS}轮，"
                    "等待下一轮行情复核"
                )
            return False, (
                f"涨停价异常{st.upper_limit:.2f}元，已连续{st.invalid_price_ticks}轮低于"
                f"{MIN_D_VALID_LIMIT_PRICE:.2f}元，本地风控确认拦截"
            )
        if not st.was_sealed:
            return False, "当前不在涨停封板状态"
        if st.st_suspect:
            return False, "ST/风险警示股不允许进入D"
        if self.factor_union_active:
            matched, match_reason = self._factor_release_match(
                st, require_fresh_reseal=False
            )
            if not matched:
                return False, match_reason
            hhmm = now_hhmm()
            if not (MONITOR_START_HHMM <= hhmm < CANCEL_HHMM):
                return False, f"当前{hhmm_to_str(hhmm)}不在D可委托窗口"
            fill_passed, fill_reason = self._refresh_fill_gate(st)
            if not fill_passed:
                return False, fill_reason
            return True, match_reason
        common_reason = common_candidate_rejection_reason(
            open_times=st.open_times_today,
            first_seal_hhmm=st.first_seal_hhmm,
            last_seal_hhmm=st.last_seal_hhmm,
            require_tail_reseal=True,
        )
        if common_reason:
            return False, common_reason
        if not self._sentiment_passes():
            return False, (
                f"当前封板{self.sealed_ever_count}只不在回测strong代理区间"
                f"{self.sentiment_current_min}~{self.sentiment_current_max}"
            )
        if st.circ_mv <= 0:
            return False, "缺少流通市值，无法按D原始fd_amount_to_circ_mv口径排序"
        hhmm = now_hhmm()
        if hhmm < SIGNAL_START_HHMM:
            return False, f"当前{hhmm_to_str(hhmm)}未到D买入时间{hhmm_to_str(SIGNAL_START_HHMM)}"
        fill_passed, fill_reason = self._refresh_fill_gate(st)
        if not fill_passed:
            return False, fill_reason
        return True, "通过D策略实时复核"

    # ── 观察提醒 ──────────────────────────────────────────────────────────────

    def _fire_watch_alert(self, st: StockState) -> None:
        hhmm = now_hhmm()
        st.watch_alerted = True
        msg = (
            f"\n{'─'*55}\n"
            f"  [WATCH] 观察提醒 {hhmm_to_str(hhmm)}\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(st.last_seal_hhmm)}  "
            f"炸板 {st.open_times_today} 次\n"
            f"  → 仅观察：14:00后必须再次真实回封并通过成交概率门才允许买入\n"
            f"{'─'*55}"
        )
        print(msg)
        self.logger.info(
            "[WATCH] %s %s 重封=%s 炸板=%d次",
            st.ts_code, st.name, hhmm_to_str(st.last_seal_hhmm), st.open_times_today,
        )
        self.signal_records.append({
            "signal_time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_type": "WATCH",
            "ts_code": st.ts_code,
            "name": st.name,
            "upper_limit": st.upper_limit,
            "reseal_hhmm": st.last_seal_hhmm,
            "open_times_today": st.open_times_today,
            "sentiment_est": self.sealed_ever_count,
            "order_id": "",
        })
        self._save_signals()

    # ── 买入信号 ──────────────────────────────────────────────────────────────

    def _fire_buy_signal(self, st: StockState) -> bool:
        if self.order_placed:
            self.logger.info("[BUY SKIP] 已锁定本轮D委托: %s，跳过 %s", self.order_locked_ts_code, st.ts_code)
            return True
        hhmm = now_hhmm()
        strict_path = self._strict_path_for(st) if self.factor_union_active else None
        event_reseal_hhmm = (
            strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
        )
        event_open_times = strict_path.open_times if strict_path else st.open_times_today
        event_first_seal_hhmm = (
            strict_path.first_seal_hhmm if strict_path else st.first_seal_hhmm
        )
        st.buy_signaled = True
        self.order_placed = True   # 先加锁再下单，防止QMT资金冻结延迟导致重复委托
        self.order_locked_ts_code = st.ts_code
        source = (
            f"半年D正式因子:{self.factor_release_id}:"
            f"{st.matched_factor_profile_ids}"
            if self.factor_union_active
            else "14:00后真实回封"
        )

        msg = (
            f"\n{'='*55}\n"
            f"  ★ [BUY] 买入信号 {hhmm_to_str(hhmm)}  [{source}]\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(event_reseal_hhmm)}  "
            f"炸板 {event_open_times} 次\n"
            f"  情绪估算：当前封板涨停 {self.sealed_ever_count} 只\n"
            f"  操作：{'实盘挂单' if self.live_order else '仅提醒（--live-order 开启下单）'}\n"
            f"{'='*55}"
        )
        print(msg)
        self.logger.warning(
            "[BUY] %s %s 重封=%s 炸板=%d次 来源=%s",
            st.ts_code, st.name, hhmm_to_str(event_reseal_hhmm),
            event_open_times, source,
        )
        record = {
            "signal_time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_type": "BUY",
            "ts_code": st.ts_code,
            "name": st.name,
            "upper_limit": st.upper_limit,
            "reseal_hhmm": event_reseal_hhmm,
            "open_times_today": event_open_times,
            "sentiment_est": self.sealed_ever_count,
            "first_time_bucket": classify_first_time_bucket_hhmm(
                event_first_seal_hhmm
            ),
            "fill_probability": st.fill_probability,
            "fill_reliable": st.fill_reliable,
            "fill_matched_source": st.fill_matched_source,
            "source": source,
            "factor_release_id": self.factor_release_id,
            "matched_factor_profile_ids": st.matched_factor_profile_ids,
            "factor_values_json": st.factor_values_json,
            "event_clock_source": (
                "QMT_COMPLETED_1M_CLOSE"
                if strict_path
                else "REALTIME_SNAPSHOT"
            ),
            "strict_break_close": (
                strict_path.last_break_close if strict_path else 0.0
            ),
            "order_id": "",
        }
        if self.live_order and self.broker is not None:
            locked = self._place_d_order(st, record)
        else:
            locked = True
        self.signal_records.append(record)
        self._save_signals()
        return locked

    # ── 实盘下单 ──────────────────────────────────────────────────────────────

    def _place_d_order(self, st: StockState, record: dict) -> bool:
        from src.broker_adapter import OrderRequest
        from src.qmt_adapter import tushare_to_qmt_code
        try:
            if self.session_orders:
                self.logger.warning("本会话已有D委托，拒绝再次下单: %s", st.ts_code)
                return True
            if st.upper_limit < MIN_D_VALID_LIMIT_PRICE:
                level = "确认拦截" if st.invalid_price_ticks >= MAX_D_INVALID_PRICE_TICKS else "等待复核"
                fail_reason = (
                    f"本地风控{level}：D涨停价异常{st.upper_limit:.2f}元，"
                    f"已连续{st.invalid_price_ticks}/{MAX_D_INVALID_PRICE_TICKS}轮低于"
                    f"{MIN_D_VALID_LIMIT_PRICE:.2f}元，疑似QMT行情字段异常"
                )
                self.logger.error(
                    "D下单拦截: 策略=D 股票=%s %s %s",
                    st.ts_code,
                    st.name,
                    fail_reason,
                )
                print(f"  → 下单拦截: 策略=D {st.ts_code} {st.name} {fail_reason}")
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_LOCAL_PRICE_GUARD"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            if st.last_price > 0 and st.upper_limit > st.last_price * 1.005:
                # 价格自洽校验（2026-07-23 春兴精工废单事故）：封板状态下现价=真实涨停价；
                # 下单价明显高于现价，说明涨停幅度判断有误（如ST股按10%误算2.08/真涨停1.98），
                # 挂出必成废单。按"买不进当天放弃"口径放弃该票，绝不猜价格。
                fail_reason = (
                    f"本地风控拦截：D下单价{st.upper_limit:.2f}高于现价{st.last_price:.2f}超0.5%，"
                    f"涨停幅度判断可能有误（疑似ST/风险警示按普通幅度误算），挂出必成废单，放弃该票"
                )
                self.logger.error("D下单拦截: 策略=D 股票=%s %s %s", st.ts_code, st.name, fail_reason)
                print(f"  → 下单拦截: 策略=D {st.ts_code} {st.name} {fail_reason}")
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_LOCAL_PRICE_GUARD"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
            occupied, occupied_desc = check_strategy_position_occupied(
                self.broker if self.live_order else None
            )
            if occupied:
                fail_reason = f"旧策略仓未实际清空，取消D开仓：{occupied_desc}"
                self.logger.warning("D下单拦截: %s", fail_reason)
                record["order_status"] = "REJECTED_EXISTING_STRATEGY_POSITION"
                record["order_status_text"] = fail_reason
                record["failure_reason"] = fail_reason
                return True
            account = self.broker.query_account()
            available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
            total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
            live_cfg = self.config.get("live_trade", {})
            max_order_amount = float(live_cfg.get("max_single_order_amount", 0) or 0)
            max_position_pct = float(live_cfg.get("max_position_pct", 0.85))
            max_total_position_pct = float(live_cfg.get("max_total_position_pct", 0.825))
            cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000) or 0)
            target_amount = min(
                max(available_cash - cash_buffer, 0.0),
                total_asset * self.position_pct,
                total_asset * max_total_position_pct,
                total_asset * max_position_pct,
            )
            if max_order_amount > 0:
                target_amount = min(target_amount, max_order_amount)
            shares = calc_shares_below_target_amount(target_amount, st.upper_limit)
            if shares <= 0:
                self.logger.warning("可用资金不足，跳过下单: %s", st.ts_code)
                return True
            actual_amount = shares * st.upper_limit
            actual_position_pct = actual_amount / total_asset if total_asset > 0 else 0.0
            req = OrderRequest(
                ts_code=st.ts_code,
                broker_code=tushare_to_qmt_code(st.ts_code),
                side="BUY",
                quantity=shares,
                price_type="FIX_PRICE",
                price=st.upper_limit,
                strategy_name="STRATEGY_D",
                remark=STRATEGY_REMARK,
                strategy_leg="D",
                business_date=today_beijing().strftime("%Y%m%d"),
                signal_date=today_beijing().strftime("%Y%m%d"),
                purpose="OPEN",
                source_key=(
                    f"D_FIRST_BOARD|{today_beijing().strftime('%Y%m%d')}|{st.ts_code}"
                ),
                metadata={"name": st.name, "entry_clock": now_beijing().strftime("%H:%M:%S")},
            )
            result = self.broker.place_order(req)
            if result.accepted:
                self.session_orders[result.order_id] = st.ts_code
                self.session_order_details[result.order_id] = {
                    "order_id": result.order_id,
                    "ts_code": st.ts_code,
                    "name": st.name,
                    "shares": shares,
                    "buy_price": st.upper_limit,
                    "target_amount": target_amount,
                    "actual_amount": actual_amount,
                    "target_position_pct": self.position_pct,
                    "actual_position_pct": actual_position_pct,
                    "buy_date": today_beijing().strftime("%Y%m%d"),
                    "strategy_leg": "D",
                }
                st.order_id = result.order_id
                record["order_id"] = result.order_id
                fill_check = self._confirm_submitted_order(result.order_id, st.ts_code)
                self.session_order_details[result.order_id]["submit_status_code"] = fill_check.status_code
                self.session_order_details[result.order_id]["submit_status_text"] = fill_check.status_text
                checked_qty = int(getattr(fill_check, "filled_qty", 0) or 0)
                checked_price = float(getattr(fill_check, "avg_price", 0.0) or 0.0)
                checked_price = checked_price if checked_price > 0 else st.upper_limit
                checked_amount = checked_qty * checked_price
                unchecked_amount = max(actual_amount - checked_amount, 0.0)
                if self._is_terminal_no_fill(fill_check):
                    fail_reason = self._describe_order_failure(fill_check, st, result.message)
                    st.last_order_fail_reason = fail_reason
                    record["order_status"] = "REJECTED_TERMINAL"
                    record["order_status_text"] = fill_check.status_text
                    record["failure_reason"] = fail_reason
                    record["filled_qty"] = 0
                    record["filled_amount"] = 0.0
                    self.logger.error(
                        "D下单失败: 策略=D 股票=%s %s 委托%d股 %.2f 总委托金额=%.2f 已成交金额=0.00 未成交金额=%.2f order_id=%s 状态=%s(%s) 失败原因=%s",
                        st.ts_code,
                        st.name,
                        shares,
                        st.upper_limit,
                        actual_amount,
                        actual_amount,
                        result.order_id,
                        fill_check.status_text,
                        fill_check.status_code,
                        fail_reason,
                    )
                    print(
                        f"  → 下单失败: 策略=D {st.ts_code} {st.name} "
                        f"委托{shares}股 总委托金额{actual_amount / 10000:.2f}万 "
                        f"已成交金额0.00万 未成交金额{actual_amount / 10000:.2f}万 "
                        f"order_id={result.order_id} 状态={fill_check.status_text}({fill_check.status_code}) "
                        f"失败原因={fail_reason}"
                    )
                    try:
                        notify(
                            "buy_result",
                            "❌ D开仓下单失败",
                            (
                                f"策略=D {st.ts_code} {st.name} 委托{shares}股 @{st.upper_limit:.2f}，"
                                f"总委托金额{actual_amount / 10000:.2f}万，已成交金额0.00万，"
                                f"未成交金额{actual_amount / 10000:.2f}万，"
                                f"order_id={result.order_id}，状态={fill_check.status_text}({fill_check.status_code})。"
                                f"失败原因：{fail_reason}"
                            ),
                            level="critical",
                            call=True,
                        )
                    except Exception:
                        pass
                    self.session_orders.pop(result.order_id, None)
                    self.session_order_details.pop(result.order_id, None)
                    st.order_id = ""
                    self.order_placed = False
                    self.order_locked_ts_code = ""
                    return False
                if checked_qty >= shares:
                    self.position_opened = True
                    self.waiting_order_only = False
                    record["order_status"] = "FILLED"
                    record["order_status_text"] = fill_check.status_text
                    record["filled_qty"] = checked_qty
                    record["filled_amount"] = checked_amount
                    self.logger.info(
                        "D持仓信息: 策略=D 股票=%s %s 持仓%d股 成本%.2f 市值=%.2f order_id=%s",
                        st.ts_code,
                        st.name,
                        checked_qty,
                        checked_price,
                        checked_amount,
                        result.order_id,
                    )
                    print(
                        f"  → 持仓信息: 策略=D {st.ts_code} {st.name} "
                        f"持仓{checked_qty}股 成本{checked_price:.2f} "
                        f"市值{checked_amount / 10000:.2f}万 order_id={result.order_id}"
                    )
                    self._record_filled_d_position(result.order_id, checked_qty, checked_price)
                else:
                    self.waiting_order_only = True
                    self.position_opened = checked_qty > 0
                    record["order_status"] = "PENDING_OR_PARTIAL"
                    record["order_status_text"] = fill_check.status_text
                    record["filled_qty"] = checked_qty
                    record["filled_amount"] = checked_amount
                    self.logger.info(
                        "D委托信息: 策略=D 股票=%s %s 委托%d股 %.2f 总委托金额=%.2f 已成交%d股 已成交金额=%.2f 未成交金额=%.2f 目标仓位=%.1f%% 实际仓位=%.2f%% 固定单笔上限=%s order_id=%s 提交后状态=%s(%s)",
                        st.ts_code,
                        st.name,
                        shares,
                        st.upper_limit,
                        actual_amount,
                        checked_qty,
                        checked_amount,
                        unchecked_amount,
                        self.position_pct * 100,
                        actual_position_pct * 100,
                        f"{max_order_amount:.0f}元" if max_order_amount > 0 else "无",
                        result.order_id,
                        fill_check.status_text,
                        fill_check.status_code,
                    )
                    print(
                        f"  → 委托信息: 策略=D {st.ts_code} {st.name} 委托{shares}股 "
                        f"总委托金额{actual_amount / 10000:.2f}万 "
                        f"已成交金额{checked_amount / 10000:.2f}万 "
                        f"未成交金额{unchecked_amount / 10000:.2f}万 "
                        f"order_id={result.order_id} 提交后状态={fill_check.status_text}({fill_check.status_code})"
                    )
                    if checked_qty > 0:
                        self._record_filled_d_position(result.order_id, checked_qty, checked_price)
                    try:
                        notify(
                            "buy_result",
                            "⏳ D开仓委托未全成",
                            (
                                f"策略=D {st.ts_code} {st.name} 委托{shares}股 @{st.upper_limit:.2f}，"
                                f"总委托金额{actual_amount / 10000:.2f}万，"
                                f"已成交金额{checked_amount / 10000:.2f}万，"
                                f"未成交金额{unchecked_amount / 10000:.2f}万，"
                                f"order_id={result.order_id}，提交后状态={fill_check.status_text}({fill_check.status_code})。"
                            ),
                            level="timeSensitive",
                        )
                    except Exception:
                        pass
                if fill_check.status_code < 0 and fill_check.filled_qty <= 0:
                    self.logger.error(
                        "D委托提交后未在QMT当日委托中确认: %s order_id=%s %d股 %.2f 金额=%.2f",
                        st.ts_code,
                        result.order_id,
                        shares,
                        st.upper_limit,
                        actual_amount,
                    )
                    try:
                        notify(
                            "buy_result",
                            "❌ D开仓委托未确认",
                            (
                                f"{st.ts_code} {st.name} QMT返回order_id={result.order_id}，"
                                f"但提交后未在当日委托中查到。程序计划委托{shares}股，"
                                f"总委托金额{actual_amount / 10000:.2f}万；请立即查看QMT是否实际挂单。"
                            ),
                            level="critical",
                            call=True,
                        )
                    except Exception:
                        pass
                return True
            else:
                self.logger.error("D委托被拒: %s %s", st.ts_code, result.message)
                fail_reason = result.message or "QMT返回拒单，未生成有效委托号"
                st.last_order_fail_reason = fail_reason
                record["order_status"] = "REJECTED_BY_QMT"
                record["order_status_text"] = result.message
                record["failure_reason"] = fail_reason
                try:
                    notify("buy_result", "❌ D开仓委托被拒",
                           f"{st.ts_code} {st.name} 委托被拒：{fail_reason}",
                           level="critical", call=True)
                except Exception:
                    pass
                print(f"  → 委托被拒: {result.message}")
                self.order_placed = False
                self.order_locked_ts_code = ""
                return False
        except Exception as e:
            self.logger.error("下单异常: %s: %s", st.ts_code, e)
            st.last_order_fail_reason = str(e)
            record["order_status"] = "ORDER_EXCEPTION"
            record["order_status_text"] = str(e)
            record["failure_reason"] = str(e)
            try:
                notify("buy_result", "❌ D开仓下单异常",
                       f"{st.ts_code} {st.name} 下单异常：{e}",
                       level="critical", call=True)
            except Exception:
                pass
            print(f"  → 下单异常: {e}")
            self.order_placed = False
            self.order_locked_ts_code = ""
            return False

    @staticmethod
    def _is_terminal_no_fill(fill: Any) -> bool:
        status_code = int(getattr(fill, "status_code", -1) or -1)
        filled_qty = int(getattr(fill, "filled_qty", 0) or 0)
        return filled_qty <= 0 and status_code in {53, 54, 57}

    @staticmethod
    def _describe_order_failure(fill: Any, st: StockState, fallback: str = "") -> str:
        status_code = int(getattr(fill, "status_code", -1) or -1)
        status_text = str(getattr(fill, "status_text", "") or "UNKNOWN")
        raw = getattr(fill, "raw", None) or {}
        if not isinstance(raw, dict):
            raw = {}

        reason_keys = [
            "status_msg", "error_msg", "error_info", "fail_reason", "cancel_reason",
            "order_remark", "remark", "m_strErrorMsg", "m_strStatusMsg",
            "m_strRemark", "m_strOrderRemark", "entrust_status_msg",
        ]
        reasons: list[str] = []
        for key in reason_keys:
            value = raw.get(key)
            if value is None or str(value).strip() == "":
                continue
            text = str(value).strip()
            if text not in reasons:
                reasons.append(f"{key}={text}")

        if fallback:
            reasons.append(f"返回消息={fallback}")

        inference = ""
        segment = classify_market_segment(st.ts_code)
        if status_code == 57:
            inference = "柜台返回废单，委托已终止且0成交"
            if segment == "bj":
                inference += (
                    "；账户已确认开通北交所权限，如仍废单应检查柜台返回原因、"
                    "证券代码格式、价格和申报数量"
                )
        elif status_code == 54:
            inference = "委托已撤且0成交"
        elif status_code == 53:
            inference = "委托部撤但本次查询0成交"
        elif status_code < 0:
            inference = "提交后未在QMT当日委托中确认"

        raw_summary = ""
        if raw:
            useful = []
            for key in sorted(raw.keys()):
                value = raw.get(key)
                if value is None or str(value).strip() == "":
                    continue
                key_l = key.lower()
                if any(token in key_l for token in ["status", "error", "remark", "msg", "reason", "order"]):
                    useful.append(f"{key}={value}")
                if len(useful) >= 8:
                    break
            if useful:
                raw_summary = "；原始字段：" + "，".join(useful)

        parts = [f"状态={status_text}({status_code})"]
        if inference:
            parts.append(inference)
        if reasons:
            parts.append("；".join(reasons))
        if raw_summary:
            parts.append(raw_summary)
        return "；".join(parts)

    def _confirm_submitted_order(self, order_id: str, ts_code: str):
        """下单后短暂等待，再反查当日委托/成交，避免把返回号误当成真实挂单。"""

        from src.broker_adapter import OrderFill

        last_error: Exception | None = None
        last_visible_fill: OrderFill | None = None
        for attempt in range(1, 4):
            time.sleep(1.0)
            try:
                fill = self.broker.get_order_fill(order_id)
                # “已报/待报”只证明券商受理，不代表成交确认结束。旧代码在
                # status=50、filled=0时直接返回，随后到达的真实成交无人回写。
                if fill.filled_qty > 0 or fill.is_terminal:
                    return fill
                if fill.status_code >= 0:
                    last_visible_fill = fill
                    self.logger.info(
                        "D委托已在券商可见但尚未成交，第%d次继续确认: %s "
                        "order_id=%s 状态=%s(%s)",
                        attempt,
                        ts_code,
                        order_id,
                        fill.status_text,
                        fill.status_code,
                    )
                    continue
                self.logger.warning(
                    "D委托提交后第%d次反查未确认: %s order_id=%s",
                    attempt,
                    ts_code,
                    order_id,
                )
            except Exception as e:
                last_error = e
                self.logger.error("D委托提交后第%d次反查异常: %s order_id=%s: %s",
                                  attempt, ts_code, order_id, e)
        if last_visible_fill is not None:
            return last_visible_fill
        status_text = f"QUERY_ERROR:{last_error}" if last_error else "NOT_FOUND_IN_QMT_ORDERS"
        return OrderFill(order_id=str(order_id), status_code=-1, status_text=status_text)

    # ── 14:55 撤单 ────────────────────────────────────────────────────────────

    def cancel_all_d_orders(self) -> None:
        if not self.live_order or self.broker is None:
            self.logger.info("非实盘模式，跳过撤单")
            return
        if not self.session_orders:
            self.logger.info("本会话无D委托")
            return

        print(f"\n{'='*55}")
        print(f"  14:55 撤单 — 共 {len(self.session_orders)} 笔D委托")
        self.logger.warning("14:55撤单，共 %d 笔", len(self.session_orders))

        # 逐笔查询真实成交（get_order_fill 以成交回报为准，避免状态码字符串误判）
        cancelled = failed = 0
        for order_id, ts_code in self.session_orders.items():
            try:
                fill = self.broker.get_order_fill(order_id)
            except Exception as e:
                self.logger.error("查询成交失败: %s order_id=%s: %s", ts_code, order_id, e)
                fill = None

            filled_qty = int(getattr(fill, "filled_qty", 0)) if fill else 0
            fill_price = float(getattr(fill, "avg_price", 0.0)) if fill else 0.0

            if filled_qty > 0:
                status_text = getattr(fill, "status_text", "") if fill else ""
                detail = self.session_order_details.get(order_id, {})
                planned_qty = int(detail.get("shares", 0) or 0)
                planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
                if planned_amount <= 0 and planned_qty > 0:
                    planned_amount = planned_qty * float(detail.get("buy_price", 0.0) or 0.0)
                filled_amount = filled_qty * fill_price
                unfilled_amount = max(planned_amount - filled_amount, 0.0)
                print(
                    f"  {ts_code}  order_id={order_id} → 已成交{filled_qty}/{planned_qty}股，"
                    f"总委托{planned_amount / 10000:.2f}万 已成交{filled_amount / 10000:.2f}万 "
                    f"未成交{unfilled_amount / 10000:.2f}万，状态={status_text}"
                )
                self.logger.warning(
                    "D 14:55成交确认: %s order_id=%s 状态=%s 成交%d/%d股 总委托=%.2f 已成交=%.2f 未成交=%.2f",
                    ts_code,
                    order_id,
                    status_text,
                    filled_qty,
                    planned_qty,
                    planned_amount,
                    filled_amount,
                    unfilled_amount,
                )
                self._record_filled_d_position(order_id, filled_qty, fill_price)
                # 部分成交：撤掉未成残单
                if not getattr(fill, "is_filled", False):
                    ok = self.broker.cancel_order(order_id)
                    self.logger.warning("D部分成交 %s 已成%d股(%s)，撤残单%s",
                                        ts_code, filled_qty, status_text,
                                        "已发" if ok else "失败")
                else:
                    self.logger.info(
                        "D委托已全部成交，无需撤单: %s order_id=%s 持仓%d股 @%.2f",
                        ts_code,
                        order_id,
                        filled_qty,
                        fill_price,
                    )
                continue

            ok = self.broker.cancel_order(order_id)
            if ok:
                cancelled += 1
                print(f"  {ts_code}  order_id={order_id} → 未成交，撤单已发")
                detail = self.session_order_details.get(order_id, {})
                planned_qty = int(detail.get("shares", 0) or 0)
                planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
                self.logger.warning("D委托未成交已撤单: %s order_id=%s 委托%d股 金额=%.2f",
                                    ts_code, order_id, planned_qty, planned_amount)
                try:
                    notify(
                        "buy_result",
                        "⚠️ D开仓未成交已撤单",
                        (
                            f"{ts_code} order_id={order_id} 委托{planned_qty}股，"
                            f"总委托金额{planned_amount / 10000:.2f}万，已成交0.00万，"
                            f"14:55已撤单。"
                        ),
                        level="timeSensitive",
                    )
                except Exception:
                    pass
            else:
                failed += 1
                print(f"  {ts_code}  order_id={order_id} → 撤单失败！请手动检查")
                self.logger.error("撤单失败: %s order_id=%s", ts_code, order_id)
                try:
                    notify(
                        "buy_result",
                        "❌ D未成交撤单失败",
                        f"{ts_code} order_id={order_id} 未成交且撤单失败，请立即检查QMT。",
                        level="critical",
                        call=True,
                    )
                except Exception:
                    pass

        print(f"  结果: 撤单={cancelled}笔  失败={failed}笔")
        print(f"{'='*55}\n")

    def _record_filled_d_position(self, order_id: str, filled_qty: int | None = None,
                                  fill_price: float | None = None) -> None:
        detail = self.session_order_details.get(order_id)
        if not detail:
            self.logger.warning("找不到D成交委托明细，无法写入持仓: order_id=%s", order_id)
            return
        buy_date = str(detail.get("buy_date", today_beijing().strftime("%Y%m%d")))
        shares = int(filled_qty) if filled_qty and filled_qty > 0 else int(detail.get("shares", 0))
        buy_price = float(fill_price) if fill_price and fill_price > 0 else float(detail.get("buy_price", 0.0))
        recorded_filled_qty = max(
            int(detail.get("recorded_filled_qty", 0) or 0),
            0,
        )
        if shares <= recorded_filled_qty:
            return
        planned_exit_date = next_trade_day(buy_date, 2)
        payload = {
            "order_id": str(order_id),
            "ts_code": str(detail.get("ts_code", "")),
            "name": str(detail.get("name", "")),
            "signal_date": buy_date,
            "buy_date": buy_date,
            "planned_exit_date": planned_exit_date,
            "shares": shares,
            "buy_price": buy_price,
            "strategy_leg": "D",
            "planned_order_qty": int(detail.get("shares", shares) or shares),
        }
        if self.position_recorder is not None:
            # daemon端使用同order_id累计成交幂等更新，并同步事务意图。
            self.position_recorder(payload)
        else:
            # 仅保留给非实盘离线演练；实盘构造器已强制必须提供callback。
            positions = load_position_records()
            existing = next(
                (pos for pos in positions if str(pos.get("order_id", "")) == str(order_id)),
                None,
            )
            if existing is None:
                positions.append({
                    **payload,
                    "status": "open",
                    "sell_date": None,
                    "sell_price": None,
                })
            elif shares > int(existing.get("shares", 0) or 0):
                existing["shares"] = shares
                existing["buy_price"] = buy_price
            save_position_records(positions)
        # 同一委托的成交回报是累计数量；只在持仓投影成功后推进确认水位。
        detail["recorded_filled_qty"] = shares
        self.position_opened = True
        self.logger.warning(
            "D持仓信息已写入持仓账本: 策略=D order_id=%s ts_code=%s %d股 @%.2f 买入日=%s 默认计划平仓日=%s；若次日有A/B/C接力则T+1开盘先卖D",
            order_id,
            detail.get("ts_code", ""),
            shares,
            buy_price,
            buy_date,
            planned_exit_date,
        )
        try:
            planned_shares = int(detail.get("shares", 0) or 0)
            planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
            if planned_amount <= 0 and planned_shares > 0:
                planned_amount = planned_shares * float(detail.get("buy_price", 0.0) or 0.0)
            filled_amount = shares * buy_price
            unfilled_amount = max(planned_amount - filled_amount, 0.0)
            partial = planned_shares > 0 and shares < planned_shares
            if partial:
                body = (
                    f"策略=D {detail.get('ts_code', '')} {detail.get('name', '')} "
                    f"成交{shares}/{planned_shares or shares}股 @{buy_price:.2f}。"
                    f"总委托金额{planned_amount / 10000:.2f}万，"
                    f"已成交金额{filled_amount / 10000:.2f}万，"
                    f"未成交金额{unfilled_amount / 10000:.2f}万。"
                )
            else:
                body = (
                    f"策略=D {detail.get('ts_code', '')} {detail.get('name', '')} "
                    f"持仓{shares}股 成本{buy_price:.2f} 市值{filled_amount / 10000:.2f}万。"
                )
            notify(
                "buy_result",
                "⚠️ D开仓部分成交" if partial else "✅ D持仓信息",
                body,
                level="timeSensitive" if partial else "active",
            )
            # D竞价卖容量提醒(2026-07-24 用户要求):历史36笔标的实算,次日开盘
            # 竞价的单笔安全卖单上限中位约300万(竞价参与≤10%口径)。买入金额≈
            # 次日9:23要卖的金额,达到300万说明D已进入竞价冲击灰色区——提醒用户
            # 验证"竞价实际成交价 vs 开盘价"偏差,决定是否给D卖出加方案。
            if filled_amount >= 3_000_000:
                notify(
                    "buy_result",
                    "📏 D单仓已达300万:该验证竞价冲击了",
                    f"本次D买入成交{filled_amount / 10000:.0f}万,明日9:23集合竞价卖出规模已达"
                    f"历史标的安全线(中位约300万,小票更低)。请对比明日实际竞价成交价与开盘价的偏差:"
                    f"偏差明显(如>0.5%)则需考虑D卖出方案(竞价部分卖+盘中卖余量),"
                    f"并把D纳入执行对账(开盘价基准)持续跟踪。",
                    level="timeSensitive",
                )
        except Exception:
            pass

    # ── 轮询 ─────────────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        batches = self._batches()
        if not batches:
            return
        if strategy_d_checkpoint_recovery_block_path(self.checkpoint_path).exists():
            self.checkpoint_io_degraded = True
            if not self.checkpoint_io_reason:
                self.checkpoint_io_reason = "检测到尚未由新READY解除的恢复阻断标记"
        # 先原子摧毁上一轮READY检查点。若进程在本轮扫描中途退出，新进程只能看到
        # SCAN_IN_PROGRESS，绝不会拿上一轮状态跨过一段未知行情继续计数。
        if not self._invalidate_checkpoint(
            D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
            f"第{self.scan_round + 1}轮全市场扫描进行中",
        ):
            self._enter_checkpoint_io_degraded(
                "无法使上一轮D检查点原子切换为SCAN_IN_PROGRESS"
                + (
                    f":{self._last_checkpoint_io_error}"
                    if self._last_checkpoint_io_error
                    else ""
                )
            )
        updated_count = 0
        failures: list[str] = []
        for batch in batches:
            try:
                quotes = self.broker.get_full_tick(batch)
            except Exception as e:
                self.logger.warning("get_full_tick 异常: %s", e)
                failures.append(str(e))
                continue
            if not quotes:
                failures.append(f"空行情批次({len(batch)}只)")
                continue
            requested_codes = {str(code) for code in batch}
            returned_codes = {str(code) for code in quotes}
            missing_codes = requested_codes.difference(returned_codes)
            unexpected_codes = returned_codes.difference(requested_codes)
            if missing_codes or unexpected_codes:
                failures.append(
                    "行情批次成分不完整"
                    f"(请求{len(requested_codes)}/返回{len(returned_codes)}/"
                    f"缺失{len(missing_codes)}/意外{len(unexpected_codes)})"
                )
                continue
            updated_count += len(quotes)
            self._update_states(quotes)
        if failures:
            self.path_integrity_failed = True
            self.path_integrity_reason = (
                f"第{self.scan_round + 1}轮有{len(failures)}/{len(batches)}个批次未完整返回；"
                "无法证明全市场09:30起封板/炸板路径完整"
            )
            self.logger.error("D完整路径已永久失效：%s", self.path_integrity_reason)
            if not self._path_failure_notified:
                self._path_failure_notified = True
                try:
                    notify(
                        "buy_result",
                        "⛔ D实时路径不完整，今日停止开仓",
                        f"{self.path_integrity_reason}。为与回测严格对齐，"
                        "今日D只保留监控日志，不会买入。",
                        level="critical",
                    )
                except Exception:
                    pass
        self.scan_round += 1
        self.last_scan_updated_count = updated_count
        self.logger.info("完成全市场扫描: round=%d updated=%d states=%d", self.scan_round, updated_count, len(self.states))
        # 历史D只取strong，不含very_strong；实时代理必须同时满足上下界。
        sealed = self.sealed_ever_count
        if (
            not self.factor_union_active
            and not self._strong_notified
            and self._sentiment_passes()
        ):
            self._strong_notified = True
            self.logger.warning(
                "📈 情绪进入D历史strong代理区间：当前封板%d只(%d~%d)。",
                sealed,
                self.sentiment_current_min,
                self.sentiment_current_max,
            )
            try:
                notify(
                    "buy_result",
                    "📈 D满足开仓条件",
                    f"全市场当前封板{sealed}只，位于D历史strong代理区间"
                    f"{self.sentiment_current_min}~{self.sentiment_current_max}，"
                    "且不属于very_strong；"
                    f"14:00后将扫描首板回封发出买入信号。",
                )
            except Exception as exc:
                self.logger.warning("情绪转强推送失败：%s", exc)
        if self.path_integrity_failed:
            self._invalidate_checkpoint(
                D_CHECKPOINT_STATUS_CLOSED,
                self.path_integrity_reason or "D全市场路径已失效",
            )
            return

        if self.checkpoint_io_degraded:
            # I/O降级期间内存路径仍连续，但必须先把本轮完整状态持久化并解除阻断，
            # 才能重新判断BUY。这样临时锁不会关闭全天D，也不会让陈旧READY被恢复。
            if self._save_ready_checkpoint() and self._clear_checkpoint_io_degraded():
                self._check_and_fire()
                if not self._save_ready_checkpoint():
                    self._enter_checkpoint_io_degraded(
                        "D恢复信号判断后未能保存最新READY"
                        + (
                            f":{self._last_checkpoint_io_error}"
                            if self._last_checkpoint_io_error
                            else ""
                        )
                    )
            else:
                self.logger.warning(
                    "D检查点I/O仍处于降级保护，本轮全市场行情虽完整，但不判断BUY；"
                    "下一轮继续尝试生成新的原子READY。"
                )
            return

        self._check_and_fire()
        if not self._save_ready_checkpoint():
            self._enter_checkpoint_io_degraded(
                "D本轮完整路径未能保存为READY"
                + (
                    f":{self._last_checkpoint_io_error}"
                    if self._last_checkpoint_io_error
                    else ""
                )
            )

    def status_line(self) -> str:
        hhmm = now_hhmm()
        watching = sum(1 for st in self.states.values()
                       if st.watch_alerted and not st.buy_signaled)
        bought = sum(1 for st in self.states.values() if st.buy_signaled)
        order_text = "无"
        if self.session_order_details:
            detail_parts = []
            for order_id, detail in self.session_order_details.items():
                detail_parts.append(
                    f"{detail.get('ts_code', '')}({detail.get('shares', 0)}股 order_id={order_id})"
                )
            order_text = ";".join(detail_parts)
        elif self.order_locked_ts_code:
            order_text = f"{self.order_locked_ts_code}(已锁定)"
        if self._sentiment_passes():
            sentiment = f"strong代理({self.sealed_ever_count}只)"
        elif self.sealed_ever_count < self.sentiment_current_min:
            sentiment = (
                f"不足({self.sealed_ever_count}只，需>="
                f"{self.sentiment_current_min})"
            )
        else:
            sentiment = (
                f"very_strong代理({self.sealed_ever_count}只，D上限="
                f"{self.sentiment_current_max})"
            )
        return (
            f"[{hhmm_to_str(hhmm)}] "
            f"扫过{len(self.states)}只 | {sentiment} | "
            f"观察={watching} 买入={bought} | D委托/持仓={order_text} | 全市场扫描轮次={self.scan_round}"
        )

    def _wait_without_open_scan(self) -> None:
        """已有有效委托/持仓后不再跑开仓扫描，只等待撤单窗口或退出。"""

        if self.position_opened and not self.waiting_order_only:
            self.logger.warning(
                "D已形成持仓，停止开仓扫描；后续由主守护进程账户心跳和平仓检查接管。"
            )
            print("D已形成持仓，停止开仓扫描；后续由主守护进程账户心跳和平仓检查接管。")
            return

        if self.waiting_order_only:
            self.logger.warning(
                "D已有有效委托/部分成交，停止新的开仓扫描；持续确认真实成交，"
                "%s撤未成残单。",
                hhmm_to_str(CANCEL_HHMM),
            )
            print(
                "D已有有效委托/部分成交，停止新的开仓扫描；"
                f"持续确认真实成交，{hhmm_to_str(CANCEL_HHMM)}撤未成残单。"
            )
            while now_hhmm() < CANCEL_HHMM:
                if self._reconcile_active_d_orders_once():
                    self.logger.warning(
                        "D活动委托已全部成交并写入策略持仓，成交确认生命周期完成。"
                    )
                    return
                time.sleep(ORDER_FILL_POLL_INTERVAL_SEC)
            self.cancel_all_d_orders()

    def _reconcile_active_d_orders_once(self) -> bool:
        """推进D活动委托的成交状态；成交事实只能来自同一QMT委托。"""

        if not self.session_orders or self.broker is None:
            return False

        all_filled = True
        for order_id, ts_code in self.session_orders.items():
            detail = self.session_order_details.get(order_id, {})
            planned_qty = max(int(detail.get("shares", 0) or 0), 0)
            try:
                fill = self.broker.get_order_fill(order_id)
            except Exception as exc:
                all_filled = False
                self.logger.error(
                    "D活动委托成交确认异常: %s order_id=%s: %s",
                    ts_code,
                    order_id,
                    exc,
                )
                continue

            filled_qty = max(int(getattr(fill, "filled_qty", 0) or 0), 0)
            fill_price = float(getattr(fill, "avg_price", 0.0) or 0.0)
            if fill_price <= 0:
                fill_price = float(detail.get("buy_price", 0.0) or 0.0)
            if filled_qty > int(detail.get("recorded_filled_qty", 0) or 0):
                self._record_filled_d_position(order_id, filled_qty, fill_price)
                self.logger.warning(
                    "D活动委托成交已回写: %s order_id=%s 累计成交%d/%d股 @%.2f",
                    ts_code,
                    order_id,
                    filled_qty,
                    planned_qty,
                    fill_price,
                )

            if planned_qty <= 0 or filled_qty < planned_qty:
                all_filled = False

        if all_filled:
            self.position_opened = True
            self.waiting_order_only = False
        return all_filled

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        session_start_hhmm = now_hhmm()
        if self.original_session_start_hhmm <= 0:
            self.original_session_start_hhmm = session_start_hhmm
        resumable_in_memory_path = (
            self.scan_round > 0
            and bool(self.states)
            and not self.path_integrity_failed
        )
        self.setup()

        # 回测使用完整日内路径。午后重启若从当前快照重新计数，会把早盘已封/已炸历史
        # 当成不存在，从而把t_board误判成multi_open。无法重建完整路径时必须fail-closed。
        checkpoint_reason = ""
        if not intraday_history_is_complete(session_start_hhmm) and not resumable_in_memory_path:
            restored, checkpoint_reason = self._restore_ready_checkpoint()
            resumable_in_memory_path = restored
        if not intraday_history_is_complete(session_start_hhmm) and not resumable_in_memory_path:
            reason = (
                f"D监控于{hhmm_to_str(session_start_hhmm)}启动，晚于完整路径截止"
                f"{hhmm_to_str(D_LATEST_COMPLETE_HISTORY_START_HHMM)}；缺少早盘首次封板/炸板历史，"
                f"且检查点不可恢复({checkpoint_reason or '无检查点'})，"
                "按回测严格对齐口径今日禁止D开仓"
            )
            self.logger.error(reason)
            print(f"[D跳过] {reason}")
            try:
                notify(
                    "buy_result",
                    "⛔ D因午后重启停止开仓",
                    reason,
                    level="critical",
                )
            except Exception as exc:
                self.logger.warning("D午后重启阻断推送失败: %s", exc)
            return
        if not intraday_history_is_complete(session_start_hhmm):
            if self._restored_from_checkpoint:
                self.logger.warning(
                    "D监控已安全恢复原子检查点：已保留%d轮、%d只逐票状态；%s。"
                    "下一轮将从这些状态继续识别真实封板/炸板转换。",
                    self.scan_round,
                    len(self.states),
                    checkpoint_reason,
                )
            else:
                self.logger.warning(
                    "D监控使用同一对象内存续跑：已保留%d轮、%d只状态，"
                    "不使用重启后快照伪造早盘路径。",
                    self.scan_round, len(self.states),
                )

        # ── 串行单仓检测：券商仍有任何旧策略仓就直接退出，不做D ─────────────
        occupied, desc = check_strategy_position_occupied(self.broker if self.live_order else None)
        if occupied:
            msg = (
                f"\n{'='*55}\n"
                f"  [跳过] 今日检测到旧策略仓，D策略不启动\n"
                f"  持仓: {desc}\n"
                f"  原因: 已取消衔接开仓；券商确认实际清仓前禁止D买入\n"
                f"{'='*55}\n"
            )
            print(msg)
            self.logger.info("旧策略仓未实际清空，D监控跳过: %s", desc)
            return

        while now_hhmm() < self.monitor_start_hhmm:
            remaining_min = (self.monitor_start_hhmm - now_hhmm())
            print(f"等待开盘... 距{hhmm_to_str(self.monitor_start_hhmm)}还有约{remaining_min}分钟")
            time.sleep(60)

        batches = self._batches()
        print(
            f"\n开始扫描 — {len(self.universe)}只股票，"
            f"{len(batches)}批x{POLL_BATCH_SIZE}，"
            f"每轮扫完整市场后等待{POLL_INTERVAL_SEC}s\n"
            + (
                f"  真实新回封 → 匹配{len(self.factor_profiles)}条发布if条件 "
                "| 14:55 → 自动撤单\n"
                if self.factor_union_active
                else "  09:35 → 观察提醒  |  14:00 → 买入信号  |  14:55 → 自动撤单\n"
            )
        )

        try:
            while now_hhmm() < CANCEL_HHMM:
                tracking_allowed, tracking_reason = self._tracking_gate_allows_monitor()
                if not tracking_allowed:
                    self.logger.info(
                        "[D TRACKING STOPPED] 其他策略候选已成交或路径继续门禁失效，"
                        "停止D只读扫描：%s",
                        tracking_reason,
                    )
                    self._invalidate_checkpoint(
                        D_CHECKPOINT_STATUS_CLOSED,
                        f"D只读扫描终止:{tracking_reason}",
                    )
                    return
                self.poll_once()
                print(self.status_line())
                if self.position_opened or self.waiting_order_only:
                    self._wait_without_open_scan()
                    return
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n用户中断，执行撤单流程...")

        self.cancel_all_d_orders()
        self._invalidate_checkpoint(
            D_CHECKPOINT_STATUS_CLOSED,
            "D当日监控已到撤单边界并正常结束",
        )
        self._print_summary()

    def _print_summary(self) -> None:
        watched = [st for st in self.states.values() if st.watch_alerted and not st.buy_signaled]
        bought = [st for st in self.states.values() if st.buy_signaled]
        print(f"\n=== 今日策略D监控完毕 ===")
        print(f"观察提醒: {len(watched) + len(bought)} 次 | 买入信号: {len(bought)} 次")
        for st in bought:
            order_str = f"order_id={st.order_id}" if st.order_id else "仅提醒"
            source = "正式因子回封" if self.factor_union_active else "14:00后真实回封"
            strict_path = self._strict_path_for(st) if self.factor_union_active else None
            reseal_hhmm = (
                strict_path.last_reseal_hhmm if strict_path else st.last_seal_hhmm
            )
            open_times = strict_path.open_times if strict_path else st.open_times_today
            print(f"  [BUY/{source}] {st.ts_code} {st.name} "
                  f"重封={hhmm_to_str(reseal_hhmm)} 炸板={open_times}次 {order_str}")
        print(f"信号记录: {self.signal_csv}")
        self.logger.info("监控结束 观察=%d 买入=%d", len(watched) + len(bought), len(bought))

    def _save_signals(self) -> None:
        try:
            pd.DataFrame(self.signal_records).to_csv(self.signal_csv, index=False)
        except Exception as e:
            self.logger.warning("保存信号CSV失败: %s", e)


# ── 入口 ─────────────────────────────────────────────────────────────────────

def build_broker(config: dict, live_order: bool) -> Any:
    try:
        from src.qmt_adapter import QMTBrokerAdapter
        from src.qmt_single_owner import assert_standalone_qmt_allowed

        assert_standalone_qmt_allowed(
            PROJECT_ROOT,
            caller="monitor_strategy_d_intraday.py独立入口",
        )
        adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
        adapter.connect()
        mode = "实盘" if live_order else "行情"
        print(f"QMT已连接（{mode}模式）。")
        return adapter
    except Exception as e:
        if live_order:
            raise
        print(f"[警告] QMT连接失败（{e}），将无法获取实时行情。")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="策略D盘中监控")
    parser.add_argument("--live-order", action="store_true", help="实盘下单（默认仅提醒）")
    parser.add_argument("--dry-run", action="store_true", help="打印配置后退出")
    parser.add_argument("--start-hhmm", type=int, default=MONITOR_START_HHMM,
                        help=f"开始扫描时间，默认{MONITOR_START_HHMM}")
    args = parser.parse_args()

    if args.live_order:
        raise RuntimeError(
            "策略D独立进程实盘下单入口已退役；"
            "请由trading_daemon内嵌监控器经统一交易意图和唯一串行QMT通道执行。"
        )

    today_str = today_beijing().strftime("%Y%m%d")
    signal_dir = PROJECT_ROOT / "reports" / "strategy_d"
    signal_dir.mkdir(parents=True, exist_ok=True)
    signal_csv = signal_dir / f"intraday_signals_{today_str}.csv"

    logger = setup_logger(
        log_dir=PROJECT_ROOT / "logs",
        log_file=f"strategy_d_monitor_{today_str}.log",
        level="INFO",
    )
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    allowed_segments = configured_allowed_segments(config)
    position_pct = configured_position_pct(config)
    min_open_times = configured_min_open_times(config)
    max_open_times = configured_max_open_times(config)
    preferred_open_times = configured_preferred_open_times(config)
    min_fill_probability = configured_min_fill_probability(config)
    first_time_buckets = configured_first_time_buckets(config)
    tail_reseal_hhmm = configured_tail_reseal_hhmm(config)
    sentiment_min, sentiment_max = configured_sentiment_bounds(config)
    factor_release_path = configured_factor_release_path(config)
    factor_release = load_factor_release(factor_release_path)
    factor_release_active = release_uses_factor_union(factor_release)

    if args.dry_run:
        print("=== 策略D监控配置 ===")
        print(
            f"  D发布版本: {factor_release.get('release_id', '')} | "
            f"模式={factor_release.get('strategy_mode', '')} | "
            f"if条件数={len(factor_release.get('profiles', []))}"
        )
        if factor_release_active:
            print("  正式D条件: 最佳半年因子条件（旧D时间/情绪/炸板次数门不参与）")
            for profile in factor_release.get("profiles", []):
                conditions = " AND ".join(
                    f"{name}={value}"
                    for name, value in profile.get("conditions", {}).items()
                )
                print(f"    {profile.get('profile_id', '')}: {conditions}")
            print(
                "  因子排序口径: 当日最早合格回封；同一分钟优先炸板2次，再按代码排序"
            )
            print(
                "  公共安全门: 首板、非ST、当前封板、真实新回封、09:30~14:54、"
                "成交概率可靠"
            )
        else:
            print(
                f"  情绪阈值: 全市场当前封板涨停数 {sentiment_min}~{sentiment_max} "
                "(代理历史strong，不含very_strong)"
            )
            print(
                f"  D排序口径: 优先炸板{preferred_open_times}次，再按实时封单金额 / "
                "流通市值(fd_amount_to_circ_mv)降序"
            )
            print(f"  炸板次数: {min_open_times}~{max_open_times}（multi_open）")
            print(
                f"  首次封板时段: {','.join(sorted(first_time_buckets))} | "
                f"最后真实回封>={hhmm_to_str(tail_reseal_hhmm)}"
            )
        print(
            f"  成交概率: >={min_fill_probability:.0%}且历史匹配可靠，"
            "实时复算失败则禁止开仓"
        )
        print(f"  允许市场分段: {','.join(sorted(allowed_segments))}")
        print(f"  目标开仓仓位: {position_pct:.1%}")
        print("  补偿机制: 已取消，只尝试D排序第1名")
        print(f"  扫描开始: {hhmm_to_str(args.start_hhmm)}")
        if factor_release_active:
            print("  因子信号: 09:30~14:54发生真实新回封时即时匹配正式条件")
        else:
            print(
                f"  观察提醒: {hhmm_to_str(WATCH_START_HHMM)} 起（只提醒，不升级买入）"
            )
            print(
                f"  买入信号: {hhmm_to_str(SIGNAL_START_HHMM)} 起发生真实回封才允许BUY"
            )
        print(
            f"  重启保护: {hhmm_to_str(D_LATEST_COMPLETE_HISTORY_START_HHMM)}后才启动时，"
            "因缺少完整日内路径而禁止当日D开仓"
        )
        print(f"  撤单时间: {hhmm_to_str(CANCEL_HHMM)}")
        print(f"  实盘下单: {'是' if args.live_order else '否（仅提醒）'}")
        print(f"  信号输出: {signal_csv}")
        return

    broker = build_broker(config, args.live_order)
    if broker is None:
        print("[错误] 策略D盘中监控必须连接 QMT 实时行情；当前无法获取行情，退出。")
        import sys; sys.exit(1)

    monitor = StrategyDMonitor(
        broker=broker,
        live_order=args.live_order,
        logger=logger,
        signal_csv=signal_csv,
        monitor_start_hhmm=args.start_hhmm,
        allowed_segments=allowed_segments,
        position_pct=position_pct,
        config=config,
    )
    try:
        monitor.run()
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    main()
