# 策略D逐笔队列严格重放

最后更新：2026-08-22

## 一、结论

本专项已经完成严格采集入口、统一事件契约、同步全市场信号重放器、价格时间
优先队列重放器和认证闸门；正式D规则、A/E/C规则及当前复利基线均未修改。

当前数据还不能执行正式D复利和ACDE逐腿替换比较。机器审计状态为：

```text
BLOCKED_HISTORICAL_FULL_MARKET_L2_REQUIRED
```

原因不是代码没有候选，而是当前没有覆盖整个24个月窗口的全市场历史逐笔
委托、逐笔成交和盘口队列。缺这些数据时，程序会主动阻止收益认证，绝不把
5分钟或1分钟OHLCV冒充真实排队成交。

## 二、冻结基线

| 项目 | 当前正式值 |
|---|---:|
| 窗口 | ``20240630~20260630`` |
| D独立腿 | 39笔，2.0261239235922566倍 |
| ACDE组合 | 132笔，327.72671897548867倍 |
| ACDE腿数 | D22 / A44 / E49 / C17 |
| 占仓顺序 | D>A>E>C |
| 仓位 | 82.5% |
| 本项修改正式规则 | 否 |

未来只有同时满足以下条件，才允许修改D：

1. 新数据先严格复现当前D；
2. 候选D独立复利高于2.0261239235922566倍；
3. 从327.72671897548867倍当前ACDE基线逐腿替换后，总组合复利也变大；
4. 费用、滑点、涨跌停、T+1、D>A>E>C和82.5%仓位全部不变。

## 三、数据源实测

| 数据层 | 当前覆盖 | 能解决的问题 | 能否正式认证 |
|---|---:|---|---|
| BaoStock 5分钟 | 6,848/6,848目标已终态 | 近似筛查明显炸板/回封路径 | 否 |
| Tushare 1分钟 | 真实接口可返回241根/日；完整目标0/6,848 | 缩小bar内路径歧义 | 否 |
| QMT历史1分钟抽样 | 6个日期分位目标中仅最近2个有241根 | 证明本机近期分钟覆盖 | 否 |
| QMT历史tick抽样 | 0/6 | 无 | 否 |
| QMT历史盘口字段抽样 | 0/6 | 无 | 否 |
| 全市场严格L2日文件 | 0/1,452个交易所日文件 | 情绪、排序、队列、撤单 | 当前缺失 |

窗口共有484个交易日。正式D包含沪、深、京市场，因此严格日文件门槛为：

```text
484日 × SSE/SZSE/BSE = 1,452个交易所日文件
```

QMT探针只调用只读``xtdata``行情接口，没有读取账户、持仓、委托，也没有调用
任何下单或撤单接口。实测结果保存在
``reports/strategy_d_intraday_research/qmt_depth_probe.json``。

