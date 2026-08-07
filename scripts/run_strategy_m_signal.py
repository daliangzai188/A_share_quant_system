"""策略M每日收盘后信号生成脚本。

M 是补位腿，在腿序 **D > L > A > M > E2 > C**（2026-08-07 定稿）里排第四。
只有排在它**前面**的腿才有资格挡住它，三个条件必须同时成立：

  1. 账户没有任何未平仓头寸（这一条同时覆盖 D —— D 在信号日盘中买入，
     成交后会出现在 positions.json，因此 D 天然优先于 M）；
  2. L 当日无正式信号；
  3. A/C daily ops 当日未生成 **strategy_leg=A** 的计划委托。

  ⚠️ **E2 和 C 排在 M 后面，不得挡住 M 出信号。** 2026-08-07 之前这里还要求
     "E2 无信号 + A/C 都无计划"，M 事实上仍是"五腿全空才兜底"，而认证口径已把
     M 提到 E2/C 之前 —— 下游 combined_live_engine 排得再靠前也是空转。
     481信号日回放：认证口径 27870.31x，上游门未同步时实盘只跑出 22903.30x。

再叠加两道自有闸门：

  4. 当日"深市主板情绪 = weak"（策略条件）；
  5. 账户当前回撤未超过阈值（风控条件，默认 10%）。

触发时机：每日收盘流水线第 ⑨ 步。必须排在 A/C（⑥）和 L（⑧）之后运行，
才能读到这两者当日的产物；E2（⑦）的先后已无所谓，M 不再读它。

⚠️ 本脚本只生成信号文件，不提交任何委托。是否真实下单由
   config.json/strategy_m.live_order_enabled 与组合引擎共同决定。

用法：
    py -3.11 scripts/run_strategy_m_signal.py
    py -3.11 scripts/run_strategy_m_signal.py --signal-date 20260804 --dry-run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_strategy_e2_signal import (  # noqa: E402  复用同一套占用判据
    has_ac_planned_order,
    has_existing_open_position,
    load_open_positions,
    next_trade_day,
    resolve_signal_date,
)
from src.rolling_signal_store import (  # noqa: E402
    latest_signal_for_buy_date,
    save_recent_signal,
    save_recent_signal_run,
    signal_by_signal_date,
)
from src.strategy_m import (  # noqa: E402
    M_RESEARCH_AUDIT,
    M_VERSION,
    build_m_candidate,
    drawdown_guard_passed,
    load_m_spec,
    resolve_exit_offset,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
LIVE_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv"
HIST_SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
L_SIGNAL_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "l_signals_recent.json"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_m"
ROLLING_SIGNAL_PATH = OUTPUT_DIR / "m_signals_recent.json"
RUN_STATUS_PATH = OUTPUT_DIR / "m_signal_runs_recent.json"
EQUITY_PEAK_PATH = OUTPUT_DIR / "m_equity_peak.json"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_day_pool(signal_date: str) -> pd.DataFrame:
    """读取当日涨停打分池；实盘池优先，回落到历史池。"""

    frames: list[pd.DataFrame] = []
    for path in (LIVE_SCORED_PATH, HIST_SCORED_PATH):
        if not path.exists():
            continue
        frame = pd.read_csv(path, low_memory=False)
        frame["trade_date"] = (
            frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        )
        frames.append(frame[frame["trade_date"] == signal_date])
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    return merged.drop_duplicates(["trade_date", "ts_code"], keep="last").reset_index(drop=True)


def higher_priority_leg_has_signal(signal_date: str) -> tuple[bool, str]:
    """按腿序排在 M **前面** 的腿，当日是否已有信号或计划。

    腿序 D>L>A>M>E2>C（2026-08-07 定稿）：M 之前只有 D、L、A。
      · D  由 has_existing_open_position 覆盖（D 建仓即写 positions.json）
      · L  查 l_signals_recent.json
      · A  查 A/C 操作台里 strategy_leg=A 的计划委托
    **E2 和 C 排在 M 后面，不得挡住 M 出信号。**

    2026-08-07 之前这里把 E2 和 C（经不分腿的 has_abc_planned_order）也算作
    占用，导致 M 事实上仍是"五腿全空才兜底"，而认证口径已把 M 提到 E2/C 之前。
    下游 combined_live_engine 排得再靠前也没用——那些日子 M 根本没有信号。
    481信号日回放：认证口径 27870.31x，上游门未同步时实盘只能跑出 22903.30x。
    """

    if has_ac_planned_order(signal_date, legs=("A",)):
        return True, "A当日已生成计划委托（腿序A>M）"
    l_signal = signal_by_signal_date(L_SIGNAL_PATH, signal_date)
    if l_signal:
        return True, f"L当日已有信号（{l_signal.get('ts_code','')}，腿序L>M）"
    return False, "排在M前面的L、A当日均无信号"


def load_equity_peak() -> dict[str, Any]:
    if not EQUITY_PEAK_PATH.exists():
        return {}
    try:
        return json.loads(EQUITY_PEAK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_equity_peak(state: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    EQUITY_PEAK_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def current_equity_and_peak(config: dict[str, Any]) -> tuple[float, float, str]:
    """读取账户当前净值与历史峰值。

    净值口径与回测一致：只用"已实现"净值，即当前没有持仓时的账户总资产。
    M 只在空仓日决策，因此这个口径天然可得，也不受浮动盈亏干扰。

    实盘取值优先级：QMT 实时总资产 → 本地账本记录。取不到时返回 (0,0)，
    由调用方按安全口径暂停 M。
    """

    state = load_equity_peak()
    equity = 0.0
    source = "未取到"
    if config.get("broker_adapter_enabled") and config.get("qmt_enabled"):
        try:
            from src.live_order_gateway import QmtBrokerAdapter  # noqa: WPS433

            adapter = QmtBrokerAdapter(config.get("broker", {}))
            account = adapter.query_account()
            equity = float(getattr(account, "total_asset", 0.0) or 0.0)
            source = "QMT实时总资产"
        except Exception as exc:  # pragma: no cover - 实盘环境相关
            print(f"[M信号] 读取QMT账户失败：{exc}")
    if equity <= 0:
        equity = float(state.get("last_equity", 0.0) or 0.0)
        source = "本地账本last_equity" if equity > 0 else "未取到"
    peak = max(float(state.get("peak_equity", 0.0) or 0.0), equity)
    return equity, peak, source


def update_equity_peak(equity: float, peak: float, signal_date: str, dry_run: bool = False) -> None:
    """持久化净值与峰值。--dry-run 只打印不落盘，避免调试污染回撤闸的输入。"""

    if equity <= 0 or dry_run:
        return
    state = load_equity_peak()
    state["last_equity"] = equity
    state["peak_equity"] = max(peak, equity)
    state["updated_signal_date"] = signal_date
    save_equity_peak(state)


def record_run(signal_date: str, status: str, note: str, dry_run: bool, **extra: Any) -> None:
    if dry_run:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run = {"signal_date": signal_date, "status": status, "note": note,
           "strategy_version": M_VERSION, **extra}
    save_recent_signal_run(RUN_STATUS_PATH, run, strategy_leg="M", max_trade_days=20)


def build_signal(signal_date: str, row: pd.Series, spec: dict[str, Any]) -> dict[str, Any]:
    offset = resolve_exit_offset(spec)
    return {
        "strategy_leg": "M",
        "strategy_version": M_VERSION,
        "signal_date": signal_date,
        "ts_code": str(row.get("ts_code", "")),
        "name": str(row.get("name", "")),
        "market_segment": str(row.get("market_segment", "")),
        "circ_mv": float(pd.to_numeric(row.get("circ_mv"), errors="coerce") or 0.0),
        "limit_close": float(pd.to_numeric(row.get("limit_close"), errors="coerce") or 0.0),
        "fill_probability": float(pd.to_numeric(row.get("fill_probability"), errors="coerce") or 0.0),
        "planned_buy_date": next_trade_day(signal_date, 1),
        "planned_buy_price": "T+1_OPEN",
        "planned_exit_date": next_trade_day(signal_date, offset),
        "planned_exit_rule": f"T+{offset}_CLOSE",
        "position_pct": float(spec.get("position_pct", 0.825)),
        "research_audit": M_RESEARCH_AUDIT,
        "status": "pending",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="策略M兜底补位信号生成")
    parser.add_argument("--signal-date", help="信号日 YYYYMMDD，不填自动推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    args = parser.parse_args()

    config = load_config()
    spec = load_m_spec(config)
    signal_date = args.signal_date or resolve_signal_date()
    print(f"[M信号] 信号日期: {signal_date}")

    if not bool(spec.get("enabled", False)):
        print("[M信号] strategy_m.enabled=false，本腿未启用。")
        record_run(signal_date, "NO_SIGNAL_OCCUPIED", "strategy_m.enabled=false", args.dry_run)
        return

    positions = load_open_positions()
    if has_existing_open_position(positions):
        held = [(str(p.get("strategy_leg", "")), str(p.get("ts_code", ""))) for p in positions]
        note = f"账户有未平仓头寸，M不触发；持仓={held}"
        print(f"[M信号] {note}")
        record_run(signal_date, "NO_SIGNAL_OCCUPIED", note, args.dry_run)
        return

    # 净值峰值必须在**每一个空仓日**都记录，不能等腿序门和回撤闸都放行了才记。
    # 口径依据：回测里 peak_equity 是逐日维护的组合净值峰值；实盘只能在空仓日
    # 取到同口径的"已实现净值"（有持仓时 QMT 总资产含浮动盈亏，会污染峰值），
    # 而空仓日恰好就是 M 可能决策的全部日子。
    #
    # 2026-08-07 之前 update_equity_peak 在回撤闸之后，于是"别的腿有信号"或
    # "回撤闸拦下"的空仓日一律不记峰值 —— 峰值滞后、回撤被低估、M 在真实回撤
    # 中仍可能被放行。提前到这里后覆盖全部空仓日。本次判断结果不变：
    # current_equity_and_peak 已把 peak 取成 max(历史峰值, 当前净值)，
    # update_equity_peak 只负责持久化。
    equity, peak, source = current_equity_and_peak(config)
    update_equity_peak(equity, peak, signal_date, dry_run=args.dry_run)

    busy, why = higher_priority_leg_has_signal(signal_date)
    if busy:
        print(f"[M信号] {why}，M不触发。")
        record_run(signal_date, "NO_SIGNAL_OCCUPIED", why, args.dry_run,
                   equity=equity, peak_equity=peak)
        return
    print(f"[M信号] {why}，进入M补位判断。")

    ok, dd_note = drawdown_guard_passed(equity, peak, spec)
    print(f"[M信号] 净值={equity:.2f}（{source}） 峰值={peak:.2f} → {dd_note}")
    if not ok:
        record_run(signal_date, "NO_SIGNAL_OCCUPIED", f"回撤保护：{dd_note}", args.dry_run,
                   equity=equity, peak_equity=peak)
        return

    pool = load_day_pool(signal_date)
    if pool.empty:
        note = "当日涨停打分池为空，无法判断"
        print(f"[M信号] {note}")
        record_run(signal_date, "ERROR", note, args.dry_run)
        return

    picked, reason = build_m_candidate(pool, spec)
    if picked.empty:
        print(f"[M信号] 不触发：{reason}")
        record_run(signal_date, "NO_CANDIDATE", reason, args.dry_run)
        return

    signal = build_signal(signal_date, picked.iloc[0], spec)
    print(f"[M信号] ✅ 命中 {signal['ts_code']} {signal['name']}  "
          f"流通市值={signal['circ_mv']/10000:.1f}亿  {reason}")
    print(f"[M信号] 计划：{signal['planned_buy_date']}开盘买入，"
          f"{signal['planned_exit_date']}收盘卖出，仓位{signal['position_pct']:.1%}")
    if not bool(spec.get("live_order_enabled", False)):
        print("[M信号] ⚠️ live_order_enabled=false：只生成信号，不会提交真实委托。")

    if args.dry_run:
        print("[M信号] --dry-run 未落盘。")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    save_recent_signal(ROLLING_SIGNAL_PATH, signal, strategy_leg="M", max_trade_days=10)
    record_run(signal_date, "SIGNAL_READY", reason, args.dry_run,
               ts_code=signal["ts_code"], name=signal["name"])
    print(f"[M信号] 已写入 {ROLLING_SIGNAL_PATH}")


if __name__ == "__main__":
    main()
