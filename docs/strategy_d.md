# 策略 D：首板打板设计文档

> 2026-07-22：策略 B 已删除。本文旧回测表中的 A+B+C 组合只保留历史审计价值；当前资金关系按 A/C/D/E 重新计算，历史 B 仓仅人工退出时阻断 D 新开仓。

## 一、策略定位

策略 D（首板打板）当前叠加在 A/C 框架之上。

**核心逻辑**：当市场处于强势情绪时，买入当日第一次涨停（首板）且曾炸板后重新封板（multi_open）的股票，在涨停价排队买入，**T+2 收盘卖出**。
（2026-08-07 前为「次日开盘集合竞价卖出/接力让路」，随 D 接力全关废止，见第十三节。）

**与 A/C 的关系**：D 在盘中执行，A/C 在收盘后生成信号、次日开盘执行；真实持仓占用优先于理论时序。仅人工退出的历史 B 仓存在时，D必须跳过。

---

## 二、触发条件

### 2.1 股票筛选条件

| 条件 | 说明 |
|---|---|
| `limit_times == 1` | 首板（当日第一次涨停，非连板） |
| 不在昨日涨停池 | 排除昨日已涨停的股票（连板进入 A/B/C 策略） |
| `open_times >= 2` | `multi_open`板型；炸板1次属于`t_board`，不在D核心回测样本内 |
| `open_times <= 3` | 炸板次数不超过 3 次（过多炸板说明情绪不稳） |
| 当前处于涨停封板状态 | `last_price == upper_limit` |

### 2.2 市场情绪条件

| 情绪等级 | 全市场今日涨停累计数 | D 策略是否触发 |
|---|---:|---|
| weak | < 50 | 不触发 |
| neutral | 50–99 | 不触发 |
| **strong** | **100–150** | **触发** |
| very_strong | > 150 | 不触发（不在历史D样本） |

**只在 strong 市场情绪下触发，不包含 very_strong**，与回测
`market_sentiment_level == "strong"`严格一致。盘中使用当前封板88～132只作为同桶代理，
代理范围仍需用分钟快照持续校准。

### 2.3 两档信号时序

```
09:30  开盘
  │
09:35  WATCH 窗口开启
  │    → 检测到符合条件的首板 multi_open 回封
  │    → 发出 [WATCH] 提醒，记录关注名单
  │    → 继续追踪该股票（WATCH只提醒，不能直接升级买入）
  │
14:00  BUY 窗口开启
  │    仅当14:00后发生真实回封 → 发出 [BUY] 信号
  │    → 在涨停价挂限价买单
  │
14:55  撤单
  │    → 调用 cancel_order_stock 撤销所有未成交的 D 委托
  │
15:00  全天结束
```

**为什么等到 14:00**：尾盘封板（14:00+）历史成功率更高，炸板概率更低，是 D 策略的核心过滤时间节点。

---

## 三、资金模型与 ABC 冲突分析

> **2026-08-07 修订（commit 54a66c1）**：本节 3.1/3.2 原先描述的是「D 在 T+1 开盘
> 卖出、同日 ABC 用同一笔资金买入」的顺序用款模型。该模型已随 **D 接力全关** 废止：
> D 一律 T+2 收盘卖出，**平仓确认后的下一个信号日**才轮到别的腿开仓，不存在同日
> 卖D-买候选的资金接续。下表 `HISTORICAL_SIM_FILLED` 行已按新规则改写，3.2 的
> 时序图保留为历史对照。

### 3.1 四种日状态与 D 的关系

| ABC 状态 | 含义 | D 是否执行 | 原因 |
|---|---|---|---|
| `NO_CANDIDATE` | ABC 无信号，账户空仓 | **执行** | 资金完全可用 |
| `HISTORICAL_SIM_FILLED` | ABC 生成信号，T+1 开盘买入 | **执行** | D 盘中先成交即先占用资金；D 走 T+2 收盘平仓，当日 ABC 计划单因资金被占而作废（2026-08-07 起不再 T+1 让路卖出） |
| `BUY_REJECTED` | ABC 信号被风控拒绝，账户空仓 | **执行** | 实际账户空仓，D 可独立使用资金 |
| `POSITION_OCCUPIED_SKIP` | ABC 有旧持仓占用 80% 资金 | **跳过** | 资金真实冲突；且 D 在此情况下实测胜率仅 20%（均收益 -2%） |

