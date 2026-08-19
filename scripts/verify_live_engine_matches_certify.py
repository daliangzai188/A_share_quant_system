"""核对当前实盘计划选择路径与组合认证逐笔一致。

本脚本直接调用当前组合引擎的 `build_plan`，并把481个冻结信号日的A/M/E/C/N
候选写成实盘会读取的形式。D仍由认证回放按盘中时序先处理。
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import shutil
from types import ModuleType
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

import scripts.certify_current_executable_portfolio as certify  # noqa: E402
from scripts import run_strategy_e_signal as e_signal  # noqa: E402
from scripts import run_strategy_m_signal as m_signal  # noqa: E402
from src.combined_live_engine import CombinedLiveEngine  # noqa: E402


LIVE_CONFIG = {
    "trade_mode": "backtest",
    "position": {"initial_cash": 500_000},
    "live_trade": {"max_single_order_amount": 0},
    "active_strategy_profile": {"mode": 1, "mode_name": "D_A_M_E_C_N"},
    "strategy_m": {
        "enabled": True,
        "live_order_enabled": True,
        "position_pct": 0.825,
        "exit_hold_offset": 2,
    },
    "strategy_n": {
        "enabled": True,
        "live_order_enabled": True,
        "position_pct": 0.825,
        "exit_hold_offset": 2,
    },
}


def make_engine(project_root: Path) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = project_root
    engine.config = dict(LIVE_CONFIG)
    engine.load_positions = lambda: []
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "D_A_M_E_C_N"
    engine.is_b_strategy_removed = lambda: True
    engine.load_today_e_signal = lambda _today: None
    return engine


def write_ac_ops(
    ops_dir: Path, signal_date: str, ac: dict[str, Any] | None
) -> pd.DataFrame:
    if ac is None:
        return pd.DataFrame()
    frame = pd.DataFrame(
        [
            {
                "strategy_leg": str(ac["strategy_leg"]),
                "ts_code": str(ac["ts_code"]),
                "name": str(ac.get("name", "")),
                "side": "BUY",
                "planned_order_date": str(ac["buy_date"]),
                "reference_price": 10.0,
                "round_lot_shares": 10_000,
                "estimated_shares": 10_000,
            }
        ]
    )
    frame.to_csv(ops_dir / f"ops_{signal_date}_planned_orders.csv", index=False)
    return frame


def build_live_picker(
    sources: certify.Sources, workdir: Path, stats: dict[str, int]
):
    ops_dir = workdir / "daily_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(PROJECT_ROOT)

    def pick(
        sources_: certify.Sources,
        row: pd.Series,
        row_index: int,
        *,
        entry_gate_enabled: bool,
        m_enabled: bool,
        n_enabled: bool,
        equity: float,
        peak_equity: float,
    ) -> dict[str, Any] | None:
        del row_index
        signal_date = str(row["date"])
        buy_date = certify.nth_trade_date(sources_, signal_date, 1)
        for stale in ops_dir.glob("*.csv"):
            stale.unlink()

        ac = sources_.ac_daily.get(signal_date)
        ac_frame = write_ac_ops(ops_dir, signal_date, ac)
        e_blocked = e_signal.has_ac_planned_order(signal_date, legs=("A",))
        m_busy, _reason = m_signal.higher_priority_leg_has_signal(signal_date)

        e_payload: dict[str, Any] | None = None
        if not e_blocked and signal_date in sources_.e.index:
            e_row = certify.source_row(sources_.e, signal_date, "E R1")
            if not (
                entry_gate_enabled
                and not certify.e_entry_gate_passes(e_row, sources_.e_spec)
            ):
                e_payload = {
                    "signal_date": signal_date,
                    "ts_code": str(e_row.get("ts_code", "")),
                    "name": str(e_row.get("name", "")),
                    "limit_close": 10.0,
                    "exit_offset": int(
                        pd.to_numeric(e_row.get("exit_offset", 2), errors="coerce") or 2
                    ),
                }

        m_order: dict[str, Any] | None = None
        if m_enabled and not m_busy:
            m_pick = certify.m_candidate(
                sources_, signal_date, equity, peak_equity
            )
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

        n_order: dict[str, Any] | None = None
        n_pick = certify.n_candidate(sources_, signal_date) if n_enabled else None
        if n_pick is not None:
            n_order = {
                "strategy_leg": "N",
                "ts_code": str(n_pick["ts_code"]),
                "name": str(n_pick.get("name", "")),
                "side": "BUY",
                "planned_order_date": buy_date,
                "planned_action": "PLAN_BUY_T1_OPEN",
                "round_lot_shares": 10_000,
                "planned_amount_by_equity": 412_500.0,
            }

        engine.load_latest_abc_orders = lambda: (ops_dir / "ops.csv", ac_frame.copy())
        engine.load_yesterday_e_signal = lambda _today, payload=e_payload: payload
        engine.build_m_buy_order_if_any = lambda _today, _codes=None, order=m_order: (
            dict(order) if order is not None else None
        )
        engine.build_n_buy_order_if_any = lambda _today, _codes=None, order=n_order: (
            dict(order) if order is not None else None
        )

        _state, _decisions, orders = engine.build_mode1_plan(buy_date)
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

        if leg in {"A", "C"}:
            if ac is None or str(ac["ts_code"]) != code:
                raise RuntimeError(
                    f"{signal_date} 实盘选A/C={code}，认证候选={ac}"
                )
            return dict(ac)
        if leg == "M":
            return certify.m_candidate(
                sources_, signal_date, equity, peak_equity
            )
        if leg == "E":
            e_row = certify.source_row(sources_.e, signal_date, "E R1")
            return {
                "strategy_leg": "E",
                "ts_code": str(e_row.get("ts_code", "")),
                "name": str(e_row.get("name", "")),
                "buy_date": certify.normalize_date(e_row.get("buy_date")),
                "exit_date": certify.normalize_date(e_row.get("exit_date")),
                "account_return": certify.to_float(e_row.get("net_return"))
                * certify.POSITION_PCT,
                "return_source": f"E_R1:{e_row.get('scenario_rank', '')}",
            }
        if leg == "N":
            selected_n = certify.n_candidate(sources_, signal_date)
            if selected_n is None or str(selected_n["ts_code"]) != code:
                raise RuntimeError(
                    f"{signal_date} 实盘选N={code}，认证候选={selected_n}"
                )
            return selected_n
        raise RuntimeError(f"{signal_date} 实盘返回未知腿 {leg}")

    return pick


def main() -> None:
    sources = certify.load_sources()
    workdir = Path(tempfile.mkdtemp(prefix="verify_live_"))
    stats = {"no_plan": 0}
    try:
        certified = certify.replay(
            sources, entry_gate_enabled=True, m_enabled=True
        )
        picker = build_live_picker(sources, workdir, stats)
        original_pick = certify.pick_by_priority
        original_ops = e_signal.DAILY_OPS_DIR
        try:
            certify.pick_by_priority = picker
            e_signal.DAILY_OPS_DIR = workdir / "daily_ops"
            live = certify.replay(
                sources, entry_gate_enabled=True, m_enabled=True
            )
        finally:
            certify.pick_by_priority = original_pick
            e_signal.DAILY_OPS_DIR = original_ops
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    a = certify.summarize(certified, "认证脚本")
    b = certify.summarize(live, "实盘计划模块")
    ex_a = certified[certified["status"].eq("EXECUTED")]
    ex_b = live[live["status"].eq("EXECUTED")]
    key_a = list(
        zip(
            ex_a["signal_date"].astype(str),
            ex_a["strategy_leg"].astype(str),
            ex_a["ts_code"].astype(str),
        )
    )
    key_b = list(
        zip(
            ex_b["signal_date"].astype(str),
            ex_b["strategy_leg"].astype(str),
            ex_b["ts_code"].astype(str),
        )
    )
    if key_a != key_b:
        raise RuntimeError("实盘计划选择路径与认证逐笔不一致")
    for field in (
        "executed_trade_count",
        "equity_multiple",
        "max_drawdown",
        "win_rate",
        "max_consecutive_losses",
    ):
        if abs(float(a[field]) - float(b[field])) > 1e-12:
            raise RuntimeError(
                f"{field}不一致：认证={a[field]}，实盘计划={b[field]}"
            )
    print("历史选择路径核对通过")
    print(
        f"{a['executed_trade_count']}笔 | {a['equity_multiple']:.6f}倍 | "
        f"回撤{a['max_drawdown']:.4%} | 胜率{a['win_rate']:.4%} | "
        f"无计划日{stats['no_plan']}"
    )


if __name__ == "__main__":
    main()
