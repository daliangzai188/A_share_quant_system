import subprocess, sys, os, time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
full_log = "--full-log" in sys.argv
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

root = Path(__file__).absolute().parent
log = root / "logs" / "trading_daemon.log"
pid_file = root / ".daemon_pid"
d_monitor_pid_file = root / "logs" / "strategy_d_monitor.pid"
daemon = root / "scripts" / "trading_daemon.py"
log.parent.mkdir(exist_ok=True)

def pid_exists(pid: str) -> bool:
    try:
        import ctypes

        process_id = int(pid)
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True

def terminate_process(pid: str) -> bool:
    try:
        import ctypes

        process_id = int(pid)
        kernel32 = ctypes.windll.kernel32
        access = PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(access, False, process_id)
        if not handle:
            return not pid_exists(pid)
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False

def list_descendant_pids(root_pid: str) -> list[int]:
    """用进程快照枚举 root_pid 的全部后代进程。

    纯 Windows API（CreateToolhelp32Snapshot），不启动 taskkill/PowerShell，
    毫秒级完成，不会在慢IO机器上卡顿。
    必须在杀主进程之前调用：TerminateProcess 不级联杀子进程，
    收盘流水线子进程（collect/clean等）成孤儿后会继续狂读写共享盘，
    把下次启动的 import/读配置拖到分钟级（2026-07-03 实测2.5分钟+）。
    """
    try:
        import ctypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        snap_handle = ctypes.c_void_p(snapshot)
        parent_map = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if kernel32.Process32FirstW(snap_handle, ctypes.byref(entry)):
                while True:
                    parent_map.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                    if not kernel32.Process32NextW(snap_handle, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap_handle)
        descendants: list[int] = []
        seen: set[int] = set()
        queue = [int(root_pid)]
        while queue:
            pid = queue.pop()
            for child in parent_map.get(pid, []):
                if child not in seen:
                    seen.add(child)
                    descendants.append(child)
                    queue.append(child)
        return descendants
    except Exception as exc:
        print(f"枚举 PID {root_pid} 子进程失败（继续停止主进程）：{exc}")
        return []

def wait_pid_gone(pid: str) -> bool:
    """启动前只按进程状态判断旧进程是否释放，不用固定 sleep 猜时间。"""
    start = time.monotonic()
    while True:
        if not pid_exists(pid):
            return True
        if time.monotonic() - start >= 60:
            return False
        time.sleep(0.1)

def stop_pid_file(path: Path, label: str) -> bool:
    if not path.exists():
        return False
    old_pid = path.read_text().strip()
    if not old_pid:
        path.unlink(missing_ok=True)
        return False
    if not pid_exists(old_pid):
        path.unlink(missing_ok=True)
        print(f"Old {label} pid file cleaned (PID {old_pid} not running)")
        return False
    # 杀主进程前先枚举后代：之后父子关系依然保留在快照结果里，逐个清理。
    descendants = list_descendant_pids(old_pid)
    if not terminate_process(old_pid):
        print(f"Old {label} terminate failed (PID {old_pid}); start aborted.")
        raise SystemExit(1)
    if not wait_pid_gone(old_pid):
        print(f"Old {label} still running (PID {old_pid}); start aborted to avoid duplicate daemon/QMT session.")
        raise SystemExit(1)
    for child_pid in descendants:
        child = str(child_pid)
        if not pid_exists(child):
            continue
        print(f"Stopping orphan child of {label} (PID {child}) ...")
        if not terminate_process(child) or not wait_pid_gone(child):
            print(f"Orphan child PID {child} still running; start aborted to avoid shared-disk IO contention.")
            raise SystemExit(1)
    path.unlink(missing_ok=True)
    print(f"Old {label} stopped (PID {old_pid})")
    return True

stopped_d = stop_pid_file(d_monitor_pid_file, "D monitor")
# 不再每次启动都用 PowerShell 全进程扫描孤儿 D 监控。那一步很慢，且不是状态确认。
# D 监控的正常生命周期由 pid 文件和 taskkill /T 进程树停止保证；真出现孤儿进程时再单独排查。
stopped_orphan_d = False
stopped_daemon = stop_pid_file(pid_file, "daemon process")
if stopped_d or stopped_orphan_d or stopped_daemon:
    print("Old process state verified; starting new daemon.")

log_start_pos = log.stat().st_size if log.exists() else 0

