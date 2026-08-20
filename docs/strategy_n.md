# 策略 N v3：双分支最低优先级补位腿

> 状态：已按用户明确风险接受接入当前实盘；正式优先级为
> `D>A>M>E>C>N`，N只使用前五腿都未占用后的单账户空闲资金。

## 一、策略目标与数据

N用于补充当前组合空仓日，不改变D、A、M、E、C任何一腿的候选规则。信号只读取
T日收盘后已存在的完整涨停池、严格as-of成交空间代理、市场分段高度和退潮状态，
不使用T+1价格、未来成交或未来收益字段。

历史`fill_probability`字段为成交空间代理，不是经过校准的真实概率。历史打分只能
使用信号日前样本；当日换手率必须在当日候选打分完成后才进入后续训练集。

## 二、买入与退出规则

```text
基础过滤：非ST、strategy_compatible、allow_buy_reliable、
          is_fill_score_reliable、fill_probability >= 60%
第一分支：segment_limit_max_height_bucket = 1
          segment_retreat_state_bucket ∈ {retreat_weak, retreat_2day}
          first_time_minutes升序 → circ_mv升序 → ts_code升序
补充分支：仅在第一分支无候选时启用
          market_chain_count_bucket = 3_8
          market_emotion_state_bucket = mixed
          amount降序 → circ_mv升序 → ts_code升序
候选数量：只取第一名；不可买或被上游占用时不回补第二名
执行规则：T+1固定开盘买入；T+2计划退出日可按实盘冲板止盈，否则收盘卖；
          计划日跌停/停牌则顺延到可卖日固定收盘，目标仓位82.5%
优先级：D>A>M>E>C>N
```

真实买入继续经过统一账户空仓、现金、85%单票硬顶、涨停不可买、停牌、重复委托、
实时行情与订单网关检查。到期卖出复用A/C/E/M的14:55主平仓、容量型POV、撤单交接
和收盘看门狗；停牌、跌停无买盘、QMT断线或券商拒单仍可能造成无法成交。

## 三、回测结果与风险

冻结窗口为2024-05-20～2026-05-14，共481个信号日。v3严格as-of重建得到105个
候选日，其中102个按固定开盘规则可买、3个开盘封涨停不可买；放入统一账户时间线
并计入对其他策略的占用后实际入选32笔。

| 指标 | N入选交易 | 完整组合 |
|---|---:|---:|
| 样本数 | 32 | 173 |
| 胜率 | 59.38% | 68.21% |
| 平均账户收益 | +1.3235% | +5.6811% |
| 中位数账户收益 | +1.1044% | +2.6114% |
| 逐笔复利 | 1.432724倍 | 5813.315346倍 |
| 最大回撤 | -20.6996% | -22.3862% |
| 最大盈利 | +14.6886% | +52.4368% |
| 最大亏损 | -9.0586% | -13.8239% |
| 最大连续亏损 | 3 | 4 |

训练段18笔N自身复利1.391420，验证段5笔为1.083055；最后测试段9笔仅0.950722。
测试段加入N后的完整组合为3.382735倍，不含N为3.930873倍，比例0.860556；最大
回撤也从-10.4367%恶化为-13.6760%，因此严格样本外非劣门禁没有通过。当前上线
仍属于既有风险接受，不是已经证明长期稳定或不过拟合。必须继续小资金前向验证，
达到至少20笔真实完整平仓样本后再审查；扩大资金前还必须完成容量认证。

旧v2的106候选日、35笔N、2.433396倍N复利和9508.426795倍组合复利含历史成交
打分前视、未前复权收益、不完整涨跌停/止盈/费用口径，已经失效，只保留在
`reports/strategy_n/`和`reports/strategy_n_v2_research/`作历史审计，不能回滚为当前锚点。

## 四、唯一规则源与验证

- `config/config.json`：N开关、参数、仓位和风险接受记录。
- `src/strategy_n.py`：唯一候选筛选与排序实现。
- `scripts/run_strategy_n_signal.py`：每日信号、占用门和不回补逻辑。
- `scripts/build_strategy_n_backtest_pool.py`：从完整历史特征重建N候选。
- `scripts/research_strategy_n.py`：v3研究公共口径；强制as-of、前复权和日期化费用。
- `scripts/strategy_health_monitor.py`：N只统计券商真实完整平仓，不读取候选收益。
- `src/combined_live_engine.py`：把N放在C之后、D之前不存在的最低优先位置。
- `src/live_order_gateway.py`：认证失败只拦截新增BUY，绝不阻断已有持仓SELL。
- `scripts/certify_current_executable_portfolio.py`：六腿逐日单账户认证。

验证命令：

```bash
python3 scripts/build_strategy_n_backtest_pool.py
python3 scripts/research_strategy_n_v2.py --verify-only --output-dir reports/strategy_n_v3_research
python3 scripts/certify_current_executable_portfolio.py
python3 scripts/verify_live_engine_matches_certify.py
python3 -m unittest tests.test_strategy_n tests.test_current_portfolio_runtime
```

验证必须得到105个N候选日（102可买、3个开盘涨停不可买）、组合入选N=32、组合
总计173笔，并逐笔对齐5813.315346倍与-22.3862%最大回撤；认证文件、代码/配置哈希
或发布冻结任一不一致时，只拒绝真实新增BUY，已有持仓SELL链路继续运行。
