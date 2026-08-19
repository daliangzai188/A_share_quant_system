# 策略 N：低高度退潮补位腿

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
市场状态：segment_limit_max_height_bucket = 1
          segment_retreat_state_bucket ∈ {retreat_weak, retreat_2day}
每日排序：first_time_minutes升序 → circ_mv升序 → ts_code升序
候选数量：只取第一名；不可买或被上游占用时不回补第二名
执行规则：T+1开盘买入，T+2收盘退出，目标仓位82.5%
优先级：D>A>M>E>C>N
```

真实买入继续经过统一账户空仓、现金、85%单票硬顶、涨停不可买、停牌、重复委托、
实时行情与订单网关检查。到期卖出复用A/C/E/M的14:55主平仓、容量型POV、撤单交接
和收盘看门狗；停牌、跌停无买盘、QMT断线或券商拒单仍可能造成无法成交。

## 三、回测结果与风险

冻结窗口为2024-05-20～2026-05-14，共481个信号日。N完整规则产生46个候选日，
放入统一账户时间线后实际入选16笔。

| 指标 | N入选交易 | 完整组合 |
|---|---:|---:|
| 样本数 | 16 | 155 |
| 胜率 | 43.75% | 67.10% |
| 平均账户收益 | +3.8529% | +6.5384% |
| 中位数账户收益 | -2.4482% | +3.6915% |
| 逐笔复利 | 1.568243倍 | 7108.624210倍 |
| 最大回撤 | -24.9728% | -22.4806% |
| 最大盈利 | +42.4614% | +52.3692% |
| 最大亏损 | -12.0774% | -15.2135% |
| 最大连续亏损 | 5 | 4 |

N的16笔仍是小样本，收益依赖少数右尾盈利；Bootstrap平均收益2.5%分位为-2.95%，
保守统计门禁没有通过。当前上线依据是用户明确接受风险，不是已经证明长期稳定或
不过拟合。必须继续小资金前向验证，达到至少20笔真实/影子完成样本后再重新审查，
扩大资金前还必须完成容量认证。

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

验证必须得到46个N候选日、组合入选N=16、组合总计155笔，并逐笔对齐
7108.624210倍与-22.4806%最大回撤；任一项不一致时认证门禁拒绝真实新单。
