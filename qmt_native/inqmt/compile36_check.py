import sys
ns = {}
exec(compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'), ns)
bad = 0
for name, (rel, is_pkg, src) in sorted(ns['QN_MODULES'].items()):
    try:
        compile(src, rel, 'exec')
    except SyntaxError as e:
        bad += 1
        print('SYNTAX', rel, e.lineno, e.msg, (e.text or '').strip()[:160])
print(sys.version.split()[0], 'modules', len(ns['QN_MODULES']), 'syntax errors', bad)
