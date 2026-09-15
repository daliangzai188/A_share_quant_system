#!/usr/bin/env python3
"""把 QMT 内置执行端预置为 live，但保持人工停机硬闸不变。

本脚本不启动交易 daemon，不清除 .manual_stop.json，也不调用任何券商交易接口。
执行完成后，仍需用户显式运行 start_windows.py 才会恢复原自动化程序。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qmt_inner.protocol import atomic_json, config_path, load_settings, lock_owner
from scripts.prepare_qmt_inner import install_bundle
from scripts.show_trading_status import pid_running


def stage(project_root: Path = ROOT, settings_path: Path | None = None) -> dict:
    if os.name != "nt" and settings_path is None:
        raise RuntimeError("请在目标 Windows 运行；测试时必须显式传入私有配置路径")
    project_root = Path(project_root).resolve()
    marker = project_root / ".manual_stop.json"
    if not marker.exists():
        raise RuntimeError("人工停机标记不存在，拒绝预置 live；请先运行 stop_windows.py")
    for filename, label in ((".daemon_pid", "交易主程序"), (".keeper_pid", "守护程序")):
        running, pid = pid_running(project_root / filename)
        if running:
            raise RuntimeError(f"{label}仍在运行（PID {pid}），请先完成停止")

    path = Path(settings_path) if settings_path else config_path()
    cfg = load_settings(path)
    expected_runtime = (project_root / "config" / "config.json").resolve()
    if Path(cfg["runtime_config"]).resolve() != expected_runtime:
        raise RuntimeError("私有配置指向其他项目，拒绝修改")

    spool = Path(cfg["spool_dir"])
    spool.mkdir(parents=True, exist_ok=True)
    with (spool / "owner.lock").open("a+b") as owner:
        try:
            lock_owner(owner)
        except OSError as exc:
            raise RuntimeError("QMT内置模型仍在运行，请先在模型交易界面停止模型") from exc

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"config_before_live_{stamp}.json"
        shutil.copy2(path, backup_path)

        install_bundle(project_root, path.parent)
        updated = dict(cfg)
        updated.update(
            mode="live",
            manual_stop_path=str(marker),
            # 0 表示不增加迁移层固定金额上限；原项目的单票仓位、总仓位、
            # 资金、流动性、重复委托、T+1及退出风控继续完整生效。
            max_order_notional=0,
        )
        atomic_json(path, updated)

    verified = load_settings(path)
    if verified.get("mode") != "live" or float(verified.get("max_order_notional", -1)) != 0:
        raise RuntimeError("live 预置写入后校验失败")
    if Path(verified.get("manual_stop_path", "")).resolve() != marker:
        raise RuntimeError("人工停机硬闸路径校验失败")
    if not marker.exists():
        raise RuntimeError("预置过程中人工停机标记意外消失")
    return {
        "status": "LIVE_STAGED_MANUAL_STOP_ACTIVE",
        "mode": "live",
        "manual_stop": True,
        "max_order_notional": 0,
        "config": str(path),
        "backup": str(backup_path),
        "entry": str(path.parent / "A_SYSTEM_QMT_INNER.py"),
    }


def main() -> None:
    result = stage()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("预置完成：人工停机仍生效。请重新启动 QMT 内置模型并先运行查看交易状态.cmd。")


if __name__ == "__main__":
    main()
