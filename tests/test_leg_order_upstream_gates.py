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
    """净值峰值必须在每个空仓日都记录，否则回撤闸的输入是滞后的。"""

    def test_腿序门拦下时仍然记录峰值(self) -> None:
        """被 L/A 挡住的空仓日，净值同样是"已实现"的，必须计入峰值。

        2026-08-07 之前 update_equity_peak 排在腿序门和回撤闸之后，这些日子
        一律不记峰值 → 峰值滞后 → 回撤被低估 → M 在真实回撤中仍可能被放行。
        """

        with tempfile.TemporaryDirectory() as tmp:
            peak_path = Path(tmp) / "m_equity_peak.json"
            calls: list[tuple[float, float, str]] = []

            with patch.object(m_signal, "EQUITY_PEAK_PATH", peak_path), \
                 patch.object(m_signal, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(m_signal, "load_config", return_value={}), \
                 patch.object(m_signal, "load_m_spec", return_value={"enabled": True}), \
                 patch.object(m_signal, "resolve_signal_date", return_value="20260803"), \
                 patch.object(m_signal, "load_open_positions", return_value=[]), \
                 patch.object(m_signal, "has_existing_open_position", return_value=False), \
                 patch.object(m_signal, "current_equity_and_peak",
                              return_value=(1_000_000.0, 1_200_000.0, "测试")), \
                 patch.object(m_signal, "update_equity_peak",
                              side_effect=lambda e, p, d, dry_run=False: calls.append((e, p, d))), \
                 patch.object(m_signal, "higher_priority_leg_has_signal",
                              return_value=(True, "L当日已有信号")), \
                 patch.object(m_signal, "record_run"), \
                 patch.object(sys, "argv", ["run_strategy_m_signal.py", "--signal-date", "20260803"]):
                m_signal.main()

        self.assertEqual(len(calls), 1, "被腿序门拦下的空仓日也必须记录一次峰值")
        self.assertEqual(calls[0][0], 1_000_000.0)
        self.assertEqual(calls[0][1], 1_200_000.0)

    def test_dry_run_不写盘(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            peak_path = Path(tmp) / "m_equity_peak.json"
            with patch.object(m_signal, "EQUITY_PEAK_PATH", peak_path), \
                 patch.object(m_signal, "OUTPUT_DIR", Path(tmp)):
                m_signal.update_equity_peak(1_000_000.0, 1_200_000.0, "20260803", dry_run=True)
                self.assertFalse(peak_path.exists(), "--dry-run 不得污染回撤闸的输入")
                m_signal.update_equity_peak(1_000_000.0, 1_200_000.0, "20260803", dry_run=False)
                self.assertTrue(peak_path.exists())


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
        with_m = summary[summary["scenario"] == "current_with_m_gap_leg"].iloc[0]
        without_m = summary[
            summary["scenario"] == "current_after_e2_gate_and_l_chain_3_8_expansion"
        ].iloc[0]

        metrics = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))[
            "strategy_model3"
        ]["live_candidate_metrics"]

        self.assertEqual(metrics["trade_count"], int(with_m["executed_trade_count"]))
        self.assertEqual(metrics["max_consecutive_losses"], int(with_m["max_consecutive_losses"]))
        for key, col in (
            ("win_rate", "win_rate"),
            ("avg_return", "avg_return"),
            ("equity_multiple", "equity_multiple"),
            ("max_drawdown", "max_drawdown"),
        ):
            self.assertAlmostEqual(
                metrics[key], float(with_m[col]), places=5, msg=f"{key} 与认证输出不一致"
            )
        prev = metrics["previous_without_m"]
        self.assertEqual(prev["trade_count"], int(without_m["executed_trade_count"]))
        self.assertAlmostEqual(prev["equity_multiple"], float(without_m["equity_multiple"]), places=5)
        self.assertAlmostEqual(prev["max_drawdown"], float(without_m["max_drawdown"]), places=5)

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
