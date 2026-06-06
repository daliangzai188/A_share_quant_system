# 模拟盘每日操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

|   signal_date | operation_status      | next_action                   |   candidate_count |   selected_count |   planned_order_count | manual_review_required   |   manual_review_count | historical_execution_found   | top_ts_code   | top_name   | risk_flags   |   planned_order_date |   planned_position_pct |   planned_amount_by_equity |   reference_price |   round_lot_shares |   execution_event_count |   position_update_count |   equity_update_count | live_order_enabled   | safety_note                      |
|--------------:|:----------------------|:------------------------------|------------------:|-----------------:|----------------------:|:-------------------------|----------------------:|:-----------------------------|:--------------|:-----------|:-------------|---------------------:|-----------------------:|---------------------------:|------------------:|-------------------:|------------------------:|------------------------:|----------------------:|:---------------------|:---------------------------------|
|      20260417 | HISTORICAL_SIM_FILLED | 复盘历史模拟成交闭环，检查买入、卖出、收益、回撤是否合理。 |                 4 |                1 |                     1 | False                    |                     0 | True                         | 301319.SZ     | 唯特偶        | 无            |             20260420 |                    0.8 |                1.73892e+07 |             68.87 |             252400 |                       2 |                       1 |                     1 | False                | 只允许模拟观察；未完成人工复核、分钟K和盘口验证前，不允许实盘。 |

## 单日流程输出文件

| name              | path                                                                                                                    |
|:------------------|:------------------------------------------------------------------------------------------------------------------------|
| candidates        | /Users/user/Desktop/A_System/reports/paper_trade/a_clean_exclude_star_prev0_3_bj_candidates_20260417.csv                |
| candidate_summary | /Users/user/Desktop/A_System/reports/paper_trade/a_clean_exclude_star_prev0_3_bj_candidates_20260417_summary.csv        |
| planned_orders    | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_planned_orders.csv |
| manual_review     | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_manual_review.csv  |
| executions        | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_executions.csv     |
| positions         | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_positions.csv      |
| equity_update     | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_equity_update.csv  |
| summary           | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417_summary.csv        |
| markdown          | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20260417.md                 |

## 执行限制

- `live_order_enabled` 必须为 `False`。
- `REVIEW_REQUIRED_PLAN_ONLY` 只能进入人工复核，不能直接买入。
- 当前仍未完成分钟 K、盘口五档、集合竞价和模拟盘连续运行验证，不允许实盘。
