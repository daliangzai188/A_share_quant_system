# 新 M 系列 Mac 重建与迁移

本流程重新创建 VMware Windows 11 ARM，通过网络同步项目，单独迁移运行账本，不传输完整虚拟机包、不使用移动硬盘。旧系统继续运行到新环境基础准备完成；最终切换必须使用旧系统停机后的最新快照。

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

当前运行接口以旧机实际 `.env` 为准。本次迁移使用 `QMT_TRANSPORT=qmt_inner`；`.env.example` 中的 `miniqmt` 是旧接口模板，不能直接当成本次迁移的最终接口。

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

完成模拟柜台验证后，旧交易端退出，旧机自动恢复任务及 Mac 自动启动虚拟机守护停用，旧设备退出生产同步拓扑。新拓扑收敛后，再恢复发送与接收，配置新 Mac/Windows 的监控与定时任务。禁止两套执行系统同时启用。

迁移保持现行 `A>C>E>D` 与方案甲规则，不扩大资金或修改策略参数。恢复实盘前先小资金验证。最终账本导出、Windows 停机门禁、恢复、客户端对账和模拟验收均须在真实新旧 Windows 现场完成；Mac 上的隔离测试不能代替这些步骤。

## 七、迁移工具隔离验证

在同步目录之外的仓库副本运行：

```sh
python3 -B -m unittest discover -s tests -p 'test_export_runtime_migration.py' -v
```

测试使用临时目录与模拟账本，覆盖 WAL 事务、私有执行账本、篡改检测、运行中模型锁、缺少必需文件、缺少停机标记、导出目录越界和路径穿越，不访问生产账本或真实券商。
