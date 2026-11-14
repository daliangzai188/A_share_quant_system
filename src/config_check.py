"""生产配置的启动校验（2026-11-14 项目优化第5步）。

config/config.json 只放生产代码在读的段；研究用的段在 config/research/research.json（研究代码用
research/research_config.py 读，读到的是两份合起来的配置）。

守护进程启动时先过这道门（src/daemon/scheduler.py 的 wait_for_config_gate）：config.json 的结构必须和
config/production_config_schema.json 记下的一致——段和键一个不少、类型不变（数字不分整数小数，记为 any 的键不查），
trade_mode 必须是已知的模式。不通过就不启动任何交易线程（fail-closed），推送后每分钟复查。
多出来的段和键、卖出POV交接时刻写错（代码本来就退回14:53）不拦，只在日志里提示。

改生产配置的结构（加段、加键、删键、改类型）时，同一次改动里重新生成结构文件并一起提交：
    python -m src.config_check --write-schema
只改取值不用重新生成。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).absolute().parents[1]
CONFIG_PATH = Path("config") / "config.json"
SCHEMA_PATH = Path("config") / "production_config_schema.json"
TRADE_MODES = ("paper", "simulation", "dry_run", "research", "live")


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


def _compare(schema: dict[str, Any], config: Any, path: str, problems: list[str], extras: list[str]) -> None:
    if not isinstance(config, dict):
        problems.append("%s 应为对象，实际是 %s" % (path or "config.json", _type_name(config)))
        return
    for key, expected in schema.items():
        where = "%s.%s" % (path, key) if path else key
        if key not in config:
            problems.append("缺少 %s" % where)
            continue
        value = config[key]
        if isinstance(expected, dict):
            _compare(expected, value, where, problems, extras)
        elif expected != "any" and _type_name(value) != expected:
            problems.append("%s 应为 %s，实际是 %s" % (where, expected, _type_name(value)))
    for key in config:
        if key not in schema:
            extras.append("%s.%s" % (path, key) if path else key)


def check_config(config: Any, schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    """返回 (不通过的问题, 只需提示的事项)。问题为空才算通过。"""
    problems: list[str] = []
    extras: list[str] = []
    _compare(schema, config, "", problems, extras)
    notes = ["多出来的 %s（生产代码不读；研究用的放 config/research/research.json）" % item for item in extras]
    if isinstance(config, dict):
        mode = config.get("trade_mode")
        if isinstance(mode, str) and mode.strip().lower() not in TRADE_MODES:
            problems.append("trade_mode=%r 不是 %s 之一" % (mode, "/".join(TRADE_MODES)))
        live_trade = config.get("live_trade")
        handoff = live_trade.get("exit_pov_handoff_time") if isinstance(live_trade, dict) else None
        if isinstance(handoff, str):
            try:
                datetime.time.fromisoformat(handoff.strip())
            except ValueError:
                notes.append("live_trade.exit_pov_handoff_time=%r 不是 HH:MM:SS，卖出POV按14:53交接" % handoff)
    return problems, notes


def check_production_config(root: Path = PROJECT_ROOT) -> tuple[list[str], list[str]]:
    """读 config.json 和结构文件并检查；读不了也算不通过。"""
    try:
        config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ["config/config.json 读不了：%s" % exc], []
    try:
        schema = json.loads((root / SCHEMA_PATH).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ["%s 读不了：%s" % (SCHEMA_PATH.as_posix(), exc)], []
    return check_config(config, schema)


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
