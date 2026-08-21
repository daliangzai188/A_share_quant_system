"""锁定 strategy_config.json 里 A/C 两套规则的归属。

2026-08-05 排查实盘选股时踩过的坑：该文件顶层写着"只用于本地回测、不接实盘"
且 enabled=false，实际却驱动着 A/C 两条实盘腿；更麻烦的是文件里有两套并存
且互斥的规则——

    A → candidate_filters（market_chain_count_bucket=8_15）
    C → paper_ab_filtered_strategy.c_strategy（market_chain_count_bucket=15_30）

当时误把 candidate_filters 当成 C 的规则去比对，得出了错误的"实盘与回测一致"
结论。这些测试确保：两段都存在、归属清晰、互斥关系不被无意改掉。
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).absolute().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy_config.json"


class StrategyConfigSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_section_map_exists(self) -> None:
        """必须保留段落归属说明，否则下一个读这个文件的人还会踩同样的坑。"""
        self.assertIn("_config_section_map", self.config)
        section_map = self.config["_config_section_map"]
        self.assertIn("A策略", section_map)
        self.assertIn("C策略", section_map)

    def test_file_role_warns_it_drives_live(self) -> None:
        """顶层 file_role 必须写明本文件驱动实盘，不能再说'不接实盘'。"""
        role = str(self.config.get("file_role", ""))
        self.assertIn("实盘", role)
        self.assertNotIn("不接实盘", role)

    def test_c_rules_live_in_their_own_section(self) -> None:
        """C 的规则在 paper_ab_filtered_strategy.c_strategy，不在 candidate_filters。"""
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertTrue(c_strategy["enabled"])
        values = {
            str(item["column"]): str(item["value"])
            for item in c_strategy["conditions"]
        }
        self.assertEqual(values["market_chain_count_bucket"], "15_30")
        self.assertEqual(values["segment_limit_up_count_bucket"], "40_80")

    def test_c_holds_three_days(self) -> None:
        """C 是全系统唯一 T+3 退出的腿，18笔回测已验证全部持有3个交易日。"""
        exit_rule = self.config["paper_ab_filtered_strategy"]["c_strategy"]["exit_rule"]
        self.assertEqual(exit_rule["rule_name"], "fixed_hold3_close")
        self.assertEqual(int(exit_rule["max_hold_days"]), 3)

    def test_c_only_backs_up_when_a_empty(self) -> None:
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertTrue(c_strategy["only_when_a_no_candidate"])

    def test_current_strict_anchor_and_c_ranking_are_locked(self) -> None:
        """A/C当前数据窗口必须使用新锚点，且A排序变化不能串到C。"""
        self.assertEqual(
            self.config["data_scope"]["signal_window"],
            {"start_date": "20240630", "end_date": "20260630"},
        )
        c_ranking = self.config["paper_ab_filtered_strategy"]["c_strategy"]["ranking"]
        self.assertEqual(
            c_ranking["columns"], ["profit_source_score", "turnover_rate"]
        )
        self.assertEqual(c_ranking["ascending"], [False, False])

    def test_a_and_c_audit_metrics_are_standalone_single_account(self) -> None:
        """候选池连乘不能再冒充A/C独立策略复利。"""
        a_audit = self.config["risk_metrics_from_latest_audit"]
        self.assertEqual(
            a_audit["metric_scope"], "STRICT_ASOF_A_STANDALONE_SINGLE_ACCOUNT"
        )
        self.assertEqual(a_audit["executed_trade_count"], 58)
        self.assertAlmostEqual(a_audit["equity_multiple"], 12.023750012345724)
        self.assertEqual(a_audit["candidate_pool_trade_count"], 69)

        c_audit = self.config["paper_ab_filtered_strategy"]["c_strategy"][
            "latest_2y_audit"
        ]
        self.assertEqual(
            c_audit["metric_scope"], "STRICT_ASOF_C_STANDALONE_SINGLE_ACCOUNT"
        )
        self.assertEqual(c_audit["c_trade_count"], 35)
        self.assertAlmostEqual(c_audit["c_equity_multiple"], 3.1108307989904436)
        self.assertEqual(c_audit["candidate_pool_trade_count"], 46)

    def test_a_and_c_chain_count_are_mutually_exclusive(self) -> None:
        """A 要 8_15、C 要 15_30，互斥。这个关系变了说明有人改错了段。"""
        a_values = {
            str(item["column"]): str(item["value"])
            for item in self.config["candidate_filters"]["conditions"]
        }
        c_values = {
            str(item["column"]): str(item["value"])
            for item in self.config["paper_ab_filtered_strategy"]["c_strategy"]["conditions"]
        }
        self.assertEqual(a_values["market_chain_count_bucket"], "8_15")
        self.assertEqual(c_values["market_chain_count_bucket"], "15_30")
        self.assertNotEqual(
            a_values["market_chain_count_bucket"],
            c_values["market_chain_count_bucket"],
        )


if __name__ == "__main__":
    unittest.main()
