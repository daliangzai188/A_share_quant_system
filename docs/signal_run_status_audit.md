# E2/L每日信号运行状态审计

## 目标

正式信号文件只保存真正入选的股票。过去审计仅检查正式信号文件是否含当天日期，
因此无法区分以下两种情况：

1. 脚本已经正常执行，但因已有持仓或没有候选而不产生信号；
2. 脚本没有运行，或者执行过程中发生错误。

现在为E2和L分别增加独立运行状态账本，不向正式信号文件写入空记录，因此不会
改变组合引擎、开仓计划或实盘下单读取口径。

## 状态定义

| 状态 | 含义 | 审计显示 |
|---|---|---|
| `SIGNAL_READY` | 脚本正常完成且已生成正式信号 | ✅ |
| `NO_SIGNAL_OCCUPIED` | E2因已有策略仓或A/C/D已占用资金而正常不触发 | ℹ️ |
| `NO_CANDIDATE` | 脚本正常完成，但所有候选均未通过过滤 | ℹ️ |
| `ERROR` | 数据、规则或脚本执行失败 | ⚠️ |
| `NOT_RUN` | 当日没有运行状态，也没有升级前正式信号 | ⚠️ |

若状态为`SIGNAL_READY`但正式信号文件没有当日记录，或者正常无信号状态下仍残留
当日正式信号，审计会按`ERROR`处理，防止两个账本互相矛盾时静默放行。

## 文件

- E2正式信号：`reports/strategy_e2/e2_signals_recent.json`
- E2运行状态：`reports/strategy_e2/e2_signal_runs_recent.json`
- L正式信号：`reports/strategy_l/l_signals_recent.json`
- L运行状态：`reports/strategy_l/l_signal_runs_recent.json`

运行状态按`signal_date`覆盖，同一天重跑后以最后一次结果为准，最多保留最近20个
交易日。`--dry-run`不会写正式信号，也不会写运行状态。

## 运行与验证

正常收盘流水线无需增加命令，原有E2、L步骤会自动写状态：

```powershell
py -3.11 start_windows.py
```

也可以盘后单独验证：

```powershell
py -3.11 scripts\run_strategy_e2_signal.py --signal-date 20260803
py -3.11 scripts\run_strategy_l_signal.py --signal-date 20260803
```

验证测试：

```powershell
py -3.11 -m unittest tests.test_signal_run_status
```

当日正常无信号时，收盘审计应显示`ℹ️ NO_SIGNAL_OCCUPIED`或
`ℹ️ NO_CANDIDATE`，不再进入“增强/备用项缺失”警告汇总。
