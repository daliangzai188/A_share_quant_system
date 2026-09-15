#!/usr/bin/env python3
"""在 Windows 本地部署 QMT 内置执行端。默认只读，不改正式交易开关。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from qmt_inner.protocol import atomic_json, config_path, load_settings, lock_owner


def prepare(project_root=ROOT, destination=None):
    if os.name != 'nt' and destination is None:
        raise RuntimeError('请在目标 Windows 运行部署；本机测试必须显式提供临时目录')
    load_dotenv(project_root / '.env', override=False)
    path = Path(destination) / 'config.json' if destination else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.exists()
    if existing:
        cfg = load_settings(path)
    else:
        account = os.environ.get('QMT_ACCOUNT_ID', '').strip()
        if not account:
            raise RuntimeError('请先在现有 .env 配置 QMT_ACCOUNT_ID')
        cfg = dict(account_id=account, account_type='STOCK', token=secrets.token_hex(32),
                   spool_dir=str(path.parent / 'spool'), mode='read_only',
                   runtime_config=str(project_root / 'config/config.json'),
                   intent_database=str(project_root / 'data/state/execution_events.sqlite3'),
                   trade_calendar=str(project_root / 'data/raw/trade_calendar.csv'),
                   manual_stop_path=str(project_root / '.manual_stop.json'),
                   max_order_notional=1000, buy_fee_reserve_rate=0.001, minimum_fee_reserve=5, poll_interval_ms=100, request_timeout_seconds=8, query_timeout_seconds=20,
                   heartbeat_max_age_seconds=5, max_minute_subscriptions=50)
        atomic_json(path, cfg)
    spool = Path(cfg['spool_dir'])
    spool.mkdir(parents=True, exist_ok=True)
    with (spool / 'owner.lock').open('a+b') as owner:
        try:
            lock_owner(owner)
        except OSError as exc:
            raise RuntimeError('内置模型正在运行，必须先停止模型再更新执行包') from exc
        entry = install_bundle(project_root, path.parent)
    return dict(config=str(path), entry=str(entry), mode=cfg['mode'], reused_config=existing)


def install_bundle(project_root, destination):
    bundle = destination / 'bundle'
    bundle.mkdir(exist_ok=True)
    shutil.copytree(project_root / 'qmt_inner', bundle / 'qmt_inner', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    source = (project_root / 'qmt_inner/entry.py').read_text(encoding='utf-8')
    source = source.replace("PROJECT_ROOT = r'__PROJECT_ROOT__'", 'PROJECT_ROOT = ' + repr(str(bundle)))
    # The QMT strategy editor expects GBK. ASCII source + escaped paths is portable.
    source = source.replace('# coding: utf-8', '# coding: gbk', 1)
    entry = destination / 'A_SYSTEM_QMT_INNER.py'
    entry.write_bytes(source.encode('gbk'))
    return entry


def probe(settings_path=None):
    from src.qmt_inner_adapter import QMTInnerBrokerAdapter
    from src.qmt_single_owner import assert_standalone_qmt_allowed
    assert_standalone_qmt_allowed(ROOT, caller='prepare_qmt_inner --probe')
    load_dotenv(ROOT / '.env', override=False)
    adapter = QMTInnerBrokerAdapter.from_config({'inner_config_path': settings_path} if settings_path else {})
    adapter.connect()
    try:
        account = adapter.query_account()
        positions = adapter.query_positions()
        orders = adapter.query_orders()
        trades = adapter.query_trades()
        codes = [p.ts_code for p in positions]
        quotes = adapter.get_full_tick(codes) if codes else {}
        return dict(status='READ_ONLY_CONNECTED', transport='qmt_inner',
                    account_suffix=account.account_id[-2:], position_count=len(positions),
                    order_count=len(orders), trade_count=len(trades), quote_count=len(quotes),
                    mode=adapter.server_info['mode'])
    finally:
        adapter.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--config')
    args = parser.parse_args()
    if args.probe:
        result = probe(args.config)
    else:
        result = prepare()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
