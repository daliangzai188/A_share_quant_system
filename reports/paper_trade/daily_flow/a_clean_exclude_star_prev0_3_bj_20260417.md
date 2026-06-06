# 单日模拟盘流程报告

本报告只串联本地候选生成和本地历史模拟成交更新，不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   signal_date |   candidate_count |   selected_count |   planned_order_count |   execution_event_count |   closed_position_count |   pending_position_count |   manual_review_blocked_execution_count | top_ts_code   | top_name   | top_risk_flags   |   risk_warn_candidate_count |   loss_overlay_watch_candidate_count |   selected_loss_overlay_watch_count | selected_loss_overlay_watch   | loss_overlay_watch_top_codes   | manual_review_required   | manual_review_status   | manual_review_reason   | historical_execution_found   |   equity_before |   equity_after |   account_return | live_order_enabled   |
|:-------------------------------------------------|:-------------|--------------:|------------------:|-----------------:|----------------------:|------------------------:|------------------------:|-------------------------:|----------------------------------------:|:--------------|:-----------|:-----------------|----------------------------:|-------------------------------------:|------------------------------------:|:------------------------------|:-------------------------------|:-------------------------|:-----------------------|:-----------------------|:-----------------------------|----------------:|---------------:|-----------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |      20260417 |                 4 |                1 |                     1 |                       2 |                       1 |                        0 |                                       0 | 301319.SZ     | 唯特偶        | 无                |                           0 |                                    0 |                                   0 | False                         |                                | False                    | NOT_REQUIRED           |                        | True                         |     2.17365e+07 |    2.25033e+07 |        0.0352808 | False                |

## 候选

|   candidate_rank | planned_action   | ts_code   | name   |   profit_source_score | risk_flags   |   historical_reference_next_trade_date |   historical_reference_net_return |
|-----------------:|:-----------------|:----------|:-------|----------------------:|:-------------|---------------------------------------:|----------------------------------:|
|                1 | PLAN_BUY_T1_OPEN | 301319.SZ | 唯特偶    |                   3.8 | 无            |                               20260420 |                         0.0579453 |
|                2 | WATCH_ONLY       | 300776.SZ | 帝尔激光   |                   3   | 无            |                               20260420 |                        -0.0561299 |
|                3 | WATCH_ONLY       | 300905.SZ | 宝丽迪    |                   3   | 无            |                               20260420 |                        -0.133917  |
|                4 | WATCH_ONLY       | 301237.SZ | 和顺科技   |                   1.3 | 无            |                               20260420 |                        -0.0153313 |

## 计划委托

| paper_order_id            |   signal_date |   planned_order_date | side   | ts_code   | name   | planned_action   | order_status   |   planned_position_pct |   planned_equity |   planned_amount_by_equity |   reference_price |   estimated_shares |   round_lot_shares | risk_flags   | manual_review_required   | manual_review_status   | manual_review_reason   | live_order_enabled   |
|:--------------------------|--------------:|---------------------:|:-------|:----------|:-------|:-----------------|:---------------|-----------------------:|-----------------:|---------------------------:|------------------:|-------------------:|-------------------:|:-------------|:-------------------------|:-----------------------|:-----------------------|:---------------------|
| PLAN-20260417-301319.SZ-B |      20260417 |             20260420 | BUY    | 301319.SZ | 唯特偶    | PLAN_BUY_T1_OPEN | PLAN_ONLY      |                    0.8 |      2.17365e+07 |                1.73892e+07 |             68.87 |             252493 |             252400 | 无            | False                    | NOT_REQUIRED           |                        | False                |

## 人工确认清单

无人工确认项。

## 成交更新

| paper_execution_id           |   signal_date |   event_date | side   | ts_code   | name   | execution_status      |   price |      amount |   account_return |   equity_before |   equity_after | message                |
|:-----------------------------|--------------:|-------------:|:-------|:----------|:-------|:----------------------|--------:|------------:|-----------------:|----------------:|---------------:|:-----------------------|
| EXEC-20260417-301319.SZ-BUY  |      20260417 |     20260420 | BUY    | 301319.SZ | 唯特偶    | HISTORICAL_SIM_FILLED | 69.2143 | 1.73892e+07 |        0         |     2.17365e+07 |    2.17365e+07 | 使用本地审计记录生成历史模拟买入成交。    |
| EXEC-20260417-301319.SZ-SELL |      20260417 |     20260421 | SELL   | 301319.SZ | 唯特偶    | HISTORICAL_SIM_FILLED | 72.3789 | 1.83679e+07 |        0.0352808 |     2.17365e+07 |    2.25033e+07 | 使用本地审计记录生成历史模拟卖出和资金更新。 |

## 持仓更新

|   signal_date | ts_code   | name   | position_status          |   open_date |   close_date |   account_return |   equity_after | message           |
|--------------:|:----------|:-------|:-------------------------|------------:|-------------:|-----------------:|---------------:|:------------------|
|      20260417 | 301319.SZ | 唯特偶    | CLOSED_BY_HISTORICAL_SIM |    20260420 |     20260421 |        0.0352808 |    2.25033e+07 | 已按本地审计记录完成模拟买卖闭环。 |

## 资金更新

|   signal_date | equity_event          |   event_date |   equity_before |   equity_after |   account_return | message                |
|--------------:|:----------------------|-------------:|----------------:|---------------:|-----------------:|:-----------------------|
|      20260417 | HISTORICAL_SIM_FILLED |     20260421 |     2.17365e+07 |    2.25033e+07 |        0.0352808 | 使用本地审计记录生成历史模拟卖出和资金更新。 |

## 口径限制

如果 `historical_execution_found=false`，表示该日只生成计划，不记录成交。真实模拟盘仍需后续接入分钟 K、集合竞价、盘口五档和人工确认流程后再推进。
