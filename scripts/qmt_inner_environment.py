"""只读检查 Windows/QMT 环境；不连接账户、不输出密码或 Token。"""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_qmt_inner import prepare


def main():
    if os.name != 'nt':
        raise RuntimeError('Windows only')
    command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Process | Where-Object {$_.Path -match 'QMT|XtMini|XtIt|python'} | Select-Object ProcessName,Id,Path,Responding,CPU,WorkingSet64 | ConvertTo-Json"
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=20)
    processes = json.loads(result.stdout or '[]')
    qmt_dirs = {str(Path(p['Path']).parent) for p in processes if p.get('Path') and 'qmt' in p['Path'].lower()}
    executables = {d: [p.name for p in Path(d).glob('*.exe')] for d in qmt_dirs}
    embedded = []
    for directory in qmt_dirs:
        executable = Path(directory) / 'python.exe'
        if executable.exists():
            check = subprocess.run([str(executable), '-c',
                'import sys,sqlite3,hmac,pathlib; print(sys.version); print("STDLIB_OK")'],
                capture_output=True, encoding='utf-8', errors='replace', timeout=20)
            embedded.append(dict(executable=str(executable), exit_code=check.returncode, output=check.stdout, error=check.stderr))
        elif '--check-embedded' in sys.argv and (Path(directory) / 'pythonw.exe').exists():
            # pythonw has no console. Record only runtime/import information to a file.
            target = ROOT / 'reports/qmt_inner_migration/embedded_python.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}', encoding='utf-8')
            bundle = Path(os.environ['LOCALAPPDATA']) / 'A_System/qmt_inner/bundle'
            code = ('import sys,json,sqlite3,hmac,pathlib; sys.path.insert(0,' + repr(str(bundle)) + '); '
                    'import qmt_inner.engine; pathlib.Path(' + repr(str(target)) + ').write_text('
                    'json.dumps(dict(python=sys.version,sqlite=sqlite3.sqlite_version,engine_import=True)),encoding="utf-8")')
            check = subprocess.run([str(Path(directory)/'pythonw.exe'), '-c', code],
                                    cwd=directory, capture_output=True, timeout=30)
            embedded.append(dict(executable=str(Path(directory)/'pythonw.exe'), exit_code=check.returncode,
                                  output=json.loads(target.read_text(encoding='utf-8')) if target.exists() else None))
    report = dict(embedded_python=embedded, qmt_executables=executables, project_root=str(ROOT), python=sys.version, executable=sys.executable,
                  machine=platform.machine(), processes_output=result.stdout,
                  process_check_exit=result.returncode, deployment=prepare())
    report['python_library_files'] = {directory: [dict(name=p.name, directory=p.is_dir(),
            size=p.stat().st_size if p.is_file() else None)
            for p in Path(directory).iterdir()
            if p.name.lower().startswith(('python', 'lib', 'dll')) or p.suffix.lower() in ('.zip', '.7z')]
            for directory in qmt_dirs}
    if '--diagnose' in sys.argv:
        # OS crash metadata only: no trading logs, environment values or passwords.
        crash_command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001; StartTime=(Get-Date).AddMinutes(-45)} -ErrorAction SilentlyContinue | Select-Object -First 8 TimeCreated,ProviderName,Message | ConvertTo-Json"
        crash = subprocess.run(['powershell', '-NoProfile', '-Command', crash_command],
                               capture_output=True, encoding='utf-8', errors='replace', timeout=20)
        report['recent_application_crashes'] = crash.stdout
    path = ROOT / 'reports/qmt_inner_migration/windows_environment.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('READ_ONLY_PREPARATION_OK: ' + str(path))
    for directory, names in executables.items():
        print('QMT_DIRECTORY: ' + directory)
        print('CLIENTS: ' + ', '.join(n for n in names if n.lower().startswith(('xt', 'python'))))
    print('ENTRY: ' + report['deployment']['entry'])
    for check in embedded:
        print('EMBEDDED_PYTHON: ' + str(check))
    if '--open-client' in sys.argv:
        candidates = [Path(d)/'XtItClient.exe' for d in qmt_dirs if (Path(d)/'XtItClient.exe').exists()]
        if len(candidates) != 1:
            raise RuntimeError('Cannot uniquely locate full QMT client')
        subprocess.Popen([str(candidates[0])], cwd=str(candidates[0].parent))

if __name__ == '__main__':
    main()