### 3.2 顺序使用同一资金的完整流程（历史，2026-08-07 已废止）

```
T 日（D 执行）
  14:xx  检测到首板 multi_open 重封，满足 strong 情绪
  14:xx  在涨停价挂单买入（80% 仓位）

T+1 日（D 卖出 + ABC 买入）
  09:25  集合竞价成交，D 仓位卖出（次日开盘溢价）
  09:25  同时或稍后：ABC 计划单买入
         （D 卖出释放资金 → ABC 立即可用同一笔资金）
```

两者在 T+1 开盘同时操作，但 D 是卖出（释放资金），ABC 是买入（使用资金），资金流方向互补，账户层面顺序完成，不需要额外保证金。

---

## 四、当前严格锚点回放结果（2026-08-21）

### 4.1 D 单腿信号池

输入为`data/processed/limit_up_fill_scored_asof.csv`，窗口为
2024-06-30～2026-06-30。每日只取D排序第一名，仓位82.5%，成交压力折扣80%，
费用、滑点、涨跌停及跌停延期卖出与组合认证共用同一口径，基准退出为T+2收盘。

| 指标 | 数值 |
|---|---:|
| 独立单账户成交数 | 39笔 |
| 胜率 | 53.85% |
| 平均账户收益 | +2.1665% |
| 中位账户收益 | +1.0783% |
| 独立单账户机械复利 | 2.026124倍 |
| 独立单账户最大回撤 | -31.6484% |
| 盈亏比 | 1.9355 |
| 最大单笔账户盈利 | +45.2795% |
| 最大单笔账户亏损 | -7.4178% |
| 最大连续亏损 | 4笔 |

机械复利按历史每笔连续复投计算，不代表真实容量或未来收益。D本轮没有排序变体同时
提高D独立腿复利和ACDE组合复利，因此规则保持不变。未做单账户占仓去重的候选池为
53笔、3.156594倍，只作信号池诊断，不能写成独立策略复利。

### 4.2 历史分年表的口径状态

旧分年表属于2024-05-20～2026-05-14非当前锚点，不再引用。新窗口分年和分段数据必须从
`reports/current_portfolio_alignment/strict_asof_audit.json`及同版本候选明细重新生成，
不能将两个窗口的年度数据混在一张表中。

### 4.3 验证闸门

本轮只完成新锚点下的严格as-of发现和机械成交回放。分钟级成交、盘口排队、容量、
样本外和模拟盘尚未取得发布认证，因此`release_eligible=false`，新增BUY继续fail-closed。
候选池与独立单账户指标分开保存，避免把重叠持仓的候选逐笔相乘误当成可执行收益。

### 4.4 当前完整组合认证

当前严格串行腿序为 `D > A > E > C`；
组合132笔、胜率68.18%、平均账户收益4.9120%、中位数2.3043%、最大回撤-22.9705%，
单账户机械复利为327.726719倍。该数值仍未通过真实容量和样本外认证，不能作为实盘收益预期。
当前四腿发布认证尚未通过；旧认证不得继续授权新BUY，这不代表D样本门槛已经通过。

---

## 五、盘中监控实现

### 5.1 文件

```
scripts/monitor_strategy_d_intraday.py
```

### 5.2 关键参数

