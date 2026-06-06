# 备用策略 B 风险过滤压力测试

本报告只使用本地 CSV 做模拟盘风险过滤压力测试，不接实盘，不调用 QMT，不下真实订单。

## 汇总

| scenario                             | filter_type                   | description                                     |   rejected_b_trade_count |   executed_trade_count |   b_trade_count |   a_trade_count |   filtered_skip_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   profit_loss_ratio |   initial_equity |     final_equity |   equity_multiple |   max_drawdown | live_order_enabled   |
|:-------------------------------------|:------------------------------|:------------------------------------------------|-------------------------:|-----------------------:|----------------:|----------------:|----------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|--------------------:|-----------------:|-----------------:|------------------:|---------------:|:---------------------|
| baseline_no_extra_filter             | baseline                      | B_0018 原始日线保守审计结果，不额外过滤。                        |                        0 |                     31 |              20 |              11 |                     0 |   0.645161 |            0.0306544 |               0.0246405 |     0.181313 | -0.120319  |             1.75329 |           500000 |      1.18785e+06 |           2.37571 |     -0.164756  | False                |
| diagnostic_reject_negative_b         | diagnostic_future_not_allowed | 诊断过滤：过滤未来亏损 B 交易。不能作为实盘事前条件。                    |                       11 |                     22 |              11 |              11 |                     9 |   0.909091 |            0.0591044 |               0.0427672 |     0.181313 | -0.0505146 |             1.64162 |           500000 |      1.70772e+06 |           3.41544 |     -0.0505146 | False                |
| diagnostic_reject_limit_down_blocked | diagnostic_future_not_allowed | 诊断过滤：过滤未来发生跌停阻塞的交易。不能作为实盘事前条件。                  |                        1 |                     30 |              19 |              11 |                     1 |   0.666667 |            0.0356869 |               0.0248928 |     0.181313 | -0.0639164 |             2.20459 |           500000 |      1.35033e+06 |           2.70065 |     -0.0966498 | False                |
| reject_fd_warn_or_loss_overlay       | pre_trade                     | 过滤 封单/流通市值偏高 或 LOSS_OVERLAY_WATCH。              |                       10 |                     22 |              11 |              11 |                     9 |   0.727273 |            0.0479782 |               0.0355152 |     0.181313 | -0.0505146 |             2.16411 |           500000 |      1.33571e+06 |           2.67141 |     -0.0915278 | False                |
| reject_fd_amount_warn                | pre_trade                     | 过滤风险标记含 封单/流通市值偏高。                              |                        7 |                     24 |              13 |              11 |                     7 |   0.666667 |            0.0429627 |               0.0302218 |     0.181313 | -0.0505146 |             2.59869 |           500000 |      1.30324e+06 |           2.60647 |     -0.0966498 | False                |
| reject_fd_1pct_2pct                  | pre_trade                     | 过滤 fd_ratio_bucket=1pct_2pct。                   |                        7 |                     24 |              13 |              11 |                     7 |   0.666667 |            0.0429627 |               0.0302218 |     0.181313 | -0.0505146 |             2.59869 |           500000 |      1.30324e+06 |           2.60647 |     -0.0966498 | False                |
| reject_loss_overlay_watch            | pre_trade                     | 过滤 LOSS_OVERLAY_WATCH。                          |                        3 |                     29 |              18 |              11 |                     2 |   0.689655 |            0.0336104 |               0.0251451 |     0.181313 | -0.120319  |             1.51995 |           500000 |      1.21745e+06 |           2.4349  |     -0.164756  | False                |
| reject_market_chain_15_30            | pre_trade                     | 过滤 market_chain_count_bucket=15_30。             |                       11 |                     22 |              11 |              11 |                     9 |   0.681818 |            0.0403516 |               0.02716   |     0.181313 | -0.0505146 |             2.36679 |           500000 |      1.14149e+06 |           2.28297 |     -0.0966498 | False                |
| reject_fd_warn_or_chain_15_30        | pre_trade                     | 过滤 封单/流通市值偏高 或 market_chain_count_bucket=15_30。 |                       13 |                     20 |               9 |              11 |                    11 |   0.65     |            0.0427221 |               0.0344684 |     0.181313 | -0.0505146 |             2.64869 |           500000 |      1.10449e+06 |           2.20898 |     -0.0966498 | False                |
| reject_market_chain_gte_30           | pre_trade                     | 过滤 market_chain_count_bucket=gte_30。            |                        2 |                     30 |              19 |              11 |                     1 |   0.633333 |            0.0263893 |               0.0243924 |     0.181313 | -0.120319  |             1.63417 |           500000 |      1.02524e+06 |           2.05049 |     -0.164756  | False                |
| reject_chain_15_30_or_gte_30         | pre_trade                     | 过滤 market_chain_count_bucket=15_30 或 gte_30。    |                       13 |                     21 |              10 |              11 |                    10 |   0.666667 |            0.0347204 |               0.0251451 |     0.181313 | -0.0505146 |             2.17211 |           500000 | 985222           |           1.97044 |     -0.0966498 | False                |

