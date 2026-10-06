# 新 M 系列 Mac 重建与迁移

本流程重新创建 VMware Windows 11 ARM，通过网络同步项目，单独迁移运行账本，不传输完整虚拟机包、不使用移动硬盘。旧系统继续运行到新环境基础准备完成；最终切换必须使用旧系统停机后的最新快照。

**2026-10 重建后的配置入口**：[机器配置清单](../config/new_machine_profile.json)、[凭据模板](../.env.example)、本文第八节。
清单保存本次实际版本、资源、接口、路径、同步和防休眠设置；路径与版本是重建参考，换机时须按新机实际安装位置核对。
完整 QMT 内置通道为 `qmt_inner`，外部 Python 不需要安装 `xtquant`。不要沿旧 miniQMT 排查步骤反复换 Python 或安装 xtquant。

## 一、克隆能取得什么

```sh
git clone https://github.com/daliangzai188/A_share_quant_system.git A_System
```

| 内容 | 获取方式 | 说明 |
|---|---|---|
| `src/`、`scripts/`、`qmt_inner/`、启动与停止入口 | Git 克隆 | 完整业务代码与 QMT 内置执行包 |
| `requirements.txt`、`.env.example`、`config/` | Git 克隆 | 依赖清单、凭据模板、正式策略配置 |
| `AGENTS.md`、`docs/`、`.stignore` | Git 克隆 | 项目纪律、重建说明与同步忽略模板 |
| `scripts/export_runtime_migration.py`、配套测试 | Git 克隆 | 停机后导出与网络传输后的账本校验 |
| 真实 `.env` | 受控同步或在新机填写 | 包含账号与密钥，禁止提交 Git |
| 小型交易日历、换手率参考表、历史认证参考样本 | Git 克隆 | 下表具名参考输入；不代表已完成新机数据验收 |
| 完整原始行情、分钟数据、清洗结果 | Syncthing 同步 | Git 内占位目录不是已具备完整历史数据 |
| 持仓、执行意图、委托成交与停手状态 | 最终停机快照与核对 | 原有业务状态须连续，不能以空账本代替 |
| Windows 私有 QMT 执行账本 | 单独导出迁移 | 默认在 `%LOCALAPPDATA%\A_System\qmt_inner`，不在项目同步目录 |
| Python 环境、Windows、QMT、系统任务 | 在新机安装与配置 | 不搬运旧虚拟环境、PID、心跳或进程锁 |

**当前正式配置可能是 `live`。克隆完成不代表可以直接运行 `start_windows.py`。** 新环境先保持 QMT 内置端 `mode=read_only`，不安装会自动启动交易的计划任务。不能在新机修改正在同步的正式策略配置来试验，否则可能影响旧机。

当前运行接口以旧机实际 `.env` 为准。本次迁移使用 `QMT_TRANSPORT=qmt_inner`；`.env.example` 已与本次重建方案一致。旧环境若仍使用 miniQMT，迁移时必须明确选择接口，不能自动猜测或失败后回退。

以下小型文件原先被通用 CSV/报告忽略规则排除，干净克隆的部分研究导入和历史回归会因此缺文件；已按具名例外纳入版本控制。它们不包含真实账户、委托号或密钥，历史样本只供原有口径的复现，不是当前实盘计划。

