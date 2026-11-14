"""每条规则只有一个出处（2026-11-15，项目优化验收）。

生产代码里：
- 仓位数字（ACE 82.5%、单票硬顶85%、M 17.5%、合计99.5%）只写在 src/portfolio_ace_m.py；
- 交易所板块规则（按代码前缀判断板块、涨跌停幅度、6位代码补后缀、申报股数）只写在 src/market_rules.py；
- 策略腿和固定腿序 A>C>E>D 只写在 src/portfolio_ace_m.py（ACE_LEG_PRIORITY 等），实盘组合状态机按它依次尝试开仓，
  影子账回放按它裁决；
- src 不反过来导入 scripts，生产模块之间没有循环导入。

以后在别处再写一份，这里立刻失败并列出位置。另外核对：收拢前各处的旧写法，在正常（非ST）股票上和
现在的唯一出处逐一相同；只有ST股票的涨跌停幅度改按现行规则（主板风险警示股10%），系统从不买ST股票。
"""
from __future__ import annotations

import io
import tokenize
import unittest
from pathlib import Path

from tests.golden import project_metrics as pm

ROOT = Path(__file__).resolve().parents[1]


def _production() -> tuple[dict[str, Path], set[str]]:
    files = pm.module_files(ROOT)
    return files, pm.closure(pm.import_edges(files), pm.ROOTS)


class RuleSingleSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files, cls.prod = _production()

    def test_sizing_numbers_only_in_portfolio_ace_m(self) -> None:
        found = []
        for name in sorted(self.prod - {pm.SIZING_SOURCE}):
            text = self.files[name].read_text(encoding="utf-8")
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.NUMBER and tok.string in pm.SIZING_NUMBERS:
                    found.append("%s:%d %s" % (self.files[name].relative_to(ROOT), tok.start[0], tok.string))
        self.assertEqual(found, [], "仓位数字只许写在 src/portfolio_ace_m.py，改为引用那里的常量：%s" % found)

    def test_board_rules_only_in_market_rules(self) -> None:
        found = []
        for name in sorted(self.prod - {pm.MARKET_RULES_SOURCE}):
            for number, line in enumerate(self.files[name].read_text(encoding="utf-8").splitlines(), 1):
                if pm.BOARD_PREFIX_CHECK.search(line):
                    found.append("%s:%d %s" % (self.files[name].relative_to(ROOT), number, line.strip()))
        self.assertEqual(found, [], "按代码前缀判断板块只许写在 src/market_rules.py（market_segment、price_limit_pct、"
                                    "normalize_ts_code、min_order_shares）：%s" % found)

    def test_leg_combinations_only_in_portfolio_ace_m(self) -> None:
        import ast

        found = []
        for name in sorted(self.prod - {pm.SIZING_SOURCE}):
            tree = ast.parse(self.files[name].read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and len(node.elts) >= 2 and all(
                        isinstance(e, ast.Constant) and e.value in pm.LEG_LETTERS for e in node.elts):
                    found.append("%s:%d %s" % (self.files[name].relative_to(ROOT), node.lineno, ast.unparse(node)))
        self.assertEqual(found, [], "策略腿组合只许写在 src/portfolio_ace_m.py（ACE_LEG_PRIORITY、ACE_STATIC_LEGS、"
                                    "AC_LEGS、ALL_LEGS、REPORT_LEGS）：%s" % found)

    def test_live_engine_and_replay_share_leg_priority(self) -> None:
        from src.acde_rolling_framework import FIXED_PRIORITY
        from src.portfolio_ace_m import ACE_LEG_PRIORITY, first_leg_to_open

        self.assertIs(FIXED_PRIORITY, ACE_LEG_PRIORITY)
        self.assertEqual(ACE_LEG_PRIORITY, ("A", "C", "E", "D"))
        calls = []

        def opener(leg, result):
            def run():
                calls.append(leg)
                return result
            return run

        # 第一条开成的腿胜出，之后的腿不再调用；没有开仓函数的腿（D）跳过
        self.assertEqual(first_leg_to_open({"A": opener("A", False), "C": opener("C", True),
                                            "E": opener("E", True)}), "C")
        self.assertEqual(calls, ["A", "C"])
        calls.clear()
        self.assertIsNone(first_leg_to_open({"E": opener("E", False), "A": opener("A", False)}))
        self.assertEqual(calls, ["A", "E"])

    def test_src_never_imports_scripts(self) -> None:
        imports = pm.import_edges(self.files, strings=False)
        reverse = sorted("%s→%s" % (m, t) for m in self.prod if m.startswith("src.")
                         for t in imports[m] if t.startswith("scripts."))
        self.assertEqual(reverse, [], "src 反过来导入了 scripts：%s" % reverse)

    def test_no_import_cycles_in_production(self) -> None:
        imports = pm.import_edges(self.files, strings=False)
        cycles = [c for c in pm.cycles(imports) if any(m in self.prod for m in c)]
        self.assertEqual(cycles, [], "生产代码有循环导入：%s" % cycles)


# ── 收拢前的旧写法（原样抄自 2026-11-14 的代码），用来证明在正常股票上结果不变 ──────────────────

def _legacy_normalize(value) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _legacy_daemon_limit(ts_code: str, name: str = "") -> float:
    code = str(ts_code).upper()
    if "ST" in str(name).upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "920", "830", "870")) or code.endswith(".BJ"):
        return 0.30
    return 0.10