| 参数 | 值 | 说明 |
|---|---|---|
| `SENTIMENT_STRONG_MIN` | 88 | 14:00实时封板数阈值；校准对应历史收盘涨停数约100只的strong环境 |
| `SENTIMENT_STRONG_MAX` | 132 | strong代理上界；超过后按very_strong代理排除 |
| `D_MIN_OPEN_TIMES` | 2 | multi_open下限；炸板1次不得进入实盘D |
| `D_MAX_OPEN_TIMES` | 3 | 最大允许炸板次数 |
| `D_PREFERRED_OPEN_TIMES` | 2 | 候选排序先优先炸板2次，再比较封单金额/流通市值 |
| `WATCH_START_HHMM` | 935 | WATCH 信号开始时间 |
| `SIGNAL_START_HHMM` | 1400 | BUY 信号开始时间 |
| `CANCEL_HHMM` | 1455 | 自动撤单时间 |
| `POLL_BATCH_SIZE` | 500 | 每批 get_full_tick 数量 |
| `POLL_INTERVAL_SEC` | 30 | 轮询间隔（秒） |
| `D_POSITION_PCT` | 0.825 | 普通空仓日目标仓位82.5%，仍受85%单票硬顶 |

### 5.3 股票状态跟踪（StockState）

```python
@dataclass
class StockState:
    ts_code: str
    name: str
    upper_limit: float
    was_sealed: bool       # 上次轮询时是否涨停
    ever_sealed: bool      # 今日曾涨停过
    open_times_today: int  # 今日炸板次数
    first_seal_hhmm: int   # 首次封板时间
    last_seal_hhmm: int    # 最近封板时间（每次重封更新）
    watch_alerted: bool    # WATCH 提醒已发出
    buy_signaled: bool     # BUY 信号已发出
    order_id: str          # 已提交委托的 order_id
```

### 5.4 主循环逻辑

```
启动时：
  1. 检查 data/processed/positions.json → 若 ABC 有 open 持仓 → 跳过 D，退出

轮询循环（30s 一轮，每批 500 只）：
  2. 从 data/raw/limit_list/最新.csv 加载昨日涨停码（排除连板）
  3. 从 data/raw/daily/最新.csv 加载全股票宇宙（约 5512 只）
  4. get_full_tick → 更新每只股票的 StockState
     - 当前价 == 涨停价 → was_sealed = True → 若之前炸板，open_times++
     - 当前价 < 涨停价 且之前封板 → 炸板，was_sealed = False
 5. _check_and_fire()：
     if hhmm >= 1400:
         if st.last_seal_hhmm >= 1400 and 成交概率>=80%且可靠 → 发 BUY 信号
     elif hhmm >= 1000 and not st.watch_alerted:
         → 发 WATCH 提醒
 6. 同一时刻有多只候选：先选炸板2次，再按封单金额/流通市值降序，只尝试第一名
 7. 14:55 → cancel_all_d_orders()，脚本退出
```

`open_times`必须在2~3之间。该排序与回测候选选择一致；若第一名下单失败，不递补第二名，
避免回测只记录第一名、实盘却在失败后换票造成收益口径漂移。

若D监控在09:30以后才启动（包括盘中重启程序），当天按fail-closed禁止D开仓，避免
缺失早盘首次封板/炸板路径后错误重建候选。

### 5.5 情绪计算方式

- 每轮轮询统计全市场**今日曾涨停过**（`ever_sealed == True`）的股票总数
- 数量 ≥ 100 → `strong`，满足触发条件
- 不依赖收盘数据，纯盘中实时估算

### 5.6 运行方式

```bash
# 仅提醒，不下单
python scripts/monitor_strategy_d_intraday.py

# 实盘下单（需 QMT 已连接，config 中 qmt_enabled=true）
python scripts/monitor_strategy_d_intraday.py --live-order

# 打印配置后退出（调试用）
python scripts/monitor_strategy_d_intraday.py --dry-run
```

输出：
```
logs/strategy_d_monitor_YYYYMMDD.log
reports/strategy_d/intraday_signals_YYYYMMDD.csv
```

---

## 六、守护进程集成

### 6.1 触发时间

守护进程（`scripts/trading_daemon.py`）在09:20组合状态检查允许D时，以后台线程启动
D监控；监控从09:30开始建立完整日内路径。09:30以后才启动无法复原早盘封板/炸板
历史，因此当天fail-closed禁止D新开仓。