| 文件 | 行数/范围 | 用途与限制 |
|---|---|---|
| `data/raw/trade_calendar.csv` | 2,922 行，2019-01-01 至 2026-12-31 | 研究入口所需交易日历；新机仍须检查最新交易日期 |
| `data/processed/fill_rate_table.csv` | 1,059 组 | 原有换手率分组参考表；完整数据到位后按现行流程更新 |
| `data/processed/fill_rate_fallback.csv` | 343 组 | 原有换手率备用分组表，与主表配套加载 |
| `data/raw/stock_basic/stock_basic_all.csv` | 5,856 行 | 含上市/退市状态的股票基本信息参考；历史身份仍依原有时点规则与覆盖记录 |
| `data/research/strategy_d_intraday/minute_target_manifest.csv` | 6,848 行，2024-09-26 至 2026-06-30 | D历史分钟数据采集目标清单，不包含分钟行情本体 |
| `data/research/strategy_d_intraday/mother_pool.csv` | 6,848 行，2024-09-26 至 2026-06-30 | D旧母池，用于核对完整窗口中的历史子集 |
| `data/research/strategy_d_intraday/mother_pool_full_window.csv` | 40,336 行，2024-07-01 至 2026-06-30 | D完整窗口触板母池的历史参考输入 |
| `data/research/strategy_d_intraday/minute_target_manifest_full_window.csv` | 40,336 行，同上 | D完整窗口的分钟采集目标清单，不含分钟行情本体 |
| `reports/ac_daily_candidates/ac_daily_candidates.csv` | 484 行，2024-07-01 至 2026-06-30 | A/C历史逐日参考，空仓日股票与收益字段留空 |
| `reports/strategy_expansion/abcd_expansion_selected_e2_equity_curve.csv` | 481 行，2024-05-20 至 2026-05-14 | 旧组合认证参考曲线，保持历史口径 |
| `reports/strategy_d/d_daily_candidates.csv` | 45 行，2024-09-26 至 2026-05-13 | 旧D候选历史回归样本 |
| `reports/strategy_e_samples/e_r1_daily_candidates_full.csv` | 102 行，2024-05-23 至 2026-05-12 | E旧版门禁前完整参考样本；可选题材/竞价等字段缺失，不补零 |
| `reports/current_portfolio_alignment/strict_asof_portfolio_report.md` | 历史认证报告 | 对应仓库已有严格证书，禁止把历史报告当作本次新环境认证 |

这些具名参考输入合计约 19.5 MB，检查过字段、范围与重复行，未发现完全重复行。D/E历史样本的 `limit_amount`、`score_error`、E的可选竞价/资金字段以及D母池的部分历史身份/静态属性有原始缺失；股票基本信息的未退市记录不填退市日期，A/C空仓日不填标的或收益。保持原值，不据此宣称数据完整或可以实盘。

仓库通过 `.gitattributes` 保留以上具名参考文件的原始字节，避免 Windows 克隆时自动转换换行符导致冻结哈希变化。

## 二、新 Mac 与新 Windows 基础环境

1. 检查新 Mac 的系统版本、内存与空间，安装兼容的 VMware Fusion，创建 Windows 11 ARM，安装 VMware Tools。所有操作使用普通窗口。
2. 在新 Windows 安装 Python 3.11 x64、券商官方完整 QMT 客户端；Windows 使用北京时间并同步时钟。
3. 在新 Mac 与新 Windows 各自安装 Syncthing，分别生成自己的设备 ID，不复制其他设备的 `cert.pem`、`key.pem`。
4. 旧 Mac 与新 Mac 互相添加设备，共享现有文件夹 ID `a-system`，新 Mac 指向克隆的项目目录。迁移准备期新机设为“仅接收”。
5. 新 Windows 再与新 Mac 配对，同一个 `a-system` 指向 `C:\A_System`，准备期也设为“仅接收”。同路径不要求 Mac 用户名相同。
6. `.stignore` 不会自动同步，按仓库中的模板在新 Mac 和新 Windows 各配置一份。确认文件同步状态，不把运行中的 SQLite 普通同步结果直接当作最终账本快照。

每台设备在自己的浏览器打开 `http://127.0.0.1:8384` 进行配对。旧环境关闭了全局发现和中继，开启本地发现；跨局域网时先建立可达的安全网络连接，不假定会自动跨网连接。

在新 Windows 的项目目录安装依赖：

```powershell
cd C:\A_System
py -3.11 -m pip install -r requirements.txt
```

旧版重建背景见 [虚拟机搭建手册](vm_environment_setup.md)；当前接口部署以 [QMT 内置执行迁移说明](qmt_inner_migration.md) 为准。本次不要求重新安装旧版 miniQMT 接口来代替内置执行端。

## 三、新接口先只读部署

凭据在新机安全到位后，运行：

```powershell
cd C:\A_System
py -3.11 scripts\prepare_qmt_inner.py
```

确认新私有配置 `mode=read_only`，新 Token 由部署器生成。按照 QMT 内置迁移说明创建 `A_SYSTEM_QMT_INNER` 模型，并完成只读探测。不要复制旧私有配置里的 Windows 用户路径、Token 或 `live` 模式，不运行正式交易启动器。

## 四、旧机最终停机与导出

先核对旧机真实持仓、可卖数量、未完成委托和待恢复意图。新环境准备好后，在旧 Windows 执行：

```powershell
cd C:\A_System
py -3.11 stop_windows.py
```

再停止 QMT 内置模型，确认业务进程已退出、人工停机标记存在。等项目同步收敛后暂停旧 Windows 的项目同步，新 Windows 也暂停项目同步，避免恢复期间继续收到旧数据库覆盖。

