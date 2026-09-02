"""锁定 strategy_config.json 里 A/C 两套规则的归属。

2026-08-05 排查实盘选股时踩过的坑：该文件顶层写着"只用于本地回测、不接实盘"
且 enabled=false，实际却驱动着 A/C 两条实盘腿；更麻烦的是文件里有两套并存
但必须独立加载的规则——

    A → candidate_filters.condition_profiles（两个OR分支）
    C → paper_ab_filtered_strategy.c_strategy.condition_profiles（六个可执行profile，归属三类逻辑分支）

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
        """C正式规则必须是六个冻结profile的OR，不能退回旧单AND条件。"""
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertTrue(c_strategy["enabled"])
        self.assertEqual(
            c_strategy["release_id"],
            "C_THIRD_BRANCH_T2_20260902_V16",
        )
        self.assertEqual(c_strategy["condition_mode"], "ANY_PROFILE")
        self.assertEqual(c_strategy["conditions"], [])
        profiles = {}
        for item in c_strategy["condition_profiles"]:
            conditions = {}
            for condition in item["conditions"]:
                conditions[str(condition["column"])] = (
                    tuple(str(value) for value in condition["values"])
                    if str(condition.get("operator", "==")) == "in"
                    else str(condition["value"])
                )
            profiles[str(item["profile_id"])] = conditions
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
            "C_STRONG_LEADER_RANK2_3_FD01_03_LD_LT5": {
                "limit_up_count_bucket": "50_80",
                "market_leader_rank_bucket": "rank_2_3",
                "fd_ratio_bucket": "0_1pct_0_3pct",
                "market_limit_down_count_bucket": "lt_5",
            },
            "C_STRONG_LEADER_RANK2_3_FD01_03_LD_5_15": {
                "limit_up_count_bucket": "50_80",
                "market_leader_rank_bucket": "rank_2_3",
                "fd_ratio_bucket": "0_1pct_0_3pct",
                "market_limit_down_count_bucket": "5_15",
            },
            "C_STRONG_LEADER_RANK2_3_FD01_03_LD_15_30": {
                "limit_up_count_bucket": "50_80",
                "market_leader_rank_bucket": "rank_2_3",
                "fd_ratio_bucket": "0_1pct_0_3pct",
                "market_limit_down_count_bucket": "15_30",
            },
            "C_THIRD_LIMITUP30_50_RANK4_10_FD01_03_CHAIN_NOT15_AMOUNT_NOT2_3": {
                "limit_up_count_bucket": "30_50",
                "market_leader_rank_bucket": "rank_4_10",
                "fd_ratio_bucket": "0_1pct_0_3pct",
                "market_chain_count_bucket": ("lt_3", "3_8", "8_15", "gte_30"),
                "amount_ratio_bucket": (
                    "lt_0_8",
                    "0_8_1_2",
                    "1_2_2",
                    "3_5",
                    "gte_5",
                ),
            },
        })
        single_open_rules = [
            condition
            for rule in c_strategy["risk_reject_rules"]
            for condition in rule.get("numeric_conditions", [])
            if condition.get("column") == "open_times"
        ]
        self.assertIn(
            {"column": "open_times", "operator": "==", "value": 1},
            single_open_rules,
        )

    def test_c_uses_profile_specific_exit_rules(self) -> None:
        """旧C分支T+3、新第3分支T+2，退出映射不得被统一默认值覆盖。"""
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertEqual(c_strategy["default_exit_rule_id"], "fixed_hold3_close")
        self.assertEqual(
            int(c_strategy["exit_rules"]["fixed_hold3_close"]["max_hold_days"]),
            3,
        )
        self.assertEqual(
            int(c_strategy["exit_rules"]["fixed_hold2_close"]["max_hold_days"]),
            2,
        )
        rule_by_profile = {
            str(profile["profile_id"]): str(profile["exit_rule_id"])
            for profile in c_strategy["condition_profiles"]
        }
        self.assertEqual(
            rule_by_profile[
                "C_THIRD_LIMITUP30_50_RANK4_10_FD01_03_CHAIN_NOT15_AMOUNT_NOT2_3"
            ],
            "fixed_hold2_close",
        )
        self.assertEqual(
            {
                rule
                for profile_id, rule in rule_by_profile.items()
                if not profile_id.startswith("C_THIRD_")
            },
            {"fixed_hold3_close"},
        )

    def test_c_only_backs_up_when_a_empty(self) -> None:
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertTrue(c_strategy["only_when_a_no_candidate"])

    def test_current_strict_anchor_and_c_ranking_are_locked(self) -> None:
        """A/C当前数据窗口必须使用新锚点，且A排序变化不能串到C。"""
        self.assertEqual(
            self.config["data_scope"]["signal_window"],
            {"start_date": "20230901", "end_date": "20260831"},
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
        self.assertEqual(a_audit["executed_trade_count"], 82)
        self.assertAlmostEqual(a_audit["equity_multiple"], 94.39844282719737)
        self.assertEqual(a_audit["candidate_pool_trade_count"], 103)

        c_audit = self.config["paper_ab_filtered_strategy"]["c_strategy"][
            "latest_2y_audit"
        ]
        self.assertEqual(
            c_audit["metric_scope"],
            "STRICT_ASOF_C_STANDALONE_SINGLE_ACCOUNT_THREE_YEAR_V12",
        )
        self.assertEqual(c_audit["c_trade_count"], 58)
        self.assertAlmostEqual(c_audit["c_equity_multiple"], 16.592266587212748)
        self.assertEqual(c_audit["candidate_plan_count"], 73)
        self.assertAlmostEqual(
            c_audit["released_acde_equity_multiple"], 6046.316593512633
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
        fallback = a_filters["fallback_when_primary_empty"]
        self.assertTrue(fallback["enabled"])
        self.assertTrue(fallback["same_trade_date_only"])
        self.assertTrue(fallback["inherit_primary_ranking"])
        self.assertEqual(
            {
                str(condition["column"]): str(condition["value"])
                for condition in fallback["conditions"]
            },
            {
                "segment_limit_up_count_bucket": "lt_5",
                "market_chain_count_bucket": "8_15",
                "fd_ratio_bucket": "1pct_2pct",
            },
        )
        self.assertEqual(
            fallback["exclude_conditions"],
            [{"column": "board_type", "operator": "==", "value": "one_word"}],
        )
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        self.assertEqual(c_strategy["conditions"], [])
        self.assertEqual(len(c_strategy["condition_profiles"]), 6)


if __name__ == "__main__":
    unittest.main()