```python
09:20 组合状态允许D → 启动后台线程
09:30 全市场实时路径跟踪
09:35 WATCH只提醒
14:00 后发生真实回封且全部门禁通过 → BUY
14:55 撤销D未成交委托
```

### 6.2 非阻塞启动原因

D监控在守护进程后台线程中运行，并通过`SharedQMTBrokerProxy`复用主进程唯一QMT连接；
不再启动独立QMT子进程，避免多个会话争抢miniQMT，同时不阻塞平仓和收盘流水线。

### 6.3 日程全览

| 时间 | 任务 | 是否阻塞守护进程 |
|---|---|---|
| 09:20 | 平仓检查 + 复核组合状态 + 有开仓计划时预挂买单（job_morning → job_premarket_buy）| 是 |
| 09:20 | 组合允许时启动D后台线程，09:30开始完整扫描 | 否（线程，复用主QMT连接） |
| 14:56 | 盘中收盘平仓 + 14:57 撤未成交买单（job_afternoon）| 是 |
| 15:10 | 收盘流水线 + A/C/E 信号（job_post_market）| 是 |

---

## 七、撤单机制

### 7.1 接口层

`src/broker_adapter.py` 新增抽象接口：

```python
@abstractmethod
def cancel_order(self, order_id: str) -> bool:
    """撤销委托。返回 True 表示撤单请求已提交（不代表最终成功）。"""
```

`src/qmt_adapter.py` 实现：

```python
def cancel_order(self, order_id: str) -> bool:
    result = self.trader.cancel_order_stock(self.account, int(order_id))
    return result not in {None, -1}
```

### 7.2 14:55 自动撤单流程

```
1. 查询当日所有委托（query_orders）
2. 筛选 remark == "D_FIRST_BOARD" 且状态为"未成交"的委托
3. 逐个调用 cancel_order(order_id)
4. 记录撤单结果到日志
5. 脚本退出
```

---

## 八、ABC 持仓检测逻辑

启动时读取`data/processed/positions.json`，识别所有`open/sell_pending`策略仓，再用券商
实际持仓确认。只要A/C/D/E/N任一策略仓尚未真实清空，D都跳过；打新和人工仓
不参与策略占仓判断。券商查询失败或账本损坏时按fail-closed禁止D开仓。

---

## 九、回测脚本说明

文件：`scripts/backtest_strategy_d.py`

关键常量：

```python
# D 可触发的 ABC 状态集合
D_ELIGIBLE_STATUSES = {"NO_CANDIDATE", "HISTORICAL_SIM_FILLED", "BUY_REJECTED"}
# POSITION_OCCUPIED_SKIP 排除原因：资金冲突 + 实测胜率 20%（均收益 -2%）
```

回测逻辑：
1. 只读取配置指定的冻结近2年输入，不允许命令行切换数据源或放宽门槛。
2. 候选必须同时满足：strong但非very_strong、首板、非ST、multi_open、炸板2～3次、
   首次封板不早于10:00、最后回封不早于14:00、成交概率≥80%且可靠、市场分段获准。
3. 每个信号日优先炸板2次，再按封单金额/流通市值降序，最后按股票代码稳定排序，
   只取第一名且失败不递补。
4. D全部按T+2收盘退出，仓位82.5%，成交压力折扣80%，往返费用及滑点0.15%。
5. 本脚本中的旧A/B/C叠加曲线只保留审计用途；当前组合收益和串行占仓只能以
   `scripts/certify_current_executable_portfolio.py`只作身份核对；发布标尺只认
   `scripts/certify_strict_asof_portfolio.py`。

输出：
```
reports/strategy_d/backtest_summary.csv   总体指标对比
reports/strategy_d/d_trades.csv           D 策略逐笔明细
reports/strategy_d/equity_curve.csv       逐日净值曲线
reports/strategy_d/yearly_comparison.csv  年度对比
```

运行：

```bash
cd /Users/user/Desktop/A_System
.venv/bin/python -B scripts/backtest_strategy_d.py
```

---

## 十、核心文件职责

