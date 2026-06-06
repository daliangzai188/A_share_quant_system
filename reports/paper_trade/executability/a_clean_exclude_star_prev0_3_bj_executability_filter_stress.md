# 严格版可执行性复核项压力测试

本报告只使用本地可执行性审计明细，不接实盘，不调用 QMT，不下真实订单。

## 方案汇总

| scenario                               |   equity_multiple |   kept_trade_count |   rejected_trade_count |   win_rate |   avg_account_return |   max_loss |   max_drawdown |   rejected_avg_account_return |
|:---------------------------------------|------------------:|-------------------:|-----------------------:|-----------:|---------------------:|-----------:|---------------:|------------------------------:|
| diagnostic_reject_negative_review_only |           66.6479 |                 53 |                      6 |   0.811321 |            0.0888981 | -0.0714087 |     -0.0714087 |                    -0.0629891 |
| reject_loss_overlay_or_slippage        |           64.3766 |                 52 |                      7 |   0.807692 |            0.0899292 | -0.0714087 |     -0.0714087 |                    -0.0489506 |
| reject_any_executability_review        |           64.3766 |                 52 |                      7 |   0.807692 |            0.0899292 | -0.0714087 |     -0.0714087 |                    -0.0489506 |
| reject_loss_overlay_watch              |           63.2812 |                 54 |                      5 |   0.796296 |            0.0863164 | -0.0714087 |     -0.0714087 |                    -0.065484  |
| reject_amount_ratio_warnings           |           47.4011 |                 58 |                      1 |   0.741379 |            0.0755893 | -0.10726   |     -0.110076  |                    -0.0505146 |
| reject_slippage_warnings               |           47.0857 |                 56 |                      3 |   0.75     |            0.0781519 | -0.10726   |     -0.110076  |                    -0.0142805 |
| baseline_keep_all                      |           45.0067 |                 59 |                      0 |   0.728814 |            0.0734519 | -0.10726   |     -0.110076  |                     0         |

## 被跳过交易明细

| scenario                               |   trade_date | ts_code   | name   |   dynamic_account_return | issue_labels                                                |
|:---------------------------------------|-------------:|:----------|:-------|-------------------------:|:------------------------------------------------------------|
| reject_loss_overlay_watch              |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | loss_overlay_watch                                          |
| reject_loss_overlay_watch              |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | high_sell_slippage;loss_overlay_watch                       |
| reject_loss_overlay_watch              |     20251125 | 301117.SZ | 佳缘科技   |               -0.059315  | loss_overlay_watch                                          |
| reject_loss_overlay_watch              |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | loss_overlay_watch                                          |
| reject_loss_overlay_watch              |     20260309 | 301606.SZ | 绿联科技   |               -0.0506941 | loss_overlay_watch                                          |
| reject_slippage_warnings               |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | high_sell_slippage;loss_overlay_watch                       |
| reject_slippage_warnings               |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
| reject_slippage_warnings               |     20260417 | 301319.SZ | 唯特偶    |                0.0352808 | high_sell_slippage                                          |
| reject_amount_ratio_warnings           |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
| reject_loss_overlay_or_slippage        |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | loss_overlay_watch                                          |
| reject_loss_overlay_or_slippage        |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | high_sell_slippage;loss_overlay_watch                       |
| reject_loss_overlay_or_slippage        |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
| reject_loss_overlay_or_slippage        |     20251125 | 301117.SZ | 佳缘科技   |               -0.059315  | loss_overlay_watch                                          |
| reject_loss_overlay_or_slippage        |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | loss_overlay_watch                                          |
| reject_loss_overlay_or_slippage        |     20260309 | 301606.SZ | 绿联科技   |               -0.0506941 | loss_overlay_watch                                          |
| reject_loss_overlay_or_slippage        |     20260417 | 301319.SZ | 唯特偶    |                0.0352808 | high_sell_slippage                                          |
| reject_any_executability_review        |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | loss_overlay_watch                                          |
| reject_any_executability_review        |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | high_sell_slippage;loss_overlay_watch                       |
| reject_any_executability_review        |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
| reject_any_executability_review        |     20251125 | 301117.SZ | 佳缘科技   |               -0.059315  | loss_overlay_watch                                          |
| reject_any_executability_review        |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | loss_overlay_watch                                          |
| reject_any_executability_review        |     20260309 | 301606.SZ | 绿联科技   |               -0.0506941 | loss_overlay_watch                                          |
| reject_any_executability_review        |     20260417 | 301319.SZ | 唯特偶    |                0.0352808 | high_sell_slippage                                          |
| diagnostic_reject_negative_review_only |     20241227 | 300599.SZ | 雄塑科技   |               -0.082543  | loss_overlay_watch                                          |
| diagnostic_reject_negative_review_only |     20251028 | 920748.BJ | 路桥信息   |               -0.0276078 | high_sell_slippage;loss_overlay_watch                       |
| diagnostic_reject_negative_review_only |     20251120 | 301092.SZ | 争光股份   |               -0.0505146 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage |
| diagnostic_reject_negative_review_only |     20251125 | 301117.SZ | 佳缘科技   |               -0.059315  | loss_overlay_watch                                          |
| diagnostic_reject_negative_review_only |     20251219 | 300557.SZ | 理工光科   |               -0.10726   | loss_overlay_watch                                          |
| diagnostic_reject_negative_review_only |     20260309 | 301606.SZ | 绿联科技   |               -0.0506941 | loss_overlay_watch                                          |

## 分钟 K / 盘口验证目标

|   trade_date | ts_code   | name   |   buy_trade_date |   exit_trade_date | issue_labels                                                | validation_focus                          |
|-------------:|:----------|:-------|-----------------:|------------------:|:------------------------------------------------------------|:------------------------------------------|
|     20241227 | 300599.SZ | 雄塑科技   |         20241230 |          20241231 | loss_overlay_watch                                          | 复核是否应升级为硬过滤                               |
|     20251028 | 920748.BJ | 路桥信息   |         20251029 |          20251030 | high_sell_slippage;loss_overlay_watch                       | 复核是否应升级为硬过滤；验证T+2卖出盘口冲击                   |
|     20251120 | 301092.SZ | 争光股份   |         20251121 |          20251124 | high_sell_amount_ratio;high_buy_slippage;high_sell_slippage | 验证T+1开盘买入盘口冲击；验证T+2卖出盘口冲击；验证计划金额相对成交额是否过大 |
|     20251125 | 301117.SZ | 佳缘科技   |         20251126 |          20251127 | loss_overlay_watch                                          | 复核是否应升级为硬过滤                               |
|     20251219 | 300557.SZ | 理工光科   |         20251222 |          20251223 | loss_overlay_watch                                          | 复核是否应升级为硬过滤                               |
|     20260309 | 301606.SZ | 绿联科技   |         20260310 |          20260311 | loss_overlay_watch                                          | 复核是否应升级为硬过滤                               |
|     20260417 | 301319.SZ | 唯特偶    |         20260420 |          20260421 | high_sell_slippage                                          | 验证T+2卖出盘口冲击                               |

## 解释限制

- `diagnostic_reject_negative_review_only` 使用了事后盈亏，只能用于理解风险来源，不能直接写入策略。
- 其他方案也只是基于日线审计风险标签的压力测试，是否升级为正式硬过滤还要做样本外和模拟盘验证。
- 当前结果不代表可以实盘，后续仍需分钟 K、集合竞价和盘口五档验证。
