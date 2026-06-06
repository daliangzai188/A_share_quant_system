# 严格版策略继续验证决策

文件作用：
1. 记录当前阶段为什么继续使用严格版策略。
2. 固化后续模拟盘、观察窗口、人工复核、分钟数据验证的统一口径。
3. 防止后续把单条件放宽测试误当成正式策略。

## 决策结论

继续使用严格版：

```text
strategy_name: a_clean_plus_exclude_star_prev0_3_bj_risk_filter
trade_mode: paper
live_trading_enabled: false
broker_adapter_enabled: false
qmt_enabled: false
allow_live_order: false
```

本阶段不采用以下放宽版本：

```text
relax_market_chain_count
relax_segment_limit_up_count
relax_fd_ratio
```

原因：最近 120 个本地交易日压力测试显示，单独放宽任一条件都会提高交易频率，但收益、胜率、最大回撤均变差。

## 严格版核心条件

```text
segment_limit_up_count_bucket == lt_5
market_chain_count_bucket == 8_15
fd_ratio_bucket == 0_5pct_1pct
排除 amount_ratio_bucket == 0_8_1_2
排除 market_segment == star
排除 prev_pct_chg_bucket == 0_3 且 market_segment == bj
排序规则：profit_source_oos_resilient
买入：T+1 开盘，动态滑点
卖出：T+2 收盘，动态滑点
仓位：账户资金 80%，单次最多 1 只
```

## 最近 120 个交易日对比

数据区间：

```text
20251112-20260514
```

| 方案 | 成交笔数 | 胜率 | 资金倍数 | 最大回撤 |
|---|---:|---:|---:|---:|
| 严格版 | 13 | 76.92% | 1.78 倍 | -5.05% |
| 放宽市场连板数 | 32 | 53.13% | 1.57 倍 | -17.43% |
| 放宽分段涨停数 | 30 | 53.33% | 1.36 倍 | -25.06% |
| 放宽封单比例 | 27 | 51.85% | 1.32 倍 | -18.82% |

## 解释

严格版的问题是低频，不是收益质量差。

单条件放宽后的现象：

```text
交易次数增加
胜率下降
平均收益下降
最大回撤扩大
资金倍数下降
```

因此，当前不能为了提高频率直接放宽主策略条件。

## 后续验证口径

后续全部基于严格版继续：

1. 每日模拟盘候选继续用严格版。
2. 人工复核继续只处理严格版选出的候选。
3. 分钟数据和盘口验证优先验证严格版成交。
4. 如果要提高频率，另开备用策略分支，不修改严格版主策略。
5. 实盘前仍必须完成分钟 K、盘口档位、集合竞价、滑点、跌停卖出和模拟盘验证。

## 风险说明

该结论只表示当前本地历史数据和模拟盘观察口径下，严格版优于单条件放宽版本。

这不代表策略可以直接实盘，也不代表未来一定盈利。