在旧 Windows 执行完整导出：

```powershell
cd C:\A_System
py -3.11 scripts\export_runtime_migration.py export --project C:\A_System
```

如果旧环境使用显式的 `QMT_INNER_CONFIG` 自定义路径，需通过 `--private-config` 指定实际旧私有配置文件。默认导出位置是 `%LOCALAPPDATA%\A_System\migration_exports` 下的时间戳子目录，位于项目同步目录之外。预期输出 `EXPORT_PASS`、实际目录和文件数。

导出包括：

- 正式 `config/runtime_state_backup.json` 清单中的必需文件，以及补充的 JSON 运行状态和执行报告；缺少必需文件即失败。
- 项目 `execution_events.sqlite3`、持仓与执行状态、账户风险与影子净值停手数据。
- 私有 `spool/journal.sqlite3`，通过 SQLite backup API 合并未落入主库的 WAL 事务，再检查完整性与各表记录数。
- 旧请求/响应证据，单独放在 `evidence/`，用于核对结果未知的委托，不能恢复成活动请求。

工具检查人工停机与业务进程，并取得与内置引擎共用的 `owner.lock`；模型仍运行时拒绝导出。普通文件导出前后核对哈希，失败时清理未完成快照。它不停止程序、不连接券商、不发送或撤销委托，也不直接恢复或启动交易。

工具不导出真实 `.env`、旧私有 Token、PID、进程锁或心跳。快照仍包含敏感运行数据，必须通过受控网络连接传输，不能提交到公开仓库。

## 五、网络传输、校验与恢复

通过网络传输整个成功导出的时间戳目录到新环境。在新 Windows 运行：

```powershell
cd C:\A_System
py -3.11 scripts\export_runtime_migration.py verify "收到的快照目录"
```

预期输出 `VERIFY_PASS`。它检查路径、文件大小、SHA-256、JSON 格式、SQLite 完整性及表记录数；失败时禁止恢复。这个工具只有导出和验证入口，没有自动覆盖生产账本的恢复入口。

先在隔离目录核对清单与账本，再按实际现场恢复：

1. `project/` 中文件对应新项目的同名路径，恢复时新项目同步和业务程序均保持停止。
2. `private_qmt/journal.sqlite3` 对应新私有配置 `spool_dir` 下的执行账本，恢复时新内置模型也必须停止。
3. 新私有配置由新环境部署器生成，保留新路径、新 Token 和只读模式；旧参数逐项核对，不直接复用旧 `live` 配置。
4. `evidence/old_spool/` 保留作证据，绝不能放入新 `spool/requests` 或 `spool/responses`，避免重新消费旧请求。
5. 不恢复 PID、旧心跳或旧锁文件，不用空账本覆盖历史意图或委托记录。

按 QMT 内置迁移说明完成只读对账，核对账户、策略分仓、可卖数量、委托、成交、待平仓计划、待恢复意图及影子净值停手判定。只读连接成功不等于交易验收成功；有差异先处理差异，不能猜测或重发未知委托。

## 六、最终切换与验收

完成模拟柜台验证后，旧交易端退出，旧机自动恢复任务及 Mac 自动启动虚拟机守护停用，旧设备退出生产同步拓扑。新拓扑收敛后，再按确认的数据方向配置同步、监控与定时任务。2026-10 当前 Mac 为 `sendonly`，Windows 接收项目并保留本机额外运行文件；不要擅自改双向或点击“还原本地更改”删除本机账本。禁止两套执行系统同时启用。

迁移保持现行 `A>C>E>D` 与方案甲规则，不扩大资金或修改策略参数。恢复实盘前先小资金验证。最终账本导出、Windows 停机门禁、恢复、客户端对账和模拟验收均须在真实新旧 Windows 现场完成；Mac 上的隔离测试不能代替这些步骤。

## 七、迁移工具隔离验证

在同步目录之外的仓库副本运行：

```sh
python3 -B -m unittest discover -s tests -p 'test_export_runtime_migration.py' -v
```

测试使用临时目录与模拟账本，覆盖 WAL 事务、私有执行账本、篡改检测、运行中模型锁、缺少必需文件、缺少停机标记、导出目录越界和路径穿越，不访问生产账本或真实券商。

## 八、下一次换机直接使用的配置流程

### 8.1 下载与基础环境

