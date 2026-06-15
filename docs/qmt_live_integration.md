# QMT / miniQMT 实盘接入说明

本文档说明当前项目如何接入券商 QMT / miniQMT，以及 Windows 虚拟机的部署架构。

## 当前分工

| 层级 | 实现 |
|---|---|
| 历史数据 | Tushare Pro + 本地 CSV |
| 策略信号 | A+B+C 每日操作台 |
| 实时行情 | QMT `xtdata` |
| 资金/持仓/委托/成交 | QMT `xttrader` |
| 下单/撤单 | QMT `xttrader`，已开启 |

## 部署架构

QMT / miniQMT 只能在 Windows 上运行。项目采用双机架构：

| 机器 | 职责 |
|---|---|
| Mac（主机） | 编辑代码、Tushare 数据采集、信号生成 |
| Windows VM（UTM 虚拟机） | 运行 miniQMT 客户端、执行实盘委托 |

A_System 目录通过 UTM 共享文件夹挂载到 Windows VM 的 `Z:` 盘，代码在 Mac 修改后 Windows 即时可见。

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
⑥ A+B+C 信号生成
```

清洗阶段会显示百分比进度：

```text
清洗进度: 54.5% (6/11)，当前日期: 20260608，daily累计: 33066，limit累计: 463
```

`collect_all_data.py`、`clean_collected_data.py`、`score_limit_up_fill_probability.py` 属于关键步骤。关键步骤第一次失败会等待 10 秒自动重试一次；仍失败则停止本次流水线，不生成计划单，避免使用旧信号。

Windows 子进程日志已强制 UTF-8 输出并实时转发到主日志，PowerShell 中不应再出现中文乱码。

## 当日涨停池兜底模拟观察

如果 A+B+C 历史回放因为缺少未来 T+2 收益数据，暂时不能生成目标交易日计划，守护进程会自动调用：

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
| 09:20 | 盘前：平仓检查 + 组合状态机 + A/B/C 买入 + 策略D监控启动 |
| 14:50 | 盘中：持仓检查 |
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

### Windows 共享盘 WinError 58（已缓解）
UTM WebDAV 共享盘偶发返回 `WinError 58`，导致目录检查、CSV 写入或清洗读取失败。修复：数据采集和清洗模块对目录创建、文件存在检查、CSV 读写增加重试。

### PowerShell 中文乱码（已修复）
守护进程调用子进程时使用 `python -u` 实时输出，并设置 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。主进程读取输出时优先 UTF-8，失败再 GBK 兜底。
