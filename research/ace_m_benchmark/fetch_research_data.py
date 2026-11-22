# coding: utf-8
"""Read-only research fetch (2026-11 strategy re-evaluation): download market data that the Mac
research copy lacks and upload it to the Mac receiver. Writes only to %TEMP%; never touches
C:\\A_System or QMT. Tushare token from C:\\A_System\\.env (TUSHARE_TOKEN), never printed.

  per open day in [START, END]: daily, daily_basic, adj_factor, limit_list_d
  once: stock_basic (L/D/P), namechange (history of names -> ST periods), trade_cal

Usage: py -3.11 fetch_research_data.py START END     e.g. 20261009 20261110

2026-11-23 移入 git：11-11 在虚拟机上跑过一次（取 20261009~20261110，就是基准用的 fetched_20261110）；原文件 11-22 随
回收站清空，从 11-11 的会话记录原样恢复，之后没有在虚拟机上重新跑过。上传前要先在 Mac 上临时启动接收服务
（qmt_native/receiver.py，复制到同步目录外再启动，用完停掉），见 research/ace_m_benchmark/README.md。
"""
import io, os, sys, time, zipfile, urllib.request
import tushare as ts

start, end = sys.argv[1], sys.argv[2]
token = ''
for line in open(r'C:\A_System\.env', encoding='utf-8-sig'):
    key, sep, value = line.strip().partition('=')
    if sep and key.strip() == 'TUSHARE_TOKEN':
        token = value.strip().strip('"').strip("'")
if not token:
    sys.exit('TUSHARE_TOKEN missing in C:\\A_System\\.env')
pro = ts.pro_api(token)


def call(fn, **kw):
    for attempt in range(5):
        try:
            return getattr(pro, fn)(**kw)
        except Exception as exc:
            print('%s %s retry %d: %s' % (fn, kw, attempt + 1, str(exc)[:120]), flush=True)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError('%s failed %s' % (fn, kw))


buf = io.BytesIO()
log = []
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
    cal = call('trade_cal', exchange='SSE', start_date='20180101', end_date='20271231')
    z.writestr('trade_cal.csv', cal.to_csv(index=False))
    days = sorted(cal.loc[(cal['is_open'] == 1) & (cal['cal_date'] >= start) & (cal['cal_date'] <= end), 'cal_date'])
    for day in days:
        for fn, folder in (('daily', 'daily'), ('daily_basic', 'daily_basic'), ('adj_factor', 'adj_factor'),
                           ('limit_list_d', 'limit_list_d')):
            frame = call(fn, trade_date=day)
            z.writestr('%s/%s.csv' % (folder, day), frame.to_csv(index=False))
            log.append('%s %s %d' % (folder, day, len(frame)))
            time.sleep(0.35)
        print('day %s done' % day, flush=True)
    basics = [call('stock_basic', exchange='', list_status=s,
                   fields='ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date')
              for s in ('L', 'D', 'P')]
    import pandas as pd
    basic = pd.concat(basics, ignore_index=True)
    z.writestr('stock_basic_all.csv', basic.to_csv(index=False))
    # name history per stock in batches (namechange without ts_code is capped per call)
    codes = sorted(basic['ts_code'])
    parts = []
    for i in range(0, len(codes), 100):
        frame = call('namechange', ts_code=','.join(codes[i:i + 100]),
                     fields='ts_code,name,start_date,end_date,ann_date,change_reason')
        parts.append(frame)
        time.sleep(0.35)
    names = pd.concat(parts, ignore_index=True).drop_duplicates()
    z.writestr('namechange.csv', names.to_csv(index=False))
    log.append('stock_basic %d namechange %d' % (len(basic), len(names)))
    z.writestr('_fetch_log.txt', '\n'.join(log))
req = urllib.request.Request('http://172.16.136.1:8765/upload/research_data_%s_%s.zip' % (start, end),
                             data=buf.getvalue(), method='PUT')
urllib.request.urlopen(req, timeout=600).read()
print('uploaded research_data_%s_%s.zip (%d days, %d bytes)' % (start, end, len(days), len(buf.getvalue())))
