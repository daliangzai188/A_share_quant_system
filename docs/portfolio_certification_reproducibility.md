# 当前组合严格as-of机械复利复现规则

当前正式配置为`ACDE_CED_V12_6046_20260630`，生效日`20260902`，固定腿序
`A>C>E>D`。A保持不变，只替换C/E/D：

- C：保留原两分支，并在全市场跌停少于30只时允许市场龙头第2～3名；
- E：保留R1双因子排名，第一名落在龙头排名11～30或全市场涨停120～180时空仓，不回补第二名；
- D：只允许成长板09:30～10:00浅炸回封，信号时全市场炸板率25%～75%、累计首板触板少于40只。

三年认证窗口为`20230701~20260630`。按真实`action_date`、单账户资金占用、
82.5%仓位、日期化费用、双边0.1%滑点、T+1、涨跌停及D成交压力重放，正式锁定结果为：

| 指标 | 结果 |
|---|---:|
| 成交数 | 176 |
| 胜率 | 72.73% |
| 平均单笔账户收益 | 5.4840% |
| 中位数单笔账户收益 | 3.7326% |
| 机械复利倍数 | 6046.316594 |
| 最大回撤 | -24.3374% |
| 最大单笔盈利 | 47.6253% |
| 最大单笔亏损 | -18.5129% |
| 盈亏比 | 2.1794 |
| 最大连续亏损 | 4 |
| 分腿成交 | A78 / C46 / E36 / D16 |

最近两年确认段为135笔、3165.327401倍、最大回撤-14.1198%；2026H1为40笔、
7.425547倍、最大回撤-11.5284%。这两个区间与三年发现窗口重叠，不是独立样本外。
2026-07～08冻结前向只有10笔，且新旧组合没有产生差异，不能证明V12已经通过前向验证。

## 正式复现

先重新生成V12三年正式基准：

```bash
python3 scripts/optimize_acde_rolling_three_year.py \
  --as-of 20260630 \
  --output-dir reports/acde_rolling_optimization/20260630_v12_formal_baseline_verification
```

然后生成并核验正式证书：

```bash
python3 scripts/certify_acde_v12_release.py
```

证书必须输出`status=PASS`、`scenario=acde_ced_v12_6046_formal`、176笔和
6046.316594倍；配置、代码、输入和基准重放任一哈希变化都必须失败关闭，不能静默刷新。

正式产物包括：

- `reports/current_portfolio_alignment/live_certification.json`；
- `reports/current_portfolio_alignment/strict_asof_audit.json`；
- `reports/current_portfolio_alignment/strict_asof_portfolio_report.md`；
- `reports/acde_rolling_optimization/20260630_v12_formal_baseline_verification/`。

旧`certify_strict_asof_portfolio.py`和`certify_current_executable_portfolio.py`只用于历史
两年口径或身份归档，不得覆盖V12正式证书。

## 风险边界

V12候选空间是在同一三年窗口查看亏损暴露后扩展，协议仍是`STRICT_DISCOVERY`，
`release_eligible=false`。本次启用依据是用户明确风险接受，不等于`LOCKED_OOS`或
`WALK_FORWARD`通过，也不构成未来收益承诺。机械复利必须按成交顺序执行
`equity *= 1 + account_return`；候选池连乘、固定本金收益、跳过交易或各腿独立复利
不得混入组合复利。实盘应先小资金运行，并单独监控成交偏差、连续亏损和滚动回撤。
