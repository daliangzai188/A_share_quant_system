# 策略发布与后续操作手册

本文档用于指导后续操作。以后你让我“先读文档”时，优先读取：

```text
AGENTS.md
README.md
docs/strategy_release_playbook.md
```

核心原则：

```text
固定策略版本
发布前验证
运行期不随意改参数
季度或半年重新训练和重新发布
先模拟，后小资金，再考虑扩大
```

### 固定组合腿序标准

所有后续研究、优化、回测、认证和生产执行统一使用：

```text
A > C > E > D
```

该顺序是组合层不变量，不是每次半年优化时重新搜索的参数。A作为规则较简单、
持有周期较短的低热度主策略优先；C作为强势环境和龙头形态补充；E第三；D只有
A/C/E均无当日静态计划时才启动盘中路径。A优先表示模型稳健性和执行风险优先，
不表示A的历史回撤小于C。

每次优化必须执行：

```text
冻结A>C>E>D
    ↓
按真实action_date和退出日回放单账户资金
    ↓
先检查被优化策略的独立指标
    ↓
再检查只替换该策略后的A>C>E>D组合指标
    ↓
复核样本、回撤、最大亏损、连续亏损、成交、滑点和过拟合风险
    ↓
通过发布认证后才允许建立新版本
```

禁止为了提高历史复利把腿序改成`A>E>C>D`或其他排列，禁止使用`signal_date`把D与
收盘后才能知道的A/C/E候选同日重排，也禁止低优先级策略绕过高优先级正式计划。
其他腿序结果只能用于诊断。除非用户以后明确推翻本标准并完成独立顺序发布流程，
普通因子、阈值、过滤、排序和退出优化都不得修改`A>C>E>D`。

### 当前model=3冻结发布门禁

当前实盘组合除了认证文件的配置/代码/输入哈希外，还必须通过
`config/strategy_release_freeze.json`。认证脚本允许刷新认证时效，但不会自动改写冻结清单；
因此同一历史窗口重新调参、改腿序或更换候选代码后，即使回测认证重新通过，也会先被实盘
买入门禁阻断，直到人工明确发布新版本并提交冻结清单。

发布新版本的固定顺序：

```text
1. 完成训练/测试/样本外、成交、回撤和容量复核
2. python scripts/certify_strict_asof_portfolio.py
3. python scripts/freeze_current_strategy_release.py \
     --release-id <唯一版本号> \
     --change-reason <至少8个字符的发布原因> \
     --oos-start-date <晚于研究截止日的YYYYMMDD> \
     --baseline-commit <策略行为基线提交> \
     --replace
4. 运行全量测试并提交认证文件、冻结清单和代码
```

`oos_start_date`之后的结果只进入滚动真实成交报告，不得用于回填、改写本次冻结清单；
如果根据这些新结果调参，必须产生新的`release_id`和新的样本外起点。

### 真实容量与TCA监控

收盘流水线先重建`reports/execution_tracking/trade_completion_summary.csv`，再运行：

```text
python scripts/report_rolling_live_performance.py
```

报告的“真实执行容量与TCA”只使用开仓前已冻结的`LIVE_FROZEN`计划。上线前历史交易的
目标数量由真实持仓反推，统一标为`BACKFILLED`，只能用于收益和成交审计，不能证明容量。
容量状态使用以下数据：

```text
真实冻结计划数
买入98%以上完成率
平均及P10买入完成率
退出完整率和隔夜残量
开盘/收盘基准覆盖率
买入、卖出和总滑点均值/P90
超过计划数量的异常成交
```

当前`capacity_review.enforce_live_gate=false`，容量状态只监控、不阻断实盘下单；达到样本门槛
也只是允许进入人工容量复核，必须确认报告为`PASS`后才可称为容量认证通过。

### 事务型执行事件镜像

执行审计继续以`positions.json`和`reports/execution_tracking/*.csv`为权威口径，同时把以下事件
镜像到`data/state/execution_events.sqlite3`：

```text
PLAN：开仓前冻结计划及状态修订
BUY：每一笔买入委托/成交片段及后续修订
SELL：每一笔卖出委托/成交片段及后续修订
```

SQLite使用WAL、`synchronous=FULL`和逐事件事务。同一事件相同内容重复写入保持幂等；成交数量、
价格或状态发生变化时追加revision，旧版本不覆盖。镜像延迟初始化，daemon中的记录调用已有异常
隔离：SQLite异常只告警，不改变下单、撤单、持仓回写或平仓结果。

