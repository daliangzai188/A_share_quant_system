# A+B filtered 每日模拟盘操作台

本报告只用于本地模拟盘流程，不接实盘，不调用 QMT，不下真实订单。

## 今日操作清单

|   signal_date | strategy_leg   | operation_status   | next_action              |   a_candidate_count |   b_candidate_count |   b_rejected_by_filter_count |   selected_count |   planned_order_count | manual_review_required   |   manual_review_count | top_ts_code   | top_name   |   account_return | return_source   | live_order_enabled   | selection_status              |
|--------------:|:---------------|:-------------------|:-------------------------|--------------------:|--------------------:|-----------------------------:|-----------------:|----------------------:|:-------------------------|----------------------:|:--------------|:-----------|-----------------:|:----------------|:---------------------|:------------------------------|
|      20260413 | NONE           | NO_SELECTED        | A无候选且B无可用候选，今日不生成模拟买入计划。 |                   0 |                  10 |                            1 |                0 |                     0 | False                    |                     0 |               |            |                0 |                 | False                | A_NO_SELECTED_B_RISK_FILTERED |

## 选中标的

今日无选中标的。

## 输出文件

| name                | path                                                                                                                                  |
|:--------------------|:--------------------------------------------------------------------------------------------------------------------------------------|
| a_candidates        | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_a_candidates.csv         |
| b_candidates        | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_b_candidates.csv         |
| b_rejected          | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_b_rejected_by_filter.csv |
| selected            | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_selected.csv             |
| planned_orders      | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_planned_orders.csv       |
| manual_review       | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_manual_review.csv        |
| execution_reference | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_execution_reference.csv  |
| checklist           | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413_checklist.csv            |
| markdown            | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_20260413.md                       |

## 执行限制

- A 优先；只有 A 无选中标的时才启用 B。
- B 命中 `risk_reject_rules` 时直接跳过，不寻找下一只替代。
- `live_order_enabled` 必须为 `False`。
- 当前仍未完成分钟 K、盘口五档、集合竞价和连续模拟盘验证，不允许实盘。
