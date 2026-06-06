# 每日模拟盘候选报告

本报告只基于本地 T 日已知因子生成候选，不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   signal_date |   filtered_candidate_count_all_dates |   matched_candidate_count_on_signal_date |   output_candidate_count |   selected_count |   watch_count | top_ts_code   | top_name   |   top_profit_source_score | top_risk_flags   |   risk_warn_candidate_count |   loss_overlay_watch_candidate_count |   selected_loss_overlay_watch_count | selected_loss_overlay_watch   | loss_overlay_watch_top_codes   | manual_review_required   | manual_review_status   | manual_review_reason   | future_columns_used_for_ranking   | live_order_enabled   |
|:-------------------------------------------------|:-------------|--------------:|-------------------------------------:|-----------------------------------------:|-------------------------:|-----------------:|--------------:|:--------------|:-----------|--------------------------:|:-----------------|----------------------------:|-------------------------------------:|------------------------------------:|:------------------------------|:-------------------------------|:-------------------------|:-----------------------|:-----------------------|:----------------------------------|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |      20260417 |                                  189 |                                        4 |                        4 |                1 |             3 | 301319.SZ     | 唯特偶        |                       3.8 | 无                |                           0 |                                    0 |                                   0 | False                         |                                | False                    | NOT_REQUIRED           |                        | False                             | False                |

## 候选列表

|   candidate_rank | planned_action   | ts_code   | name   | market_segment   |   profit_source_score | risk_flags   |   fill_probability | fd_ratio_bucket   |   market_chain_count_bucket | segment_limit_up_count_bucket   |   historical_reference_next_trade_date |   historical_reference_net_return |
|-----------------:|:-----------------|:----------|:-------|:-----------------|----------------------:|:-------------|-------------------:|:------------------|----------------------------:|:--------------------------------|---------------------------------------:|----------------------------------:|
|                1 | PLAN_BUY_T1_OPEN | 301319.SZ | 唯特偶    | chi_next         |                   3.8 | 无            |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20260420 |                         0.0579453 |
|                2 | WATCH_ONLY       | 300776.SZ | 帝尔激光   | chi_next         |                   3   | 无            |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20260420 |                        -0.0561299 |
|                3 | WATCH_ONLY       | 300905.SZ | 宝丽迪    | chi_next         |                   3   | 无            |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20260420 |                        -0.133917  |
|                4 | WATCH_ONLY       | 301237.SZ | 和顺科技   | chi_next         |                   1.3 | 无            |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20260420 |                        -0.0153313 |

## 口径说明

- `planned_action=PLAN_BUY_T1_OPEN` 表示计划在 T+1 开盘用模拟盘观察买入。
- `historical_reference_*` 字段只用于历史复盘，不参与候选排序。
- 当前仍未验证集合竞价、盘口五档、分钟 K 和真实排队成交，不能直接用于实盘。
