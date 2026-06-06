# 最近交易日模拟盘历史观察回放

本报告使用过去 N 个本地历史交易日做模拟盘流程回放，不接实盘，不调用 QMT，不下真实订单。

## 汇总

|   start_date |   end_date |   requested_recent_days |   actual_day_count | window_requirement_met   |   historical_sim_filled_count |   review_required_count |   no_candidate_count |   position_occupied_skip_count |   planned_order_total_count |   manual_review_required_day_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   initial_equity |   final_equity |   equity_multiple |   max_drawdown | batch_daily_path                                                                                                        | batch_summary_path                                                                                                        | live_order_enabled   | verification_status                      |
|-------------:|-----------:|------------------------:|-------------------:|:-------------------------|------------------------------:|------------------------:|---------------------:|-------------------------------:|----------------------------:|-----------------------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|-----------------:|---------------:|------------------:|---------------:|:------------------------------------------------------------------------------------------------------------------------|:--------------------------------------------------------------------------------------------------------------------------|:---------------------|:-----------------------------------------|
|     20260414 |   20260514 |                      20 |                 20 | True                     |                             2 |                       0 |                   17 |                              1 |                           2 |                                  0 |        0.5 |          0.000137795 |             0.000137795 |    0.0352808 | -0.0350052 |           500000 |         499520 |          0.999041 |     -0.0350052 | /Users/user/Desktop/A_System/reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20260414_20260514_daily.csv | /Users/user/Desktop/A_System/reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20260414_20260514_summary.csv | False                | HISTORICAL_REPLAY_ONLY_NOT_FORWARD_PAPER |

## 状态分布

| operation_status       |   day_count |
|:-----------------------|------------:|
| HISTORICAL_SIM_FILLED  |           2 |
| NO_CANDIDATE           |          17 |
| POSITION_OCCUPIED_SKIP |           1 |

## 每日明细

|   signal_date | operation_status       | top_ts_code   | top_name   | manual_review_required   |   planned_order_count |   counted_account_return |     drawdown | risk_flags   |
|--------------:|:-----------------------|:--------------|:-----------|:-------------------------|----------------------:|-------------------------:|-------------:|:-------------|
|      20260414 | HISTORICAL_SIM_FILLED  | 301189.SZ     | 奥尼电子       | False                    |                     1 |               -0.0350052 | -0.0350052   | 无            |
|      20260415 | POSITION_OCCUPIED_SKIP | 301189.SZ     | 奥尼电子       | False                    |                     0 |                0         | -0.0350052   | nan          |
|      20260416 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.0350052   | nan          |
|      20260417 | HISTORICAL_SIM_FILLED  | 301319.SZ     | 唯特偶        | False                    |                     1 |                0.0352808 | -0.000959421 | 无            |
|      20260420 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260421 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260422 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260423 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260424 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260427 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260428 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260429 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260430 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260506 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260507 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260508 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260511 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260512 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260513 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |
|      20260514 | NO_CANDIDATE           | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan          |

## 口径限制

- 这是历史回放验证，不等同于未来 20 个交易日模拟盘。
- `REVIEW_REQUIRED_PLAN_ONLY` 不计入收益，避免把需要人工复核的交易默认成交。
- 即使最近 20 日结果通过，也还需要分钟 K、盘口五档、滑点和真实排队成交验证。
