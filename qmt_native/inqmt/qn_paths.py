# coding: utf-8
"""本目录测试用到的主仓库路径（2026-11-15 起虚拟机脚本只在主仓库 qmt_native_phase0/ 放一份，本目录不再有 vm_scripts/）。

A_SYSTEM_REPO 指定主仓库（要测导出副本时设成它），默认同步工作区。
"""
import os

A_SYSTEM_REPO = os.environ.get('A_SYSTEM_REPO', '/Users/eass/Desktop/A_System')
VM_SCRIPTS = os.path.join(A_SYSTEM_REPO, 'qmt_native_phase0')
