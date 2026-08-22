# 策略D逐笔队列严格重放

最后更新：2026-08-22

## 一、结论

本专项已经完成严格采集入口、统一事件契约、同步全市场信号重放器、价格时间
优先队列重放器和认证闸门；正式D规则、A/E/C规则及当前复利基线均未修改。

当前数据可以继续执行484日全窗口的一分钟路径研究，但还不能把始终封板样本
认证为真实FIFO成交，也不能据此修改正式D。最终L2认证闸门状态仍为：

```text
BLOCKED_HISTORICAL_FULL_MARKET_L2_REQUIRED
```

原因不是代码没有候选，也不是一分钟数据不足以研究，而是当前没有覆盖整个
24个月窗口的全市场历史逐笔委托、逐笔成交和盘口队列。缺这些数据时，程序
只阻止最终FIFO收益认证；分钟价格穿透样本可以继续研究，始终封板样本则按
保守不成交处理，绝不把5分钟或1分钟OHLCV冒充真实排队成交。

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
| Tushare 1分钟 | 40,332/40,336目标完整，共9,720,012根；4只供应商缺口 | 缩小bar内路径歧义 | 否 |
| Tushare官方涨跌停价 | 484/484交易日完整 | 生成全窗口首板触板母池 | 是（母池层） |
| QMT历史1分钟抽样 | 6个日期分位目标中仅最近2个有241根 | 证明本机近期分钟覆盖 | 否 |
| QMT历史tick抽样 | 0/6 | 无 | 否 |
| QMT历史盘口字段抽样 | 0/6 | 无 | 否 |
| QMT沪深京同日抽样 | 3/3有241根1分钟；tick和盘口均0/3 | 证明北交所也只有分钟层可用 | 否 |
| 全市场严格L2日文件 | 0/1,452个交易所日文件 | 情绪、排序、队列、撤单 | 当前缺失 |

窗口共有484个交易日。正式D包含沪、深、京市场，因此严格日文件门槛为：

```text
484日 × SSE/SZSE/BSE = 1,452个交易所日文件
```

QMT探针只调用只读``xtdata``行情接口，没有读取账户、持仓、委托，也没有调用
任何下单或撤单接口。实测结果保存在
``reports/strategy_d_intraday_research/qmt_depth_probe.json``。沪深京同日独立结果保存在
``reports/strategy_d_intraday_research/qmt_three_market_probe.json``。

