# QMT / miniQMT 实盘接入准备度检查

本报告不连接券商、不提交委托、不打印真实账号。

## 汇总

| 项目 | 数量 |
|---|---:|
| FAIL | 0 |
| WARN | 0 |
| 阻断项 | 0 |

## 明细

| item                   | status   | blocking   | detail                                                                                                                                      |
|:-----------------------|:---------|:-----------|:--------------------------------------------------------------------------------------------------------------------------------------------|
| trade_mode             | PASS     | False      | 当前 trade_mode=paper; 未正式测试前建议保持 paper。                                                                                                      |
| broker_adapter_enabled | PASS     | False      | broker_adapter_enabled=True; 审批通过后只读检查需要改为 true。                                                                                            |
| qmt_enabled            | PASS     | False      | qmt_enabled=True; 审批通过后只读检查需要改为 true。                                                                                                       |
| broker.enabled         | PASS     | False      | broker.enabled=True; 审批通过后只读检查需要改为 true。                                                                                                    |
| real_order_gate        | PASS     | False      | live_trade.enabled=False, real_order_enabled=False; 未完成 100 股测试前必须保持 false。                                                                 |
| QMT_ACCOUNT_ID         | PASS     | False      | QMT_ACCOUNT_ID 已配置，未打印真实值。                                                                                                                  |
| QMT_ACCOUNT_TYPE       | PASS     | False      | QMT_ACCOUNT_TYPE 已配置，未打印真实值。                                                                                                                |
| QMT_PATH               | PASS     | False      | QMT_PATH 已配置，未打印真实值。                                                                                                                        |
| QMT_SESSION_ID         | PASS     | False      | QMT_SESSION_ID 已配置，未打印真实值。                                                                                                                  |
| QMT_PATH_EXISTS        | PASS     | True       | QMT_PATH 路径存在。                                                                                                                              |
| xtquant_import         | PASS     | True       | xtquant 核心模块可完整导入；python=3.11.9, arch=AMD64。                                                                                                |
| latest_planned_orders  | PASS     | False      | \\localhost@9843\DavWWWRoot\reports\paper_trade\ab_filtered_daily_ops\a_strict_plus_b0018_filtered_plus_c_hold3_20260611_planned_orders.csv |
| latest_live_preview    | PASS     | False      | \\localhost@9843\DavWWWRoot\reports\live_trade\mock_qmt_live_order_preview.csv                                                              |

## 结论

- 阻断项为 0 前，不进入真实下单。
- 第一次通过后也只做只读检查和实盘预览。
- 真实下单前先做 100 股小资金测试。
