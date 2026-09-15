# 国金 QMT 内置 Python：完整项目迁移与切换

## 2026-09-15 目标 Windows 验收进度

- 已安装 QMT 官方 Python 库；实测内置 Python 3.6.8、SQLite 3.21.0，执行包导入成功。
- 已保存 `A_SYSTEM_QMT_INNER` 模型并以 `read_only` 启动；账户、持仓、3 笔委托及 11 笔成交查询通过。
- 原项目 1 个活动分仓与券商持仓数量一致，未完成委托和待恢复意图均为 0。
- 持仓股票的 09:30—11:30 分钟线和研究行情均取得 121 条，所查字段缺失值为 0。
- Windows 迁移与账户脱敏回归 79 项通过；真实账户未发送下单或撤单。
- 完整退出并重新登录 QMT 后，模型在“模型交易”启动，12:58 再次完成只读验收；会话实例已变化，持仓对账与行情结果一致。
- 13:03 已将 Windows 本机 `.env` 的 `QMT_TRANSPORT=qmt_inner` 持久化并备份；其他配置校验不变。原 daemon/keeper 保持人工停机，内置配置保持只读。
- 13:35 已部署人工停机硬闸：`stop_windows.py` 的同一标记会在内置执行层阻断下单和撤单；内置私有配置已预置为 `live`，迁移临时1000元限额已取消，但 daemon/keeper 仍保持人工停机，当前不会触发券商交易调用。
- 模型已勾选“终端启动后自动运行”、账户登录后延迟 10 秒；这一设置是在本次冷启动后保存的，自动运行本身仍待下一次登录验证。
- 当前尚未完成模拟柜台或小资金真实委托闭环，不能标记为生产自动交易已恢复。

证据：`reports/qmt_inner_migration/native_before_restart.json`、`embedded_python.json`、
`native_acceptance.json`、`transport_selection.json`、`tests_windows.json`、`migration_status.json`。
不得将以上只读验收描述为真实委托闭环验收通过。

## 账户隐私显示

- 项目新增 `mask_account_id/public_account_data`：账户姓名显示 `***`，资金账号显示 `***` 加末两位。
- `LiveOrderGateway.account_check` 的账户、持仓、委托、成交 CSV 使用脱敏副本；去掉可能携带身份信息的原生 `raw` 字段。
- 原连接日志、账户心跳、连接探测、健康状态与通知账户展示统一调用脱敏函数。执行配置、券商请求及恢复账本仍保留真实身份，不能以星号替代。
- QMT 2.0.8.300：已隐藏模型列表、模型交易持仓/委托/成交/策略信号中的资金账号、账号名称；持仓中的股东账号也已隐藏。
- 2026-09-15 13:18：另在“交易 → 股票交易 → 账号资金”表的“定制列”中取消资金账号、账号名称，并确认保存；页面已核对不再显示实名。原生表采用隐藏整列，未将券商姓名数据改成星号。
- 2026-09-15 13:44—13:52：继续隐藏股票交易持仓/委托/成交/任务列表、资金股份流水，以及模型交易持仓/委托/成交/策略信号中的资金账号、账号名称和股东账号。模型成交页原来显示的实名已从操作表删除。
- QMT 的登录框、账户选择框、银证划拨账户下拉框属于原生输入控件，没有“定制列”，仍可能显示完整资金账号；QMT 也没有找到“姓名显示星号、资金账号只留末两位”的设置。交易结果表和本项目输出已经脱敏，无法定制的原生选择框需要券商客户端支持。
- 旧日志、旧导出和历史备份未批量改写；上述脱敏适用于修改后的展示及新生成报告。

## 范围与当前运行结构

本迁移复用现有数据、研究、ACDE 信号、分仓、风控、成交账本和守护程序。
Windows 外部 Python 3.11 继续运行整个业务项目；只有券商接口由 QMT 内置 Python 执行。
内置代码兼容 Python 3.6，仅依赖标准库，不导入 xtquant、pandas 或项目策略模块。

```
原启动器 / keeper / 开机守护
  → trading_daemon（原有全部日常任务、ACDE、策略分仓、POV及尾盘退出）
  → IntentBrokerExecutionService（唯一串行交易通道和持久化意图）
  → QMTInnerBrokerAdapter
  → 本机签名文件请求（不经过 Syncthing，不开网络端口）
  → QMT 模型内定时回调 / Engine
  → 内置 get_trade_detail_data、passorder、cancel、行情接口
  → 真实委托和成交查询 → 原恢复器 / 策略持仓 / 通知和日志
```

