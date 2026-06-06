# A+B filtered 中 B 剩余风险过滤压力测试

本报告只使用事前可见字段测试 B 备用策略的额外过滤条件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

| scenario                                     | description                               |   executed_trade_count |   b_trade_count |   a_trade_count |   win_rate |   equity_multiple |   max_drawdown |   max_loss |   residual_b_rejected_count |   residual_b_after_filter_count |
|:---------------------------------------------|:------------------------------------------|-----------------------:|----------------:|----------------:|-----------:|------------------:|---------------:|-----------:|----------------------------:|--------------------------------:|
| reject_b_market_segment_sh_main              | B额外过滤沪市主板。                                |                     17 |               5 |              12 |   0.823529 |           2.46    |     -0.0505146 | -0.0505146 |                          10 |                               5 |
| reject_b_retreat_warming_2day                | B额外过滤retreat_state_bucket=warming_2day。   |                     18 |               6 |              12 |   0.777778 |           2.4365  |     -0.0505146 | -0.0505146 |                           8 |                               7 |
| reject_b_market_emotion_warming              | B额外过滤market_emotion_state_bucket=warming。 |                     18 |               6 |              12 |   0.777778 |           2.4365  |     -0.0505146 | -0.0505146 |                           8 |                               7 |
| reject_b_open_times_gte_4                    | B额外过滤炸板次数>=4。                             |                     17 |               5 |              12 |   0.823529 |           2.2801  |     -0.0505146 | -0.0505146 |                          10 |                               5 |
| reject_b_turnover_rate_gte_20                | B额外过滤换手率>=20%。                            |                     18 |               7 |              11 |   0.833333 |           2.17239 |     -0.0505146 | -0.0505146 |                           7 |                               8 |
| reject_b_sh_main_or_chain_gte_30             | B额外过滤沪市主板或全市场连板数量gte_30。                  |                     16 |               4 |              12 |   0.8125   |           2.12324 |     -0.0505146 | -0.0505146 |                          11 |                               4 |
| reject_b_sh_main_or_open_times_gte_4         | B额外过滤沪市主板或炸板次数>=4。                        |                     14 |               1 |              13 |   0.785714 |           2.06364 |     -0.0505146 | -0.0505146 |                          14 |                               1 |
| reject_b_open_times_gte_4_or_turnover_gte_20 | B额外过滤炸板次数>=4或换手率>=20%。                    |                     16 |               4 |              12 |   0.8125   |           1.99664 |     -0.0505146 | -0.0505146 |                          11 |                               4 |
| reject_b_turnover_bucket_15_25               | B额外过滤换手率分桶15_25。                          |                     18 |               7 |              11 |   0.777778 |           1.94353 |     -0.0505146 | -0.0505146 |                           8 |                               7 |
| reject_b_market_chain_gte_30                 | B额外过滤全市场连板数量gte_30。                       |                     21 |              11 |              10 |   0.714286 |           2.17334 |     -0.0915278 | -0.0505146 |                           2 |                              13 |
| reject_b_amount_lt_800k                      | B额外过滤成交额字段amount<80万口径单位。                 |                     20 |              10 |              10 |   0.7      |           2.17703 |     -0.143882  | -0.113904  |                           5 |                              10 |
| baseline_ab_filtered                         | 当前 A+B filtered，不增加额外B过滤。                 |                     23 |              13 |              10 |   0.695652 |           2.23123 |     -0.143882  | -0.113904  |                           0 |                              15 |
| reject_b_open_times_gte_6                    | B额外过滤炸板次数>=6。                             |                     20 |               8 |              12 |   0.8      |           2.21045 |     -0.143882  | -0.113904  |                           5 |                              10 |

## 最优回撤方案逐日明细

