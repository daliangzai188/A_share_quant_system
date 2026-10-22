# coding: utf-8
"""The production notifier inside the bundle: a Bark request becomes one marked, ASCII-only
line in QMT's log, with title/body/level intact; any other URL stays blocked.

Usage: python test_notify_outbox.py CORE
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from urllib.parse import unquote, urlsplit, parse_qs
except ImportError:
    pass


def main():
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', sys.argv[1]):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    for rel, text in ns['QN_STATIC_FILES'].items():
        ns['QN_VFS'].put_text(rel, text)
    ns['QN_ENVIRON']['BARK_URL'] = ns['QN_NOTIFY_BASE']
    ns['QN_ENVIRON'].pop('A_SYSTEM_DISABLE_NOTIFICATIONS', None)
    ns['QN_NOTIFY_MODE'][0] = 'live'
    loader = ns['QnLoader'](ns['QnCodeBase'](ns['QN_MODULES']))
    notify = loader.load('src.notify')
    ok1 = notify.notify('daily_summary', u'\U0001F4CA 今日收盘汇总', u'持仓0笔，明日计划已生成。', level='timeSensitive')
    ok2 = notify.notify('sell_fail', u'卖出失败', u'600000.SH 平仓未成交', call=True)
    lines = [l for l in ns['QN_PRINT_LINES'] if l.startswith('[A_SYSTEM_NOTIFY ')]
    print('returned', ok1, ok2, 'lines', len(lines))
    good = ok1 and ok2 and len(lines) == 2
    for line in lines:
        good &= all(ord(c) < 128 for c in line) and ' ' not in line.split(' ', 3)[3]
        path = line.split(' ', 3)[3]
        parts = urlsplit(path)
        title, body = [unquote(p) for p in parts.path.strip('/').split('/', 1)]
        print(' ', line[:60], '...', '| title=%s body=%s query=%s' % (title, body, parse_qs(parts.query)))
    try:
        loader.load('urllib.request') if False else notify.urllib.request.urlopen('https://example.com/x', timeout=3)
        good = False
        print('other URL was NOT blocked')
    except OSError as exc:
        print('other URL blocked:', type(exc).__name__)
    print('RESULT', 'PASS' if good else 'FAIL')


if __name__ == '__main__':
    main()