| 文件 | 核心内容 |
|---|---|
| `src/strategy_d_spec.py` | 回测与实盘共用的D不可变候选边界和排序 |
| `scripts/backtest_strategy_d.py` | 冻结D候选、T+2收益及验证闸门 |
| `scripts/monitor_strategy_d_intraday.py` | 09:30全日路径、WATCH/BUY、成交概率和撤单 |
| `scripts/trading_daemon.py` | 组合状态允许时启动D线程，复用主QMT连接 |
| `scripts/certify_current_executable_portfolio.py` | 旧来源串行身份核对，不生成正式收益认证 |
| `scripts/certify_strict_asof_portfolio.py` | 严格as-of单账户机械复利和代码/输入指纹认证 |
| `src/broker_adapter.py`、`src/qmt_adapter.py` | 委托、成交确认和撤单接口 |

---

## 十一、当前状态与限制

| 项目 | 状态 |
|---|---|
| 回测完成 | ✅ 36笔D信号，全部T+2收盘；样本数36<50，闸门仍FAIL |
| 守护进程集成 | ✅ 09:20允许时启动、09:30完整跟踪；晚启动fail-closed |
| 两档信号逻辑 | ✅ WATCH只提醒；14:00后真实回封才可BUY |
| 14:55 自动撤单 | ✅ cancel_order 已实现 |
| QMT 实盘下单 | ✅ 仍须先小资金核验，不可按机械复利放大 |
| 成交概率门 | ✅ 与回测同模型实时复算，≥80%且可靠；失败即阻断 |
| 情绪口径 | ⚠️ 历史为收盘100～150只，盘中用当前封板88～132只代理，需持续校准 |

---

## 十二、普通D按容量分流卖出（执行层已接入）

### 12.1 为什么不能直接把全部D改成POV

当前组合中的D全部按T+2收盘退出，D接力已经关闭。若不分金额全部提前卖，
可能因日内价格路径损失而偏离收盘价；若仓位过大又全部挤在14:55，可能产生
冲击成本。因此普通D只研究“**容量不足时才触发**”的分档卖出，不做无条件POV。

当前执行层规则已经接入：

1. `planned_exit_date=今日`的普通D允许进入容量检查；
2. 13:00仓位市值不超过当时累计成交额1%时，不启动POV，继续14:55平仓；
3. 超过1%才建立5分钟拆卖跑道，14:30和14:45还会按实际流速复核；
4. 普通D不使用“仓位达到950万元就固定14:15启动”的绝对金额规则，避免流动性
   充足时被无谓提前卖出；
5. 14:53撤销未完成POV委托，14:55只按QMT实际余仓继续平仓；
6. 选股、打板买入、持有期和T+2收盘回测收益口径均未修改。

### 12.2 新增研究工具

| 文件 | 作用 |
|---|---|
| `scripts/research_strategy_d_exit_fetch.py` | 从当前组合账本提取17笔实际执行D，采集退出日全日5分钟和尾盘1分钟行情 |
| `scripts/research_strategy_d_exit_pov.py` | 回放动态起点、14:30起点、14:45起点三种卖出方案，并复算D腿及完整组合 |
| `tests/test_strategy_d_exit_pov_research.py` | 验证样本隔离、无冲击时保持基准复利、分钟数据不完整时拒绝认证 |

Windows盘后运行：

```powershell
cd C:\A_System
py -3.11 scripts\research_strategy_d_exit_fetch.py --dry-run
py -3.11 scripts\research_strategy_d_exit_fetch.py
py -3.11 scripts\research_strategy_d_exit_pov.py
```

### 12.3 参数研究与后续门禁
> **2026-08-07 D接力全关（commit 54a66c1）**：D 不再在 T+1 09:23 卖竞价安全部分、
> 09:30 后按「卖D一片→买候选一片」成对POV接力给 A/C/E，一律走自己的 T+2 收盘
> 平仓，平仓确认后的下一个信号日才轮到别的腿。本文 13.2/13.3 节的接力容量研究
> 随之失去研究对象（`EXPECTED_RELAY_COUNT=0`，对应测试类整体 skip）；若将来重开
> 接力，把该常量改回实际笔数即可恢复。当前腿序为 **D>A>E>C**；当前四腿严格
> 发布认证尚未完成，新BUY保持fail-closed。