|   signal_date | strategy_leg   | operation_status          | ts_code   | name   |   account_return |     equity_after |   drawdown | risk_flags         |
|--------------:|:---------------|:--------------------------|:----------|:-------|-----------------:|-----------------:|-----------:|:-------------------|
|      20251112 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251113 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251114 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251117 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251118 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251119 | NONE           | NO_CANDIDATE              |           |        |        0         | 500000           |  0         |                    |
|      20251120 | A              | HISTORICAL_SIM_FILLED     | 301092.SZ | 争光股份   |       -0.0505146 | 474743           | -0.0505146 | 无                  |
|      20251121 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 474743           | -0.0505146 |                    |
|      20251124 | NONE           | NO_CANDIDATE              |           |        |        0         | 474743           | -0.0505146 |                    |
|      20251125 | A              | REVIEW_REQUIRED_PLAN_ONLY | 301117.SZ | 佳缘科技   |        0         | 474743           | -0.0505146 | LOSS_OVERLAY_WATCH |
|      20251126 | NONE           | NO_CANDIDATE              |           |        |        0         | 474743           | -0.0505146 |                    |
|      20251127 | NONE           | NO_CANDIDATE              |           |        |        0         | 474743           | -0.0505146 |                    |
|      20251128 | NONE           | NO_CANDIDATE              |           |        |        0         | 474743           | -0.0505146 |                    |
|      20251201 | B              | HISTORICAL_SIM_FILLED     | 000078.SZ | 海王生物   |        0.148382  | 545186           |  0         | 无                  |
|      20251202 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 545186           |  0         |                    |
|      20251203 | NONE           | NO_CANDIDATE              |           |        |        0         | 545186           |  0         |                    |
|      20251204 | A              | HISTORICAL_SIM_FILLED     | 300946.SZ | 恒而达    |        0.0291749 | 561092           |  0         | 无                  |
|      20251205 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 561092           |  0         |                    |
|      20251208 | B              | HISTORICAL_SIM_FILLED     | 000571.SZ | 新大洲A   |        0.0312686 | 578636           |  0         | 无                  |
|      20251209 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 578636           |  0         |                    |
|      20251210 | NONE           | NO_CANDIDATE              |           |        |        0         | 578636           |  0         |                    |
|      20251211 | NONE           | NO_CANDIDATE              |           |        |        0         | 578636           |  0         |                    |
|      20251212 | A              | HISTORICAL_SIM_FILLED     | 920576.BJ | 天力复合   |        0.181313  | 683551           |  0         | 无                  |
|      20251215 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 683551           |  0         |                    |
|      20251216 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251217 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251218 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251219 | A              | REVIEW_REQUIRED_PLAN_ONLY | 300557.SZ | 理工光科   |        0         | 683551           |  0         | LOSS_OVERLAY_WATCH |
|      20251222 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251223 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251224 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251225 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251226 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251229 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251230 | NONE           | NO_CANDIDATE              |           |        |        0         | 683551           |  0         |                    |
|      20251231 | A              | HISTORICAL_SIM_FILLED     | 300058.SZ | 蓝色光标   |        0.130855  | 772997           |  0         | 无                  |
|      20260105 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 772997           |  0         |                    |
|      20260106 | B              | HISTORICAL_SIM_FILLED     | 002865.SZ | 钧达股份   |        0.158607  | 895599           |  0         | 无                  |
|      20260107 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 895599           |  0         |                    |
|      20260108 | NONE           | NO_CANDIDATE              |           |        |        0         | 895599           |  0         |                    |
|      20260109 | NONE           | NO_CANDIDATE              |           |        |        0         | 895599           |  0         |                    |
|      20260112 | NONE           | NO_CANDIDATE              |           |        |        0         | 895599           |  0         |                    |
|      20260113 | NONE           | NO_CANDIDATE              |           |        |        0         | 895599           |  0         |                    |
|      20260114 | NONE           | NO_CANDIDATE              |           |        |        0         | 895599           |  0         |                    |
|      20260115 | A              | HISTORICAL_SIM_FILLED     | 301629.SZ | 矽电股份   |       -0.0338314 | 865300           | -0.0338314 | 无                  |
|      20260116 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 865300           | -0.0338314 |                    |
|      20260119 | A              | HISTORICAL_SIM_FILLED     | 300658.SZ | 延江股份   |        0.0681174 | 924242           |  0         | 无                  |
|      20260120 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         | 924242           |  0         |                    |
|      20260121 | NONE           | NO_CANDIDATE              |           |        |        0         | 924242           |  0         |                    |
|      20260122 | NONE           | NO_CANDIDATE              |           |        |        0         | 924242           |  0         |                    |
|      20260123 | A              | HISTORICAL_SIM_FILLED     | 920368.BJ | 连城数控   |        0.102773  |      1.01923e+06 |  0         | 无                  |
|      20260126 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.01923e+06 |  0         |                    |
|      20260127 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.01923e+06 |  0         |                    |
|      20260128 | A              | HISTORICAL_SIM_FILLED     | 300164.SZ | 通源石油   |        0.0457726 |      1.06588e+06 |  0         | 无                  |
|      20260129 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260130 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260202 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260203 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260204 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260205 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260206 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260209 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260210 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260211 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260212 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260213 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260224 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260225 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.06588e+06 |  0         |                    |
|      20260226 | A              | HISTORICAL_SIM_FILLED     | 300499.SZ | 高澜股份   |        0.0193949 |      1.08655e+06 |  0         | 无                  |
|      20260227 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.08655e+06 |  0         |                    |
|      20260302 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.08655e+06 |  0         |                    |
|      20260303 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.08655e+06 |  0         |                    |
|      20260304 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.08655e+06 |  0         |                    |
|      20260305 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.08655e+06 |  0         |                    |
|      20260306 | B              | HISTORICAL_SIM_FILLED     | 000509.SZ | 华塑控股   |        0.0220556 |      1.11052e+06 |  0         | 无                  |
|      20260309 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260310 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260311 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260312 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260313 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260316 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260317 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260318 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260319 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260320 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260323 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260324 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260325 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260326 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.11052e+06 |  0         |                    |
|      20260327 | A              | HISTORICAL_SIM_FILLED     | 300204.SZ | 舒泰神    |        0.0397618 |      1.15468e+06 |  0         | 无                  |
|      20260330 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260331 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260401 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260402 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260403 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260407 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260408 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.15468e+06 |  0         |                    |
|      20260409 | A              | HISTORICAL_SIM_FILLED     | 300489.SZ | 光智科技   |        0.0826627 |      1.25012e+06 |  0         | 无                  |
|      20260410 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.25012e+06 |  0         |                    |
|      20260413 | B              | HISTORICAL_SIM_FILLED     | 000968.SZ | 蓝焰控股   |       -0.0496262 |      1.18808e+06 | -0.0496262 | 无                  |
|      20260414 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.18808e+06 | -0.0496262 |                    |
|      20260415 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.18808e+06 | -0.0496262 |                    |
|      20260416 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.18808e+06 | -0.0496262 |                    |
|      20260417 | A              | HISTORICAL_SIM_FILLED     | 301319.SZ | 唯特偶    |        0.0352808 |      1.23e+06    | -0.0160963 | 无                  |
|      20260420 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260421 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260422 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260423 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260424 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260427 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260428 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260429 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260430 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260506 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260507 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260508 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260511 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260512 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260513 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |
|      20260514 | NONE           | NO_CANDIDATE              |           |        |        0         |      1.23e+06    | -0.0160963 |                    |

