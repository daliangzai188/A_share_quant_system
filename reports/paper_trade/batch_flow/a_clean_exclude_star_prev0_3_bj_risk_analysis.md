# 批量模拟盘风险事件分析

本报告只基于本地 CSV 拆解风险事件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

|   risk_event_count |   pending_no_historical_match_count |   single_trade_loss_warn_count |   position_occupied_skip_count |   pending_likely_position_occupied_count |   pending_avg_historical_reference_return |   pending_win_reference_count |   loss_avg_account_return |   loss_max_account_loss |   position_skip_avg_reference_return |   position_skip_win_reference_count |
|-------------------:|------------------------------------:|-------------------------------:|-------------------------------:|-----------------------------------------:|------------------------------------------:|------------------------------:|--------------------------:|------------------------:|-------------------------------------:|------------------------------------:|
|                  2 |                                   0 |                              2 |                             12 |                                        0 |                                         0 |                             0 |                -0.0949016 |                -0.10726 |                           -0.0231916 |                                   5 |

## Pending 事件

无 Pending 事件。

## 单笔亏损预警

|   trade_date | ts_code   | name   |   dynamic_account_return |   buy_trade_date |   exit_trade_date |   buy_amount_ratio |   sell_amount_ratio | first_time_detail_bucket   |   amount_ratio_bucket | retreat_state_bucket   |
|-------------:|:----------|:-------|-------------------------:|-----------------:|------------------:|-------------------:|--------------------:|:---------------------------|----------------------:|:-----------------------|
|     20241227 | 300599.SZ | 雄塑科技   |                -0.082543 |         20241230 |          20241231 |          0.0070189 |           0.0112191 | before_1000                |                   3_5 | neutral                |
|     20251219 | 300557.SZ | 理工光科   |                -0.10726  |         20251222 |          20251223 |          0.0125252 |           0.018835  | 1100_1330                  |                   2_3 | warming_2day           |

## 持仓占用跳过

|   signal_date | ts_code   | name   | position_occupied_by   |   position_occupied_exit_dates |   historical_reference_net_return | market_segment   | first_time_detail_bucket   | amount_ratio_bucket   | retreat_state_bucket   |
|--------------:|:----------|:-------|:-----------------------|-------------------------------:|----------------------------------:|:-----------------|:---------------------------|:----------------------|:-----------------------|
|      20240805 | 300329.SZ | 海伦钢琴   | 300149.SZ 睿智医药         |                       20240806 |                        0.00524918 | chi_next         | before_1000                | gte_5                 | retreat_2day           |
|      20241230 | 300933.SZ | 中辰股份   | 300599.SZ 雄塑科技         |                       20241231 |                       -0.110965   | chi_next         | before_1000                | gte_5                 | retreat_2day           |
|      20250103 | 301568.SZ | 思泰克    | 300947.SZ 德必集团         |                       20250106 |                        0.0538399  | chi_next         | before_1000                | gte_5                 | retreat_weak           |
|      20250311 | 300819.SZ | 聚杰微纤   | 301225.SZ 恒勃股份         |                       20250312 |                       -0.0280561  | chi_next         | 1330_1430                  | 1_2_2                 | neutral                |
|      20250730 | 300877.SZ | 金春股份   | 300528.SZ 幸福蓝海         |                       20250731 |                       -0.0365522  | chi_next         | 1100_1330                  | 1_2_2                 | neutral                |
|      20250903 | 300092.SZ | 科新机电   | 920274.BJ 宏裕包材         |                       20250904 |                        0.0468267  | chi_next         | 1100_1330                  | 1_2_2                 | retreat_2day           |
|      20251103 | 300455.SZ | 航天智装   | 300204.SZ 舒泰神          |                       20251104 |                        0.032196   | chi_next         | 1100_1330                  | 1_2_2                 | warming_2day           |
|      20251107 | 300102.SZ | 乾照光电   | 300437.SZ 清水源          |                       20251110 |                       -0.0885257  | chi_next         | 1330_1430                  | gte_5                 | retreat_2day           |
|      20251121 | 301171.SZ | 易点天下   | 301092.SZ 争光股份         |                       20251124 |                        0.0892371  | chi_next         | 1100_1330                  | 3_5                   | weak_below_30          |
|      20251215 | 920665.BJ | 科强股份   | 920576.BJ 天力复合         |                       20251216 |                       -0.177304   | bj               | 1000_1100                  | 2_3                   | neutral                |
|      20260227 | 301226.SZ | 祥明智能   | 300499.SZ 高澜股份         |                       20260302 |                       -0.0107992  | chi_next         | after_1430                 | 3_5                   | neutral                |
|      20260415 | 301189.SZ | 奥尼电子   | 301189.SZ 奥尼电子         |                       20260416 |                       -0.0534462  | chi_next         | before_1000                | 1_2_2                 | neutral                |

