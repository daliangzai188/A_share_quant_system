# 多日模拟盘批量流程报告

本报告按日期区间批量串联本地候选生成、计划委托、历史模拟成交、持仓和资金更新。不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   start_date |   end_date |   trade_day_count |   closed_trade_day_count |   no_candidate_day_count |   pending_day_count |   manual_review_blocked_day_count |   position_occupied_skip_day_count |   manual_review_required_day_count |   initial_equity |   final_equity |   equity_multiple |   win_rate |   closed_trade_win_rate |   positive_day_rate |   avg_account_return |   median_account_return |   avg_daily_account_return |   median_daily_account_return |   max_profit |   max_loss |   max_drawdown |   risk_event_count | live_order_enabled   |
|:-------------------------------------------------|:-------------|-------------:|-----------:|------------------:|-------------------------:|-------------------------:|--------------------:|----------------------------------:|-----------------------------------:|-----------------------------------:|-----------------:|---------------:|------------------:|-----------:|------------------------:|--------------------:|---------------------:|------------------------:|---------------------------:|------------------------------:|-------------:|-----------:|---------------:|-------------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |     20260414 |   20260514 |                20 |                        2 |                       17 |                   0 |                                 0 |                                  1 |                                  0 |       2.2525e+07 |    2.25033e+07 |          0.999041 |        0.5 |                     0.5 |                0.05 |          0.000137795 |             0.000137795 |                1.37795e-05 |                             0 |    0.0352808 | -0.0350052 |     -0.0350052 |                  0 | False                |

## 每日状态预览

|   signal_date | daily_status             |   candidate_count |   selected_count | top_ts_code   | top_name   | top_risk_flags   |   selected_loss_overlay_watch |   manual_review_required |   manual_review_blocked_execution_count | manual_review_status   |   account_return |   equity_end_of_day |
|--------------:|:-------------------------|------------------:|-----------------:|:--------------|:-----------|:-----------------|------------------------------:|-------------------------:|----------------------------------------:|:-----------------------|-----------------:|--------------------:|
|      20260414 | CLOSED_BY_HISTORICAL_SIM |                 1 |                1 | 301189.SZ     | 奥尼电子       | 无                |                             0 |                        0 |                                       0 | NOT_REQUIRED           |       -0.0350052 |         2.17365e+07 |
|      20260415 | POSITION_OCCUPIED_SKIP   |                 1 |                0 | 301189.SZ     | 奥尼电子       | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.17365e+07 |
|      20260416 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.17365e+07 |
|      20260417 | CLOSED_BY_HISTORICAL_SIM |                 4 |                1 | 301319.SZ     | 唯特偶        | 无                |                             0 |                        0 |                                       0 | NOT_REQUIRED           |        0.0352808 |         2.25033e+07 |
|      20260420 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260421 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260422 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260423 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260424 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260427 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260428 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260429 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260430 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260506 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260507 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260508 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260511 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260512 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260513 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |
|      20260514 | NO_CANDIDATE             |                 0 |                0 |               |            | nan              |                           nan |                      nan |                                     nan | nan                    |        0         |         2.25033e+07 |

## 人工确认清单

无人工确认项。

## 资金曲线尾部

|     date | event                    |   signal_date | ts_code   | name   |      equity |   account_return | daily_status             |   peak_equity |     drawdown |
|---------:|:-------------------------|--------------:|:----------|:-------|------------:|-----------------:|:-------------------------|--------------:|-------------:|
| 20260414 | CLOSED_BY_HISTORICAL_SIM |      20260414 | 301189.SZ | 奥尼电子   | 2.17365e+07 |       -0.0350052 | CLOSED_BY_HISTORICAL_SIM |    2.2525e+07 | -0.0350052   |
| 20260415 | POSITION_OCCUPIED_SKIP   |      20260415 | 301189.SZ | 奥尼电子   | 2.17365e+07 |        0         | POSITION_OCCUPIED_SKIP   |    2.2525e+07 | -0.0350052   |
| 20260416 | NO_CANDIDATE             |      20260416 |           |        | 2.17365e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.0350052   |
| 20260417 | CLOSED_BY_HISTORICAL_SIM |      20260417 | 301319.SZ | 唯特偶    | 2.25033e+07 |        0.0352808 | CLOSED_BY_HISTORICAL_SIM |    2.2525e+07 | -0.000959421 |
| 20260420 | NO_CANDIDATE             |      20260420 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260421 | NO_CANDIDATE             |      20260421 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260422 | NO_CANDIDATE             |      20260422 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260423 | NO_CANDIDATE             |      20260423 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260424 | NO_CANDIDATE             |      20260424 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260427 | NO_CANDIDATE             |      20260427 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260428 | NO_CANDIDATE             |      20260428 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260429 | NO_CANDIDATE             |      20260429 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260430 | NO_CANDIDATE             |      20260430 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260506 | NO_CANDIDATE             |      20260506 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260507 | NO_CANDIDATE             |      20260507 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260508 | NO_CANDIDATE             |      20260508 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260511 | NO_CANDIDATE             |      20260511 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260512 | NO_CANDIDATE             |      20260512 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260513 | NO_CANDIDATE             |      20260513 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |
| 20260514 | NO_CANDIDATE             |      20260514 |           |        | 2.25033e+07 |        0         | NO_CANDIDATE             |    2.2525e+07 | -0.000959421 |

## 风险事件

无风险事件。

## 口径限制

该批量流程仍使用本地历史审计成交作为模拟成交依据。没有历史匹配的计划不会被记为成交；真实模拟盘还需要接入分钟 K、集合竞价、盘口五档和人工确认。
