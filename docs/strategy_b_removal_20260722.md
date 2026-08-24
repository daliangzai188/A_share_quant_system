# 策略 B 删除发布记录（2026-07-22）

## 一、发布结论

策略B已从当前交易链路退役；当前组合已更新为A>C>E>D，且须取得正式样本外认证后才能授权新BUY。

本次删除的含义是：

```text
不再生成 B 候选
不再生成 B 新开仓计划
不再接受历史目录中的 B 买入计划
不再生成 B 自动卖出计划
不再执行 B 盘中止盈、POV、14:55平仓或看门狗补卖
旧 B 回放和旧 A+B 发布验证禁止继续作为当前发布依据
```

配置中保留最小的 `b_strategy` 退役墓碑，而不是保留策略条件。墓碑只用于 fail-closed：即使旧计划文件、旧持仓或旧脚本仍存在，也能识别“B已删除”并拒绝自动执行。

## 二、删除原因与历史数据口径

删除由用户在 2026-07-22 明确确认。历史 B 回测、逐笔报告和旧成交账本保留，只用于审计，不参与当前候选和下单。

原 A+B、A+B+C 报告包含 B 历史交易，因此这些复利、胜率和回撤指标不能直接代表删除 B 后的当前组合。`strategy_release_validation.current_release_valid` 已设置为 `false`，在完成 A/C/D/E 全链路重新回放前，不得把旧指标当成当前发布结果。

## 三、当前持仓处理

删除发生时仍有一笔历史 B 持仓：

```text
股票：000048.SZ 京基智农
买入日：2026-07-22
本地计划退出日：2026-07-23
处理方式：用户通过券商/QMT手动卖出
系统行为：仅监控持仓占用，不提交任何自动卖单
```

运行时持仓记录使用：

```json
{
  "manual_exit_only": true,
  "auto_exit_disabled": true
}
```

即使运行时文件没有上述字段，只要策略配置中的 B 退役墓碑存在，守护进程仍会把未平仓 B 识别为仅人工退出，属于第二道 fail-closed 保护。

在该持仓由用户卖出并由账户心跳同步为零以前，所有当前策略均不得占用同一资金开新仓。

## 四、代码改动清单

### 1. 配置

`config/strategy_config.json`

- 删除 B 的启用状态、选股条件、风险过滤和执行模型。
- 增加 B 退役墓碑：禁止新增买入、禁止自动退出、仅人工处理旧仓。
- C 从“A/B都无候选”调整为“A无候选”后直接检查。
- C 使用自己的风险规则，不再在当前链路中继承 B 配置。
- 旧发布验证标记失效，A/C组合必须重新回测。

`config/config.json`

- 当前 mode=1 名称由 `ABCDE` 改为 `ACDE`。
- 组合说明同步改为 A/C/D/E 口径。

### 2. 候选与每日操作台

`scripts/run_paper_ab_filtered_daily_ops.py`

- 主流程由 A→B→C 改为 A→C。
- 删除 B 回放、B 风险过滤和 B 输出文件。
- 把当前 A/C 需要的通用筛选函数移入当前操作台，解除对旧 B 研究脚本的运行依赖。

`scripts/generate_live_limit_pool_daily_ops.py`

- 当日涨停池兜底顺序改为 A→C→WATCH。
- 不再生成空的 B 候选和 B 拒绝文件。

`scripts/audit_signal_readiness.py`

- 审计漏斗只展示 A/C。
- 不再加载 B 条件或输出 B 候选审计。

### 3. 组合状态机与补位

`src/combined_live_engine.py`

- 旧 B `BUY` 和 `SELL` 计划全部从当前组合计划中过滤。
- 人工退出仓不生成组合卖单。
- 人工退出仓存在时阻断 A/C/D/E 新开仓。
- 当前组合标题、原因和模式名称更新为 A/C 口径。

`scripts/run_strategy_e_signal.py`

- 历史 B 计划文件不再被当作当前买入计划。
- 真实未平仓的历史 B 仓仍属于资金占用，用户卖出前 E 不触发。

### 4. 自动退出硬拦截

`scripts/trading_daemon.py`

新增统一判断 `_position_manual_exit_only()`，并应用到：

```text
集合竞价退出旁路
POV退出监控
14:55直接平仓
收盘看门狗补挂
收盘后双快照对账
盘中止盈
盘前到期平仓检测
到期计划检测
```

`_do_sell()` 和 `_watchdog_rescue_sell()` 内部也有最终硬拦截。即使上游遗漏过滤，底层仍拒绝 B 自动卖出。

### 5. 旧脚本失效保护

`scripts/run_paper_ab_filtered_observation_window.py`

- 当前配置下拒绝运行 B 观察回放。
- 旧代码只为历史报告复现保留，不属于当前策略入口。

`scripts/run_strategy_release_validation.py`

- 检测到旧发布验证失效后立即拒绝运行，防止旧 A+B 门槛被误当成 A/C 结果。

