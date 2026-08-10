# 当前组合认证复现规则

当前组合认证分为两类文件：

- Git内的代码、配置、`input_manifest.json`和认证结果；
- Git外的历史行情及研究明细。大体量市场数据不提交到Git，但每个认证输入都必须在清单中记录相对路径、文件大小和SHA-256。

## 日常复核

把历史数据恢复到清单记录的相对路径后运行：

```bash
python3 scripts/certify_current_executable_portfolio.py
```

默认行为只核对锁定清单。任一输入缺失或内容变化都会失败，并把实盘认证状态先改为非PASS，禁止新的买入计划。

## 确认更新数据版本

只有确认数据修复或研究口径调整后才运行：

```bash
python3 scripts/certify_current_executable_portfolio.py --refresh-input-manifest
```

随后必须单独审查并提交：

1. `reports/current_portfolio_alignment/input_manifest.json`差异；
2. `portfolio_summary.csv`和`portfolio_report.md`收益变化；
3. `live_certification.json`中的代码、配置和输入摘要；
4. 完整测试及历史选择路径核对结果。

不得为了让认证通过而无说明刷新清单，也不得把冻结历史倍数当作实盘收益预期。
