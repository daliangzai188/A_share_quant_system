# 策略 E2：板块中性状态小市值首板设计文档

> 2026-07-22：策略 B 已删除。本文旧回测组合名和倍数包含 B 历史交易，只保留审计价值；当前 E2 只在 A/C/D 无计划且不存在仅人工退出的历史 B 仓时触发。

## 一、策略定位

策略 E2（板块中性小市值）当前是在 A/C/D 框架之上叠加的补位机会策略。

**核心逻辑**：当 A/C/D 均未占用资金、且不存在仅人工退出的历史 B 仓时，若今日各板块整体处于"中性"回撤状态，从符合成交概率要求的今日涨停股中选取**流通市值最小**的一只，T+1 开盘买入，T+2 收盘卖出。

**策略代码**：`E2`（由穷举扩展搜索筛选出，选出条件 `segment_retreat_state_bucket=neutral` + 排序规则 `circ_mv_asc`）

---

## 二、触发条件

### 2.1 市场状态条件

| 条件字段 | 目标值 | 说明 |
|---|---|---|
| `segment_retreat_state_bucket` | `neutral` | **各板块**当日均处于中性状态（无明显回撤或升温趋势） |

`segment_retreat_state_bucket` 是每个市场板块（如 sh_main / sz_main / chi_next / star / bj）的回撤状态，由连续3天该板块涨停数量计算得出。E2 要求**当日至少存在一个板块处于 neutral 状态**，且候选股来自 neutral 板块。

### 2.2 候选股过滤条件

| 条件 | 说明 |
|---|---|
| `is_st == False` | 排除 ST / *ST 股票 |
| `allow_buy_reliable == True` | 成交可靠性通过（历史口径验证） |
| `is_fill_score_reliable == True` | 涨停成交概率打标可信 |
| `circ_mv` 有效 | 流通市值数据不为空 |

所有候选按 `circ_mv` 升序排列，取第一只（流通市值最小）。

### 2.3 ABCD 空闲条件（三选一均需满足）

| 检查项 | 判断方式 |
|---|---|
| `positions.json` 无 open 头寸 | 账户当前无任何持仓（含 A/B/C/D） |
| A/B/C 当日无计划委托 | `ab_filtered_daily_ops/` 目录下无今日 planned_orders 文件 |
| D 当日无建仓记录 | `positions.json` 无今日 D 策略记录 |

三项均满足时，E2 才可触发。

---

## 三、segment_retreat_state_bucket 计算逻辑

每日收盘后，取信号日 `T`、前一交易日 `T-1`、前两交易日 `T-2` 各板块的涨停数量：

```
current = 板块 T 日涨停数
prev1   = 板块 T-1 日涨停数
prev2   = 板块 T-2 日涨停数
```

分类规则（优先级从上到下）：

| 条件 | 状态 |
|---|---|
| `current <= 3` | `weak_below_3` |
| `current < prev1 < prev2` | `retreat_2day` |
| `current < prev1` 且 `current <= 5` | `retreat_weak` |
| `current > prev1 > prev2` | `warming_2day` |
| 其他 | **`neutral`** ← E2 目标状态 |

数据来源：`data/raw/limit_list/{YYYYMMDD}.csv`（字段 `limit == "U"` 筛选涨停）

---

## 四、资金模型

### 4.1 与 A/B/C/D 的关系

E2 严格在 A/B/C/D 均**未占用资金**时触发：

| 状态 | E2 是否执行 | 原因 |
|---|---|---|
| A/B/C 有计划委托 | **不执行** | A/B/C 将于 T+1 开盘买入，资金冲突 |
| `positions.json` 有 open 持仓 | **不执行** | 资金被上一笔仓位占用 |
| D 今日已建仓 | **不执行** | D 持仓将于 T+1/T+2 平仓，资金冲突 |
| 全部空闲 | **执行** | 80% 仓位可用 |

### 4.2 仓位参数

| 参数 | 值 |
|---|---|
| `position_pct` | 0.80（80% 仓位） |
| 买入价格 | T+1 开盘价（集合竞价成交） |
| 卖出价格 | T+2 收盘价 |

---

## 五、买卖时机