## 被额外过滤的B交易

| residual_filter_scenario                     |   trade_date | ts_code   | name   |   strict_account_return | market_segment   | market_chain_count_bucket   |   open_times |   turnover_rate |
|:---------------------------------------------|-------------:|:----------|:-------|------------------------:|:-----------------|:----------------------------|-------------:|----------------:|
| reject_b_market_segment_sh_main              |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_market_segment_sh_main              |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_market_segment_sh_main              |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_market_segment_sh_main              |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_market_segment_sh_main              |     20260313 | 603248.SH | 锡华科技   |              0.141968   | sh_main          | 3_8                         |            2 |         46.3969 |
| reject_b_market_segment_sh_main              |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_market_segment_sh_main              |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_market_segment_sh_main              |     20260408 | 603083.SH | 剑桥科技   |              0.0205069  | sh_main          | 8_15                        |            1 |         13.7674 |
| reject_b_market_segment_sh_main              |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_market_segment_sh_main              |     20260507 | 603256.SH | 宏和科技   |              0.0246405  | sh_main          | 15_30                       |            0 |          2.5128 |
| reject_b_market_chain_gte_30                 |     20260106 | 002865.SZ | 钧达股份   |              0.158607   | sz_main          | gte_30                      |            0 |         15.8685 |
| reject_b_market_chain_gte_30                 |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_open_times_gte_4                    |     20251201 | 000078.SZ | 海王生物   |              0.148382   | sz_main          | 15_30                       |            5 |         20.3422 |
| reject_b_open_times_gte_4                    |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_open_times_gte_4                    |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_open_times_gte_4                    |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_open_times_gte_4                    |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_open_times_gte_4                    |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_open_times_gte_4                    |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_open_times_gte_4                    |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_open_times_gte_4                    |     20260413 | 000968.SZ | 蓝焰控股   |             -0.0496262  | sz_main          | 3_8                         |            8 |         12.3248 |
| reject_b_open_times_gte_4                    |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_open_times_gte_6                    |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_open_times_gte_6                    |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_open_times_gte_6                    |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_open_times_gte_6                    |     20260413 | 000968.SZ | 蓝焰控股   |             -0.0496262  | sz_main          | 3_8                         |            8 |         12.3248 |
| reject_b_open_times_gte_6                    |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_turnover_rate_gte_20                |     20251201 | 000078.SZ | 海王生物   |              0.148382   | sz_main          | 15_30                       |            5 |         20.3422 |
| reject_b_turnover_rate_gte_20                |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_turnover_rate_gte_20                |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_turnover_rate_gte_20                |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_turnover_rate_gte_20                |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_turnover_rate_gte_20                |     20260313 | 603248.SH | 锡华科技   |              0.141968   | sh_main          | 3_8                         |            2 |         46.3969 |
| reject_b_turnover_rate_gte_20                |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_turnover_bucket_15_25               |     20251201 | 000078.SZ | 海王生物   |              0.148382   | sz_main          | 15_30                       |            5 |         20.3422 |
| reject_b_turnover_bucket_15_25               |     20260106 | 002865.SZ | 钧达股份   |              0.158607   | sz_main          | gte_30                      |            0 |         15.8685 |
| reject_b_turnover_bucket_15_25               |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_turnover_bucket_15_25               |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_turnover_bucket_15_25               |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_turnover_bucket_15_25               |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_turnover_bucket_15_25               |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_turnover_bucket_15_25               |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_amount_lt_800k                      |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_amount_lt_800k                      |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_amount_lt_800k                      |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_amount_lt_800k                      |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_amount_lt_800k                      |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_retreat_warming_2day                |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_retreat_warming_2day                |     20260106 | 002865.SZ | 钧达股份   |              0.158607   | sz_main          | gte_30                      |            0 |         15.8685 |
| reject_b_retreat_warming_2day                |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_retreat_warming_2day                |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_retreat_warming_2day                |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_retreat_warming_2day                |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_retreat_warming_2day                |     20260408 | 603083.SH | 剑桥科技   |              0.0205069  | sh_main          | 8_15                        |            1 |         13.7674 |
| reject_b_retreat_warming_2day                |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_market_emotion_warming              |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_market_emotion_warming              |     20260106 | 002865.SZ | 钧达股份   |              0.158607   | sz_main          | gte_30                      |            0 |         15.8685 |
| reject_b_market_emotion_warming              |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_market_emotion_warming              |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_market_emotion_warming              |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_market_emotion_warming              |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_market_emotion_warming              |     20260408 | 603083.SH | 剑桥科技   |              0.0205069  | sh_main          | 8_15                        |            1 |         13.7674 |
| reject_b_market_emotion_warming              |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_sh_main_or_open_times_gte_4         |     20251201 | 000078.SZ | 海王生物   |              0.148382   | sz_main          | 15_30                       |            5 |         20.3422 |
| reject_b_sh_main_or_open_times_gte_4         |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_sh_main_or_open_times_gte_4         |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_sh_main_or_open_times_gte_4         |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_sh_main_or_open_times_gte_4         |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_sh_main_or_open_times_gte_4         |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_sh_main_or_open_times_gte_4         |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_sh_main_or_open_times_gte_4         |     20260313 | 603248.SH | 锡华科技   |              0.141968   | sh_main          | 3_8                         |            2 |         46.3969 |
| reject_b_sh_main_or_open_times_gte_4         |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_sh_main_or_open_times_gte_4         |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_sh_main_or_open_times_gte_4         |     20260408 | 603083.SH | 剑桥科技   |              0.0205069  | sh_main          | 8_15                        |            1 |         13.7674 |
| reject_b_sh_main_or_open_times_gte_4         |     20260413 | 000968.SZ | 蓝焰控股   |             -0.0496262  | sz_main          | 3_8                         |            8 |         12.3248 |
| reject_b_sh_main_or_open_times_gte_4         |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_sh_main_or_open_times_gte_4         |     20260507 | 603256.SH | 宏和科技   |              0.0246405  | sh_main          | 15_30                       |            0 |          2.5128 |
| reject_b_sh_main_or_chain_gte_30             |     20260106 | 002865.SZ | 钧达股份   |              0.158607   | sz_main          | gte_30                      |            0 |         15.8685 |
| reject_b_sh_main_or_chain_gte_30             |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_sh_main_or_chain_gte_30             |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_sh_main_or_chain_gte_30             |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_sh_main_or_chain_gte_30             |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_sh_main_or_chain_gte_30             |     20260313 | 603248.SH | 锡华科技   |              0.141968   | sh_main          | 3_8                         |            2 |         46.3969 |
| reject_b_sh_main_or_chain_gte_30             |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_sh_main_or_chain_gte_30             |     20260325 | 603829.SH | 洛凯股份   |              0.00178105 | sh_main          | 15_30                       |            0 |          4.4887 |
| reject_b_sh_main_or_chain_gte_30             |     20260408 | 603083.SH | 剑桥科技   |              0.0205069  | sh_main          | 8_15                        |            1 |         13.7674 |
| reject_b_sh_main_or_chain_gte_30             |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |
| reject_b_sh_main_or_chain_gte_30             |     20260507 | 603256.SH | 宏和科技   |              0.0246405  | sh_main          | 15_30                       |            0 |          2.5128 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20251201 | 000078.SZ | 海王生物   |              0.148382   | sz_main          | 15_30                       |            5 |         20.3422 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20251208 | 000571.SZ | 新大洲A   |              0.0312686  | sz_main          | 15_30                       |            5 |         12.9008 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260112 | 603015.SH | 弘讯科技   |             -0.113904   | sh_main          | gte_30                      |            4 |         20.2739 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260302 | 603257.SH | 中国瑞林   |             -0.027628   | sh_main          | 15_30                       |            9 |         43.5145 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260306 | 000509.SZ | 华塑控股   |              0.0220556  | sz_main          | 3_8                         |            8 |         15.514  |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260311 | 603778.SH | 国晟科技   |             -0.0155492  | sh_main          | 8_15                        |           16 |         22.4102 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260312 | 603092.SH | 德力佳    |              0.00651637 | sh_main          | 3_8                         |            4 |         20.878  |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260313 | 603248.SH | 锡华科技   |              0.141968   | sh_main          | 3_8                         |            2 |         46.3969 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260324 | 601016.SH | 节能风电   |              0.0500117  | sh_main          | 3_8                         |            4 |         19.4927 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260413 | 000968.SZ | 蓝焰控股   |             -0.0496262  | sz_main          | 3_8                         |            8 |         12.3248 |
| reject_b_open_times_gte_4_or_turnover_gte_20 |     20260416 | 603220.SH | 中贝通信   |             -0.0440896  | sh_main          | 3_8                         |            6 |         20.437  |

## 口径限制

- 所有过滤字段必须是 T 日收盘后已知字段。
- 该测试仍是日线保守成交模型，不是盘口五档真实撮合。
- 如果某个方案收益改善但样本数过少，不能直接升级为正式策略。
