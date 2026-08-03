# 策略 D：首板打板设计文档

> 2026-07-22：策略 B 已删除。本文旧回测表中的 A+B+C 组合只保留历史审计价值；当前资金关系按 A/C/D/E2/L 重新计算，历史 B 仓仅人工退出时阻断 D 新开仓。

## 一、策略定位

策略 D（首板打板）当前叠加在 A/C 框架之上。

**核心逻辑**：当市场处于强势情绪时，买入当日第一次涨停（首板）且曾炸板后重新封板（multi_open）的股票，在涨停价排队买入，次日开盘集合竞价卖出。

**与 A/C 的关系**：D 在盘中执行，A/C 在收盘后生成信号、次日开盘执行；真实持仓占用优先于理论时序。仅人工退出的历史 B 仓存在时，D必须跳过。

---

## 二、触发条件

### 2.1 股票筛选条件

| 条件 | 说明 |
|---|---|
| `limit_times == 1` | 首板（当日第一次涨停，非连板） |
| 不在昨日涨停池 | 排除昨日已涨停的股票（连板进入 A/B/C 策略） |
| `open_times >= 1` | 曾经炸板（multi_open 板型） |
| `open_times <= 3` | 炸板次数不超过 3 次（过多炸板说明情绪不稳） |
| 当前处于涨停封板状态 | `last_price == upper_limit` |

### 2.2 市场情绪条件

| 情绪等级 | 全市场今日涨停累计数 | D 策略是否触发 |
|---|---:|---|
| weak | < 50 | 不触发 |
| neutral | 50–99 | 不触发 |
| **strong** | **≥ 100** | **触发** |
| very_strong | ≥ 150 | 触发 |

**只在 strong / very_strong 市场情绪下触发**，这是核心过滤条件。

### 2.3 两档信号时序

```
09:30  开盘
  │
09:35  WATCH 窗口开启
  │    → 检测到符合条件的首板 multi_open 回封
  │    → 发出 [WATCH] 提醒，记录关注名单
  │    → 继续追踪该股票（可能后续炸板）
  │
14:00  BUY 窗口开启
  │    场景一：WATCH 名单中的股票在 14:00 时仍处于封板 → 升级为 [BUY] 信号
  │    场景二：14:00 后新出现的重封 → 直接发出 [BUY] 信号
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

### 3.1 四种日状态与 D 的关系

| ABC 状态 | 含义 | D 是否执行 | 原因 |
|---|---|---|---|
| `NO_CANDIDATE` | ABC 无信号，账户空仓 | **执行** | 资金完全可用 |
| `HISTORICAL_SIM_FILLED` | ABC 生成信号，T+1 开盘买入 | **执行** | D 当日买→T+1 卖；ABC T+1 买→顺序使用同一笔资金，不冲突 |
| `BUY_REJECTED` | ABC 信号被风控拒绝，账户空仓 | **执行** | 实际账户空仓，D 可独立使用资金 |
| `POSITION_OCCUPIED_SKIP` | ABC 有旧持仓占用 80% 资金 | **跳过** | 资金真实冲突；且 D 在此情况下实测胜率仅 20%（均收益 -2%） |

### 3.2 顺序使用同一资金的完整流程

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

## 四、回测结果

### 4.1 全量对比（近 2 年）

| 策略组合 | 资金倍数 | 总成交笔数 |
|---|---:|---:|
| 纯 A+B+C | 110x | 90 |
| A+B+C+D（旧，仅 NO_CANDIDATE 日）| 235x | 90+22 |
| **A+B+C+D（落地版，扩展触发）** | **303x** | **90+36** |
| A+B+C+D+E2（叠加板块中性小市值策略）| 3640x | 90+36+62 |

> D 出场规则：`HISTORICAL_SIM_FILLED` 日 → T+1 开盘卖出（顺序使用同一资金，不冲突）；`NO_CANDIDATE` / `BUY_REJECTED` 日 → T+2 收盘卖出。

### 4.2 D 腿单独表现（strong 情绪，22 笔完整回测）

| 指标 | 数值 |
|---|---:|
| 情绪筛选 | strong（≥ 100 只涨停）|
| 样本笔数 | 22 |
| 胜率 | 59.1% |
| 平均收益 | +5.09% |
| 仅 NO_CANDIDATE 日倍数贡献 | 235x（vs ABC 110x） |

### 4.3 D+ABC 叠加笔分布（36 笔）

| 组合类型 | 笔数 | 胜率 | 平均收益 |
|---|---:|---:|---:|
| D only（NO_CANDIDATE 日）| 22 | 59.1% | +5.09% |
| D+A | 2 | 100% | +9.47% |
| D+B | 5 | 60% | +9.10% |
| D+C | 7 | 71.4% | +9.47% |

### 4.4 穿越漏斗（strong 情绪，近 2 年）

```
strong 情绪交易日：97 天
  → 首板候选：≈ 12,000 只（各日首板总量）
  → 满足 multi_open + open_times ≤ 3 + last_time ≥ 10:00：1,118 只
  → 满足 last_time ≥ 14:00（尾盘重封）：491 只
  → 当日收盘于涨停（历史成交确认）：491 只（全部）
  → 触发 D 策略（取最优一只）：36 天