def _legacy_replay_limit(ts_code: str, name: str = "") -> float:
    stock_name = str(name).upper()
    code = str(ts_code).strip().upper()
    prefix = code.split(".")[0]
    if "ST" in stock_name or "退" in stock_name:
        return 0.05
    if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
        return 0.30
    if prefix.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def _legacy_takeprofit_limit(ts_code: str) -> float:
    short = ts_code.split(".")[0]
    return 0.30 if ts_code.endswith(".BJ") else (0.20 if short.startswith(("300", "301", "688", "689")) else 0.10)


def _legacy_segment(ts_code: str) -> str:
    code = str(ts_code).strip().upper()
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
        return "bj"
    if prefix.startswith(("688", "689")):
        return "star"
    if prefix.startswith(("300", "301")):
        return "chi_next"
    if code.endswith(".SH") and prefix.startswith("6"):
        return "sh_main"
    if code.endswith(".SZ") and prefix.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "other"


STOCK_CODES = [
    "600000.SH", "601398.SH", "603259.SH", "605499.SH", "688001.SH", "688981.SH", "689009.SH",
    "000001.SZ", "001979.SZ", "002594.SZ", "003816.SZ", "300750.SZ", "301236.SZ",
    "430047.BJ", "830799.BJ", "870199.BJ", "920001.BJ",
]


class MarketRulesMatchLegacyTests(unittest.TestCase):
    def test_code_suffix_same_as_the_four_old_copies(self) -> None:
        from src.daemon.common import _normalize_ts_code
        from src.execution_completion_tracker import _code as tracker_code
        from src.execution_data_quality import _code as quality_code
        from src.market_rules import normalize_ts_code
        from src.trade_recovery import _normalize_code

        for value in ["", None, " 002687 ", "002687.sz", "600000", "601398", "900901", "200002", "300750",
                      "430047", "830799", "920001", "123456", "688001.SH"]:
            expected = _legacy_normalize(value)
            for function in (normalize_ts_code, _normalize_ts_code, _normalize_code):
                self.assertEqual(function(value), expected, (function.__name__, value))
            self.assertEqual(tracker_code(value), expected, value)
            self.assertEqual(quality_code(value), expected, value)

    def test_segment_same_as_old_copies(self) -> None:
        from scripts.monitor_strategy_d_intraday import classify_market_segment
        from src.e_signal_inputs import classify_segment
        from src.market_rules import market_segment

        for code in STOCK_CODES + ["900901.SH", "200002.SZ", "", "600000"]:
            expected = _legacy_segment(code)
            self.assertEqual(market_segment(code), expected, code)
            self.assertEqual(classify_market_segment(code), expected, code)
            self.assertEqual(classify_segment(code), expected, code)

    def test_limit_pct_unchanged_for_normal_stocks(self) -> None:
        from scripts.monitor_strategy_d_intraday import limit_up_pct as d_limit
        from src.daemon.plans import _limit_up_pct_for_stock
        from src.market_rules import price_limit_pct
        from src.trade_replay import ConservativeTradeReplay as TradeReplay

        for code in STOCK_CODES:
            for name in ("", "平安银行", "某某科技"):
                legacy = _legacy_daemon_limit(code, name)
                self.assertEqual(_legacy_replay_limit(code, name), legacy, code)
                self.assertEqual(_legacy_takeprofit_limit(code), legacy, code)
                self.assertEqual(price_limit_pct(code, name=name), legacy, (code, name))
                self.assertEqual(_limit_up_pct_for_stock(code, name), legacy, (code, name))
                self.assertEqual(d_limit(code, name), legacy, (code, name))
                self.assertEqual(TradeReplay.limit_up_pct(code, name), legacy, (code, name))

    def test_st_limit_follows_current_exchange_rule(self) -> None:
        """只有ST股票变了：旧写法一律5%，现行规则主板风险警示股10%、创业板/科创板20%、北交所30%。"""
        from scripts.monitor_strategy_d_intraday import limit_up_pct as d_limit
        from src.daemon.plans import _limit_up_pct_for_stock
        from src.trade_replay import ConservativeTradeReplay as TradeReplay

        for code, expected in (("600001.SH", 0.10), ("000004.SZ", 0.10), ("300001.SZ", 0.20),
                               ("688001.SH", 0.20), ("830799.BJ", 0.30)):
            for function in (_limit_up_pct_for_stock, d_limit, TradeReplay.limit_up_pct):
                self.assertEqual(function(code, "*ST某某"), expected, (function.__name__, code))

    def test_min_order_shares(self) -> None:
        from src.market_rules import BOARD_LOT_SHARES, STAR_MIN_ORDER_SHARES, min_order_shares

        self.assertEqual((STAR_MIN_ORDER_SHARES, BOARD_LOT_SHARES), (200, 100))
        self.assertEqual([min_order_shares(c) for c in ("688001.SH", "689009.SH", "688001")], [200, 200, 200])
        self.assertEqual([min_order_shares(c) for c in ("600000.SH", "300750.SZ", "830799.BJ")], [100, 100, 100])


if __name__ == "__main__":
    unittest.main()
