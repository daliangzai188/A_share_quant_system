# coding: utf-8
"""Extract the VM research fetch into its own folder (only expected CSV paths, no path traversal).
  python extract_fetch.py ZIP OUT_DIR

2026-11-23 移入 git：原文件 11-22 随回收站清空，从 11-11 的会话记录原样恢复。"""
import re, sys, zipfile
from pathlib import Path

zp, out = sys.argv[1], Path(sys.argv[2])
ok = re.compile(r'^(?:(?:daily|daily_basic|adj_factor|limit_list_d)/\d{8}\.csv|index_daily/[0-9A-Z.]+\.csv|'
                r'trade_cal\.csv|stock_basic_all\.csv|namechange\.csv|_fetch_log\.txt)$')
n = 0
with zipfile.ZipFile(zp) as z:
    for info in z.infolist():
        if not ok.match(info.filename):
            print('skip', info.filename)
            continue
        p = out / info.filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(z.read(info))
        n += 1
print('extracted', n)