# 让 daemon 自己的 RotatingFileHandler 写日志，stdout/stderr 丢弃
proc = subprocess.Popen(
    [sys.executable, str(daemon)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
)

pid_file.write_text(str(proc.pid))
print(f"Started PID {proc.pid}")
print(f"Log: {log}")

# 等日志文件出现
for _ in range(10):
    if log.exists():
        break
    time.sleep(0.5)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED   = "\033[91m"
ORANGE = "\033[38;5;208m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


print(GREEN + BOLD + f"{timestamp()} | A_System 已启动，默认只显示关键日志；完整日志仍写入文件。" + RESET)
print(f"{timestamp()} | Ctrl+C 可立即脱离终端，daemon 会继续运行。需要终端显示全部日志可用：py -3.11 start_windows.py --full-log")


def color_for_line(text: str) -> str:
    # 开仓决策链整段（行内以┃为标记）显示橙色，优先于其他颜色规则，
    # 与普通成功/警告日志区分开，方便一眼定位明日开仓逻辑。
    if "┃" in text:
        return ORANGE
    success_words = [
        "✅",
        "QMT连接成功",
        "程序正常",
        "当日涨停池模拟观察计划已生成",
        "收盘流水线完成",
        "收盘流水线进度",
        "采集结果",
        "清洗结果",
        "动态特征",
        "增强因子",
        "A/B/C策略状态",
        "E2策略状态",
        "L策略状态",
        "计划单",
    ]
    warning_words = ["⚠️", "| WARNING |", "WARNING", "仅供参考", "暂不开仓"]
    error_words = ["❌", "| ERROR |", "ERROR", "失败", "异常"]
    if any(word in text for word in error_words):
        return RED
    if any(word in text for word in success_words):
        return GREEN
    if any(word in text for word in warning_words):
        return YELLOW
    return ""


def should_print_line(text: str) -> bool:
    """默认少刷屏，避免 Windows 终端被详细审计日志拖慢。

    详细 A/B/C 漏斗、逐层停留原因仍完整写入 logs/trading_daemon.log；
    终端只保留连接、账户、候选、计划、错误、警告和任务节点。
    """
    if full_log:
        return True
    color = color_for_line(text)
    if color:
        return True
    important_words = [
        "下次任务",
        "A_System 守护进程启动",
        "交易日历",
        "启动检查",
        "已有 ",
        "收盘数据缓存",
        "【今日候选】",
        "【明日候选】",
        "最终结果",
        "总策略模式",
        "判定：",
        "开仓计划",
        "计划买入",
        "准备下单时间",
        "准备平仓时间",
        "E2策略",
        "L/model3状态",
        "L龙头策略",
        "L龙头条件",
        "L/model3结论",
        "组合决策明细",
        "发现组合计划",
        "准备开仓",
        "持仓信息",
        "平仓",
        "撤单",
        "QMT账户连接状态",
        "QMT启动连接检查",
        "QMT启动门禁",
        "QMT快速连接尝试",
        "QMT完整连接尝试",
        "QMT非交易时段",
        "账户",
        "信号就绪审计",
        "市场环境",
        "数据口径",
        "必需字段",
        "成交概率",
        "执行:",
        "进度：已运行",
        "完成，用时",
        "日线采集进度",
        "每日基本面采集进度",
        "涨停池采集进度",
        "检查本地日线行情文件",
        "检查本地每日基本面文件",
        "检查本地涨停池文件",
        "跳过日线行情",
        "跳过每日基本面",
        "跳过涨停池",
        "请求 Tushare",
        "保存日线行情",
        "保存每日基本面",
        "保存涨停池",
        "Tushare ",
    ]
    suppressed_words = [
        "A/B/C逐层筛选漏斗",
        "未进入/停留原因",
        "0_load_base_candidates",
        "1_universe_filters",
        "2_include_conditions",
        "3_exclude_conditions",
        "4_compound_exclude_rules",
        "5_rank_and_select_first",
        "A/B/C停止点",
    ]
    if any(word in text for word in suppressed_words):
        return False
    return any(word in text for word in important_words)


try:
    with open(log, "r", encoding="utf-8", errors="replace") as f:
        f.seek(log_start_pos)
        while True:
            line = f.readline()
            if line:
                stripped = line.rstrip()
                if not should_print_line(stripped):
                    continue
                color = color_for_line(stripped)
                if color:
                    print(color + stripped + RESET, flush=True)
                else:
                    print(stripped, flush=True)
            else:
                time.sleep(0.5)
except KeyboardInterrupt:
    print(f"\n{timestamp()} | 已脱离实时日志，daemon 继续后台运行。查看完整日志：logs/trading_daemon.log", flush=True)
    os._exit(0)
