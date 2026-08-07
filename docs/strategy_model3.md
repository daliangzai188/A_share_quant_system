# model=3 自动切换候选说明

> 2026-08-03：策略B已删除。旧8302倍认证仅保留审计价值；当前发布标尺已经按
> ACDE2/D/L、82.5%仓位、串行单账户和可执行成交口径重新逐日回放。

## 当前结论

model=3 已完成离线研究和模拟实盘口径认证，并已按用户确认切换为当前实盘状态机。

当前默认配置：

```text
active_strategy_profile.mode = 3
strategy_model3.enabled = true
strategy_model3.live_order_enabled = true
strategy_model3.run_mode = live_candidate
```

含义：

```text
mode=1：使用当前 ACDE2/D 组合实盘状态机（B已删除）。
mode=2：独立 L 龙头策略状态机。
mode=3：model=3 自动切换实盘状态机，先生成 mode=1 计划，再按认证规则决定是否由 L 补位/替换。
```

真实下单仍必须通过 `LiveOrderGateway` 的交易时间、资金、仓位、涨跌停、重复委托等风控校验。

## 候选规则

model=3 的设计目标是让当前 mode=1 和 L 龙头策略在不同市场环境下切换，但不让两套策略同时占用同一资金。

当前候选规则：

```text
mode=1 默认优先；
L 通过稳健基础条件时才参与；
mode=1 空闲时允许 L 补位；
mode=1 有交易计划时，L 替换必须满足：
  market_segment = chi_next
  theme_limit_count >= 2
  first_time_detail_bucket != after_1430
```

L 稳健基础条件：

```text
market_segment != star
segment_retreat_state_bucket in neutral/warming_2day
market_chain_count_bucket in 3_8/8_15/15_30/gte_30
```

本轮只扩展L的基础环境，不改变L本体选股、T+1买入、退出规则、成交可靠性过滤，
也不放宽L顶替A/C/E2时的三项保护。基础规则统一放在
`src/strategy_model3_policy.py`，组合实盘、守护进程播报和历史认证共同调用；关键字段缺失时拒绝。

## 当前可执行组合认证（2026-08-03）

认证脚本：

```bash
python3 scripts/certify_current_executable_portfolio.py
```

| 指标 | L扩容前 | L扩容后 |
|---|---:|---:|
| 完整组合样本 | 129 | 132 |
| L分支样本 | 32 | 41 |
| L分支复利 | 3.619倍 | 7.186倍 |
| 完整组合复利 | 3254.13倍 | 4712.47倍 |
| 完整组合最大回撤 | -18.84% | -18.84% |

最终132笔构成：A 27、C 9、D普通17、D→A 2、D→C 6、E2 30、L 41。
扩容前后，前半段完整组合202.16→219.97倍，后半段16.10→21.42倍；2024和2025改善，
2026短窗2.412→2.349倍（-2.58%）略降。该短窗已如实保留，不针对单笔结果继续调参。

> **注（2026-08-07）**：以上为该改动**上线当时**的验收数据。此后 A/C 候选不再被
> `baseline.abc_return` 这张作废持仓表裁剪（commit f927d36），组合基准已更新为
> L扩容后 139笔/6907.348272倍、含M 155笔/20606.559742倍（**该组数字为当日中间态，
> 已被下方 2026-08-07 再次更新取代**）。
> 本处数字保留为历史验收记录，**不再是当前标尺**；当前标尺见
> `docs/strategy_benchmark_and_verdicts.md` 第一节。
>
> 2026-08-07 再次更新：衔接日D剔除 + D接力全关 + 腿序重排 D>L>A>M>E2>C 后，
> 当前发布标尺为 **151笔 / 27870.307776倍 / 最大回撤 -23.50% / 胜率 68.87%**
> （不含M的对照 135笔 / 7677.219011倍）。见 commit 5e2b409 / 54a66c1 / 0b20b24。

## 核心优先级逻辑

当前实盘的核心逻辑是先生成 mode=1 的 A/C/E2/D 计划，再判断 L 是否参与。L 不能无条件抢单，只有满足认证过的补位或替换规则时才会改变 mode=1 结果。

| 情况 | 结果 |
|---|---|
| mode=1 有 A/C/E2 计划，L 不满足替换保护 | 继续执行 A/C/E2 |
| mode=1 没有计划，L 满足基础条件 | L 补位 |
| mode=1 有计划，L 满足更严格替换条件 | L 替换 mode=1 计划 |
| L 不满足条件 | 回到 mode=1，也就是 A/C 优先，无 A/C 才 E2 |

