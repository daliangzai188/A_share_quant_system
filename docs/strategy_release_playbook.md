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

---

## 一、当前策略状态

当前固定观察策略：

```text
a_strict_plus_b0018_filtered
```

策略组成：

1. A 严格主策略优先。
2. 只有 A 无候选时，才启用 B0018 备用策略。
3. B0018 条件：`segment_emotion_state_bucket=warming`。
4. B0018 额外过滤：
   - `risk_flags` 包含 `封单/流通市值偏高`
   - `risk_flags` 包含 `LOSS_OVERLAY_WATCH`
   - `open_times >= 4`
5. 仓位口径：单笔 80%。
6. 卖出口径：T+2 收盘。
7. 当前仍是 paper / simulation 阶段。
8. 默认不接实盘，不调用 QMT，不下真实订单。

当前发布验证结论：

```text
PASS_PAPER_READY_REVIEW_ONLY_MINUTE_K_REQUIRED
```

含义：

```text
通过当前发布前稳定性阈值；
可以进入下一阶段模拟 / 小资金人工确认前复核；
仍不允许自动实盘；
分钟K、集合竞价、盘口五档验证仍是硬缺口。
```

---

## 二、不要每天改策略

每天观察的作用不是改策略，而是检查执行链路：

1. 今天有没有候选。
2. 候选来自 A 还是 B。
3. B 是否被风险过滤拦截。
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
5. 是否已经补齐分钟 K、集合竞价、盘口五档验证？
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
5. `b_rejected_by_filter.csv`
6. `execution_reference.csv`

判断规则：

```text
NO_SELECTED
    今天没有模拟买入计划。

REVIEW_REQUIRED_PLAN_ONLY
    有候选，但必须人工复核。
    只能进入模拟观察，不允许实盘。

B_SELECTED_HIT_RISK_REJECT_RULES
    B 有候选但被风险过滤拦截。
    不找替代标的。

HISTORICAL_SIM_FILLED
    只代表历史复盘里可形成闭环，不代表现实一定成交。
```

### 2. 每 3 个月或 6 个月执行发布验证

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_strategy_release_validation.py
```

看输出：

```text
reports/strategy_release/a_strict_plus_b0018_filtered_release_validation_summary.csv
reports/strategy_release/a_strict_plus_b0018_filtered_release_validation_windows.csv
reports/strategy_release/a_strict_plus_b0018_filtered_release_validation_gates.csv
reports/strategy_release/a_strict_plus_b0018_filtered_release_validation.md
```

如果结论是：

```text
PASS_PAPER_READY_REVIEW_ONLY_MINUTE_K_REQUIRED
```

说明当前版本可以继续进入下一阶段模拟 / 小资金人工确认前复核。

如果结论是：

```text
FAIL_RESEARCH_ONLY
```

说明当前版本不应继续发布，需要重新优化或降低风险。

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

## 五、发布验证阈值

配置位置：

```text
config/strategy_config.json
```

字段：

```text
strategy_release_validation.gates
```

当前阈值：

| 指标 | 阈值 |
|---|---:|
| 最低资金倍数 | >= 1.05 |
| 最低胜率 | >= 60% |
| 最大回撤绝对值 | <= 12% |
| 最大单笔亏损绝对值 | <= 8% |
| 最少成交笔数 | >= 5 |
| B交易占比 | <= 55% |
| 跌停阻塞卖出次数 | = 0 |

这些阈值不是收益承诺，只是发布前过滤标准。

---

## 六、当前发布验证结果

最近一次验证输出：

```text
reports/strategy_release/a_strict_plus_b0018_filtered_release_validation_summary.csv
```

当前结果：

| 窗口 | 成交 | 胜率 | 资金倍数 | 最大回撤 | 最大单笔亏损 |
|---|---:|---:|---:|---:|---:|
| 样本外 2026 | 13 | 84.62% | 1.6891 | -3.50% | -3.50% |
| 最近60日 | 8 | 87.50% | 1.2667 | -3.50% | -3.50% |
| 最近90日 | 14 | 85.71% | 1.9752 | -3.50% | -3.50% |
| 最近120日 | 17 | 82.35% | 2.2801 | -5.05% | -5.05% |

当前状态：

```text
PASS_PAPER_READY_REVIEW_ONLY_MINUTE_K_REQUIRED
```

注意：

```text
这不代表可以自动实盘。
这只代表当前固定版本通过本地发布前稳定性验证。
```

---

## 七、实盘前必须补齐的验证

当前还缺：

1. 分钟 K 路径验证。
2. 集合竞价成交验证。
3. 盘口五档买入卖出滑点验证。
4. 涨停排队能否买入验证。
5. 跌停排队能否卖出验证。
6. 小资金人工确认交易验证。
7. 连续运行日志复盘。

在这些完成前，不允许自动实盘。

如果要进入小资金人工确认阶段，必须保持：

```text
live_order_enabled = false
broker_adapter_enabled = false
qmt_enabled = false
```

---

## 八、以后让我继续时的判断规则

如果你说：

```text
按照文档继续下一步
```

我应该按下面顺序判断：

1. 先读取 `docs/strategy_release_playbook.md`。
2. 问你当前已经模拟 / 小资金运行多久。
3. 如果没有运行，先执行发布验证。
4. 如果发布验证 PASS，下一步是补分钟 K / 盘口成交真实性验证。
5. 如果发布验证 FAIL，下一步是重新优化策略。
6. 如果已经运行到季度 / 半年，下一步是重新训练和重新发布。
7. 如果只是单日观察异常，不直接改策略，先归类是数据问题、成交问题、风控问题还是策略衰减。

---

## 九、常用命令汇总

发布验证：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_strategy_release_validation.py
```

单日 A+B filtered 操作台：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10
```

历史窗口回放：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/run_paper_ab_filtered_observation_window.py --recent-days 120 --end-date 20260518
```

B 剩余风险过滤压力测试：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/stress_test_ab_filtered_b_residual_filters.py --recent-days 120 --end-date 20260518
```

---

## 十、文件职责

| 文件 | 作用 |
|---|---|
| `config/strategy_config.json` | 当前策略参数、发布验证阈值、实盘禁用开关 |
| `scripts/run_strategy_release_validation.py` | 策略发布前稳定性验证 |
| `scripts/run_paper_ab_filtered_daily_ops.py` | A+B filtered 每日模拟盘操作台 |
| `scripts/run_paper_ab_filtered_observation_window.py` | A+B filtered 历史窗口回放 |
| `scripts/stress_test_ab_filtered_b_residual_filters.py` | B 备用策略剩余风险过滤压力测试 |
| `reports/strategy_release/` | 发布验证报告 |
| `reports/paper_trade/ab_filtered_daily_ops/` | 每日模拟盘操作台输出 |
| `reports/paper_trade/ab_filtered/` | A+B filtered 窗口回放和压力测试报告 |

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
