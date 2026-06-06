# 单日模拟盘流程报告

本报告只串联本地候选生成和本地历史模拟成交更新，不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   signal_date |   candidate_count |   selected_count |   planned_order_count |   execution_event_count |   closed_position_count |   pending_position_count |   manual_review_blocked_execution_count | top_ts_code   | top_name   | top_risk_flags     |   risk_warn_candidate_count |   loss_overlay_watch_candidate_count |   selected_loss_overlay_watch_count | selected_loss_overlay_watch   | loss_overlay_watch_top_codes   | manual_review_required   | manual_review_status   | manual_review_reason                       | historical_execution_found   |   equity_before |   equity_after |   account_return | live_order_enabled   |
|:-------------------------------------------------|:-------------|--------------:|------------------:|-----------------:|----------------------:|------------------------:|------------------------:|-------------------------:|----------------------------------------:|:--------------|:-----------|:-------------------|----------------------------:|-------------------------------------:|------------------------------------:|:------------------------------|:-------------------------------|:-------------------------|:-----------------------|:-------------------------------------------|:-----------------------------|----------------:|---------------:|-----------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |      20251219 |                 2 |                1 |                     1 |                       1 |                       0 |                        1 |                                       1 | 300557.SZ     | 理工光科       | LOSS_OVERLAY_WATCH |                           1 |                                    1 |                                   1 | True                          | 300557.SZ 理工光科                 | True                     | PENDING_MANUAL_REVIEW  | 选中标的命中 LOSS_OVERLAY_WATCH，进入模拟买入观察前需要人工复核。 | False                        |               0 |              0 |                0 | False                |

## 候选

|   candidate_rank | planned_action   | ts_code   | name   |   profit_source_score | risk_flags         |   historical_reference_next_trade_date |   historical_reference_net_return |
|-----------------:|:-----------------|:----------|:-------|----------------------:|:-------------------|---------------------------------------:|----------------------------------:|
|                1 | PLAN_BUY_T1_OPEN | 300557.SZ | 理工光科   |                   1.8 | LOSS_OVERLAY_WATCH |                               20251222 |                        -0.127356  |
|                2 | WATCH_ONLY       | 300947.SZ | 德必集团   |                  -0.5 | 无                  |                               20251222 |                        -0.0493238 |

## 计划委托

| paper_order_id            |   signal_date |   planned_order_date | side   | ts_code   | name   | planned_action   | order_status              |   planned_position_pct |   planned_equity |   planned_amount_by_equity |   reference_price |   estimated_shares |   round_lot_shares | risk_flags         | manual_review_required   | manual_review_status   | manual_review_reason               | live_order_enabled   |
|:--------------------------|--------------:|---------------------:|:-------|:----------|:-------|:-----------------|:--------------------------|-----------------------:|-----------------:|---------------------------:|------------------:|-------------------:|-------------------:|:-------------------|:-------------------------|:-----------------------|:-----------------------------------|:---------------------|
| PLAN-20251219-300557.SZ-B |      20251219 |             20251222 | BUY    | 300557.SZ | 理工光科   | PLAN_BUY_T1_OPEN | REVIEW_REQUIRED_PLAN_ONLY |                    0.8 |      1.72092e+07 |                1.37674e+07 |             41.54 |             331425 |             331400 | LOSS_OVERLAY_WATCH | True                     | PENDING_MANUAL_REVIEW  | 命中 LOSS_OVERLAY_WATCH，模拟买入前需要人工复核。 | False                |

## 人工确认清单

|   signal_date |   planned_order_date | ts_code   | name   | manual_review_status   | manual_review_reason               | risk_flags         |   planned_position_pct |   planned_amount_by_equity |   reference_price |   amount_ratio_bucket |   open_times |   first_time_detail_bucket |   turnover_rate_bucket |   historical_reference_net_return | review_instruction                 | live_order_enabled   |
|--------------:|---------------------:|:----------|:-------|:-----------------------|:-----------------------------------|:-------------------|-----------------------:|---------------------------:|------------------:|----------------------:|-------------:|---------------------------:|-----------------------:|----------------------------------:|:-----------------------------------|:---------------------|
|      20251219 |             20251222 | 300557.SZ | 理工光科   | PENDING_MANUAL_REVIEW  | 命中 LOSS_OVERLAY_WATCH，模拟买入前需要人工复核。 | LOSS_OVERLAY_WATCH |                    0.8 |                1.37674e+07 |             41.54 |                   2_3 |            2 |                  1100_1330 |                  15_25 |                         -0.127356 | 人工确认后才允许进入模拟买入观察；未确认时不得进入实盘或半自动流程。 | False                |

## 成交更新

| paper_execution_id                     |   signal_date | event_date   | side   | ts_code   | name   | execution_status               | price   | amount   |   account_return | equity_before   | equity_after   | message                                           |
|:---------------------------------------|--------------:|:-------------|:-------|:----------|:-------|:-------------------------------|:--------|:---------|-----------------:|:----------------|:---------------|:--------------------------------------------------|
| EXEC-20251219-300557.SZ-REVIEW-BLOCKED |      20251219 |              | BUY    | 300557.SZ | 理工光科   | MANUAL_REVIEW_REQUIRED_BLOCKED | <NA>    | <NA>     |                0 | <NA>            | <NA>           | 命中 LOSS_OVERLAY_WATCH，未人工确认前只保留计划和复核清单，不自动记为模拟成交。 |

## 持仓更新

|   signal_date | ts_code   | name   | position_status    | open_date   | close_date   |   account_return | equity_after   | message        |
|--------------:|:----------|:-------|:-------------------|:------------|:-------------|-----------------:|:---------------|:---------------|
|      20251219 | 300557.SZ | 理工光科   | PLANNED_OR_PENDING |             |              |                0 | <NA>           | 未形成完整历史模拟成交闭环。 |

## 资金更新

|   signal_date | equity_event    | event_date   | equity_before   | equity_after   |   account_return | message         |
|--------------:|:----------------|:-------------|:----------------|:---------------|-----------------:|:----------------|
|      20251219 | NO_CLOSED_TRADE |              | <NA>            | <NA>           |                0 | 没有完整卖出成交，资金不更新。 |

## 口径限制

如果 `historical_execution_found=false`，表示该日只生成计划，不记录成交。真实模拟盘仍需后续接入分钟 K、集合竞价、盘口五档和人工确认流程后再推进。