这条优先级是当前实盘 mode=3 的资金占用核心：同一时间只允许一套策略占用同一资金，避免 L、A/C、E2、D 之间重复开仓。仅人工退出的历史 B 仓同样阻断新仓。

## 删除B前的模拟实盘口径认证（历史）

认证脚本：

```bash
python3 scripts/certify_strategy_model3_live_candidate.py
```

认证口径：

```text
使用当前 mode=1 日收益曲线作为基准。
L 分支使用已认证的 L2 信号源。
同一资金不重叠持仓。
L 买入按实盘约束口径处理。
L 卖出按 T+2 收盘/跌停顺延口径处理。
重新计算组合资金占用和复利。
```

删除 B 前的认证结果如下，不能代表当前 ACDE2/L：

| 指标 | 结果 |
|---|---:|
| 总交易数 | 173 |
| L交易数 | 30 |
| mode=1交易数 | 143 |
| 胜率 | 69.94% |
| 平均收益 | 5.92% |
| 中位数收益 | 2.69% |
| 复利 | 8302.39倍 |
| 最大回撤 | -18.28% |
| 最大单笔盈利 | +79.23% |
| 最大单笔亏损 | -15.87% |
| 最大连续亏损 | 4 |
| 最大回撤区间 | 2024-08-15 到 2024-09-05 |
| 恢复前高 | 2024-09-20 |

年度表现：

| 年份 | 交易数 | L交易数 | 复利 | 最大回撤 |
|---|---:|---:|---:|---:|
| 2024 | 53 | 8 | 23.32倍 | -18.28% |
| 2025 | 92 | 18 | 100.62倍 | -13.46% |
| 2026 | 28 | 4 | 3.54倍 | -5.74% |

报告路径：

```text
reports/strategy_model3/live_candidate/model3_live_candidate_report.md
reports/strategy_model3/live_candidate/model3_live_candidate_summary.csv
reports/strategy_model3/live_candidate/model3_live_candidate_trades.csv
reports/strategy_model3/live_candidate/model3_live_candidate_drawdown.csv
reports/strategy_model3/live_candidate/model3_live_candidate_yearly.csv
```

## 研究链路

已生成的研究脚本：

```text
scripts/research_strategy_model3_switch.py
scripts/validate_strategy_model3_switch.py
scripts/search_strategy_model3_robust_rules.py
scripts/audit_strategy_model3_conflicts.py
scripts/search_strategy_model3_safe_modes.py
scripts/audit_strategy_model3_safe_candidates.py
scripts/validate_strategy_model3_safe_candidate_choice.py
scripts/audit_strategy_model3_failed_window.py
scripts/search_strategy_model3_occupancy_guards.py
scripts/certify_strategy_model3_live_candidate.py
```

关键研究结论：

```text
原始 model=3 全量切换会在部分窗口跑输 mode=1。
跑输原因不是 L 单笔收益差，而是 L 持仓占用后错过 mode=1 的正收益交易。
加入 theme_limit_count>=2 且排除 after_1430 后，唯一失败窗口被修复。
```

## 当前开关方式

回滚到 mode=1：

```json
{
  "active_strategy_profile": {
    "mode": 1
  },
  "strategy_model3": {
    "enabled": false,
    "live_order_enabled": false
  }
}
```

当前 mode=3 实盘配置：

```json
{
  "active_strategy_profile": {
    "mode": 3
  },
  "strategy_model3": {
    "enabled": true,
    "live_order_enabled": true
  }
}
```

注意：mode=3 不绕过风控；生成的买入计划仍必须经过 `LiveOrderGateway` 和 QMT 返回结果确认。

## 验证方式

配置合法性：

```bash
python3 -m json.tool config/config.json
```

编译检查：

```bash
python3 -m py_compile src/combined_live_engine.py scripts/certify_strategy_model3_live_candidate.py
```

生成当前组合计划：

```bash
python3 scripts/run_combined_live_plan.py
```

重新运行当前组合认证：

```bash
python3 scripts/certify_current_executable_portfolio.py
```

## 风险说明

model=3 已按用户确认接入实盘状态机，但仍处于小资金验证优先阶段。

运行过程中仍需要重点复核：

```text
小资金验证；
盘前信号日志复核；
买入委托和撤单复核；
持仓释放复核；
通知中心复核；
异常回滚方案。
```

历史认证结果不代表未来收益，不承诺固定收益率。
