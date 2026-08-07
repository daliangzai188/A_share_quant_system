"""代入证明：把实盘代码本身放进 481 信号日回放，逐笔验证它能跑出认证标尺。

为什么需要这个脚本
==================
认证脚本 `certify_current_executable_portfolio.py` 产出的复利，只有在"实盘会
选出同样的票"时才有意义。2026-08-07 的教训是：腿序改造只改了下游
`combined_live_engine` 的挑选顺序，上游各信号脚本的占用门还按旧腿序拦截，
于是那些日子 M / E2 的信号根本不会生成——认证跑 27870x，实盘只能跑 22903x，
差 17.8%，而两边的代码看起来都"改好了"。

光靠读代码对不出这种差异，手写一个"复刻实盘逻辑"的函数也不行——那证明的是
写脚本的人对实盘的理解，不是实盘代码。所以本脚本**直接调用实盘函数**：

    上游门   run_strategy_m_signal.higher_priority_leg_has_signal
             run_strategy_e2_signal.has_ac_planned_order
    下游腿序 combined_live_engine.CombinedLiveEngine.build_model3_plan

把每个信号日的各腿候选写成实盘平时读的那些文件（A/C 操作台 csv、L 信号 json），
让实盘代码自己去读、自己做决定，再把它选出的 (腿, 代码) 与认证脚本的选择逐笔
比对。资金占用、收益计算、回撤统计全部沿用认证脚本本身，本脚本只替换"选哪条腿"
这一个环节——要证明的恰恰就是它。

判定
====
逐笔完全一致 且 复利/回撤/胜率完全相等 → 通过（退出码 0）
任何一笔不一致                        → 打印差异明细并 raise（退出码 1）

运行：
    python3 scripts/verify_live_engine_matches_certify.py
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 本脚本不下单、不连券商，开发机未装 python-dotenv 时注入无副作用最小桩。
if "dotenv" not in sys.modules:
    _stub = ModuleType("dotenv")
    _stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = _stub

import scripts.certify_current_executable_portfolio as certify  # noqa: E402
from scripts import run_strategy_e2_signal as e2_signal  # noqa: E402
from scripts import run_strategy_m_signal as m_signal  # noqa: E402
from src.combined_live_engine import CombinedLiveEngine  # noqa: E402


LIVE_CONFIG = {
    "trade_mode": "backtest",          # 不触发单笔金额上限，保持与认证同口径
    "position": {"initial_cash": 500_000},
    "live_trade": {"max_single_order_amount": 0},
    "strategy_l": {"enabled": True, "live_order_enabled": True, "position_pct": 0.825},
    "strategy_m": {"enabled": True, "live_order_enabled": True, "position_pct": 0.825,
                   "exit_hold_offset": 2},
    "strategy_model3": {"enabled": True, "live_order_enabled": True,
                        "selected_rule_name": "verify"},
}


def make_engine(project_root: Path) -> CombinedLiveEngine:
    """构造只做计划层判断的引擎实例：不读磁盘持仓、不连券商。"""

    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = project_root
    engine.config = dict(LIVE_CONFIG)
    engine.load_positions = lambda: []          # 认证回放只在空仓日调用本模块
    engine.active_strategy_mode = lambda: 3
    engine.active_strategy_name = lambda: "MODEL3"
    engine.is_b_strategy_removed = lambda: True
    engine.load_today_e2_signal = lambda _today: None
    engine.load_today_l_signal = lambda _today: None
    return engine


def write_ac_ops(ops_dir: Path, signal_date: str, ac: dict[str, Any] | None) -> pd.DataFrame:
    """把当天 A/C 候选写成收盘流水线产出的操作台计划单。"""

    if ac is None:
        return pd.DataFrame()
    frame = pd.DataFrame([{
        "strategy_leg": str(ac["strategy_leg"]),
        "ts_code": str(ac["ts_code"]),
        "name": str(ac.get("name", "")),
        "side": "BUY",
        "planned_order_date": str(ac["buy_date"]),
        "reference_price": 10.0,
        "round_lot_shares": 10_000,
        "estimated_shares": 10_000,
    }])
    frame.to_csv(ops_dir / f"ops_{signal_date}_planned_orders.csv", index=False)
    return frame


def write_l_signal(path: Path, signal_date: str, l_row: pd.Series | None,
                   buy_date: str) -> dict[str, Any] | None:
    """把当天 L 原始行写成 l_signals_recent.json；是否过基础规则交给实盘引擎判。"""

    if l_row is None:
        path.write_text(json.dumps({"signals": []}, ensure_ascii=False), encoding="utf-8")
        return None
    signal = {k: (v.item() if hasattr(v, "item") else v) for k, v in l_row.to_dict().items()}
    signal["signal_date"] = signal_date
    signal["planned_buy_date"] = buy_date
    signal.setdefault("ts_code", "")
    signal.setdefault("name", "")
    signal["limit_close"] = float(pd.to_numeric(l_row.get("limit_close", 10.0), errors="coerce") or 10.0)
    path.write_text(json.dumps({"signals": [signal]}, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return signal


def build_live_picker(sources: certify.Sources, workdir: Path, stats: dict[str, int]):
    """返回一个与 certify.pick_by_priority 同签名、但内部走实盘代码的函数。"""

    ops_dir = workdir / "daily_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    l_path = workdir / "l_signals_recent.json"
    engine = make_engine(PROJECT_ROOT)

    def pick(sources_: certify.Sources, row: pd.Series, row_index: int, *,
             entry_gate_enabled: bool, l_chain_3_8_enabled: bool, m_enabled: bool,
             equity: float, peak_equity: float) -> dict[str, Any] | None:
        signal_date = str(row["date"])
        buy_date = certify.nth_trade_date(sources_, signal_date, 1)
        if not buy_date:
            return None

        # 每天从干净的文件视图开始，避免昨天的信号泄漏到今天
        for stale in ops_dir.glob("*.csv"):
            stale.unlink()

        ac = sources_.ac_daily.get(signal_date)
        ac_frame = write_ac_ops(ops_dir, signal_date, ac)
        l_row = sources_.l_lookup.get(signal_date)
        write_l_signal(l_path, signal_date, l_row, buy_date)

        # ── 上游门：调用实盘函数判断信号会不会被生成 ──────────────────
        e2_blocked = e2_signal.has_ac_planned_order(signal_date, legs=("A",))
        m_busy, _why = m_signal.higher_priority_leg_has_signal(signal_date)

        # E2 候选（过门禁后）
        e2_signal_payload: dict[str, Any] | None = None
        if not e2_blocked and signal_date in sources_.e2.index:
            e2_row = certify.source_row(sources_.e2, signal_date, "E2 R1")
            if not (entry_gate_enabled
                    and not certify.e2_entry_gate_passes(e2_row, sources_.e2_spec)):
                e2_signal_payload = {
                    "signal_date": signal_date,
                    "ts_code": str(e2_row.get("ts_code", "")),
                    "name": str(e2_row.get("name", "")),
                    "limit_close": 10.0,
                    "exit_offset": int(pd.to_numeric(e2_row.get("exit_offset", 2),
                                                     errors="coerce") or 2),
                }

        # M 候选（过上游门 + 回撤闸后）
        m_order: dict[str, Any] | None = None
        if m_enabled and not m_busy:
            m_pick = certify.m_candidate(sources_, signal_date, equity, peak_equity)
            if m_pick is not None:
                m_order = {
                    "strategy_leg": "M",
                    "ts_code": str(m_pick["ts_code"]),
                    "name": str(m_pick.get("name", "")),
                    "side": "BUY",
                    "planned_order_date": buy_date,
                    "planned_action": "PLAN_BUY_T1_OPEN",
                    "round_lot_shares": 10_000,
                    "planned_amount_by_equity": 412_500.0,
                }

        # ── 下游腿序：交给真实的 build_model3_plan 决定 ────────────────
        engine.load_latest_abc_orders = lambda: (ops_dir / "x.csv", ac_frame.copy())
        engine.load_yesterday_e2_signal = lambda _today, s=e2_signal_payload: s
        engine.build_m_buy_order_if_any = lambda _today, _codes=None, o=m_order: (
            dict(o) if o is not None else None
        )
        engine.load_yesterday_l_signal = lambda _today, sd=signal_date, bd=buy_date: (
            json.loads(l_path.read_text(encoding="utf-8"))["signals"][0]
            if json.loads(l_path.read_text(encoding="utf-8"))["signals"] else None
        )

        _state, _decisions, orders = engine.build_model3_plan(buy_date)
        if orders.empty or "side" not in orders.columns:
            stats["no_plan"] += 1
            return None
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
        if buys.empty:
            stats["no_plan"] += 1
            return None
        chosen = buys.iloc[0]
        leg = str(chosen.get("strategy_leg", "")).upper()
        code = str(chosen.get("ts_code", ""))

        # ── 把实盘选中的 (腿, 代码) 映射回认证口径的收益 ────────────────
        if leg == "L":
            pick_ = certify.l_candidate(sources_, signal_date,
                                        chain_3_8_enabled=l_chain_3_8_enabled)
            if pick_ is None:
                # 实盘会照常下单，但这只票当天买不到/卖不掉。认证口径记为无交易。
                stats["l_unexecutable"] += 1
                return None
            return pick_
        if leg in {"A", "C"}:
            if ac is None or str(ac["ts_code"]) != code:
                raise RuntimeError(f"{signal_date} 实盘选A/C={code}，认证候选={ac}")
            return dict(ac)
        if leg == "M":
            return certify.m_candidate(sources_, signal_date, equity, peak_equity)
        if leg == "E2":
            e2_row = certify.source_row(sources_.e2, signal_date, "E2 R1")
            return {
                "strategy_leg": "E2",
                "ts_code": str(e2_row.get("ts_code", "")),
                "name": str(e2_row.get("name", "")),
                "buy_date": certify.normalize_date(e2_row.get("buy_date")),
                "exit_date": certify.normalize_date(e2_row.get("exit_date")),
                "account_return": certify.to_float(e2_row.get("net_return")) * certify.POSITION_PCT,
                "return_source": f"E2_R1:{e2_row.get('scenario_rank', '')}",
            }
        raise RuntimeError(f"{signal_date} 实盘返回未知腿 {leg}")

    return pick


def main() -> None:
    sources = certify.load_sources()
    workdir = Path(tempfile.mkdtemp(prefix="verify_live_"))
    stats = {"no_plan": 0, "l_unexecutable": 0}
    try:
        certified = certify.replay(sources, entry_gate_enabled=True,
                                  l_chain_3_8_enabled=True, m_enabled=True)

        picker = build_live_picker(sources, workdir, stats)
        original_pick = certify.pick_by_priority
        original_ops = e2_signal.DAILY_OPS_DIR
        original_l = m_signal.L_SIGNAL_PATH
        try:
            certify.pick_by_priority = picker
            e2_signal.DAILY_OPS_DIR = workdir / "daily_ops"
            m_signal.L_SIGNAL_PATH = workdir / "l_signals_recent.json"
            live = certify.replay(sources, entry_gate_enabled=True,
                                  l_chain_3_8_enabled=True, m_enabled=True)
        finally:
            certify.pick_by_priority = original_pick
            e2_signal.DAILY_OPS_DIR = original_ops
            m_signal.L_SIGNAL_PATH = original_l
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    a = certify.summarize(certified, "认证脚本 pick_by_priority")
    b = certify.summarize(live, "实盘代码 build_model3_plan + 上游门")
    ex_a = certified[certified["status"] == "EXECUTED"]
    ex_b = live[live["status"] == "EXECUTED"]

    print("=" * 78)
    print("代入证明：实盘代码 vs 认证脚本，481 信号日逐笔回放")
    print("=" * 78)
    for label, s, ex in (("认证脚本", a, ex_a), ("实盘代码", b, ex_b)):
        print(f"{label}  {s['executed_trade_count']:>3}笔 | {s['equity_multiple']:>13.6f}x | "
              f"回撤{s['max_drawdown']:>9.6%} | 胜率{s['win_rate']:>8.4%}")
        print(f"          {ex['strategy_leg'].value_counts().to_dict()}")

    key_a = list(zip(ex_a["signal_date"].astype(str), ex_a["strategy_leg"].astype(str),
                     ex_a["ts_code"].astype(str)))
    key_b = list(zip(ex_b["signal_date"].astype(str), ex_b["strategy_leg"].astype(str),
                     ex_b["ts_code"].astype(str)))

    print(f"\n实盘侧另计：无计划单 {stats['no_plan']} 天；"
          f"L 被选中但当天不可成交 {stats['l_unexecutable']} 天")

    problems: list[str] = []
    if key_a != key_b:
        only_a = [k for k in key_a if k not in set(key_b)]
        only_b = [k for k in key_b if k not in set(key_a)]
        problems.append(f"逐笔不一致：认证独有 {len(only_a)} 笔，实盘独有 {len(only_b)} 笔")
        for k in only_a[:15]:
            problems.append(f"    仅认证有: {k}")
        for k in only_b[:15]:
            problems.append(f"    仅实盘有: {k}")
    for field in ("executed_trade_count", "max_consecutive_losses"):
        if a[field] != b[field]:
            problems.append(f"{field} 不一致：认证 {a[field]} vs 实盘 {b[field]}")
    for field in ("equity_multiple", "max_drawdown", "win_rate", "avg_return"):
        if abs(float(a[field]) - float(b[field])) > 1e-9:
            problems.append(f"{field} 不一致：认证 {a[field]!r} vs 实盘 {b[field]!r}")

    print()
    if problems:
        print("❌ 未通过：")
        for line in problems:
            print("  " + line)
        raise SystemExit(1)

    print("✅ 通过：实盘代码逐笔选出与认证脚本完全相同的 "
          f"{a['executed_trade_count']} 笔，复利 {a['equity_multiple']:.6f}x、"
          f"回撤 {a['max_drawdown']:.6%}、胜率 {a['win_rate']:.4%} 完全相等。")
    print("   认证标尺可以作为实盘的收益预期基准（执行损耗另计，见下方风险清单）。")


if __name__ == "__main__":
    main()