```

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
         if st.last_seal_hhmm >= 1400 or st.watch_alerted → 发 BUY 信号
     elif hhmm >= 1000 and not st.watch_alerted:
         → 发 WATCH 提醒
 6. 同一时刻有多只候选：先选炸板2次，再按封单金额/流通市值降序，只尝试第一名
 7. 14:55 → cancel_all_d_orders()，脚本退出
```

`open_times`必须在1~3之间。该排序与回测候选选择一致；若第一名下单失败，不递补第二名，
避免回测只记录第一名、实盘却在失败后换票造成收益口径漂移。

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

守护进程（`scripts/trading_daemon.py`）在 **13:30** 以非阻塞子进程启动 D 监控：

```python
SCHEDULE = [
    datetime.time(9, 20),   # 盘前：平仓检查 + 读取计划单
    datetime.time(13, 30),  # 策略 D 盘中监控启动
    datetime.time(14, 50),  # 盘中：平仓检查
    datetime.time(15, 10),  # 收盘：数据流水线 + A+B+C 信号
]
```

### 6.2 非阻塞启动原因

D 监控脚本从 13:30 持续运行到 14:55（约 85 分钟），若用 `subprocess.run` 会阻塞守护进程，导致 14:56 收盘平仓任务无法执行。改用 `subprocess.Popen`，守护进程立即返回，D 监控在后台独立运行。

```python
def job_strategy_d() -> None:
    cmd = [PYTHON, "-B", str(PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py")]
    if live_order:
        cmd.append("--live-order")
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT)
    logger().info("策略D监控已启动（PID %d）", proc.pid)
```

### 6.3 日程全览

| 时间 | 任务 | 是否阻塞守护进程 |
|---|---|---|
| 09:20 | 平仓检查 + 复核组合状态 + 有开仓计划时预挂买单（job_morning → job_premarket_buy）| 是 |
| 13:30 | 启动 D 监控子进程（job_strategy_d）| 否（Popen） |
| 14:56 | 盘中收盘平仓 + 14:57 撤未成交买单（job_afternoon）| 是 |
| 15:10 | 收盘流水线 + A+B+C 信号（job_post_market）| 是 |

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

启动时读取 `data/processed/positions.json`，若存在任意 `strategy != "D"` 且 `status == "open"` 的持仓，则跳过当日 D 监控并打印警告：

```python
def check_abc_position_occupied() -> tuple[bool, str]:
    positions = json.loads(Path("data/processed/positions.json").read_text())
    for pos in positions:
        if pos.get("strategy") != "D" and pos.get("status") == "open":
            return True, f"ABC持仓占用: {pos['ts_code']}"
    return False, ""
```

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
1. 按日推进，若前日有 D 持仓 → T+1 开盘卖出
2. 若前日有 ABC 持仓且到期 → 平仓
3. 若账户空仓且当日 op_status in D_ELIGIBLE_STATUSES → 检查是否有 D 候选
4. D 候选条件：strong 情绪 + 首板 + multi_open + open_times ≤ 3 + last_time ≥ 140000
5. 有 D 候选：D 当日买，T+1 开盘以 next_open 卖出
6. 同日若有 ABC 信号（HISTORICAL_SIM_FILLED）：D 收益 + ABC 收益叠加（同一资金顺序使用的近似）

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

