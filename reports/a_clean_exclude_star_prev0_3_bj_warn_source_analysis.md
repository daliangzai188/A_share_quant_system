# 模拟盘前 WARN 来源分析

本报告只基于本地审计和预审文件，不调用外部接口，不接实盘。

## 汇总

|   trade_count |   warn_trade_count |   large_loss_threshold |   large_loss_count |   risk_target_count |   all_trade_count |   all_win_rate |   all_avg_account_return |   all_median_account_return |   all_compound_multiple |   all_max_drawdown |   all_max_profit |   all_max_loss |   all_profit_loss_ratio |   all_max_consecutive_losses |   risk_trade_count |   risk_win_rate |   risk_avg_account_return |   risk_median_account_return |   risk_compound_multiple |   risk_max_drawdown |   risk_max_profit |   risk_max_loss |   risk_profit_loss_ratio |   risk_max_consecutive_losses |
|--------------:|-------------------:|-----------------------:|-------------------:|--------------------:|------------------:|---------------:|-------------------------:|----------------------------:|------------------------:|-------------------:|-----------------:|---------------:|------------------------:|-----------------------------:|-------------------:|----------------:|--------------------------:|-----------------------------:|-------------------------:|--------------------:|------------------:|----------------:|-------------------------:|------------------------------:|
|            59 |                  5 |                  -0.08 |                  2 |                   5 |                59 |       0.728814 |                0.0734519 |                   0.0454512 |                 45.0067 |          -0.110076 |         0.498054 |       -0.10726 |                 2.81342 |                            2 |                  5 |             0.2 |                 -0.046529 |                   -0.0505146 |                 0.782886 |           -0.175758 |         0.0352808 |        -0.10726 |                 0.526725 |                             4 |

## 风险目标交易

| review_status   | risk_flags                |   trade_date | ts_code   | name   |   dynamic_account_return | limit_height_rank_bucket   | first_time_detail_bucket   |   amount_ratio_bucket | retreat_state_bucket   |
|:----------------|:--------------------------|-------------:|:----------|:-------|-------------------------:|:---------------------------|:---------------------------|----------------------:|:-----------------------|
| WARN            | 单笔亏损超过8%账户收益              |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | rank_4_10                  | before_1000                |                   3_5 | neutral                |
| WARN            | 卖出滑点偏高                    |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | rank_4_10                  | 1100_1330                  |                   2_3 | neutral                |
| WARN            | 卖出成交额占比偏高; 买入滑点偏高; 卖出滑点偏高 |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | rank_4_10                  | before_1000                |                   2_3 | retreat_weak           |
| WARN            | 单笔亏损超过8%账户收益              |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | rank_4_10                  | 1100_1330                  |                   2_3 | warming_2day           |
| WARN            | 卖出滑点偏高                    |     20260417 | 301319.SZ | 唯特偶    |                0.0352808 | rank_4_10                  | 1100_1330                  |                   2_3 | neutral                |

## 风险集中单因子 Top 20

| condition                                  |   trade_count |   risk_target_count |   risk_rate_in_bucket |   risk_target_coverage |   avg_account_return |   compound_multiple |   max_loss |
|:-------------------------------------------|--------------:|--------------------:|----------------------:|-----------------------:|---------------------:|--------------------:|-----------:|
| amount_ratio_bucket=2_3                    |            15 |                   4 |              0.266667 |                    0.8 |           0.0420234  |            1.72997  | -0.10726   |
| first_time_detail_bucket=1100_1330         |            17 |                   3 |              0.176471 |                    0.6 |           0.0415103  |            1.92685  | -0.10726   |
| turnover_rate_bucket=6_10                  |             3 |                   1 |              0.333333 |                    0.2 |          -0.00248928 |            0.980486 | -0.082543  |
| limit_height_rank_bucket=rank_4_10         |            34 |                   5 |              0.147059 |                    1   |           0.0448404  |            3.90959  | -0.10726   |
| prev_pct_chg_bucket=3_7                    |            10 |                   2 |              0.2      |                    0.4 |           0.0568313  |            1.65648  | -0.059315  |
| turnover_rate_bucket=15_25                 |            20 |                   3 |              0.15     |                    0.6 |           0.0514644  |            2.40278  | -0.10726   |
| amount_bucket=1e8_3e8                      |             4 |                   1 |              0.25     |                    0.2 |          -0.00626471 |            0.969683 | -0.082543  |
| volume_ratio_bucket=2_4                    |            23 |                   3 |              0.130435 |                    0.6 |           0.0466557  |            2.66408  | -0.10726   |
| open_times_bucket=2_3                      |            15 |                   2 |              0.133333 |                    0.4 |           0.0354039  |            1.61447  | -0.10726   |
| limit_up_count_bucket=50_80                |            29 |                   4 |              0.137931 |                    0.8 |           0.0625912  |            4.99862  | -0.10726   |
| market_leader_rank_bucket=rank_gt_30       |            21 |                   3 |              0.142857 |                    0.6 |           0.0682796  |            3.67779  | -0.10726   |
| market_emotion_state_bucket=warming        |             6 |                   1 |              0.166667 |                    0.2 |          -0.00455534 |            0.955304 | -0.10726   |
| retreat_state_bucket=warming_2day          |             6 |                   1 |              0.166667 |                    0.2 |          -0.00455534 |            0.955304 | -0.10726   |
| first_time_detail_bucket=before_1000       |            16 |                   2 |              0.125    |                    0.4 |           0.0606368  |            2.23238  | -0.082543  |
| market_limit_down_count_bucket=lt_5        |            18 |                   2 |              0.111111 |                    0.4 |           0.0398607  |            1.94007  | -0.10726   |
| segment_market_leader_rank_bucket=rank_2_3 |            25 |                   3 |              0.12     |                    0.6 |           0.064709   |            4.06809  | -0.10726   |
| prev_pct_chg_bucket=7_10                   |             5 |                   1 |              0.2      |                    0.2 |           0.15876    |            1.95802  | -0.0338314 |
| segment_retreat_state_bucket=retreat_weak  |            16 |                   2 |              0.125    |                    0.4 |           0.0758068  |            2.97276  | -0.082543  |
| segment_limit_max_height_bucket=1          |            33 |                   4 |              0.121212 |                    0.8 |           0.0685945  |            7.15397  | -0.10726   |
| market_limit_down_count_bucket=5_15        |            24 |                   3 |              0.125    |                    0.6 |           0.0830323  |            5.68659  | -0.082543  |

