# 2026-09-15 Windows 夜间重启与恢复断点

## 已核实事实（北京时间）

- 01:58:53：旧 daemon PID 6808 最后一次心跳。
- 01:59:19：System 1074，`MoUsoCoreWorker.exe` 发起计划内 Service Pack 重启。
- 02:00:17：System 1074，`TrustedInstaller.exe` 发起计划内操作系统升级重启。
- VMware 日志记录两次 guest 主动 reset；VM 进程一直存在，早盘 Windows 停在密码登录页。
- 09:12:29：完成 Windows 登录后，`A_System_RuntimeGuard` 自动触发。
- 09:12:46 / 09:12:51：keeper PID 6528、daemon PID 7344 分别启动。
- daemon 的交易会话门禁因桌面会话尚未就绪而持续阻断，09:15:10 已发连接故障通知。
- 现场运行 `ensure_windows_runtime.py` 确认两个进程存活，未重启健康进程。

Windows 审计数据：`reports/runtime/windows_power_events_latest.json`，生成于
2026-09-15 09:20:35。两条 1074、两条 6006、两条 6005，查询区间内无 41/6008。
启动事件时间早于重启事件，与启动过程中时钟校正可能有关；不使用其排序推断精确启动耗时。

## 自动恢复为什么没有完成

1. Mac 哨兵只调用 `vmrun list`。Windows 重启后 VM 仍在列表中，于是直接返回正常。
2. 原 Desktop 心跳哨兵已因 macOS 目录权限问题退役，替代版没有继续检查交易心跳。
3. Windows 登录触发任务需要用户会话；没有完成密码登录时无法恢复桌面交易环境。
4. 登录任务只恢复 daemon/keeper；进程存在不代表桌面交易会话已经就绪。

## 本次已完成的 Mac 修复

文件：`scripts/mac_vm_watchdog.py`

- 新增 `_heartbeat_status`，通过本机 Syncthing 只读索引核对本地
  `logs/daemon_heartbeat.txt` 修改时间；不直接访问 Desktop。
- 新增 `_check_runtime`，分别记录 VM 运行和交易心跳状态。心跳超过 15 分钟、
  索引不存在、查询失败、异常未来时间均不判正常，按一小时去重发送告警。
- `main` 的 VM 已运行分支继续检查交易心跳，替换原先直接返回正常的行为。
- 修复 `_notify` 中未编码的中文 URL 参数；原请求会在发送前因编码失败被异常分支吞掉。
- 兼容 Windows/Syncthing 的 7~9 位时间小数及 macOS 自带 Python 3.9。

文件：`scripts/install_mac_vm_watchdog.py`

- 新增 `_syncthing_heartbeat_config`，识别已有本机 Syncthing 项目目录。
- 安装器把 API 配置保存在 Application Support 下权限 0600 的本机文件，
  不把凭据写入仓库，不保存 Windows/券商密码。
- 已重新安装到 `com.asystem.vm-watchdog`，仍每 5 分钟检查一次。

验证：8 项新增看门狗测试与 7 项现有 Windows 恢复测试通过；
现场 launchd 状态已经包含 `runtime_status` 和真实心跳时间。

复现/验证命令：

```sh
/usr/bin/python3 -m unittest tests.test_mac_vm_watchdog tests.test_runtime_recovery_tools -v
/usr/bin/python3 scripts/install_mac_vm_watchdog.py
```

## Windows 更新脚本的两个缺陷与修复

文件：`scripts/manage_windows_automatic_updates.ps1`

- Windows PowerShell 5.1 对无 BOM 的中文脚本按本地代码页解码，现场第46行解析失败。
  已补 UTF-8 BOM 和编码注释；Windows 现场已成功解析并执行。
- `Set-DwordPolicy` 每次调用都执行 `New-Item -Force`，Registry Provider 重建已有键，
  先写的 `NoAutoUpdate`、`AUOptions` 被后续调用清除。现场曾出现“成功”字样，
  读回却只有 `NoAutoRebootWithLoggedOnUsers=1`。已改为只在键不存在时创建。
- 主流程新增成功前断言：三项策略和服务状态全部读回正确，才输出成功。
- 最终读回：`NoAutoUpdate=1`、`AUOptions=2`、`NoAutoRebootWithLoggedOnUsers=1`，
  `wuauserv=Stopped/Disabled`。

## 防自动锁屏、睡眠及退出的现场验收

文件：`scripts/configure_windows_session_stability.ps1`（新增，UTF-8 BOM）

- 主流程保存修改前原值，关闭当前用户屏保、屏保锁屏、闲置超时锁屏和动态锁屏；
  通过系统 API 读回当前会话屏保关闭状态。
- 交流/电池两组电源设置均关闭自动关屏、睡眠、休眠、无人值守唤醒后睡眠和唤醒密码提示。
- 同一入口运行修复后的更新脚本，统一核验；`Get-RegistryState`、`Invoke-PowerConfig`
  提供不受中文显示名称影响的读回。`-VerifyOnly` 不改系统设置。
- 2026-09-15 09:34:44 只读复核 `PASS`：8项注册表设置、5组电源设置、更新状态及
  当前会话屏保API均通过，无失败项。
- Mac交流/电池两种模式本来已是 `sleep=0`、`displaysleep=0`，无需修改宿主电源设置。
- 09:35现场查进程：daemon PID 7344、keeper PID 6528 均仍是09:12启动的原进程。
  保活仍每30秒检查；本次设置修复没有重启交易程序。
- 09:37补充复核：运行兜底任务为 `Ready`，最近运行结果 `0`，下次08:15；
  CBS `RebootPending` 和 Windows Update `RebootRequired` 两项待重启标记均不存在。
- `install_windows_runtime_guard.py` 同时管理 `A_System_SessionStabilityGuard`：用户登录时及
  每日07:50以最高权限重新应用并验收会话稳定设置，任务只修改系统设置，不启停交易程序。
  `--status` 现在要求运行兜底和会话稳定两个任务都存在，缺任一项都会触发重新安装。
- 09:42手动触发新增计划任务做首次验收，任务生成的新报告仍为 `PASS`，证明计划任务权限、
  脚本路径和执行链路均可用；报告明确记录 `trading_processes_restarted=false`。

Windows管理员 PowerShell 运行方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\A_System\scripts\configure_windows_session_stability.ps1
# 后续只读复核：
powershell -NoProfile -ExecutionPolicy Bypass -File C:\A_System\scripts\configure_windows_session_stability.ps1 -VerifyOnly
```

验收报告：`reports/runtime/windows_session_stability_latest.json`。
第一次修改前备份：`reports/runtime/session_policy_before_20260915_093155.json`。
本次补齐完整脚本、注释、现场读回验证与回归测试后提交Git。

## 范围与限制

- 本次没有修改策略、仓位、下单规则或实盘开关。
- 心跳恢复只说明进程继续工作，桌面交易会话是否就绪仍由程序门禁检查。
- 不配置免密码登录，不把 Windows 或券商密码持久化到脚本。
- 本次已在客户机读取确认更新和会话策略；后续系统大版本升级或管理员修改仍可能覆盖设置，
  可以用 `-VerifyOnly` 检查。人工关机、断电及操作系统崩溃不属于闲置锁屏设置可消除的故障。
