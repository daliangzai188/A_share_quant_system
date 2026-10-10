"""停手产物来源校验。只读本地文件，不连接券商或发送委托。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def producer_identity(root: Path, signal_date: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """代码/配置用内容哈希；历史原始输入用文件清单、大小和纳秒修改时间。

    清单用于发现补采/替换/删除，避免每30秒重读数GB历史。源文件哈希及研究池
    内容哈希另外保存在判定内。配置不含环境变量值，也不把凭据写入判定。
    """
    source = hashlib.sha256()
    paths = sorted(set(root.glob('src/**/*.py')) | set(root.glob('scripts/*.py'))
                   | set(root.glob('config/*.json')))
    for path in paths:
        source.update(str(path.relative_to(root)).encode('utf-8'))
        source.update(b'\0')
        source.update(hashlib.sha256(path.read_bytes()).digest())
    data = config.get('data', {}) or {}
    inputs: list[tuple[str, int, int]] = []
    for kind in ('daily', 'daily_basic', 'adj_factor', 'limit_list'):
        folder = root / str(data.get(kind + '_dir', 'data/raw/' + kind))
        for path in sorted(folder.glob('*.csv')):
            if path.stem <= str(signal_date):
                stat = path.stat()
                inputs.append((kind + '/' + path.name, stat.st_size, stat.st_mtime_ns))
    # 早期市场计数/行业/股票状态/日历也参与影子账。
    raw = root / 'data/raw'
    for path in sorted(raw.glob('*.csv')):
        stat = path.stat()
        inputs.append((path.name, stat.st_size, stat.st_mtime_ns))
    return {
        'schema_version': 1,
        'signal_date': str(signal_date),
        'source_sha256': source.hexdigest(),
        'raw_inventory_sha256': hashlib.sha256(
            json.dumps(inputs, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ).hexdigest(),
        'raw_inventory_file_count': len(inputs),
    }


def decision_producer_fresh(root: Path, signal_date: str, config: Mapping[str, Any],
                            payload: Mapping[str, Any]) -> bool:
    recorded = payload.get('producer_identity')
    return (payload.get('diagnostic_dataset_reused') is not True
            and isinstance(recorded, dict)
            and recorded == producer_identity(root, signal_date, config))
