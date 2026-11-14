#!/usr/bin/env python3
"""迁移运行账本：停止旧系统后导出，网络传输后验证；不连接券商、不恢复或启动交易。"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import uuid

DB_SUFFIXES = {'.sqlite3', '.sqlite', '.db'}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def safe_relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value:
        raise ValueError('清单包含不安全路径')
    return Path(*p.parts)


def sqlite_check(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite 完整性检查失败：' + Path(path).name)
        counts = {}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = conn.execute('SELECT COUNT(*) FROM ' + quoted).fetchone()[0]
        return counts


def copy_sqlite(source, target):
    # mode=rw 不创建缺失源库；query_only 禁止业务写入。SQLite 可完成 WAL 连接级恢复。
    src = sqlite3.connect(source.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    dst = sqlite3.connect(target, timeout=10)
    deadline = __import__('time').monotonic() + 30
    try:
        src.execute('PRAGMA query_only=ON')
        def progress(_status, _remaining, _total):
            if __import__('time').monotonic() > deadline:
                raise TimeoutError('SQLite 备份超过 30 秒；请检查源库是否仍在写入')
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        dst.commit()
        # 输出为独立数据库；网络搬运时不依赖额外的 WAL/SHM 文件。
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    return sqlite_check(target)


def copy_regular(source, target):
    before = sha256(source)
    shutil.copy2(source, target)
    if before != sha256(target) or before != sha256(source):
        raise RuntimeError('导出期间源文件发生变化：' + source.name)
    if source.suffix == '.json':
        json.loads(target.read_text(encoding='utf-8-sig'))
    return before


@contextmanager
def model_lock(spool):
    """与现有 QMT 内置引擎使用同一 owner.lock，拒绝在模型运行时导出。"""
    path = Path(spool) / 'owner.lock'
    if not path.exists():
        raise RuntimeError('未找到旧内置引擎 owner.lock；拒绝假定模型已停止')
    with path.open('r+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def assert_windows_stopped(root):
    if os.name != 'nt':
        raise RuntimeError('export 必须在旧 Windows 运行；verify 可在 Mac 或 Windows 运行')
    if not (root / '.manual_stop.json').is_file():
        raise RuntimeError('请先运行项目 stop_windows.py，并停止 QMT 内置模型')
    # 不输出进程命令行；即使 PID 文件缺失，也拒绝仍存活的业务进程。
    script = """
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$rows = @(Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match 'trading_daemon[.]py|win_daemon_keeper[.]py|monitor_strategy_d_intraday[.]py|start_windows[.]py|start_qmt_inner_windows[.]py|ensure_windows_runtime[.]py'
} | Where-Object { $_.Name -notmatch '^powershell|^pwsh' } | Select-Object ProcessId,Name)
ConvertTo-Json -InputObject $rows -Compress
"""
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    if result.returncode:
        raise RuntimeError('无法确认 Windows 业务进程已停止；拒绝导出')
    rows = json.loads(result.stdout.strip() or '[]')
    if rows:
        raise RuntimeError('仍有业务进程运行；请先停机后重试。进程数量：' + str(len(rows)))


def source_files(root, private_config):
    """按正式备份清单收集必需文件，并补齐全部小型运行状态和私有执行日志。"""
    cfg = json.loads((root / 'config/runtime_state_backup.json').read_text(encoding='utf-8-sig'))
    result = {}
    for item in cfg['items']:
        rel = safe_relative(item['path'])
        source = root / rel
        if not source.is_file():
            if item.get('required'):
                raise FileNotFoundError('缺少正式备份清单的必需文件：' + str(rel))
            continue
        result['project/' + rel.as_posix()] = source
    for directory in ('config', 'data/state', 'data/processed',
                      'reports/execution_tracking', 'reports/account_risk_shadow',
                      'reports/equity_curve_stop'):
        for source in (root / directory).rglob('*'):
            if not source.is_file() or source.suffix not in DB_SUFFIXES | {'.json', '.csv'}:
                continue
            if '.sync-conflict-' in source.name:
                continue
            # data/processed 只补齐 JSON 运行状态；历史 CSV 随项目正常同步。
            if directory == 'data/processed' and source.suffix == '.csv':
                continue
            result['project/' + source.relative_to(root).as_posix()] = source
    marker = root / '.manual_stop.json'
    if not marker.is_file():
        raise RuntimeError('缺少停机标记')
    result['project/.manual_stop.json'] = marker
    journal = Path(private_config['spool_dir']) / 'journal.sqlite3'
    if not journal.is_file():
        raise FileNotFoundError('缺少旧 QMT 私有执行账本 journal.sqlite3')
    result['private_qmt/journal.sqlite3'] = journal
    # 请求/响应仅用于核对未知委托，放入 evidence；不得投放到新引擎活动目录。
    for directory in ('requests', 'responses'):
        for source in (journal.parent / directory).glob('*.json'):
            result['evidence/old_spool/' + directory + '/' + source.name] = source
    for source in result.values():
        allowed = inside(source, root) or inside(source, journal.parent)
        if source.is_symlink() or not allowed:
            raise ValueError('源文件指向受控目录之外；拒绝导出')
    return result


def export_snapshot(root, private_config, destination):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if inside(destination, root) or inside(destination, Path(private_config['spool_dir'])):
        raise ValueError('导出目录必须位于项目同步目录和 QMT spool 目录之外')
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
    target = destination / identifier
    partial = destination / ('.' + identifier + '.incomplete')
    partial.mkdir(parents=True, exist_ok=False)
    try:
        with model_lock(private_config['spool_dir']):
            sources = source_files(root, private_config)
            before = {key: sha256(src) for key, src in sources.items() if src.suffix not in DB_SUFFIXES}
            records = []
            for relative, source in sorted(sources.items()):
                output = partial / safe_relative(relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                counts = copy_sqlite(source, output) if source.suffix in DB_SUFFIXES else None
                if counts is None:
                    copy_regular(source, output)
                records.append(dict(path=relative, bytes=output.stat().st_size,
                                    sha256=sha256(output), sqlite_table_counts=counts))
            if before != {key: sha256(sources[key]) for key in before}:
                raise RuntimeError('跨文件导出期间运行状态发生变化；暂停旧 Windows 项目同步后重试')
            manifest = dict(schema_version=1, status='PASS', kind='STOPPED_RUNTIME_MIGRATION',
                            created_at_utc=datetime.now(timezone.utc).isoformat(), files=records,
                            excludes=['.env', 'private config token', 'PID', 'owner lock', 'heartbeat'],
                            note='新内置端重新部署并生成新 Token；evidence 不得恢复为活动请求。')
            (partial / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            verify_snapshot(partial)
            partial.rename(target)
        return target, len(records)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def verify_snapshot(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('status') != 'PASS' or not manifest.get('files'):
        raise ValueError('迁移清单格式或状态不正确')
    seen = set()
    for item in manifest['files']:
        path = directory / safe_relative(item['path'])
        if item['path'] in seen or path.is_symlink() or not inside(path, directory):
            raise ValueError('清单存在重复或不安全路径')
        seen.add(item['path'])
        if not path.is_file() or path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            raise RuntimeError('迁移文件缺失或哈希不一致：' + item['path'])
        if item.get('sqlite_table_counts') is not None:
            if sqlite_check(path) != item['sqlite_table_counts']:
                raise RuntimeError('SQLite 表记录数不一致：' + item['path'])
        elif path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8-sig'))
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='在旧 Windows 停机后导出')
    export.add_argument('--project', type=Path, default=Path(r'C:\A_System'))
    export.add_argument('--private-config', type=Path)
    export.add_argument('--output', type=Path)
    verify = commands.add_parser('verify', help='只验证导出包，不写生产目录')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'verify':
        print(json.dumps(dict(status='VERIFY_PASS', files=verify_snapshot(args.directory)), ensure_ascii=False))
        return
    assert_windows_stopped(args.project)
    local = Path(os.environ['LOCALAPPDATA']) / 'A_System'
    private_path = args.private_config or local / 'qmt_inner/config.json'
    cfg = json.loads(private_path.read_text(encoding='utf-8-sig'))
    if not cfg.get('spool_dir'):
        raise ValueError('私有配置缺少 spool_dir')
    path, count = export_snapshot(args.project, cfg, args.output or local / 'migration_exports')
    print(json.dumps(dict(status='EXPORT_PASS', directory=str(path), files=count,
                         credentials_policy='env_and_private_token_excluded', trading_started=False), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('迁移未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