内置引擎不是策略替代品，不能绕开原意图服务独立发单。
只改接口，不改变 A>C>E>D、月度冻结规则、仓位、退出、费用和策略参数。

## 全功能对应表

| 原功能 | 迁移后的执行路径 | 验证方式 |
|---|---|---|
| 数据收集、清洗、研究、报告 | 原脚本及 Tushare 保留 | 策略/配置版本哈希不变 |
| 每日流水线、选股、冻结计划 | 原 daemon 定时任务保留 | 原运行与 ACDE 对齐测试 |
| A/C/E 静态计划、D 盘中路径 | 原调度和优先级保留，D 分钟行情换内置来源 | 分钟时间/原始价格/订阅测试；客户端仍须核对完整分钟路径 |
| 单策略分仓、同股多片 | 原策略账本和加权成本保留 | position_slice_merge、position_projection_recovery |
| 限价开仓、分批执行 | 原风控和交易意图→内置 passorder | 实际文件协议端到端测试 |
| 通用执行器的最新价委托 | 原 `LATEST_PRICE` 对应内置 `prType=5`；限价固定对应11 | 类型映射及price=0仍须按实际行情校验资金和限额 |
| 部成、撤残单、补剩余数量 | 原执行服务→内置 cancel→真实终态查询 | 部成40股/撤余60股测试，不把撤单申请当终态 |
| T+1、停牌、涨跌停与资金限制 | 原风控保留，执行端再查可用资金和可卖数量 | 原安全测试+内置T+1拒单测试 |
| 到期平仓、尾盘退出/看门狗 | 原退出任务、POV和分仓核销保留 | exit_pov_safety、runtime_recovery_tools |
| 断线、超时、重启恢复 | 原恢复器+内置持久化发单标记、唯一标签 | 丢失回报/双重启动/重复提交测试 |
| 日志、告警、每日报告 | 原系统保留；新增内置交易回调审计 | 原统一执行架构及通知测试；客户端验收检查产物 |
| 自动守护重启 | keeper/开机守护继续调用原启动器，接口选择持久化在本机 .env | 冷启动验收；QMT登录和模型运行属于额外启动前提 |
| 原启动/停止指令 | `start_windows.py` 先检查内置live再解除硬闸；`stop_windows.py` 先写硬闸再停全部进程 | read_only拒启、人工停机拒绝下单/撤单测试 |
| 账户/资金/连接/交易结果查看 | `查看交易状态.cmd` / `scripts/show_trading_status.py` | 只读查询并生成脱敏JSON；不下单、不撤单、不发通知 |

## 代码修改清单

