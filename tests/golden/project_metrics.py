#!/usr/bin/env python3
"""项目优化的验收指标（2026-11-14，优化第1步“安全网”）：一条命令算出验收表上的数字，每一步交付都附前后对比。

“生产代码”= 从实盘入口出发、按导入关系（含函数内的延迟导入、按名字调起的脚本）能走到的全部模块：
QMT里的守护进程和它调起的收盘流水线脚本、虚拟机QMT外的检查和转发程序、Windows/Mac上的启动停止和提醒程序。
只解析代码，不运行任何东西，几秒钟算完。

目录约定（2026-11-14 第2步起）：src/、scripts/ 只放生产代码；研究代码在 research/，运维工具在 tools/；
研究和工具可以导入生产代码，生产代码不得导入研究和工具（tests/test_production_modules.py 检查）。

用法：python tests/golden/project_metrics.py            打印验收指标
      python tests/golden/project_metrics.py --modules  打印生产代码清单（tests/golden/expected/production_modules.txt 的内容）
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = (
    "scripts.trading_daemon",                 # QMT里的守护进程（收盘流水线脚本由它按名字调起）
    "qmt_inner.engine", "qmt_inner.protocol",  # QMT里的交易引擎（和守护进程一起打进QMT包）
    "phase0.qmt_external_checks", "phase0.qmt_notify_forwarder", "phase0.qn_state_sink",   # 虚拟机QMT外
    "start_windows", "stop_windows", "setup_windows",
    "scripts.show_trading_status", "scripts.ensure_windows_runtime", "scripts.qmt_client_guard",
    "scripts.open_sync_status", "scripts.install_mac_vm_watchdog", "scripts.mac_monthly_maintenance_reminder",
)
RESEARCH_PREFIXES = ("research", "optimize", "search", "analyze", "compare", "stress", "backtest", "audit",
                     "validate", "certify")
PRODUCTION_DIRS = ("src", "scripts")
NON_PRODUCTION_DIRS = ("research", "tools")
PACKAGE_INITS = ("src", "src.utils", "qmt_inner", "research", "tools")  # 包本身，不算模块


def module_files(root: Path) -> dict[str, Path]:
    files = {}
    for folder in ("src", "scripts", "research", "tools", "qmt_inner"):
        for path in (root / folder).rglob("*.py"):
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            files[".".join(parts)] = path
    for path in root.glob("*.py"):
        files[path.stem] = path
    for path in (root / "qmt_native_phase0").glob("*.py"):
        files["phase0." + path.stem] = path
    return files


def import_edges(files: dict[str, Path], *, strings: bool = True) -> dict[str, set[str]]:
    """模块之间的依赖。strings=True 时把代码里写着的模块名/脚本文件名也算上（按名字调起的脚本），
    用来圈定生产代码（宁多勿少）；strings=False 只算真正的 import，用来数反向依赖和循环依赖。"""
    edges: dict[str, set[str]] = {name: set() for name in files}
    for name, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets.append(node.module)
                targets += [node.module + "." + alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                targets += [alias.name for alias in node.names]
            elif strings and isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value.startswith(("scripts.", "src.")):
                    targets.append(value)
                elif value.endswith(".py") and "/" not in value.replace("scripts/", ""):
                    targets.append("scripts." + Path(value).stem)
            for target in targets:
                if target in files and target != name:
                    edges[name].add(target)
    return edges


def closure(edges: dict[str, set[str]], roots) -> set[str]:
    seen, stack = set(), [r for r in roots if r in edges]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(edges[name])
    return seen


def cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    index, low, on_stack, stack, found, counter = {}, {}, set(), [], [], [0]
    sys.setrecursionlimit(100000)

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for v in edges:
        if v not in index:
            visit(v)
    return found


def production_modules(root: Path = REPO) -> list[str]:
    files = module_files(root)
    return sorted(closure(import_edges(files), ROOTS))


def boundary(root: Path = REPO) -> dict[str, list[str]]:
    """目录边界：src/、scripts/ 里不在生产链上的模块；生产链走到 research/、tools/ 的模块。两项都应为空。"""
    files = module_files(root)
    prod = closure(import_edges(files), ROOTS)
    in_production_dirs = sorted(m for m in files if m.split(".")[0] in PRODUCTION_DIRS and m not in prod
                                and m not in PACKAGE_INITS)
    production_outside = sorted(m for m in prod if m.split(".")[0] in NON_PRODUCTION_DIRS)
    return {"non_production_in_production_dirs": in_production_dirs, "production_reaching_research_or_tools": production_outside}


def metrics(root: Path = REPO) -> dict:
    files = module_files(root)
    edges = import_edges(files)
    imports = import_edges(files, strings=False)
    prod = closure(edges, ROOTS)
    prod_scripts = [m for m in prod if m.startswith("scripts.")]
    research_in_prod = sorted(m for m in prod_scripts if m.split(".", 1)[1].split("_")[0] in RESEARCH_PREFIXES)
    reverse = sorted("%s→%s" % (m, t) for m in prod if m.startswith("src.") for t in imports[m] if t.startswith("scripts."))
    prod_cycles = [c for c in cycles(imports) if any(m in prod for m in c)]
    long_funcs, swallowed, biggest = 0, 0, (0, "")
    for name in prod:
        path = files[name]
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n")
        if lines > biggest[0]:
            biggest = (lines, str(path.relative_to(root)))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 > 200:
                long_funcs += 1
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                swallowed += 1
    prod_text = "\n".join(files[m].read_text(encoding="utf-8", errors="replace") for m in prod)
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    used = [k for k in config if ('"%s"' % k) in prod_text or ("'%s'" % k) in prod_text]
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "data"], capture_output=True, text=True,
                                 timeout=60).stdout.split()
        data_in_git = len(tracked)
    except (OSError, subprocess.SubprocessError):
        data_in_git = None
    vm_copies = sum(1 for p in (root / "qmt_native_phase0" / "qmt_external_checks.py",
                                root / "qmt_native" / "vm_scripts" / "qmt_external_checks.py") if p.exists())
    edge_rules = boundary(root)
    return {
        "生产代码模块数": len(prod),
        "src/和scripts/里的非生产模块": len(edge_rules["non_production_in_production_dirs"]),
        "生产代码导入research/tools": len(edge_rules["production_reaching_research_or_tools"]),
        "生产代码依赖的研究/验证类脚本": len(research_in_prod),
        "src反向依赖scripts（生产代码，真实导入）": len(reverse),
        "循环依赖组（含生产模块，真实导入）": len(prod_cycles),
        "最大的生产文件（行）": "%d（%s）" % biggest,
        "生产代码里超过200行的函数": long_funcs,
        "生产代码里捕获错误后直接pass": swallowed,
        "config.json段数（生产在读/全部）": "%d/%d" % (len(used), len(config)),
        "git跟踪的行情数据文件": data_in_git,
        "主仓库里的虚拟机检查脚本份数": vm_copies,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modules", action="store_true")
    args = parser.parse_args()
    if args.modules:
        print("\n".join(production_modules()))
        return 0
    for key, value in metrics().items():
        print("%s：%s" % (key, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