Tushare历史分钟单次最多8,000行；实测SDK支持逗号分隔多股返回。采集器因此按
交易日横截面聚合，每job最多33只（7,953行），将40,336个股票日目标压缩为
1,446个job，理论下载行数与目标槽位均为9,720,976，放大倍数1.00。聚合响应
漏某只时，只对该股该日精确补取。采集配置按500次/分钟上限使用0.15秒
保守间隔，限频时等待65秒重试；每25个job原子保存状态。
[Tushare历史分钟接口说明](https://tushare.pro/document/1?doc_id=234)

全量结果为40,332个完整目标、9,720,012根，覆盖率99.990083%；分钟键无重复。
剩4只是2024-11-28北交所供应商缺口，没有其他未知缺口。它们留在母样本分母内并
严格fail-closed；因此``path_layer_complete=false``，但
``research_replay_fail_closed_ready=true``。机器报告保存在
``reports/strategy_d_intraday_research/tushare_1m_collection.json``。

上述6,848只次只是56个最终收盘strong日的第一阶段目标，并非484日全窗口。
官方``stk_limit``已补齐其余428日；全窗口40,336只次正式目标清单已经生成，
旧6,848只次逐键完整保留；全窗口分钟采集已达到40,332完整+4供应商缺口失败关闭。

第一阶段一分钟事件账本进一步按``14:00<=第一次可交易回封<14:55``重放，得到370个
路径信号：263个可由信号后板下成交确认限价单成交，107个始终封板且缺队列
证据；370个信号全部仍有至少一个分钟内事件先后歧义。详细统计见
[策略D一分钟路径与爆发/爆亏研究](strategy_d_intraday_1m_research.md)。

## 四、为什么最终FIFO认证仍需要全市场L2

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
| 同上 | ``normalize_event_time`` | 保留``HH:MM:SS.mmm``毫秒，避免破坏逐笔先后顺序 |
| ``scripts/collect_strategy_d_intraday_tushare_1m.py`` | ``load_collection_policy`` / ``build_cross_section_jobs`` / ``fetch_job_with_retry`` / ``write_summary`` | 付费权限限速、同日最多33股横截面聚合、缺股精确补取、断点状态、已知缺口fail-closed和总表合并 |
| ``scripts/collect_strategy_d_stk_limit_history.py`` | ``load_window_open_dates`` / ``normalize_stk_limit`` / ``collect_missing`` | 补齐484日官方涨跌停价、内容校验、原子保存和断点续跑 |
| ``scripts/probe_strategy_d_intraday_qmt_depth.py`` | ``fetch_period`` / ``report_paths`` / ``main`` | 只读探测QMT 1分钟、tick和历史盘口字段；不同批次报告互不覆盖 |
| ``scripts/audit_strategy_d_l2_purchase_readiness.py`` | ``build_audit`` | 汇总当前权限、官方候选来源和付款前样本门槛 |
| ``src/strategy_d_l2_sample_acceptance.py`` | ``validate_sample_package`` | 读取真实供应商样本，校验逐笔序列、委托关联、全市场同步扫描和前50队列 |
| ``scripts/validate_strategy_d_l2_vendor_sample.py`` | ``main`` | 生成付款前样本内容验收报告；九份样本任一缺失即fail-closed |
| ``scripts/audit_strategy_d_strict_intraday_sources.py`` | ``build_audit`` | 汇总各层覆盖并生成正式认证闸门 |
| ``tests/test_strategy_d_strict_intraday.py`` | 全文件 | 队列、部成撤单、同步情绪、排名和缺数fail-closed测试 |

## 七、运行与验证

### 7.1 全窗口Tushare分钟数据验收

```bash
python3 scripts/collect_strategy_d_intraday_tushare_1m.py --dry-run
```

当前必须看到：

```text
target_count=40336
clustered_request_count=1446
access_tier=PAID_A_SHARE_HISTORY_MINUTE
request_limit_per_minute=500
request_interval_seconds=0.15
complete_target_count=40332
pending_target_count=4
known_vendor_gap_target_count=4
unresolved_non_known_gap_target_count=0
merged_bar_count=9720012
research_replay_fail_closed_ready=true
path_layer_complete=false
queue_depth_layer_complete=false
```

``path_layer_complete=false``是对数据完整性的如实表达；不等于停止研究。下一步账本重放保留
全40,336分母，对4只缺数目标不产生信号。

### 7.2 小批采集验证

```bash
PYTHONPATH="$PWD/.venv/lib/python3.9/site-packages" \
python3 scripts/collect_strategy_d_intraday_tushare_1m.py --limit-jobs 5
```

脚本每个job先原子保存独立分钟分片，再保存目标状态。限频和瞬时网络错误按配置
重试；无权限或参数错误立即留证，不会被快速重试掩盖。中断后重复运行只补未完成
目标；只需重建总表时运行``--consolidate-only``，不会请求网络。

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

## 八、当前下一步与最终数据闸门

当前不再购买或申请L2。484日全窗口40,336只次首板触板目标已经生成，
40,332只一分钟完整且余4只供应商缺口已明确fail-closed。下一项是全窗口机械重放：分钟研究中，价格
跌破涨停价可以确认限价委托成交；始终封板且没有队列证据的样本一律按保守
不成交。只有分钟候选先同时改善D独立腿和ACDE逐腿替换研究结果，才重新评估
下述L2样本与报价。

上证信息官方历史产品表列有集合竞价、快照、逐笔成交和K线，但没有公开列出
逐笔委托，所以不能据此认定可重建FIFO。公开价格页的24万元/年非展示自用许可
及6万元/年VDE技术服务属于实时Level-2，并非本项目历史数据报价；原文“历史
Level-2公开价12万元/年”的口径已撤销。
[上交所历史数据](https://www.sseinfo.com/services/assortment/historical/)
[上交所产品服务价格](https://www.sseinfo.com/services/cpfwjg/)

掘金量化文档提供SSE、SZSE从2016-01-04至今的历史十档、逐笔成交、逐笔委托
和委托队列接口，时间跨度符合本项目；但L2只支持券商内网环境，公开接口表也
没有列BSE。因此它只作为未来最终认证的候选来源，当前不申请、不购买。
[掘金量化L2数据说明](https://www.myquant.cn/docs2/tools/L2%E6%95%B0%E6%8D%AE.html)

深交所官方历史增强行情明确包含逐笔委托、逐笔成交、3秒快照和证券委托队列；
北交所股票历史逐笔产品及价格尚未从官方公开页面确认。完整权限审计、联系方式、
样本门槛和询价模板见
``docs/strategy_d_l2_permission_purchase_audit.md``。

本专项没有购买L2、没有联系供应商，也没有启用任何实盘接口。用户已经购买的
Tushare历史一分钟权限是当前D研究主数据，不属于这里所说的缺失L2。未来只有
分钟候选通过双复利研究门槛，才取得沪深京九份真实L2样本并运行
``python3 scripts/validate_strategy_d_l2_vendor_sample.py``；当前结果为``0/9``、
``BLOCKED_NO_VENDOR_SAMPLE_MANIFEST``。在最终认证前不修改
``src/strategy_d_spec.py``或正式配置。
