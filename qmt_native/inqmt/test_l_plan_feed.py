# coding: utf-8
"""策略 L 计划经注册表进 QMT（ACE+M+L，2026-11-26）：推送转发程序（QMT 外，Tushare 龙虎榜）把次日 L 计划写进
注册表 LPlan，宿主读出、校验后写进内存文件 reports/strategy_l/l_signals_recent.json（守护进程读的同一路径）：
  1. 格式、校验码、信号日/买入日都对 → 写进内存文件，守护进程按买入日能读到；
  2. 同一计划再读一次 → 文件不变；新信号日 → 追加，只留最近 10 个信号日；
  3. 校验码不对、日期对不上、不是 L 计划 → 不写，原文件不变。

Usage: python test_l_plan_feed.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import hashlib, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_stop_threads import load


def feed(plan, digest=None):
    body = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'v1|%s|%s|%s|%s' % (plan['signal_date'], plan['planned_buy_date'], body,
                               digest or hashlib.sha256(body.encode('utf-8')).hexdigest()[:8])


def main():
    ns = load(sys.argv[1], {})
    box = ['']
    ns['QN_L_PLAN_READER'][0] = lambda: box[0]
    host = ns['QnHost'].__new__(ns['QnHost'])
    host.log = lambda *a: None
    vfs, path = ns['QN_VFS'], ns['QN_L_PLAN_PATH']
    plan = {'signal_date': '20261126', 'planned_buy_date': '20261127', 'strategy_leg': 'L', 'status': 'SIGNAL_READY',
            'ts_code': '300001.SZ', 'name': '测试', 'l_allowed': True, 'l_scale': 1.0}
    box[0] = feed(plan)
    host._load_l_plan_feed()
    data = json.loads(vfs.get_text(path))
    ok1 = data['signals'][-1]['ts_code'] == '300001.SZ' and data['strategy_leg'] == 'L'
    print('1. valid feed written: %s' % ok1)
    before = vfs.get_text(path)
    host._load_l_plan_feed()
    same = vfs.get_text(path) == before
    plan2 = dict(plan, signal_date='20261127', planned_buy_date='20261130', ts_code='300002.SZ')
    box[0] = feed(plan2)
    host._load_l_plan_feed()
    data = json.loads(vfs.get_text(path))
    ok2 = same and [x['signal_date'] for x in data['signals']] == ['20261126', '20261127']
    print('2. same feed unchanged, new day appended: %s' % ok2)
    before = vfs.get_text(path)
    bad = [feed(plan2, digest='00000000'), feed(dict(plan2, strategy_leg='M')),
           'v1|20261127|20261201|' + feed(plan2).split('|', 3)[3]]
    for b in bad:
        box[0] = b
        host._load_l_plan_feed()
    ok3 = vfs.get_text(path) == before
    print('3. bad checksum / not L / wrong dates rejected: %s' % ok3)
    print('RESULT %s' % ('PASS' if ok1 and ok2 and ok3 else 'FAIL'))
    return 0 if ok1 and ok2 and ok3 else 1


if __name__ == '__main__':
    sys.exit(main())
