# 策略 N：双分支最低优先级补位腿

> 状态：已按用户明确风险接受接入当前实盘；正式优先级为
> `D>A>M>E>C>N`，N只使用前五腿都未占用后的单账户空闲资金。

## 一、策略目标与数据

N用于补充当前组合空仓日，不改变D、A、M、E、C任何一腿的候选规则。信号只读取
T日收盘后已存在的完整涨停池、成交可靠性、成交概率、市场分段高度和退潮状态，
不使用T+1价格、未来成交或未来收益字段。

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
执行规则：T+1开盘买入，T+2收盘退出，目标仓位82.5%
优先级：D>A>M>E>C>N
```

真实买入继续经过统一账户空仓、现金、85%单票硬顶、涨停不可买、停牌、重复委托、
实时行情与订单网关检查。到期卖出复用A/C/E/M的14:55主平仓、容量型POV、撤单交接
和收盘看门狗；停牌、跌停无买盘、QMT断线或券商拒单仍可能造成无法成交。

## 三、回测结果与风险

冻结窗口为2024-05-20～2026-05-14，共481个信号日。N双分支产生106个候选日，
放入统一账户时间线并计入对其他策略的占用后实际入选35笔。

| 指标 | N入选交易 | 完整组合 |
|---|---:|---:|
| 样本数 | 35 | 174 |
| 胜率 | 57.14% | 66.09% |
| 平均账户收益 | +3.1026% | +6.0084% |
| 中位数账户收益 | +1.0811% | +2.6780% |
| 逐笔复利 | 2.433396倍 | 9508.426795倍 |
| 最大回撤 | -26.9447% | -22.4806% |
| 最大盈利 | +42.4614% | +52.3692% |
| 最大亏损 | -9.4096% | -13.8583% |
| 最大连续亏损 | 3 | 4 |

双分支全样本指标提高，但最后测试段的补充分支7笔复利只有0.943674倍，并使测试段
完整组合降至单分支N基线的70.55%，因此严格推广门禁没有通过。当前上线依据是用户
明确接受这项统计风险，不是已经证明长期稳定或不过拟合。必须继续小资金前向验证，
补充分支达到至少20笔真实/影子完成样本后再重新审查，扩大资金前还必须完成容量认证。

逐笔机会成本对照：旧155笔中142笔保持不变、13笔因路径改变失去；新路径增加32笔，
净增19笔。失去交易复利1.148014倍，新增交易复利1.535572倍，两者相除1.337590倍，
与`7108.624210 × 1.337590 = 9508.426795`一致。N最终为35笔而不是16+21=37，
因为补充分支持仓挡住了原N的2笔。

## 四、唯一规则源与验证

- `config/config.json`：N开关、参数、仓位和风险接受记录。
- `src/strategy_n.py`：唯一候选筛选与排序实现。
- `scripts/run_strategy_n_signal.py`：每日信号、占用门和不回补逻辑。
- `scripts/build_strategy_n_backtest_pool.py`：从完整历史特征重建N候选。
- `src/combined_live_engine.py`：把N放在C之后、D之前不存在的最低优先位置。
- `scripts/certify_current_executable_portfolio.py`：六腿逐日单账户认证。

验证命令：

```bash
python3 scripts/build_strategy_n_backtest_pool.py
python3 scripts/certify_current_executable_portfolio.py
python3 scripts/verify_live_engine_matches_certify.py
python3 -m unittest tests.test_strategy_n tests.test_current_portfolio_runtime
```

验证必须得到106个N候选日、组合入选N=35、组合总计174笔，并逐笔对齐
9508.426795倍与-22.4806%最大回撤；任一项不一致时认证门禁拒绝真实新单。