以下命令会从现有CSV幂等重建镜像并写出完整性报告：

```text
python scripts/update_execution_completion.py --rebuild-only
```

应看到`integrity_check=ok`、`mirror_complete=true`、`missing_event_uids=[]`。账本允许保留已从当前
CSV视图移出的旧事件，因此`retained_history_head_count>0`不属于错误。

---

## 一、当前策略状态

当前固定观察策略：

```text
a_strict_plus_c_leader_union_hold3
```

历史操作台和报告文件名继续保留`a_strict_plus_c_hold3`前缀，以兼容既有账本读取；
策略身份以配置中的`strategy_label`和C的`release_id`为准，不能依据文件名前缀判断版本。

策略组成：

1. A 严格主策略优先。
2. B 已于 2026-07-22 删除，不再参与候选、买入或自动卖出。
3. A 无候选时直接检查 C。
4. C正式版本为`C_LEADER_UNION_20260630_V1`，按任一分支命中：
   - 核心精修：`market_chain_count_bucket=15_30`、`segment_limit_up_count_bucket=40_80`、`first_time_detail_bucket=1100_1330`、`board_type=multi_open`；
   - 强势龙头：`limit_up_count_bucket=50_80`、`market_leader_rank_bucket=rank_4_10`、`fd_ratio_bucket=0_1pct_0_3pct`。
5. C 使用自己的风险过滤：`封单/流通市值偏高`、`LOSS_OVERLAY_WATCH`，以及按板高分档的炸板次数限制。
6. C 卖出口径仍为 T+3 收盘；A按自己的既有退出口径执行。
7. D、E按当前组合资金占用规则继续参与；M、N已退役。
8. 仅人工退出的历史 B 仓存在时，所有新开仓均阻断。

当前C规则发布状态：

```text
C_LEADER_UNION_20260630_V1
effective_from=20260825
```

发布依据：在固定`20240630~20260630`窗口中，C独立复利由3.110831倍提高到
23.617616倍，只替换C后的D>A>E>C总复利由486.366143倍提高到921.336502倍，
同时最大回撤由-22.9705%改善到-15.3995%，满足半年更新框架的双复利门槛。

发布边界：

```text
排序仍为profit_source_score、turnover_rate降序；
退出仍为T+3收盘；
风险过滤必须先于最终选股并允许下一名递补；
更早6个月和发布后前向账本不反向改写本次24个月选择；
历史机械复利不代表未来收益或真实资金容量。
```

详细删除范围、持仓处理和部署检查见：

```text
docs/strategy_b_removal_20260722.md
```

---

## 二、不要每天改策略

每天观察的作用不是改策略，而是检查执行链路：

1. 今天有没有候选。
2. 候选来自 A 还是 C；如果日志出现 B 候选，视为旧进程/旧文件异常。
3. B/C 是否被风险过滤拦截。
4. 是否生成计划委托。
5. 是否需要人工复核。
6. 是否存在无法成交、无法卖出、滑点异常等执行问题。

运行期间禁止因为某一天亏损或没候选就改参数。

正确节奏：

```text
发布一个固定策略版本
运行一个周期
记录执行结果
到季度或半年重新验证
验证通过再发布新版本
```

---

## 三、后续你让我读文档时，我应该先问什么

以后你说“读取文档，告诉我下一步”时，我应该先确认以下问题：

1. 当前是否已经开始模拟盘或小资金实盘？
2. 如果开始了，已经运行了多少个交易日？
3. 最近一次策略发布验证是什么日期？
4. 当前是否要继续执行当前版本，还是准备季度 / 半年重新训练？
5. C 的涨停排队买不到、跌停排队卖不出是否仍然按保守口径生效？
6. 最近是否出现连续亏损、无法成交、跌停无法卖出、滑点异常？
7. 当前目标是：
   - 继续执行固定策略
   - 跑发布验证
   - 重新寻找最佳策略
   - 做成交真实性验证
   - 准备小资金人工确认

如果你没有给这些信息，我应该先问你当前实盘 / 模拟盘运行状态，而不是直接改策略。

---

## 四、固定操作流程

### 1. 每天收盘后的固定操作

