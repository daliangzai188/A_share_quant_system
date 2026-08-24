# 当前组合严格as-of机械复利复现规则

当前锚点为`20240630~20260630`。新窗口优化前底座为133笔、
300.31246148623836倍；经过A、C、A封单比例扩展及E双因子更新后，当前严格
研究标尺为真实开仓日A>C>E>D的136笔、1023.791243962826倍。旧305.348870倍、
五年343.5434倍、信号日D>A>E>C的1375.6238529689376倍和同信号日重排
1463.912878倍都不得进入当前比较。

正式收益证书只能由 `scripts/certify_strict_asof_portfolio.py` 生成。旧脚本
`scripts/certify_current_executable_portfolio.py` 只写
`legacy_identity_alignment.json`，用于核对实盘选股身份，不能覆盖正式证书、
不能提供正式复利、不能用于发布比较。

当前组合认证分为两类文件：

- Git内的代码、配置、`input_manifest.json`和认证结果；
- Git外的历史行情及研究明细。大体量市场数据不提交到Git，但每个认证输入都必须在清单中记录相对路径、文件大小和SHA-256。

## 日常复核

把历史数据恢复到清单记录的相对路径后运行：

```bash
python3 scripts/certify_strict_asof_portfolio.py
```

默认行为只核对锁定清单。任一输入缺失或内容变化都会失败，并把实盘认证状态先改为非PASS，禁止新的买入计划。

## 重现本轮排序搜索

```bash
python3 scripts/optimize_strict_acde_from_official_baseline.py --legacy-baseline
```

该命令是历史归档复现，不是当前A>C>E>D半年优化入口。优化器显式重建A原`profit_source_score+turnover_rate`排序和E原
`circ_mv:asc`排序，因此不会因为当前A/E已应用新规则而丢失本轮底座。
预期日志必须显示：

- 底座133笔、300.31246148623836倍；
- D/C为`KEEP_CURRENT`；
- A/E为`DUAL_GATE_PASSED`；
- A独立单账户由58笔、10.103128倍提高到58笔、12.023750倍；
- E独立单账户由76笔、4.664899倍提高到76笔、10.834162倍；
- A/E同时应用后132笔、327.72671897548867倍。

上述命令复现的是早期A/E排序搜索，不包含后续C、A封单扩展和E双因子研究。
当前E双因子发现过程由以下命令复现：

```bash
python3 scripts/research_strategy_e_current_window.py
```

它生成202条规则定义并去重为160个唯一候选结果；正式落地规则为换手率高值与
一日成交额倍率低值的50%/50%同日分位综合分。当前严格证书必须另外复现E独立
74笔、11.7037898965倍，以及ACDE 136笔、1023.7912439628倍、分腿A42/C47/E36/D11。

候选池连乘和独立单账户复利在摘要中分栏保存。独立腿门槛只读取执行自身占仓约束后的
`official_baseline_legs`及`best_by_leg.*.leg_metrics`，不得用候选池数字替代。

## 确认更新数据版本

只有确认数据修复或研究口径调整后才运行：

```bash
python3 scripts/certify_strict_asof_portfolio.py --refresh-input-manifest
```

随后必须单独审查并提交：

1. `reports/current_portfolio_alignment/strict_asof_input_manifest.json`差异；
2. `strict_asof_portfolio_trades.csv`和`strict_asof_audit.json`收益变化；
3. `live_certification.json`中的代码、配置和输入摘要；
4. 完整测试及历史选择路径核对结果。

机械复利固定为按信号日升序，对单账户实际成交逐笔执行
`equity *= 1 + account_return`；候选、跳过交易、固定本金收益和各腿独立复利
不得混入。不得为了让认证通过而无说明刷新清单，也不得把历史倍数当作
实盘收益预期。
