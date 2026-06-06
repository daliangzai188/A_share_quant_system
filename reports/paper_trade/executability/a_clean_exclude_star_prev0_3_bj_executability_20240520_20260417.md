# 严格版策略实盘前可执行性审计

本报告只使用本地历史数据和模拟盘审计文件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

| strategy_name                                    |   start_date |   end_date |   trade_day_count |   executed_trade_count |   pass_trade_count |   review_trade_count |   review_trade_pct |   manual_review_required_day_count |   no_candidate_day_count |   position_occupied_skip_day_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   avg_buy_amount_ratio |   max_buy_amount_ratio |   avg_sell_amount_ratio |   max_sell_amount_ratio |   avg_buy_slippage |   max_buy_slippage |   avg_sell_slippage |   max_sell_slippage |   limit_down_blocked_trade_count |   buy_not_executed_count |   sell_not_executed_count |   path_conflict_count | trade_mode   | live_trading_enabled   | broker_adapter_enabled   | qmt_enabled   | paper_allow_live_order   | batch_allow_live_order   |
|:-------------------------------------------------|-------------:|-----------:|------------------:|-----------------------:|-------------------:|---------------------:|-------------------:|-----------------------------------:|-------------------------:|-----------------------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|-----------------------:|-----------------------:|------------------------:|------------------------:|-------------------:|-------------------:|--------------------:|--------------------:|---------------------------------:|-------------------------:|--------------------------:|----------------------:|:-------------|:-----------------------|:-------------------------|:--------------|:-------------------------|:-------------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter |     20240520 |   20260417 |               465 |                     59 |                 52 |                    7 |           0.118644 |                                  5 |                      394 |                                 12 |   0.728814 |            0.0734519 |               0.0454512 |     0.498054 |   -0.10726 |             0.00556046 |              0.0210051 |              0.00668585 |               0.0315139 |         0.00215254 |               0.01 |          0.00252542 |                0.01 |                                0 |                        0 |                         0 |                     0 | paper        | False                  | False                    | False         | False                    | False                    |

## 问题类型统计

| issue                  |   trade_count |   trade_pct |
|:-----------------------|--------------:|------------:|
| loss_overlay_watch     |             5 |   0.0847458 |
| high_sell_slippage     |             3 |   0.0508475 |
| high_sell_amount_ratio |             1 |   0.0169492 |
| high_buy_slippage      |             1 |   0.0169492 |
| buy_not_executed       |             0 |   0         |
| sell_not_executed      |             0 |   0         |
| high_buy_amount_ratio  |             0 |   0         |
| limit_down_blocked     |             0 |   0         |
| path_conflict          |             0 |   0         |
| low_fill_probability   |             0 |   0         |
| fill_score_unreliable  |             0 |   0         |

## 需要复核的交易

|   trade_date | ts_code   | name   |   dynamic_account_return |   buy_amount_ratio |   sell_amount_ratio |   dynamic_buy_slippage_rate |   dynamic_sell_slippage_rate |   limit_down_blocked_days | issue_labels                                                |
|-------------:|:----------|:-------|-------------------------:|-------------------:|--------------------:|----------------------------:|-----------------------------:|--------------------------:|:------------------------------------------------------------|
|     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  |         0.0070189  |          0.0112191  |                       0.002 |                        0.005 |                         0 | loss_overlay_watch                                          |
|     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 |         0.0168136  |          0.023983   |                       0.005 |                        0.01  |                         0 | high_sell_slippage;loss_overlay_watch                       |
|     20251120 | 301092.SZ | 争光股份   |               -0.0505146 |         0.0210051  |          0.0315139  |                       0.01  |                        0.01  |                         0 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
|     20251125 | 301117.SZ | 佳缘科技   |               -0.059315  |         0.00563884 |          0.00747403 |                       0.002 |                        0.002 |                         0 | loss_overlay_watch                                          |
|     20251219 | 300557.SZ | 理工光科   |               -0.10726   |         0.0125252  |          0.018835   |                       0.005 |                        0.005 |                         0 | loss_overlay_watch                                          |
|     20260309 | 301606.SZ | 绿联科技   |               -0.0506941 |         0.00945017 |          0.00839797 |                       0.002 |                        0.002 |                         0 | loss_overlay_watch                                          |
|     20260417 | 301319.SZ | 唯特偶    |                0.0352808 |         0.0158819  |          0.0200577  |                       0.005 |                        0.01  |                         0 | high_sell_slippage                                          |

## 解释限制

- 当前买入价格口径是 T+1 开盘价加动态滑点，不是逐笔盘口五档真实撮合。
- 当前卖出价格口径是 T+2 收盘价减动态滑点，不是逐笔盘口五档真实撮合。
- `PASS` 只代表当前日线审计口径下未触发预警，不代表可以实盘。
- 后续必须继续用分钟 K、集合竞价、盘口五档、跌停排队卖出验证关键交易。
