# 最近交易日模拟盘历史观察回放

本报告使用过去 N 个本地历史交易日做模拟盘流程回放，不接实盘，不调用 QMT，不下真实订单。

## 汇总

|   start_date |   end_date |   requested_recent_days |   actual_day_count | window_requirement_met   |   historical_sim_filled_count |   review_required_count |   no_candidate_count |   position_occupied_skip_count |   planned_order_total_count |   manual_review_required_day_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   initial_equity |   final_equity |   equity_multiple |   max_drawdown | batch_daily_path                                                                                                        | batch_summary_path                                                                                                        | live_order_enabled   | verification_status                      |
|-------------:|-----------:|------------------------:|-------------------:|:-------------------------|------------------------------:|------------------------:|---------------------:|-------------------------------:|----------------------------:|-----------------------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|-----------------:|---------------:|------------------:|---------------:|:------------------------------------------------------------------------------------------------------------------------|:--------------------------------------------------------------------------------------------------------------------------|:---------------------|:-----------------------------------------|
|     20251112 |   20260514 |                     120 |                120 | True                     |                            13 |                       3 |                  100 |                              4 |                          16 |                                  3 |   0.769231 |            0.0473658 |               0.0397618 |     0.181313 | -0.0505146 |           500000 |         890569 |           1.78114 |     -0.0505146 | /Users/user/Desktop/A_System/reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20251112_20260514_daily.csv | /Users/user/Desktop/A_System/reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20251112_20260514_summary.csv | False                | HISTORICAL_REPLAY_ONLY_NOT_FORWARD_PAPER |

## 状态分布

| operation_status          |   day_count |
|:--------------------------|------------:|
| HISTORICAL_SIM_FILLED     |          13 |
| NO_CANDIDATE              |         100 |
| POSITION_OCCUPIED_SKIP    |           4 |
| REVIEW_REQUIRED_PLAN_ONLY |           3 |

## 每日明细

|   signal_date | operation_status          | top_ts_code   | top_name   | manual_review_required   |   planned_order_count |   counted_account_return |     drawdown | risk_flags         |
|--------------:|:--------------------------|:--------------|:-----------|:-------------------------|----------------------:|-------------------------:|-------------:|:-------------------|
|      20251112 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251113 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251114 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251117 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251118 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251119 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251120 | HISTORICAL_SIM_FILLED     | 301092.SZ     | 争光股份       | False                    |                     1 |               -0.0505146 | -0.0505146   | 无                  |
|      20251121 | POSITION_OCCUPIED_SKIP    | 301171.SZ     | 易点天下       | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251124 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251125 | REVIEW_REQUIRED_PLAN_ONLY | 301117.SZ     | 佳缘科技       | True                     |                     1 |                0         | -0.0505146   | LOSS_OVERLAY_WATCH |
|      20251126 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251127 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251128 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251201 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251202 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251203 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0505146   | nan                |
|      20251204 | HISTORICAL_SIM_FILLED     | 300946.SZ     | 恒而达        | False                    |                     1 |                0.0291749 | -0.0228134   | 无                  |
|      20251205 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0228134   | nan                |
|      20251208 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0228134   | nan                |
|      20251209 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0228134   | nan                |
|      20251210 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0228134   | nan                |
|      20251211 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0228134   | nan                |
|      20251212 | HISTORICAL_SIM_FILLED     | 920576.BJ     | 天力复合       | False                    |                     1 |                0.181313  |  0           | 无                  |
|      20251215 | POSITION_OCCUPIED_SKIP    | 920665.BJ     | 科强股份       | False                    |                     0 |                0         |  0           | nan                |
|      20251216 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251217 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251218 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251219 | REVIEW_REQUIRED_PLAN_ONLY | 300557.SZ     | 理工光科       | True                     |                     1 |                0         |  0           | LOSS_OVERLAY_WATCH |
|      20251222 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251223 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251224 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251225 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251226 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251229 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251230 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20251231 | HISTORICAL_SIM_FILLED     | 300058.SZ     | 蓝色光标       | False                    |                     1 |                0.130855  |  0           | 无                  |
|      20260105 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260106 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260107 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260108 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260109 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260112 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260113 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260114 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260115 | HISTORICAL_SIM_FILLED     | 301629.SZ     | 矽电股份       | False                    |                     1 |               -0.0338314 | -0.0338314   | 无                  |
|      20260116 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0338314   | nan                |
|      20260119 | HISTORICAL_SIM_FILLED     | 300658.SZ     | 延江股份       | False                    |                     1 |                0.0681174 |  0           | 无                  |
|      20260120 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260121 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260122 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260123 | HISTORICAL_SIM_FILLED     | 920368.BJ     | 连城数控       | False                    |                     1 |                0.102773  |  0           | 无                  |
|      20260126 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260127 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260128 | HISTORICAL_SIM_FILLED     | 300164.SZ     | 通源石油       | False                    |                     1 |                0.0457726 |  0           | 无                  |
|      20260129 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260130 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260202 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260203 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260204 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260205 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260206 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260209 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260210 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260211 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260212 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260213 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260224 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260225 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260226 | HISTORICAL_SIM_FILLED     | 300499.SZ     | 高澜股份       | False                    |                     1 |                0.0193949 |  0           | 无                  |
|      20260227 | POSITION_OCCUPIED_SKIP    | 301226.SZ     | 祥明智能       | False                    |                     0 |                0         |  0           | nan                |
|      20260302 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260303 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260304 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260305 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260306 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260309 | REVIEW_REQUIRED_PLAN_ONLY | 301606.SZ     | 绿联科技       | True                     |                     1 |                0         |  0           | LOSS_OVERLAY_WATCH |
|      20260310 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260311 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260312 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260313 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260316 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260317 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260318 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260319 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260320 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260323 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260324 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260325 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260326 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260327 | HISTORICAL_SIM_FILLED     | 300204.SZ     | 舒泰神        | False                    |                     1 |                0.0397618 |  0           | 无                  |
|      20260330 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260331 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260401 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260402 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260403 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260407 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260408 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260409 | HISTORICAL_SIM_FILLED     | 300489.SZ     | 光智科技       | False                    |                     1 |                0.0826627 |  0           | 无                  |
|      20260410 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260413 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         |  0           | nan                |
|      20260414 | HISTORICAL_SIM_FILLED     | 301189.SZ     | 奥尼电子       | False                    |                     1 |               -0.0350052 | -0.0350052   | 无                  |
|      20260415 | POSITION_OCCUPIED_SKIP    | 301189.SZ     | 奥尼电子       | False                    |                     0 |                0         | -0.0350052   | nan                |
|      20260416 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.0350052   | nan                |
|      20260417 | HISTORICAL_SIM_FILLED     | 301319.SZ     | 唯特偶        | False                    |                     1 |                0.0352808 | -0.000959421 | 无                  |
|      20260420 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260421 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260422 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260423 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260424 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260427 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260428 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260429 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260430 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260506 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260507 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260508 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260511 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260512 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260513 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |
|      20260514 | NO_CANDIDATE              | nan           | nan        | False                    |                     0 |                0         | -0.000959421 | nan                |

## 口径限制

- 这是历史回放验证，不等同于未来 20 个交易日模拟盘。
- `REVIEW_REQUIRED_PLAN_ONLY` 不计入收益，避免把需要人工复核的交易默认成交。
- 即使最近 20 日结果通过，也还需要分钟 K、盘口五档、滑点和真实排队成交验证。
