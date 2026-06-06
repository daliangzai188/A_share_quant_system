# 模拟盘策略低频原因分析

本报告只分析本地候选和策略过滤条件，不接实盘，不调用 QMT，不下真实订单。

## 过滤漏斗

| stage                                                 |   days_with_candidate |   zero_candidate_days |   avg_count |   median_count |   max_count |
|:------------------------------------------------------|----------------------:|----------------------:|------------:|---------------:|------------:|
| raw_count                                             |                   120 |                     0 |   58.775    |           54.5 |         161 |
| after_universe_count                                  |                   120 |                     0 |   56.7833   |           53   |         144 |
| after_include_1_segment_limit_up_count_bucket=lt_5    |                    93 |                    27 |    2.51667  |            3   |           7 |
| after_include_2_market_chain_count_bucket=8_15        |                    43 |                    77 |    1.225    |            0   |           7 |
| after_include_3_fd_ratio_bucket=0_5pct_1pct           |                    24 |                    96 |    0.316667 |            0   |           4 |
| after_exclude_condition_1_amount_ratio_bucket=0_8_1_2 |                    20 |                   100 |    0.258333 |            0   |           4 |
| after_exclude_condition_2_market_segment=star         |                    20 |                   100 |    0.258333 |            0   |           4 |
| after_exclude_rule_1_exclude_bj_prev_pct_0_3          |                    20 |                   100 |    0.258333 |            0   |           4 |
| final_count                                           |                    20 |                   100 |    0.258333 |            0   |           4 |

## 首次归零阶段

| first_zero_stage                                      |   day_count |
|:------------------------------------------------------|------------:|
| after_include_2_market_chain_count_bucket=8_15        |          50 |
| after_include_1_segment_limit_up_count_bucket=lt_5    |          27 |
| not_zero                                              |          20 |
| after_include_3_fd_ratio_bucket=0_5pct_1pct           |          19 |
| after_exclude_condition_1_amount_ratio_bucket=0_8_1_2 |           4 |

## 单条件放宽机会

| relaxed_condition                  |   created_candidate_day_count | created_candidate_days                                                                                                                                                                                                                                                        |   strict_final_candidate_days |   relaxed_candidate_days |   avg_relaxed_count |   max_relaxed_count |
|:-----------------------------------|------------------------------:|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------:|-------------------------:|--------------------:|--------------------:|
| segment_limit_up_count_bucket=lt_5 |                            31 | 20251119;20251124;20251127;20251128;20251202;20251203;20251205;20251211;20251216;20251224;20260105;20260120;20260126;20260127;20260202;20260203;20260204;20260205;20260209;20260211;20260311;20260317;20260326;20260330;20260408;20260410;20260420;20260423;20260427;20260428 |                            20 |                       51 |             5.01667 |                  27 |
| market_chain_count_bucket=8_15     |                            26 | 20251112;20251201;20251210;20251217;20251218;20251222;20251223;20251229;20260121;20260129;20260213;20260304;20260306;20260310;20260312;20260313;20260319;20260324;20260325;20260402;20260421;20260422;20260424;20260506;20260508;20260511                                     |                            20 |                       46 |             0.525   |                   4 |
| fd_ratio_bucket=0_5pct_1pct        |                            22 | 20251128;20251202;20251211;20251216;20251224;20260105;20260120;20260127;20260202;20260203;20260205;20260211;20260311;20260317;20260330;20260408;20260410;20260420;20260423;20260427;20260428;20260429                                                                         |                            20 |                       42 |             1.05833 |                   6 |

## 入选字段原始分布

| condition_column              | required_value   | observed_value   |   raw_candidate_count | is_required_value   |
|:------------------------------|:-----------------|:-----------------|----------------------:|:--------------------|
| segment_limit_up_count_bucket | lt_5             | 20_40            |                  4163 | False               |
| segment_limit_up_count_bucket | lt_5             | 40_80            |                  1477 | False               |
| segment_limit_up_count_bucket | lt_5             | 10_20            |                   639 | False               |
| segment_limit_up_count_bucket | lt_5             | lt_5             |                   476 | True                |
| segment_limit_up_count_bucket | lt_5             | 5_10             |                   298 | False               |
| market_chain_count_bucket     | 8_15             | 8_15             |                  2815 | True                |
| market_chain_count_bucket     | 8_15             | 15_30            |                  2436 | False               |
| market_chain_count_bucket     | 8_15             | 3_8              |                  1464 | False               |
| market_chain_count_bucket     | 8_15             | gte_30           |                   338 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | 0_5pct_1pct      |                  1980 | True                |
| fd_ratio_bucket               | 0_5pct_1pct      | 1pct_2pct        |                  1896 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | 0_1pct_0_3pct    |                   977 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | 0_3pct_0_5pct    |                   907 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | 2pct_5pct        |                   813 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | lt_0_1pct        |                   444 | False               |
| fd_ratio_bucket               | 0_5pct_1pct      | gte_5pct         |                    36 | False               |

## 解释限制

- 单条件放宽只说明“可能增加候选”，不代表收益会变好。
- 是否放宽必须再跑收益、回撤、人工复核和成交真实性验证。
