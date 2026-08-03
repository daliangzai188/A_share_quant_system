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
