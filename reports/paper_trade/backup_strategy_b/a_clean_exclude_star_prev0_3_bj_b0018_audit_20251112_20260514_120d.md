# 备用策略 B 日线保守成交审计

本报告只使用本地日线数据和保守成交模型，不接实盘，不调用 QMT，不下真实订单。

## 汇总

| scenario                        | condition                            |   day_count |   executed_trade_count |   b_trade_count |   a_trade_count |   buy_rejected_count |   sell_unresolved_count |   review_required_count |   no_candidate_count |   position_occupied_skip_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   profit_loss_ratio |   initial_equity |     final_equity |   equity_multiple |   max_drawdown |   path_conflict_count |   limit_down_blocked_trade_count | live_order_enabled   |
|:--------------------------------|:-------------------------------------|------------:|-----------------------:|----------------:|----------------:|---------------------:|------------------------:|------------------------:|---------------------:|-------------------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|--------------------:|-----------------:|-----------------:|------------------:|---------------:|----------------------:|---------------------------------:|:---------------------|
| A_strict                        | current_config                       |         120 |                     13 |               0 |              13 |                    0 |                       0 |                       3 |                   91 |                             13 |   0.769231 |            0.0473658 |               0.0397618 |     0.181313 | -0.0505146 |             1.84776 |           500000 | 890569           |           1.78114 |     -0.0505146 |                     0 |                                0 | False                |
| B_strict_on_A_no_candidate_days | segment_emotion_state_bucket=warming |          91 |                     20 |              20 |               0 |                    0 |                       0 |                       0 |                   54 |                             17 |   0.55     |            0.0167404 |               0.0151015 |     0.158607 | -0.120319  |             1.60083 |           500000 | 666268           |           1.33254 |     -0.120319  |                     0 |                                1 | False                |
| A_plus_B_strict                 | segment_emotion_state_bucket=warming |         120 |                     31 |              20 |              11 |                    0 |                       0 |                       1 |                   56 |                             32 |   0.645161 |            0.0306544 |               0.0246405 |     0.181313 | -0.120319  |             1.75329 |           500000 |      1.18785e+06 |           2.37571 |     -0.164756  |                     0 |                                1 | False                |

## 审计缺口

| check_item                 |   value | note                   |
|:---------------------------|--------:|:-----------------------|
| b_selected_signal_count    |      25 | B 条件选出的信号数             |
| b_buy_rejected_count       |       0 | 日线保守口径下 T+1 涨停开盘等买入失败  |
| b_sell_unresolved_count    |       0 | 日线保守口径下卖出未解决           |
| b_path_conflict_count      |       0 | 止盈止损同日路径冲突             |
| b_limit_down_blocked_count |       1 | 跌停阻塞卖出                 |
| minute_k_required          |      25 | 仍需分钟 K / 集合竞价 / 五档盘口验证 |

## A+B 严格逐日明细