本次通过验证的组合是 Fusion 26H1u1、Windows 11 专业版 ARM64、国金完整 QMT 2.1.19.0、外部 Python 3.11 x64。
Fusion 使用 4 核、6144MB 内存、NAT 网络、VMware Tools 时间同步；Windows 使用北京时间和本地账户。
这些是已用版本，不代表未来最新版。优先从 Broadcom、微软、Python 和券商官网下载兼容版本；Broadcom 登录是下载环节，不是程序运行依赖。
若官方下载暂时不可用，备份已校验的安装包可用于重建；其他来源安装包必须匹配官方 SHA-256，并核验 VMware 签名和苹果公证后才能安装。安装包留在私有备份，不放进 Git。

安装 VMware Tools 后保留默认 USB 鼠标，关闭 Fusion 的游戏鼠标捕获模式。若鼠标被捕获，先用 Control + Command 释放，再核对这两项；不要先改交易程序。
虚拟机关闭、暂停、主动睡眠、合盖或断电都会影响后台运行。本文防空闲休眠配置不支持合盖运行。

### 8.2 新建本机凭据，选择正确接口

新 Windows 的项目路径为 `C:\A_System`，在普通 PowerShell 安装依赖并确认 Python：

```powershell
cd C:\A_System
py -3.11 -c "import platform; print(platform.machine())"
py -3.11 -m pip install -r requirements.txt
```

应输出 `AMD64`。先保持人工停机，再创建本机 `.env`，已有文件不覆盖：

```powershell
py -3.11 stop_windows.py
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

填写 `TUSHARE_TOKEN`、真实 `QMT_ACCOUNT_ID`、`BARK_URL` 和实际 `QMT_PATH`。
本次路径是 `C:/QMT/GJQMT/userdata`；其他安装位置用自己的 `userdata`。明确保留 `QMT_TRANSPORT=qmt_inner`。
`QMT_INNER_CONFIG` 留空注释即可使用默认私有路径，不需要 `QMT_SESSION_ID`。
真实 `.env` 不在 Git；旧电脑不可访问时也可以重新填写，无需等待“原来的 .env”。旧交易账本仍须另外恢复，不能把重新填写配置当作完成账本迁移。

按第三节部署只读模型。若本机已有只读 QMT 私有配置，可运行这次已验证过并保存到项目的工具：

```powershell
py -3.11 scripts\configure_new_windows_env.py --qmt-path C:/QMT/GJQMT/userdata
```

它从本机私有配置读取账号，备份已有 `.env`、填写内置通道和本机路径，然后打开文件供填写 Token 和 Bark 地址；保留已有其他字段、人工停机和只读模式，不启动交易、不重置账本。
配置处于 `live`、停机标记缺失或账号不一致时会拒绝，不能用它重配运行中的系统。

### 8.3 QMT 模型与定时重启

使用第三节及 [内置迁移说明](qmt_inner_migration.md) 的完整模型入口，不把普通 `.py` 文件作为 `.rzrk` 策略包导入。
先看到 `A_SYSTEM QMT INNER STARTED; mode=read_only`，再执行只读探测和账本核对。
保存模型“终端启动后自动运行”，本次采用账户登录后延迟 10 秒；退出并重新登录后确认模型定时回调确实恢复。
在客户端关闭定时重启，并核验 `userdata/users/<本机账号>/Config.xml` 的 `TradeSetting modrestart="0"`。
不要改整个账户配置，不提交该文件。项目 `QMT_PATH` 必须指向实际 userdata，否则启动时无法确认定时重启设置。

`start_windows.py` 会拒绝只读端启动正式程序，这是验收门禁。
只读对账、模拟验证和第六节切换完成后，才按内置迁移说明预置 `live` 并由人工启动；不要为了消除报错删除门禁。
日志应依次出现“账户已验证”和“交易恢复门禁通过”，只看到 PID 或后台守护已创建不能算启动成功。实盘继续先小资金验证。

### 8.4 浏览器查看同步状态与登录自启

两端分别打开 `http://127.0.0.1:8384/`，文件夹 ID 使用 `a-system`，Mac 指向项目目录，Windows 指向 `C:\A_System`。
保留本地发现，关闭全局发现和中继；两端单独配置仓库 `.stignore`。新设备生成自己的设备 ID 和密钥，重新配对。
QMT 私有 `spool` 放在 `%LOCALAPPDATA%`，不能加入同步。设备 ID、API Key、IP 地址和 Windows 用户路径不从旧电脑硬复制。

