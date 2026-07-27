# QMT / miniQMT 实盘接入说明

本文档说明当前项目如何接入券商 QMT / miniQMT，以及 Windows 虚拟机的部署架构。

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
| Windows VM（UTM 虚拟机） | 运行 miniQMT 客户端、执行实盘委托 |

A_System 目录通过 UTM 共享文件夹（WebDAV）挂载到 Windows VM 的 `Z:` 盘，代码在 Mac 修改后 Windows 即时可见。

> ⚠️ 但 `Z:` 是 WebDAV 共享盘，负载下会间歇性抛 `WinError 58`（网络错误），拖慢甚至卡死文件 I/O。
> 因此 **运行期间不直接跑在 `Z:` 上**，而是"启动时把代码同步到 VM 本地盘、之后全程本地运行"。见下节。

### 运行位置：本地盘运行 + 代码/记录双向同步

**问题背景**：早期 daemon 直接从 `Z:`（WebDAV 共享盘）运行，每个周期都从 `Z:` 读脚本、把 `positions.json`/CSV/日志写回 `Z:`。共享盘一抽风（`WinError 58`），平仓、收盘流水线等关键 I/O 就会被拖住（曾出现 `run_combined_live_plan.py` 卡满 180s、`run_strategy_l_signal.py` 因 `Z:` 建目录报 `WinError 58` 崩溃）。

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

- 虚拟机软件：UTM（免费，Mac M 系列专用）
- Windows：Windows 11 ARM
- Python：x64 版 Python 3.11（**必须是 x64，不能用 ARM64**，因为 xtquant 的 DLL 是 x64）
- 安装路径：`C:\Users\大A\AppData\Local\Programs\Python\Python311\`
- 运行方式：`py -3.11 <脚本>`

### xtquant 安装

xtquant 可通过 PyPI 直接安装（需 x64 Python）：

```powershell
py -3.11 -m pip install xtquant pandas numpy python-dotenv requests tenacity openpyxl tabulate tushare
```

miniQMT 的服务端 DLL 位于：`C:\Users\大A\国金证券QMT交易端\bin.x64\XtQuantServer.dll`

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
cd Z:\
py -3.11 start_windows.py
```

停止守护进程：
```powershell
cd Z:\
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
cd Z:\
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
| 09:20 | 盘前：平仓检查 + 组合状态机 + A/C 买入 + 策略D监控启动 |
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

### Windows 共享盘 WinError 58（根治：本地盘运行）
UTM WebDAV 共享盘偶发返回 `WinError 58`，导致目录检查、CSV 写入或清洗读取失败，曾拖垮 14:xx 平仓前的组合刷新（`run_combined_live_plan.py` 卡满 180s 被强杀）、并让 `run_strategy_l_signal.py` 在 `Z:\reports\strategy_l` 建目录时崩溃。早期缓解：采集/清洗模块对目录创建、文件存在检查、CSV 读写增加重试。**根治**：daemon 不再直接跑在 `Z:`，而是启动时把代码同步到本地盘、全程本地运行（见上文「运行位置：本地盘运行 + 代码/记录双向同步」）。

### Tushare 单请求超时（已加固）
`Z:` / 网络抖动或 Tushare 限流时，单个请求可能长时间卡住，拖满收盘流水线 `collect_all_data.py` 的 600s 预算。修复：`data_source.py` 给每个 Tushare 请求加应用层墙钟超时（`config.json` 的 `data_source.request_timeout_seconds`，默认 60s），超时即中止交给 tenacity 重试，避免单接口卡死拖垮整条流水线。

### PowerShell 中文乱码（已修复）
守护进程调用子进程时使用 `python -u` 实时输出，并设置 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。主进程读取输出时优先 UTF-8，失败再 GBK 兜底。

---

## QMT 登录失败排查手册（2026-07-25 实战总结）

**症状**：QMT 终端登录报错只有干巴巴一句"登录失败"，无任何细节；账号资金页显示
"准备登录"、更新时间停在上一交易日、总资产是垃圾值（0 或 1 亿）。

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

### 兜底方案（QMT 登不上时）

- **手机 App 可交易**：到期持仓必须在到期日 14:55 前手动平仓（超期就脱离回测口径）；
- **开仓可以放弃**：买不进只是丢机会，不亏钱；
- **daemon 与 keeper 保持运行**：QMT 不通时启动门禁阻止交易调度，连接尝试自动退避；
  daemon 退出或假死由 keeper 自动拉起，维护结束后只有账户与程序双验证成功才通知恢复。

### 已完成的维护自愈（2026-07-27）

- 前 3 轮完整扫描，4～10 轮只试首选 session 并等待 30 秒，之后每 5 分钟重试；
- 账户连续不可用 2 分钟后发送通知，恢复后发送“程序+账户”双验证通知；
- 套接字资源耗尽时 daemon 主动退出释放资源，由 keeper 拉起新进程；
- 快速崩溃超过配置上限后转为每 10 分钟低频永久重试，不再永久停掉自动恢复；
- keeper 日志写入 `logs/win_daemon_keeper.log`，账户状态写入 `logs/broker_health.json`。
