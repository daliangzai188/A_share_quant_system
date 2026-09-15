"""显式启动内置 QMT 模式；只读预检成功后才交给原启动器。"""
from __future__ import annotations
import os
import runpy
from pathlib import Path


def main():
    if os.name != 'nt':
        raise RuntimeError('请在当前 Windows 虚拟机运行')
    from scripts.prepare_qmt_inner import probe
    result = probe()
    if result['mode'] != 'live':
        raise RuntimeError('内置执行端仍为 read_only。只读连接已验证；尚未进入交易运行验收。')
    os.environ['QMT_TRANSPORT'] = 'qmt_inner'
    runpy.run_path(str(Path(__file__).resolve().with_name('start_windows.py')), run_name='__main__')


if __name__ == '__main__':
    main()
