"""生产配置的启动校验（2026-11-14 项目优化第5步）。

config/config.json 只放生产代码在读的段；研究用的段在 config/research/research.json（研究代码用
research/research_config.py 读，读到的是两份合起来的配置）。

守护进程启动时先过这道门（src/daemon/scheduler.py 的 wait_for_config_gate）：config.json 的结构要和
config/production_config_schema.json 记下的一致——段和键一个不少、类型不变（数字不分整数小数，记为 any 的键不查），
trade_mode 必须是已知的模式。问题分两档（2026-11-15）：
- 致命：配置或结构文件读不了、trade_mode 不认识、券商连接（broker、broker_adapter_enabled、qmt_enabled、trade_mode）
  的结构不对。不启动任何交易线程（fail-closed），推送后每分钟复查；
- 其它结构问题：只禁止开新仓（统一执行通道拒绝一切 BUY，new_entry_block_reason），账户恢复、启动平仓检查、收盘看门狗、
  卖出POV照常，已有持仓能按时卖出；推送告警，配置改好后自动解除。
说明文字（note、*_note、*_description、file_role 等，以及报警消息里的说明 strategy_warnings.reference）缺了或改了类型
只提示，不算问题；多出来的段和键、卖出POV交接时刻写错（代码本来就退回14:53）也只在日志里提示。

改生产配置的结构（加段、加键、删键、改类型）时，同一次改动里重新生成结构文件并一起提交：
    python -m src.config_check --write-schema
只改取值不用重新生成。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).absolute().parents[1]
CONFIG_PATH = Path("config") / "config.json"
SCHEMA_PATH = Path("config") / "production_config_schema.json"
TRADE_MODES = ("paper", "simulation", "dry_run", "research", "live")
# 券商连接相关：这几项结构不对，系统连券商、分清模拟和实盘都靠不住，只能全部停下
FATAL_SECTIONS = ("trade_mode", "broker", "broker_adapter_enabled", "qmt_enabled")
# 说明文字：代码不读或只拿来显示，缺了不影响交易
DOC_KEYS = ("note", "notes", "file_role", "user_risk_acceptance", "selected_rule_description",
            "input_manifest_update_policy", "d_runtime_policy")
DOC_SUFFIXES = ("_note", "_notes", "_description")
DOC_SUBTREES = ("strategy_warnings.reference",)


def is_doc_key(path: str, key: str) -> bool:
    """说明文字的键：不算结构问题。"""
    return key in DOC_KEYS or key.endswith(DOC_SUFFIXES) or any(
        path == tree or path.startswith(tree + ".") for tree in DOC_SUBTREES)


def _type_name(value: Any) -> str:
    if value is None:
        return "any"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def build_schema(config: dict[str, Any]) -> dict[str, Any]:
    """按当前配置记下结构：对象往下展开，其余记类型。"""
    schema: dict[str, Any] = {}
    for key, value in config.items():
        schema[key] = build_schema(value) if isinstance(value, dict) else _type_name(value)
    return schema


def _compare(schema: dict[str, Any], config: Any, path: str, problems: list[tuple[str, str]],
             extras: list[str], docs: list[str]) -> None:
    if not isinstance(config, dict):
        problems.append((path, "%s 应为对象，实际是 %s" % (path or "config.json", _type_name(config))))
        return
    for key, expected in schema.items():
        where = "%s.%s" % (path, key) if path else key
        doc = is_doc_key(where, key)
        if key not in config:
            if doc:
                docs.append("说明文字 %s 缺少" % where)
            else:
                problems.append((where, "缺少 %s" % where))
            continue
        value = config[key]
        if isinstance(expected, dict) and not doc:
            _compare(expected, value, where, problems, extras, docs)
        elif not isinstance(expected, dict) and expected != "any" and _type_name(value) != expected:
            message = "%s 应为 %s，实际是 %s" % (where, expected, _type_name(value))
            if doc:
                docs.append("说明文字 " + message)
            else:
                problems.append((where, message))
    for key in config:
        if key not in schema:
            extras.append("%s.%s" % (path, key) if path else key)


@dataclass(frozen=True)
class ConfigStatus:
    """配置检查结果：fatal 不为空就不启动任何交易；只有 entry 不为空就只禁止开新仓。"""

    fatal: tuple[str, ...]
    entry: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def problems(self) -> list[str]:
        return list(self.fatal) + list(self.entry)


def config_status(config: Any, schema: dict[str, Any]) -> ConfigStatus:
    problems: list[tuple[str, str]] = []
    extras: list[str] = []
    docs: list[str] = []
    _compare(schema, config, "", problems, extras, docs)
    notes = ["多出来的 %s（生产代码不读；研究用的放 config/research/research.json）" % item for item in extras]
    notes += ["%s（不影响运行）" % item for item in docs]
    fatal = [message for where, message in problems if not where or where.split(".")[0] in FATAL_SECTIONS]
    entry = [message for where, message in problems if where and where.split(".")[0] not in FATAL_SECTIONS]
    if isinstance(config, dict):
        mode = config.get("trade_mode")
        if isinstance(mode, str) and mode.strip().lower() not in TRADE_MODES:
            fatal.append("trade_mode=%r 不是 %s 之一" % (mode, "/".join(TRADE_MODES)))
        live_trade = config.get("live_trade")
        handoff = live_trade.get("exit_pov_handoff_time") if isinstance(live_trade, dict) else None
        if isinstance(handoff, str):
            try:
                datetime.time.fromisoformat(handoff.strip())
            except ValueError:
                notes.append("live_trade.exit_pov_handoff_time=%r 不是 HH:MM:SS，卖出POV按14:53交接" % handoff)
    return ConfigStatus(tuple(fatal), tuple(entry), tuple(notes))


def check_config(config: Any, schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    """返回 (问题, 只需提示的事项)；问题为空才算通过（致命和只禁止开新仓的都算问题）。"""
    status = config_status(config, schema)
    return status.problems, list(status.notes)


def production_config_status(root: Path = PROJECT_ROOT) -> ConfigStatus:
    """读 config.json 和结构文件并检查；读不了算致命。"""
    try:
        config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ConfigStatus(("config/config.json 读不了：%s" % exc,), (), ())
    try:
        schema = json.loads((root / SCHEMA_PATH).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ConfigStatus(("%s 读不了：%s" % (SCHEMA_PATH.as_posix(), exc),), (), ())
    return config_status(config, schema)


def check_production_config(root: Path = PROJECT_ROOT) -> tuple[list[str], list[str]]:
    status = production_config_status(root)
    return status.problems, list(status.notes)


# ── 只禁止开新仓（2026-11-15）──────────────────────────────────────────────────
# 守护进程的配置门禁发现非致命的结构问题时设置；统一执行通道（src/broker_execution_service）对一切 BUY
# 先问这里，卖出不受影响。配置改好后，下一次询问（最多隔 ENTRY_RECHECK_SECONDS 秒复查一次）自动解除。
ENTRY_RECHECK_SECONDS = 60.0
_entry_lock = threading.Lock()
_entry_block = {"reason": "", "checked_at": 0.0, "root": None}


def block_new_entries(reason: str, root: Path = PROJECT_ROOT) -> None:
    with _entry_lock:
        _entry_block.update(reason=str(reason), checked_at=time.monotonic(), root=root)


def clear_new_entry_block() -> None:
    with _entry_lock:
        _entry_block.update(reason="", checked_at=0.0, root=None)


def entry_block_text(status: ConfigStatus) -> str:
    items = list(status.entry)
    return "；".join(items[:5]) + ("；等%d处" % len(items) if len(items) > 5 else "")


def new_entry_block_reason() -> str:
    """配置检查不通过、禁止开新仓时返回原因（空串表示可以开仓）；被禁止期间按间隔复查，修好了自动解除。"""
    with _entry_lock:
        reason, checked_at, root = _entry_block["reason"], _entry_block["checked_at"], _entry_block["root"]
    if not reason:
        return ""
    if time.monotonic() - checked_at < ENTRY_RECHECK_SECONDS:
        return reason
    status = production_config_status(root or PROJECT_ROOT)
    if not status.problems:
        clear_new_entry_block()
        return ""
    reason = "；".join(status.fatal) if status.fatal else entry_block_text(status)
    block_new_entries(reason, root or PROJECT_ROOT)
    return reason


def write_schema(root: Path = PROJECT_ROOT) -> Path:
    config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
    path = root / SCHEMA_PATH
    path.write_text(json.dumps(build_schema(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查生产配置 config/config.json；--write-schema 按当前配置重新生成结构文件")
    parser.add_argument("--write-schema", action="store_true")
    args = parser.parse_args(argv)
    if args.write_schema:
        print("已写", write_schema())
        return 0
    problems, notes = check_production_config()
    for item in notes:
        print("提示：", item)
    for item in problems:
        print("不通过：", item)
    print("通过" if not problems else "不通过（%d处）" % len(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
