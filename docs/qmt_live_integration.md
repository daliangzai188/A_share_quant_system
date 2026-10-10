# QMT / miniQMT 实盘接入说明

本文档说明当前项目如何接入券商 QMT / miniQMT，以及 Windows 虚拟机的部署架构。

> ⚠️ **通道现状（2026-10 校订）**：实盘交易通道已切换为 **`qmt_inner`**（QMT 内置 Python，
> `QMT_TRANSPORT=qmt_inner`，由 `src/qmt_inner_start_gate.py` 强制校验），部署以
> `docs/qmt_inner_migration.md` 为准。本文下面的 **xtquant / `userdata_mini` / `QMT_SESSION_ID`
> 相关章节属于旧 miniQMT 接口**，仅供参考与回溯，不要照着当现行方案配。
>
> 但**「QMT 登录失败排查手册」一节与通道无关、始终适用** —— 终端自身的账号密码登录
> （→ 交易服务器 56001）在两种通道下完全相同。

## 当前分工

| 层级 | 实现 |
|---|---|
| 历史数据 | Tushare Pro + 本地 CSV |
| 策略信号 | A/C 每日操作台（B已于2026-07-22删除） |
| 实时行情 | QMT `xtdata` |
| 资金/持仓/委托/成交 | QMT `xttrader` |
| 下单/撤单 | QMT `xttrader`，已开启 |

## 部署架构

QMT / miniQMT 只能在 Windows 上运行。项目采用双机架构：

| 机器 | 职责 |
|---|---|
| Mac（主机） | 编辑代码、Tushare 数据采集、信号生成 |
| Windows VM（**VMware Fusion**） | 运行 QMT 交易端、执行实盘委托 |

**当前代码/数据通道是 Syncthing**（Mac `~/Desktop/A_System` ↔ VM `C:\A_System` 双向实时同步），
见下节。

> 📜 **历史设计（已废弃，勿再启用）**：早期用 UTM 共享文件夹（WebDAV）把 A_System 挂成 VM 的
> `Z:` 盘直跑。`Z:` 在负载下会间歇性抛 `WinError 58`，拖慢甚至卡死文件 I/O，是四次实盘事故的
> 根因。现行架构**不存在 `Z:` 盘运行路径**；`start_windows.py` 的防呆闸仍保留对 `Z:`/UNC 启动的
> 拒绝，作为残留配置的兜底。

### 运行位置：本地盘运行 + 代码/记录双向同步

**问题背景**：早期 daemon 直接从 `Z:`（WebDAV 共享盘）运行，每个周期都从 `Z:` 读脚本、把 `positions.json`/CSV/日志写回 `Z:`。共享盘一抽风（`WinError 58`），平仓、收盘流水线等关键 I/O 就会被拖住（曾出现组合计划脚本卡满180秒、信号脚本因共享盘建目录失败而崩溃）。

**解决架构**：把「代码」和「运行时状态」分开，各有一个权威方，做**系统级双向同步（不是同一文件双向覆盖）**：

| 内容 | 权威方 | 同步方向 | 说明 |
|---|---|---|---|
| 代码 `src/` `scripts/` `config/` `docs/` + 根级脚本 | **Mac** | Mac → 本地 | 你在 Mac 改，重启自动同步进来 |
| 持仓 `data/processed/positions.json`（开仓/平仓记录） | **VM 本地** | 本地 → Mac | 运行时写，回传给 Mac 查看 |
| 候选、开仓计划、组合计划单（`reports/`） | **VM 本地** | 本地 → Mac | 同上 |
| live 信号/评分 CSV（`data/processed/`）、日志 | **VM 本地** | 本地 → Mac | 同上 |
| 行情缓存 `data/raw/`（1.7G） | VM 本地 | 不回传 | Mac 侧已有且可重采，无需回传 |

整体是双向的（代码下去、记录上来），但**任何单个文件都不会被反方向覆盖**——尤其绝不让 Mac 上一份旧的 `positions.json` 回灌覆盖实盘持仓。

> ⚠️ 约定：Mac 与 VM 本地 `C:\A_System` 通过 **Syncthing 双向实时同步**（2026-07-10 定稿）。运行状态（positions.json 等）以 VM 端为权威；人工修正持仓请在盘后进行，改动会自动双向传播。

