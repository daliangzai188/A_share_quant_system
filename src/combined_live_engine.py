"""组合状态机：把 D、A、E、C 四条在役腿合成每日唯一的开仓/平仓计划。

腿序与接力口径（2026-08-07 定稿）
================================

**当前腿序：D > A > E > C**

D 排第一不是优化结果，是时序决定的——D 在信号日**盘中**(14:00~14:56)买入，
而 A/C/E 的候选要到当天**收盘后**才算得出来。让 D "看到别的腿有票就不做"
需要预知几小时后的结果，属于前视，实盘做不到。

其余腿序依据（481信号日同口径回放，A/C 已用逐日独立候选、衔接日D已剔除）：
A 与 C 的条件互斥
（A 要 market_chain_count_bucket=8_15、C 要 15_30），同一天不可能都有票，
所以 C 排在 A 之后的任何位置结果都相同。

**D 接力：全关**

旧口径下 D 未到期时若当天有 A/C/E 候选，会在 09:23 卖竞价安全部分、09:30 后按
「卖D一片→买候选一片」的资金中性成对POV接力，同一天资金用两次。现在 D 一律走
自己的 T+2 收盘平仓，确认清仓后的下一个信号日才轮到别的腿。

    接力全关      27870.31x  胜率68.87%
    接力A/C/E   30315.57x  胜率68.21%
（该对比只保留历史审计价值；当前发布必须以新的四腿严格认证为准）

接力多出的 8.8% 里超过一半来自口径不对称——接力的 D 用 T+1 竞价卖出、不打成交
压力折扣，而 T+2 退出的 D 要打 80% 折扣；同折扣口径下接力只值 +7.8%。换来的是
执行链路从五步（09:23卖安全部分→09:30分片卖→确认释放→分片买→累计买入额不得
超过累计卖出额）简化成一条直线，胜率反而更高。代价是两年里有 7 天候选被 D 的
持仓挡掉，已计入上述27870.31x（历史对比标尺；当前发布以认证报告为准）。

**腿序在代码里的落点**

腿序不在一个函数里，读代码时按这个映射找：

    D   monitor_strategy_d_intraday.py 盘中自己判；本模块只负责在有 D 持仓时
        阻断其余各腿（build_mode1_plan 的 open_d_positions 分支）
    A   build_mode1_plan 空仓分支 ①，取 abc_orders 里 strategy_leg=A 的行
    E   build_mode1_plan 空仓分支 ②
    C   build_mode1_plan 空仓分支 ③，取 abc_orders 里 strategy_leg=C 的行
        （2026-08-07 之前 A 和 C 是同一份 abc_orders 一起判的，等于 C 也享受了
         A 的最高优先级，与认证脚本 pick_by_priority 不一致，本次拆开收口）

对应的回测口径见certify_current_executable_portfolio.pick_by_priority。改任何一侧
都要重跑认证。旧组合认证已经失效；新组合未重新冻结认证前，
新BUY必须由LiveOrderGateway fail-closed，已有持仓SELL不得受影响。

复现：python scripts/certify_current_executable_portfolio.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from src.live_order_gateway import LiveOrderGateway
from src.rolling_signal_store import latest_signal_for_buy_date, signal_by_signal_date
from src.strategy_identity import (
    ACTIVE_E_VARIANT,
    STRATEGY_E_LEG,
    normalize_strategy_leg,
    normalize_strategy_record,
)
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.time_utils import today_beijing

_E_POSITION_PCT = 0.825
_E_LOT_SIZE = 100


def round_lot_shares_below_amount(amount: float, price: float, lot_size: int = _E_LOT_SIZE) -> int:
    if amount <= 0 or price <= 0:
        return 0
    max_qty = int((amount - 0.01) / price)
    if lot_size > 0:
        max_qty -= max_qty % lot_size
    return max(max_qty, 0)


@dataclass(frozen=True)
class CombinedLiveDecision:
    action: str
    strategy_leg: str
    ts_code: str = ""
    name: str = ""
    side: str = ""
    quantity: int = 0
    reason: str = ""
    source: str = ""


class CombinedLiveEngine:
    """D>A>E>C 唯一生产组合状态机（B、M、N均已移除）。

    这个类只负责组合层面的顺序和阻断，不直接提交真实委托。
    当前总策略开关在 config/config.json 的 active_strategy_profile.mode：
      1 = D>A>E>C 组合状态机

    当前只保留这一种模式，所有计划单仍经过 LiveOrderGateway 风控。
    """

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.gateway = LiveOrderGateway(config_path)
        self.positions_path = self.project_root / "data" / "processed" / "positions.json"
        self.output_dir = self.project_root / "reports" / "live_trade" / "combined"
        mkdir_p(self.output_dir)

    def is_b_new_entry_enabled(self) -> bool:
        """读取退役标记；当前配置必须返回False，阻断全部B新增买入。"""
        strategy_path = self.project_root / "config" / "strategy_config.json"
        try:
            strategy_config = load_json_config(strategy_path)
        except Exception:
            return False
        b_config = strategy_config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
        return bool(b_config.get("enabled", False)) and not bool(
            b_config.get("new_entries_disabled", False)
        )

    def is_b_strategy_removed(self) -> bool:
        """B彻底删除后，组合计划不得再接收B的买单或卖单。"""
        strategy_path = self.project_root / "config" / "strategy_config.json"
        try:
            strategy_config = load_json_config(strategy_path)
        except Exception:
            return True
        b_config = strategy_config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
        return bool(b_config.get("removed", False)) or bool(b_config.get("manual_exit_only", False))

    def is_manual_exit_only_position(self, position: dict[str, Any]) -> bool:
        if bool(position.get("manual_exit_only", False)) or bool(position.get("auto_exit_disabled", False)):
            return True
        return str(position.get("strategy_leg", "")).upper() == "B" and self.is_b_strategy_removed()

    def active_strategy_mode(self) -> int:
        profile = self.config.get("active_strategy_profile", {})
        try:
            return int(profile.get("mode", 1))
        except (TypeError, ValueError):
            return 1

    def active_strategy_name(self) -> str:
        profile = self.config.get("active_strategy_profile", {})
        modes = profile.get("available_modes", {})
        return str(modes.get(str(self.active_strategy_mode()), profile.get("mode_name", "ACDE")))

    def load_positions(self) -> list[dict[str, Any]]:
        if not self.positions_path.exists():
            return []
        try:
            data = json.loads(self.positions_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [normalize_strategy_record(row) for row in data] if isinstance(data, list) else []

    def load_latest_abc_orders(self) -> tuple[Path | None, pd.DataFrame]:
        try:
            path, orders = self.gateway.load_planned_orders("latest")
        except RuntimeError:
            return None, pd.DataFrame()
        except EmptyDataError:
            return None, pd.DataFrame()
        if not orders.empty and {"strategy_leg", "side"}.issubset(orders.columns):
            is_b = orders["strategy_leg"].astype(str).str.upper().eq("B")
            if self.is_b_strategy_removed():
                orders = orders[~is_b].copy()
            elif not self.is_b_new_entry_enabled():
                disabled_b_buy = is_b & orders["side"].astype(str).str.upper().eq("BUY")
                orders = orders[~disabled_b_buy].copy()
        return path, orders

    @staticmethod
    def is_open_position(position: dict[str, Any]) -> bool:
        return str(position.get("status", "open")).lower() in {"open", "sell_pending"}

    @staticmethod
    def is_d_position(position: dict[str, Any]) -> bool:
        return str(position.get("strategy_leg", "")).upper() == "D"

    @staticmethod
    def is_e_position(position: dict[str, Any]) -> bool:
        return normalize_strategy_leg(position.get("strategy_leg")) == STRATEGY_E_LEG

    @staticmethod
    def as_int(value: Any, default: int = 0) -> int:
        try:
            if value in {None, ""}:
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def load_yesterday_e_signal(self, today: str) -> dict[str, Any] | None:
        """找 today 之前最近的 E 信号，且其 planned_buy_date == today。"""
        signal_dirs = (
            (self.project_root / "reports" / "strategy_e", "e_signals_recent.json", "e_signal_????????.json"),
            (self.project_root / "reports" / "strategy_e2", "e2_signals_recent.json", "e2_signal_????????.json"),
        )
        for signal_dir, rolling_name, _pattern in signal_dirs:
            rolling = latest_signal_for_buy_date(signal_dir / rolling_name, today)
            if rolling is not None:
                return normalize_strategy_record(rolling)
        files = [
            path
            for signal_dir, _rolling_name, pattern in signal_dirs
            for path in signal_dir.glob(pattern)
        ]
        files.sort()
        for f in reversed(files):
            date_part = f.stem.replace("e_signal_", "").replace("e2_signal_", "")
            if date_part >= today:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if str(data.get("planned_buy_date", "")) == today:
                    return normalize_strategy_record(data)
            except Exception:
                continue
        return None

    def load_today_e_signal(self, today: str) -> dict[str, Any] | None:
        """加载今日收盘流水线已生成的 E 信号（signal_date == today），用于盘中状态展示。"""
        paths = (
            self.project_root / "reports" / "strategy_e" / "e_signals_recent.json",
            self.project_root / "reports" / "strategy_e2" / "e2_signals_recent.json",
        )
        for path in paths:
            rolling = signal_by_signal_date(path, today)
            if rolling is not None:
                return normalize_strategy_record(rolling)
        return None

    def compute_e_preview(self, today: str) -> dict[str, Any]:
        """盘中实时预判 E 信号，用当前可用数据尽量多给信息。

        返回 dict 含 keys:
          data_date, segment_states, neutral_segs,
          has_scored_data, has_candidate,
          ts_code, name, circ_mv, fill_probability, limit_close (有候选时)
          reason (无候选时说明原因)
        """
        result: dict[str, Any] = {"data_date": today}
        try:
            from scripts.run_strategy_e_signal import (
                compute_segment_retreat_states,
                load_e_candidates,
                SCORED_PATH,
            )
            # ── 1. 板块状态（只需 raw/limit_list/*.csv，盘中即可用）─────────────
            segment_states = compute_segment_retreat_states(today)
            neutral_segs = [s for s, st in segment_states.items() if st == "neutral"]
            result["segment_states"] = segment_states
            result["neutral_segs"] = neutral_segs

            if not segment_states:
                result["reason"] = f"raw/limit_list/{today}.csv 尚未采集，板块状态未知"
                return result
            # 此处的板块状态只用于盘中预览。正式E neutral必须由统一R1特征链计算，
            # 不能再用旧“连续自然交易日”算法提前阻断，否则实盘会与回测漂移。

            # ── 2. 候选（需要 limit_up_fill_scored.csv 含今日记录）────────────
            has_scored = False
            if SCORED_PATH.exists():
                try:
                    tmp = pd.read_csv(SCORED_PATH, usecols=["trade_date"], nrows=500)
                    has_scored = tmp["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).eq(today).any()
                except Exception:
                    pass
            result["has_scored_data"] = has_scored

            if not has_scored:
                result["reason"] = (
                    f"板块状态已判定：neutral板块={neutral_segs}，E前提满足。"
                    f"候选股待15:10收盘流水线⑦步采集scored数据后确定。"
                )
                return result

            candidates = load_e_candidates(today)
            if candidates.empty:
                result["has_candidate"] = False
                result["reason"] = (
                    "统一R1每日第一名未通过neutral/成交可靠性/"
                    "13:30~14:30入场门禁；当日不回补第二名。"
                )
                return result

            top = candidates.iloc[0]
            result["has_candidate"] = True
            result["candidate_count"] = len(candidates)
            result["ts_code"] = str(top.get("ts_code", ""))
            result["name"] = str(top.get("name", ""))
            result["circ_mv"] = float(top.get("circ_mv", 0) or 0)
            result["fill_probability"] = float(top.get("fill_probability", 0) or 0)
            result["limit_close"] = float(top.get("limit_close", 0) or 0)
            result["r1_scenario_rank"] = int(top.get("scenario_rank", 0) or 0)
            result["exit_rule"] = str(top.get("exit_rule", ""))
        except Exception as exc:
            result["reason"] = f"预判异常：{exc}"
        return result

    def build_e_buy_order(self, signal: dict[str, Any], today: str) -> dict[str, Any]:
        signal = normalize_strategy_record(signal, default_e_variant=ACTIVE_E_VARIANT)
        limit_close = float(signal.get("limit_close", 0.0))
        initial_equity = float(self.config.get("position", {}).get("initial_cash", 500_000.0))
        planned_amount = initial_equity * _E_POSITION_PCT
        if str(self.config.get("trade_mode", "")).lower() == "live":
            # 0=不限额（82.5%目标仓位接管），>0=单笔限额。
            max_single_order_amount = float(
                self.config.get("live_trade", {}).get("max_single_order_amount", 0) or 0
            )
            if max_single_order_amount > 0:
                planned_amount = min(planned_amount, max_single_order_amount)
        round_lot = round_lot_shares_below_amount(planned_amount, limit_close)
        estimated_shares = round_lot
        planned_amount = round_lot * limit_close
        planned_position_pct = planned_amount / initial_equity if initial_equity > 0 else _E_POSITION_PCT
        return {
            "paper_order_id": f"E-BUY-{today}-{signal.get('ts_code','')}",
            "signal_date": signal.get("signal_date", ""),
            "strategy_leg": STRATEGY_E_LEG,
            "strategy_family": STRATEGY_E_LEG,
            "strategy_variant": ACTIVE_E_VARIANT,
            "planned_order_date": today,
            "side": "BUY",
            "ts_code": str(signal.get("ts_code", "")),
            "name": str(signal.get("name", "")),
            "planned_action": "PLAN_BUY_T1_OPEN",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": planned_position_pct,
            "planned_equity": initial_equity,
            "planned_amount_by_equity": planned_amount,
            "reference_price": limit_close,
            "estimated_shares": estimated_shares,
            "round_lot_shares": round_lot,
            "risk_flags": "",
            "live_order_enabled": True,
            # R1规则允许T+2或T+3退出。exit_offset相对信号日，买入发生在T+1，
            # 因此持仓登记使用exit_offset-1；信号缺字段时沿用T+2旧安全默认值。
            "exit_n_days": max(int(signal.get("exit_offset", 2) or 2) - 1, 1),
        }

    def build_e_sell_order(self, position: dict[str, Any], today: str) -> dict[str, Any]:
        shares = self.as_int(position.get("shares", 0))
        return {
            "paper_order_id": f"E-SELL-{today}-{position.get('ts_code','')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "E",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": "PLAN_SELL_T2_CLOSE",
            "order_status": "PLAN_ONLY",
            "planned_position_pct": 0.0,
            "planned_equity": 0.0,
            "planned_amount_by_equity": 0.0,
            "reference_price": float(position.get("buy_price", 0.0)),
            "estimated_shares": shares,
            "round_lot_shares": shares,
            "risk_flags": "E_SELL_T2_CLOSE",
            "live_order_enabled": True,
        }

    def build_d_sell_decision(
        self,
        position: dict[str, Any],
        today: str,
        reason: str | None = None,
        action: str = "PLAN_SELL_D_T2_CLOSE",
    ) -> CombinedLiveDecision:
        # 默认值刻意是 T2_CLOSE 而不是 PLAN_SELL_D_FIRST：后者会被
        # trading_daemon:6832 收进 force_d_sell_codes 触发 09:23 接力卖出。
        # 接力全关后若有调用方漏传 action，安全的方向是走收盘平仓而不是接力。
        return CombinedLiveDecision(
            action=action,
            strategy_leg="D",
            ts_code=str(position.get("ts_code", "")),
            name=str(position.get("name", "")),
            side="SELL",
            quantity=self.as_int(position.get("shares", 0)),
            reason=reason or (
                f"D持仓计划平仓日={position.get('planned_exit_date', '')}，今日={today}；"
                "接力已全关，D按T+2收盘平仓，确认清仓后的下一个信号日才允许新开仓。"
            ),
            source="positions.json",
        )

    def build_d_sell_order(
        self,
        position: dict[str, Any],
        today: str,
        planned_action: str = "PLAN_SELL_D_T2_CLOSE",
    ) -> dict[str, Any]:
        return {
            "paper_order_id": f"D-SELL-{today}-{position.get('ts_code', '')}",
            "signal_date": str(position.get("signal_date", "")),
            "strategy_leg": "D",
            "planned_order_date": today,
            "side": "SELL",
            "ts_code": str(position.get("ts_code", "")),
            "name": str(position.get("name", "")),
            "planned_action": planned_action,
            "order_status": "PLAN_ONLY",
            "planned_position_pct": 0.0,
            "planned_equity": 0.0,
            "planned_amount_by_equity": 0.0,
            "reference_price": 0.0,
            "estimated_shares": self.as_int(position.get("shares", 0)),
            "round_lot_shares": self.as_int(position.get("shares", 0)),
            "risk_flags": "D_SELL_FIRST",
            "live_order_enabled": False,
        }

    def build_abc_buy_decisions(self, orders: pd.DataFrame, source: str) -> list[CombinedLiveDecision]:
        if orders.empty or "side" not in orders.columns:
            return []
        rows = orders[orders["side"].astype(str).str.upper() == "BUY"].copy()
        if rows.empty:
            return []
        qty = rows.apply(
            lambda row: self.as_int(row.get("round_lot_shares", row.get("estimated_shares", 0))),
            axis=1,
        )
        ref_price = pd.to_numeric(rows.get("reference_price", 0.0), errors="coerce").fillna(0.0)
        rows = rows[(qty > 0) & (ref_price > 0)].copy()
        decisions = []
        for _, row in rows.iterrows():
            decisions.append(
                CombinedLiveDecision(
                    action="ALLOW_ABC_BUY_PREVIEW",
                    strategy_leg=str(row.get("strategy_leg", "")),
                    ts_code=str(row.get("ts_code", "")),
                    name=str(row.get("name", "")),
                    side="BUY",
                    quantity=self.as_int(row.get("round_lot_shares", row.get("estimated_shares", 0))),
                    reason="无D未平仓或待卖持仓，允许进入A/C买入预览；真实下单仍需LiveOrderGateway二次校验。",
                    source=source,
                )
            )
        return decisions

    def build_mode1_plan(self, today: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        positions = self.load_positions()
        open_positions = [p for p in positions if self.is_open_position(p)]
        open_d_positions = [p for p in open_positions if self.is_d_position(p)]
        open_non_d_positions = [p for p in open_positions if not self.is_d_position(p)]
        open_e_positions = [p for p in open_non_d_positions if self.is_e_position(p)]
        due_e_positions = [
            p for p in open_e_positions
            if str(p.get("planned_exit_date", "99991231")) <= today
            or str(p.get("status", "")).lower() == "sell_pending"
        ]
        # 今日到期集合只用于生成退出计划和T+0同票保护，绝不能再从开仓占用中排除。
        # 新口径（2026-08-03）：旧仓实际清空前一律不买新仓，取消尾盘旧仓与早盘新仓并存。
        # ABC 到期卖出仍由 daemon check_and_close_positions 执行，不需要额外计划单行。
        due_non_d_positions = [
            p for p in open_non_d_positions
            if not self.is_manual_exit_only_position(p)
            and (
                str(p.get("planned_exit_date", "99991231")) <= today
                or str(p.get("status", "")).lower() == "sell_pending"
            )
        ]
        # 今日到期卖出的标的代码（T+0限制：当日不可再买入同一标的）
        due_selling_codes: set[str] = {str(p.get("ts_code", "")) for p in due_non_d_positions}
        abc_path, abc_orders = self.load_latest_abc_orders()
        # ABC 计划单日期校验（E planned_buy_date==today 的同款保护）：
        # load_latest_abc_orders 只按文件时间取最新，若某晚收盘流水线失败，
        # 第二天会读到前一天的计划——只执行 planned_order_date==today 的行，陈旧计划一律丢弃。
        if not abc_orders.empty and "planned_order_date" in abc_orders.columns:
            date_ok = abc_orders["planned_order_date"].astype(str).str.strip() == str(today)
            stale = abc_orders[~date_ok]
            if not stale.empty:
                import logging as _logging
                for _, srow in stale.iterrows():
                    _logging.getLogger(__name__).warning(
                        "ABC计划单 %s %s planned_order_date=%s ≠ today=%s，视为陈旧计划跳过。",
                        srow.get("ts_code", ""), srow.get("name", ""),
                        srow.get("planned_order_date", ""), today,
                    )
            abc_orders = abc_orders[date_ok].copy()

        decisions: list[CombinedLiveDecision] = []
        planned_orders: list[dict[str, Any]] = []
        state_rows: list[dict[str, Any]] = [
            {
                "today": today,
                "active_strategy_mode": self.active_strategy_mode(),
                "active_strategy_name": self.active_strategy_name(),
                "open_position_count": len(open_positions),
                "open_d_position_count": len(open_d_positions),
                "open_non_d_position_count": len(open_non_d_positions),
                "open_e_position_count": len(open_e_positions),
                "due_e_count": len(due_e_positions),
                "abc_planned_orders_path": str(abc_path or ""),
                "abc_planned_order_count": int(len(abc_orders)) if not abc_orders.empty else 0,
            }
        ]

        # ── E 到期卖出（R1可能T+2/T+3，以planned_exit_date为唯一依据） ───────
        for pos in due_e_positions:
            decisions.append(
                CombinedLiveDecision(
                    action="PLAN_SELL_E",
                    strategy_leg="E",
                    ts_code=str(pos.get("ts_code", "")),
                    name=str(pos.get("name", "")),
                    side="SELL",
                    quantity=self.as_int(pos.get("shares", 0)),
                    reason=(
                        f"E持仓到期平仓，planned_exit_date={pos.get('planned_exit_date','')}，"
                        f"今日={today}，按R1信号锁定的到期日收盘卖出。"
                    ),
                    source="positions.json",
                )
            )
            planned_orders.append(self.build_e_sell_order(pos, today))

        # ── D / ABC / 空仓 主流程 ──────────────────────────────────────────────
        # opened_leg 记录本日最终由哪一腿占用资金（None=没有任何腿开仓）。
        # 有旧持仓的两个分支一律阻断新开仓，所以只有空仓分支会写这个变量；
        # 函数末尾的 E 状态播报要靠它区分"E被前面的腿挡住"和"E自己没信号"。
        opened_leg: str | None = None
        if open_d_positions:
            due_d = [
                p for p in open_d_positions
                if str(p.get("planned_exit_date", "99991231")) <= today
                or str(p.get("status", "")).lower() == "sell_pending"
            ]
            # ── D 接力已于 2026-08-07 全关（见本文件顶部「腿序与接力口径」）──
            #
            # 旧口径：D 未到期时若当天有 A/C/E 候选，就在 09:23 卖竞价安全部分、
            # 09:30 后按「卖D一片→买候选一片」的资金中性成对POV接力，同一天资金
            # 用两次。现口径：D 一律走自己的 T+2 收盘平仓，平仓确认后下一个信号日
            # 才轮到别的腿。
            #
            # 依据（同口径回放，481信号日、已剔除实盘拿不到的衔接日D）：
            #   接力全关 27870.31x / 胜率68.87% / 回撤-23.51%（对比时标尺）
            #   接力A/C/E 30315.57x / 胜率68.21%
            # 接力多出的收益里，超过一半来自「接力的D不打80%成交压力折扣、
            # 而T+2的D要打」这个口径不对称；同折扣口径下接力只值 +7.8%。
            # 换来的是：执行链路从「09:23卖安全部分→09:30分片卖→确认释放→
            # 分片买→累计买入额不得超过累计卖出额」简化为一条直线，胜率反而更高。
            # 代价：两年里有7天候选被D持仓挡掉；该结论已经纳入当前145笔标尺。
            #
            # 因此这里不再生成 PLAN_SELL_D_FIRST（daemon 靠它填 force_d_sell_codes
            # 触发 09:23 接力）与 PLAN_D_RELAY_PAIRED_BUY 影子计划；上游一断，
            # trading_daemon 的整条接力链路自然不触发。
            for position in due_d:
                decisions.append(self.build_d_sell_decision(
                    position,
                    today,
                    action="PLAN_SELL_D_T2_CLOSE",
                    reason=(
                        f"D持仓到默认T+2平仓日，planned_exit_date={position.get('planned_exit_date', '')}，"
                        f"今日={today}，按D回测口径等待14:53收盘平仓，不在09:23集合竞价卖出。"
                    ),
                ))
                planned_orders.append(self.build_d_sell_order(
                    position,
                    today,
                    planned_action="PLAN_SELL_D_T2_CLOSE",
                ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_ABC_BUY", strategy_leg="A+C",
                reason=(
                    "D持仓占用资金且接力已关闭：D按T+2收盘平仓，确认清仓后的"
                    "下一个信号日才允许新开仓。"
                ),
                source="combined_state_machine",
            ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_E_BUY", strategy_leg="E",
                reason="D持仓占用资金且接力已关闭，E今日不开仓。",
                source="combined_state_machine",
            ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                reason="已有D持仓占用资金，今日不启动新的D盘中买入监控。",
                source="combined_state_machine",
            ))

        elif open_non_d_positions:
            # 只要非D旧仓尚未实际清空（含今日到期、逾期、sell_pending），就阻断所有新开仓。
            decisions.append(CombinedLiveDecision(
                action="BLOCK_ABC_BUY", strategy_leg="A+C",
                reason="存在尚未实际清空的旧策略仓（A/C/E或仅人工退出的历史B仓），取消衔接开仓；券商确认清仓前不允许新买入。",
                source="positions.json",
            ))
            decisions.append(CombinedLiveDecision(
                action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                reason="存在尚未实际清空的旧策略仓，D盘中策略跳过；确认清仓后才允许下一次正常开仓。",
                source="positions.json",
            ))

        else:
            # ── 账户无旧策略仓：按腿序 A > E > C 决定今日开仓 ─────────────
            # 完整腿序为 D > A > E > C：
            #   · D 由时序自然排在最前——它在 signal 日盘中 14:00 后就买了，
            #     其余各腿要等收盘出信号、T+1 开盘才买，所以"让 D 往后排"必须
            #     用到收盘后才知道的信息，是前视，不可实现；
            #   · 本函数负责D之后的三档：A > E > C。
            # D 接力已全关：D 未确认卖出前根本不会走到本分支（上面的持仓分支
            # 会一路阻断新开仓），所以这里不再有"卖D一片→买候选一片"的路径。
            abc_decisions = self.build_abc_buy_decisions(abc_orders, str(abc_path or ""))
            # T+0限制：过滤今日集合竞价已卖出的标的（同日不可再买入）
            if due_selling_codes:
                abc_decisions = [d for d in abc_decisions if d.ts_code not in due_selling_codes]
                abc_orders_buy = abc_orders[
                    ~abc_orders["ts_code"].astype(str).isin(due_selling_codes)
                ] if not abc_orders.empty else abc_orders
            else:
                abc_orders_buy = abc_orders

            # ── A 与 C 拆开：C 垫底，排到 E 之后 ────────────────────────────
            # 之前 A 和 C 是同一个 abc_orders 一起判断的，等于 C 也享受了 A 的
            # 最高优先级，与认证脚本 pick_by_priority 的 A>E>C 不一致——
            # 这是腿序改造后实盘与回测之间最后一处口径差，本次收口。
            # 安全性依据：A 与 C 条件互斥。收盘流水线
            # generate_live_limit_pool_daily_ops.select_candidates 每个信号日
            # 先试 A 池，A 池为空才试 C 池，因此同一份 planned_orders.csv 里
            # 不会同时出现 A 和 C（历史48份操作台文件实测 0 例同现）；拆开不会
            # 造成同日两腿抢同一笔资金。
            # 未知腿（含历史 B）归入 A 档，保持改动前的行为不变。
            c_decisions = [
                d for d in abc_decisions if str(d.strategy_leg).strip().upper() == "C"
            ]
            a_decisions = [
                d for d in abc_decisions if str(d.strategy_leg).strip().upper() != "C"
            ]
            if not abc_orders_buy.empty and "strategy_leg" in abc_orders_buy.columns:
                is_c_row = (
                    abc_orders_buy["strategy_leg"].astype(str).str.strip().str.upper().eq("C")
                )
                c_orders_buy = abc_orders_buy[is_c_row]
                a_orders_buy = abc_orders_buy[~is_c_row]
            else:
                c_orders_buy = abc_orders_buy.iloc[0:0]
                a_orders_buy = abc_orders_buy

            # 各腿判断串成一条链，任一腿开仓后（opened_leg 被写上），
            # 后面的腿只写阻断决策、不再取信号。
            yesterday_signal: dict[str, Any] | None = None
            e_order: dict[str, Any] | None = None

            # ① A
            if a_decisions:
                opened_leg = "A"
                decisions.extend(a_decisions)
                planned_orders.extend(a_orders_buy.to_dict("records"))

            # ② E
            if opened_leg is None:
                yesterday_signal = self.load_yesterday_e_signal(today)
                if yesterday_signal:
                    buy_code = str(yesterday_signal.get("ts_code", ""))
                    if buy_code in due_selling_codes:
                        # 今日集合竞价已卖出同一标的，T+0限制不可当日回买
                        import logging as _logging
                        _logging.getLogger(__name__).warning(
                            "E新买入标的 %s 与今日集合竞价卖出标的相同，T+0限制，跳过买入。", buy_code
                        )
                    else:
                        e_order = self.build_e_buy_order(yesterday_signal, today)
                if e_order and e_order.get("round_lot_shares", 0) > 0:
                    opened_leg = "E"
                    planned_orders.append(e_order)
                    decisions.append(CombinedLiveDecision(
                        action="ALLOW_E_BUY",
                        strategy_leg="E",
                        ts_code=str(yesterday_signal.get("ts_code", "")),  # type: ignore[union-attr]
                        name=str(yesterday_signal.get("name", "")),  # type: ignore[union-attr]
                        side="BUY",
                        quantity=e_order.get("round_lot_shares", 0),
                        reason=(
                            f"E昨日信号今日开仓：{yesterday_signal.get('ts_code')} "  # type: ignore[union-attr]
                            f"{yesterday_signal.get('name')}，"  # type: ignore[union-attr]
                            f"T+1开盘买入{e_order.get('round_lot_shares', 0)}股，"
                            f"计划金额约{float(e_order.get('planned_amount_by_equity', 0.0)):.0f}元，"
                            f"按R1信号在T+{int(yesterday_signal.get('exit_offset', 2) or 2)}收盘卖出。"  # type: ignore[union-attr]
                        ),
                        source=str(self.project_root / "reports" / "strategy_e"),
                    ))
                else:
                    # 统一成 None，避免"有 order 但股数为0"的半成品流到摘要里
                    e_order = None

            # ③ C（垫底）
            if opened_leg is None and c_decisions:
                opened_leg = "C"
                decisions.extend(c_decisions)
                planned_orders.extend(c_orders_buy.to_dict("records"))

            # ── 统一写各腿的结论/阻断决策 ────────────────────────────────────
            if opened_leg == "E":
                decisions.append(CombinedLiveDecision(
                    action="NO_ABC_BUY", strategy_leg="A+C",
                    reason="今日A无买入计划，E代替开仓；C排在E之后，本日不再开仓。",
                    source=str(abc_path or ""),
                ))
            elif opened_leg is None:
                decisions.append(CombinedLiveDecision(
                    action="NO_ABC_BUY", strategy_leg="A+C",
                    reason="今日没有A/C买入计划。",
                    source=str(abc_path or ""),
                ))
            if opened_leg is None:
                decisions.append(CombinedLiveDecision(
                    action="ALLOW_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason="无持仓且A、E、C今日均无买入计划，允许启动D盘中监控；D本身仍需实时行情、成交概率和风控校验。",
                    source="combined_state_machine",
                ))
            else:
                _d_block_reason = {
                    "A": "今日存在A买入计划，D盘中策略不再使用同一资金。",
                    "E": "E今日开仓使用同一资金，D盘中监控跳过。",
                    "C": "C按腿序垫底开仓并使用同一资金，D盘中监控跳过。",
                }[opened_leg]
                decisions.append(CombinedLiveDecision(
                    action="BLOCK_D_INTRADAY_MONITOR", strategy_leg="D",
                    reason=_d_block_reason,
                    source="combined_state_machine",
                ))
        # ── E 盘中状态显示（摘要用） ─────────────────────────────────────────
        has_e_buy = any(d.action == "ALLOW_E_BUY" for d in decisions)
        has_e_sell = any(d.action == "PLAN_SELL_E" for d in decisions)
        has_abc_buy = any(d.action == "ALLOW_ABC_BUY_PREVIEW" for d in decisions)
        # BLOCK_E_BUY_UNTIL_D_SOLD 及其 WAIT_D_SELL_THEN_ALLOW_E_BUY 播报随
        # 2026-08-07 D接力全关一并删除：D持仓日现在直接走 BLOCK_E_BUY，
        # 不存在"等D卖完再买E"的中间态。

        if has_e_sell:
            e_status_action = "PLAN_SELL_E_TODAY"
            e_status_reason = f"E持仓今日到期，收盘前平仓（14:53 job_afternoon 执行）。"
        elif has_e_buy:
            e_status_action = "ALLOW_E_BUY_TODAY"
            e_status_reason = "E今日T+1开仓，已加入组合计划单。"
        elif open_positions:
            e_status_action = "BLOCK_E"
            e_status_reason = f"账户有 {len(open_positions)} 个未平仓头寸，E 不触发。"
        elif has_abc_buy:
            e_status_action = "BLOCK_E"
            # C 排在 E 之后：走到 C 开仓说明 E 这一档已经先判过且没信号，
            # 不能再播报成"被A/C抢走资金"。
            e_status_reason = (
                "今日 C 按腿序垫底开仓（E 已先判过且无可执行信号）。"
                if opened_leg == "C"
                else "今日 A 已生成买入计划，E 不触发（资金冲突）。"
            )
        else:
            e_status_action = "ALLOW_E_SIGNAL"
            today_signal = self.load_today_e_signal(today)
            if today_signal:
                ts = today_signal.get("ts_code", "")
                nm = today_signal.get("name", "")
                buy_dt = today_signal.get("planned_buy_date", "")
                lc = float(today_signal.get("limit_close", 0) or 0)
                fp = float(today_signal.get("fill_probability", 0) or 0)
                circ = float(today_signal.get("circ_mv", 0) or 0)
                e_status_reason = (
                    f"今日已扫描到E候选：{ts} {nm}，"
                    f"计划买入日={buy_dt}，"
                    f"涨停参考价={lc:.2f}元，"
                    f"流通市值={circ/10000:.1f}亿，"
                    f"成交概率={fp:.1%}。"
                    f"（明日09:20组合状态机将生成ALLOW_E_BUY开仓计划）"
                )
            else:
                preview = self.compute_e_preview(today)
                neutral_segs = preview.get("neutral_segs", [])
                if preview.get("has_candidate"):
                    ts = preview["ts_code"]
                    nm = preview["name"]
                    circ = preview["circ_mv"]
                    fp = preview["fill_probability"]
                    lc = preview["limit_close"]
                    e_status_reason = (
                        f"E预判可触发：候选 {ts} {nm}，"
                        f"流通市值={circ/10000:.1f}亿，成交概率={fp:.1%}，参考价={lc:.2f}元，"
                        f"neutral板块={neutral_segs}。"
                        f"（15:10流水线⑦步生成正式信号文件和明日买入计划）"
                    )
                elif neutral_segs and not preview.get("has_scored_data", True):
                    e_status_reason = (
                        f"板块状态已判定：neutral板块={neutral_segs}，E前提满足。"
                        f"候选股待15:10流水线⑦步采集scored数据后确定。"
                    )
                elif preview.get("has_candidate") is False:
                    e_status_reason = preview.get("reason", "E今日不触发。")
                else:
                    e_status_reason = preview.get(
                        "reason",
                        "今日raw数据尚未采集，等待15:10收盘流水线⑦步自动扫描。"
                    )
        decisions.append(CombinedLiveDecision(
            action=e_status_action,
            strategy_leg="E_STATUS",
            reason=e_status_reason,
            source="combined_state_machine",
        ))

        decision_df = pd.DataFrame([decision.__dict__ for decision in decisions])
        state_df = pd.DataFrame(state_rows)
        planned_orders_df = pd.DataFrame(planned_orders)
        return state_df, decision_df, planned_orders_df

    def build_plan(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        today = today_beijing().strftime("%Y%m%d")
        return self.build_mode1_plan(today)

    def write_plan(self) -> dict[str, Path]:
        today = today_beijing().strftime("%Y%m%d")
        state, decisions, planned_orders = self.build_plan()
        state_path = self.output_dir / f"combined_state_{today}.csv"
        decisions_path = self.output_dir / f"combined_decisions_{today}.csv"
        orders_path = self.output_dir / f"combined_planned_orders_{today}.csv"
        md_path = self.output_dir / f"combined_plan_{today}.md"
        state.to_csv(state_path, index=False, encoding="utf-8-sig")
        decisions.to_csv(decisions_path, index=False, encoding="utf-8-sig")
        planned_orders.to_csv(orders_path, index=False, encoding="utf-8-sig")
        self.write_markdown(md_path, state, decisions, planned_orders)

        # 打印 E 状态
        print()
        print("─" * 60)
        print("  策略 E 状态（无前视单账户R1 · T+1买/T+2或T+3卖）")
        print("─" * 60)
        if not decisions.empty and "strategy_leg" in decisions.columns:
            # 当日买卖动作行（PLAN_SELL_E / ALLOW_E_BUY）
            e_act_rows = decisions[
                decisions["strategy_leg"].astype(str).eq("E")
                & decisions["action"].astype(str).isin({"PLAN_SELL_E", "ALLOW_E_BUY"})
            ]
            for _, row in e_act_rows.iterrows():
                action = str(row.get("action", ""))
                code = str(row.get("ts_code", ""))
                nm = str(row.get("name", ""))
                qty = row.get("quantity", 0)
                try:
                    qty_str = f"{int(float(qty))}股" if qty and str(qty) not in {"", "nan", "0"} else ""
                except Exception:
                    qty_str = ""
                if action == "PLAN_SELL_E":
                    print(f"  ⏳ 今日 T+2 平仓 → {code} {nm}  {qty_str}  14:53收盘前卖出")
                elif action == "ALLOW_E_BUY":
                    amount_text = ""
                    if not planned_orders.empty and "ts_code" in planned_orders.columns:
                        order_rows = planned_orders[
                            planned_orders["ts_code"].astype(str).eq(code)
                            & planned_orders["side"].astype(str).str.upper().eq("BUY")
                        ]
                        if not order_rows.empty:
                            amount = float(order_rows.iloc[0].get("planned_amount_by_equity", 0.0) or 0.0)
                            amount_text = f"  计划金额约{amount:.0f}元"
                    print(f"  ✅ 今日 T+1 开仓 → {code} {nm}  {qty_str}{amount_text}  开盘买入")

            # 汇总状态行
            e_status_rows = decisions[decisions["strategy_leg"].astype(str).eq("E_STATUS")]
            if not e_status_rows.empty:
                e = e_status_rows.iloc[0]
                action = str(e.get("action", ""))
                reason = str(e.get("reason", ""))
                if action == "ALLOW_E_SIGNAL":
                    today_sig = self.load_today_e_signal(today)
                    if today_sig:
                        code = today_sig.get("ts_code", "")
                        nm = today_sig.get("name", "")
                        buy_dt = today_sig.get("planned_buy_date", "")
                        lc = float(today_sig.get("limit_close", 0) or 0)
                        fp = float(today_sig.get("fill_probability", 0) or 0)
                        circ = float(today_sig.get("circ_mv", 0) or 0)
                        print(f"  ✔ 今日已扫描到E候选：{code} {nm}")
                        print(f"     计划买入日：{buy_dt}  涨停参考价：{lc:.2f}元")
                        print(f"     流通市值：{circ/10000:.1f}亿  成交概率：{fp:.1%}")
                        print(f"     → 明日09:20组合状态机自动生成开仓计划")
                    else:
                        preview = self.compute_e_preview(today)
                        neutral_segs = preview.get("neutral_segs", [])
                        if preview.get("has_candidate"):
                            print(f"  ✔ E预判可触发：{preview['ts_code']} {preview['name']}")
                            print(f"     neutral板块={neutral_segs}")
                            print(f"     流通市值={preview['circ_mv']/10000:.1f}亿  成交概率={preview['fill_probability']:.1%}  参考价={preview['limit_close']:.2f}元")
                            print(f"     → 15:10流水线⑦步生成正式信号文件和明日买入计划")
                        elif neutral_segs and not preview.get("has_scored_data", True):
                            print(f"  ✔ 板块状态已判定：neutral板块={neutral_segs}，E前提满足")
                            print(f"     → 候选股待15:10流水线⑦步采集scored数据后确定")
                        elif preview.get("has_candidate") is False:
                            print(f"  ✘ E今日不触发：{preview.get('reason', '')}")
                        else:
                            print(f"  ？ {preview.get('reason', 'raw数据尚未采集，等待15:10流水线')}")
                elif action in {"ALLOW_E_BUY_TODAY", "PLAN_SELL_E_TODAY"}:
                    print(f"  ✔ {reason}")
                elif action == "BLOCK_E":
                    print(f"  ✘ E不触发：{reason}")
                else:
                    print(f"  {action}：{reason}")
            elif e_act_rows.empty:
                print("  — E 无决策")
        print("─" * 60)

        return {"state": state_path, "decisions": decisions_path, "planned_orders": orders_path, "markdown": md_path}

    @staticmethod
    def write_markdown(path: Path, state: pd.DataFrame, decisions: pd.DataFrame, planned_orders: pd.DataFrame) -> None:
        active_mode = "1"
        active_name = "D_A_E_C"
        if not state.empty:
            active_mode = str(state.iloc[0].get("active_strategy_mode", "1"))
            active_name = str(state.iloc[0].get("active_strategy_name", "D_A_E_C"))
        title = "D>A>E>C 组合实盘计划"
        status_leg = "E"
        status_title = "策略 E 状态"
        status_rows = (
            decisions[decisions["strategy_leg"] == status_leg]
            if not decisions.empty and "strategy_leg" in decisions.columns
            else pd.DataFrame()
        )
        strategy_status = ""
        if not status_rows.empty:
            status = status_rows.iloc[0]
            strategy_status = f"\n## {status_title}\n\n{status['reason']}\n"

        content = f"""# {title}

