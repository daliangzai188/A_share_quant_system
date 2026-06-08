# QMT / miniQMT 实盘接入准备度检查

本报告不连接券商、不提交委托、不打印真实账号。

## 汇总

| 项目 | 数量 |
|---|---:|
| FAIL | 6 |
| WARN | 3 |
| 阻断项 | 6 |

## 明细

| item                   | status   | blocking   | detail                                                                                                                                       |
|:-----------------------|:---------|:-----------|:---------------------------------------------------------------------------------------------------------------------------------------------|
| trade_mode             | PASS     | False      | 当前 trade_mode=paper; 未正式测试前建议保持 paper。                                                                                                       |
| broker_adapter_enabled | WARN     | False      | broker_adapter_enabled=False; 审批通过后只读检查需要改为 true。                                                                                            |
| qmt_enabled            | WARN     | False      | qmt_enabled=False; 审批通过后只读检查需要改为 true。                                                                                                       |
| broker.enabled         | WARN     | False      | broker.enabled=False; 审批通过后只读检查需要改为 true。                                                                                                    |
| real_order_gate        | PASS     | False      | live_trade.enabled=False, real_order_enabled=False; 未完成 100 股测试前必须保持 false。                                                                  |
| QMT_ACCOUNT_ID         | FAIL     | True       | QMT_ACCOUNT_ID 未配置或仍是占位值。                                                                                                                    |
| QMT_ACCOUNT_TYPE       | FAIL     | True       | QMT_ACCOUNT_TYPE 未配置或仍是占位值。                                                                                                                  |
| QMT_PATH               | FAIL     | True       | QMT_PATH 未配置或仍是占位值。                                                                                                                          |
| QMT_SESSION_ID         | FAIL     | True       | QMT_SESSION_ID 未配置或仍是占位值。                                                                                                                    |
| QMT_PATH_EXISTS        | FAIL     | True       | QMT_PATH 不存在或未配置。                                                                                                                            |
| xtquant_import         | FAIL     | True       | 当前 Python 环境不能导入 xtquant。                                                                                                                    |
| latest_planned_orders  | PASS     | False      | /Users/user/Desktop/A_System/reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_b0018_filtered_plus_c_hold3_20260514_planned_orders.csv |
| latest_live_preview    | PASS     | False      | /Users/user/Desktop/A_System/reports/live_trade/mock_qmt_live_order_preview.csv                                                              |

## 结论

- 阻断项为 0 前，不进入真实下单。
- 第一次通过后也只做只读检查和实盘预览。
- 真实下单前先做 100 股小资金测试。