## 十、涉及的文件变动汇总

| 文件 | 变动类型 | 核心内容 |
|---|---|---|
| `scripts/backtest_strategy_d.py` | 新建 | D 策略完整回测，触发范围扩展到 36 笔 |
| `scripts/monitor_strategy_d_intraday.py` | 新建 | D 盘中监控，两档信号，14:55 自动撤单 |
| `scripts/collect_strategy_d_minute_data.py` | 新建 | D 候选分钟数据采集辅助脚本 |
| `scripts/trading_daemon.py` | 修改 | 新增 13:30 调度槽，`job_strategy_d()` 用 Popen 启动 |
| `src/broker_adapter.py` | 修改 | 新增 `cancel_order` 抽象方法 |
| `src/qmt_adapter.py` | 修改 | 实现 `cancel_order`（`cancel_order_stock`） |

---

## 十一、当前状态与限制

| 项目 | 状态 |
|---|---|
| 回测完成 | ✅ 近 2 年，303x（混合出场：SIM日T+1开，NC/BR日T+2收） |
| 守护进程集成 | ✅ 13:30 自动启动 |
| 两档信号逻辑 | ✅ WATCH / BUY |
| 14:55 自动撤单 | ✅ cancel_order 已实现 |
| QMT 实盘下单 | 待量化权限开通（--live-order 开关已就绪）|
| 成交填充率验证 | 未做（A 股打板排队，失败即不成交，无亏损） |
| 情绪历史准确率 | 盘中估算（ever_sealed 累计），非收盘精确数 |

---

## 十二、普通D按容量分流卖出（执行层已接入）

### 12.1 为什么不能直接把全部D改成POV

当前组合中的D退出分为两类，必须严格分开：

1. **普通D（17笔）**：T+2收盘退出，理论收益使用退出日收盘价；
2. **D→A/C/E2接力（8笔）**：T+1集合竞价先卖D，再释放资金买入A/C/E2。

接力D必须保留集合竞价卖出，不能拖到尾盘POV。普通D若不分金额全部提前卖，
可能因日内价格路径损失而偏离收盘价；若仓位过大又全部挤在14:55，可能产生
冲击成本。因此普通D只研究“**容量不足时才触发**”的分档卖出，不做无条件POV。

当前执行层规则已经接入：

1. `planned_exit_date=今日`的普通D允许进入容量检查；
2. 13:00仓位市值不超过当时累计成交额1%时，不启动POV，继续14:55平仓；
3. 超过1%才建立5分钟拆卖跑道，14:30和14:45还会按实际流速复核；
4. 普通D不使用“仓位达到950万元就固定14:15启动”的绝对金额规则，避免流动性
   充足时被无谓提前卖出；
5. 14:53撤销未完成POV委托，14:55只按QMT实际余仓继续平仓；
6. D接力发生在T+1，而普通计划平仓日仍为T+2，因此不会误入尾盘POV；
7. 选股、打板买入、持有期和T+2收盘回测收益口径均未修改。

### 12.2 新增研究工具

| 文件 | 作用 |
|---|---|
| `scripts/research_strategy_d_exit_fetch.py` | 从当前132笔组合账本精确提取17笔普通D，采集退出日全日5分钟和尾盘1分钟行情；接力D自动排除 |
| `scripts/research_strategy_d_exit_pov.py` | 回放动态起点、14:30起点、14:45起点三种卖出方案，并复算D腿及完整132笔组合 |
| `tests/test_strategy_d_exit_pov_research.py` | 验证样本隔离、无冲击时保持基准复利、分钟数据不完整时拒绝认证 |

Windows盘后运行：

```powershell
cd C:\A_System
py -3.11 scripts\research_strategy_d_exit_fetch.py --dry-run
py -3.11 scripts\research_strategy_d_exit_fetch.py
py -3.11 scripts\research_strategy_d_exit_pov.py
```

### 12.3 参数研究与后续门禁

当前只复用了已有的1%容量门禁，没有根据17笔D样本另行调参。后续若要修改D专属
触发比例、参与率或启动时间，研究结果必须同时满足以下条件：