只运行当前固定策略版本，不改参数：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10
```

看输出：

```text
reports/paper_trade/ab_filtered_daily_ops/
```

重点检查：

1. `checklist.csv`
2. `selected.csv`
3. `planned_orders.csv`
4. `manual_review.csv`
5. `c_rejected_by_filter.csv`
6. `execution_reference.csv`

判断规则：

```text
NO_SELECTED
    今天没有模拟买入计划。

REVIEW_REQUIRED_PLAN_ONLY
    有候选，但必须人工复核。
    只能进入模拟观察，不允许实盘。

C_SELECTED_HIT_RISK_REJECT_RULES
    C 有候选但被风险过滤拦截。
    不找替代标的。

HISTORICAL_SIM_FILLED
    只代表历史复盘里可形成闭环，不代表现实一定成交。
```

### 2. 删除B后的当前严格认证

旧A+B验证器只保留为失效保护，运行时应明确拒绝：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_strategy_release_validation.py
```

当前正式规则身份与机械回放改由以下命令复核：

```bash
cd /Users/user/Desktop/A_System
python3 scripts/certify_strict_asof_portfolio.py
```

它必须复现A独立63笔、18.9115486868倍、E独立74笔、11.7037898965倍、
C独立55笔、23.6176160942倍，以及真实开仓日A>C>E>D组合136笔、
1023.7912439628倍、分腿A42/C47/E36/D11。用户已明确接受该组合低于
A>E>C>D的1164.500295倍。该证书仍标记
`STRICT_DISCOVERY`：它证明配置、代码、输入和本次规则能够重放，但不等于冻结
样本外或真实容量已经通过；研究证书不参与实盘BUY开关，真实订单继续由执行风控拦截。

### 3. 什么时候重新找最新策略

只有以下情况才建议重新寻找最佳策略：

1. 到季度或半年更新周期。
2. 发布验证失败。
3. 样本外窗口明显衰减。
4. 最大回撤超过配置阈值。
5. 连续亏损超过可接受范围。
6. 市场制度或数据口径发生明显变化。
7. 成交真实性验证显示当前策略无法买入或无法卖出。

不因为单日亏损、单日无候选、单日错过上涨而改策略。

---

## 五、旧A+B验证阈值（仅历史兼容）

配置位置：

```text
config/strategy_config.json
```

字段：

```text
strategy_release_validation.gates
```

以下字段只属于已停用的A+B验证器，不能用来判断当前C版本：

| 指标 | 阈值 |
|---|---:|
| 最低资金倍数 | >= 1.05 |
| 最低胜率 | >= 60% |
| 最大回撤绝对值 | <= 12% |
| 最大单笔亏损绝对值 | <= 8% |
| 最少成交笔数 | >= 5 |
| 跌停阻塞卖出次数 | = 0 |

这些阈值不是收益承诺，也不是当前C双复利门槛。

---

## 六、当前发布验证结果

当前策略规则状态：

```text
C_LEADER_UNION_20260630_V1_RULE_LANDED
STRICT_ASOF_REPLAY_PASSED
LOCKED_OOS_AND_CAPACITY_PENDING
```

旧A+B窗口结果继续归档，不得显示为当前PASS。当前C规则已进入正式配置和候选流水线，
严格两年重放通过；冻结版本从2026-08-25起积累前向样本外。全局自动BUY是否开放仍由
冻结清单、真实容量、模拟/小资金验证和执行风控单独决定，本次C规则替换不修改这些开关。

---

## 七、实盘前必须补齐的验证

当前还缺：

1. 涨停排队能否买入验证：当前 C 使用日线保守口径，T+1 开盘涨停直接视为买不到。
2. 跌停排队能否卖出验证：当前 C 使用日线保守口径，卖出日跌停视为排队卖不出并顺延。
3. 集合竞价成交验证。
4. 盘口五档买入卖出滑点验证。
5. 分钟 K 路径验证：C 当前不强制，但有数据后可作为更高精度复核。
6. 小资金人工确认交易验证。
7. 连续运行日志复盘。

在这些完成前，不得因为历史复利提高而自动扩大资金。进入小资金人工确认阶段前，
必须单独复核当前部署开关、冻结清单、账户和风控，且所有订单继续经过
`LiveOrderGateway`；本次C规则替换不修改任何交易开关。

---

## 八、以后让我继续时的判断规则

如果你说：

```text
按照文档继续下一步
```

我应该按下面顺序判断：