本报告只做组合状态机判断，不提交真实委托。

当前总策略模式：{active_mode}（{active_name}）

## 状态

{state.to_markdown(index=False)}

## 决策

{decisions.to_markdown(index=False) if not decisions.empty else "无决策。"}

## 组合计划单

{planned_orders.to_markdown(index=False) if not planned_orders.empty else "无组合计划单。"}
{strategy_status}
## 执行原则

- 若存在 D 待卖持仓，先卖 D，未确认卖出前阻断 A/C 买入。
- 若存在 A/C 旧持仓或仅人工退出的历史B仓，阻断 D 盘中买入，避免资金冲突。
- 若今日已有 A/C 买入计划，默认不启动 D 盘中买入监控。
- 若无持仓且 A/C、E 今日均无买入计划，才允许 D 盘中监控。
- 普通空仓日目标仓位为总资产82.5%，任何单票仍受总资产85%硬顶；旧策略仓未实际清空前取消衔接开仓。
- E 条件：40条R1规则的当日第一名并集 → neutral + 非ST + 成交可靠 → 流通市值最小1只；T+1按82.5%目标仓开仓，按命中规则在T+2或T+3到期日卖出。
- 腿序：D > A > E > C。D的位置由时序锁死（盘中买入，早于收盘后各腿）。
- D 接力已全关：D 一律走 T+2 收盘平仓，平仓确认后的下一个信号日才轮到别的腿，不再有 09:23 卖一片买一片的成对POV。
- 真实下单仍必须经过 LiveOrderGateway 的交易时间、涨跌停、持仓、资金和重复委托校验。
"""
        path.write_text(content, encoding="utf-8")
