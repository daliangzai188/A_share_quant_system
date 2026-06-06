# 模拟盘每日操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

|   signal_date | operation_status          | next_action             |   candidate_count |   selected_count |   planned_order_count | manual_review_required   |   manual_review_count | review_decision_path                                                                                                             | review_decision_status   | paper_observation_allowed   | historical_execution_found   | top_ts_code   | top_name   | risk_flags         |   planned_order_date |   planned_position_pct |   planned_amount_by_equity |   reference_price |   round_lot_shares |   execution_event_count |   position_update_count |   equity_update_count | live_order_enabled   | safety_note                      |
|--------------:|:--------------------------|:------------------------|------------------:|-----------------:|----------------------:|:-------------------------|----------------------:|:---------------------------------------------------------------------------------------------------------------------------------|:-------------------------|:----------------------------|:-----------------------------|:--------------|:-----------|:-------------------|---------------------:|-----------------------:|---------------------------:|------------------:|-------------------:|------------------------:|------------------------:|----------------------:|:---------------------|:---------------------------------|
|      20251219 | REVIEW_REQUIRED_PLAN_ONLY | 先人工复核；通过后只进入模拟观察，不进入实盘。 |                 2 |                1 |                     1 | True                     |                     1 | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_manual_review_decisions.csv | PENDING                  | False                       | False                        | 300557.SZ     | 理工光科       | LOSS_OVERLAY_WATCH |             20251222 |                    0.8 |                1.37674e+07 |             41.54 |             331400 |                       1 |                       1 |                     1 | False                | 只允许模拟观察；未完成人工复核、分钟K和盘口验证前，不允许实盘。 |

## 人工复核决策

如果 `review_decision_status=PENDING`，先打开 `review_decision_path`，把 `review_decision` 填为 `APPROVED` / `REJECTED` / `PENDING`，并填写 `review_note`。

## 单日流程输出文件

| name              | path                                                                                                                    |
|:------------------|:------------------------------------------------------------------------------------------------------------------------|
| candidates        | /Users/user/Desktop/A_System/reports/paper_trade/a_clean_exclude_star_prev0_3_bj_candidates_20251219.csv                |
| candidate_summary | /Users/user/Desktop/A_System/reports/paper_trade/a_clean_exclude_star_prev0_3_bj_candidates_20251219_summary.csv        |
| planned_orders    | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_planned_orders.csv |
| manual_review     | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_manual_review.csv  |
| executions        | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_executions.csv     |
| positions         | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_positions.csv      |
| equity_update     | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_equity_update.csv  |
| summary           | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219_summary.csv        |
| markdown          | /Users/user/Desktop/A_System/reports/paper_trade/daily_flow/a_clean_exclude_star_prev0_3_bj_20251219.md                 |

## 执行限制

- `live_order_enabled` 必须为 `False`。
- `REVIEW_REQUIRED_PLAN_ONLY` 只能进入人工复核，不能直接买入。
- 当前仍未完成分钟 K、盘口五档、集合竞价和模拟盘连续运行验证，不允许实盘。