Tushare历史分钟接口一次只支持单一股票、单次最多8,000行；采集器已把同一
股票最多33个连续交易日跨度合并成一个请求，将6,848个股票日目标压缩为5,270
个请求。当前项目Token于2026-08-22真实返回``1次/小时``限频，按3605秒间隔
仍需约5,277.32小时，且一分钟K本身没有队列，因此不作为当前完整方案。
[Tushare历史分钟接口说明](https://tushare.pro/document/1?doc_id=234)

## 四、为什么必须是全市场L2

### 4.1 盘中情绪不是收盘情绪

正式监控器的``sealed_ever_count``名称容易误解，但其实际逻辑是：

```text
信号扫描当下仍封在涨停的股票数量
```

炸板打开的股票不计，重新封板后再计。当前历史母池只包含最终收盘被分类为
``strong``的56日；它不能证明其他交易日在14:00~14:55之间是否曾经进入
实时允许区间88~132只。因此不能只买56日候选文件来倒推盘中情绪，必须覆盖
窗口内每个交易日的全市场同步快照。

### 4.2 排名使用信号当时盘口

同一轮通过条件的D候选按下列顺序选择：

```text
炸板2次优先
    ↓
信号当时买一封单金额 / as-of流通市值降序
    ↓
股票代码稳定排序
```

收盘封单、最终炸板次数和最终收盘涨停池都不能替代信号当时数据。

### 4.3 排队成交是反事实委托

涨停价不再打开时，只看后续涨停价成交量仍不够。严格重放需要：

1. 从09:30前完整逐笔委托流建立涨停买一FIFO队列；
2. 在第一可交易回封事件之后插入按当时账户权益计算、按100股取整的虚拟委托；
3. 按原始sequence处理前方委托的成交和撤单；
4. 虚拟单排到后，才累计自身成交量；
5. 14:55撤销剩余数量，区分全成、部成和未成。

普通分钟K、周期盘口快照或只有逐笔成交而没有逐笔委托，都无法完整执行这一步。

## 五、统一数据契约

### 5.1 标准逐笔事件

最低字段：

```text
trade_date, ts_code, event_time, sequence, event_type,
price, volume, side, order_id
```

``event_type``只允许：

```text
ORDER_ADD, ORDER_CANCEL, TRADE, BOOK_SNAPSHOT
```

数量单位必须为股（``SHARE``），时间至少精确到秒并保留原始事件顺序。日清单
必须声明：全市场、序列完整、含委托/成交/快照、09:30前开始、至少覆盖14:55。

### 5.2 同步D扫描快照

完整L2事件要按正式监控频率重采样为同步全市场扫描。每个``scan_id``必须包含
完全相同的股票宇宙；任何一轮少一只都会整日fail-closed。扫描字段还包括：

```text
limit_price, last_price, bid_volume_1, circ_mv,
previous_day_limit_up, historical_st, market_segment,
fill_probability, fill_reliable
```

其中涨停价、历史ST、昨日涨停和流通市值必须严格as-of；成交概率必须用信号
时点封单和当时机械复利账户计划金额重新计算，不能使用收盘字段。

## 六、代码落地

| 文件 | 方法/入口 | 修改内容 |
|---|---|---|
| ``src/strategy_d_strict_intraday.py`` | ``strict_l2_manifest_gate`` | 检查全窗口、全交易所、全市场L2日文件完整性 |
| 同上 | ``replay_synchronized_d_scans`` | 重建首次封板、炸板、回封、当前封板情绪和同日D排序 |
| 同上 | ``replay_price_time_queue`` | 重建价格时间优先队列、部分成交和14:55撤余单 |
| ``scripts/collect_strategy_d_intraday_tushare_1m.py`` | ``build_cluster_jobs`` / ``main`` | 33交易日聚合请求、断点状态、分钟价格路径回填 |
| ``scripts/probe_strategy_d_intraday_qmt_depth.py`` | ``fetch_period`` / ``main`` | 只读探测QMT 1分钟、tick和历史盘口字段 |
| ``scripts/audit_strategy_d_strict_intraday_sources.py`` | ``build_audit`` | 汇总各层覆盖并生成正式认证闸门 |
| ``tests/test_strategy_d_strict_intraday.py`` | 全文件 | 队列、部成撤单、同步情绪、排名和缺数fail-closed测试 |

## 七、运行与验证

### 7.1 只看Tushare工作量

```bash
python3 scripts/collect_strategy_d_intraday_tushare_1m.py --dry-run
```

当前必须看到：

```text
target_count=6848
clustered_request_count=5270
request_interval_seconds=3605
estimated_hours_at_current_rate=5277.32
queue_depth_layer_complete=false
```

### 7.2 小批采集验证

```bash
python3 scripts/collect_strategy_d_intraday_tushare_1m.py --limit-jobs 1
```

脚本每个job后都会原子保存目标状态。当前Token若仍在一小时限频窗内，会记录
``RETRYABLE_ERROR``，下次运行继续请求，不会把失败目标写成已完成。

### 7.3 数据源总审计

```bash
python3 scripts/audit_strategy_d_strict_intraday_sources.py
```

当前必须输出：

```text
expected_file_count=1452
complete_file_count=0
status=BLOCKED_HISTORICAL_FULL_MARKET_L2_REQUIRED
current_d_reproduction_allowed=false
acde_one_leg_replacement_allowed=false
```

### 7.4 测试

```bash
python3 -m pytest -q \
  tests/test_strategy_d_strict_intraday.py \
  tests/test_strategy_d_intraday_ledger.py
```

测试覆盖：同信号时点真实封单排在虚拟单前、队列全成、部成后14:55撤余、
数据不完整fail-closed、全市场同步扫描、实时情绪、同日排名和分钟请求聚合上限。

## 八、下一步需要的数据权限

上交所官方历史Level-2产品明确包含快照、逐笔委托/成交和分钟K；公开价格页显示
上交所历史Level-2为12万元/年，且这只解决上海市场，不能自动补齐深交所和北交所。
[上交所Level-2产品说明](https://www.sseinfo.com/services/assortment/level2/)
[上交所产品服务价格](https://www.sseinfo.com/services/cpfwjg/)

掘金量化文档提供历史L2 tick、逐笔成交、逐笔委托和委托队列接口，但实际历史
跨度、交易所覆盖和券商版本权限必须用现有账号逐项验证，不能只凭接口名称假设
两年数据可取。[掘金量化历史L2接口](https://www.myquant.cn/docs/python/python_other_api)

本专项没有购买数据、没有联系供应商，也没有启用任何实盘接口。下一步必须先
确认已有数据权限或选定合规数据来源，再导入统一契约；在此之前不运行D/ACDE
收益比较，也不修改``src/strategy_d_spec.py``或正式配置。
