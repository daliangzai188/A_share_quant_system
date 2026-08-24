"""锁定 strategy_config.json 里 A/C 两套规则的归属。

2026-08-05 排查实盘选股时踩过的坑：该文件顶层写着"只用于本地回测、不接实盘"
且 enabled=false，实际却驱动着 A/C 两条实盘腿；更麻烦的是文件里有两套并存
但必须独立加载的规则——

    A → candidate_filters.condition_profiles（两个OR分支）
    C → paper_ab_filtered_strategy.c_strategy.condition_profiles（两个OR分支）

当时误把 candidate_filters 当成 C 的规则去比对，得出了错误的"实盘与回测一致"
结论。这些测试确保：两段都存在、归属清晰，A/C各自的OR分支不会串用。
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
        """C正式规则必须是两个冻结profile的OR，不能退回旧单AND条件。"""
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertTrue(c_strategy["enabled"])
        self.assertEqual(c_strategy["release_id"], "C_LEADER_UNION_20260630_V1")
        self.assertEqual(c_strategy["condition_mode"], "ANY_PROFILE")
        self.assertEqual(c_strategy["conditions"], [])
        profiles = {
            str(item["profile_id"]): {
                str(condition["column"]): str(condition["value"])
                for condition in item["conditions"]
            }
            for item in c_strategy["condition_profiles"]
        }
        self.assertEqual(profiles, {
            "C_CORE_REFINEMENT_1100_1330_MULTI_OPEN": {
                "market_chain_count_bucket": "15_30",
                "segment_limit_up_count_bucket": "40_80",
                "first_time_detail_bucket": "1100_1330",
                "board_type": "multi_open",
            },
            "C_STRONG_LEADER_RANK4_10_FD01_03": {
                "limit_up_count_bucket": "50_80",
                "market_leader_rank_bucket": "rank_4_10",
                "fd_ratio_bucket": "0_1pct_0_3pct",
            },
        })

    def test_c_holds_three_days(self) -> None:
        """C正式两分支版仍使用T+3退出，不得因入选条件更新改变卖出口径。"""
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
        self.assertEqual(a_audit["executed_trade_count"], 63)
        self.assertAlmostEqual(a_audit["equity_multiple"], 18.91154868679943)
        self.assertEqual(a_audit["candidate_pool_trade_count"], 78)

        c_audit = self.config["paper_ab_filtered_strategy"]["c_strategy"][
            "latest_2y_audit"
        ]
        self.assertEqual(
            c_audit["metric_scope"], "STRICT_ASOF_C_STANDALONE_SINGLE_ACCOUNT"
        )
        self.assertEqual(c_audit["c_trade_count"], 55)
        self.assertAlmostEqual(c_audit["c_equity_multiple"], 23.617616094205008)
        self.assertEqual(c_audit["candidate_day_count"], 72)
        self.assertAlmostEqual(
            c_audit["released_acde_equity_multiple"], 921.3365015462819
        )

    def test_a_and_c_rules_remain_in_separate_config_sections(self) -> None:
        """A/C各自的OR规则不得覆盖另一段配置。"""
        a_filters = self.config["candidate_filters"]
        self.assertEqual(a_filters["condition_mode"], "ANY_PROFILE")
        self.assertEqual(a_filters["conditions"], [])
        a_profiles = {
            str(item["profile_id"]): {
                str(condition["column"]): str(condition["value"])
                for condition in item["conditions"]
            }
            for item in a_filters["condition_profiles"]
        }
        self.assertEqual(a_profiles, {
            "A_FD_0_5PCT_1PCT": {
                "segment_limit_up_count_bucket": "lt_5",
                "market_chain_count_bucket": "8_15",
                "fd_ratio_bucket": "0_5pct_1pct",
            },
            "A_FD_0_3PCT_0_5PCT": {
                "segment_limit_up_count_bucket": "lt_5",
                "market_chain_count_bucket": "8_15",
                "fd_ratio_bucket": "0_3pct_0_5pct",
            },
        })
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertEqual(c_strategy["conditions"], [])
        self.assertEqual(len(c_strategy["condition_profiles"]), 2)


if __name__ == "__main__":
    unittest.main()