### 6. 通知测试隔离

`src/notify.py`

- 增加 `A_SYSTEM_DISABLE_NOTIFICATIONS=1` 环境硬开关。
- 该开关优先于正式通知配置和 Bark 地址。

`tests/test_exit_pov_safety.py`

- 退出安全测试启动时强制设置通知隔离变量。
- 冻结时钟、虚构持仓和故障注入不得进入正式手机通知中心。

`tests/test_notify_safety.py`

- 验证通知硬开关生效时不会调用任何网络请求。

## 五、当前执行顺序

```text
有未平仓持仓？
  是：等待系统正常退出；历史 B 仓只允许用户手动退出，期间禁止新仓
  否：A 主策略
        ↓ A无候选
      C 补位策略
        ↓ C无候选
      E / D 按原组合规则检查
        ↓
      当前组合状态机再按A>C>E>D真实开仓日裁决
```

内部仍有 `ABC` 文件名、函数名、委托备注等兼容标识。这些是历史接口协议，不表示 B 仍参与选股。为了避免破坏旧订单归属识别，本次不批量改名；所有真正的 `strategy_leg=B` 执行入口已经按退役墓碑过滤。

## 六、验证标准

提交前必须同时满足：

```text
JSON配置可解析
修改文件可编译
git diff --check通过
B删除专项测试通过
退出安全与QMT行情测试通过
通知隔离测试通过且不产生真实推送
当日A/C操作台不生成B文件或B计划
2026-07-23组合演练：B自动卖单=0，新买单=0
旧B观察脚本明确拒绝运行
旧A+B发布验证明确拒绝运行
```

历史报告不满足上述“当前发布”定义，只保留审计价值。

## 七、部署与重启检查

Python常驻进程不会热加载本次代码。提交和同步完成后，必须在 Windows/QMT 端安全重启交易守护进程。

重启后检查日志：

```text
当前 mode=1 名称为 ACDE
优先级为 A > C > E（B已删除）
B行只显示“已删除”
京基智农显示“仅人工退出，系统禁卖”
不存在 B 自动卖出计划
```

如果重启后仍出现 `A > B > C` 或 B 候选，说明旧进程仍在运行，应停止继续开仓并检查进程与同步目录。

用户手动卖出京基智农后，还需要确认：

```text
券商实际持仓数量为0
无未成交卖单
账户心跳已将本地B仓同步关闭
组合状态机不再显示“人工退出仓占用”
```

## 八、风险说明

删除B会释放历史上由B占用的交易日，后续可能由当前策略补入，也可能空仓。因此不能用“直接从旧组合扣掉B收益”的方法估算新复利。

必须按照真实优先级和资金占用重新逐日回放，统计样本数、胜率、平均/中位收益、复利、最大回撤、最大单笔盈亏、连续亏损、手续费、滑点和不可成交情况。完成前不承诺删除 B 后的收益改善幅度。

## 九、本次提交前实际验证记录

验证时间：2026-07-22。

### 静态检查

```text
config/config.json：JSON解析通过
config/strategy_config.json：JSON解析通过
data/processed/positions.json：JSON解析通过（运行时文件，不进入Git提交）
修改Python文件：py_compile通过
git diff --check：通过
```

### 自动化测试

```text
python3 -m unittest discover -s tests -p 'test_*.py'
Ran 55 tests
OK
```

测试覆盖包括：

```text
B候选删除后A直接回落到C
旧B BUY/SELL计划全部过滤
B配置不可读时失败关闭（禁止新增并视为已退役）
B持仓人工退出识别
_do_sell底层拒绝B自动卖出
看门狗补挂底层拒绝B自动卖出
C独立风险规则
退出POV与收盘交接安全
QMT行情字段映射
测试环境通知硬隔离且网络请求次数为0
```

### 当日操作台演练

使用 2026-07-21 信号数据运行当前 A/C 每日操作台：

```text
A候选=0
C候选=0
planned_order_count=0
输出目录不存在任何B候选、B拒绝或B计划文件
E因京基智农历史B仓仍占用而阻断
```

### 2026-07-23组合状态机演练

```text
open_b_count=1
manual_exit_guard=true
b_removed=true
b_new_entry_enabled=false
buy_order_count=0
b_sell_order_count=0
```

决策包含：

```text
BLOCK_ABC_BUY（当前strategy_leg=A+C）
BLOCK_D_INTRADAY_MONITOR
BLOCK_E
BLOCK_BUY_BY_MANUAL_EXIT_POSITION
```

### 旧入口失效检查

```text
B观察回放：按预期退出并提示“策略B已彻底删除”
旧A+B发布验证：按预期退出并提示“A/C组合必须重新回测”
```

### 尚未完成的部署动作

Windows/QMT 常驻守护进程在代码修改前已经启动，Python进程不会热加载新代码。本次Git提交不等于完成运行时部署；代码同步后仍必须安全重启守护进程，并按第七节复核新日志。