## 候选排除规则 Top 20

| exclude_rule                                                                   |   removed_trade_count |   removed_risk_target_count |   remaining_trade_count |   remaining_compound_multiple |   remaining_win_rate |   remaining_max_drawdown |   remaining_max_loss |
|:-------------------------------------------------------------------------------|----------------------:|----------------------------:|------------------------:|------------------------------:|---------------------:|-------------------------:|---------------------:|
| turnover_rate_bucket=6_10&&amount_bucket=1e8_3e8                               |                     1 |                           1 |                      58 |                       49.0559 |             0.741379 |                -0.10726  |            -0.10726  |
| turnover_rate_bucket=6_10&&segment_retreat_state_bucket=retreat_weak           |                     1 |                           1 |                      58 |                       49.0559 |             0.741379 |                -0.10726  |            -0.10726  |
| amount_bucket=1e8_3e8&&volume_ratio_bucket=2_4                                 |                     1 |                           1 |                      58 |                       49.0559 |             0.741379 |                -0.10726  |            -0.10726  |
| amount_bucket=1e8_3e8&&first_time_detail_bucket=before_1000                    |                     1 |                           1 |                      58 |                       49.0559 |             0.741379 |                -0.10726  |            -0.10726  |
| open_times_bucket=2_3&&prev_pct_chg_bucket=7_10                                |                     1 |                           1 |                      58 |                       46.2845 |             0.741379 |                -0.110076 |            -0.10726  |
| prev_pct_chg_bucket=3_7&&turnover_rate_bucket=15_25                            |                     2 |                           2 |                      57 |                       45.7858 |             0.736842 |                -0.110076 |            -0.10726  |
| amount_ratio_bucket=2_3&&open_times_bucket=2_3                                 |                     3 |                           2 |                      56 |                       55.1146 |             0.767857 |                -0.110076 |            -0.082543 |
| turnover_rate_bucket=15_25&&market_emotion_state_bucket=warming                |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| turnover_rate_bucket=15_25&&retreat_state_bucket=warming_2day                  |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| market_emotion_state_bucket=warming&&prev_pct_chg_bucket=neg3_0                |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| market_emotion_state_bucket=warming&&segment_retreat_state_bucket=weak_below_3 |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| retreat_state_bucket=warming_2day&&prev_pct_chg_bucket=neg3_0                  |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| retreat_state_bucket=warming_2day&&segment_retreat_state_bucket=weak_below_3   |                     2 |                           1 |                      57 |                       51.9738 |             0.754386 |                -0.106833 |            -0.082543 |
| turnover_rate_bucket=6_10&&first_time_detail_bucket=before_1000                |                     2 |                           1 |                      57 |                       51.6756 |             0.754386 |                -0.10726  |            -0.10726  |
| turnover_rate_bucket=6_10&&segment_limit_max_height_bucket=1                   |                     2 |                           1 |                      57 |                       51.6756 |             0.754386 |                -0.10726  |            -0.10726  |
| turnover_rate_bucket=6_10&&market_leader_rank_bucket=rank_11_30                |                     2 |                           1 |                      57 |                       51.6756 |             0.754386 |                -0.10726  |            -0.10726  |
| amount_bucket=1e8_3e8&&segment_market_leader_rank_bucket=rank_2_3              |                     2 |                           1 |                      57 |                       50.3217 |             0.754386 |                -0.10726  |            -0.10726  |
| amount_bucket=1e8_3e8&&market_limit_down_count_bucket=5_15                     |                     2 |                           1 |                      57 |                       50.3217 |             0.754386 |                -0.10726  |            -0.10726  |
| prev_pct_chg_bucket=3_7&&first_time_detail_bucket=before_1000                  |                     2 |                           1 |                      57 |                       50.39   |             0.754386 |                -0.110076 |            -0.10726  |
| market_limit_down_count_bucket=lt_5&&prev_pct_chg_bucket=neg3_0                |                     2 |                           1 |                      57 |                       48.4862 |             0.736842 |                -0.110076 |            -0.082543 |

## 使用说明

候选排除规则只能作为下一轮回测输入，不能直接认定为最终规则。后续必须重新跑完整策略优化和审计，比较复利、回撤、样本数、滑点、手续费和成交约束。