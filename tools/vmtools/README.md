# vmtools：Claude 从 Mac 操作虚拟机 QMT 界面的工具

2026-11-22 从 `~/A_System_Rebuild/2026-10-19/vmtools/` 移入 git（用户要求：可能影响实盘交易的东西都要在 git 里）。
只放脚本和源码；截图、日志、`vmctl` 编译产物不进 git。

| 文件 | 作用 |
|---|---|
| `vmctl.swift` | 输入助手源码：点击、拖动、按键、打字（只能打 ASCII）。`sh build_vmctl.sh` 编译出同目录的 `vmctl` |
| `idle.sh` | 打印 Mac 距上次键盘鼠标操作的秒数；用户在用电脑时不操作虚拟机 |
| `locked.sh` | Mac 锁屏时返回失败 |
| `vmfront.sh` | 把 VMware Fusion 调到最前并确认，失败就不能点击打字 |
| `front.sh` | 只检查：没锁屏且 VMware 在最前 |
| `shot.sh 名字` | 截 VMware 窗口，存到 `~/A_System_vm_shots/`（可用 `VMTOOLS_SHOT_DIR` 改），不进仓库 |
| `batch_step.sh`、`batch_loop.sh`、`run_tick_batches.sh` | 2026-11-11~12 在 QMT 草稿编辑器里批量跑 `qmt_native_phase0/PROBE_TICK_QUEUE_*.py` 探针（只读研究用），坐标写死，用前先核对 |

坐标换算、组合键写法、QMT 按钮位置等注意事项见 `qmt_native/切换上线手册_QMT原生版.md` 末尾附录。