| 文件 | 方法/位置 | 操作与原因 |
|---|---|---|
| `src/qmt_adapter.py` | `QMTBrokerAdapter.from_config`，加载 .env 后 | 新增显式 `QMT_TRANSPORT=qmt_inner` 分流；旧模式兼容；新模式失败绝不回退 miniQMT |
| `src/broker_execution_service.py` | `submit_order.execute`，调用适配器处 | 替换调用参数：添加 `_execution_intent_id`，使内置回报与原持久化意图唯一对应 |
| `src/trade_recovery.py` | `normalize_broker_orders`、`_order_matches_intent` | 新增意图标识保留与精确匹配，避免同备注的执行片错误认领 |
| `scripts/trading_daemon.py` | `_qmt_connect_once`，连接成功后 | 新增内置端实际运行模式门禁，阻止只读端被当作已就绪实盘端 |
| `src/account_privacy.py` | `mask_account_id`、`public_account_data` | 新增展示脱敏：姓名统一 `***`，资金账号 `***` 加后两位；生成副本，不修改执行身份 |
| `src/live_order_gateway.py` | `account_check`，CSV 导出位置 | 替换未脱敏导出；移除展示副本中的 `raw` 扩展字段，保留数值及证券字段 |
| `scripts/trading_daemon.py`、`src/qmt_adapter.py`、`scripts/probe_qmt_connection.py` | `_mask_account` / `mask_account_id` / `mask_account` | 统一调用展示脱敏函数；旧探测不再显示资金账号前两位 |
| `scripts/trading_daemon.py`、`src/notify.py` | `_print_account_status` 及通知说明 | 替换账户心跳中独立拼接的四星号为统一脱敏函数；更新说明为三星号，通知内容和交易逻辑不变 |
| `tests/test_account_privacy.py`、`tests/test_broker_maintenance_recovery.py` | 脱敏测试 | 验证不泄露姓名/完整账号，且原始执行对象不变；相关 27 项通过 |
| `src/qmt_inner_adapter.py` | 全文件 | 新增原接口兼容适配器，复用现有账户、持仓、行情和成交归一化 |
| `src/qmt_market_data.py` | `selected_transport/InnerMarketData/get_market_data_client` | 新增研究数据接口选择及内置tick、1m、5m、日线历史帧重建；缺失值保持缺失，五档数组保持数组 |
| `qmt_inner/protocol.py` | 全文件 | 新增本机签名请求、响应、期限、会话检查及 Windows 原子替换重试 |
| `qmt_inner/engine.py` | 全文件 | 新增内置账户/持仓/行情/委托/成交/撤单实现、SQLite审计和不确定结果保护 |
| `qmt_inner/engine.py` | `runtime_gate/_submit` | 新增人工停机硬闸；`max_order_notional=0`恢复原项目仓位口径，正数仍可作为额外迁移限额 |
| `qmt_inner/entry.py` | `init/handlebar/a_system_pump/*callback/stop` | 新增 QMT 模型入口；历史bar不发单，停止回调不触碰已断开的交易连接 |
| `scripts/prepare_qmt_inner.py` | `prepare/probe/main` | 新增本机部署和只读联通检查 |
| `scripts/qmt_inner_environment.py` | `main` | 新增目标 Windows 环境检查和只读部署报告 |
| `scripts/verify_qmt_inner.py` | `main` | 新增统一迁移测试命令及机器可读报告；使用模拟券商，不发真实委托 |
| `scripts/accept_qmt_inner.py` | `main` | 新增目标客户端只读验收：连接、原分仓对账、分钟线及研究行情查询，输出验收 JSON |
| `scripts/select_qmt_inner_transport.py` | `main` | 新增验收后的本机接口选择；要求人工停机、只读连接和零对账差异，备份并核验 `.env` 只改变 `QMT_TRANSPORT`，不打开交易开关 |
| `scripts/qmt_inner_backup.py` | `backup` | 新增本机状态快照、SQLite在线备份及完整性校验；`--require-stopped`核验停止后快照 |
| `scripts/qmt_inner_audit.py` | `audit` | 新增真实持仓与策略分仓、未完成委托及待恢复意图的只读核对；完整报告仅存Windows本机 |
| `.env.example` | QMT配置段 | 新增接口选择说明，不修改实际.env |
| `scripts/probe_qmt_connection.py` | `main`，旧xtquant导入前 | 新增内置模式只读连接探测；不会再遍历mini路径和session |
| `scripts/check_qmt_live_readiness.py` | `build_report` | 内置模式改查认证心跳与账户配置，跳过不再需要的xtquant/mini路径检查 |
| `scripts/research_buy_auction_fetch.py`、`research_buy_smooth_fetch.py`、`research_exit_5m_fetch.py`、`research_exit_tick_fetch.py`、`research_exit_vol_fetch.py`、`research_strategy_d_exit_fetch.py`、`research_strategy_d_relay_fetch.py` | 各文件`main`原xtdata导入位置 | 替换为统一行情工厂；原样保留研究筛选、字段、输出、统计与失败记录 |
| `scripts/probe_strategy_d_intraday_qmt_depth.py` | `main`原xtdata导入位置 | 同上，历史深度探测也支持内置行情 |
| `README.md` | 实盘接入说明下方 | 新增完整迁移文档入口 |
| `start_qmt_inner_windows.py` | `main` | 新增明确选择内置模式的启动入口，启动前联通检查 |
| `src/qmt_inner_start_gate.py`、`start_windows.py` | `assert_selected_transport_ready`及人工启动入口 | 内置端未运行或仍只读时不清除人工停机；就绪后沿用原启动、日志、通知和keeper流程 |
| `scripts/stage_qmt_inner_live.py` | `stage/main` | 停机状态下备份私有配置、更新执行包并预置live；不清除人工停机、不启动daemon、不调用券商交易接口 |
| `scripts/show_trading_status.py`、`查看交易状态.cmd` | 全文件 | 新增只读状态面板：程序、连接、脱敏账号、资金、持仓、委托、成交及脱敏JSON |
| `tests/test_qmt_inner.py` | 全文件 | 新增协议、恢复、原交易服务端到端和Python3.6兼容测试 |
| `tests/test_qmt_inner_deployment.py` | 全文件 | 新增WAL在线备份和运行中禁止覆盖执行包的测试 |

