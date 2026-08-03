# 策略 L 龙头策略接入说明

> 2026-08-03：策略B已删除；当前model=3组合已按ACDE2/D/L、82.5%仓位、
> 串行单账户和可执行成交口径重新逐日回放。

## 当前结论

策略 L 已接入实盘状态机。独立 mode=2 不开启；当前 mode=3 只在认证规则允许时让 L 补位或替换。

当前默认配置：

```text
active_strategy_profile.mode = 3
strategy_l.enabled = false
strategy_l.live_order_enabled = false
```

含义：

```text
mode=1：继续使用当前 ACDE2/D 组合实盘状态机（B已删除）。
mode=2：切换到独立 L 龙头策略状态机。
mode=3：model=3 自动切换实盘状态机，在 mode=1 和 L 之间按认证规则切换。
strategy_l.enabled=false：L 已接入，但策略本身不允许生成实盘计划。
strategy_l.live_order_enabled=false：L 即使有信号，也不允许生成真实买入计划单。
```

当前用户已确认切换到 mode=3；L 独立 mode=2 仍然关闭，L 只会在 model=3 规则允许时补位或替换。

model=3基础环境现允许`market_chain_count_bucket=3_8/8_15/15_30/gte_30`。
新增的3~8组只扩容可成交机会，不改变L2本体选股，也不放宽L替换A/C/E2时的
“创业板、题材涨停数至少2、非14:30后首封”三项保护。

model=3 的研究候选说明见：

```text
docs/strategy_model3.md
```

## 已选 L 版本

当前接入版本为 L2：

```text
L_theme_mainline_leader
+ 排除 segment_retreat_state_bucket=retreat_2day
+ 排除 segment_limit_down_count_bucket=3_8
+ 排除 theme_limit_count=30
```

本体条件：

```text
theme_data_available = true
theme_is_mainline = true
theme_leader_rank = 1
```

实盘信号脚本还会做基础过滤：

```text
limit_data_quality = full
strategy_compatible = true
非 ST
allow_buy_reliable = true
is_fill_score_reliable = true
```

## 过去 2 年实盘约束模拟认证

认证脚本：

```bash
.venv/bin/python scripts/certify_strategy_l_live_execution.py
```

认证口径：

```text
T 日收盘生成信号。
T+1 买入。
T+2 收盘卖出。
同一资金不能重叠持仓，上一笔未卖出前跳过新信号。
买入日涨停开盘按保守口径判定买不到。
卖出日跌停按保守口径判定卖不出，并在 D5 内顺延到第一个可卖日。
重新扣手续费、印花税、过户费、买卖滑点。
```

最新认证结果：

| 版本 | 理论信号数 | 实盘约束执行数 | 持仓冲突跳过 | 买入拒绝 | 胜率 | 平均账户收益 | 中位账户收益 | 复利 | 最大回撤 | 最大盈利 | 最大亏损 | 最大连续亏损 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L2 | 201 | 138 | 63 | 0 | 57.97% | 4.41% | 1.34% | 142.35倍 | -30.35% | 79.23% | -18.26% | 4 |

对比参考：

| 版本 | 说明 | 实盘约束执行数 | 胜率 | 平均账户收益 | 复利 | 最大回撤 |
|---|---|---:|---:|---:|---:|---:|
| L1 | 排除沪主板+retreat_2day+theme_limit_count=30 | 136 | 58.09% | 4.32% | 118.14倍 | -30.64% |
| L2 | 排除retreat_2day+跌停3_8+theme_limit_count=30 | 138 | 57.97% | 4.41% | 142.35倍 | -30.35% |
| L3 | 排除半导体+沪主板+retreat_2day | 138 | 60.14% | 4.69% | 209.65倍 | -28.24% |

说明：

```text
L2 理论复利为 2254.44 倍。
加入实盘约束后，L2 复利下降为 142.35 倍。
下降主要来自同一资金持仓占用导致 63 个信号被跳过。
L独立mode=2仍关闭；当前mode=3只在既有保护规则允许时参与ACDE2/D。
```

