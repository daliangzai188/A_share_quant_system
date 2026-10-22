import sys, os, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
ns = {'__name__': 'qn_core'}
for name in ('qn_runtime.py', sys.argv[1]):
    exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
for rel, text in ns['QN_STATIC_FILES'].items():
    ns['QN_VFS'].put_text(rel, text)
ns['QN_VFS'].put_bytes('data/raw/trade_calendar.csv', open('/tmp/qn_cal.csv', 'rb').read())
loader = ns['QnLoader'](ns['QN_MODULES'])
bad = {}
for name in sorted(ns['QN_MODULES']):
    loader.reset([name])
    try:
        loader.load(name)
    except BaseException as exc:
        tb = traceback.extract_tb(sys.exc_info()[2])
        where = [f for f in tb if f[0].startswith('/A_System')]
        loc = ('%s:%s' % (where[-1][0].replace('/A_System/', ''), where[-1][1])) if where else ''
        bad[name] = '%s: %s @ %s' % (type(exc).__name__, str(exc)[:150], loc)
for k, v in bad.items():
    print('%-50s %s' % (k, v))
print(sys.version.split()[0], 'modules', len(ns['QN_MODULES']), 'import failures', len(bad))
