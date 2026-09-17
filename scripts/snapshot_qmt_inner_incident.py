"""只读故障快照：备份原交易状态及内置发单日志，不连接券商、不改账。"""
from __future__ import annotations

from contextlib import closing
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qmt_inner.protocol import config_path, load_settings
from scripts.qmt_inner_backup import backup


def snapshot() -> dict:
    if os.name != 'nt':
        raise RuntimeError('请在目标 Windows 运行')
    cfg_path = config_path()
    settings = load_settings(cfg_path)
    if Path(settings['runtime_config']).resolve() != (ROOT / 'config/config.json').resolve():
        raise RuntimeError('内置配置指向其他项目，拒绝混合备份')
    result = backup()
    target = Path(result['directory']) / 'inner'
    target.mkdir()
    shutil.copy2(cfg_path, target / 'config.json')
    journal = Path(settings['spool_dir']) / 'journal.sqlite3'
    with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(target / 'journal.sqlite3')) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('内置发单日志备份完整性检查失败')
    hashes = {}
    for label, path in [('source_engine', ROOT / 'qmt_inner/engine.py'),
                        ('installed_engine', cfg_path.parent / 'bundle/qmt_inner/engine.py'),
                        ('daemon', ROOT / 'scripts/trading_daemon.py')]:
        hashes[label] = hashlib.sha256(path.read_bytes()).hexdigest()
    class Memory(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ('physical', 'available_physical', 'commit_limit', 'available_commit',
             'virtual', 'available_virtual', 'extended')]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise RuntimeError('无法获取 Windows 内存状态')
    result.update(status='INCIDENT_SNAPSHOT_OK', inner_journal_backed_up=True,
                  captured_utc=datetime.now(timezone.utc).isoformat(),
                  hashes=hashes, engine_bundle_matches_source=hashes['source_engine'] == hashes['installed_engine'],
                  memory_load_pct=memory.load, available_physical_mb=memory.available_physical // 1048576,
                  commit_limit_mb=memory.commit_limit // 1048576,
                  available_commit_mb=memory.available_commit // 1048576)
    output = ROOT / 'reports/incident_20260917_qmt_order_converter/windows_snapshot.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    print(json.dumps(snapshot(), ensure_ascii=True))