1. 17笔普通D均有48根5分钟bar和16根尾盘1分钟bar；
2. 所有17笔在对应仓位金额下均能卖完，残仓不得按收盘价虚构成交；
3. D全样本复利不低于原普通D基准；
4. 按时间切分后的前11笔与后6笔都不劣于原基准；
5. 替换D退出价后，完整132笔组合复利不低于4712.470092倍；
6. 完整组合最大回撤不劣于-18.840599%；
7. 同时查看成交均价代理和bar最低价压力代理，不能只看乐观结果。

分钟K成交额不等于Level-2真实买盘深度，因此研究会对容量再打折。样本仅17笔，
它只能用来排除明显不合格方案，不能保证未来收益。在D专属参数通过门禁前，保持
当前1%容量阈值不变；仍须先用小资金观察实盘成交均价与收盘价的偏差。

---

## 十三、D接力集合竞价容量研究（实盘路径暂未修改）

### 13.1 本轮结论与边界

第12节“接力D必须整仓集合竞价卖出”只描述当前实盘实现，不再作为大资金方案结论。
本轮锁定的研究方向是：

1. 小资金D整仓不超过09:23虚拟匹配量安全比例时，保留整仓竞价接力；
2. 大资金D禁止继续09:23整仓跌停价卖，只让安全部分参与竞价；
3. 剩余D在09:30后按真实成交先卖，确认资金释放后再买A/C/E2，形成资金中性的
   成对POV；
4. 09:23盘口无效、卖方未匹配量过大、数据不完整或回放价格冲击过大时，直接
   `CANCEL_RELAY`，宁可D恢复普通T+2退出，也不主动增加开盘卖压；
5. 当前只完成数据采集与第一阶段容量分流工具，尚未修改
   `trading_daemon.py::job_premarket_sell()`，实盘仍是09:23整仓跌停价委托。

上海证券交易所的行情接口规则明确：集合竞价时买一和卖一价格同时表示虚拟开盘
参考价，买一量和卖一量表示虚拟匹配量，买二量/卖二量分别表示买方/卖方未匹配量。
研究脚本据此只读取09:23及以前已经发布的快照，不用09:25真实开盘结果倒推决策。

### 13.2 锁定样本与收益冲击

当前132笔可执行组合中共有8笔D接力：D→A 2笔、D→C 6笔、D→E2 0笔。8笔
接力合并复利为1.623200倍，其中D的T+1腿复利仅0.935628倍，新A/C腿单独复利
为1.734878倍。D卖出产生额外冲击时，完整组合对冲击非常敏感：

| D额外卖价冲击 | 132笔组合复利 | 相对4712.470092倍变化 |
|---:|---:|---:|
| 0.5% | 4557.91倍 | -3.28% |
| 1.0% | 4407.80倍 | -6.47% |
| 2.0% | 4120.50倍 | -12.56% |
| 3.0% | 3849.72倍 | -18.31% |
| 5.0% | 3354.48倍 | -28.82% |
| 10.0% | 2352.05倍 | -50.09% |

这些数值是历史收益敏感性，不是对未来成交价或收益的承诺。样本只有8笔，不能用来
反复筛选出看似最优的单一阈值。

### 13.3 新增研究工具

| 文件 | 方法/作用 |
|---|---|
| `scripts/research_strategy_d_relay_fetch.py` | `load_relay_targets()`精确锁定8笔接力；采集D与新候选共16个角色的09:15~10:30 tick和09:30~10:30一分钟行情，只连接`xtdata`，不创建交易会话、不读取账户、不下单 |
| `scripts/research_strategy_d_relay_capacity.py` | `validate_inputs()`执行16角色完整性门禁；`infer_book_volume_unit()`自动核验盘口数量是股还是手；`build_capacity_replay()`按不同资金规模分为整仓竞价、竞价安全部分+成对POV、取消接力 |
| `tests/test_strategy_d_relay_research.py` | 验证8笔样本锁定、两种QMT tick字段格式、时间戳、股/手单位、16角色门禁、大小资金分流、弱竞价取消及收益冲击 |

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
验证：全部D能否在10:30前卸完、A/C/E2能否用已确认资金买到、成交均价相对开盘价
偏差、不同资金档完成率、132笔组合复利及最大回撤。上述门禁通过前不改实盘D接力，
通过后也必须先小资金验证真实成交与模拟偏差。