Windows 可将 Syncthing 原生 ARM64 安装包解压到 `%LOCALAPPDATA%\Programs\Syncthing`，确保该目录包含 `syncthing.exe`；已有 PATH 安装也可复用。
在新 Windows 运行：

```powershell
py -3.11 scripts\open_sync_status.py --install-shortcuts
py -3.11 scripts\open_sync_status.py
```

安装命令把入口复制到本机私有目录，创建桌面“A_System 同步状态”和登录自启快捷方式。
桌面入口先检测服务，停止时启动 Syncthing，再打开浏览器；服务正常时直接复用。自启入口只启动服务，不打开网页。
如果网页提示连接被拒绝，先看 `%LOCALAPPDATA%\A_System\sync_tools\last_launch.json` 和 Syncthing 日志；这次出现过的原因是服务退出，不是交易数据文件权限。
验收要求网页远程设备已连接且“最新”，待同步项为 0；用无执行含义的临时文本文件验证 Mac 到 Windows 的实际传输，再删除并确认同步收敛。
“本地添加”是接收端自己的额外文件，不能据此点击还原或删除账本。

### 8.5 通知配置与验收

在 Windows `.env` 填入 `BARK_URL=https://api.day.app/<自己的设备Key>/`。
事件开关和每两小时健康通知沿用 `config/config.json` 的 `notify`，不要把整个通知配置留在聊天或临时脚本里。
通知测试命令：

```powershell
py -3.11 scripts\send_notify.py system_error "A_System 新电脑通知测试" "请确认手机收到；本次仅测试通知。"
```

命令成功后仍须确认手机收到；HTTP 成功不能单独代替送达验收。
若出现“未配置 BARK_URL”，检查运行程序所在 Windows 的 `.env`，不能只在 Mac 填写。
运行中的 daemon 不会自动重读所有环境变量，应在确认允许业务重启后停、启一次加载新配置。
当前健康通知为北京时间偶数小时的第 2 分钟，读取本地心跳和现有账户健康快照，不主动查询券商。

### 8.6 允许锁屏、息屏，防止空闲休眠

Mac 在项目目录运行：

```sh
python3 scripts/install_mac_fusion_awake.py
```

省略参数时必须只有一台运行中的虚拟机；否则用 `--vmx "/实际路径/目标.vmwarevm/目标.vmx"` 指定。
工具把完整守护复制到用户 `Library/Application Support/A_System/fusion_awake`，安装登录 LaunchAgent `com.eass.a-system.fusion-awake`。
目标 VM 的 `vmware-vmx` 进程存在时执行 `caffeinate -i -w <PID>`，进程退出时释放断言；不会启动 VM。
它没有 `-d` 参数，屏幕可以熄灭，也允许 Control + Command + Q 锁屏。
验收：`launchctl print gui/$(id -u)/com.eass.a-system.fusion-awake` 显示任务运行，`pmset -g assertions` 中本守护有系统空闲休眠断言、没有显示常亮断言。

第六节最终切换后，在 Windows 安装运行兜底任务：

```powershell
py -3.11 scripts\install_windows_runtime_guard.py
py -3.11 scripts\install_windows_runtime_guard.py --status
```

安装器创建两项普通用户任务：

| 任务 | 时间 | 功能 |
|---|---|---|
| `A_System_SessionStabilityGuard` | 登录及每日 07:50 | 私有 pythonw 守护，`0x80000001` 只阻止系统空闲休眠，允许息屏和锁屏；运行时间无限制 |
| `A_System_RuntimeGuard` | 登录及每日 08:15 | 沿用缺失进程恢复；人工停机时不启动业务 |

安装过程只立即启动防休眠任务，不立即启动交易兜底任务或 daemon。
不使用管理员 `Highest`，不禁用锁屏、密码认证或 Windows Update。
旧 `configure_windows_session_stability.ps1` 是历史处理，当前安装器已取消调用，不能作为新机设置步骤。
检查 `%LOCALAPPDATA%\A_System\runtime_tools\session_awake_status.json`：`status=AWAKE`、`execution_state=0x80000001`、API 返回非零。
`--status` 会识别旧任务动作、权限、执行时限和旧私有代码，返回 `OUTDATED`，避免仅凭同名任务误判为已配置。

本次 2026-10-07 实际锁屏约 70.8 秒：Windows 采样、QMT 定时回调、daemon 心跳均继续推进；Mac 锁屏期间新建的临时文本传到 Windows，删除后同步收敛。
测试只读取本地认证心跳，没有发送券商请求或委托。它验证本次运行环境，不能替代将来新机的验收。
新机应插电、保持盖子打开，实际锁屏至少一分钟后再解锁，核对上述心跳和同步文件。