完整代码已经在上述文件中，不需要手工拼接代码片段。没有删除原策略和旧接口实现。
旧接口保留用于审计和停服前的原运行环境，不作为内置模式失败后的自动回退。

研究脚本沿用原运行命令，按`QMT_TRANSPORT`统一选择数据接口。内置历史下载也经过模型定时回调；
批量研究只允许在daemon停止后运行，避免大批tick下载阻塞交易执行。
历史深度与字段是否实际可用，仍以国金客户端返回结果为准；不会用零值或合成行情补齐缺失数据。

## 部署（先只读）

在当前 Windows，项目路径 `C:\A_System`：

```powershell
cd C:\A_System
python scripts\prepare_qmt_inner.py
python -m unittest tests.test_qmt_inner -v
python scripts\verify_qmt_inner.py
```

部署输出：

- `%LOCALAPPDATA%\A_System\qmt_inner\config.json`：本机账户/随机 Token/模式配置，不通过同步目录传递。
- 同目录 `A_SYSTEM_QMT_INNER.py`：GBK 编码 QMT 模型入口。
- 同目录 `bundle\qmt_inner`：内置 Python 标准库执行包。
- 同目录 `spool`：本机请求、响应、心跳、唯一实例锁和 `journal.sqlite3`。

国金完整版 QMT 的“我的 → 新建策略 → Python策略”，名称填写 `A_SYSTEM_QMT_INNER`。
用以下完整入口替换编辑器的全部默认示例，点击“编译”保存：

```python
exec(open(__import__('os').path.expandvars('%LOCALAPPDATA%/A_System/qmt_inner/A_SYSTEM_QMT_INNER.py'),'rb').read())
```