当前只复用了已有的1%容量门禁，没有根据普通D样本另行调参。后续若要修改D专属
触发比例、参与率或启动时间，研究结果必须同时满足以下条件：

1. 18笔普通D均有48根5分钟bar和16根尾盘1分钟bar；
2. 所有18笔在对应仓位金额下均能卖完，残仓不得按收盘价虚构成交；
3. D全样本复利不低于原普通D基准；
4. 按时间切分后的开发段与验证段都不劣于原基准；
5. 替换D退出价后，完整154笔严格研究组合复利不低于664.315543倍；
6. 完整组合最大回撤不劣于-22.353794%；
7. 同时查看成交均价代理和bar最低价压力代理，不能只看乐观结果。

> **当前基准**：当前唯一口径为D>A>E>C；D认证输入
> 必须先保留45个未被旧组合占仓裁剪的逐日第一名，再由当前统一账户时间线判断
> 是否占仓。旧4445.281570倍误用了旧A/B/C路径裁剪后的D候选，已停止使用。
> 旧分钟样本必须按当前17笔补齐后，才可重新出具容量结论。

分钟K成交额不等于Level-2真实买盘深度，因此研究会对容量再打折。样本仅17笔，
它只能用来排除明显不合格方案，不能保证未来收益。在D专属参数通过门禁前，保持
当前1%容量阈值不变；仍须先用小资金观察实盘成交均价与收盘价的偏差。

---

## 十三、D接力集合竞价容量与成对POV（~~实盘路径已接入~~ **2026-08-07 整节作废**）

> **本节全部内容已停止生效（commit 54a66c1）。** D 接力已全关：D 一律走自己的
> T+2 收盘平仓，不再在 T+1 09:23 卖竞价安全部分、也不再做「卖D一片→买候选一片」
> 的成对POV；D 平仓确认后的下一个信号日才轮到别的腿开仓。
>
> **关闭依据（同折扣口径重算）**：把接力D也按与T+2 D相同的80%成交折扣和冲击口径
> 计费后，接力只值 +7.8%（不是未折扣口径下看到的 +18.3%），而它换来的是
> 「D未卖完就买新票」的资金穿仓风险和一条只有9笔样本支撑的执行链路；
> 当前发布标尺必须以重新生成的D>A>E>C严格认证为准；旧组合认证已经失效。
> **151笔 / 27870.307776倍 / -23.50% / 胜率68.87%**，两项都不劣于开接力口径。
> （该对比完成后又做了M成交口径对齐；2026-08-10用户明确接受M未通过分段回撤
> 非劣门禁的风险并恢复M；该150笔/29388.980134倍是当时历史标尺。2026-08-18
> 后续组合结构调整后，旧标尺仅保留历史审计，不得继续用于发布。
> 接力全关结论不受影响。）
>
> 因此 13.1 的七条执行规则、13.2 的接力笔数与冲击敏感表、13.3 的研究工具
> **均只保留历史审计价值**：`research_strategy_d_relay_fetch.EXPECTED_RELAY_COUNT`
> 已置 0、返回空表，`tests/test_strategy_d_relay_research.py` 对应用例整体 skip。
> 将来若重开接力，把该常量改回实际笔数即可让这套研究链路复活。

### 13.1 本轮结论与边界（历史，已停止生效）

第12节旧版“接力D必须整仓集合竞价卖出”已废止。当前实盘执行规则是：

1. 小资金D整仓不超过09:23虚拟匹配量安全比例时，保留整仓竞价接力；
2. 大资金D禁止继续09:23整仓跌停价卖，只让安全部分参与竞价；
3. 剩余D在09:30后按真实成交先卖，确认资金释放后再买A/C/E，形成资金中性的
   成对POV；
