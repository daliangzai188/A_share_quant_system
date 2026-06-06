# 每日模拟盘候选报告

本报告只基于本地 T 日已知因子生成候选，不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   signal_date |   filtered_candidate_count_all_dates |   matched_candidate_count_on_signal_date |   output_candidate_count |   selected_count |   watch_count | top_ts_code   | top_name   |   top_profit_source_score | top_risk_flags     |   risk_warn_candidate_count |   loss_overlay_watch_candidate_count |   selected_loss_overlay_watch_count | selected_loss_overlay_watch   | loss_overlay_watch_top_codes   | manual_review_required   | manual_review_status   | manual_review_reason                       | future_columns_used_for_ranking   | live_order_enabled   |
|:-------------------------------------------------|:-------------|--------------:|-------------------------------------:|-----------------------------------------:|-------------------------:|-----------------:|--------------:|:--------------|:-----------|--------------------------:|:-------------------|----------------------------:|-------------------------------------:|------------------------------------:|:------------------------------|:-------------------------------|:-------------------------|:-----------------------|:-------------------------------------------|:----------------------------------|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |      20251219 |                                  189 |                                        2 |                        2 |                1 |             1 | 300557.SZ     | 理工光科       |                       1.8 | LOSS_OVERLAY_WATCH |                           1 |                                    1 |                                   1 | True                          | 300557.SZ 理工光科                 | True                     | PENDING_MANUAL_REVIEW  | 选中标的命中 LOSS_OVERLAY_WATCH，进入模拟买入观察前需要人工复核。 | False                             | False                |

## 候选列表

|   candidate_rank | planned_action   | ts_code   | name   | market_segment   |   profit_source_score | risk_flags         |   fill_probability | fd_ratio_bucket   |   market_chain_count_bucket | segment_limit_up_count_bucket   |   historical_reference_next_trade_date |   historical_reference_net_return |
|-----------------:|:-----------------|:----------|:-------|:-----------------|----------------------:|:-------------------|-------------------:|:------------------|----------------------------:|:--------------------------------|---------------------------------------:|----------------------------------:|
|                1 | PLAN_BUY_T1_OPEN | 300557.SZ | 理工光科   | chi_next         |                   1.8 | LOSS_OVERLAY_WATCH |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20251222 |                        -0.127356  |
|                2 | WATCH_ONLY       | 300947.SZ | 德必集团   | chi_next         |                  -0.5 | 无                  |                  1 | 0_5pct_1pct       |                        8_15 | lt_5                            |                               20251222 |                        -0.0493238 |

## 口径说明

- `planned_action=PLAN_BUY_T1_OPEN` 表示计划在 T+1 开盘用模拟盘观察买入。
- `historical_reference_*` 字段只用于历史复盘，不参与候选排序。
- 当前仍未验证集合竞价、盘口五档、分钟 K 和真实排队成交，不能直接用于实盘。