|   signal_date | strategy_leg   | operation_status          | ts_code   | name   |   account_return |     equity_after |    drawdown | return_source                  | buy_reject_reason   | sell_reject_reason   |
|--------------:|:---------------|:--------------------------|:----------|:-------|-----------------:|-----------------:|------------:|:-------------------------------|:--------------------|:---------------------|
|      20251112 | NONE           | NO_CANDIDATE              |           |        |       0          | 500000           |  0          |                                |                     |                      |
|      20251113 | B              | HISTORICAL_SIM_FILLED     | 002780.SZ | 三夫户外   |      -0.120319   | 439840           | -0.120319   | b_conservative_daily_replay    |                     |                      |
|      20251114 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 439840           | -0.120319   |                                |                     |                      |
|      20251117 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 439840           | -0.120319   |                                |                     |                      |
|      20251118 | NONE           | NO_CANDIDATE              |           |        |       0          | 439840           | -0.120319   |                                |                     |                      |
|      20251119 | NONE           | NO_CANDIDATE              |           |        |       0          | 439840           | -0.120319   |                                |                     |                      |
|      20251120 | A              | HISTORICAL_SIM_FILLED     | 301092.SZ | 争光股份   |      -0.0505146  | 417622           | -0.164756   | a_audit_dynamic_account_return |                     |                      |
|      20251121 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 417622           | -0.164756   |                                |                     |                      |
|      20251124 | NONE           | NO_CANDIDATE              |           |        |       0          | 417622           | -0.164756   |                                |                     |                      |
|      20251125 | A              | REVIEW_REQUIRED_PLAN_ONLY | 301117.SZ | 佳缘科技   |       0          | 417622           | -0.164756   | manual_review_skip             |                     |                      |
|      20251126 | NONE           | NO_CANDIDATE              |           |        |       0          | 417622           | -0.164756   |                                |                     |                      |
|      20251127 | NONE           | NO_CANDIDATE              |           |        |       0          | 417622           | -0.164756   |                                |                     |                      |
|      20251128 | NONE           | NO_CANDIDATE              |           |        |       0          | 417622           | -0.164756   |                                |                     |                      |
|      20251201 | B              | HISTORICAL_SIM_FILLED     | 000078.SZ | 海王生物   |       0.148382   | 479589           | -0.040821   | b_conservative_daily_replay    |                     |                      |
|      20251202 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 479589           | -0.040821   |                                |                     |                      |
|      20251203 | NONE           | NO_CANDIDATE              |           |        |       0          | 479589           | -0.040821   |                                |                     |                      |
|      20251204 | A              | HISTORICAL_SIM_FILLED     | 300946.SZ | 恒而达    |       0.0291749  | 493581           | -0.012837   | a_audit_dynamic_account_return |                     |                      |
|      20251205 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 493581           | -0.012837   |                                |                     |                      |
|      20251208 | B              | HISTORICAL_SIM_FILLED     | 000571.SZ | 新大洲A   |       0.0312686  | 509015           |  0          | b_conservative_daily_replay    |                     |                      |
|      20251209 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 509015           |  0          |                                |                     |                      |
|      20251210 | NONE           | NO_CANDIDATE              |           |        |       0          | 509015           |  0          |                                |                     |                      |
|      20251211 | NONE           | NO_CANDIDATE              |           |        |       0          | 509015           |  0          |                                |                     |                      |
|      20251212 | A              | HISTORICAL_SIM_FILLED     | 920576.BJ | 天力复合   |       0.181313   | 601306           |  0          | a_audit_dynamic_account_return |                     |                      |
|      20251215 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 601306           |  0          |                                |                     |                      |
|      20251216 | NONE           | NO_CANDIDATE              |           |        |       0          | 601306           |  0          |                                |                     |                      |
|      20251217 | NONE           | NO_CANDIDATE              |           |        |       0          | 601306           |  0          |                                |                     |                      |
|      20251218 | B              | HISTORICAL_SIM_FILLED     | 002865.SZ | 钧达股份   |       0.0504471  | 631640           |  0          | b_conservative_daily_replay    |                     |                      |
|      20251219 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 631640           |  0          |                                |                     |                      |
|      20251222 | B              | HISTORICAL_SIM_FILLED     | 000407.SZ | 胜利股份   |      -0.00446606 | 628820           | -0.00446606 | b_conservative_daily_replay    |                     |                      |
|      20251223 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 628820           | -0.00446606 |                                |                     |                      |
|      20251224 | NONE           | NO_CANDIDATE              |           |        |       0          | 628820           | -0.00446606 |                                |                     |                      |
|      20251225 | B              | HISTORICAL_SIM_FILLED     | 001400.SZ | 江顺科技   |       0.0241444  | 644002           |  0          | b_conservative_daily_replay    |                     |                      |
|      20251226 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 644002           |  0          |                                |                     |                      |
|      20251229 | NONE           | NO_CANDIDATE              |           |        |       0          | 644002           |  0          |                                |                     |                      |
|      20251230 | NONE           | NO_CANDIDATE              |           |        |       0          | 644002           |  0          |                                |                     |                      |
|      20251231 | A              | HISTORICAL_SIM_FILLED     | 300058.SZ | 蓝色光标   |       0.130855   | 728273           |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260105 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 728273           |  0          |                                |                     |                      |
|      20260106 | B              | HISTORICAL_SIM_FILLED     | 002865.SZ | 钧达股份   |       0.158607   | 843782           |  0          | b_conservative_daily_replay    |                     |                      |
|      20260107 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 843782           |  0          |                                |                     |                      |
|      20260108 | NONE           | NO_CANDIDATE              |           |        |       0          | 843782           |  0          |                                |                     |                      |
|      20260109 | B              | HISTORICAL_SIM_FILLED     | 600410.SH | 华胜天成   |      -0.0639164  | 789850           | -0.0639164  | b_conservative_daily_replay    |                     |                      |
|      20260112 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 789850           | -0.0639164  |                                |                     |                      |
|      20260113 | NONE           | NO_CANDIDATE              |           |        |       0          | 789850           | -0.0639164  |                                |                     |                      |
|      20260114 | NONE           | NO_CANDIDATE              |           |        |       0          | 789850           | -0.0639164  |                                |                     |                      |
|      20260115 | A              | HISTORICAL_SIM_FILLED     | 301629.SZ | 矽电股份   |      -0.0338314  | 763129           | -0.0955854  | a_audit_dynamic_account_return |                     |                      |
|      20260116 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 763129           | -0.0955854  |                                |                     |                      |
|      20260119 | A              | HISTORICAL_SIM_FILLED     | 300658.SZ | 延江股份   |       0.0681174  | 815111           | -0.033979   | a_audit_dynamic_account_return |                     |                      |
|      20260120 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 815111           | -0.033979   |                                |                     |                      |
|      20260121 | NONE           | NO_CANDIDATE              |           |        |       0          | 815111           | -0.033979   |                                |                     |                      |
|      20260122 | NONE           | NO_CANDIDATE              |           |        |       0          | 815111           | -0.033979   |                                |                     |                      |
|      20260123 | A              | HISTORICAL_SIM_FILLED     | 920368.BJ | 连城数控   |       0.102773   | 898882           |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260126 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 898882           |  0          |                                |                     |                      |
|      20260127 | NONE           | NO_CANDIDATE              |           |        |       0          | 898882           |  0          |                                |                     |                      |
|      20260128 | A              | HISTORICAL_SIM_FILLED     | 300164.SZ | 通源石油   |       0.0457726  | 940026           |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260129 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 940026           |  0          |                                |                     |                      |
|      20260130 | NONE           | NO_CANDIDATE              |           |        |       0          | 940026           |  0          |                                |                     |                      |
|      20260202 | NONE           | NO_CANDIDATE              |           |        |       0          | 940026           |  0          |                                |                     |                      |
|      20260203 | B              | HISTORICAL_SIM_FILLED     | 002471.SZ | 中超控股   |       0.00814732 | 947685           |  0          | b_conservative_daily_replay    |                     |                      |
|      20260204 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 947685           |  0          |                                |                     |                      |
|      20260205 | NONE           | NO_CANDIDATE              |           |        |       0          | 947685           |  0          |                                |                     |                      |
|      20260206 | NONE           | NO_CANDIDATE              |           |        |       0          | 947685           |  0          |                                |                     |                      |
|      20260209 | B              | HISTORICAL_SIM_FILLED     | 601360.SH | 三六零    |      -0.0187769  | 929891           | -0.0187769  | b_conservative_daily_replay    |                     |                      |
|      20260210 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260211 | NONE           | NO_CANDIDATE              |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260212 | NONE           | NO_CANDIDATE              |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260213 | NONE           | NO_CANDIDATE              |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260224 | NONE           | NO_CANDIDATE              |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260225 | NONE           | NO_CANDIDATE              |           |        |       0          | 929891           | -0.0187769  |                                |                     |                      |
|      20260226 | A              | HISTORICAL_SIM_FILLED     | 300499.SZ | 高澜股份   |       0.0193949  | 947926           |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260227 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 947926           |  0          |                                |                     |                      |
|      20260302 | B              | HISTORICAL_SIM_FILLED     | 603257.SH | 中国瑞林   |      -0.027628   | 921736           | -0.027628   | b_conservative_daily_replay    |                     |                      |
|      20260303 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 921736           | -0.027628   |                                |                     |                      |
|      20260304 | NONE           | NO_CANDIDATE              |           |        |       0          | 921736           | -0.027628   |                                |                     |                      |
|      20260305 | NONE           | NO_CANDIDATE              |           |        |       0          | 921736           | -0.027628   |                                |                     |                      |
|      20260306 | B              | HISTORICAL_SIM_FILLED     | 000509.SZ | 华塑控股   |       0.0220556  | 942066           | -0.00618174 | b_conservative_daily_replay    |                     |                      |
|      20260309 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 942066           | -0.00618174 |                                |                     |                      |
|      20260310 | NONE           | NO_CANDIDATE              |           |        |       0          | 942066           | -0.00618174 |                                |                     |                      |
|      20260311 | B              | HISTORICAL_SIM_FILLED     | 603778.SH | 国晟科技   |      -0.0155492  | 927417           | -0.0216348  | b_conservative_daily_replay    |                     |                      |
|      20260312 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          | 927417           | -0.0216348  |                                |                     |                      |
|      20260313 | B              | HISTORICAL_SIM_FILLED     | 603248.SH | 锡华科技   |       0.141968   |      1.05908e+06 |  0          | b_conservative_daily_replay    |                     |                      |
|      20260316 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260317 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260318 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260319 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260320 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260323 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.05908e+06 |  0          |                                |                     |                      |
|      20260324 | B              | HISTORICAL_SIM_FILLED     | 601016.SH | 节能风电   |       0.0500117  |      1.11205e+06 |  0          | b_conservative_daily_replay    |                     |                      |
|      20260325 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.11205e+06 |  0          |                                |                     |                      |
|      20260326 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.11205e+06 |  0          |                                |                     |                      |
|      20260327 | A              | HISTORICAL_SIM_FILLED     | 300204.SZ | 舒泰神    |       0.0397618  |      1.15626e+06 |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260330 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.15626e+06 |  0          |                                |                     |                      |
|      20260331 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15626e+06 |  0          |                                |                     |                      |
|      20260401 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15626e+06 |  0          |                                |                     |                      |
|      20260402 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15626e+06 |  0          |                                |                     |                      |
|      20260403 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15626e+06 |  0          |                                |                     |                      |
|      20260407 | B              | HISTORICAL_SIM_FILLED     | 603090.SH | 宏盛股份   |       0.0251451  |      1.18534e+06 |  0          | b_conservative_daily_replay    |                     |                      |
|      20260408 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.18534e+06 |  0          |                                |                     |                      |
|      20260409 | A              | HISTORICAL_SIM_FILLED     | 300489.SZ | 光智科技   |       0.0826627  |      1.28332e+06 |  0          | a_audit_dynamic_account_return |                     |                      |
|      20260410 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.28332e+06 |  0          |                                |                     |                      |
|      20260413 | B              | HISTORICAL_SIM_FILLED     | 000968.SZ | 蓝焰控股   |      -0.0496262  |      1.21964e+06 | -0.0496262  | b_conservative_daily_replay    |                     |                      |
|      20260414 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.21964e+06 | -0.0496262  |                                |                     |                      |
|      20260415 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.21964e+06 | -0.0496262  |                                |                     |                      |
|      20260416 | B              | HISTORICAL_SIM_FILLED     | 603220.SH | 中贝通信   |      -0.0440896  |      1.16586e+06 | -0.0915278  | b_conservative_daily_replay    |                     |                      |
|      20260417 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260420 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260421 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260422 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260423 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260424 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.16586e+06 | -0.0915278  |                                |                     |                      |
|      20260427 | B              | HISTORICAL_SIM_FILLED     | 600152.SH | 维科技术   |      -0.00563805 |      1.15929e+06 | -0.0966498  | b_conservative_daily_replay    |                     |                      |
|      20260428 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.15929e+06 | -0.0966498  |                                |                     |                      |
|      20260429 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15929e+06 | -0.0966498  |                                |                     |                      |
|      20260430 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15929e+06 | -0.0966498  |                                |                     |                      |
|      20260506 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.15929e+06 | -0.0966498  |                                |                     |                      |
|      20260507 | B              | HISTORICAL_SIM_FILLED     | 603256.SH | 宏和科技   |       0.0246405  |      1.18785e+06 | -0.0743908  | b_conservative_daily_replay    |                     |                      |
|      20260508 | A_OR_B         | POSITION_OCCUPIED_SKIP    |           |        |       0          |      1.18785e+06 | -0.0743908  |                                |                     |                      |
|      20260511 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.18785e+06 | -0.0743908  |                                |                     |                      |
|      20260512 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.18785e+06 | -0.0743908  |                                |                     |                      |
|      20260513 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.18785e+06 | -0.0743908  |                                |                     |                      |
|      20260514 | NONE           | NO_CANDIDATE              |           |        |       0          |      1.18785e+06 | -0.0743908  |                                |                     |                      |

## 口径限制

- B 使用日线保守成交回放，不是盘口五档真实撮合。
- 涨停开盘默认买不到，跌停日默认无法卖出。
- 该结果只能判断 B 是否值得进入分钟 K / 盘口验证，不能直接用于实盘。