1. 先读取 `docs/strategy_release_playbook.md`。
2. 问你当前已经模拟 / 小资金运行多久。
3. 运行严格认证，确认C独立与ACDE组合锚点没有漂移；不能运行旧A+B验证器代替。
4. 严格重放通过后，下一步是积累冻结版本前向账本并复核真实成交、容量和滑点。
5. 如果严格认证失败，先定位数据、代码或输入清单漂移，不直接放宽门槛。
6. 如果已经运行到6月30日或12月31日更新节点，再按最新两年重新研究并生成新版本。
7. 如果只是单日观察异常，不直接改策略，先归类是数据问题、成交问题、风控问题还是策略衰减。

---

## 九、常用命令汇总

当前严格认证：

```bash
cd /Users/user/Desktop/A_System
python3 scripts/certify_strict_asof_portfolio.py
```

旧A+B验证器失效保护：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_strategy_release_validation.py
```

单日 A/C filtered 操作台：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10
```

旧 B 历史窗口回放（当前配置应拒绝运行，只保留审计代码）：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_paper_ab_filtered_observation_window.py --recent-days 120 --end-date 20260518
```

B 历史风险过滤压力测试（研究归档，不属于当前发布）：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/stress_test_ab_filtered_b_residual_filters.py --recent-days 120 --end-date 20260518
```

---

## 十、文件职责

**当前 A/C 与 B 退役相关文件：**

| 文件 | 作用 |
|---|---|
| `config/strategy_config.json` | A/C当前参数、B退役墓碑、失效的旧发布标记 |
| `scripts/run_strategy_release_validation.py` | 旧A+B发布验证失效保护；当前仍应拒绝运行 |
| `scripts/certify_strict_asof_portfolio.py` | 当前A>C>E>D真实开仓日严格as-of机械回放与输入/代码哈希证书 |
| `scripts/run_paper_ab_filtered_daily_ops.py` | A/C filtered 每日操作台 |
| `scripts/run_paper_ab_filtered_observation_window.py` | B历史回放代码；当前配置拒绝运行 |
| `scripts/stress_test_ab_filtered_b_residual_filters.py` | B历史研究归档，不属于当前执行链 |
| `scripts/refine_backup_strategy_c_sort_exit.py` | C 备用策略排序和卖出规则精修 |
| `docs/strategy_b_removal_20260722.md` | B删除范围、验证和部署记录 |
| `docs/strategy_c.md` | C两分支OR正式条件、排序、退出、指标与发布边界 |
| `reports/strategy_release/` | 发布验证报告 |
| `reports/paper_trade/ab_filtered_daily_ops/` | 每日模拟盘操作台输出 |
| `reports/paper_trade/ab_filtered/` | 删除B前的历史窗口回放和压力测试报告 |
| `reports/paper_trade/backup_strategy_c/` | C 历史搜索与精修报告 |

**D 策略相关文件：**

| 文件 | 作用 |
|---|---|
| `scripts/backtest_strategy_d.py` | D 历史叠加回测；删除B后必须重新生成当前口径 |
| `scripts/monitor_strategy_d_intraday.py` | D 策略盘中监控：两档信号 + 14:55 自动撤单 |
| `scripts/collect_strategy_d_minute_data.py` | D 候选历史分钟数据采集（辅助人工复核） |
| `scripts/trading_daemon.py` | 守护进程：13:30 启动 D 监控子进程 |
| `src/broker_adapter.py` | 新增 `cancel_order` 抽象接口 |
| `src/qmt_adapter.py` | 实现 `cancel_order`（调用 `cancel_order_stock`） |
| `data/processed/next_day_premium_trades.csv` | D 策略回测数据源（近 2 年首板次日溢价记录）|
| `reports/strategy_d/` | D 策略回测和盘中信号报告输出目录 |
| `docs/strategy_d.md` | D 策略完整设计文档 |

---

## 十一、风险说明

本文档中的任何 PASS 都不是盈利保证。

PASS 的含义只是：

```text
在当前本地数据、当前成交模型、当前阈值下，通过发布前检查。
```

仍可能存在：

1. 数据偏差。
2. 样本不足。
3. 盘口真实成交失败。
4. 滑点扩大。
5. 市场环境变化。
6. 策略衰减。
7. 过拟合风险。

因此实盘前必须先小资金、人工确认、逐步验证。
