"""策略日报（2026-11-14起）：每个交易日一份，标题写级别，要处理的排最前，已复核的列为观察中，后面是各项数值。"""
from __future__ import annotations

import unittest

from src.daily_report import build_daily_report, report_level, terminal_lines, value_lines
from src.review_triggers import FINAL_STANDARD, ReviewTrigger

ACTION = "请在Claude发送：甲·临时复核"


def first(key="signal_drought", detail="A/C/E和M已连续23个行动日没有选出一只股票（最近一次20261009，历史最长22天）"):
    return ReviewTrigger(key, "数据", detail)


def middle(scope="E腿"):
    return ReviewTrigger("warn2:leg:E", "中级·策略复核", "E腿单独按规则最近12个月-30.0%", action="请在Claude发送：甲·策略复核（E腿）",
                         tier=2, scope=scope)


def final(scope="A腿"):
    return ReviewTrigger("warn3:leg:A", "终极·建议停止交易", "A腿单独按规则最近12个月-55.0%",
                         action="建议先暂停交易，再在Claude发送：甲·强制优化（A腿）", tier=3, scope=scope)


WATCHED = ReviewTrigger("factor_health:A", "策略逻辑", "A腿条件集滚动12个月优势+0.05%，连续4个月跌破历史最低+0.54%（207个样本，选股土壤可能已退化）")


class LevelAndTitleTests(unittest.TestCase):
    def report(self, triggers, acknowledged=None, first_seen=None):
        return build_daily_report("20261116", triggers, acknowledged or {}, {"factor_health:A": "2027-01-02"},
                                  first_seen or {}, ["实盘：逐笔对账一致"], final_standard=FINAL_STANDARD)

    def test_all_normal_still_lists_watched_and_values(self):
        got = self.report([WATCHED], acknowledged={"factor_health:A": "已复核"})
        self.assertEqual((got["level"], got["title"]), ("ok", "✅ 方案甲策略日报 11-16：一切正常"))
        self.assertEqual(got["lines"][0], "要处理：无")
        self.assertIn("观察中（已复核，不用处理）：A腿条件集滚动12个月优势+0.05%，连续4个月跌破历史最低+0.54%（2027-01-02复查）",
                      got["lines"])
        self.assertEqual(got["lines"][-1], "实盘：逐笔对账一致")
        self.assertEqual(got["first_seen"], {}, "已登记复核的不记首次出现日")

    def test_first_tier_lists_problems_first_with_what_to_do(self):
        got = self.report([first()])
        self.assertEqual((got["level"], got["title"]), ("review", "🚨 方案甲策略日报 11-16：需要临时复核（1项）"))
        self.assertEqual(got["lines"][:3], ["要处理：", "1.【数据·新】" + first().detail, "需要你做的：" + ACTION])
        self.assertEqual(got["first_seen"], {"signal_drought": "20261116"})
        again = self.report([first()], first_seen={"signal_drought": "20261113"})
        self.assertEqual(again["lines"][1], "1.【数据·自11-13起】" + first().detail, "持续中的写明从哪天起")
        self.assertEqual(again["first_seen"], {"signal_drought": "20261113"})

    def test_middle_and_final_titles_scopes_order_and_standard(self):
        got = self.report([first(), middle()])
        self.assertEqual((got["level"], got["title"]), ("middle", "⚠️ 方案甲策略日报 11-16：中级警告，E腿需要策略复核"))
        self.assertTrue(got["lines"][1].startswith("1.【中级·策略复核·新】"), "级别高的排前面")
        self.assertEqual(got["lines"][3], "需要你做的：请在Claude发送：甲·策略复核（E腿）；" + ACTION)
        worst = self.report([first(), middle(), final()])
        self.assertEqual((worst["level"], worst["title"]),
                         ("final", "⛔ 方案甲策略日报 11-16：终极警告，建议停止交易、优化策略（A腿）"))
        self.assertIn(FINAL_STANDARD + "。系统不会自己停止交易，停不停由你决定。", worst["lines"])
        self.assertNotIn(FINAL_STANDARD + "。系统不会自己停止交易，停不停由你决定。", got["lines"])

    def test_record_only_items_and_duplicates(self):
        note = ReviewTrigger("stop_missed_gain:20260916", "停手参数", "本段停手踏空+30.0%", requires_review=False,
                             action="记入复核记录，作为年度复核输入；停手参数当年不改")
        self.assertEqual(report_level([note]), "note")
        got = self.report([note, note])
        self.assertEqual(got["title"], "📝 方案甲策略日报 11-16：1项需记录")
        self.assertEqual(sum(1 for line in got["lines"] if line.startswith("1.") or line.startswith("2.")), 1, "同一个键只列一次")


