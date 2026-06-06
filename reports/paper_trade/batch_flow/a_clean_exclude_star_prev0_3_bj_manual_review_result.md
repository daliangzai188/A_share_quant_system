# 人工确认决策结果

本报告只处理本地人工确认 CSV，不接实盘，不调用 QMT，不下真实订单。

## 汇总

|   manual_review_count |   approved_count |   rejected_count |   pending_count |   suggested_approved_count |   suggested_rejected_count |   suggested_pending_count |   paper_observation_allowed_count | template_created   | decisions_path                                                                                                                            | live_order_enabled   |
|----------------------:|-----------------:|-----------------:|----------------:|---------------------------:|---------------------------:|--------------------------:|----------------------------------:|:-------------------|:------------------------------------------------------------------------------------------------------------------------------------------|:---------------------|
|                     5 |                0 |                2 |               3 |                          0 |                          2 |                         3 |                                 0 | False              | /Users/user/Desktop/A_System/reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_manual_review_decisions.csv | False                |

## 明细

|   signal_date |   planned_order_date | ts_code   | name   | review_decision   | suggested_decision   | suggestion_confidence   | suggestion_reason                                  | final_review_status              | paper_observation_allowed   | risk_flags         |   planned_amount_by_equity | review_note                                    |
|--------------:|---------------------:|:----------|:-------|:------------------|:---------------------|:------------------------|:---------------------------------------------------|:---------------------------------|:----------------------------|:-------------------|---------------------------:|:-----------------------------------------------|
|      20241227 |             20241230 | 300599.SZ | 雄塑科技   | REJECTED          | REJECTED             | HIGH                    | 命中 LOSS_OVERLAY_WATCH，且历史参考亏损不高于 -8%，建议跳过模拟观察。     | MANUAL_REJECTED_SKIP_OBSERVATION | False                       | LOSS_OVERLAY_WATCH |                1.32397e+06 | 命中 LOSS_OVERLAY_WATCH，且历史参考亏损不高于 -8%，建议跳过模拟观察。 |
|      20251028 |             20251029 | 920748.BJ | 路桥信息   | PENDING           | PENDING              | MEDIUM                  | 命中 LOSS_OVERLAY_WATCH，历史参考收益为负，建议保留人工复核。           | PENDING_MANUAL_REVIEW            | False                       | LOSS_OVERLAY_WATCH |                8.93427e+06 |                                                |
|      20251125 |             20251126 | 301117.SZ | 佳缘科技   | PENDING           | PENDING              | MEDIUM                  | 命中 LOSS_OVERLAY_WATCH，历史参考亏损在 -5% 到 -8% 区间，建议重点复核。 | PENDING_MANUAL_REVIEW            | False                       | LOSS_OVERLAY_WATCH |                1.2038e+07  |                                                |
|      20251219 |             20251222 | 300557.SZ | 理工光科   | REJECTED          | REJECTED             | HIGH                    | 命中 LOSS_OVERLAY_WATCH，且历史参考亏损不高于 -8%，建议跳过模拟观察。     | MANUAL_REJECTED_SKIP_OBSERVATION | False                       | LOSS_OVERLAY_WATCH |                1.37674e+07 | 命中 LOSS_OVERLAY_WATCH，且历史参考亏损不高于 -8%，建议跳过模拟观察。 |
|      20260309 |             20260310 | 301606.SZ | 绿联科技   | PENDING           | PENDING              | MEDIUM                  | 命中 LOSS_OVERLAY_WATCH，历史参考亏损在 -5% 到 -8% 区间，建议重点复核。 | PENDING_MANUAL_REVIEW            | False                       | LOSS_OVERLAY_WATCH |                1.68624e+07 |                                                |

## 填写说明

- `APPROVED`：允许进入模拟买入观察，不代表可以实盘。
- `REJECTED`：跳过该模拟观察。
- `PENDING`：未确认，不能进入实盘或半自动流程。
- `suggested_decision` 只是基于历史亏损叠加标签的保守建议，不会自动覆盖 `review_decision`。