## 风险桶集中度

| source        | factor                         | bucket       |   event_count |   avg_reference_return |   avg_account_return |
|:--------------|:-------------------------------|:-------------|--------------:|-----------------------:|---------------------:|
| loss          | fd_ratio_bucket                | 0_5pct_1pct  |             2 |             0          |           -0.0949016 |
| loss          | market_segment                 | chi_next     |             2 |             0          |           -0.0949016 |
| loss          | volume_ratio_bucket            | 2_4          |             2 |             0          |           -0.0949016 |
| loss          | amount_ratio_bucket            | 2_3          |             1 |             0          |           -0.10726   |
| loss          | amount_ratio_bucket            | 3_5          |             1 |             0          |           -0.082543  |
| loss          | first_time_detail_bucket       | 1100_1330    |             1 |             0          |           -0.10726   |
| loss          | first_time_detail_bucket       | before_1000  |             1 |             0          |           -0.082543  |
| loss          | market_limit_down_count_bucket | 5_15         |             1 |             0          |           -0.082543  |
| loss          | market_limit_down_count_bucket | lt_5         |             1 |             0          |           -0.10726   |
| loss          | open_times_bucket              | 0            |             1 |             0          |           -0.082543  |
| loss          | open_times_bucket              | 2_3          |             1 |             0          |           -0.10726   |
| loss          | prev_pct_chg_bucket            | 0_3          |             1 |             0          |           -0.082543  |
| loss          | prev_pct_chg_bucket            | neg3_0       |             1 |             0          |           -0.10726   |
| loss          | retreat_state_bucket           | neutral      |             1 |             0          |           -0.082543  |
| loss          | retreat_state_bucket           | warming_2day |             1 |             0          |           -0.10726   |
| loss          | turnover_rate_bucket           | 15_25        |             1 |             0          |           -0.10726   |
| loss          | turnover_rate_bucket           | 6_10         |             1 |             0          |           -0.082543  |
| position_skip | market_segment                 | chi_next     |            11 |            -0.00918137 |            0         |
| position_skip | amount_ratio_bucket            | 1_2_2        |             5 |            -0.00780636 |            0         |
| position_skip | market_limit_down_count_bucket | 5_15         |             5 |            -0.0692142  |            0         |
| position_skip | retreat_state_bucket           | neutral      |             5 |            -0.0612316  |            0         |
| position_skip | amount_ratio_bucket            | gte_5        |             4 |            -0.0351003  |            0         |
| position_skip | first_time_detail_bucket       | 1100_1330    |             4 |             0.0329269  |            0         |
| position_skip | first_time_detail_bucket       | before_1000  |             4 |            -0.0263304  |            0         |
| position_skip | retreat_state_bucket           | retreat_2day |             4 |            -0.0368536  |            0         |
| position_skip | market_limit_down_count_bucket | lt_5         |             3 |            -0.0424603  |            0         |
| position_skip | prev_pct_chg_bucket            | 0_3          |             3 |            -0.0119469  |            0         |
| position_skip | prev_pct_chg_bucket            | 3_7          |             3 |            -0.04994    |            0         |
| position_skip | amount_ratio_bucket            | 3_5          |             2 |             0.0392189  |            0         |
| position_skip | first_time_detail_bucket       | 1330_1430    |             2 |            -0.0582909  |            0         |

## 初步处理建议

1. `PENDING_NO_HISTORICAL_MATCH` 优先按持仓占用处理，不能把这些候选强行计入收益。
2. 单笔亏损预警先作为风控观察项，不直接硬过滤；样本只有少数时，硬过滤容易过拟合。
3. 后续需要把持仓占用逻辑前置到候选批量流程，在已经有持仓未释放时直接标记 `POSITION_OCCUPIED_SKIP`。
