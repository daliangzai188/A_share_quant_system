# QMT 客户端自动恢复

本次新增四个完整脚本，独立于策略与交易 daemon：

- `scripts/qmt_client_guard.py`：`choose_action` 决定恢复动作；`run_guard` 每30秒观察，进程退出时拉起完整 QMT，登录失败按60/180/600/1800秒退避；`install` 注册用户登录和08:00触发的常驻任务。单例锁、密码错误停试及重试时间跨重启保留。
- `scripts/qmt_login_controls.ps1`：检查指定安装目录的客户端和登录控件；支持 UI Automation 时直接填写。国金当前版本 Qt 不暴露控件时，交给原生后端验证。
- `scripts/qmt_win32_login.py`：`NativeLogin.login` 验证进程路径、前台、桌面、输入框边框、公开字符0显示为密码掩码之后，才输入本机凭据；`decrypt_credentials` 解密当前 Windows 用户的凭据。仅支持数字账号和数字密码。
- `scripts/configure_qmt_credentials.ps1`：本机弹窗；由用户点击“加密保存”后，通过 DPAPI 保存到 `%LOCALAPPDATA%\A_System\qmt_client_recovery\credentials.json`。密码不写入同步目录、命令参数或日志；脚本不保存密码框截图。

未替换或删除原策略、交易 daemon、旧 keeper 代码。新增原因：原 `win_daemon_keeper.py` 仅恢复 Python daemon，不恢复 QMT 登录。

安装核验中修正了新脚本的两处兼容问题：`qmt_client_guard.py` 的 `install` 在同名任务不存在时直接继续首次安装；`qmt_login_controls.ps1` 的原生后端版本识别兼容 Windows 返回的 `2,1,19,0`，同时保留点号版本格式。

安装（在 Windows 使用现有 Python）：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" C:\A_System\scripts\qmt_client_guard.py --install --project C:\A_System
```

第一次配置凭据（只在本机弹窗填写）：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" C:\A_System\scripts\qmt_client_guard.py --configure
```

检查和停用：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" C:\A_System\scripts\qmt_client_guard.py --status
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" C:\A_System\scripts\qmt_client_guard.py --disable
```

验证：任务 `A_System_QmtClientGuard` 应 Running，私有 `status.json` 时间应持续推进。`client_launched_unverified` 和 `login_submitted_unverified` 仅表示尝试；`client_online` 还要求本次客户端启动之后最近会话日志明确 status=12。账户和模型最终可用性仍由现有业务程序核验，进程或本地端口不能作为账户在线证明。

当前客户端已在验证码页时，状态为 `waiting_captcha`；验证码必须用户完成。锁屏无法自动输入密码。主窗口存在但券商离线时等待终端自身重连，不按未知原因强杀客户端。人工停机标记存在则暂停拉起和认证。服务端故障无法通过自动重启保证恢复。

测试在同步目录外执行：14项状态机、日志会话隔离、掩码拒绝与字段变更测试通过；这些不是 Windows 登录实测。Windows 安装及运行结果如下。

恢复后如需验证实盘接口，先小资金验证。


2026-10-10 Windows 实机核验（北京时间）：

| 核验项目 | 实际结果 |
|---|---|
| 安装命令 | `installed: true`，任务名 `A_System_QmtClientGuard` |
| 任务计划程序 | `Running` |
| 状态更新时间 | 02:00:12 → 02:01:57 → 02:06:03 → 02:07:13，守护 PID 10312 |
| 四个安装脚本 | Windows 私有安装目录、同步项目目录、外部验证副本的 SHA-256 全部一致 |
| 两个 PowerShell 脚本 | Windows 解析错误均为 0 |
| QMT 控件检查 | 2,1,19,0；识别原生后端；当前有验证码 |
| 本机凭据弹窗 | 已实际打开，等待用户填写并点击“加密保存” |
| 目前客户端状态 | `waiting_captcha`，QMT PID 9268，`broker_online: false` |
| 自动登录实测 | 尚未完成；截至 02:07:13，本机凭据文件尚未生成 |

结论：后台守护已经启用并持续运行；自动拉起和登录重试逻辑已部署。仍需在本机保存凭据并完成当前验证码，才能继续验证真实登录。以上证据不能代表客户端已经恢复在线。核验过程中只检查凭据文件是否存在，没有读取其内容。
