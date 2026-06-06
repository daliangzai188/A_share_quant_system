# 策略发布前稳定性验证

本报告用于策略版本发布前验证，不用于每日改策略。不接实盘，不调用 QMT，不下真实订单。

## 发布结论

| strategy_label               | strategy_name                                    | validation_time     | release_status                                 | all_gates_passed   |   oos_start_date |   oos_end_date |   validated_window_count | rebalance_cycle        | trade_mode   | live_order_enabled   | minute_k_required_before_live   | decision_note                  |
|:-----------------------------|:-------------------------------------------------|:--------------------|:-----------------------------------------------|:-------------------|-----------------:|---------------:|-------------------------:|:-----------------------|:-------------|:---------------------|:--------------------------------|:-------------------------------|
| a_strict_plus_b0018_filtered | a_clean_plus_exclude_star_prev0_3_bj_risk_filter | 2026-06-06 10:05:54 | PASS_PAPER_READY_REVIEW_ONLY_MINUTE_K_REQUIRED | True               |         20260101 |       20260518 |                        4 | quarterly_or_half_year | paper        | False                | True                            | 可进入下一阶段模拟/小资金人工确认前复核；仍不允许自动实盘。 |

## 窗口表现

| window_label   |   start_date |   end_date |   executed_trade_count |   a_trade_count |   b_trade_count |   win_rate |   equity_multiple |   max_drawdown |   max_loss |   limit_down_blocked_trade_count |
|:---------------|-------------:|-----------:|-----------------------:|----------------:|----------------:|-----------:|------------------:|---------------:|-----------:|---------------------------------:|
| oos_range      |     20260105 |   20260514 |                     13 |               8 |               5 |   0.846154 |           1.68909 |     -0.0350052 | -0.0350052 |                                0 |
| recent_60d     |     20260206 |   20260514 |                      8 |               4 |               4 |   0.875    |           1.2667  |     -0.0350052 | -0.0350052 |                                0 |
| recent_90d     |     20251224 |   20260514 |                     14 |               9 |               5 |   0.857143 |           1.9752  |     -0.0350052 | -0.0350052 |                                0 |
| recent_120d    |     20251112 |   20260514 |                     17 |              12 |               5 |   0.823529 |           2.2801  |     -0.0505146 | -0.0505146 |                                0 |

## 阈值检查

| window_label   | gate_name                          |     actual | operator   |   threshold | passed   |
|:---------------|:-----------------------------------|-----------:|:-----------|------------:|:---------|
| oos_range      | min_equity_multiple                |  1.68909   | >=         |        1.05 | True     |
| oos_range      | min_win_rate                       |  0.846154  | >=         |        0.6  | True     |
| oos_range      | max_drawdown_abs                   |  0.0350052 | <=         |        0.12 | True     |
| oos_range      | max_single_loss_abs                |  0.0350052 | <=         |        0.08 | True     |
| oos_range      | min_executed_trade_count           | 13         | >=         |        5    | True     |
| oos_range      | max_b_trade_share                  |  0.384615  | <=         |        0.55 | True     |
| oos_range      | max_limit_down_blocked_trade_count |  0         | <=         |        0    | True     |
| recent_60d     | min_equity_multiple                |  1.2667    | >=         |        1.05 | True     |
| recent_60d     | min_win_rate                       |  0.875     | >=         |        0.6  | True     |
| recent_60d     | max_drawdown_abs                   |  0.0350052 | <=         |        0.12 | True     |
| recent_60d     | max_single_loss_abs                |  0.0350052 | <=         |        0.08 | True     |
| recent_60d     | min_executed_trade_count           |  8         | >=         |        5    | True     |
| recent_60d     | max_b_trade_share                  |  0.5       | <=         |        0.55 | True     |
| recent_60d     | max_limit_down_blocked_trade_count |  0         | <=         |        0    | True     |
| recent_90d     | min_equity_multiple                |  1.9752    | >=         |        1.05 | True     |
| recent_90d     | min_win_rate                       |  0.857143  | >=         |        0.6  | True     |
| recent_90d     | max_drawdown_abs                   |  0.0350052 | <=         |        0.12 | True     |
| recent_90d     | max_single_loss_abs                |  0.0350052 | <=         |        0.08 | True     |
| recent_90d     | min_executed_trade_count           | 14         | >=         |        5    | True     |
| recent_90d     | max_b_trade_share                  |  0.357143  | <=         |        0.55 | True     |
| recent_90d     | max_limit_down_blocked_trade_count |  0         | <=         |        0    | True     |
| recent_120d    | min_equity_multiple                |  2.2801    | >=         |        1.05 | True     |
| recent_120d    | min_win_rate                       |  0.823529  | >=         |        0.6  | True     |
| recent_120d    | max_drawdown_abs                   |  0.0505146 | <=         |        0.12 | True     |
| recent_120d    | max_single_loss_abs                |  0.0505146 | <=         |        0.08 | True     |
| recent_120d    | min_executed_trade_count           | 17         | >=         |        5    | True     |
| recent_120d    | max_b_trade_share                  |  0.294118  | <=         |        0.55 | True     |
| recent_120d    | max_limit_down_blocked_trade_count |  0         | <=         |        0    | True     |

## 解释

- PASS 只代表当前固定口径通过本地发布前验证，不代表可以自动实盘。
- 仍需分钟 K、集合竞价、盘口五档和小资金人工确认验证。
- 发布后不应每日改参数；建议按季度或半年重新训练和重新发布。
