#!/usr/bin/env python3
"""QMT 换版核对（2026-11-26）：换版后补跑的收盘流水线和换版前逐项对比，代替手工挑日志看。

只读 QMT 策略日志（XtClient_FormulaOutput_YYYYMMDD.log）的副本，不连 QMT、不碰交易、不写任何文件。日志在虚拟机上，
换版时用 qmt_native/切换上线手册_QMT原生版.md 附录里的 PowerShell 循环每 60 秒复制到 C:\\A_System\\reports\\qmt_native\\，
Syncthing 同步到本机同名目录（*.tmp 不同步，复制到一半的文件不会过来）。

  python3 tools/qmt_switch_check.py                        核对日志里最近一次启动
  python3 tools/qmt_switch_check.py --release 发布号        核对指定发布号的那次启动（换版时用它：没看到这个发布号的启动行就继续等）
  python3 tools/qmt_switch_check.py --watch 60             还没出结果时每 60 秒重读一次，最多等 60 分钟
  python3 tools/qmt_switch_check.py --logs 文件 [文件 …]     指定日志文件（默认 reports/qmt_native/ 下全部）

核对项（全部满足才算通过）：
  1. 新版本以实盘模式启动；交易恢复门禁通过；推送了“✅ 程序与账户已恢复正常”；启动后的日志里没有 Traceback。
  2. 启动后补跑的收盘流水线 14 步全部完成（目标日就是启动后第一次流水线的目标日）。
  3. 和换版前同一目标日最后一次完成的流水线逐项相同：14 步名单、各步结果行（采集、涨停数据、清洗、动态特征、成交概率、
     增强因子、A/C、M 信号）、⑬停手判定、候选产物统计里从“信号就绪审计”到“明日开盘因子就绪检查结束”的每一行
     （市场环境、数据口径、A/C 逐层漏斗、停止点、漏斗统计、明日因子检查）。
  4. 换版前最后一个和补跑完成后第一个“决策优先级流程图/开仓决策链”计划框（信号日、操作日相同）逐行相同；框里有
     A/C/E 每条腿的结论、D 是否启动和最终开仓计划。盘中（09:15–15:00）启动的，框会随成交变化，只列出、不比对。
只提示、不判失败的差异（换版前后运行方式或账户状态不同时本来就会变，结论已由第 4 项的计划框严格比对）：
  - 正常收盘才打印的“【明日候选】”和开仓计划推送（补跑时账户门禁还没通过，不打印）；
  - 取决于当时持仓的说明行：E策略优先级结论、D策略停止点、E信号脚本说明；
  - “特征已加载 … 行数”（QMT 重启后内存里的特征表只补最近几天，行数比正常收盘少；用到的结果行仍逐字比对）。
启动到补跑后第一个计划框之间的 ERROR 行全部列出，供人看一眼。

退出码：0 通过；1 有不一致或失败；3 还没出结果（还没看到新版本的启动行、补跑没完成，或补跑后的第一个计划框还没打印）；
4 没有可比的基准（缺换版前的日志，或换版前同一目标日的流水线被这次重启打断、没跑完）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = ROOT / "reports" / "qmt_native"
STRATEGY = "A_SYSTEM_QMT_INNER"  # 实盘副本的策略文件名（QMT 日志 reqid 里带它）

HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} \[\w+\] \[0x[0-9a-fA-F]+\] reqid = (.*?), output = (.*)$")
LOG_LINE = re.compile(r"^\[A_SYSTEM_LOG\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (.*)$")
STARTED = re.compile(r"^\[A_SYSTEM\] started: mode=(\w+) release=(\S+)")
RUN_START = re.compile(r"^===== 收盘流水线（目标日期 (\d{8})）=====$")
RUN_STEP_DONE = re.compile(r"^收盘流水线进度：(\d+)/(\d+) (.*) 完成$")
RUN_THREAD_END = "收盘/采集流水线线程结束"
RUN_STOPPED = "本次收盘流水线停止"
BLOCK_START = "收盘流水线候选产物统计"
BLOCK_END = "明日开盘因子就绪检查结束"
BOX_START = "决策优先级流程图"
BOX_DATES = re.compile(r"^┃ 信号日：(\d{8})；操作日：(\d{8})")
RESULT_LINE = re.compile(
    r"^(采集结果：|涨停数据状态：|清洗结果：|动态特征：|成交概率：|增强因子：|A/C策略状态：|A/C候选分布：|\s*E策略：|"
    r"\s*\[run_strategy_m_signal\.py\] (M 信号 |\{)|\s*\[update_equity_curve_stop\.py\] EQUITY_CURVE_STOP )")
STOP_DECISION = re.compile(r"EQUITY_CURVE_STOP (\d{8}) .*")
AUDIT_START = "----- 信号就绪审计"
# 只提示、不判失败的行（见文件开头说明）
ACCOUNT_RELATED = re.compile(r"^\s*E策略：|D策略停止点|E信号脚本")
FEATURE_ROWS = re.compile(r"特征已加载: .*, 行数: \d+$")
# 其它线程（账户心跳、健康播报、启动检查）在流水线运行时插进来的行，不属于流水线结果
NOISE = re.compile(r"\[账户\]|账户快照|QMT查询通道|^notify:|status: trading|全策略净值快照|需要平仓|收盘卖门禁|下次任务|"
                   r"启动检查|启动补检|交易恢复门禁|QMT启动门禁|交易日历已覆盖|守护进程启动|D盘中临时数据清理|health_beacon|健康播报|"
                   r"健康检查异常")


def norm(text: str) -> str:
    """去掉和结果无关、每次运行都会变的部分：用时、临时目录。"""
    text = re.sub(r"用时\s*[\d.]+\s*(秒|s)", "用时*", text)
    text = re.sub(r"/tmp/[^/\s]+/", "/tmp/*/", text)
    return text.rstrip()


@dataclass
class Entry:
    seq: int
    qmt_time: str  # QMT 日志这一条的时间（续行沿用所在条目的时间）
    payload: str


@dataclass
class Run:
    target: str
    start_seq: int
    start_time: str
    end_seq: int | None = None
    end_time: str = ""
    steps_done: list[str] = field(default_factory=list)
    steps_total: int = 0
    results: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)
    broadcast: list[str] = field(default_factory=list)  # 候选产物统计里审计之前的部分（正常收盘的明日候选播报）
    stopped: bool = False
    thread_ended: bool = False

    @property
    def complete(self) -> bool:
        return (not self.stopped and self.steps_total > 0 and len(self.steps_done) == self.steps_total
                and bool(self.block) and BLOCK_END in self.block[-1])

    @property
    def stop_decision(self) -> str:
        for line in self.results:
            m = STOP_DECISION.search(line)
            if m:
                return m.group(0)
        return ""


def read_entries(paths: list[Path], strategy: str = STRATEGY) -> list[Entry]:
    """按文件名（日期）顺序读出实盘副本的每一行输出；续行归到它所在的条目。"""
    out: list[Entry] = []
    seq = 0
    for path in sorted(paths, key=lambda p: p.name):
        keep = False
        qmt_time = ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.rstrip("\r\n")
                m = HEAD.match(raw)
                if m:
                    qmt_time, reqid, payload = m.group(1), m.group(2), m.group(3)
                    keep = strategy in reqid
                elif keep:
                    payload = raw
                else:
                    continue
                if not keep:
                    continue
                if payload.startswith("[A_SYSTEM_NOTIFY"):
                    payload = urllib.parse.unquote(payload)
                out.append(Entry(seq, qmt_time, payload))
                seq += 1
    return out


def log_msg(payload: str) -> tuple[str, str] | None:
    m = LOG_LINE.match(payload)
    return (m.group(1), m.group(2)) if m else None


def find_runs(entries: list[Entry]) -> list[Run]:
    runs: list[Run] = []
    cur: Run | None = None
    in_block = False
    for e in entries:
        if STARTED.match(e.payload) and cur is not None:  # 重启打断了正在跑的流水线
            cur.end_seq, cur.end_time, cur = e.seq, e.qmt_time, None
            in_block = False
            continue
        parsed = log_msg(e.payload)
        if not parsed:
            continue
        ts, msg = parsed
        m = RUN_START.match(msg)
        if m:
            if cur is not None:
                cur.end_seq, cur.end_time = e.seq, ts
            cur = Run(m.group(1), e.seq, ts)
            runs.append(cur)
            in_block = False
            continue
        if cur is None:
            continue
        if RUN_THREAD_END in msg:
            cur.thread_ended = True
            cur.end_seq, cur.end_time, cur = e.seq, ts, None
            in_block = False
            continue
        if RUN_STOPPED in msg:
            cur.stopped = True
        step = RUN_STEP_DONE.match(msg)
        if step:
            cur.steps_done.append(step.group(3))
            cur.steps_total = int(step.group(2))
        if BLOCK_START in msg:
            in_block = True
            continue
        if in_block:
            if NOISE.search(msg):
                continue
            if not cur.block and AUDIT_START not in msg:
                cur.broadcast.append(norm(msg))
                continue
            cur.block.append(norm(msg))
            if BLOCK_END in msg:
                in_block = False
            continue
        if RESULT_LINE.match(msg) and not NOISE.search(msg):
            cur.results.append(norm(msg))
    return runs


@dataclass
class Box:
    seq: int
    time: str
    lines: list[str]

    @property
    def dates(self) -> tuple[str, str] | None:
        for line in self.lines:
            m = BOX_DATES.match(line)
            if m:
                return m.group(1), m.group(2)
        return None


def find_boxes(entries: list[Entry]) -> list[Box]:
    boxes: list[Box] = []
    cur: Box | None = None
    for e in entries:
        if cur is not None:
            if e.payload.startswith("┃"):
                cur.lines.append(e.payload.rstrip())
                continue
            cur = None
        parsed = log_msg(e.payload)
        if parsed and BOX_START in parsed[1]:
            cur = Box(e.seq, parsed[0], [parsed[1].rstrip()])
            boxes.append(cur)
    return boxes


def diff_lines(old: list[str], new: list[str]) -> list[str]:
    import difflib
    return [d for d in difflib.unified_diff(old, new, "换版前", "换版后", n=0, lineterm="")][2:]


@dataclass
class Report:
    code: int
    lines: list[str]


def check(entries: list[Entry], release: str | None = None) -> Report:
    out: list[str] = []
    starts = [(e, STARTED.match(e.payload)) for e in entries if STARTED.match(e.payload)]
    if release:
        starts = [s for s in starts if s[1].group(2) == release]
    if not starts:
        if release:  # 新版本刚启动、日志副本还没同步过来时会这样，等下一次复制
            return Report(3, ["还没看到发布号 %s 的启动行（[A_SYSTEM] started: …）" % release,
                              "结论：还没出结果（等：新版本启动）"])
        return Report(4, ["日志里没有找到启动行（[A_SYSTEM] started: …）"])
    start, sm = starts[-1]
    mode, rel = sm.group(1), sm.group(2)
    after = [e for e in entries if e.seq > start.seq]
    next_start = next((e for e in after if STARTED.match(e.payload)), None)
    if next_start is not None:  # 只看这次启动到下一次启动之间
        after = [e for e in after if e.seq < next_start.seq]
    fails: list[str] = []
    pending: list[str] = []
    out.append("换版核对：release %s，%s 以%s模式启动" % (rel, start.qmt_time, "实盘" if mode == "live" else mode))
    if mode != "live":
        fails.append("不是实盘模式（mode=%s）" % mode)

    def first(pattern: str) -> Entry | None:
        return next((e for e in after if pattern in e.payload), None)

    for label, pattern in (("交易恢复门禁通过", "✅ 交易恢复门禁通过"),
                           ("推送“✅ 程序与账户已恢复正常”", "已推送 [connection] ✅ 程序与账户已恢复正常")):
        hit = first(pattern)
        if hit:
            out.append("  ✅ %s（%s）" % (label, (log_msg(hit.payload) or (hit.qmt_time, ""))[0]))
        else:
            out.append("  … 还没看到：%s" % label)
            pending.append(label)
    tracebacks = [e for e in after if "Traceback" in e.payload]
    if tracebacks:
        fails.append("启动后有 %d 处 Traceback" % len(tracebacks))
        out.append("  ❌ 启动后有 %d 处 Traceback（第一处 %s）" % (len(tracebacks), tracebacks[0].qmt_time))
    else:
        out.append("  ✅ 启动后没有 Traceback")

    runs = find_runs(entries)
    new_runs = [r for r in runs if r.start_seq > start.seq and (next_start is None or r.start_seq < next_start.seq)]
    if not new_runs:
        out.append("  … 启动后还没开始补跑收盘流水线")
        return _finish(out, fails, pending + ["补跑收盘流水线"])
    target = new_runs[0].target
    new_done = [r for r in new_runs if r.target == target and r.complete]
    failed = [r for r in new_runs if r.target == target and r.stopped]
    for r in failed:
        out.append("  ⚠️ 补跑 %s 起的一次停止（%d/%d 步），之后按规则重试" % (r.start_time, len(r.steps_done), r.steps_total))
    if not new_done:
        last = new_runs[-1]
        if last.thread_ended and not last.complete:
            fails.append("补跑的收盘流水线没有完成就结束了")
            out.append("  ❌ 补跑的收盘流水线（目标日 %s）没有完成就结束（%d/%d 步）" % (
                target, len(last.steps_done), last.steps_total))
            return _finish(out, fails, pending)
        out.append("  … 补跑的收盘流水线（目标日 %s）还在跑：已完成 %d/%s 步" % (
            target, len(last.steps_done), last.steps_total or "?"))
        return _finish(out, fails, pending + ["补跑收盘流水线"])
    new = new_done[0]
    out.append("  ✅ 补跑收盘流水线 目标日 %s：%d/%d 步完成（%s–%s）" % (
        target, len(new.steps_done), new.steps_total, new.start_time[11:], new.end_time[11:] or "?"))

    old_done = [r for r in runs if r.target == target and r.complete and r.start_seq < start.seq]
    if not old_done:
        if any(r.target == target and r.start_seq < start.seq for r in runs):
            out.append("  ❌ 换版前目标日 %s 的流水线没跑完（被这次重启打断或失败），没有可比的基准；"
                       "以后在⑬完成后再换版" % target)
            return Report(4 if not fails else 1, out + ["结论：没有可比的基准，无法比对"])
        out.append("  ❌ 找不到换版前目标日 %s 完成的那次流水线：把那天的 QMT 日志也复制过来再核对" % target)
        return Report(4 if not fails else 1, out + ["结论：缺换版前的日志，无法比对"])
    old = old_done[-1]
    old_rel = ""
    for e, m in [(e, STARTED.match(e.payload)) for e in entries if e.seq < old.start_seq]:
        if m:
            old_rel = m.group(2)
    out.append("对比换版前那次（%s 开始%s）：" % (old.start_time, "，release %s" % old_rel if old_rel else ""))
    def split(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
        strict = [x for x in lines if not ACCOUNT_RELATED.search(x) and not FEATURE_ROWS.search(x)]
        return strict, [x for x in lines if ACCOUNT_RELATED.search(x)], [x for x in lines if FEATURE_ROWS.search(x)]

    old_res, old_res_acct, _ = split(old.results)
    new_res, new_res_acct, _ = split(new.results)
    old_blk, old_blk_acct, old_rows = split(old.block)
    new_blk, new_blk_acct, new_rows = split(new.block)
    for label, a, b in (("14 步名单", old.steps_done, new.steps_done),
                        ("⑬停手判定", [old.stop_decision], [new.stop_decision]),
                        ("各步结果行", old_res, new_res),
                        ("候选产物统计（信号就绪审计到明日因子检查）", old_blk, new_blk)):
        if not a or a == [""]:
            fails.append("换版前缺%s" % label)
            out.append("  ❌ %s：换版前那次没有这一项" % label)
        elif a == b:
            shown = "：%s" % a[0] if label == "⑬停手判定" else "（%d 行）" % len(a)
            out.append("  ✅ %s 一致%s" % (label, shown))
        else:
            fails.append("%s不一致" % label)
            out.append("  ❌ %s 不一致：" % label)
            out.extend("      " + d for d in diff_lines(a, b)[:40])

    notes: list[str] = []
    if old_res_acct + old_blk_acct != new_res_acct + new_blk_acct:
        notes.append("  ⚠️ 和持仓有关的说明行不同（换版前后持仓或运行方式不同时正常；结论看下面的计划框）：")
        notes.extend("      " + d for d in diff_lines(old_res_acct + old_blk_acct, new_res_acct + new_blk_acct)[:20])
    if old_rows != new_rows:
        notes.append("  ℹ️ 特征表行数不同（重启后只补最近几天）：%s" % "；".join(
            "%s → %s" % (a.split(", 行数: ")[-1], b.split(", 行数: ")[-1]) for a, b in zip(old_rows, new_rows)))
    if old.broadcast and not new.broadcast:
        notes.append("  ℹ️ 换版前那次是正常收盘，打印了明日候选播报（%d 行）；补跑时账户门禁未过，不打印" % len(old.broadcast))
    elif old.broadcast != new.broadcast:
        notes.append("  ⚠️ 明日候选播报不同：")
        notes.extend("      " + d for d in diff_lines(old.broadcast, new.broadcast)[:20])

    boxes = find_boxes(entries)
    dates = None
    old_box = next((b for b in reversed(boxes) if b.seq < start.seq and b.dates and b.dates[0] == target), None)
    if old_box:
        dates = old_box.dates
    new_box = next((b for b in boxes if new.end_seq is not None and b.seq > new.end_seq
                    and (next_start is None or b.seq < next_start.seq) and b.dates == dates), None) if dates else None
    intraday = "09:15:00" <= start.qmt_time[11:19] < "15:00:00"
    if old_box is None:
        out.append("  ⚠️ 换版前没有信号日 %s 的计划框，计划框不比对" % target)
    elif new_box is None:
        out.append("  … 补跑后的第一个计划框还没打印（每 30 分钟一次）")
        pending.append("补跑后的计划框")
    elif intraday:
        out.append("  ⚠️ 盘中换版，计划框随成交变化，只列出不比对（换版前 %s / 换版后 %s）" % (old_box.time, new_box.time))
    elif old_box.lines[1:] == new_box.lines[1:]:
        out.append("  ✅ 计划框一致（信号日 %s，操作日 %s；换版前 %s / 换版后 %s，%d 行）" % (
            dates[0], dates[1], old_box.time[11:], new_box.time[11:], len(new_box.lines)))
    else:
        fails.append("计划框不一致")
        out.append("  ❌ 计划框不一致（换版前 %s / 换版后 %s）：" % (old_box.time, new_box.time))
        out.extend("      " + d for d in diff_lines(old_box.lines[1:], new_box.lines[1:])[:40])

    out.extend(notes)
    until = new_box.seq if new_box is not None else None
    errors = [log_msg(e.payload) for e in after if (until is None or e.seq < until)
              and (log_msg(e.payload) or ("", ""))[1].startswith("ERROR |")]
    if errors:
        out.append("启动到补跑后第一个计划框之间的 ERROR 行（%d 行，看一眼是否和换版有关）：" % len(errors))
        out.extend("  %s %s" % (ts[11:], msg[:160]) for ts, msg in errors[:20])
    return _finish(out, fails, pending)


def _finish(out: list[str], fails: list[str], pending: list[str]) -> Report:
    if fails:
        return Report(1, out + ["结论：❌ 不通过（%s）" % "；".join(fails)])
    if pending:
        return Report(3, out + ["结论：还没出结果（等：%s）" % "、".join(pending)])
    return Report(0, out + ["结论：✅ 通过，换版前后一致"])


def default_logs(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("XtClient_FormulaOutput_*.log"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QMT 换版核对：补跑的收盘流水线和换版前逐项对比（只读日志副本）")
    ap.add_argument("--logs", nargs="+", type=Path, help="QMT 日志文件（默认 reports/qmt_native/ 下全部）")
    ap.add_argument("--release", help="核对这个发布号的那次启动（默认最近一次）")
    ap.add_argument("--watch", type=int, default=0, metavar="分钟", help="还没出结果时每 60 秒重读，最多等这么多分钟")
    args = ap.parse_args(argv)
    deadline = time.time() + args.watch * 60
    while True:
        paths = args.logs or default_logs(DEFAULT_LOG_DIR)
        if not paths:
            print("没有日志文件：先按切换上线手册附录把 QMT 日志复制到 reports/qmt_native/")
            return 4
        report = check(read_entries(list(paths)), args.release)
        if report.code != 3 or time.time() >= deadline:
            print("\n".join(report.lines))
            return report.code
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
