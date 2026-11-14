"""从本机只读 QMT 配置重建 .env 并打开填写凭据；不启动交易或重置账本。"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def configure(root, private_file, qmt_path=None):
    from dotenv import dotenv_values, set_key

    root = Path(root).resolve()
    private_file = Path(private_file).resolve()
    private_dir = private_file.parent
    settings = json.loads(private_file.read_text(encoding='utf-8-sig'))
    account_id = str(settings.get('account_id', '')).strip()
    if not account_id or '\n' in account_id or '\r' in account_id:
        raise RuntimeError('Existing local QMT account configuration is invalid')
    if settings.get('mode') != 'read_only':
        raise RuntimeError('New-machine setup requires the QMT model to remain read_only')
    env = root / '.env'
    stop = root / '.manual_stop.json'
    protected = [stop, root / 'config/config.json', root / 'config/strategy_config.json', private_file]
    before = {str(p): fingerprint(p) for p in protected}
    if fingerprint(stop) is None:
        raise RuntimeError('Keep the manual stop marker during configuration')
    current = dict(dotenv_values(env, encoding='utf-8-sig')) if env.exists() else {}
    expected_account = str(current.get('QMT_ACCOUNT_ID') or '').strip()
    if expected_account and expected_account not in {account_id, 'your_qmt_account_id_here'}:
        raise RuntimeError('Existing .env uses a different QMT account; no change made')
    original_env = fingerprint(env)
    if qmt_path is not None:
        qmt_path = Path(qmt_path).resolve()
        if not qmt_path.is_dir() or qmt_path.name not in {'userdata', 'userdata_mini'}:
            raise RuntimeError('QMT_PATH must be an existing QMT userdata directory')

    folder = private_dir / 'new_machine_setup' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    candidate = folder / '.env.candidate'
    if env.exists():
        shutil.copy2(env, folder / 'previous.env')
        shutil.copy2(env, candidate)
    else:
        candidate.write_text(
            '# 新电脑配置：在下一行等号右侧粘贴 Tushare Pro Token，然后按 Ctrl+S。\n'
            '# 密钥只保存在本机，不要发到聊天里。\n'
            'TUSHARE_TOKEN=\n\n'
            '# 可选：Bark 手机通知地址；不需要通知可留空。\n'
            'BARK_URL=\n'
            'LOG_LEVEL=INFO\n\n'
            '# QMT 内置接口已按本机现有私有配置填写。\n'
            '# 本步骤保持人工停机和只读模式，不启动交易。\n', encoding='utf-8')
    set_key(str(candidate), 'QMT_TRANSPORT', 'qmt_inner', quote_mode='never')
    set_key(str(candidate), 'QMT_ACCOUNT_ID', account_id, quote_mode='always')
    set_key(str(candidate), 'QMT_ACCOUNT_TYPE', 'STOCK', quote_mode='never')
    set_key(str(candidate), 'QMT_INNER_CONFIG', str(private_file), quote_mode='always')
    changed_keys = {'QMT_TRANSPORT', 'QMT_ACCOUNT_ID', 'QMT_ACCOUNT_TYPE', 'QMT_INNER_CONFIG'}
    if qmt_path is not None:
        set_key(str(candidate), 'QMT_PATH', qmt_path.as_posix(), quote_mode='always')
        changed_keys.add('QMT_PATH')
    parsed = dict(dotenv_values(candidate))
    for key, value in current.items():
        if key not in changed_keys and parsed.get(key) != value:
            raise RuntimeError('Existing environment setting would be lost: ' + key)
    if parsed.get('QMT_ACCOUNT_ID') != account_id or parsed.get('QMT_TRANSPORT') != 'qmt_inner':
        raise RuntimeError('Candidate environment validation failed')
    if {str(p): fingerprint(p) for p in protected} != before:
        raise RuntimeError('Protected settings changed during preparation; .env not installed')
    if fingerprint(env) != original_env:
        raise RuntimeError('.env changed concurrently; no change made')
    with candidate.open('rb') as source:
        if not env.exists():
            with env.open('xb') as target:
                shutil.copyfileobj(source, target)
        else:
            temporary = root / '.env.new_machine_setup_tmp'
            with temporary.open('xb') as target:
                shutil.copyfileobj(source, target)
            os.replace(temporary, env)
    actual = dict(dotenv_values(env))
    assert actual == parsed
    assert {str(p): fingerprint(p) for p in protected} == before
    token = str(actual.get('TUSHARE_TOKEN') or '').strip()
    report = {
        'status': 'NEW_MACHINE_ENV_CREATED' if not current else 'NEW_MACHINE_ENV_CONFIGURED',
        'env_path': str(env), 'transport': actual['QMT_TRANSPORT'],
        'qmt_account_matches_local_private_config': actual['QMT_ACCOUNT_ID'] == account_id,
        'tushare_token_configured': bool(token and token != 'your_tushare_pro_token_here'),
        'manual_stop_preserved': True, 'qmt_mode': settings['mode'],
        'protected_files_unchanged': True, 'business_process_started': False,
        'runtime_ledgers_created_or_reset': False,
        'credential_values_printed': False,
        'candidate_and_backup_directory': str(folder),
    }
    (folder / 'setup_report.json').write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-config', type=Path)
    parser.add_argument('--qmt-path', type=Path, help='已安装客户端的 userdata 或 userdata_mini 目录')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Run this setup on the new Windows machine')
    private = args.private_config or Path(os.environ['LOCALAPPDATA']) / 'A_System/qmt_inner/config.json'
    report = configure(args.project, private, args.qmt_path)
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    if not args.no_open:
        subprocess.Popen(['notepad.exe', str(args.project / '.env')])
    print('NEXT: Fill TUSHARE_TOKEN and BARK_URL in .env and save. Do not paste credentials into chat.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
