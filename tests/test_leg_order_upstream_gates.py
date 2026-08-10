"""腿序上游门锁：信号生成侧只能被"排在自己前面的腿"挡住。

背景（2026-08-07）
==================
腿序 D>L>A>M>E2>C 落地时踩过一个坑：只改了下游 combined_live_engine 的挑选
顺序，没改上游各信号脚本的占用门。上游门按旧腿序写（"A/C 有计划就挡"、
"E2 有信号就挡 M"），于是那些日子里 M / E2 的信号**根本不会生成**，下游排得
再靠前也是空转——实盘真实跑出来是 L>A>C>E2>M，481信号日回放 22903.30x，
比认证口径 27870.31x 低 17.8%。

所以腿序正确 = 上游门 ∧ 下游腿序，两侧都对才算数。本文件锁上游门那一侧；
下游那一侧见 tests/test_opening_position_policy.py::LegPriorityOrderTests。

认证口径见 scripts/certify_current_executable_portfolio.py::pick_by_priority。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pandas as pd

# 纯逻辑测试不读取.env；开发机未安装python-dotenv时注入无副作用最小桩。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import run_strategy_e2_signal as e2_signal
from scripts import run_strategy_m_signal as m_signal
from scripts.trading_daemon import _build_candidate_choice_lines


def write_ops(directory: Path, signal_date: str, leg: str, ts_code: str = "600000.SH") -> None:
    """写一份 A/C 每日操作台计划单，格式与 generate_live_limit_pool_daily_ops 一致。"""

    pd.DataFrame([{
        "strategy_leg": leg,
        "ts_code": ts_code,
        "name": f"测试{leg}候选",
        "side": "BUY",
        "round_lot_shares": 5_000,
        "reference_price": 10.0,
    }]).to_csv(directory / f"ops_{signal_date}_planned_orders.csv", index=False)


class AcPlannedOrderLegSplitTests(unittest.TestCase):
    """has_ac_planned_order 必须按调用方声明的腿过滤，不能 A/C 一把抓。"""

    def test_只认声明的腿(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            write_ops(ops, "20260803", "C")
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops):
                # C 的计划不能挡住只关心 A 的调用方（E2、M）
                self.assertFalse(e2_signal.has_ac_planned_order("20260803", legs=("A",)))
                # 显式关心 C 时才认
                self.assertTrue(e2_signal.has_ac_planned_order("20260803", legs=("A", "C")))

    def test_a的计划照常挡住(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            write_ops(ops, "20260803", "A")
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops):
                self.assertTrue(e2_signal.has_ac_planned_order("20260803", legs=("A",)))

    def test_旧b计划不占用资金(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            write_ops(ops, "20260803", "B")
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops):
                self.assertFalse(e2_signal.has_ac_planned_order("20260803", legs=("A", "C")))
                self.assertFalse(e2_signal.has_ac_planned_order("20260803", legs=("A", "B", "C")))

    def test_无腿标记的历史文件按最保守口径当作占用(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            pd.DataFrame([{"ts_code": "600000.SH", "side": "BUY"}]).to_csv(
                ops / "ops_20260803_planned_orders.csv", index=False
            )
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops):
                self.assertTrue(e2_signal.has_ac_planned_order("20260803", legs=("A",)))


class MUpstreamGateTests(unittest.TestCase):
    """M 排在 E2/C 之前：只有 L 和 A 有资格挡住 M 的信号。"""

    def test_e2有信号不挡m(self) -> None:
        with patch.object(m_signal, "has_ac_planned_order", return_value=False), \
             patch.object(m_signal, "signal_by_signal_date", return_value=None):
            busy, why = m_signal.higher_priority_leg_has_signal("20260803")
        self.assertFalse(busy, f"E2/C 不得挡住 M：{why}")

    def test_c的计划不挡m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            write_ops(ops, "20260803", "C")
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops), \
                 patch.object(m_signal, "signal_by_signal_date", return_value=None):
                busy, why = m_signal.higher_priority_leg_has_signal("20260803")
        self.assertFalse(busy, f"C 排在 M 之后，不得挡住 M：{why}")

    def test_a的计划挡住m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = Path(tmp)
            write_ops(ops, "20260803", "A")
            with patch.object(e2_signal, "DAILY_OPS_DIR", ops), \
                 patch.object(m_signal, "signal_by_signal_date", return_value=None):
                busy, why = m_signal.higher_priority_leg_has_signal("20260803")
        self.assertTrue(busy)
        self.assertIn("A", why)

    def test_l的信号挡住m(self) -> None:
        with patch.object(m_signal, "has_ac_planned_order", return_value=False), \
             patch.object(m_signal, "signal_by_signal_date",
                          return_value={"ts_code": "300750.SZ"}):
            busy, why = m_signal.higher_priority_leg_has_signal("20260803")
        self.assertTrue(busy)
        self.assertIn("L", why)


class MEquityPeakTests(unittest.TestCase):
    """M信号子进程只读schema=2策略净值账本，所有写入归主守护进程。"""

    def test_腿序门判断前仍读取最新账本(self) -> None:
        calls: list[bool] = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m_signal, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(m_signal, "load_config", return_value={}), \
                 patch.object(m_signal, "load_m_spec", return_value={"enabled": True}), \
                 patch.object(m_signal, "resolve_signal_date", return_value="20260803"), \
                 patch.object(m_signal, "load_open_positions", return_value=[]), \
                 patch.object(m_signal, "has_existing_open_position", return_value=False), \
                 patch.object(
                     m_signal,
                     "current_equity_and_peak",
                     side_effect=lambda _cfg: (calls.append(True) or (1_000_000.0, 1_200_000.0, "测试")),
                 ), \
                 patch.object(m_signal, "higher_priority_leg_has_signal", return_value=(True, "L当日已有信号")), \
                 patch.object(m_signal, "record_run"), \
                 patch.object(sys, "argv", ["run_strategy_m_signal.py", "--signal-date", "20260803"]):
                m_signal.main()
        self.assertEqual(len(calls), 1)

    def test_只接受完整且ready的schema2账本(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            peak_path = Path(tmp) / "m_equity_peak.json"
            with patch.object(m_signal, "EQUITY_PEAK_PATH", peak_path):
                peak_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "ledger_ready": True,
                            "last_equity": 900_000,
                            "peak_equity": 1_000_000,
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    m_signal.current_equity_and_peak({}),
                    (900_000.0, 1_000_000.0, "策略已实现盈亏账本"),
                )
                before = peak_path.read_text(encoding="utf-8")
                self.assertEqual(before, peak_path.read_text(encoding="utf-8"), "读取不得改写账本")

                peak_path.write_text(
                    json.dumps({"schema_version": 2, "ledger_ready": False}),
                    encoding="utf-8",
                )
                self.assertEqual(m_signal.current_equity_and_peak({})[:2], (0.0, 0.0))


class LiveMatchesCertifyProofTests(unittest.TestCase):
    """代入证明：实盘代码本身跑 481 信号日，必须逐笔跑出认证标尺。

    这是整套腿序工作的最终判据——上面那些门的单元测试只能验证"某一处对了"，
    只有把 combined_live_engine.build_model3_plan 和两个上游门函数一起放进
    回放，才能证明"合起来也对"。2026-08-07 的空转事故正是每一处单看都对、
    合起来却差 17.8%。约 9 秒。
    """

    def test_实盘代码逐笔跑出认证标尺(self) -> None:
        import subprocess

        root = Path(__file__).absolute().parents[1]
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "verify_live_engine_matches_certify.py")],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=900,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"实盘代码与认证脚本不一致：\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("✅ 通过", proc.stdout)


class MFillMethodologyTests(unittest.TestCase):
    """M 的成交口径必须与 A/C 逐字相同，回测里不许出现实盘买不到的交易。"""

    def test_m池不含一字涨停买不到的交易(self) -> None:
        """每一笔 M 的 T+1 都必须是能买进去的。

        2026-08-07 之前 M 池直接按 open 成交、不判一字板，含有 20240716 格利尔
        和 20240809 宿迁联盛两笔实盘根本排不到的交易。
        """

        from scripts.build_strategy_m_backtest_pool import is_limit_up_unbuyable

        root = Path(__file__).absolute().parents[1]
        trades = pd.read_csv(
            root / "reports/strategy_m/m_backtest_trades.csv",
            dtype={"trade_date": str, "ts_code": str, "buy_date": str, "exit_date": str},
        )
        bad = [
            (r["trade_date"], r["ts_code"])
            for _, r in trades.iterrows()
            if is_limit_up_unbuyable(str(r["buy_date"]), str(r["ts_code"]))
        ]
        self.assertEqual(bad, [], f"M池含一字涨停买不到的交易: {bad}")

    def test_m双边扣费与ac一致(self) -> None:
        """买 open*1.001、卖 close*0.999，两侧都要扣。"""

        from scripts.build_strategy_m_backtest_pool import BUY_COST, SELL_COST

        self.assertAlmostEqual(BUY_COST, 0.001)
        self.assertAlmostEqual(SELL_COST, 0.001, msg="卖出侧费用不得为0（印花税/过户费/佣金/滑点）")

        root = Path(__file__).absolute().parents[1]
        trades = pd.read_csv(root / "reports/strategy_m/m_backtest_trades.csv")
        # net_return 必须等于 exit_price/buy_price-1（两列已是含费净价）
        recomputed = trades["exit_price"] / trades["buy_price"] - 1.0
        pd.testing.assert_series_equal(
            recomputed, trades["net_return"], check_names=False, atol=1e-12
        )


class RetiredRuleFunctionsTests(unittest.TestCase):
    """被腿序改造废弃的旧规则函数必须从认证脚本里消失，不能留着等人误接回去。"""

    def test_旧规则函数已从certify删除(self) -> None:
        import scripts.certify_current_executable_portfolio as certify

        for name in (
            "mode1_candidate",          # 旧 A/C→E2 两段式
            "choose_l",                 # 旧 L 补位/替换两段式
            "l_replace_guard_passes",   # 旧 L 替换窄门
            "d_relay_candidate",        # 旧 D 接力
        ):
            self.assertFalse(
                hasattr(certify, name),
                f"{name} 编码的是已作废的旧腿序/接力规则，必须删除而不是留在文件里",
            )
        # 现行选腿入口必须还在
        self.assertTrue(hasattr(certify, "pick_by_priority"))
        self.assertTrue(hasattr(certify, "l_candidate"))
        self.assertTrue(hasattr(certify, "m_candidate"))


class LegOrderDeclarationTests(unittest.TestCase):
    """config 里的腿序声明必须和代码一致，避免文档与实现再次分叉。"""

    def test_config记录的标尺与certify输出一致(self) -> None:
        """live_candidate_metrics 必须整块与认证输出对齐，不能只改复利。

        这个块是全仓库唯一同时记录笔数/胜率/回撤/最大连亏的地方，最容易只改
        一两个字段就留下自相矛盾的记录（如 27870x 配 -24.68% 回撤和 132 笔）。
        """

        import json

        import pandas as pd

        root = Path(__file__).absolute().parents[1]
        summary = pd.read_csv(root / "reports/current_portfolio_alignment/portfolio_summary.csv")
        current = summary[summary["is_current_executable"].astype(bool)].iloc[0]
        with_m = summary[summary["scenario"] == "current_with_m_gap_leg"].iloc[0]

        metrics = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))[
            "strategy_model3"
        ]["live_candidate_metrics"]

        self.assertEqual(metrics["scenario"], str(current["scenario"]))
        self.assertEqual(metrics["trade_count"], int(current["executed_trade_count"]))
        self.assertEqual(metrics["max_consecutive_losses"], int(current["max_consecutive_losses"]))
        for key, col in (
            ("win_rate", "win_rate"),
            ("avg_return", "avg_return"),
            ("equity_multiple", "equity_multiple"),
            ("max_drawdown", "max_drawdown"),
        ):
            self.assertAlmostEqual(
                metrics[key], float(current[col]), places=5, msg=f"{key} 与认证输出不一致"
            )
        research = metrics["research_with_m"]
        self.assertEqual(research["trade_count"], int(with_m["executed_trade_count"]))
        self.assertAlmostEqual(research["equity_multiple"], float(with_m["equity_multiple"]), places=5)
        self.assertAlmostEqual(research["max_drawdown"], float(with_m["max_drawdown"]), places=5)

    def test_config_不再声明五腿全空才触发m(self) -> None:
        import json

        cfg = json.loads(
            (Path(__file__).absolute().parents[1] / "config" / "config.json").read_text(
                encoding="utf-8"
            )
        )
        m_cfg = cfg["strategy_m"]
        self.assertFalse(
            m_cfg["require_all_legs_idle"],
            "M 已提到 E2/C 之前，require_all_legs_idle 必须为 false",
        )
        self.assertIn("D>L>A>M>E2>C", m_cfg["file_role"])


if __name__ == "__main__":
    unittest.main()


class BroadcastMatchesOrderingTests(unittest.TestCase):
    """daemon 播报层的腿序必须与下单口径一致。

    播报不下单，但用户靠它盯盘——播报说买 A、实盘买 L，比不播报更糟。
    2026-08-07 腿序改造时这一层漏改过：树状图画着 A>C>E2、最终计划仍用已退役的
    L 替换窄门(l_guard_ok)，与 build_model3_plan 相反。
    """

    def _daemon_source(self) -> str:
        root = Path(__file__).absolute().parents[1]
        return (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")

    def test_不再用已退役的替换窄门做判定(self) -> None:
        src = self._daemon_source()
        # 允许出现在注释/参考播报里，但不得再参与任何 if 判定
        offending = [
            line.strip()
            for line in src.splitlines()
            if "l_guard_ok" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(
            offending, [], f"daemon 仍在用退役的 L 替换窄门做判定: {offending}"
        )

    def test_树状图与总图画的是新腿序(self) -> None:
        src = self._daemon_source()
        for stale in ("A > C > E2", "A主 > C补位 > E2兜底", "L 替换窄门{TAG"):
            self.assertNotIn(stale, src, f"播报里仍有旧腿序文案: {stale}")
        self.assertIn("腿序 D > L > A > M > E2 > C", src)
        self.assertIn("③A主 → ④M补位 → ⑤E2 → ⑥C垫底", src)
        # 开仓决策链的逐腿编号必须与腿序同一套，不能出现两套矛盾编号
        for expect in ("① D盘中：", "② L/model3：", "③ A主策略：",
                       "④ M补位：", "⑤ E2：", "⑥ C垫底："):
            self.assertIn(expect, src, f"决策链编号未按腿序: {expect}")
        self.assertNotIn("⑦ M兜底补位：", src)

    def test_最终计划按腿序A_M_E2_C取第一个(self) -> None:
        src = self._daemon_source()
        self.assertIn("mode1_buy = a_buy or m_buy or e2_buy or c_buy", src)

    def test_旧仓分支才是持仓日的今日路径(self) -> None:
        src = self._daemon_source()
        self.assertIn(
            "结束{TAG if _blocked_by_holding else ''}",
            src,
        )
        self.assertIn(
            "TAG if (l_wins and not _blocked_by_holding) else ''",
            src,
        )
        self.assertIn("账户空仓时无条件优先", src)

    def test_持仓原因优先于L_A候选原因(self) -> None:
        src = self._daemon_source()

        decision_start = src.index("# ── M 补位腿播报")
        decision_end = src.index("# 无论有无开仓候选", decision_start)
        decision_body = src[decision_start:decision_end]
        self.assertLess(
            decision_body.index("if _blocked_by_holding:"),
            decision_body.index('elif l_buy:'),
        )

        d_start = src.index("def _log_d_status_for_signal")
        d_end = src.index("def _value_counts_text", d_start)
        d_body = src[d_start:d_end]
        self.assertLess(d_body.index("if open_positions:"), d_body.index("elif planned_count > 0:"))
        self.assertIn("串行单仓规则优先于A/C候选判断", d_body)

        l_start = src.index("def _log_l_model3_signal_status")
        l_end = src.index("def _load_ab_checklist", l_start)
        l_body = src[l_start:l_end]
        self.assertIn("本次只保留L候选供审计，不生成开仓计划", l_body)
        self.assertIn("L仅在账户空仓时无条件优先于A/M/E2/C", l_body)

    def test_候选让路汇总在D持仓时明确全部不开仓(self) -> None:
        lines = _build_candidate_choice_lines(
            [
                ("D", None, "当前已有D持仓，不再扫描新D"),
                ("L", {"ts_code": "600664.SH", "name": "哈药股份"}, ""),
                ("A", {"ts_code": "301051.SZ", "name": "信濠光电"}, ""),
                ("M", None, "当前持仓已阻断M候选评估"),
                ("E2", None, "无入围候选"),
                ("C", None, "A已入围，C未继续评估"),
            ],
            ["L", "A", "M", "E2", "C"],
            None,
            ["D"],
        )

        text = "\n".join(lines)
        self.assertIn("L策略候选：600664.SH 哈药股份", text)
        self.assertIn("A策略候选：301051.SZ 信濠光电", text)
        self.assertIn(
            "账户空仓时让路排序：L策略 600664.SH 哈药股份 > A策略 301051.SZ 信濠光电",
            text,
        )
        self.assertIn("按让路规则首选：L策略 600664.SH 哈药股份", text)
        self.assertIn("由于当前D策略有持仓，以上候选均不会开仓", text)

    def test_m净值缺失记ERROR而非正常态(self) -> None:
        """M 取不到净值是故障，必须走 ERROR 告警通道，不能伪装成'今天不触发'。"""

        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "run_strategy_m_signal.py").read_text(encoding="utf-8")
        self.assertIn('record_run(signal_date, "ERROR", note, args.dry_run,', src)
        self.assertIn("取不到策略已实现净值", src)
        # 真回撤超阈值仍属正常策略行为
        self.assertIn('record_run(signal_date, "NO_SIGNAL_OCCUPIED", f"回撤保护：{dd_note}"', src)


class MEquitySnapshotByDaemonTests(unittest.TestCase):
    """daemon只负责调度，策略净值计算和原子落盘由独立账本模块完成。"""

    def test_收盘流水线在第9步之前落盘净值(self) -> None:
        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        self.assertIn("def snapshot_equity_for_m(", src)
        # 必须在 job_post_market 里、且排在 M 信号那一步之前
        job_start = src.index("def job_post_market(")
        call_pos = src.index("snapshot_equity_for_m(target_str)", job_start)
        m_step_pos = src.index("run_strategy_m_signal.py", job_start)
        self.assertLess(call_pos, m_step_pos, "净值快照必须排在 M 信号步骤之前")

    def test_启动时也落盘一次(self) -> None:
        """收盘流水线要等到15:10，重启后应能立刻确认M能否取到净值。"""

        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        startup = src.index('log.info("启动检查：扫描逾期/待平仓持仓...")')
        tail = src[startup:startup + 2000]
        self.assertIn("snapshot_equity_for_m(", tail,
                      "启动检查阶段必须做一次M净值快照，否则要等到收盘才知道M能否开仓")

    def test_启动先初始化并恢复m再开启决策播报(self) -> None:
        """首次建账不能让播报线程抢跑；可执行窗口内还要重算净值缺失的M信号。"""

        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        main_start = src.index("def main() -> None:")
        main_body = src[main_start:]
        snapshot_pos = main_body.index("snapshot_equity_for_m(today_beijing()")
        refresh_pos = main_body.index("refresh_m_signal_after_startup_if_needed(expected_str)")
        broadcast_pos = main_body.index('name="decision-chain-broadcast"')
        self.assertLess(snapshot_pos, refresh_pos)
        self.assertLess(refresh_pos, broadcast_pos)

        helper_start = src.index("def refresh_m_signal_after_startup_if_needed(")
        helper_end = src.index("def job_post_market(", helper_start)
        helper = src[helper_start:helper_end]
        self.assertIn("now.time() >= datetime.time(9, 25)", helper)
        self.assertIn('"run_strategy_m_signal.py"', helper)
        self.assertIn("不补生成过期买单", helper)
        self.assertIn("历史状态｜该信号生成时净值账本尚未就绪", src)
        self.assertIn("恢复异常｜当前净值账本已就绪", src)

    def test_m开启后的播报文案不再声称m关闭(self) -> None:
        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        self.assertNotIn("M 关着不该是静默的", src)
        self.assertIn("M已开启，故障必须告警", src)

    def test_只在空仓时落盘(self) -> None:
        """有持仓时 total_asset 含浮动盈亏，会污染峰值。"""

        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        fn_start = src.index("def snapshot_equity_for_m(")
        fn_end = src.index("def job_post_market(", fn_start)
        body = src[fn_start:fn_end]
        self.assertIn('if str(p.get("status", "")).lower() in {"open", "sell_pending"}', body)
        self.assertIn("if open_positions:", body)
        self.assertIn("return", body)

    def test_净值计算已经抽离守护进程(self) -> None:
        root = Path(__file__).absolute().parents[1]
        src = (root / "scripts" / "trading_daemon.py").read_text(encoding="utf-8")
        self.assertIn("update_strategy_equity_ledger(", src)
        module = (root / "src" / "strategy_equity_ledger.py").read_text(encoding="utf-8")
        self.assertIn("peak = max(", module)
        self.assertIn("temporary.replace(path)", module)
