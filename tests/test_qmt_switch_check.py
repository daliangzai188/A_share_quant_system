"""QMT 换版核对（tools/qmt_switch_check.py，2026-11-26）：换版后补跑的收盘流水线和换版前逐项对比。"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import qmt_switch_check as qsc

REQ = "0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_000300279"
OTHER = "0C:\\QMT\\GJQMT\\python\\OTHER_STRATEGY.py_000000001"


def head(t: str, payload: str, reqid: str = REQ) -> str:
    return "%s,123 [INFO] [0x0000138c] reqid = %s, output = %s" % (t, reqid, payload)


def log(t: str, msg: str) -> str:
    return head(t, "[A_SYSTEM_LOG] %s | %s" % (t, msg))


def box(t: str, signal: str, action: str, d_line: str = "④ D盘中兜底：不启动｜券商仍有本系统策略仓") -> list[str]:
    return [log(t, "┃━━━━━━━━━━━━ 决策优先级流程图 ━━━━━━━━━━━━"),
            "┃ 信号日：%s；操作日：%s" % (signal, action),
            "┃ 当前互斥顺序：①持仓 > ②非D候选/计划(A>C>E) > ③D盘中",
            "┃ ① A主策略：不触发（当前有持仓）｜候选 301066.SZ 万事利",
            "┃ %s" % d_line]


def run(day: str, start: str, target: str, *, normal: bool, stop: str = "停手", funnel: str = "当日 44 -> 44",
        m_count: int = 0, rows: int = 18, d_reason: str = "今日已有非D正式候选/开仓计划", fail_at: int = 0) -> list[str]:
    """一次收盘流水线的日志：normal=True 是正常收盘（打印明日候选播报），False 是换版后补跑。"""
    h, m = int(start[:2]), int(start[3:5])

    def t(sec: int) -> str:
        total = h * 3600 + m * 60 + sec
        return "%s %02d:%02d:%02d" % (day, total // 3600 % 24, total // 60 % 60, total % 60)

    out = [log(t(0), "===== 收盘流水线（目标日期 %s）=====" % target)]
    results = {1: "采集结果：✅ %s raw日线=5573行，每日基本面=5573行，涨停池=51行" % target,
               6: "A/C策略状态：✅ 符合开仓条件，计划单=1，选中=301066.SZ 万事利",
               7: "  E策略：信号日期 %s，正式信号=未生成；优先级结论=账户有未平仓头寸，E不触发" % target}
    for step in range(1, 15):
        if fail_at and step == fail_at:
            out.append(log(t(step * 10), "ERROR | ❌ 第%d步 仍然失败，本次收盘流水线停止；不生成计划单" % step))
            out.append(log(t(step * 10 + 1), "收盘/采集流水线线程结束：测试"))
            return out
        out.append(log(t(step * 10), "收盘流水线进度：%d/14 第%d步 完成（用时%d秒）" % (step, step, step)))
        out[-1] = out[-1].replace("完成（用时%d秒）" % step, "完成")
        out.append(log(t(step * 10 + 1), "%s.py 完成，用时%d秒" % (step, step + (0 if normal else 7))))
        if step in results:
            out.append(log(t(step * 10 + 2), results[step]))
        if step == 8:
            out.append(log(t(step * 10 + 2), "  [run_strategy_m_signal.py] M 信号 %s：20cm 涨停 29 只，候选 %d 只"
                           % (target, m_count)))
        if step == 14:
            out.append(log(t(step * 10 + 2), "  [update_equity_curve_stop.py] EQUITY_CURVE_STOP 20261126 %s：影子净值低于60日均线"
                           % stop))
    out.append(log(t(150), "┃━━━━━━━━━━ 收盘流水线候选产物统计 ━━━━━━━━━━"))
    if normal:
        out += [log(t(150), "=" * 60), log(t(150), "【明日候选】2026-11-26  信号日期：%s" % target),
                log(t(150), "=" * 60), log(t(150), "[开仓计划推送] 已推送 收盘:📋 明日开仓计划")]
        out.append(log(t(150), "notify: 已推送 [daily_summary] 📋 收盘候选统计 %s" % target))
    out += [log(t(151), "----- 信号就绪审计：%s -----" % target),
            log(t(151), "  市场环境：market_sentiment=neutral  全市场涨停=51  跌停=69"),
            log(t(151), "动态市场情绪 特征已加载: /A_System/data/processed/live_market_emotion_features.csv, 行数: %d" % rows),
            log(t(151), "    A主策略 | 1_universe_filters | %s，剔除 0" % funnel),
            log(t(151), "✅ [账户] ***03 总资产14.92万 | 持仓：策略=C"),  # 账户心跳插进来，不属于流水线
            log(t(151), "  D策略停止点：%s；按全日互斥规则D不启动" % d_reason),
            log(t(151), "  漏斗统计：A候选1 C候选0 C过滤0 最终选中1 计划单1"),
            log(t(152), "----- 信号就绪审计结束 -----"),
            log(t(152), "  ✅ raw日线: rows=5573 file=%s.csv" % target),
            log(t(152), "----- 明日开盘因子就绪检查结束 -----")]
    if normal:
        out.append(log(t(152), "===== 收盘流水线完成 ====="))
    else:
        out.append(log(t(152), "离线数据维护完成：%s 数据/影子停手判定已更新；账户未验证" % target))
    out.append(log(t(153), "收盘/采集流水线线程结束：测试"))
    return out


def switch(day: str, t: str, mode: str = "live", release: str = "202611260107-new") -> list[str]:
    return [head("%s %s" % (day, t), "[A_SYSTEM] started: mode=%s release=%s" % (mode, release)),
            log("%s %s" % (day, t), "✅ 交易恢复门禁通过：待恢复0笔"),
            log("%s %s" % (day, t), "notify: 已推送 [connection] ✅ 程序与账户已恢复正常")]


class SwitchCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, lines: list[str]) -> Path:
        p = self.dir / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def check(self, day1: list[str], day2: list[str] | None = None, release: str | None = None) -> qsc.Report:
        paths = [self.write("XtClient_FormulaOutput_20261125.log", day1)]
        if day2 is not None:
            paths.append(self.write("XtClient_FormulaOutput_20261126.log", day2))
        return qsc.check(qsc.read_entries(paths), release)

    def night(self, *, new_kwargs: dict | None = None, new_box: list[str] | None = None, mode: str = "live",
              extra_after: list[str] | None = None) -> qsc.Report:
        """11-25 15:10 正常收盘 → 11-26 03:47 换版 → 补跑 → 04:17 计划框（和 2026-11-26 实际的那次同构）。"""
        old = [head("2026-11-25 15:00:00", "[A_SYSTEM] started: mode=live release=202611242025-old")]
        old += run("2026-11-25", "15:10", "20261125", normal=True, rows=30,
                   d_reason="组合状态机。原因：今日已有A正式候选 301066.SZ 万事利")
        old += box("2026-11-25 03:41:15", "20261125", "20261126")
        new = switch("2026-11-26", "03:47:41", mode=mode) + (extra_after or [])
        new += run("2026-11-26", "03:47", "20261125", normal=False, **(new_kwargs or {}))
        new += new_box if new_box is not None else box("2026-11-26 04:17:44", "20261125", "20261126")
        return self.check(old, new)

    def test_identical_night_switch_passes_and_notes_expected_differences(self) -> None:
        r = self.night()
        text = "\n".join(r.lines)
        self.assertEqual(r.code, 0, text)
        self.assertIn("✅ ⑬停手判定 一致", text)
        self.assertIn("✅ 计划框一致", text)
        # 运行方式不同带来的差异只提示：D 停止原因措辞、特征表行数、正常收盘才有的明日候选播报
        self.assertIn("⚠️ 和持仓有关的说明行不同", text)
        self.assertIn("ℹ️ 特征表行数不同（重启后只补最近几天）：30 → 18", text)
        self.assertIn("ℹ️ 换版前那次是正常收盘", text)

    def test_stop_decision_difference_fails(self) -> None:
        r = self.night(new_kwargs={"stop": "恢复开仓"})
        self.assertEqual(r.code, 1)
        self.assertIn("⑬停手判定不一致", r.lines[-1])

    def test_funnel_difference_fails(self) -> None:
        r = self.night(new_kwargs={"funnel": "当日 44 -> 43"})
        self.assertEqual(r.code, 1)
        self.assertIn("候选产物统计", r.lines[-1])

    def test_m_signal_difference_fails(self) -> None:
        r = self.night(new_kwargs={"m_count": 1})
        self.assertEqual(r.code, 1)
        self.assertIn("各步结果行不一致", r.lines[-1])

    def test_plan_box_difference_fails(self) -> None:
        r = self.night(new_box=box("2026-11-26 04:17:44", "20261125", "20261126", d_line="④ D盘中兜底：启动"))
        self.assertEqual(r.code, 1)
        self.assertIn("计划框不一致", r.lines[-1])

    def test_traceback_after_start_fails(self) -> None:
        r = self.night(extra_after=[head("2026-11-26 03:47:42", "Traceback (most recent call last):")])
        self.assertEqual(r.code, 1)
        self.assertIn("Traceback", r.lines[-1])

    def test_shadow_mode_fails(self) -> None:
        r = self.night(mode="shadow")
        self.assertEqual(r.code, 1)
        self.assertIn("不是实盘模式", r.lines[-1])

    def test_rerun_stopped_fails(self) -> None:
        r = self.night(new_kwargs={"fail_at": 2}, new_box=[])
        self.assertEqual(r.code, 1)
        self.assertIn("没有完成就结束", r.lines[-1])

    def test_rerun_still_running_is_pending(self) -> None:
        old = run("2026-11-25", "15:10", "20261125", normal=True)
        new = switch("2026-11-26", "03:47:41") + run("2026-11-26", "03:47", "20261125", normal=False)[:12]
        r = self.check(old, new)
        self.assertEqual(r.code, 3)
        self.assertIn("还在跑", "\n".join(r.lines))

    def test_waits_for_first_plan_box_after_rerun(self) -> None:
        r = self.night(new_box=[])
        self.assertEqual(r.code, 3)
        self.assertIn("补跑后的计划框", r.lines[-1])

    def test_missing_pre_switch_log_is_reported(self) -> None:
        new = switch("2026-11-26", "03:47:41") + run("2026-11-26", "03:47", "20261125", normal=False)
        r = self.check([], new)
        self.assertEqual(r.code, 4)
        self.assertIn("把那天的 QMT 日志也复制过来", "\n".join(r.lines))

    def test_intraday_switch_lists_box_without_comparing(self) -> None:
        old = run("2026-11-25", "15:10", "20261125", normal=True) + box("2026-11-26 09:55:04", "20261125", "20261126")
        new = switch("2026-11-26", "12:11:30") + run("2026-11-26", "12:11", "20261125", normal=False)
        new += box("2026-11-26 12:42:00", "20261125", "20261126", d_line="④ D盘中兜底：不启动｜已有持仓")
        r = self.check(old + new)
        self.assertEqual(r.code, 0, "\n".join(r.lines))
        self.assertIn("盘中换版", "\n".join(r.lines))

    def test_other_strategies_lines_are_ignored(self) -> None:
        noise = [head("2026-11-26 03:50:00", "[A_SYSTEM_LOG] 2026-11-26 03:50:00 | EQUITY_CURVE_STOP 20261126 恢复开仓",
                      reqid=OTHER)]
        r = self.night(extra_after=noise)
        self.assertEqual(r.code, 0, "\n".join(r.lines))

    def test_release_argument_waits_until_that_start_appears(self) -> None:
        old = [head("2026-11-25 15:00:00", "[A_SYSTEM] started: mode=live release=202611242025-old")]
        old += run("2026-11-25", "15:10", "20261125", normal=True)
        r = self.check(old, release="202611260107-new")  # 新版本的启动行还没同步过来
        self.assertEqual(r.code, 3)
        self.assertIn("新版本启动", r.lines[-1])
        new = switch("2026-11-26", "03:47:41") + run("2026-11-26", "03:47", "20261125", normal=False)
        new += switch("2026-11-26", "05:00:00", release="202611260500-later")
        r2 = self.check(old, new, release="202611260107-new")  # 指定发布号时核对那一次，不受后面的启动影响
        self.assertIn("release 202611260107-new", r2.lines[0])
        self.assertIn("✅ 补跑收盘流水线", "\n".join(r2.lines))

    def test_pre_switch_run_interrupted_by_restart_has_no_baseline(self) -> None:
        old = run("2026-11-26", "15:10", "20261126", normal=True)[:9]  # 15:10 的流水线跑到一半就换版重启
        new = switch("2026-11-26", "15:12:00") + run("2026-11-26", "15:13", "20261126", normal=False)
        r = self.check(old + new)
        self.assertEqual(r.code, 4)
        self.assertIn("被这次重启打断", "\n".join(r.lines))

    def test_main_prints_report_and_returns_code(self) -> None:
        path = self.write("XtClient_FormulaOutput_20261125.log", run("2026-11-25", "15:10", "20261125", normal=True))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = qsc.main(["--logs", str(path)])
        self.assertEqual(code, 4)  # 日志里没有启动行
        self.assertIn("没有找到", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
