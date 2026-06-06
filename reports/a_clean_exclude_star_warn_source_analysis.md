# 模拟盘前 WARN 来源分析

本报告只基于本地审计和预审文件，不调用外部接口，不接实盘。

## 汇总

|   trade_count |   warn_trade_count |   large_loss_threshold |   large_loss_count |   risk_target_count |   all_trade_count |   all_win_rate |   all_avg_account_return |   all_median_account_return |   all_compound_multiple |   all_max_drawdown |   all_max_profit |   all_max_loss |   all_profit_loss_ratio |   all_max_consecutive_losses |   risk_trade_count |   risk_win_rate |   risk_avg_account_return |   risk_median_account_return |   risk_compound_multiple |   risk_max_drawdown |   risk_max_profit |   risk_max_loss |   risk_profit_loss_ratio |   risk_max_consecutive_losses |
|--------------:|-------------------:|-----------------------:|-------------------:|--------------------:|------------------:|---------------:|-------------------------:|----------------------------:|------------------------:|-------------------:|-----------------:|---------------:|------------------------:|-----------------------------:|-------------------:|----------------:|--------------------------:|-----------------------------:|-------------------------:|--------------------:|------------------:|----------------:|-------------------------:|------------------------------:|
|            61 |                  5 |                  -0.08 |                  4 |                   5 |                61 |       0.704918 |                0.0681119 |                   0.0397618 |                 36.9447 |          -0.139569 |         0.501944 |      -0.139569 |                 2.41975 |                            2 |                  5 |               0 |                -0.0932026 |                   -0.0898617 |                 0.611403 |           -0.333589 |        -0.0467794 |       -0.139569 |                        0 |                             5 |

## 风险目标交易

| review_status   | risk_flags   |   trade_date | ts_code   | name   |   dynamic_account_return | limit_height_rank_bucket   | first_time_detail_bucket   |   amount_ratio_bucket | retreat_state_bucket   |
|:----------------|:-------------|-------------:|:----------|:-------|-------------------------:|:---------------------------|:---------------------------|----------------------:|:-----------------------|
| WARN            | 单笔亏损超过8%账户收益 |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | rank_4_10                  | before_1000                |                   3_5 | neutral                |
| WARN            | 单笔亏损超过8%账户收益 |     20250318 | 920110.BJ | 雷特科技   |               -0.0898617 | rank_4_10                  | before_1000                |                   3_5 | retreat_2day           |
| WARN            | 单笔亏损超过8%账户收益 |     20250702 | 920689.BJ | 克莱特    |               -0.139569  | rank_4_10                  | 1000_1100                  |                   3_5 | retreat_2day           |
| WARN            | 卖出滑点偏高       |     20251120 | 301092.SZ | 争光股份   |               -0.0467794 | rank_4_10                  | before_1000                |                   2_3 | retreat_weak           |
| WARN            | 单笔亏损超过8%账户收益 |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | rank_4_10                  | 1100_1330                  |                   2_3 | warming_2day           |

## 风险集中单因子 Top 20

| condition                                  |   trade_count |   risk_target_count |   risk_rate_in_bucket |   risk_target_coverage |   avg_account_return |   compound_multiple |   max_loss |
|:-------------------------------------------|--------------:|--------------------:|----------------------:|-----------------------:|---------------------:|--------------------:|-----------:|
| amount_bucket=1e8_3e8                      |             5 |                   2 |              0.4      |                    0.4 |         -0.0229841   |            0.882546 | -0.0898617 |
| amount_ratio_bucket=3_5                    |            12 |                   3 |              0.25     |                    0.6 |          0.0705984   |            1.94824  | -0.139569  |
| prev_pct_chg_bucket=0_3                    |            15 |                   3 |              0.2      |                    0.6 |          0.0378031   |            1.58242  | -0.139569  |
| market_leader_rank_bucket=rank_11_30       |            27 |                   4 |              0.148148 |                    0.8 |          0.0390549   |            2.34279  | -0.139569  |
| limit_height_rank_bucket=rank_4_10         |            36 |                   5 |              0.138889 |                    1   |          0.0367751   |            3.14916  | -0.139569  |
| first_time_detail_bucket=before_1000       |            17 |                   3 |              0.176471 |                    0.6 |          0.0527698   |            2.06472  | -0.0898617 |
| turnover_rate_bucket=6_10                  |             3 |                   1 |              0.333333 |                    0.2 |         -0.000629554 |            0.985345 | -0.082543  |
| market_emotion_state_bucket=retreat        |            11 |                   2 |              0.181818 |                    0.4 |          0.0323584   |            1.3563   | -0.139569  |
| retreat_state_bucket=retreat_2day          |            11 |                   2 |              0.181818 |                    0.4 |          0.0323584   |            1.3563   | -0.139569  |
| volume_ratio_bucket=2_4                    |            24 |                   3 |              0.125    |                    0.6 |          0.0395538   |            2.32618  | -0.139569  |
| segment_limit_max_height_bucket=1          |            35 |                   5 |              0.142857 |                    1   |          0.0587774   |            5.72602  | -0.139569  |
| limit_up_count_bucket=50_80                |            31 |                   4 |              0.129032 |                    0.8 |          0.0522388   |            4.04041  | -0.139569  |
| open_times_bucket=1                        |            15 |                   2 |              0.133333 |                    0.4 |          0.0411189   |            1.62835  | -0.139569  |
| amount_ratio_bucket=2_3                    |            15 |                   2 |              0.133333 |                    0.4 |          0.0436334   |            1.77095  | -0.10726   |
| market_emotion_state_bucket=warming        |             6 |                   1 |              0.166667 |                    0.2 |         -0.00355976  |            0.96074  | -0.10726   |
| retreat_state_bucket=warming_2day          |             6 |                   1 |              0.166667 |                    0.2 |         -0.00355976  |            0.96074  | -0.10726   |
| market_limit_down_count_bucket=lt_5        |            19 |                   2 |              0.105263 |                    0.4 |          0.0336673   |            1.78714  | -0.10726   |
| segment_retreat_state_bucket=weak_below_3  |            25 |                   4 |              0.16     |                    0.8 |          0.097385    |            7.72704  | -0.139569  |
| segment_market_leader_rank_bucket=rank_2_3 |            25 |                   3 |              0.12     |                    0.6 |          0.0657423   |            4.16811  | -0.10726   |
| market_limit_down_count_bucket=5_15        |            25 |                   3 |              0.12     |                    0.6 |          0.0749022   |            4.98115  | -0.139569  |

