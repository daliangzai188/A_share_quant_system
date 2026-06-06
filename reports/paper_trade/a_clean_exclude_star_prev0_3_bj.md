# 本地模拟盘账本报告

本报告只读取本地审计逐笔交易文件，不调用外部接口，不接实盘，不下真实订单。

## 策略

- 策略名：`a_clean_plus_exclude_star_prev0_3_bj_risk_filter`
- 模式：`paper` / `historical_replay`
- 输入文件：`/Users/user/Desktop/A_System/reports/a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv`

## 汇总

| strategy_name                                    | trade_mode   | paper_mode        | input_trades_path                                                                          |   initial_cash |   final_equity |   equity_multiple |   signal_count |   order_count |   fill_count |   executed_trade_count |   buy_order_filled_count |   sell_order_filled_count |   unresolved_order_count |   rejected_order_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   profit_loss_ratio |   max_drawdown |   max_consecutive_losses |   avg_buy_amount_ratio |   max_buy_amount_ratio |   avg_sell_amount_ratio |   max_sell_amount_ratio |   avg_buy_slippage |   avg_sell_slippage |   risk_warn_count |   risk_fail_count | live_order_enabled   |
|:-------------------------------------------------|:-------------|:------------------|:-------------------------------------------------------------------------------------------|---------------:|---------------:|------------------:|---------------:|--------------:|-------------:|-----------------------:|-------------------------:|--------------------------:|-------------------------:|-----------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|--------------------:|---------------:|-------------------------:|-----------------------:|-----------------------:|------------------------:|------------------------:|-------------------:|--------------------:|------------------:|------------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        | historical_replay | /Users/user/Desktop/A_System/reports/a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv |         500000 |    2.25033e+07 |           45.0067 |             59 |           118 |          118 |                     59 |                       59 |                        59 |                        0 |                      0 |   0.728814 |            0.0734519 |               0.0454512 |     0.498054 |   -0.10726 |             2.81342 |      -0.110076 |                        2 |             0.00556046 |              0.0210051 |              0.00668585 |               0.0315139 |         0.00215254 |          0.00252542 |                 7 |                 0 | False                |

## 风险事件预览

|   event_date | paper_trade_id   | ts_code   | risk_level   | risk_type              |   metric_value |   threshold | message       |
|-------------:|:-----------------|:----------|:-------------|:-----------------------|---------------:|------------:|:--------------|
|     20241231 | PT00013          | 300599.SZ | WARN         | SINGLE_TRADE_LOSS_WARN |     -0.082543  |      -0.08  | 单笔账户亏损达到预警阈值。 |
|     20251030 | PT00039          | 920748.BJ | WARN         | SELL_SLIPPAGE_WARN     |      0.01      |       0.005 | 卖出滑点偏高。       |
|     20251124 | PT00044          | 301092.SZ | WARN         | BUY_SLIPPAGE_WARN      |      0.01      |       0.005 | 买入滑点偏高。       |
|     20251124 | PT00044          | 301092.SZ | WARN         | SELL_AMOUNT_RATIO_WARN |      0.0315139 |       0.03  | 卖出成交额占比偏高。    |
|     20251124 | PT00044          | 301092.SZ | WARN         | SELL_SLIPPAGE_WARN     |      0.01      |       0.005 | 卖出滑点偏高。       |
|     20251223 | PT00048          | 300557.SZ | WARN         | SINGLE_TRADE_LOSS_WARN |     -0.10726   |      -0.08  | 单笔账户亏损达到预警阈值。 |
|     20260421 | PT00059          | 301319.SZ | WARN         | SELL_SLIPPAGE_WARN     |      0.01      |       0.005 | 卖出滑点偏高。       |

## 资金曲线尾部

|     date | event        | paper_trade_id   | ts_code   |      equity |        cash |   account_return |     drawdown |   peak_equity |
|---------:|:-------------|:-----------------|:----------|------------:|------------:|-----------------:|-------------:|--------------:|
| 20260119 | TRADE_CLOSED | PT00050          | 301629.SZ | 1.67859e+07 | 1.67859e+07 |       -0.0338314 | -0.0338314   |   1.73737e+07 |
| 20260121 | TRADE_CLOSED | PT00051          | 300658.SZ | 1.79294e+07 | 1.79294e+07 |        0.0681174 |  0           |   1.79294e+07 |
| 20260127 | TRADE_CLOSED | PT00052          | 920368.BJ | 1.9772e+07  | 1.9772e+07  |        0.102773  |  0           |   1.9772e+07  |
| 20260130 | TRADE_CLOSED | PT00053          | 300164.SZ | 2.0677e+07  | 2.0677e+07  |        0.0457726 |  0           |   2.0677e+07  |
| 20260302 | TRADE_CLOSED | PT00054          | 300499.SZ | 2.10781e+07 | 2.10781e+07 |        0.0193949 |  0           |   2.10781e+07 |
| 20260311 | TRADE_CLOSED | PT00055          | 301606.SZ | 2.00095e+07 | 2.00095e+07 |       -0.0506941 | -0.0506941   |   2.10781e+07 |
| 20260331 | TRADE_CLOSED | PT00056          | 300204.SZ | 2.08051e+07 | 2.08051e+07 |        0.0397618 | -0.012948    |   2.10781e+07 |
| 20260413 | TRADE_CLOSED | PT00057          | 300489.SZ | 2.2525e+07  | 2.2525e+07  |        0.0826627 |  0           |   2.2525e+07  |
| 20260416 | TRADE_CLOSED | PT00058          | 301189.SZ | 2.17365e+07 | 2.17365e+07 |       -0.0350052 | -0.0350052   |   2.2525e+07  |
| 20260421 | TRADE_CLOSED | PT00059          | 301319.SZ | 2.25033e+07 | 2.25033e+07 |        0.0352808 | -0.000959421 |   2.2525e+07  |

## 结论限制

该模拟盘账本用于验证交易流程、资金记账、委托成交状态和风控事件输出。它仍基于日线审计成交结果，尚未使用分钟 K、集合竞价、盘口五档和真实排队数据，不能直接作为实盘依据。
