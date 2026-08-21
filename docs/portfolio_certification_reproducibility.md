# 当前组合严格as-of机械复利复现规则

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
