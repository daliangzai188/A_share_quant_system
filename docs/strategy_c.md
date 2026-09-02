# 策略C：三类逻辑分支、分支级退出正式版

最后更新：2026-09-02
正式版本：`C_THIRD_BRANCH_T2_20260902_V16`
生效日期：2026-09-02

## 一、策略定位

C是`A>C>E>D`单账户组合中的第二顺位收益进攻腿。每个信号日收盘后独立计算C，
只有A没有正式计划且账户没有未平仓占用时，C才进入下一交易日开仓计划；C有计划
时阻断E和D。

C当前有三类逻辑分支。为了把强势龙头中不同跌停环境写成明确、可审核的等值条件，
第2类逻辑分支在配置中展开为四个可执行profile，因此配置一共有六个profile，
不能把“六个profile”误解成六套互相独立的策略。

## 二、正式入选条件

### 第1分支：核心承接

```text
market_chain_count_bucket       = 15_30
segment_limit_up_count_bucket   = 40_80
first_time_detail_bucket        = 1100_1330
board_type                      = multi_open
```

分支编号：`C_BRANCH_1_CORE_REFINEMENT`
退出：信号日T冻结，T+1开盘买入，T+3收盘卖出。

### 第2分支：原强势龙头

主profile：

```text
limit_up_count_bucket           = 50_80
market_leader_rank_bucket       = rank_4_10
fd_ratio_bucket                 = 0_1pct_0_3pct
```

另保留市场排名2～3且全市场跌停分别处于`lt_5`、`5_15`、`15_30`的三个冻结
profile。四个profile统一归属：`C_BRANCH_2_STRONG_LEADER`。
退出：信号日T冻结，T+1开盘买入，T+3收盘卖出。

### 第3分支：30～50只涨停收益扩展

```text
limit_up_count_bucket           = 30_50
market_leader_rank_bucket       = rank_4_10
fd_ratio_bucket                 = 0_1pct_0_3pct
market_chain_count_bucket       IN [lt_3, 3_8, 8_15, gte_30]
amount_ratio_bucket             IN [lt_0_8, 0_8_1_2, 1_2_2, 3_5, gte_5]
```

后两个集合是显式白名单，等价于排除连板数量`15_30`和成交额倍率`2_3`；未知值
不允许开仓，不能用简单的“不等于”把缺失或新桶误放进正式策略。

分支编号：`C_BRANCH_3_PROFIT_EXPANSION`
profile编号：`C_THIRD_LIMITUP30_50_RANK4_10_FD01_03_CHAIN_NOT15_AMOUNT_NOT2_3`
退出：信号日T冻结，T+1开盘买入，**T+2收盘卖出**。

## 三、排序、风险过滤与多分支裁决

所有分支先合并，再统一执行：

```text
1. 应用C自身风险过滤，并允许被过滤候选后的下一名递补；
2. profit_source_score降序；
3. turnover_rate降序；
4. 每日只选择第一名；
5. T+1开盘按既有滑点、费用和成交规则买入；
6. 按命中分支解析T+2或T+3退出，跌停无法卖出时顺延。
```

若同一股票同时命中多个C profile，正式裁决为：

```text
minimum_signal_exit_offset_then_profile_priority
```

即先采用更早的退出周期；退出周期相同时再按profile优先级。因此只要同时命中
第3分支，就按T+2退出，与本次历史研究回放口径一致。

## 四、计划单和日志硬要求

C候选、计划单、实盘校验预览、待确认委托和持仓记录必须贯穿保存：

```text
matched_condition_profile_ids
matched_strategy_branch_ids
resolved_exit_profile_id
exit_rule
exit_signal_offset
exit_n_days
planned_exit_date
exit_rule_resolution
```

其中：

- 第1/2分支：`exit_signal_offset=3`、`exit_n_days=2`；
- 第3分支：`exit_signal_offset=2`、`exit_n_days=1`；
- `planned_exit_date`按交易日历从信号日直接计算并写入计划单；
- 实盘校验层缺少C分支编号、退出规则、合法`exit_n_days`或计划平仓日时必须拒单；
- 开仓与持仓日志必须同时显示策略腿、分支、profile、退出规则和计划平仓日。

## 五、正式回放结果

窗口：2023-09-01～2026-08-31，按真实`action_date`、费用、滑点、T+1、
涨跌停和资金占用回放。

| 口径 | 样本 | 胜率 | 平均每笔 | 中位每笔 | 复利 | 最大回撤 |
|---|---:|---:|---:|---:|---:|---:|
| 第3分支独立 | 14 | 78.57% | 5.89% | 2.95% | 2.100817倍 | -10.69% |
| 新C独立 | 75 | 74.67% | 5.33% | 5.04% | 37.653391倍 | -17.60% |
| 新ACED组合 | 184 | 75.00% | 6.00% | 4.22% | 22695.892245倍 | -19.88% |

组合实际分腿成交：`A84/C62/E27/D11`。第3分支增加20条静态计划、独立账户
实际成交14笔；受A优先、资金占用及组合冲突影响，组合总成交从177笔增至184笔，
不能把14笔独立成交直接当作组合增加14笔。

## 六、认证、运行与风险边界

正式配置：`config/strategy_config.json`中的
`paper_ab_filtered_strategy.c_strategy`。

认证命令：

```bash
python3 scripts/certify_strategy_c_third_branch_release.py
```

必须看到：

```text
validation_status=PASS
old_c_plan_exactly_unchanged=true
new_branch_factor_contract_all_passed=true
new_branch_all_hold_t2=true
old_c_all_hold_t3=true
```

正式审计目录：

```text
reports/current_portfolio_alignment/acde_c_third_branch_t2_22695_20260902_v16
```

本次第3分支是在查看同一最近三年窗口后产生的
`STRICT_DISCOVERY_POST_REVIEW_STAGE4`结果，14笔样本有限，尚无独立冻结前向证据。
用户已审核并接受该收益分布，但历史机械复利不代表未来收益或真实资金容量；实盘仍应
先用模拟盘和小资金验证分支识别、T+2平仓、滑点及跌停顺延日志。
