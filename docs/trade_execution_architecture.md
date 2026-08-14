# 统一交易执行架构

## 目标

策略层只负责产生交易意图；任何策略都不能直接拥有QMT连接或自行决定重启后如何补单。
实盘执行统一经过以下链路：

```text
策略候选与让路规则
        ↓
持久化交易意图（PLANNED）
        ↓
统一校验与执行状态机
        ↓
唯一串行QMT执行通道
        ↓
券商委托 / 成交 / 持仓
        ↓
券商真实状态驱动的账本投影与重启恢复
```

## 唯一状态源

`data/state/execution_events.sqlite3`是统一事务数据库：

- `trade_intents`：下单、撤单、成交确认和恢复的当前权威状态；
- `trade_intent_transitions`：不可变状态迁移历史；
- `trade_recovery_runs`：每次券商对账恢复的输入摘要和结果；
- 原有`execution_events`：继续保存计划、买入片段、卖出片段的不可变审计历史。

`positions.json`和执行跟踪CSV在全量迁移完成后只作为人类可读投影，不再独立决定是否
重复下单。升级前产生的历史持仓仍按兼容路径管理，直至全部退出。

## 状态机

```text
PLANNED → VALIDATED → PREPARED → SUBMITTING → SUBMITTED
                                                  ├→ PARTIALLY_FILLED → FILLED
                                                  ├→ CANCEL_REQUESTED → CANCELLED
                                                  └→ RECOVERY_REQUIRED
```

所有状态更新必须满足：

1. SQLite事务提交成功后才能进入下一步；
2. 版本号CAS防止并发覆盖；
3. 券商单号一经绑定不得改绑；
4. 成交数量和金额只能单调增加；
5. 终态不能被滞后快照回退；
6. PREPARED/SUBMITTING未知结果必须先查券商，不得盲目重发。

## 分阶段迁移

1. 建立统一意图与事务账本，不改变实盘行为；
2. 建立唯一串行QMT执行服务；
3. 全部策略开仓、平仓和撤单接入意图状态机；
4. 启动时统一读取券商委托、成交和持仓恢复状态；
5. keeper仅判断进程/PID/心跳是否存活，QMT账户和交易状态全部归daemon执行层。

每个阶段单独测试、单独提交。任何阶段不得改变D>L>A>M>E2>C、82.5%目标仓位、85%硬顶、
策略候选条件或计划平仓日。

## 唯一QMT通道约束

`BrokerExecutionService`内部只有一个FIFO工作线程。策略线程、账户心跳、D盘中行情、开仓、
平仓和看门狗即使同时发起请求，也只能依序进入原始QMT adapter。命令超时只表示调用方没有
及时得到结果，不表示券商没有受理；下单超时必须进入`RECOVERY_REQUIRED`并通过券商委托查询
恢复，禁止直接重发。
