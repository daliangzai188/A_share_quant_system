# 2026-09-22 内置桥接失联：修复与验收边界

本修复在同步目录外开发并完成验证；用户于2026-09-22明确授权“提交”。
本次仅同步源码及配套认证文件，不重启客户端或daemon；运行中的daemon仍使用已加载代码，新逻辑在下次正常启动时加载。

## 已确认现场

- daemon日志最后成功验证时间为2026-09-21 20:51:50，首次心跳过期为20:52:51。
- 只读进程查询显示客户端当前进程启动于20:52:24；现场停在带验证码的登录页。
- 09:00:10 keeper发现daemon退出并拉起；新daemon继续因内置心跳过期阻断。
- 这些证据不能证明客户端由谁启动，也不能区分崩溃、更新、人工操作或其他触发来源。
- 今日正式组合报告无计划单，方案甲停手；终端旧“最终结果”未使用同一停手门禁，显示了候选名义股数。

## 修改及位置

文件 `scripts/trading_daemon.py`：

1. 在 `_is_qmt_inner_file_transport_busy` 后新增心跳过期识别及故障记录方法。
   原始和退避包装错误均识别；记录账户不可用，使用现有限频通知机制每30分钟最多通知一次，夜间也提示。
   不把心跳失效直接等同为客户端退出；不运行客户端启动、退出或重启命令。
2. 在 `_request_qmt_reconnect_stalled_recovery` 首部新增内置心跳失效排除条件。
   删除该错误复用旧miniQMT进程回收路径的行为；旧miniQMT资源回收保护保留。
3. 在 `_print_account_status` 的首次异常及退避分支识别该故障，停止本轮账户派生动作。
   首次异常仅清理外部适配器引用，供模型恢复后重新握手，不操作客户端。
4. 在 `check_qmt_connection` 接入明确故障提示；在 `wait_for_qmt_startup_gate` 避免与通用阻断通知重复。
5. 在 `_log_final_decision_summary` 组装候选前调用既有 `_equity_curve_stop_block_reason`。
   停手或门禁读取失败时，直接显示不开新仓，不预演账户定仓、不显示准备下单时间；退出规则不变。
6. 在 `_log_decision_chain_summary` 中把停手检查提前到窗口过期判断前；
   `_notify_missed_open_window_if_needed` 重新核验当日停手，避免恢复后把规则要求的空仓误报成漏单。

新增 `tests/test_inner_bridge_outage.py`：验证内置故障不触发进程退出、旧miniQMT保护仍有效、夜间告警、退避与启动阻断，以及停手和门禁读失败时不播报下单。
`tests/test_current_portfolio_runtime.py` 的 `test_candidate_broadcast_marks_failed_buy_gate_as_non_executable`
显式模拟“停手门禁允许”，让该用例只验证原来的发布认证失败，避免依赖生产当日判定文件。

## 运行与验证

所有命令仅在此隔离副本运行：

```sh
A_SYSTEM_DISABLE_NOTIFICATIONS=1 python3 -m unittest tests.test_inner_bridge_outage tests.test_broker_maintenance_recovery tests.test_current_portfolio_runtime -q
A_SYSTEM_DISABLE_NOTIFICATIONS=1 python3 -m unittest discover -s tests -q
A_SYSTEM_DISABLE_NOTIFICATIONS=1 python3 scripts/certify_plan_jia_release.py
```

认证研究输入为独立复制的文件，不与生产账本建立链接。测试关闭真实通知。

正式运行仍使用原启动入口 `py -3.11 start_windows.py`，不增加客户端重启入口。
本次授权只同步并提交，不执行该启动命令。同步后重新核验认证哈希；运行中的daemon不会因为源码同步自动加载新逻辑。

## 未完成的现场工作

客户端退出/启动的触发来源尚未查明，需只读核对Windows应用错误、更新、计划任务和客户端日志。
生产日志于10:39:20显示程序与账户恢复；至最新同步10:46:22持续账户查询成功，健康文件为verified，PID=6520。
此恢复发生在原生产代码下，与本次源码修复无关。尚未执行客户端退出/冷启动验收，不能据此保证故障不会再发生。
真实交易验证仍应先用小资金。

## 最终验证结果

- 相关77项检查通过（`targeted-final.log`）。
- 全量635项：625项通过、10项跳过、0失败、0错误（`full-tests-final.log`）。
- 正式认证PASS：`reports/current_portfolio_alignment/return_first_live_certification.json`；
  指标与锁定值一致，两次确定性回放一致，实盘逐日停手判定与认证一致。
- 语法检查、差异空白检查通过。开发验证阶段未改生产文件；仅在本次用户授权后同步并提交指定修复文件。
- 隔离环境第一轮缺少研究输入导致测试/认证失败；补齐独立数据副本后上述最终命令重跑通过。
