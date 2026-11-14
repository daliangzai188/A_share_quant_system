"""迁移前的一致状态备份；不改持仓、不连接券商、不输出账号或密钥。"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.qmt_single_owner import _pid_alive


def backup(root=ROOT, destination=None, require_stopped=False):
    running = []
    for name in ('.daemon_pid', '.keeper_pid', 'logs/strategy_d_monitor.pid'):
        path = root / name
        if path.exists():
            value = path.read_text(encoding='utf-8').strip()
            if value.isdigit() and _pid_alive(int(value)):
                running.append(name)
    if require_stopped and running:
        raise RuntimeError('Backup requires stopped processes: ' + ', '.join(running))
    if destination is None:
        if os.name != 'nt':
            raise RuntimeError('Windows only unless a test destination is provided')
        destination = Path(os.environ['LOCALAPPDATA']) / 'A_System/qmt_inner/backups'
    target = Path(destination) / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    target.mkdir(parents=True)
    files = set()
    for directory in ('config', 'data/state'):
        base = root / directory
        if base.exists():
            files.update(p for p in base.rglob('*') if p.is_file()
                         and p.suffix in ('.json', '.sqlite3', '.sqlite', '.db'))
    files.update(p for p in (root / 'data/processed').glob('*position*.json'))
    for name in ('.manual_stop.json', '.daemon_pid', '.keeper_pid'):
        if (root / name).exists():
            files.add(root / name)
    manifest = dict(created=datetime.now().isoformat(), running=running,
                    stopped_snapshot=not running, files=[])
    for source in sorted(files):
        relative = source.relative_to(root)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix in ('.sqlite3', '.sqlite', '.db'):
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(output)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Backup integrity check failed: ' + str(relative))
        else:
            shutil.copy2(source, output)
        manifest['files'].append(dict(path=str(relative), size=output.stat().st_size,
                                     sha256=hashlib.sha256(output.read_bytes()).hexdigest()))
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return dict(status='BACKUP_OK', directory=str(target), files=len(files),
                stopped_snapshot=not running, running=running)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-stopped', action='store_true')
    args = parser.parse_args()
    print(json.dumps(backup(require_stopped=args.require_stopped), ensure_ascii=True))