**运行目录**：`C:\A_System`。启动/停止都在此目录执行（`cd C:\A_System` 后 `py -3.11 start_windows.py` / `stop_windows.py`）。`start_windows.py` 带防呆闸：从 `Z:`/UNC 路径启动会被直接拒绝并提示正确命令。

**同步机制（Syncthing，2026-07-10 起取代全部旧方案）**：

- Mac 端：Homebrew 安装，`brew services` 常驻自启；文件夹 id=`a-system`，路径 `/Users/user/Desktop/A_System`；外部 relay 与全球发现已关闭（数据只走 Mac↔VM 内网直连）。
- VM 端：winget 安装原生 ARM64 版（`winget install Syncthing.Syncthing`），文件夹路径 `C:\A_System`，开机自启走启动文件夹快捷方式（`syncthing serve --no-console --no-browser`），Web UI `127.0.0.1:8384`。
- 忽略规则见根目录 `.stignore`（__pycache__、*.pyc 等）。
- 日常流：Mac 改代码 → 秒级到 C 盘 →（需重启 daemon 生效）；VM 产生的 logs/reports/data 秒级回 Mac 供分析。
- **历史方案均已废弃，勿再启用**：
  - UTM WebDAV 共享盘直跑：IO 慢 1~2 个数量级，四次实盘事故根因；
  - Mac SMB 共享（192.168.64.1）：macOS smbd 与 Windows 11 ARM 存在 SMB 签名兼容 bug，大目录枚举必报"[WinError -2146893818] 无效签名"，SMB210/302/311 dialect 全部复现，只能弃用；
  - robocopy 同步/回传脚本：依赖上述通道，且方向靠人记，误跑会用旧文件覆盖新文件，已删除。

**停止（`stop_windows.py`）**：pid/目录指向 `C:\A_System`，同时停掉 daemon 与 D 监控。

**相关文件**：`start_windows.py`、`stop_windows.py`、`.stignore`。

### Windows VM 环境

- 虚拟机软件：**VMware Fusion**（当前实际宿主）。历史上曾用 UTM；两种宿主的搭建分支见
  `docs/vm_environment_setup.md`。排查 VM 网络时按 Fusion 口径操作（重启 Fusion NAT / vmnet8），
  不要再照 UTM 的 `utmctl` 流程走
