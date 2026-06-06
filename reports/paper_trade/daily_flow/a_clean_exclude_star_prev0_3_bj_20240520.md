# 单日模拟盘流程报告

本报告只串联本地候选生成和本地历史模拟成交更新，不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   signal_date |   candidate_count |   selected_count |   planned_order_count |   execution_event_count |   closed_position_count |   pending_position_count | top_ts_code   | top_name   | historical_execution_found   |   equity_before |   equity_after |   account_return | live_order_enabled   |
|:-------------------------------------------------|:-------------|--------------:|------------------:|-----------------:|----------------------:|------------------------:|------------------------:|-------------------------:|:--------------|:-----------|:-----------------------------|----------------:|---------------:|-----------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |      20240520 |                 1 |                1 |                     1 |                       2 |                       1 |                        0 | 300162.SZ     | 雷曼光电       | True                         |          500000 |         701508 |         0.403016 | False                |

## 候选

|   candidate_rank | planned_action   | ts_code   | name   |   profit_source_score | risk_flags   |   historical_reference_next_trade_date |   historical_reference_net_return |
|-----------------:|:-----------------|:----------|:-------|----------------------:|:-------------|---------------------------------------:|----------------------------------:|
|                1 | PLAN_BUY_T1_OPEN | 300162.SZ | 雷曼光电   |                   1.5 | 无            |                               20240521 |                          0.504783 |

## 计划委托

| paper_order_id            |   signal_date |   planned_order_date | side   | ts_code   | name   | planned_action   | order_status   |   planned_position_pct |   planned_equity |   planned_amount_by_equity |   reference_price |   estimated_shares |   round_lot_shares | risk_flags   | live_order_enabled   |
|:--------------------------|--------------:|---------------------:|:-------|:----------|:-------|:-----------------|:---------------|-----------------------:|-----------------:|---------------------------:|------------------:|-------------------:|-------------------:|:-------------|:---------------------|
| PLAN-20240520-300162.SZ-B |      20240520 |             20240521 | BUY    | 300162.SZ | 雷曼光电   | PLAN_BUY_T1_OPEN | PLAN_ONLY      |                    0.8 |           500000 |                     400000 |              7.14 |            56022.4 |              56000 | 无            | False                |

## 成交更新

| paper_execution_id           |   signal_date |   event_date | side   | ts_code   | name   | execution_status      |    price |   amount |   account_return |   equity_before |   equity_after | message                |
|:-----------------------------|--------------:|-------------:|:-------|:----------|:-------|:----------------------|---------:|---------:|-----------------:|----------------:|---------------:|:-----------------------|
| EXEC-20240520-300162.SZ-BUY  |      20240520 |     20240521 | BUY    | 300162.SZ | 雷曼光电   | HISTORICAL_SIM_FILLED |  7.14714 |   400000 |         0        |          500000 |         500000 | 使用本地审计记录生成历史模拟买入成交。    |
| EXEC-20240520-300162.SZ-SELL |      20240520 |     20240522 | SELL   | 300162.SZ | 雷曼光电   | HISTORICAL_SIM_FILLED | 10.7592  |   602759 |         0.403016 |          500000 |         701508 | 使用本地审计记录生成历史模拟卖出和资金更新。 |

## 持仓更新

|   signal_date | ts_code   | name   | position_status          |   open_date |   close_date |   account_return |   equity_after | message           |
|--------------:|:----------|:-------|:-------------------------|------------:|-------------:|-----------------:|---------------:|:------------------|
|      20240520 | 300162.SZ | 雷曼光电   | CLOSED_BY_HISTORICAL_SIM |    20240521 |     20240522 |         0.403016 |         701508 | 已按本地审计记录完成模拟买卖闭环。 |

## 资金更新

|   signal_date | equity_event          |   event_date |   equity_before |   equity_after |   account_return | message                |
|--------------:|:----------------------|-------------:|----------------:|---------------:|-----------------:|:-----------------------|
|      20240520 | HISTORICAL_SIM_FILLED |     20240522 |          500000 |         701508 |         0.403016 | 使用本地审计记录生成历史模拟卖出和资金更新。 |

## 口径限制

如果 `historical_execution_found=false`，表示该日只生成计划，不记录成交。真实模拟盘仍需后续接入分钟 K、集合竞价、盘口五档和人工确认流程后再推进。
