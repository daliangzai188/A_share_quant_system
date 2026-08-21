# A_System 严格 as-of 回测标准（V1）

标准编号：`A_SYSTEM_STRICT_ASOF_V1`

机械复利编号：`A_SYSTEM_MECHANICAL_COMPOUND_V1`

当前D>A>E>C两年锚点：`20240630~20260630`（首个可用信号日
为2024-07-01）。其它旧窗口只能作历史来源说明，不得进入当前候选比较。

本标准的目标是让策略在历史日期 `T` 做决定时，只能看到当时真实可获得的数据。门禁失败时停止回测，不生成可比较的收益结论。

## 1. 两类“回视”都要阻断

### 1.1 数据回视

- 信号行必须有 `trade_date`、`as_of_date`。
- 收盘后选股要求 `as_of_date == trade_date`；不能混入之后补录的数据快照。
- 成交概率模型必须使用 `asof_turnover_space_proxy_v2`。
- 可靠成交评分必须有 `model_training_end_date`，且严格满足 `model_training_end_date < trade_date`。
- 选股、过滤、排名禁止使用 `next_*`、`d1_*`、`exit_*`、`net_return`、`is_win`、已实现盈亏等结果字段。
- 未来价格可以在选股完成后合并，用于计算结果；不得重新进入选股函数。

### 1.2 参数回视

仅有 point-in-time 特征还不够。如果先看完整历史收益再调整条件，又在同一段宣称策略有效，仍然属于回视。因此所有收益研究必须声明以下协议之一：

| 协议 | 用途 | 能否发布/实盘认证 |
|---|---|---|
| `STRICT_DISCOVERY` | 在开发段找因子、找规则 | 否 |
| `LOCKED_OOS` | 规则和哈希先冻结，只评价冻结日期之后新产生的样本 | 是，但仍需通过统计、容量和实盘门禁 |
| `WALK_FORWARD` | 每一折只用该折信号日前的数据训练，再评价下一折 | 是，但仍需通过统计、容量和实盘门禁 |

`STRICT_DISCOVERY` 的报告会固定写入 `release_eligible=false` 和 `result_scope=DISCOVERY_ONLY`。它可以帮助研究，但不能作为上线依据。

## 2. 当前严格数据链

```text
limit_up_fill_scored_asof.csv
  -> next_day_premium_trades_asof.csv
  -> candidate_pool_asof.csv
  -> 严格回测 / 优化 / 成交回放
  -> strict_asof审计JSON
```

旧的非 as-of 文件保留用于历史核对，不再是共享研究入口，也不能进入正式发布认证。

正式组合统计只能由 `scripts/certify_strict_asof_portfolio.py` 写入
`reports/current_portfolio_alignment/live_certification.json`。旧来源身份脚本只写
`legacy_identity_alignment.json`；B、L、M、N曾出现的旧来源、算错或结果回看口径
不得再进入正式收益。

## 3. 机械逐笔复利要求

组合复利只允许对已经按真实单账户时序选出的实际成交，依次计算：

```text
equity_t = equity_(t-1) × (1 + account_return_t)
```

`account_return_t`必须已经计入仓位、滑点、佣金、过户费和对应日期的印花税。
候选收益、未成交/被占仓跳过的交易、固定本金累计收益、各腿独立复利相乘都不能
作为组合复利。公共实现是 `src/mechanical_compound.py`；缺失收益、乱序成交、
同日重复成交和小于等于-100%的非法收益必须直接失败。

## 4. 新策略研究要求

新策略必须复用 `src/strict_asof.py` 的门禁，优先通过 `StrategyConditionOptimizer`、`SimpleCandidateBacktester` 或 `ConservativeTradeReplay` 运行。自定义研究脚本必须在任何收益计算之前调用：

1. `validate_strict_research_frame()`；
2. 明确列出实际参与过滤和排序的 `selection_columns`；
3. 保存 `write_audit_json()` 生成的审计文件；
4. 开发阶段使用 `STRICT_DISCOVERY`；
5. 规则确定后记录 `strategy_frozen_at` 并冻结策略文件 SHA-256；`LOCKED_OOS` 只接受冻结日之后新产生的信号，历史上已经看过的数据不能通过改日期重新变成 untouched OOS。也可以实现逐折 `WALK_FORWARD`。

没有审计文件、审计哈希不一致、协议仍是开发段，实盘认证门禁都会拒绝新增 BUY。SELL 不受该门禁影响。

## 5. Windows 运行顺序

```powershell
py -3.11 scripts\score_limit_up_fill_probability.py --historical-asof
py -3.11 scripts\validate_strict_asof_dataset.py
py -3.11 scripts\analyze_next_day_premium.py
py -3.11 scripts\generate_candidate_pool.py
py -3.11 scripts\run_backtest.py
py -3.11 scripts\certify_strict_asof_portfolio.py
```

审计产物在 `reports/strict_asof/`。共享配置当前是 `STRICT_DISCOVERY`，因此上述回测只能用于开发，不能直接恢复或扩大实盘。

## 6. 判断通过的最低条件

- `duplicate_key_count == 0`
- `invalid_signal_date_count == 0`
- `invalid_as_of_date_count == 0`
- `as_of_after_signal_count == 0`
- `as_of_mismatch_count == 0`
- `reliable_method_bad_count == 0`
- `reliable_training_end_missing_count == 0`
- `reliable_training_not_prior_count == 0`
- `strict_asof_passed == true`

这些条件只证明“当时看得到”和“训练在先”，不自动证明策略有效。样本数、胜率、平均/中位收益、最大回撤、费用、滑点、成交概率、容量、统计置信区间、untouched OOS 和真实小资金成交仍需分别验证。