- Windows：Windows 11 ARM
- Python：x64 版 Python 3.11（**必须是 x64，不能用 ARM64**，因为 xtquant 的 DLL 是 x64）
- 安装路径：`C:\Users\<当前用户>\AppData\Local\Programs\Python\Python311\`（旧机为 `大A`，现为 `eass`）
- 运行方式：`py -3.11 <脚本>`

### xtquant 安装

xtquant 可通过 PyPI 直接安装（需 x64 Python）：

```powershell
py -3.11 -m pip install xtquant pandas numpy python-dotenv requests tenacity openpyxl tabulate tushare
```

> 📍 **本机实际安装路径（2026-10-10 实测，取代下列旧路径）**
>
> | 项 | 实际值 |
> |---|---|
> | QMT 安装根目录 | `C:\QMT\GJQMT` |
> | 主程序 | `C:\QMT\GJQMT\bin.x64\XtItClient.exe`（完整 QMT，x64 经 Prism 转译） |
> | 账号配置 | `C:\QMT\GJQMT\userdata\users\<资金账号>\Config.xml` |
> | 客户端日志 | `C:\QMT\GJQMT\userdata\log\XtClient_<YYYYMMDD>.log` |
> | Windows 用户名 | `eass`（**不再是** `大A`） |
>
> 本节以下出现的 `C:\Users\大A\...`、`国金证券QMT交易端`、`userdata_mini` 均为**旧机旧接口**
> 路径，换机后已失效，仅作历史参考。排查时以上表为准。

miniQMT 的服务端 DLL（旧机路径，已失效）：`C:\Users\大A\国金证券QMT交易端\bin.x64\XtQuantServer.dll`

## 配置 .env

`.env` 文件中填写：

```text
QMT_ACCOUNT_ID=你的证券资金账号
QMT_ACCOUNT_TYPE=STOCK
QMT_PATH=C:\Users\大A\国金证券QMT交易端\userdata_mini
QMT_SESSION_ID=1001
```

`QMT_PATH` 必须指向 `userdata_mini` 目录，适配器会自动尝试多个 session_id（1001、1002、10001、20001、31001）。

## 配置 config.json（已开启实盘）

当前配置已设置为实盘模式：

```json
{
  "trade_mode": "live",
  "broker_adapter_enabled": true,
  "qmt_enabled": true,
  "broker": { "enabled": true, "adapter": "qmt" },
  "live_trade": {
    "enabled": true,
    "real_order_enabled": true
  }
}
```

## 日常操作：启动与停止

**在 Windows VM 的 PowerShell 中操作。**

启动守护进程：
```powershell
cd C:\A_System
py -3.11 start_windows.py
```

停止守护进程：
```powershell
cd C:\A_System
py -3.11 stop_windows.py
```

启动后日志实时显示在终端，颜色说明：
- 绿色 `✅` — QMT 连接成功、程序状态正常
- 黄色 `⚠️` — 警告、暂不开仓、需要关注但不一定阻塞
- 红色 `❌` — QMT 连接失败或程序错误

每 5 分钟自动打印一次状态，包含：程序状态、账户可用资金、当前持仓。

Ctrl+C 断开日志显示，守护进程继续在后台运行。

`start_windows.py` 会在发现旧守护进程时先强制停止旧 PID，并等待 15 秒释放 QMT session，再启动新进程。不要连续快速重复启动；如需重启，优先按下面顺序：

```powershell
cd C:\A_System
py -3.11 stop_windows.py
py -3.11 start_windows.py
```

## 收盘数据缓存与自动流水线

守护进程启动后会检查当天收盘流水线是否已经完成：

- 已完成：直接使用缓存，不重复拉取数据。
- 未完成：后台自动执行收盘流水线，不影响 QMT 状态刷新。

正常缓存命中日志：

```text
已有 20260615 收盘数据缓存，直接使用
```

无缓存时会自动执行：

```text
① 采集日线 + 涨停池
② 清洗合并数据
③ 市场情绪 / 题材热度
④ 涨停成交概率打分
⑤ 次日溢价因子
⑥ A/C 信号生成（B已删除）
```

清洗阶段会显示百分比进度：

```text
清洗进度: 54.5% (6/11)，当前日期: 20260608，daily累计: 33066，limit累计: 463
```

`collect_all_data.py`、`clean_collected_data.py`、`score_limit_up_fill_probability.py` 属于关键步骤。关键步骤第一次失败会等待 10 秒自动重试一次；仍失败则停止本次流水线，不生成计划单，避免使用旧信号。

Windows 子进程日志已强制 UTF-8 输出并实时转发到主日志，PowerShell 中不应再出现中文乱码。

## 当日涨停池兜底模拟观察

如果 A/C 日操作台因为数据尚未更新而暂时不能生成目标交易日计划，守护进程会自动调用：

```powershell
py -3.11 scripts\generate_live_limit_pool_daily_ops.py --signal-date YYYYMMDD --top-n 10
```

该脚本只基于当日 `data/processed/limit_up_fill_scored.csv` 生成模拟观察清单，输出到：

```text
reports/paper_trade/ab_filtered_daily_ops/
```

安全限制：

- 不连接 QMT。
- 不调用真实下单接口。
- 输出文件中 `live_order_enabled=False`。
- 当前版本只生成 `WATCH_ONLY` 观察清单，不生成 BUY 委托。
- 实盘前仍必须经过 QMT 预览、人工复核和小资金验证。

## 守护进程任务时间表

| 时间 | 任务 |
|---|---|
| 09:20 | 盘前：平仓检查 + 组合状态机 + A/C/E买入链 + 策略D监控启动；只有无持仓、无非D候选/计划时D才启动。daemon晚启动且检查点不可用时，D从QMT严格回补09:30至当前1m，认证完成后继续且不追买旧信号 |
| 14:56 | 盘中收盘平仓（最高优先，绝不被任何步骤阻塞）→ 内部等到 14:57 撤销所有未成交**买单**（不撤卖单，避免平仓单被误撤） |
| 15:35 | 收盘：采集数据 → 清洗 → 生成信号 → 明日候选 |

## 只读账户检查

验证 QMT 连接和账户数据是否正常：

```powershell
py -3.11 scripts/qmt_account_check.py
```

生成：
```text
reports/live_trade/qmt_account_check_account.csv
reports/live_trade/qmt_account_check_positions.csv
reports/live_trade/qmt_account_check_orders.csv
reports/live_trade/qmt_account_check_trades.csv
```

## 准备度检查

```powershell
py -3.11 scripts/check_qmt_live_readiness.py
```

所有条目 PASS 后才可开启实盘。

## 已知问题与修复记录

### FrozenInstanceError（已修复）
`BrokerConnectionConfig` 是 frozen dataclass，`connect()` 成功后原代码试图写入 `self.config.session_id`，抛出 `FrozenInstanceError` 导致连接被误判为失败。修复：改用 `self._active_session_id` / `self._active_qmt_path` 存储实际连接信息。

### session_id 冲突（已修复）
适配器现在自动尝试多个 session_id，不依赖单一配置值。

### Windows 上 Python 架构问题（已解决）
miniQMT 是 x64 程序，必须用 x64 Python 才能加载 xtquant 的 DLL。ARM64 Python（Windows 11 ARM 默认）不兼容，需单独安装 x64 Python 3.11。

### trading_daemon.py PYTHON 路径（已修复）
原代码硬编码 `.venv/bin/python`（Mac 路径），在 Windows 不存在。修复：优先用 `.venv/bin/python`，不存在时回退到 `sys.executable`。

### QMT session 短暂释放失败（已缓解）
快速停止后立刻启动时，QMT 可能短时间返回 `connect=-1`。修复：Windows 启动脚本等待 15 秒释放旧 session，守护进程启动检查最多重试 5 次，每次间隔 15 秒。

### 运行中瞬时空快照触发重连自锁（2026-09-08已修复）

2026-09-08 08:39，已稳定运行的持久连接发生一次 `QMT账户查询返回None`。旧代码立刻
释放连接；随后首选 session 返回 `connect=-1`。运行中恢复逻辑虽然在第3次标记“完整
扫描备用session/path”，但同一轮先失败的首选连接已经设置退避截止时间，第二次调用
因此立即被退避门拒绝，实际没有执行完整扫描。退避逐步扩大到300秒，09:20竞价预挂
和09:30 POV均无法连接QMT，最终开仓失败。

修复后的固定恢复顺序是：

1. 仅对明确的QMT只读空返回，在同一个持久会话内复查1次；非法结构、资产矛盾仍立即
   fail-closed，不按瞬时故障放过。
2. 首选 session 连续失败时，由唯一建连入口把第3次**真实建连**直接升级为一次完整
   备用 path/session 扫描；不在同一业务轮内先失败再二次调用，从根源上避免自退避。
3. 完整扫描仍返回 `connect=-1` 时，停止继续交易并让当前daemon以专用退出码退出；
   keeper拉起全新进程后，必须先核对真实委托、成交、持仓，再恢复调度，禁止盲目重发。
4. `logs/broker_health.json` 使用进程/线程/时间唯一临时文件；遇到Windows或Syncthing
   的短暂共享冲突有限重试，失败仍不阻塞交易主流程。

完整事故证据和测试记录见 `docs/incident_20260908_qmt_reconnect_open_failure.md`。

### Windows 共享盘 WinError 58（根治：本地盘运行）
UTM WebDAV 共享盘偶发返回 `WinError 58`，导致目录检查、CSV 写入或清洗读取失败，曾拖垮14:xx平仓前的组合刷新，并让信号脚本在共享盘创建报告目录时崩溃。早期缓解：采集/清洗模块对目录创建、文件存在检查、CSV读写增加重试。**根治**：daemon不再直接跑在`Z:`，而是启动时把代码同步到本地盘、全程本地运行（见上文「运行位置：本地盘运行 + 代码/记录双向同步」）。

### Tushare 单请求超时（已加固）
`Z:` / 网络抖动或 Tushare 限流时，单个请求可能长时间卡住，拖满收盘流水线 `collect_all_data.py` 的 600s 预算。修复：`data_source.py` 给每个 Tushare 请求加应用层墙钟超时（`config.json` 的 `data_source.request_timeout_seconds`，默认 60s），超时即中止交给 tenacity 重试，避免单接口卡死拖垮整条流水线。

### PowerShell 中文乱码（已修复）
守护进程调用子进程时使用 `python -u` 实时输出，并设置 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。主进程读取输出时优先 UTF-8，失败再 GBK 兜底。

---

## QMT 登录失败排查手册（2026-07-25 实战总结）

**症状**：QMT 终端登录报错只有干巴巴一句"登录失败"，无任何细节；账号资金页显示
"准备登录"、更新时间停在上一交易日、总资产是垃圾值（0 或 1 亿）。

### 第 0 步：先区分已验证现象与可能原因（2026-10-10）

| 报错形态 | 能说明什么 | 下一步 |
|---|---|---|
| `200003 超时`、`10060 超时`、卡在“创建基础界面” | 登录流程未在期限内完成；不能仅凭超时判断失败发生在鉴权前还是鉴权后 | 查当前会话日志、服务器线路、客户端状态，再核验账户 |
| 明确的密码/账号错误或错误次数累计 | 客户端收到了相关认证错误 | 停止自动重试，在本机核对凭据和账户状态 |

TCP 可达只证明某个端点接受了连接，不能证明券商后台、登录协议或账户可用。
周末或盘后可能存在维护，但时刻和“未找到公告”都不能单独证明本次故障由维护造成。
没有收到明确密码错误时，也不能反向证明账号密码已通过认证。

### 定时重启检查

QMT 的账号 `Config.xml` 中，`TradeSetting` 的 `modrestart` / `restarttimelist` 是需要核验的
客户端设置。使用 `src/qmt_client_probe.py` 的 `read_scheduled_restart_settings()` /
`scheduled_restart_problem()` 做只读检查，不能仅凭停在登录框就推断发生过定时重启。
本次读到 `modrestart="0"`，详见后面的实测记录。

### 排查顺序（按这个走，20 分钟内定位）

| 步骤 | 操作 | 判读 |
|---|---|---|
| **1. 停 daemon** | `py -3.11 stop_windows.py`，再用 `Get-CimInstance Win32_Process -Filter "Name like 'python%'"` 确认为空 | daemon 门禁每 10 秒做一次**全量 session 扫描**，会与终端抢 session、打断登录。测试前必须停干净 |
| **2. 手机 App 登录** | 用国金证券手机 App 登录 | **能登** → 账户/密码正常，排除冻结、风控、改密；**登不上** → 账户级问题，打客服 |
| **3. VM 网络** | `Test-NetConnection 114.28.170.219 -Port 56001 \| Select TcpTestSucceeded` | True → 网络正常；False → 修 VM 网络（重连网卡 / `ipconfig /release`+`/renew` / 重启 VM / 重启 Fusion NAT） |
| **4. 从 Mac 测券商服务器** | 见下方脚本 | 全通 → 券商服务器在线，不是宕机；全不通 → 券商侧故障 |
| **5. 行情 tab 单独登录** | QMT 登录页选"行情"而非"行情+交易" | 行情能登、交易不能 → **交易系统单独维护** |

### 券商服务器地址（QMT 通信设置里读出，可从任意机器直接测）

**交易服务器（端口 56001）**：`qmt.gjzq.com.cn`、`114.28.170.219`(联通2·默认)、
`114.141.171.138`(电信)、`223.166.183.159`(联通)、`162.14.133.107`(电信2)、
`221.236.15.45/46`(灾备，平时拒绝连接属正常)

**交易中心（端口 59000）**：`139.196.27.177`(默认)、`139.224.114.71`、`81.69.152.51`

**行情主站（端口 55310/55300）**：`115.231.218.79`(绍兴·默认)、`115.231.218.73`、
`218.16.123.121/122`(东莞)

连通性测试脚本（Mac/Linux 直接跑）：
```bash
python3 -c "
import socket,time
for h,p,n in [('qmt.gjzq.com.cn',56001,'交易'),('114.28.170.219',56001,'交易默认'),
              ('139.196.27.177',59000,'交易中心'),('115.231.218.79',55310,'行情')]:
    t=time.time()
    try:
        socket.create_connection((h,p),timeout=6).close()
        print(f'{n}: 通 {(time.time()-t)*1000:.0f}ms')
    except Exception as e: print(f'{n}: 不通 {type(e).__name__}')
