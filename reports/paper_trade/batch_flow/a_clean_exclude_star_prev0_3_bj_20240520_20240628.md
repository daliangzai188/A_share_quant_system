# 多日模拟盘批量流程报告

本报告按日期区间批量串联本地候选生成、计划委托、历史模拟成交、持仓和资金更新。不接实盘，不下真实订单。

## 汇总

| strategy_name                                    | trade_mode   |   start_date |   end_date |   trade_day_count |   closed_trade_day_count |   no_candidate_day_count |   pending_day_count |   initial_equity |   final_equity |   equity_multiple |   win_rate |   closed_trade_win_rate |   positive_day_rate |   avg_account_return |   median_account_return |   avg_daily_account_return |   median_daily_account_return |   max_profit |   max_loss |   max_drawdown |   risk_event_count | live_order_enabled   |
|:-------------------------------------------------|:-------------|-------------:|-----------:|------------------:|-------------------------:|-------------------------:|--------------------:|-----------------:|---------------:|------------------:|-----------:|------------------------:|--------------------:|---------------------:|------------------------:|---------------------------:|------------------------------:|-------------:|-----------:|---------------:|-------------------:|:---------------------|
| a_clean_plus_exclude_star_prev0_3_bj_risk_filter | paper        |     20240520 |   20240628 |                29 |                        2 |                       27 |                   0 |           500000 |         741862 |           1.48372 |          1 |                       1 |           0.0689655 |              0.23027 |                 0.23027 |                  0.0158807 |                             0 |     0.403016 |  0.0575252 |              0 |                  0 | False                |

## 每日状态预览

|   signal_date | daily_status             |   candidate_count |   selected_count | top_ts_code   | top_name   |   account_return |   equity_end_of_day |
|--------------:|:-------------------------|------------------:|-----------------:|:--------------|:-----------|-----------------:|--------------------:|
|      20240520 | CLOSED_BY_HISTORICAL_SIM |                 1 |                1 | 300162.SZ     | 雷曼光电       |        0.403016  |              701508 |
|      20240521 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240522 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240523 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240524 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240527 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240528 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240529 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240530 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240531 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240603 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240604 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240605 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240606 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240607 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240611 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240612 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240613 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240614 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240617 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240618 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240619 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              701508 |
|      20240620 | CLOSED_BY_HISTORICAL_SIM |                 1 |                1 | 300469.SZ     | 信息发展       |        0.0575252 |              741862 |
|      20240621 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |
|      20240624 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |
|      20240625 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |
|      20240626 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |
|      20240627 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |
|      20240628 | NO_CANDIDATE             |                 0 |                0 |               |            |        0         |              741862 |

## 资金曲线尾部

|     date | event                    |   signal_date | ts_code   | name   |   equity |   account_return | daily_status             |   peak_equity |   drawdown |
|---------:|:-------------------------|--------------:|:----------|:-------|---------:|-----------------:|:-------------------------|--------------:|-----------:|
| 20240531 | NO_CANDIDATE             |      20240531 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240603 | NO_CANDIDATE             |      20240603 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240604 | NO_CANDIDATE             |      20240604 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240605 | NO_CANDIDATE             |      20240605 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240606 | NO_CANDIDATE             |      20240606 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240607 | NO_CANDIDATE             |      20240607 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240611 | NO_CANDIDATE             |      20240611 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240612 | NO_CANDIDATE             |      20240612 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240613 | NO_CANDIDATE             |      20240613 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240614 | NO_CANDIDATE             |      20240614 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240617 | NO_CANDIDATE             |      20240617 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240618 | NO_CANDIDATE             |      20240618 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240619 | NO_CANDIDATE             |      20240619 |           |        |   701508 |        0         | NO_CANDIDATE             |        701508 |          0 |
| 20240620 | CLOSED_BY_HISTORICAL_SIM |      20240620 | 300469.SZ | 信息发展   |   741862 |        0.0575252 | CLOSED_BY_HISTORICAL_SIM |        741862 |          0 |
| 20240621 | NO_CANDIDATE             |      20240621 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |
| 20240624 | NO_CANDIDATE             |      20240624 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |
| 20240625 | NO_CANDIDATE             |      20240625 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |
| 20240626 | NO_CANDIDATE             |      20240626 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |
| 20240627 | NO_CANDIDATE             |      20240627 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |
| 20240628 | NO_CANDIDATE             |      20240628 |           |        |   741862 |        0         | NO_CANDIDATE             |        741862 |          0 |

## 风险事件

无风险事件。

## 口径限制

该批量流程仍使用本地历史审计成交作为模拟成交依据。没有历史匹配的计划不会被记为成交；真实模拟盘还需要接入分钟 K、集合竞价、盘口五档和人工确认。