```
T 日（信号生成）
  15:30  A/B/C daily ops 完成后运行 run_strategy_e2_signal.py
         → 计算各板块 segment_retreat_state_bucket
         → 从 limit_up_fill_scored.csv 筛选 neutral 板块候选
         → 选流通市值最小一只，生成 e2_signal_{YYYYMMDD}.json

T+1 日（买入）
  09:25  开盘集合竞价：以开盘价买入 80% 仓位

T+2 日（卖出）
  14:56  收盘平仓：回测口径为收盘价；实盘按买10/买5挂限价确保成交
```

---

## 六、回测结果

### 6.1 全量对比（近 2 年，2024-05-20 至 2026-05-14）

| 策略组合 | 资金倍数 |
|---|---:|
| 纯 A+B+C | 110x |
| A+B+C+D | 303x |
| **A+B+C+D+E2** | **3640x** |

### 6.2 E2 腿单独表现

| 指标 | 数值 |
|---|---:|
| 条件 | segment_retreat_state_bucket=neutral + circ_mv 最小 |
| 历史触发次数 | 62 笔 |
| 平均每笔账户收益 | +4.41% |
| 累计复利倍数贡献 | 约 12x（叠加在 303x 基础上） |

---

## 七、信号脚本

### 7.1 文件

```
scripts/run_strategy_e2_signal.py
```

### 7.2 运行方式

```bash
# 正常运行（自动推断今日日期）
python scripts/run_strategy_e2_signal.py

# 指定日期
python scripts/run_strategy_e2_signal.py --signal-date 20260617

# 仅预览，不写文件
python scripts/run_strategy_e2_signal.py --signal-date 20260617 --dry-run
```

### 7.3 输出文件

```
reports/strategy_e2/e2_signal_YYYYMMDD.json          本日 E2 信号（仅有候选时生成）
reports/strategy_e2/e2_signal_YYYYMMDD_candidates.csv 所有符合条件的候选列表
```

### 7.4 控制台输出示例

E2 触发时：
```
[E2信号] 信号日期: 20260617
[E2信号] ABCD 今日均空闲，开始筛选 E2 候选。
[E2信号] 今日各板块状态: {'sh_main': 'neutral', 'sz_main': 'neutral', ...}
[E2信号] neutral 板块: ['sh_main', 'sz_main', ...]
[E2信号] 符合条件候选: 8 只

============================================================
  策略E2 信号
============================================================
  股票:       000123.SZ  某某科技
  板块:       sz_main  (neutral)
  流通市值:   12.3 亿
  成交概率:   72.5%
  买入计划:   20260618 开盘价买入  仓位80%
  卖出计划:   20260619 收盘前卖出
============================================================
```

E2 不触发时：
```
[E2信号] 信号日期: 20260617
[E2信号] A/B/C 今日已生成计划委托，E2 不触发。
```

---

## 八、数据依赖

| 文件 | 字段 | 用途 |
|---|---|---|
| `data/processed/limit_up_fill_scored.csv` | `market_segment`, `circ_mv`, `is_st`, `allow_buy_reliable`, `is_fill_score_reliable` | 候选筛选 |
| `data/raw/limit_list/{YYYYMMDD}.csv` | `ts_code`, `limit` | 计算各板块涨停数量 |
| `data/raw/trade_calendar.csv` | `cal_date`, `is_open` | T+1/T+2 日期推算 |
| `data/processed/positions.json` | `strategy_leg`, `status`, `signal_date` | ABCD 空闲检测 |
| `reports/paper_trade/ab_filtered_daily_ops/` | `planned_orders*.csv` | A/B/C 委托检测 |

---

## 九、涉及的文件

| 文件 | 变动类型 | 核心内容 |
|---|---|---|
| `scripts/run_strategy_e2_signal.py` | 新建 | E2 每日信号生成脚本（完整实现） |
| `scripts/search_abcd_expansion_strategy.py` | 已有 | 穷举搜索脚本，E2 由此发现 |
| `reports/strategy_expansion/abcd_expansion_search_summary.csv` | 已有 | E2 在第 2 行：条件=neutral, 排序=circ_mv_asc, 62笔, 均值4.41% |

---

## 十、当前状态与限制

| 项目 | 状态 |
|---|---|
| 回测完成 | ✅ 近 2 年，ABCD+E2 = 3640x |
| 信号脚本实现 | ✅ `run_strategy_e2_signal.py` |
| 守护进程集成 | 待添加（建议在 15:30 收盘流水线后单独调用） |
| 分钟 K / 盘口验证 | 未做（沿用 A/B/C 口径：日线保守成交验证） |
| 实盘下单 | 未接入（信号仅输出 JSON 文件，需人工确认后手动操作） |