"
```

### 2026-07-25 实测结论

周六 13:07 QMT 正常（可用资金 366,274 元），15:00 后开始：账户资产返回 0 → 1 亿，
终端退回"准备登录"且再也登不上。逐项排查后：**账户正常（手机 App 能登）、VM 网络正常
（TCP 56001 通）、券商服务器全部在线（Mac 测全通）** → 判定为 **QMT 交易系统周末维护
（端口 listening 但后台不受理登录）**。通常会在下一个交易日前恢复，但仍以账户查询
实际验证成功为准，不能假设必然按时恢复。

### 周末维护与公告核验的限制

目前没有取得与 2026-10-09 晚至 2026-10-10 这次故障对应的国金 QMT 官方维护公告。
社区关于周末测试或维护的经验只能提供排查方向，不能据此宣称国金每周固定不受理登录，
也不能把交易时段直接当成该客户端的登录受理时段。

### 定时重启：风险项，但**已排除**为 2026-10-09 事故根因

> 🔻 **2026-10-10 更正**：本节初稿曾把"定时重启在周五盘后自动重登失败"写成本次事故的
> 根因并给出完整因果链。**该结论已被实测推翻，不要再引用。**

实测（2026-10-10 01:2x，客机 `C:\QMT\GJQMT\userdata\users\<账号>\Config.xml`）：

```
modrestart       = "0"            ← 定时重启已正确关闭
restarttimelist  = "085123|205335|"
```

- `modrestart="0"` 说明 `docs/new_mac_rebuild_migration.md` 8.3 节的要求**确实已经落实**；
- 表中重启时刻为 08:51:23 / 20:53:35，与当晚 **21:11:51** 的掉线时间**对不上**；
- 因此 21:11 掉线**不是**定时重启造成的，根因另在他处。

**仍然保留的结论（与本次事故无关，但是真实风险）**：若 `modrestart` 为 `"1"`，或
`TradeSetting` 里**根本没写这一项**（按 QMT 模板默认开启），则每天到点整进程重启并自动
重新登录；一旦重启时刻落在券商不受理登录的时段，客户端会**停在登录框**直到人工介入。
换机/重建后必须显式核验为 `"0"`——只读核验工具：`src/qmt_client_probe.py` 的
`read_scheduled_restart_settings()` / `scheduled_restart_problem()`。

> 教训（排查方法）：**在读到配置实际值之前，不要把推断写成根因。** 本节的错误结论来自
> "症状吻合 + 文档里该项标为待确认"的双重推测，而实际值一查即破。

### 后端存活探测（比 TCP 连通更强的判别法，2026-10-10 新增）

**问题**：原第 4 步只测 TCP 能否 connect。但维护期前端照样 accept，所以"全通"既可能是
正常也可能是维护中 —— 判别力为零（排查记录里早已写明「TCP可达不等于登录成功」）。

**补充观察**：connect 之后再 `recv()`，记录端点是否主动发送数据。
行情和交易使用不同协议；交易端点可能等待客户端先发协议报文，因此沉默不能证明后台失效。

```bash
python3 -c "
import socket,time
targets=[('114.28.170.219',56001,'交易-联通2'),('qmt.gjzq.com.cn',56001,'交易-默认域名'),
         ('162.14.133.107',56001,'交易-电信2'),('139.196.27.177',59000,'交易中心'),
         ('115.231.218.79',55310,'行情主站(对照组)')]