## 候选排除规则 Top 20

| exclude_rule                                                                   |   removed_trade_count |   removed_risk_target_count |   remaining_trade_count |   remaining_compound_multiple |   remaining_win_rate |   remaining_max_drawdown |   remaining_max_loss |
|:-------------------------------------------------------------------------------|----------------------:|----------------------------:|------------------------:|------------------------------:|---------------------:|-------------------------:|---------------------:|
| prev_pct_chg_bucket=0_3&&market_segment=bj                                     |                     2 |                           2 |                      59 |                       47.1768 |             0.728814 |                -0.110076 |            -0.10726  |
| prev_pct_chg_bucket=0_3&&pct_chg_bucket=gt_20_5                                |                     2 |                           2 |                      59 |                       47.1768 |             0.728814 |                -0.110076 |            -0.10726  |
| amount_bucket=1e8_3e8&&first_time_detail_bucket=before_1000                    |                     2 |                           2 |                      59 |                       44.2445 |             0.728814 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&market_emotion_state_bucket=retreat                     |                     1 |                           1 |                      60 |                       40.5924 |             0.716667 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&retreat_state_bucket=retreat_2day                       |                     1 |                           1 |                      60 |                       40.5924 |             0.716667 |                -0.139569 |            -0.139569 |
| first_time_detail_bucket=before_1000&&market_segment=bj                        |                     1 |                           1 |                      60 |                       40.5924 |             0.716667 |                -0.139569 |            -0.139569 |
| first_time_detail_bucket=before_1000&&pct_chg_bucket=gt_20_5                   |                     1 |                           1 |                      60 |                       40.5924 |             0.716667 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&turnover_rate_bucket=6_10                               |                     1 |                           1 |                      60 |                       40.2686 |             0.716667 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&volume_ratio_bucket=2_4                                 |                     1 |                           1 |                      60 |                       40.2686 |             0.716667 |                -0.139569 |            -0.139569 |
| amount_ratio_bucket=3_5&&turnover_rate_bucket=6_10                             |                     1 |                           1 |                      60 |                       40.2686 |             0.716667 |                -0.139569 |            -0.139569 |
| amount_ratio_bucket=3_5&&market_emotion_state_bucket=retreat                   |                     3 |                           2 |                      58 |                       46.2792 |             0.724138 |                -0.110076 |            -0.10726  |
| amount_ratio_bucket=3_5&&retreat_state_bucket=retreat_2day                     |                     3 |                           2 |                      58 |                       46.2792 |             0.724138 |                -0.110076 |            -0.10726  |
| amount_ratio_bucket=3_5&&prev_pct_chg_bucket=0_3                               |                     5 |                           3 |                      56 |                       49.9206 |             0.732143 |                -0.10726  |            -0.10726  |
| amount_bucket=1e8_3e8&&prev_pct_chg_bucket=0_3                                 |                     3 |                           2 |                      58 |                       45.3861 |             0.741379 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&amount_ratio_bucket=3_5                                 |                     3 |                           2 |                      58 |                       41.9976 |             0.724138 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&segment_limit_max_height_bucket=1                       |                     3 |                           2 |                      58 |                       41.9976 |             0.724138 |                -0.139569 |            -0.139569 |
| amount_bucket=1e8_3e8&&limit_up_count_bucket=50_80                             |                     3 |                           2 |                      58 |                       41.9976 |             0.724138 |                -0.139569 |            -0.139569 |
| market_emotion_state_bucket=retreat&&open_times_bucket=1                       |                     2 |                           1 |                      59 |                       43.8503 |             0.728814 |                -0.110076 |            -0.10726  |
| retreat_state_bucket=retreat_2day&&open_times_bucket=1                         |                     2 |                           1 |                      59 |                       43.8503 |             0.728814 |                -0.110076 |            -0.10726  |
| market_emotion_state_bucket=warming&&segment_retreat_state_bucket=weak_below_3 |                     2 |                           1 |                      59 |                       42.6638 |             0.728814 |                -0.139569 |            -0.139569 |

## 使用说明

候选排除规则只能作为下一轮回测输入，不能直接认定为最终规则。后续必须重新跑完整策略优化和审计，比较复利、回撤、样本数、滑点、手续费和成交约束。