## model=3组合内L扩容结果

L2独立认证的138笔/142.35倍用于验证L策略本体；组合内结果还要受到A/C/E2优先级、
D持仓和单账户资金占用约束，所以必须单独逐日回放，不能把各腿复利直接相乘。

| 口径 | 扩容前 | 扩容后 |
|---|---:|---:|
| 组合内L执行数 | 32 | 41 |
| 组合内L分支复利 | 3.619倍 | 7.186倍 |
| 完整组合样本 | 129 | 132 |
| 完整组合复利 | 3254.13倍 | 4712.47倍 |
| 完整组合最大回撤 | -18.84% | -18.84% |

前半段与后半段的L分支及完整组合均改善；2024、2025完整组合改善，2026短窗完整组合
略降2.58%，但L分支仍改善。历史结果不代表未来，实盘继续按小资金核对成交率、滑点和容量。

## 实盘接入链路

收盘后：

```text
scripts/trading_daemon.py
  -> job_post_market()
  -> run_strategy_l_signal.py
  -> reports/strategy_l/l_signal_YYYYMMDD.json
```

盘前/开盘：

```text
src/combined_live_engine.py
  -> active_strategy_profile.mode == 2
  -> strategy_l.enabled == true
  -> strategy_l.live_order_enabled == true
  -> 读取昨日 L 信号
  -> planned_buy_date == 今日
  -> 生成 ALLOW_L_BUY 和 L 买入计划单
```

守护进程执行：

```text
scripts/trading_daemon.py
  -> job_premarket_buy()
  -> job_opening_buy()
  -> handle_combined_order_preview()
  -> LiveOrderGateway 二次风控
  -> QMT 下单
```

平仓：

```text
positions.json 中 strategy_leg=L
planned_exit_date 到期
14:56 收盘平仓窗口进入 check_and_close_positions()
复用 A/C 的收盘平仓链路（内部ABC命名为历史接口兼容保留）
按买10/买5优先挂限价卖出
成交后回写本地持仓
```

## 开关方式

只生成 L 信号但不交易：

```json
{
  "active_strategy_profile": {
    "mode": 1
  },
  "strategy_l": {
    "enabled": false,
    "live_order_enabled": false
  }
}
```

切换到 L 独立模式但仍不下单：

```json
{
  "active_strategy_profile": {
    "mode": 2
  },
  "strategy_l": {
    "enabled": false,
    "live_order_enabled": false
  }
}
```

允许 L 生成实盘计划单：

```json
{
  "active_strategy_profile": {
    "mode": 2
  },
  "strategy_l": {
    "enabled": true,
    "live_order_enabled": true
  }
}
```

注意：即使生成 L 计划单，真实下单仍必须满足 `trade_mode=live`、`live_trade.enabled=true`、`live_trade.real_order_enabled=true`、确认文本、交易时间、资金、仓位、涨跌停、重复委托等 LiveOrderGateway 校验。

## 验证方式

配置合法性：

```bash
python3 -m json.tool config/config.json
```

编译检查：

```bash
.venv/bin/python -m py_compile src/combined_live_engine.py scripts/trading_daemon.py scripts/run_strategy_l_signal.py
```

L 信号 dry-run：

```bash
.venv/bin/python scripts/run_strategy_l_signal.py --signal-date 20260625 --dry-run
```

默认模式 1 组合计划：

```bash
.venv/bin/python scripts/run_combined_live_plan.py
```

实盘约束模拟认证：

```bash
.venv/bin/python scripts/certify_strategy_l_live_execution.py
```

## 风险说明

L2 在实盘约束模拟下仍有 -30.35% 最大回撤，最大单笔亏损 -18.26%，最大连续亏损 4 次。后续如果要打开 L，必须先小资金验证，不要直接放大资金。
