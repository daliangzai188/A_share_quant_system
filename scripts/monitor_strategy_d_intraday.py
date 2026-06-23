"""
策略D盘中监控：首板打板信号检测 + 14:55自动撤单

【两档信号设计】
  观察档（09:35-14:00 回封）:
    首板 + multi_open + strong情绪 + 当前处于涨停 → 发出 [WATCH] 提醒
    → 继续跟踪，若14:00时仍封板 → 自动升级为买入信号

  买入档（14:00+ 回封，或观察中升级）:
    满足全部条件 + 当前处于涨停 + 重封时间>=14:00（或已在观察名单中）→ 发出 [BUY] 信号
    → 涨停价挂单

  14:55: 撤销所有D腿未成交委托

运行方式：
  python scripts/monitor_strategy_d_intraday.py              # 仅提醒，不下单
  python scripts/monitor_strategy_d_intraday.py --live-order # 实盘下单（需QMT）
  python scripts/monitor_strategy_d_intraday.py --dry-run    # 打印配置后退出

日志/输出：
  logs/strategy_d_monitor_YYYYMMDD.log
  reports/strategy_d/intraday_signals_YYYYMMDD.csv
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from dotenv import load_dotenv

from src.utils.logger import setup_logger
from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.notify import notify

load_dotenv(PROJECT_ROOT / ".env")

# ── 策略参数 ──────────────────────────────────────────────────────────────────
SENTIMENT_STRONG_MIN = 88    # 全市场当前封板涨停数 >= 此值 → strong情绪（14:00封板数校准值，对应回测收盘≥100）
D_MAX_OPEN_TIMES = None      # 炸板次数不限（近2年数据：strong情绪天去掉限制不减少样本，多候选按封单比选）
WATCH_START_HHMM = 935       # 09:35 开始发出观察提醒
SIGNAL_START_HHMM = 1400     # 14:00 开始发出买入信号 / 观察升级
CANCEL_HHMM = 1455           # 14:55 撤销所有未成交D委托
POLL_BATCH_SIZE = 500        # 每次 get_full_tick 的股票数量
POLL_INTERVAL_SEC = 30       # 每批轮询间隔（秒）
MONITOR_START_HHMM = 930     # 脚本等待开始扫描的时间（集合竞价结束后）
D_POSITION_PCT = 0.80        # 默认仓位比例，优先使用 config.json/strategy_d/position_pct
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
    # 两档信号状态
    watch_alerted: bool = False    # 观察提醒已发出
    buy_signaled: bool = False     # 买入信号已发出
    order_id: str = ""


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


def check_abc_position_occupied() -> tuple[bool, str]:
    """检查是否有未平仓的ABC持仓。有持仓则返回(True, 持仓描述)，否则(False, '')。"""
    pos_file = PROJECT_ROOT / "data" / "processed" / "positions.json"
    if not pos_file.exists():
        return False, ""
    try:
        import json
        positions = json.loads(pos_file.read_text(encoding="utf-8"))
        open_pos = [p for p in positions if p.get("status") == "open"
                    and p.get("strategy_leg", "").upper() != "D"]
        if not open_pos:
            return False, ""
        desc = ", ".join(f"{p['ts_code']}({p.get('strategy_leg','?')})" for p in open_pos)
        return True, desc
    except Exception as e:
        return False, f"读取持仓失败({e})"


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


def get_account_cash(broker) -> float:
    try:
        return broker.query_account().available_cash
    except Exception:
        return 0.0


def calc_shares(cash: float, price: float, position_pct: float) -> int:
    if price <= 0:
        return 0
    return max(int(cash * position_pct / price / 100) * 100, 0)


def calc_shares_below_amount(cash: float, price: float, position_pct: float, max_order_amount: float) -> int:
    if price <= 0:
        return 0
    target_amount = cash * position_pct
    if max_order_amount > 0:
        target_amount = min(target_amount, max_order_amount)
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
                 config: dict[str, Any] | None = None) -> None:
        self.broker = broker
        self.live_order = live_order
        self.logger = logger
        self.signal_csv = signal_csv
        self.monitor_start_hhmm = monitor_start_hhmm
        self.allowed_segments = allowed_segments or set(DEFAULT_ALLOWED_SEGMENTS)
        self.position_pct = position_pct
        self.config = config or {}

        self.yesterday_limit_codes: set[str] = set()
        self.universe: list[str] = []
        self.name_map: dict[str, str] = {}
        self.states: dict[str, StockState] = {}
        self.scan_round = 0

        # 本次会话下单记录 {order_id: ts_code}
        self.session_orders: dict[str, str] = {}
        self.session_order_details: dict[str, dict[str, Any]] = {}
        self.signal_records: list[dict] = []
        self.order_placed: bool = False   # 本会话已触发BUY，不再对其他股票下单
        self.order_locked_ts_code: str = ""
        self.limit_price_fallback_logged: bool = False
        self._strong_notified: bool = False     # 情绪转强(≥阈值)只推送一次

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def setup(self) -> None:
        self.logger.info("=== 策略D盘中监控启动 ===")
        self.yesterday_limit_codes = load_yesterday_limit_codes()
        full_universe = load_stock_universe()
        self.universe = filter_universe_by_segments(full_universe, self.allowed_segments)
        self.name_map = load_stock_names()
        segment_counts: dict[str, int] = {}
        for code in self.universe:
            segment = classify_market_segment(code)
            segment_counts[segment] = segment_counts.get(segment, 0) + 1
        self.logger.info(
            "宇宙: %d只(原始%d只) | 允许分段=%s | 分段数量=%s | 昨日涨停(排除首板): %d只 | 全市场扫描: %d批x%d只/轮，轮间隔%d秒",
            len(self.universe), len(full_universe), ",".join(sorted(self.allowed_segments)),
            segment_counts, len(self.yesterday_limit_codes),
            len(self._batches()), POLL_BATCH_SIZE, POLL_INTERVAL_SEC,
        )
        self.logger.info("D开仓仓位: %.0f%%", self.position_pct * 100)

    def _batches(self) -> list[list[str]]:
        return [self.universe[i: i + POLL_BATCH_SIZE]
                for i in range(0, len(self.universe), POLL_BATCH_SIZE)]

    # ── 状态更新 ──────────────────────────────────────────────────────────────

    @property
    def sealed_ever_count(self) -> int:
        """全市场【当前正封在涨停】的家数（瞬时快照，每轮刷新）。

        与回测口径一致：回测数的是收盘封住的涨停(limit==U)，临近收盘时"当前封板数"≈"收盘涨停数"。
        只看每只票最近一次轮询的封板状态(was_sealed)，炸板打开的不计、回封的计入。
        """
        return sum(1 for st in self.states.values() if st.was_sealed)

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
                )
            st = self.states[ts_code]
            st.upper_limit = upper_limit

            if at_limit:
                # 当前封在涨停（炸板历史不影响：只看此刻是否封板）
                if not st.ever_sealed:
                    st.ever_sealed = True
                    st.first_seal_hhmm = hhmm
                if not st.was_sealed:
                    # 非涨停 → 涨停：记录重封时间
                    st.last_seal_hhmm = hhmm
                st.was_sealed = True
                # 更新封单量（涨停买一量，单位：股）
                if snap.bid_volumes:
                    st.bid_vol = snap.bid_volumes[0]
            else:
                if st.was_sealed:
                    # 涨停 → 非涨停：炸板
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
        """通用过滤：首板 + multi_open + open_times <= 3 + 当前涨停 + strong情绪。"""
        if ts_code in self.yesterday_limit_codes:  # 排除2板+
            return False
        if not st.was_sealed:                      # 当前不在涨停
            return False
        if st.open_times_today < 1:                # 未曾炸板
            return False
        if D_MAX_OPEN_TIMES is not None and st.open_times_today > D_MAX_OPEN_TIMES:
            return False
        if self.sealed_ever_count < SENTIMENT_STRONG_MIN:  # 情绪不足
            return False
        return True

    def _score(self, st: StockState) -> float:
        """打分逻辑（满分100）：基于历史数据统计，分越高次日溢价期望越高。

        当前策略D回测口径：
          - 硬过滤仍保持首板 + multi_open + open_times <= 3 + 情绪达标。
          - 近两年首板研究显示，“炸板2次”不能硬过滤，但多候选时优先它
            可以改善当前D组合的胜率和回撤。
        结论：炸板2次优先、重封越早越好、封单越大越稳。
        """
        score = 0.0

        # 炸板次数（40分）：多候选时优先炸板2次；1次次之；3次保留但降权。
        score += {2: 40, 1: 30, 3: 10}.get(st.open_times_today, 10)

        # 重封时间（40分）：越早越好（早封说明买气更强，历史溢价更高）
        t = st.last_seal_hhmm
        if t < 1000:
            score += 40
        elif t < 1200:
            score += 30
        elif t < 1300:
            score += 20
        elif t < 1400:
            score += 15
        elif t < 1430:
            score += 10
        else:
            score += 5

        # 封单量（20分）：涨停买一量，越大封板越稳
        vol = st.bid_vol  # 单位：股
        if vol >= 500_000:
            score += 20
        elif vol >= 200_000:
            score += 15
        elif vol >= 50_000:
            score += 10
        else:
            score += 5

        return score

    def _check_and_fire(self) -> None:
        if self.order_placed:
            return

        hhmm = now_hhmm()
        buy_candidates: list[StockState] = []

        for ts_code, st in self.states.items():
            if st.buy_signaled:
                continue
            if not self._passes_base_filters(ts_code, st):
                continue

            # ── 场景一：14:00后 → 收集BUY候选，稍后打分排序 ─────────────────
            if hhmm >= SIGNAL_START_HHMM:
                if st.last_seal_hhmm >= SIGNAL_START_HHMM or st.watch_alerted:
                    buy_candidates.append(st)
                continue

            # ── 场景二：WATCH窗口 → 逐个发提醒 ──────────────────────────────
            if hhmm >= WATCH_START_HHMM and not st.watch_alerted:
                self._fire_watch_alert(st)

        # 有BUY候选：打分排序，只对最高分那只下单，其余跳过
        if not buy_candidates:
            return

        scored = sorted(buy_candidates, key=self._score, reverse=True)
        best = scored[0]
        best_score = self._score(best)

        if len(scored) > 1:
            skip_info = "  ".join(
                f"{s.ts_code}({self._score(s):.0f}分)" for s in scored[1:]
            )
            self.logger.info(
                "[BUY SKIP] 本轮%d只候选，跳过低分: %s", len(scored) - 1, skip_info,
            )
            print(f"  [多候选] 共{len(scored)}只，跳过: {skip_info}")

        self.logger.info(
            "[BUY BEST] 最高分 %.0f分: %s %s  炸板%d次 重封%s 封单%.1f万股",
            best_score, best.ts_code, best.name, best.open_times_today,
            hhmm_to_str(best.last_seal_hhmm), best.bid_vol / 10000,
        )
        self._fire_buy_signal(best)

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
            f"  → 持续关注：若14:00时仍封板，自动升级买入信号\n"
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

    def _fire_buy_signal(self, st: StockState) -> None:
        if self.order_placed:
            self.logger.info("[BUY SKIP] 已锁定本轮D委托: %s，跳过 %s", self.order_locked_ts_code, st.ts_code)
            return
        hhmm = now_hhmm()
        st.buy_signaled = True
        self.order_placed = True   # 先加锁再下单，防止QMT资金冻结延迟导致重复委托
        self.order_locked_ts_code = st.ts_code
        upgraded = st.watch_alerted and st.last_seal_hhmm < SIGNAL_START_HHMM
        source = "观察升级→买入" if upgraded else "直接买入"

        msg = (
            f"\n{'='*55}\n"
            f"  ★ [BUY] 买入信号 {hhmm_to_str(hhmm)}  [{source}]\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(st.last_seal_hhmm)}  "
            f"炸板 {st.open_times_today} 次\n"
            f"  情绪估算：当前封板涨停 {self.sealed_ever_count} 只\n"
            f"  操作：{'实盘挂单' if self.live_order else '仅提醒（--live-order 开启下单）'}\n"
            f"{'='*55}"
        )
        print(msg)
        self.logger.warning(
            "[BUY] %s %s 重封=%s 炸板=%d次 来源=%s",
            st.ts_code, st.name, hhmm_to_str(st.last_seal_hhmm),
            st.open_times_today, source,
        )
        record = {
            "signal_time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_type": "BUY",
            "ts_code": st.ts_code,
            "name": st.name,
            "upper_limit": st.upper_limit,
            "reseal_hhmm": st.last_seal_hhmm,
            "open_times_today": st.open_times_today,
            "sentiment_est": self.sealed_ever_count,
            "source": source,
            "order_id": "",
        }
        if self.live_order and self.broker is not None:
            self._place_d_order(st, record)
        self.signal_records.append(record)
        self._save_signals()

    # ── 实盘下单 ──────────────────────────────────────────────────────────────

    def _place_d_order(self, st: StockState, record: dict) -> None:
        from src.broker_adapter import OrderRequest
        from src.qmt_adapter import tushare_to_qmt_code
        try:
            if self.session_orders:
                self.logger.warning("本会话已有D委托，拒绝再次下单: %s", st.ts_code)
                return
            cash = get_account_cash(self.broker)
            max_order_amount = float(self.config.get("live_trade", {}).get("max_single_order_amount", 50000))
            shares = calc_shares_below_amount(cash, st.upper_limit, self.position_pct, max_order_amount)
            if shares <= 0:
                self.logger.warning("可用资金不足，跳过下单: %s", st.ts_code)
                return
            target_amount = cash * self.position_pct
            actual_amount = shares * st.upper_limit
            actual_position_pct = actual_amount / cash if cash > 0 else 0.0
            req = OrderRequest(
                ts_code=st.ts_code,
                broker_code=tushare_to_qmt_code(st.ts_code),
                side="BUY",
                quantity=shares,
                price_type="FIX_PRICE",
                price=st.upper_limit,
                strategy_name="STRATEGY_D",
                remark=STRATEGY_REMARK,
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
                self.logger.info(
                    "D委托: %s %d股 %.2f 目标仓位=%.0f%% 实际仓位=%.2f%% 目标金额=%.2f 实际金额=%.2f 单笔上限需小于%.0f order_id=%s 提交后状态=%s(%s)",
                    st.ts_code,
                    shares,
                    st.upper_limit,
                    self.position_pct * 100,
                    actual_position_pct * 100,
                    target_amount,
                    actual_amount,
                    max_order_amount,
                    result.order_id,
                    fill_check.status_text,
                    fill_check.status_code,
                )
                print(
                    f"  → 委托已提交: {shares}股 "
                    f"目标仓位{self.position_pct:.0%} 实际仓位{actual_position_pct:.2%} "
                    f"实际金额{actual_amount:.0f}元 单笔上限需小于{max_order_amount:.0f}元 "
                    f"order_id={result.order_id} 提交后状态={fill_check.status_text}({fill_check.status_code})"
                )
                try:
                    notify(
                        "buy_result",
                        "⏳ D开仓委托已提交",
                        (
                            f"{st.ts_code} {st.name} 委托{shares}股 @{st.upper_limit:.2f}，"
                            f"总委托金额{actual_amount / 10000:.2f}万，order_id={result.order_id}，"
                            f"提交后状态={fill_check.status_text}({fill_check.status_code})。"
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
            else:
                self.logger.error("D委托被拒: %s %s", st.ts_code, result.message)
                try:
                    notify("buy_result", "❌ D开仓委托被拒",
                           f"{st.ts_code} {st.name} 委托被拒：{result.message}",
                           level="critical", call=True)
                except Exception:
                    pass
                print(f"  → 委托被拒: {result.message}")
        except Exception as e:
            self.logger.error("下单异常: %s: %s", st.ts_code, e)
            try:
                notify("buy_result", "❌ D开仓下单异常",
                       f"{st.ts_code} {st.name} 下单异常：{e}",
                       level="critical", call=True)
            except Exception:
                pass
            print(f"  → 下单异常: {e}")

    def _confirm_submitted_order(self, order_id: str, ts_code: str):
        """下单后短暂等待，再反查当日委托/成交，避免把返回号误当成真实挂单。"""

        from src.broker_adapter import OrderFill

        last_error: Exception | None = None
        for attempt in range(1, 4):
            time.sleep(1.0)
            try:
                fill = self.broker.get_order_fill(order_id)
                if fill.status_code >= 0 or fill.filled_qty > 0:
                    return fill
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
                    f"未成交{unfilled_amount / 10000:.2f}万，写入持仓"
                )
                self._record_filled_d_position(order_id, filled_qty, fill_price)
                # 部分成交：撤掉未成残单
                if not getattr(fill, "is_filled", False):
                    ok = self.broker.cancel_order(order_id)
                    self.logger.warning("D部分成交 %s 已成%d股(%s)，撤残单%s",
                                        ts_code, filled_qty, status_text,
                                        "已发" if ok else "失败")
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
        positions = load_position_records()
        if any(str(pos.get("order_id", "")) == str(order_id) for pos in positions):
            return
        buy_date = str(detail.get("buy_date", today_beijing().strftime("%Y%m%d")))
        shares = int(filled_qty) if filled_qty and filled_qty > 0 else int(detail.get("shares", 0))
        buy_price = float(fill_price) if fill_price and fill_price > 0 else float(detail.get("buy_price", 0.0))
        positions.append(
            {
                "order_id": str(order_id),
                "ts_code": str(detail.get("ts_code", "")),
                "name": str(detail.get("name", "")),
                "signal_date": buy_date,
                "buy_date": buy_date,
                # 默认持到T+2收盘。若当晚A/B/C生成信号（HISTORICAL_SIM_FILLED），次日开盘手动平仓后再执行ABC。
                "planned_exit_date": next_trade_day(buy_date, 2),
                "shares": shares,
                "buy_price": buy_price,
                "strategy_leg": "D",
                "status": "open",
                "sell_date": None,
                "sell_price": None,
            }
        )
        save_position_records(positions)
        self.logger.warning("D成交已写入持仓账本: order_id=%s ts_code=%s %d股 @%.2f",
                            order_id, detail.get("ts_code", ""), shares, buy_price)
        try:
            planned_shares = int(detail.get("shares", 0) or 0)
            planned_amount = float(detail.get("actual_amount", 0.0) or 0.0)
            if planned_amount <= 0 and planned_shares > 0:
                planned_amount = planned_shares * float(detail.get("buy_price", 0.0) or 0.0)
            filled_amount = shares * buy_price
            unfilled_amount = max(planned_amount - filled_amount, 0.0)
            partial = planned_shares > 0 and shares < planned_shares
            notify(
                "buy_result",
                "⚠️ D开仓部分成交" if partial else "✅ D开仓成交",
                (
                    f"{detail.get('ts_code', '')} {detail.get('name', '')} "
                    f"买入{shares}/{planned_shares or shares}股 @{buy_price:.2f}。"
                    f"总委托金额{planned_amount / 10000:.2f}万，"
                    f"已成交金额{filled_amount / 10000:.2f}万，"
                    f"未成交金额{unfilled_amount / 10000:.2f}万。"
                ),
                level="timeSensitive" if partial else "active",
            )
        except Exception:
            pass

    # ── 轮询 ─────────────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        batches = self._batches()
        if not batches:
            return
        updated_count = 0
        for batch in batches:
            try:
                quotes = self.broker.get_full_tick(batch)
            except Exception as e:
                self.logger.warning("get_full_tick 异常: %s", e)
                continue
            updated_count += len(quotes)
            self._update_states(quotes)
        self.scan_round += 1
        self.logger.info("完成全市场扫描: round=%d updated=%d states=%d", self.scan_round, updated_count, len(self.states))
        # 情绪转强：当前封板数首次达到阈值，推送告知 D 今日具备开仓资格（只推一次）
        sealed = self.sealed_ever_count
        if not self._strong_notified and sealed >= SENTIMENT_STRONG_MIN:
            self._strong_notified = True
            self.logger.warning("📈 情绪转强：当前封板%d只(≥%d)，D今日满足开仓情绪条件。",
                                sealed, SENTIMENT_STRONG_MIN)
            try:
                notify(
                    "buy_result",
                    "📈 D满足开仓条件",
                    f"全市场当前封板{sealed}只(≥{SENTIMENT_STRONG_MIN})，今日D情绪条件满足，"
                    f"14:00后将扫描首板回封发出买入信号。",
                )
            except Exception as exc:
                self.logger.warning("情绪转强推送失败：%s", exc)
        self._check_and_fire()

    def status_line(self) -> str:
        hhmm = now_hhmm()
        watching = sum(1 for st in self.states.values()
                       if st.watch_alerted and not st.buy_signaled)
        bought = sum(1 for st in self.states.values() if st.buy_signaled)
        sentiment = (
            f"strong({self.sealed_ever_count}只)" if self.sealed_ever_count >= SENTIMENT_STRONG_MIN
            else f"弱({self.sealed_ever_count}只，需>={SENTIMENT_STRONG_MIN})"
        )
        return (
            f"[{hhmm_to_str(hhmm)}] "
            f"扫过{len(self.states)}只 | {sentiment} | "
            f"观察={watching} 买入={bought} | 全市场扫描轮次={self.scan_round}"
        )

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.setup()

        # ── ABC持仓检测：有持仓就直接退出，不做D ────────────────────────────
        occupied, desc = check_abc_position_occupied()
        if occupied:
            msg = (
                f"\n{'='*55}\n"
                f"  [跳过] 今日检测到ABC持仓，D策略不启动\n"
                f"  持仓: {desc}\n"
                f"  原因: 资金被占用（回测验证此情况D胜率仅20%，均亏2%）\n"
                f"{'='*55}\n"
            )
            print(msg)
            self.logger.info("ABC有持仓，D监控跳过: %s", desc)
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
            f"  09:35 → 观察提醒  |  14:00 → 买入信号  |  14:55 → 自动撤单\n"
        )

        try:
            while now_hhmm() < CANCEL_HHMM:
                self.poll_once()
                print(self.status_line())
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n用户中断，执行撤单流程...")

        self.cancel_all_d_orders()
        self._print_summary()

    def _print_summary(self) -> None:
        watched = [st for st in self.states.values() if st.watch_alerted and not st.buy_signaled]
        bought = [st for st in self.states.values() if st.buy_signaled]
        print(f"\n=== 今日策略D监控完毕 ===")
        print(f"观察提醒: {len(watched) + len(bought)} 次 | 买入信号: {len(bought)} 次")
        for st in bought:
            upgraded = "升级" if (st.watch_alerted and st.last_seal_hhmm < SIGNAL_START_HHMM) else "直接"
            order_str = f"order_id={st.order_id}" if st.order_id else "仅提醒"
            print(f"  [BUY/{upgraded}] {st.ts_code} {st.name} "
                  f"重封={hhmm_to_str(st.last_seal_hhmm)} 炸板={st.open_times_today}次 {order_str}")
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

    if args.dry_run:
        print("=== 策略D监控配置 ===")
        print(f"  情绪阈值: 全市场当前封板涨停数 >= {SENTIMENT_STRONG_MIN}")
        print(f"  炸板次数上限: {D_MAX_OPEN_TIMES}")
        print(f"  允许市场分段: {','.join(sorted(allowed_segments))}")
        print(f"  开仓仓位: {position_pct:.0%}")
        print(f"  扫描开始: {hhmm_to_str(args.start_hhmm)}")
        print(f"  观察提醒: {hhmm_to_str(WATCH_START_HHMM)} 起（10:00后回封发WATCH）")
        print(f"  买入信号: {hhmm_to_str(SIGNAL_START_HHMM)} 起（14:00后回封或WATCH升级→BUY）")
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