### 8.7 必须私下备份的东西

Git 已保存可复用代码、无密钥配置和操作流程；私下另备份真实 `.env`、虚拟机加密密码、已验证安装包，以及最终停机交易账本。
设备同步密钥与 QMT Token 在新设备重新生成，不把旧私有执行队列直接当作活动队列恢复。
旧电脑无法访问且无账本备份时，必须核对券商持仓和未完成委托并处理恢复差异，不能宣称已恢复全部历史。

### 8.8 本次保存进代码的文件与验证方法

完整可运行代码已保存在下列链接，不需要手工拼接片段。新增工具来自本次私有目录中已使用的配置操作，增加了跨用户名路径处理及隔离回归测试。

| 文件 | 方法/位置 | 操作、原因 |
|---|---|---|
| [configure_new_windows_env.py](../scripts/configure_new_windows_env.py) | `configure/main` 全文件 | 新增：从本机只读配置重建 .env，保留其他凭据，备份、校验后打开编辑；避免依赖旧电脑 .env |
| [open_sync_status.py](../scripts/open_sync_status.py) | `gui_listening/ensure_running/install_shortcuts/main` 全文件 | 新增：服务检测、复用或启动、浏览器入口及私有快捷方式；修复服务退出后的网页连接拒绝 |
| [windows_session_awake_guard.py](../scripts/windows_session_awake_guard.py) | `main` 全文件 | 新增：系统空闲防休眠、单实例锁、状态报告及退出释放；没有显示常亮标志，允许锁屏 |
| [guard_fusion_awake.sh](../scripts/guard_fusion_awake.sh) | 主循环与 `cleanup` 全文件 | 新增：按目标 VM 进程持有/释放 `caffeinate -i`；进程匹配由安装器传入，移除固定 VM 名称 |
| [install_mac_fusion_awake.py](../scripts/install_mac_fusion_awake.py) | `detect_vmx/process_pattern/install/main` 全文件 | 新增：检测唯一 VM、转义路径、复制私有守护、登录自启；无需固定 Mac 用户名 |
| [install_windows_runtime_guard.py](../scripts/install_windows_runtime_guard.py) | `_session_paths/install/status` | 替换会话任务的旧 PS 动作与 Highest 权限为私有 pythonw、Limited、无限执行；删除默认防锁屏/更新脚本调用；状态检查新增旧配置识别 |
| [.env.example](../.env.example) | QMT 配置段 | 替换默认通道为本次 qmt_inner，说明 userdata 路径和旧 session 用途；没有真实凭据 |
| [new_machine_profile.json](../config/new_machine_profile.json) | 全文件 | 新增本次无密钥参考配置；不自动覆盖交易配置或激活交易 |
| [test_lock_friendly_runtime_guards.py](../tests/test_lock_friendly_runtime_guards.py) | 三个测试类 | 新增 API 标志、失败释放、重复实例、旧任务、私有安装和 VM 路径匹配测试 |
| [test_new_windows_env_setup.py](../tests/test_new_windows_env_setup.py) | `NewWindowsEnvironmentTest` | 新增凭据保留、不同账号拒绝、只读/人工停机保护测试 |
| [test_sync_status_entry.py](../tests/test_sync_status_entry.py) | `SyncthingEntryTest` | 新增冷/热启动及私有快捷方式测试，不改设备身份 |
| [test_runtime_recovery_tools.py](../tests/test_runtime_recovery_tools.py) | `test_normal_startup_auto_checks_and_installs_runtime_guard` | 替换旧 Highest/PS 调用断言，验证新默认入口与人工停机纪律 |
| 本文、[旧搭建手册](vm_environment_setup.md)、[历史事故记录](incident_20260915_windows_restart.md)、[README](../README.md) | 当前配置说明处 | 新增统一入口及历史配置失效说明，修正旧 miniQMT 和强制常亮指引 |

运行安装与现场验收命令见 8.2—8.6。代码回归只能在同步目录之外的仓库副本执行：

```sh
python3 -B -m unittest discover -s tests
sh -n scripts/guard_fusion_awake.sh
```

测试使用临时目录和模拟 Win32/计划任务/快捷方式接口，不启动生产程序。Mac 上 Windows PowerShell 实机输出测试会跳过，计划任务现场验收仍按 8.6 执行。
