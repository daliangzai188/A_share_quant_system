# C策略半年多因子条件并集优化

## 1. 目标

`scripts/optimize_strategy_c_factor_union.py`用于每年6月30日、12月31日重算C。
它参考D因子脚本的可复现结构，但严格执行C本次单独约定：不是只选一条最佳
条件，而是把所有达到分支门槛的`if`条件按OR合并为一个候选C。

脚本默认只生成研究报告，不修改正式C。只有同时满足以下条件，并由人工阅读
报告后显式传入`--apply`，才会原子发布：

1. 达标条件OR后的C独立单账户机械复利高于当前正式C；
2. 只替换C、冻结D/A/E后，`D>A>E>C`总组合机械复利高于当前正式ACDE。

## 2. 冻结口径

- 更新节点：仅`0630`、`1231`；
- 研究窗口：更新节点向前自然两年；
- 数据：严格as-of可靠可买涨停池；
- C前提：A当日无候选；
- 买入：T+1开盘，开盘涨停不可买；
- 卖出：C固定T+3收盘，跌停卖不出则顺延；
- 仓位：82.5%；
- 费用、双边滑点、前复权链接、T+1和单账户占仓规则保持正式口径；
- 组合腿序：`D>A>E>C`；
- D使用`config/strategy_d_factor_release.json`中的当前正式版本，不能退回旧D。

## 3. 固定因子值

公共定义在`src/strategy_c_factor_rules.py`，schema为
`C_CLOSE_FACTOR_VALUES_V1`。26个字段全部在T日收盘可见，并使用数据管线已经
冻结的离散值：

```text
market_segment
market_emotion_state_bucket
segment_emotion_state_bucket
market_chain_count_bucket
segment_chain_count_bucket
market_limit_down_count_bucket
segment_limit_down_ratio_bucket
segment_limit_max_height_bucket
market_leader_rank_bucket
segment_market_leader_rank_bucket
segment_limit_height_rank_bucket
first_time_detail_bucket
limit_times_detail_bucket
open_times_bucket
amount_bucket
turnover_rate_bucket
volume_ratio_bucket
fd_ratio_bucket
prev_pct_chg_bucket
amount_ratio_bucket
limit_up_count_bucket
segment_limit_up_count_bucket
segment_limit_up_ratio_bucket
retreat_state_bucket
segment_retreat_state_bucket
board_type
```

不允许把次日开盘、退出价、已实现收益、竞价后信息或开盘5分钟字段放入C条件。
字段缺失、出现未冻结分类值或正式发布文件非法时一律fail-closed。

## 4. 条件和并集语义

脚本枚举1～3个不同因子的实际值组合。一条分支内部是AND：

```python
if market_chain_count_bucket == "15_30" and fd_ratio_bucket == "0_5pct_1pct":
    allow_c = True
```

分支必须同时满足：

- 独立C单账户实际执行至少20笔；
- 平均每笔账户收益严格大于2%；
- 胜率严格大于55%；
- 没有无法解析的退出。

所有达标分支之间是OR。脚本不会再从达标分支中选唯一最佳，也不会搜索一个
收益最高的子集。OR完成后重新按当前C排序选每日第一名、执行C自己的T+3占仓，
再分别回放C和ACDE。单个分支达标不代表并集达标，最终发布只看双复利闸门。

## 5. 当前2026-06-30运行结果

正式锚点已在运行时复现：

| 口径 | 交易数 | 复利 |
|---|---:|---:|
| 当前正式C独立单账户 | 35 | 3.1108307990倍 |
| 当前正式ACDE | 129 | 486.3661434308倍 |

搜索及并集结果：

| 项目 | 结果 |
|---|---:|
| 固定因子 | 26个 |
| 1～3因子列组合 | 2,951组 |
| 实际观察因子值组合 | 289,604条 |
| 达到20笔支持度并完成回放 | 153,915条 |
| 同时满足20笔、均值>2%、胜率>55% | 2,557条 |
| OR后C独立 | 149笔、0.1538081036倍 |
| OR后ACDE | 172笔、0.8054873735倍 |
| 双复利闸门 | 不通过 |
| 正式C修改 | 否 |

2,557条单独达标分支OR后覆盖了20,351行母池中的20,333行，导致条件失去
过滤能力。这个结果不是脚本选错“最佳条件”，而是“全部达标分支必须OR”规则
本身在当前窗口的真实聚合结果。正式C因此继续使用旧AND条件。

## 6. 运行和查看

只生成报告：

```bash
python3 scripts/optimize_strategy_c_factor_union.py --as-of 20260630
```

省略`--as-of`时自动使用不晚于运行日的最近半年节点。完整结果：

```text
reports/strategy_c_factor_optimizer/<更新节点>/best_c_factor_union.txt
```

同目录还会生成：

- `all_factor_profiles.csv`：所有支持度充分的分支；
- `qualified_factor_profiles.csv`：全部达标分支；
- `c_if_conditions.py.txt`：全部OR条件的等价伪代码；
- `candidate_union_daily_picks.csv`：并集每日首选；
- `incumbent_c_detail.csv`、`candidate_c_detail.csv`：C替换前后逐日账本；
- `incumbent_acde_detail.csv`、`candidate_acde_detail.csv`：ACDE替换前后逐日账本；
- `summary.json`、`candidate_release.json`：机器可读审计与候选发布。

只有报告显示双门通过且人工确认后才运行：

```bash
python3 scripts/optimize_strategy_c_factor_union.py --as-of 20260630 --apply
```

未通过双门时，即使传入`--apply`也不会修改
`config/strategy_c_factor_release.json`。

## 7. 正式执行链

- `config/strategy_c_factor_release.json`：正式C模式和if分支；
- `src/strategy_c_factor_rules.py`：冻结因子值、发布校验和OR匹配；
- `scripts/run_paper_ab_filtered_daily_ops.py`：收盘候选和影子候选；
- `scripts/build_ac_daily_candidates.py`：A/C历史候选重建；
- `scripts/validate_other_live_strategies_strict.py`：严格C与ACDE回放；
- `scripts/audit_signal_readiness.py`、`scripts/trading_daemon.py`：漏斗审计和日志；
- `src/nested_walk_forward.py`：研究框架中的当前C读取。

当前正式发布为`C_LEGACY_FORMAL_2026H1`，所以旧C条件和实盘行为没有改变。
若未来双门通过并发布`FACTOR_UNION`，上述链路会共同读取同一个发布文件。

## 8. 风险边界

分支来自同一24个月的多重组合搜索，存在明显的数据挖掘和过拟合风险。前后
12个月拆分只作稳定性披露，不是真正未查看样本；更早6个月只作旁证，未来
6个月才进入前向样本外账本。机械复利只用于同执行口径版本比较，不代表未来
收益，也没有证明大资金容量。发布后仍应先模拟、再小资金验证。