## 最优事前过滤方案逐日明细

|   signal_date | strategy_leg   | operation_status          | ts_code   | name   |   account_return |     equity_after |    drawdown | risk_filter_rejected   | risk_flags         |
|--------------:|:---------------|:--------------------------|:----------|:-------|-----------------:|-----------------:|------------:|:-----------------------|:-------------------|
|      20251112 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 500000           |  0          | False                  | nan                |
|      20251113 | B              | B_RISK_FILTERED_SKIP      | 002780.SZ | 三夫户外   |        0         | 500000           |  0          | True                   | 封单/流通市值偏高          |
|      20251114 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 500000           |  0          | False                  | nan                |
|      20251117 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 500000           |  0          | False                  | nan                |
|      20251118 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 500000           |  0          | False                  | nan                |
|      20251119 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 500000           |  0          | False                  | nan                |
|      20251120 | A              | HISTORICAL_SIM_FILLED     | 301092.SZ | 争光股份   |       -0.0505146 | 474743           | -0.0505146  | False                  | 无                  |
|      20251121 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 474743           | -0.0505146  | False                  | nan                |
|      20251124 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 474743           | -0.0505146  | False                  | nan                |
|      20251125 | A              | REVIEW_REQUIRED_PLAN_ONLY | 301117.SZ | 佳缘科技   |        0         | 474743           | -0.0505146  | False                  | LOSS_OVERLAY_WATCH |
|      20251126 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 474743           | -0.0505146  | False                  | nan                |
|      20251127 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 474743           | -0.0505146  | False                  | nan                |
|      20251128 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 474743           | -0.0505146  | False                  | nan                |
|      20251201 | B              | HISTORICAL_SIM_FILLED     | 000078.SZ | 海王生物   |        0.148382  | 545186           |  0          | False                  | 无                  |
|      20251202 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 545186           |  0          | False                  | nan                |
|      20251203 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 545186           |  0          | False                  | nan                |
|      20251204 | A              | HISTORICAL_SIM_FILLED     | 300946.SZ | 恒而达    |        0.0291749 | 561092           |  0          | False                  | 无                  |
|      20251205 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 561092           |  0          | False                  | nan                |
|      20251208 | B              | HISTORICAL_SIM_FILLED     | 000571.SZ | 新大洲A   |        0.0312686 | 578636           |  0          | False                  | 无                  |
|      20251209 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 578636           |  0          | False                  | nan                |
|      20251210 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 578636           |  0          | False                  | nan                |
|      20251211 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 578636           |  0          | False                  | nan                |
|      20251212 | A              | HISTORICAL_SIM_FILLED     | 920576.BJ | 天力复合   |        0.181313  | 683551           |  0          | False                  | 无                  |
|      20251215 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251216 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251217 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251218 | B              | B_RISK_FILTERED_SKIP      | 002865.SZ | 钧达股份   |        0         | 683551           |  0          | True                   | 封单/流通市值偏高          |
|      20251219 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251222 | B              | B_RISK_FILTERED_SKIP      | 000407.SZ | 胜利股份   |        0         | 683551           |  0          | True                   | 封单/流通市值偏高          |
|      20251223 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251224 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251225 | B              | B_RISK_FILTERED_SKIP      | 001400.SZ | 江顺科技   |        0         | 683551           |  0          | True                   | 封单/流通市值偏高          |
|      20251226 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251229 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251230 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 683551           |  0          | False                  | nan                |
|      20251231 | A              | HISTORICAL_SIM_FILLED     | 300058.SZ | 蓝色光标   |        0.130855  | 772997           |  0          | False                  | 无                  |
|      20260105 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 772997           |  0          | False                  | nan                |
|      20260106 | B              | HISTORICAL_SIM_FILLED     | 002865.SZ | 钧达股份   |        0.158607  | 895599           |  0          | False                  | 无                  |
|      20260107 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 895599           |  0          | False                  | nan                |
|      20260108 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 895599           |  0          | False                  | nan                |
|      20260109 | B              | B_RISK_FILTERED_SKIP      | 600410.SH | 华胜天成   |        0         | 895599           |  0          | True                   | 封单/流通市值偏高          |
|      20260112 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 895599           |  0          | False                  | nan                |
|      20260113 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 895599           |  0          | False                  | nan                |
|      20260114 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 895599           |  0          | False                  | nan                |
|      20260115 | A              | HISTORICAL_SIM_FILLED     | 301629.SZ | 矽电股份   |       -0.0338314 | 865300           | -0.0338314  | False                  | 无                  |
|      20260116 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 865300           | -0.0338314  | False                  | nan                |
|      20260119 | A              | HISTORICAL_SIM_FILLED     | 300658.SZ | 延江股份   |        0.0681174 | 924242           |  0          | False                  | 无                  |
|      20260120 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         | 924242           |  0          | False                  | nan                |
|      20260121 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 924242           |  0          | False                  | nan                |
|      20260122 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         | 924242           |  0          | False                  | nan                |
|      20260123 | A              | HISTORICAL_SIM_FILLED     | 920368.BJ | 连城数控   |        0.102773  |      1.01923e+06 |  0          | False                  | 无                  |
|      20260126 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.01923e+06 |  0          | False                  | nan                |
|      20260127 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.01923e+06 |  0          | False                  | nan                |
|      20260128 | A              | HISTORICAL_SIM_FILLED     | 300164.SZ | 通源石油   |        0.0457726 |      1.06588e+06 |  0          | False                  | 无                  |
|      20260129 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260130 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260202 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260203 | B              | B_RISK_FILTERED_SKIP      | 002471.SZ | 中超控股   |        0         |      1.06588e+06 |  0          | True                   | 封单/流通市值偏高          |
|      20260204 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260205 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260206 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260209 | B              | B_RISK_FILTERED_SKIP      | 601360.SH | 三六零    |        0         |      1.06588e+06 |  0          | True                   | LOSS_OVERLAY_WATCH |
|      20260210 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260211 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260212 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260213 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260224 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260225 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.06588e+06 |  0          | False                  | nan                |
|      20260226 | A              | HISTORICAL_SIM_FILLED     | 300499.SZ | 高澜股份   |        0.0193949 |      1.08655e+06 |  0          | False                  | 无                  |
|      20260227 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.08655e+06 |  0          | False                  | nan                |
|      20260302 | B              | HISTORICAL_SIM_FILLED     | 603257.SH | 中国瑞林   |       -0.027628  |      1.05653e+06 | -0.027628   | False                  | 无                  |
|      20260303 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.05653e+06 | -0.027628   | False                  | nan                |
|      20260304 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.05653e+06 | -0.027628   | False                  | nan                |
|      20260305 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.05653e+06 | -0.027628   | False                  | nan                |
|      20260306 | B              | HISTORICAL_SIM_FILLED     | 000509.SZ | 华塑控股   |        0.0220556 |      1.07984e+06 | -0.00618174 | False                  | 无                  |
|      20260309 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.07984e+06 | -0.00618174 | False                  | nan                |
|      20260310 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.07984e+06 | -0.00618174 | False                  | nan                |
|      20260311 | B              | HISTORICAL_SIM_FILLED     | 603778.SH | 国晟科技   |       -0.0155492 |      1.06305e+06 | -0.0216348  | False                  | 无                  |
|      20260312 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.06305e+06 | -0.0216348  | False                  | nan                |
|      20260313 | B              | HISTORICAL_SIM_FILLED     | 603248.SH | 锡华科技   |        0.141968  |      1.21397e+06 |  0          | False                  | 无                  |
|      20260316 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260317 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260318 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260319 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260320 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260323 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.21397e+06 |  0          | False                  | nan                |
|      20260324 | B              | HISTORICAL_SIM_FILLED     | 601016.SH | 节能风电   |        0.0500117 |      1.27468e+06 |  0          | False                  | 无                  |
|      20260325 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.27468e+06 |  0          | False                  | nan                |
|      20260326 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.27468e+06 |  0          | False                  | nan                |
|      20260327 | A              | HISTORICAL_SIM_FILLED     | 300204.SZ | 舒泰神    |        0.0397618 |      1.32536e+06 |  0          | False                  | 无                  |
|      20260330 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260331 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260401 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260402 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260403 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260407 | B              | B_RISK_FILTERED_SKIP      | 603090.SH | 宏盛股份   |        0         |      1.32536e+06 |  0          | True                   | 封单/流通市值偏高          |
|      20260408 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.32536e+06 |  0          | False                  | nan                |
|      20260409 | A              | HISTORICAL_SIM_FILLED     | 300489.SZ | 光智科技   |        0.0826627 |      1.43492e+06 |  0          | False                  | 无                  |
|      20260410 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.43492e+06 |  0          | False                  | nan                |
|      20260413 | B              | HISTORICAL_SIM_FILLED     | 000968.SZ | 蓝焰控股   |       -0.0496262 |      1.36371e+06 | -0.0496262  | False                  | 无                  |
|      20260414 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.36371e+06 | -0.0496262  | False                  | nan                |
|      20260415 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.36371e+06 | -0.0496262  | False                  | nan                |
|      20260416 | B              | HISTORICAL_SIM_FILLED     | 603220.SH | 中贝通信   |       -0.0440896 |      1.30358e+06 | -0.0915278  | False                  | 无                  |
|      20260417 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260420 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260421 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260422 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260423 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260424 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260427 | B              | B_RISK_FILTERED_SKIP      | 600152.SH | 维科技术   |        0         |      1.30358e+06 | -0.0915278  | True                   | LOSS_OVERLAY_WATCH |
|      20260428 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260429 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260430 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260506 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.30358e+06 | -0.0915278  | False                  | nan                |
|      20260507 | B              | HISTORICAL_SIM_FILLED     | 603256.SH | 宏和科技   |        0.0246405 |      1.33571e+06 | -0.0691426  | False                  | 无                  |
|      20260508 | A_OR_B         | POSITION_OCCUPIED_SKIP    | nan       | nan    |        0         |      1.33571e+06 | -0.0691426  | False                  | nan                |
|      20260511 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.33571e+06 | -0.0691426  | False                  | nan                |
|      20260512 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.33571e+06 | -0.0691426  | False                  | nan                |
|      20260513 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.33571e+06 | -0.0691426  | False                  | nan                |
|      20260514 | NONE           | NO_CANDIDATE              | nan       | nan    |        0         |      1.33571e+06 | -0.0691426  | False                  | nan                |

## 口径限制

- `pre_trade` 过滤是事前可见字段，可以进入下一轮验证。
- `diagnostic_future_not_allowed` 是未来结果诊断，不能用于实盘或模拟盘事前过滤。
- 风险过滤后的结果仍然是日线保守口径，后续还需要分钟 K、集合竞价和五档盘口验证。