4. 09:23盘口无效、卖方未匹配量过大或字段不完整时，竞价安全卖量直接为0；
   09:30后仍须同时通过D买盘深度、D成交流量和候选承接流量才卖下一片；
5. 每片候选累计买入额不得超过D累计确认卖出额，账户原有空闲现金不参与接力；
6. 目标仓位82.5%、单票85%硬顶、QMT真实可用余额和开盘价+2%追价线同时生效；
7. D与候选同票时直接切换策略归属，不做无意义的卖出再买回；
8. 状态落在`data/state/d_relay_pair_state.json`，逐片实盘结果落在
   `reports/d_relay_pair_execution_log.csv`，daemon重启后从未知委托终态继续恢复。

上海证券交易所的行情接口规则明确：集合竞价时买一和卖一价格同时表示虚拟开盘
参考价，买一量和卖一量表示虚拟匹配量，买二量/卖二量分别表示买方/卖方未匹配量。
研究脚本据此只读取09:23及以前已经发布的快照，不用09:25真实开盘结果倒推决策。

### 13.2 锁定样本与收益冲击（历史，接力笔数现为0）

当前139笔可执行组合中共有9笔D接力：D→A 1笔、D→C 8笔、D→E 0笔。9笔
接力合并复利为1.957826倍，其中D的T+1腿复利1.116949倍，新A/C腿单独复利
为1.752835倍。D卖出产生额外冲击时，完整组合对冲击非常敏感：

| D额外卖价冲击 | 139笔组合复利 | 相对6907.348272倍变化 |
|---:|---:|---:|
| 0.5% | 6657.95倍 | -3.61% |
| 1.0% | 6416.58倍 | -7.11% |
| 2.0% | 5957.05倍 | -13.76% |
| 3.0% | 5527.00倍 | -19.98% |
| 5.0% | 4748.76倍 | -31.25% |
| 10.0% | 3211.93倍 | -53.50% |

这些数值是历史收益敏感性，不是对未来成交价或收益的承诺。样本只有9笔，不能用来
反复筛选出看似最优的单一阈值。

> **2026-08-07 重算（commit f927d36）**：A/C 候选修正后接力由8笔变9笔
> （D→A 2→1、D→C 6→8），整张敏感性表已按新口径重跑。旧口径数值
> （132笔/4712.470092倍基准，1.0%冲击对应4407.80倍/-6.47%）仅作历史对照。

### 13.3 新增研究工具（历史，已随接力关闭停用）

| 文件 | 方法/作用 |
|---|---|
| `scripts/research_strategy_d_relay_fetch.py` | `load_relay_targets()`精确锁定9笔接力；采集D与新候选共18个角色的09:15~10:30 tick和09:30~10:30一分钟行情，只连接`xtdata`，不创建交易会话、不读取账户、不下单 |
| `scripts/research_strategy_d_relay_capacity.py` | `validate_inputs()`执行18角色完整性门禁；`infer_book_volume_unit()`自动核验盘口数量是股还是手；`build_capacity_replay()`按不同资金规模分为整仓竞价、竞价安全部分+成对POV、取消接力 |
| `tests/test_strategy_d_relay_research.py` | 验证9笔样本锁定、两种QMT tick字段格式、时间戳、股/手单位、18角色门禁、大小资金分流、弱竞价取消及收益冲击 |

第一阶段暂用5%虚拟匹配量作为“竞价安全部分”的保守研究上限，同时要求卖方未匹配
量不超过虚拟匹配量5%。这两个数只是待验证参数，未写入实盘配置。脚本按以下顺序
处理：

```text
快照或单位不能确认 → CANCEL_RELAY
卖方未匹配量 > 虚拟匹配量×5% → CANCEL_RELAY
整仓D股数 ≤ 虚拟匹配量×5% → FULL_AUCTION_RELAY
其余 → 只让安全股数参加竞价，剩余标记PAIRED_POV_REQUIRED
```

### 13.4 Windows采集和回放方式