class ValueLineTests(unittest.TestCase):
    def test_full_values(self):
        lines = value_lines({
            "alignment": [], "m_slippage": {"completed": 0, "trades": 10, "limit_pct": 1.5},
            "ace_slippage": {"completed": 4, "trades": 10, "limit_pct": 1.2, "average_pct": 0.35},
            "live_loss": {"months": 0, "need": 3},
            "coverage": {"pool_rows": 84, "limit_up_count": 93.0, "min_ratio": 0.3},
            "signals": {"gap": 0, "line": 22, "last_any": "20261110",
                        "last": {"A": "20261110", "C": "20261009", "E": "20261102", "M": "20261028"}},
            "health": {"A": {"value": 0.000521, "line": 0.00539, "months_below": 4},
                       "C": {"value": 0.01666, "line": -0.00474, "months_below": 0},
                       "E": {"value": 0.01285, "line": -0.00712, "months_below": 0}},
            "warnings": {"legs": {"A": {"value": 1.385}, "C": {"value": 3.519}, "E": {"value": None}, "M": {"value": 1.815}},
                         "combo_drawdown": {"value": -0.227}, "combo_relative": {"value": 0.209}},
            "thresholds": {"combo_middle": -0.315, "combo_final": -0.5},
            "shadow": {"r3": -0.337, "r6": -0.12, "fail_3m": -0.241, "fail_6m": -0.194},
            "stall": {"streak": 1, "need_months": 3},
        })
        self.assertEqual(lines, [
            "实盘：逐笔对账一致；M滑点：还没有已平仓的；A/C/E滑点：已平仓4笔，平均双边+0.35%（满10笔判断，线1.2%）；"
            "账户连续亏损0个月（3个月报警）",
            "数据：研究池覆盖率90%（84/93只，低于30%报警）；最近一次选出股票11-10（A 11-10、C 10-09、E 11-02、M 10-28），"
            "之后0个行动日没有新的（超过22天报警）",
            "因子健康：A +0.05%（线+0.54%，连续4个月在线下）；C +1.67%（线-0.47%）；E +1.29%（线-0.71%）",
            "各腿近12个月（按规则）：A +138.5%、C +351.9%、E 不足10笔、M +181.5%",
            "组合（按规则）：ACE+M回撤-22.7%（中级-31.5%、终极-50.0%）；近12个月比只用ACE+20.9%",
            "ACE影子账：近3个月-33.7%（线-24.1%）、近6个月-12.0%（线-19.4%）；正常行情连续不赚钱1/3个月",
        ])

    def test_failed_and_missing_items(self):
        lines = value_lines({"alignment": None, "alignment_error": "E: x", "m_slippage_error": "K: y",
                             "coverage": {"pool_rows": 0, "limit_up_count": None}, "health_error": "V: z",
                             "warnings_error": "R: w", "shadow_error": "S: v", "live_loss_error": "L: u"})
        self.assertEqual(lines, ["实盘：逐笔对账没算成；M滑点没算成；连续亏损没算成",
                                 "数据：没有全市场涨停统计，覆盖率未判断",
                                 "因子健康：没算成", "规则回放：没算成", "ACE影子账：影子账没算成"])
        self.assertEqual(value_lines({"alignment": None}), ["实盘：逐笔对账未启用"])
        self.assertEqual(value_lines({"alignment": ["a", "b"]})[0], "实盘：逐笔对账2处不一致")


class TerminalTests(unittest.TestCase):
    def test_terminal_shows_the_same_report(self):
        report = build_daily_report("20261116", [], {}, {}, {}, ["实盘：逐笔对账一致"])
        self.assertEqual(terminal_lines(report), ["=" * 60, "✅ 方案甲策略日报 11-16：一切正常", "要处理：无",
                                                  "实盘：逐笔对账一致", "=" * 60])
        self.assertEqual(terminal_lines(None), [])
        self.assertEqual(terminal_lines({"lines": ["x"]}), [])


if __name__ == "__main__":
    unittest.main()
