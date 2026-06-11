"""
策略D盘中监控：首板打板信号检测 + 14:55自动撤单

【两档信号设计】
  观察档（10:00-14:00 回封）:
    首板 + multi_open + open_times<=3 + strong情绪 + 当前处于涨停 → 发出 [WATCH] 提醒
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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from dotenv import load_dotenv

from src.utils.logger import setup_logger
from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing

load_dotenv(PROJECT_ROOT / ".env")

# ── 策略参数 ──────────────────────────────────────────────────────────────────
SENTIMENT_STRONG_MIN = 100   # 全市场今日涨停累计数 >= 此值 → strong情绪
D_MAX_OPEN_TIMES = 3         # 最大炸板次数
WATCH_START_HHMM = 1000      # 10:00 开始发出观察提醒
SIGNAL_START_HHMM = 1400     # 14:00 开始发出买入信号 / 观察升级
CANCEL_HHMM = 1455           # 14:55 撤销所有未成交D委托
POLL_BATCH_SIZE = 500        # 每次 get_full_tick 的股票数量
POLL_INTERVAL_SEC = 30       # 每批轮询间隔（秒）
MONITOR_START_HHMM = 930     # 脚本等待开始扫描的时间（集合竞价结束后）
D_POSITION_PCT = 0.80        # 仓位比例
STRATEGY_REMARK = "D_FIRST_BOARD"


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class StockState:
    ts_code: str
    name: str = ""
    upper_limit: float = 0.0
    was_sealed: bool = False       # 上次轮询时是否涨停
    ever_sealed: bool = False      # 今日曾涨停过
    open_times_today: int = 0      # 今日炸板次数
    first_seal_hhmm: int = 0       # 首次封板时间
    last_seal_hhmm: int = 0        # 最后一次封板时间（每次重封更新）
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


def load_yesterday_limit_codes() -> set[str]:
    """加载昨日涨停股票代码集合，用于排除2板+。"""
    limit_dir = PROJECT_ROOT / "data" / "raw" / "limit_list"
    files = sorted(limit_dir.glob("*.csv"))
    if not files:
        return set()
    try:
        df = pd.read_csv(files[-1], dtype={"ts_code": str})
        df = df[df["limit"].astype(str).str.upper() == "U"]
        return set(df["ts_code"].tolist())
    except Exception as e:
        print(f"[警告] 加载昨日涨停数据失败: {e}")
        return set()


def load_stock_universe() -> list[str]:
    """加载全市场股票代码列表（从最新日线数据）。"""
    daily_dir = PROJECT_ROOT / "data" / "raw" / "daily"
    files = sorted(daily_dir.glob("*.csv"))
    if not files:
        return []
    try:
        df = pd.read_csv(files[-1], dtype={"ts_code": str})
        return df["ts_code"].tolist()
    except Exception as e:
        print(f"[警告] 加载股票宇宙失败: {e}")
        return []


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


def calc_shares(cash: float, price: float) -> int:
    if price <= 0:
        return 0
    return max(int(cash * D_POSITION_PCT / price / 100) * 100, 0)


# ── 核心监控逻辑 ─────────────────────────────────────────────────────────────

class StrategyDMonitor:

    def __init__(self, broker, live_order: bool, logger, signal_csv: Path,
                 monitor_start_hhmm: int = MONITOR_START_HHMM) -> None:
        self.broker = broker
        self.live_order = live_order
        self.logger = logger
        self.signal_csv = signal_csv
        self.monitor_start_hhmm = monitor_start_hhmm

        self.yesterday_limit_codes: set[str] = set()
        self.universe: list[str] = []
        self.name_map: dict[str, str] = {}
        self.states: dict[str, StockState] = {}
        self.batch_idx = 0

        # 情绪估算：今日累计曾涨停的股票数
        self.sealed_ever_count: int = 0

        # 本次会话下单记录 {order_id: ts_code}
        self.session_orders: dict[str, str] = {}
        self.signal_records: list[dict] = []

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def setup(self) -> None:
        self.logger.info("=== 策略D盘中监控启动 ===")
        self.yesterday_limit_codes = load_yesterday_limit_codes()
        self.universe = load_stock_universe()
        self.name_map = load_stock_names()
        self.logger.info(
            "宇宙: %d只 | 昨日涨停(排除首板): %d只 | 批次: %d批x%d只 ≈ %.0f分/轮",
            len(self.universe), len(self.yesterday_limit_codes),
            len(self._batches()), POLL_BATCH_SIZE,
            len(self._batches()) * POLL_INTERVAL_SEC / 60,
        )

    def _batches(self) -> list[list[str]]:
        return [self.universe[i: i + POLL_BATCH_SIZE]
                for i in range(0, len(self.universe), POLL_BATCH_SIZE)]

    # ── 状态更新 ──────────────────────────────────────────────────────────────

    def _update_states(self, quotes: dict) -> None:
        hhmm = now_hhmm()
        for ts_code, snap in quotes.items():
            if snap.upper_limit <= 0:
                continue
            at_limit = abs(snap.last_price - snap.upper_limit) < 0.015

            if ts_code not in self.states:
                self.states[ts_code] = StockState(
                    ts_code=ts_code,
                    name=self.name_map.get(ts_code, ""),
                    upper_limit=snap.upper_limit,
                )
            st = self.states[ts_code]
            st.upper_limit = snap.upper_limit

            if at_limit:
                if not st.ever_sealed:
                    st.ever_sealed = True
                    st.first_seal_hhmm = hhmm
                    self.sealed_ever_count += 1
                if not st.was_sealed:
                    # 非涨停 → 涨停：记录重封时间
                    st.last_seal_hhmm = hhmm
                st.was_sealed = True
            else:
                if st.was_sealed:
                    # 涨停 → 非涨停：炸板
                    st.open_times_today += 1
                st.was_sealed = False

    # ── 信号检测与分级触发 ────────────────────────────────────────────────────

    def _passes_base_filters(self, ts_code: str, st: StockState) -> bool:
        """通用过滤：首板 + multi_open + open_times <= 3 + 当前涨停 + strong情绪。"""
        if ts_code in self.yesterday_limit_codes:  # 排除2板+
            return False
        if not st.was_sealed:                      # 当前不在涨停
            return False
        if st.open_times_today < 1:                # 未曾炸板
            return False
        if st.open_times_today > D_MAX_OPEN_TIMES: # 炸板过多
            return False
        if self.sealed_ever_count < SENTIMENT_STRONG_MIN:  # 情绪不足
            return False
        return True

    def _check_and_fire(self) -> None:
        hhmm = now_hhmm()

        for ts_code, st in self.states.items():
            if st.buy_signaled:
                continue
            if not self._passes_base_filters(ts_code, st):
                continue

            # ── 场景一：买入信号 ──────────────────────────────────────────────
            # 条件：14:00后 且（重封时间>=14:00 或 已在观察名单中）
            if hhmm >= SIGNAL_START_HHMM:
                if st.last_seal_hhmm >= SIGNAL_START_HHMM or st.watch_alerted:
                    self._fire_buy_signal(st)
                continue

            # ── 场景二：观察提醒（10:00-14:00 回封，首次提醒）────────────────
            if hhmm >= WATCH_START_HHMM and not st.watch_alerted:
                self._fire_watch_alert(st)

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
        hhmm = now_hhmm()
        st.buy_signaled = True
        upgraded = st.watch_alerted and st.last_seal_hhmm < SIGNAL_START_HHMM
        source = "观察升级→买入" if upgraded else "直接买入"

        msg = (
            f"\n{'='*55}\n"
            f"  ★ [BUY] 买入信号 {hhmm_to_str(hhmm)}  [{source}]\n"
            f"  {st.ts_code} {st.name}  涨停价 {st.upper_limit:.2f}\n"
            f"  重封时间 {hhmm_to_str(st.last_seal_hhmm)}  "
            f"炸板 {st.open_times_today} 次\n"
            f"  情绪估算：今日涨停累计 {self.sealed_ever_count} 只\n"
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
            cash = get_account_cash(self.broker)
            shares = calc_shares(cash, st.upper_limit)
            if shares <= 0:
                self.logger.warning("可用资金不足，跳过下单: %s", st.ts_code)
                return
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
                st.order_id = result.order_id
                record["order_id"] = result.order_id
                self.logger.info(
                    "D委托: %s %d股 %.2f order_id=%s",
                    st.ts_code, shares, st.upper_limit, result.order_id,
                )
                print(f"  → 委托已提交: {shares}股 order_id={result.order_id}")
            else:
                self.logger.error("D委托被拒: %s %s", st.ts_code, result.message)
                print(f"  → 委托被拒: {result.message}")
        except Exception as e:
            self.logger.error("下单异常: %s: %s", st.ts_code, e)
            print(f"  → 下单异常: {e}")

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

        # 查询今日委托，找已成交的
        try:
            all_orders = self.broker.query_orders()
        except Exception as e:
            self.logger.error("查询委托失败: %s", e)
            all_orders = []

        filled_ids: set[str] = set()
        for o in all_orders:
            raw = o if isinstance(o, dict) else {}
            oid = str(raw.get("order_id", raw.get("m_strOrderID", "")))
            status = str(raw.get("order_status", raw.get("status", ""))).lower()
            if any(k in status for k in ["filled", "全部成交", "已成", "5"]):
                filled_ids.add(oid)

        cancelled = failed = 0
        for order_id, ts_code in self.session_orders.items():
            if order_id in filled_ids:
                print(f"  {ts_code}  order_id={order_id} → 已成交，跳过")
                continue
            ok = self.broker.cancel_order(order_id)
            if ok:
                cancelled += 1
                print(f"  {ts_code}  order_id={order_id} → 撤单已发")
            else:
                failed += 1
                print(f"  {ts_code}  order_id={order_id} → 撤单失败！请手动检查")
                self.logger.error("撤单失败: %s order_id=%s", ts_code, order_id)

        print(f"  结果: 撤单={cancelled}笔  失败={failed}笔")
        print(f"{'='*55}\n")

    # ── 轮询 ─────────────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        batches = self._batches()
        if not batches:
            return
        batch = batches[self.batch_idx % len(batches)]
        self.batch_idx += 1
        try:
            quotes = self.broker.get_full_tick(batch)
        except Exception as e:
            self.logger.warning("get_full_tick 异常: %s", e)
            return
        self._update_states(quotes)
        self._check_and_fire()

    def status_line(self) -> str:
        hhmm = now_hhmm()
        batches = self._batches()
        progress = (self.batch_idx % max(len(batches), 1)) / max(len(batches), 1) * 100
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
            f"观察={watching} 买入={bought} | 批次{progress:.0f}%"
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
            f"间隔{POLL_INTERVAL_SEC}s，约{len(batches)*POLL_INTERVAL_SEC//60}分/轮\n"
            f"  10:00 → 观察提醒  |  14:00 → 买入信号  |  14:55 → 自动撤单\n"
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

    if args.dry_run:
        print("=== 策略D监控配置 ===")
        print(f"  情绪阈值: 全市场涨停累计数 >= {SENTIMENT_STRONG_MIN}")
        print(f"  炸板次数上限: {D_MAX_OPEN_TIMES}")
        print(f"  扫描开始: {hhmm_to_str(args.start_hhmm)}")
        print(f"  观察提醒: {hhmm_to_str(WATCH_START_HHMM)} 起（10:00后回封发WATCH）")
        print(f"  买入信号: {hhmm_to_str(SIGNAL_START_HHMM)} 起（14:00后回封或WATCH升级→BUY）")
        print(f"  撤单时间: {hhmm_to_str(CANCEL_HHMM)}")
        print(f"  实盘下单: {'是' if args.live_order else '否（仅提醒）'}")
        print(f"  信号输出: {signal_csv}")
        return

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker = build_broker(config, args.live_order)
    if args.live_order and broker is None:
        print("[错误] --live-order 模式下QMT必须连接成功，退出。")
        import sys; sys.exit(1)

    monitor = StrategyDMonitor(
        broker=broker,
        live_order=args.live_order,
        logger=logger,
        signal_csv=signal_csv,
        monitor_start_hhmm=args.start_hhmm,
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