该入口加载部署器生成的本机 GBK 文件及本机模块包；不要把普通 `.py` 当作 `.rzrk` 策略包导入。
编辑器“运行”用于第一步只读启动。日志应显示 `A_SYSTEM QMT INNER STARTED; mode=read_only`。
客户真实账户查询及完整交易环境仍以验收报告为准，不能只凭启动日志判断通过。
正式柜台执行入口位于“模型交易”：选择本模型、股票账号和主图代码，再配置运行模式。
编辑器“运行”和模型交易的“模拟”只生成信号；向模拟柜台或真实柜台发送委托均需模型交易的“实盘”模式。
该界面模式与本程序 `mode=read_only/live` 是两个开关，必须分别核对；内置程序保持只读时不会发送委托。
操作口径参见[迅投官方策略交易说明](https://dict.thinktrader.net/freshman/rookie.html)。
首次必须保持本机配置 `mode=read_only`；即使客户端选到实盘账户，程序也拒绝交易调用。
本程序不代填登录密码；账户仍从原 `.env` 的 `QMT_ACCOUNT_ID` 部署到本机配置。

启动后在独立终端运行：

```powershell
python scripts\prepare_qmt_inner.py --probe
python scripts\qmt_inner_audit.py
python scripts\accept_qmt_inner.py
```

预期：`status=READ_ONLY_CONNECTED`、`transport=qmt_inner`，能获取账户、持仓、委托、成交和持仓行情。
这只证明只读连接，不等于自动交易验收通过。
对账输出 `requires_reconciliation=true` 时必须按报告处理差异；工具不会改写策略账本。
模型运行时部署器拒绝覆盖代码，更新必须先停止内置模型，以免出现新旧执行包混用。

## 切换前必须做的实际核对

1. 核对原客户端持仓、可用数量、策略分仓、未完成委托、真实成交和待平仓计划。
2. 停止原 daemon/keeper 后备份运行状态及 SQLite。数据库必须通过 SQLite backup 或停机一致副本保存，不能只复制运行中的 `.sqlite3` 而遗漏 WAL。

   运行 `python scripts\qmt_inner_backup.py` 保存停机前现场，执行 `python stop_windows.py`，
   再运行 `python scripts\qmt_inner_backup.py --require-stopped`。
   备份在本机 `%LOCALAPPDATA%\A_System\qmt_inner\backups`，包含逐文件哈希清单，不含.env。
3. 同一账户只保留一个主动交易模型，避免旧 miniQMT 与新模型双开。
4. 新内置端先查询并对账。旧 miniQMT 内部委托引用和内置合同编号不是同一概念；本实现保留旧委托内部引用，撤单前解析为内置合同编号。若字段缺失、重复或碰撞，必须停止切换，不能猜编号。
5. 在模拟柜台完成开仓、部成、撤单、再次查询、平仓、策略分仓核销与冷重启。软件模拟信号模式不等于模拟柜台，不能拿“没有委托”冒充接口成功。
6. 小资金实际验证须单独安排。正式切换时 `max_order_notional=0`，表示迁移层不再额外限制金额；原项目的单票仓位、总仓位、资金、流动性、T+1、重复委托和退出风控继续生效。需要临时验收限额时可设置正数，但必须同时确认不会误挡到期平仓。
7. 只读验收通过后运行 `python scripts\select_qmt_inner_transport.py`，把本机 `.env` 的 `QMT_TRANSPORT` 持久化为 `qmt_inner`。原 `.env` 的备份只保存在 Windows 本机备份目录，不能上传或提交；脚本核验其他密钥配置不变，并保留人工停机及 `read_only`。
   当前 Windows 已通过 `stage_qmt_inner_live.py` 预置 `mode=live`，人工停机标记仍在；原 `trade_mode/live_trade/allow_buy/allow_sell` 等风控开关仍必须满足。
8. 后续继续使用原 `start_windows.py` / `stop_windows.py`。启动器只有在内置端已运行且报告live时才解除人工停机；停止器先写人工停机，内置端会立即拒绝新的下单和撤单，再停止所有外部进程。

## 日常操作（与原程序一致）

```powershell
cd C:\A_System

# 开启：恢复daemon、keeper、终端打印、通知、自动选股与交易调度
py -3.11 start_windows.py

# 关闭：先封住内置下单/撤单，再停止全部自动化进程
py -3.11 stop_windows.py

# 只读查看连接、脱敏账号、资金、持仓、委托和成交
py -3.11 scripts\show_trading_status.py
```

也可以双击 `查看交易状态.cmd`。启动窗口按原规则实时跟随筛选后的
`logs/trading_daemon.log`；需要全部日志时使用 `py -3.11 start_windows.py --full-log`。
通知仍由原 daemon 发送；程序关闭时通知状态会显示“当前不发送”，启动后自动恢复。

真实资金动作不能用本地模拟结果代替，先小资金验证，不直接上大资金。

## 失败排查

- 没有心跳：检查 QMT 模型是否启动、Python库是否完整、入口导入和本机目录权限。
- 连接后重启报会话变化：正常保护，重连并完成原系统对账再交易。
- 账户字段缺失/行情为空：保留原始错误并核对国金版本，禁止默认零资产、空仓或即时成交。
- 发单回报超时：状态未知，先查 QMT 真实委托和 `spool/journal.sqlite3`；禁止删除账本后重发。
- D分钟数据不足：补齐原始分钟历史和实时订阅；不得降级为Tick快照冒充完整路径。
- `read_only`：只读验收阶段不允许提交订单，属于明确的阶段保护。
- QMT关闭后：keeper只能重启外部业务进程，不能替代QMT登录、Python模型自动恢复和账户权限；须在客户端完成冷启动验收。

## 官方接口依据

- https://dict.thinktrader.net/innerApi/start_now.html
- https://dict.thinktrader.net/innerApi/trading_function.html
- https://dict.thinktrader.net/innerApi/data_structure.html
- https://dict.thinktrader.net/innerApi/data_function.html
- https://dict.thinktrader.net/innerApi/system_function.html

`passorder` 无返回值，必须以投资备注匹配委托并确认合同编号；`cancel` 返回只表示撤单信号已发出。
账户总市值使用内置 `m_dInstrumentValue`；股票买卖方向使用 `m_nOpType/m_nOffsetFlag`，不能把恒为48的 `m_nDirection` 当作所有股票均买入。