```powershell
cd C:\A_System

# 第一步只核对目标，不能访问账户，也不会下单
py -3.11 scripts\research_strategy_d_relay_fetch.py --dry-run

# 第二步通过QMT行情接口采集8笔×2角色历史行情
py -3.11 scripts\research_strategy_d_relay_fetch.py

# 第三步：只有16/16个tick角色和16/16个一分钟角色完整才会输出容量结论
py -3.11 scripts\research_strategy_d_relay_capacity.py
```

采集器同时兼容QMT五档数组和`bidPrice1/askPrice1`平铺字段。盘口单位默认根据
`pvolume/volume`自动核验为1或100；比值模糊时直接停止，禁止靠猜测把容量放大
100倍。若已通过QMT原始行情人工确认单位，也可以显式运行：

```powershell
py -3.11 scripts\research_strategy_d_relay_capacity.py --book-volume-unit 1
# 或
py -3.11 scripts\research_strategy_d_relay_capacity.py --book-volume-unit 100
```

输出文件：

```text
data/processed/research_strategy_d_relay_tick.csv
data/processed/research_strategy_d_relay_1m.csv
reports/strategy_d/relay_capacity/d_relay_fetch_report.csv
reports/strategy_d/relay_capacity/d_relay_capacity_detail.csv
reports/strategy_d/relay_capacity/d_relay_capacity_summary.csv
reports/strategy_d/relay_capacity/d_relay_impact_sensitivity.csv
reports/strategy_d/relay_capacity/d_relay_capacity_report.md
```

只有采集报告显示完整角色16/16，才进入09:30后成对POV的分钟级回放。随后仍需同时
验证：全部D能否在10:30前卸完、A/C/E能否用已确认资金买到、成交均价相对开盘价
偏差、不同资金档完成率、139笔组合复利及最大回撤。上述门禁通过前不改实盘D接力，
通过后也必须先小资金验证真实成交与模拟偏差。

### 13.5 QMT历史深度实测与Tushare补数

2026-08-03在国金QMT实测：8笔接力的历史tick全部为空；一分钟行情仅最近3笔
（20260126、20260507、20260514）可取，2024~2025的5笔为空。原容量脚本因此
正确失败关闭，没有用日线或未来数据伪造09:23盘口。

项目Token有Tushare ``stk_mins``历史分钟权限，但没有``stk_auction_o``和
``stk_auction``单独权限。新增以下降级工具：

| 文件/方法 | 作用 |
|---|---|
| `src/data_source.py::get_stock_minute_bars()` | 对官方`stk_mins`做单次、无快速重试调用，避免限频错误被通用重试放大 |
| `scripts/research_strategy_d_relay_tushare_fetch.py` | 补齐16个一分钟角色；每个成功请求立即落盘；从09:30单一价格bar提取最终竞价容量代理 |
| `build_auction_proxy()` | 用成交额÷单一成交价反推成交股数，兼容QMT分钟volume按手、Tushare按股的100倍差异 |

最终竞价容量代理只回答“历史开盘最终匹配量大约能承载多大D仓位”，不能还原09:23
买卖未匹配量。实盘若以后接入分流，仍须读取当日09:23真实虚拟盘口；读不到就取消
大资金接力，不能用09:30历史代理替代实时门禁。

当前Token实测约1次/小时，剩余5笔共8个唯一股票日期。可选择提高Tushare分钟权限，
或盘后让下面命令隔夜断点续传；每次成功后都会立即保存，程序中断可重新运行：

```powershell
cd C:\A_System
py -3.11 scripts\research_strategy_d_relay_tushare_fetch.py --dry-run
py -3.11 scripts\research_strategy_d_relay_tushare_fetch.py --request-interval 3605
py -3.11 scripts\research_strategy_d_relay_capacity.py
```

最近3笔的初步容量结果（最终匹配量×5%再打50%代理折扣，即2.5%上限）显示：D仓位
25万元和50万元均为3/3整仓容量内；D仓位100万元为2/3容量内，另1笔需要成对POV。
这只能说明当前约28~30万元总资产规模在这3笔中没有容量问题，不能替代8笔完整认证。
