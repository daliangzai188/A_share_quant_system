"""迁移接口回归入口：仅运行模拟券商测试，不连接真实券商。"""
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    os.chdir(ROOT)
    names = ['tests.test_qmt_inner', 'tests.test_qmt_inner_deployment',
             'tests.test_trade_recovery', 'tests.test_broker_execution_service',
             'tests.test_account_privacy', 'tests.test_broker_maintenance_recovery']
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    report = dict(platform=platform.system(), python=platform.python_version(),
                  tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), success=result.wasSuccessful(), modules=names,
                  output=output.getvalue())
    directory = ROOT / 'reports/qmt_inner_migration'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('tests_' + platform.system().lower() + '.json')
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('MIGRATION_TESTS ' + ('PASS' if result.wasSuccessful() else 'FAIL') +
          ': tests=' + str(result.testsRun) + ', failures=' + str(len(result.failures)) +
          ', errors=' + str(len(result.errors)))
    print('REPORT: ' + str(target))
    if not result.wasSuccessful():
        print(output.getvalue())
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