for h,p,n in targets:
    t=time.time()
    try:
        s=socket.create_connection((h,p),timeout=6); ct=(time.time()-t)*1000; s.settimeout(4)
        try:
            data=s.recv(64)
            r=f'服务端主动发来 {len(data)} 字节' if data else '服务端立即关闭(FIN)'
        except socket.timeout: r='连接保持但服务端沉默4秒'
        s.close(); print(f'{n:20s} 握手{ct:6.0f}ms | {r}')
    except Exception as e: print(f'{n:20s} TCP不通 {type(e).__name__}')
"
```

**判读**：

| 行情主站 | 交易服务器 | 结论 |
|---|---|---|
| 主动发字节 | 全部沉默 | 仅说明主动发送行为不同；需结合实际 QMT 登录日志继续查 |
| 主动发字节 | 主动发字节 | 两类端点均发来数据，仍不能证明账户可登录 |
| 沉默/不通 | 沉默/不通 | 区分 TCP 连接失败与连接后沉默；前者查网络、DNS、出口 |

> 该探测不需要密码，只记录网络现象；不能代替协议认证或账户查询。

### 2026-10-09/10 实测记录（本次）

周五 10-09 为国庆假期后的正常交易日，白天系统正常，收盘流水线完整跑完（`reports/` 下有
20261009 全套信号与计划单）。21:11:51 发生一次掉线，此后在**周六凌晨**反复尝试登录全部失败。

观测与判读：

| 观测 | 判读 |
|---|---|
| 错误为 `200003 超时`，切备用线路后 `10060 超时` | 登录流程超时，无法单独定位认证阶段 |
| Mac 直测 6 台券商服务器 TCP 可达（190ms~2.2s，含交易/交易中心/行情） | 这些端点的 TCP 连接成功，不能排除应用层故障 |
| 客机侧 `114.28.170.219`(223ms)、`162.14.133.107`(221ms) 等 TCP 可达 | VM 对这些端点可建立连接，不能排除协议或出口差异 |
| 切换并保存备用线路 114.28.170.219 后仍 10060 超时 | 切换该线路没有恢复登录；尚未确认各线路后端结构 |
| 客户端卡在“创建基础界面”；00:29:47 日志记录脱机模式 | 记录了初始化停滞和脱机状态，原因仍待查 |
| 尝试时刻为北京时间周六 00:00 之后 | 非交易时段是背景信息，不能单独证明不受理登录 |
| 行情主站主动发来 64 字节；4 个交易端点全部 TCP 接受后沉默 4 秒 | 不同端点主动发送行为不同；并非维护的决定性证据 |

**当前结论**：客户端登录未恢复，根因尚未确认。现有超时和 TCP 观察不足以认定是券商维护，
也不能据此排除账户、客户端或应用层网络问题。`modrestart="0"` 的实际配置与旧稿中“定时重启
导致掉线”的猜测不符；旧猜测撤回。

**后续验证**：本机保存凭据并完成当前验证码后，检查本次会话是否真正在线，再使用现行
`qmt_inner` 通道的只读检查核验真实账户数据。旧 miniQMT 的账户检查不能作为现行通道的唯一
成功判据。自动恢复的实际安装状态及限制见 `docs/qmt_client_recovery.md`；不要为了复测未知
故障而强杀活跃交易会话。需要实盘接口验证时先小资金验证。

### 兜底方案（QMT 登不上时）

- **手机 App 可交易**：到期持仓必须在到期日 14:55 前手动平仓（超期就脱离回测口径）；
- **开仓可以放弃**：买不进只是丢机会，不亏钱；
- **daemon 与 keeper 保持运行**：QMT 不通时启动门禁阻止交易调度，连接尝试自动退避；
  daemon退出或假死由keeper自动拉起；keeper只确认新PID/心跳，账户与交易恢复由daemon自己验证和通知。

### 已完成的维护自愈（2026-07-27）

- 启动门禁前3轮完整扫描，4～10轮只试首选session并等待30秒，之后每5分钟重试；
- 运行中掉线先原会话复查瞬时空返回，第3次真实建连完整扫描；完整扫描仍失败则由keeper换新daemon进程并先对账；
- QMT启动门禁连续不可用2分钟后由daemon发送通知，恢复后也由daemon发送“程序+账户”验证通知；
- 套接字资源耗尽时 daemon 主动退出释放资源，由 keeper 拉起新进程；
- 快速崩溃超过配置上限后转为每 10 分钟低频永久重试，不再永久停掉自动恢复；
- keeper只把进程事件写入 `logs/win_daemon_keeper.log`；`logs/broker_health.json`由daemon维护，keeper不读取。
