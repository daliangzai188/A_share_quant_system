"""只读验收通过后持久化接口选择；保持人工停机和内置只读模式。"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values, set_key
from scripts.prepare_qmt_inner import probe
from scripts.qmt_inner_audit import audit
from qmt_inner.protocol import config_path, load_settings
from src.runtime_stop_state import load_manual_stop


def main():
    if os.name != 'nt':
        raise RuntimeError('请在目标 Windows 运行')
    if load_manual_stop(ROOT) is None:
        raise RuntimeError('必须先用 stop_windows.py 停止旧程序并保持人工停机标记')
    settings = load_settings()
    result = probe()
    if settings['mode'] != 'read_only' or result['mode'] != 'read_only':
        raise RuntimeError('迁移选择步骤要求内置模型保持只读')
    check = audit()
    if check['requires_reconciliation']:
        raise RuntimeError('账户仍有待对账项目，禁止持久化切换')
    env = ROOT / '.env'
    if not env.is_file():
        raise RuntimeError('缺少原项目 .env，禁止创建空白替代配置')
    original = dotenv_values(env)
    backup = config_path().parent / 'backups' / datetime.now().strftime('transport_%Y%m%d_%H%M%S')
    backup.mkdir(parents=True)
    shutil.copy2(env, backup / 'original.env')
    set_key(str(env), 'QMT_TRANSPORT', 'qmt_inner', quote_mode='never')
    expected = dict(original, QMT_TRANSPORT='qmt_inner')
    if dict(dotenv_values(env)) != expected:
        shutil.copy2(backup / 'original.env', env)
        raise RuntimeError('接口配置写入核验失败，已恢复原 .env')
    report = dict(status='TRANSPORT_SELECTED_READ_ONLY', transport='qmt_inner',
                  inner_mode=settings['mode'], manual_stop_preserved=load_manual_stop(ROOT) is not None,
                  config_backup=str(backup), financial_calls=False)
    output = ROOT / 'reports/qmt_inner_migration/transport_selection.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
