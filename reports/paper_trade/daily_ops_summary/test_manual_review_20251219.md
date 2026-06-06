# 模拟盘每日操作台汇总

本报告只汇总本地模拟盘观察结果，不接实盘，不调用 QMT，不下真实订单。

## 汇总

|   start_date |   end_date |   observation_day_count |   min_observation_days | observation_requirement_met   |   historical_sim_filled_count |   review_required_count |   plan_only_pending_count |   failed_or_no_candidate_count |   manual_review_required_day_count |   manual_review_approved_day_count |   manual_review_rejected_day_count |   manual_review_pending_day_count |   paper_observation_allowed_day_count |   manual_review_total_count |   planned_order_total_count |   win_rate |   avg_account_return |   median_account_return |   max_profit |   max_loss |   initial_equity |   final_equity |   equity_multiple |   max_drawdown | live_order_enabled   | verification_status        |
|-------------:|-----------:|------------------------:|-----------------------:|:------------------------------|------------------------------:|------------------------:|--------------------------:|-------------------------------:|-----------------------------------:|-----------------------------------:|-----------------------------------:|----------------------------------:|--------------------------------------:|----------------------------:|----------------------------:|-----------:|---------------------:|------------------------:|-------------:|-----------:|-----------------:|---------------:|------------------:|---------------:|:---------------------|:---------------------------|
|     20251219 |   20251219 |                       1 |                      1 | True                          |                             0 |                       1 |                         0 |                              0 |                                  1 |                                  0 |                                  0 |                                 1 |                                     0 |                           1 |                           1 |          0 |                    0 |                       0 |            0 |          0 |           500000 |         500000 |                 1 |              0 | False                | PASS_OBSERVATION_DAYS_ONLY |

## 状态分布

| operation_status          |   day_count |
|:--------------------------|------------:|
| REVIEW_REQUIRED_PLAN_ONLY |           1 |

## 每日明细

|   signal_date | operation_status          | top_ts_code   | top_name   | manual_review_required   | review_decision_status   | paper_observation_allowed   |   planned_order_count |   account_return | risk_flags         |
|--------------:|:--------------------------|:--------------|:-----------|:-------------------------|:-------------------------|:----------------------------|----------------------:|-----------------:|:-------------------|
|      20251219 | REVIEW_REQUIRED_PLAN_ONLY | 300557.SZ     | 理工光科       | True                     | PENDING                  | False                       |                     1 |                0 | LOSS_OVERLAY_WATCH |

## 资金曲线

|   signal_date | operation_status          | ts_code   | name   |   account_return |   equity |   peak_equity |   drawdown |
|--------------:|:--------------------------|:----------|:-------|-----------------:|---------:|--------------:|-----------:|
|      20251219 | REVIEW_REQUIRED_PLAN_ONLY | 300557.SZ | 理工光科   |                0 |   500000 |        500000 |          0 |

## 判断口径

- 未达到 `min_observation_days` 时，只能继续观察，不能实盘。
- `REVIEW_REQUIRED_PLAN_ONLY` 只能人工复核，不能自动买入。
- 本报告不包含分钟 K、盘口五档和真实排队成交验证。
