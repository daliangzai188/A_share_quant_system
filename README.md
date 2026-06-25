# A股量化交易系统

本项目用于构建一套完整的 A股量化交易系统，目标是先完成数据采集、数据清洗、因子统计、策略回测和模拟交易，后续再接入 QMT / miniQMT 等券商接口进行半自动或实盘交易。

> 当前阶段：A+B+C+D+E2 策略已定型，本地模拟盘守护进程已上线，持续自动运行。  
> D 策略（首板打板）已完成回测（303x vs 纯 A+B+C 的 110x）并接入守护进程，每日 13:30 自动启动盘中监控。  
> E2 策略（板块中性小市值）已完成回测（ABCD+E2 = 3640x）并实现收盘信号脚本 `run_strategy_e2_signal.py`。  
> L 龙头策略已完成独立研究和实盘约束模拟认证，并已接入总策略模式开关；默认仍为 `mode=1`，`strategy_l.enabled=false`，不会影响当前实盘。详见 `docs/strategy_l.md`。
> 实盘接入：QMT / miniQMT 已完成只读连接与守护进程联调，真实下单仍必须先走小资金验证。所有时间以北京时间（Asia/Shanghai）为准。

---

## 一、项目目标

本项目主要建设以下能力：

1. 自动采集 A股历史数据
2. 自动采集涨停池、连板、换手率、成交额等数据
3. 建立涨停板成交概率模型
4. 计算龙头战法相关因子
5. 进行策略回测和样本外验证
6. 输出统计报告
7. 模拟交易验证策略稳定性
8. 后续扩展 QMT / miniQMT 实盘接口

---

## 二、核心研究方向

本项目重点研究 A股龙头战法量化，包括：

1. 涨停池统计
2. 连板股研究
3. 换手率模型
4. 封单金额分析
5. 炸板次数分析
6. 首次涨停时间分析
7. 题材热度分析
8. 市场情绪分析
9. 次日溢价统计
10. 成交概率过滤

---

## 三、系统建设阶段

### 第一阶段：数据采集

目标是将 2019 年至今的 A股历史数据保存到本地。

包括：

- 日线行情
- 前复权价格
- 涨停池数据
- 换手率
- 成交额
- 流通市值
- 资金流
- 板块题材
- 龙虎榜数据

---

### 第二阶段：成交概率模型

A股龙头战法最核心的问题不是股票涨不涨，而是能不能买到。

因此需要先建立：

```text
连板天数 × 板型 × 首次涨停时间 × 市场情绪
```

对应的历史换手率查询表，用于判断涨停板排队成交概率。

---

### 第三阶段：因子统计

在成交概率过滤之后，再统计因子表现，避免把历史上根本买不到的涨停股计入收益。

核心统计指标包括：

- 样本数
- 胜率
- 平均收益
- 中位数收益
- 总收益
- 最大回撤
- 盈亏比
- 最大单笔亏损
- 最大单笔盈利

---

### 第四阶段：策略回测

回测必须考虑 A股真实交易规则：

- T+1
- 涨停买不到
- 跌停卖不出
- 停牌
- 手续费
- 印花税
- 过户费
- 滑点
- 最大仓位限制
- 最大回撤限制

---

### 第五阶段：模拟交易

在不接真实资金的情况下，模拟完整交易流程，包括：

- 选股
- 买入信号
- 卖出信号
- 委托记录
- 成交记录
- 持仓记录
- 资金变化
- 风控拦截
- 日志复盘

---

### 第六阶段：实盘接口扩展

后续在回测和模拟盘稳定后，再接入：

- QMT
- miniQMT
- 券商官方量化接口

默认不启用实盘交易。

当前已加入第一版 QMT 接入骨架：

- `src/broker_adapter.py`：统一券商适配器抽象层（含 `place_order` 和 `cancel_order` 接口）。
- `src/qmt_adapter.py`：QMT / miniQMT 适配器，运行时懒加载 `xtquant`；实现了 `cancel_order_stock` 撤单。
- `src/live_order_gateway.py`：A+B+C 计划单转实盘预览和真实下单安全闸门。
- `scripts/qmt_account_check.py`：QMT 只读账户检查。
- `scripts/preview_live_orders.py`：读取每日计划单并做实盘执行前校验，不下单。
- `scripts/submit_live_orders.py`：真实下单入口，默认配置会拒绝执行。

详细流程见 `docs/qmt_live_integration.md`。

---

## 四、推荐项目结构

```text
A_System/
├── AGENTS.md
├── README.md
├── requirements.txt
├── run.sh
├── .env.example
├── config/
│   ├── config.json
│   └── strategy_config.json
├── data/
│   ├── raw/
│   ├── processed/
│   └── database/
├── logs/
├── reports/
├── src/
│   ├── data_source.py
│   ├── data_collector.py
│   ├── data_cleaner.py
│   ├── trading_calendar.py
│   ├── fill_model.py
│   ├── factors.py
│   ├── whitelist.py
│   ├── strategy_base.py
│   ├── backtest_engine.py
│   ├── risk_manager.py
│   ├── paper_trader.py
│   ├── broker_adapter.py
│   ├── qmt_adapter.py
│   ├── report_generator.py
│   └── utils.py
├── scripts/
│   ├── collect_daily_data.py
│   ├── collect_limit_data.py
│   ├── build_fill_rate_table.py
│   ├── run_backtest.py
│   └── run_paper_trade.py
└── web/
    ├── app.py
    └── templates/
```

---

## 五、技术栈

优先使用：

- Python 3.10+
- pandas
- numpy
- sqlite / PostgreSQL
- Tushare Pro
- akshare / baostock
- FastAPI 或 Flask
- APScheduler
- matplotlib
- python-dotenv
- logging
- QMT / miniQMT

---

## 六、数据源规划

第一阶段优先使用：

```text
Tushare Pro
```

主要原因：

- 数据稳定
- Python 接口方便
- 支持日线行情
- 支持涨停池
- 支持换手率
- 支持资金流
- 支持龙虎榜
- 支持板块概念数据

AKShare 和 baostock 可以作为辅助数据源，但不作为核心数据唯一来源。

---

## 七、重要交易规则

本系统必须遵守 A股交易规则：

1. A股是 T+1，今天买入不能今天卖出。
2. 涨停股票可能买不到。
3. 跌停股票可能卖不出。
4. 停牌股票不能交易。
5. 普通 A股账户不能裸卖空。
6. 回测必须扣除手续费、印花税、过户费和滑点。
7. 实盘前必须先经过回测、样本外验证和模拟盘验证。

---

## 八、运行方式

安装依赖：

```bash
pip install -r requirements.txt
```

复制环境变量文件：

```bash
cp .env.example .env
```

运行数据采集：

```bash
python scripts/collect_daily_data.py
```

运行涨停池采集：

```bash
python scripts/collect_limit_data.py
```

构建成交概率表：

```bash
python scripts/build_fill_rate_table.py
```

运行回测：

```bash
python scripts/run_backtest.py
```

运行模拟交易：

```bash
python scripts/run_paper_trade.py
```

当前策略发布验证和后续操作流程见：

```text
docs/strategy_release_playbook.md
```

常用命令：

```bash
# 策略发布前稳定性验证：季度或半年执行一次，不用于每日改策略
.venv/bin/python -B scripts/run_strategy_release_validation.py

# A+B+C filtered 单日模拟盘操作台：只生成模拟观察和人工复核清单，不接实盘
.venv/bin/python -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10

# A+B filtered 历史窗口回放：用于复查 60/90/120 日窗口表现；C 补位另见 backup_strategy_c 报告
.venv/bin/python -B scripts/run_paper_ab_filtered_observation_window.py --recent-days 120 --end-date 20260518
```

---

## 九、配置说明

核心配置放在：

```text
config/config.json
```

敏感信息放在：

```text
.env
```

不要把真实的 Token、账号、密码提交到 Git。

只允许提交：

```text
.env.example
```

---

## 十、Codex / AI 开发规则

本项目的 Codex 开发规则放在：

```text
AGENTS.md
```

`README.md` 只作为项目说明文档。

`AGENTS.md` 用于约束 Codex / AI agent 在本项目中如何写代码、如何修改文件、如何遵守项目规则。

---

## 十一、风险提示

本项目仅用于量化研究、策略回测、模拟交易和技术学习。

任何策略都不能保证盈利。

在进行真实交易前，必须完成：

1. 数据完整性验证
2. 回测验证
3. 样本外验证
4. 模拟盘验证
5. 风控测试
6. 小资金验证

实盘交易存在亏损风险，请谨慎使用。

---

## 十二、当前策略发布流程

当前固定策略版本为：

```text
a_strict_plus_b0018_filtered_plus_c_hold3
```

原则：

1. 不每天改策略。
2. 每天最多只执行当前固定策略版本的候选检查：A 优先，B 次之，C 只在 A/B 没有历史模拟成交时补位。
3. 每 3 个月或 6 个月重新执行一次发布验证。
4. 只有发布验证通过，才考虑进入下一阶段模拟或小资金人工确认。
5. C 补位策略当前不强制分钟 K 验收；最低验收口径是涨停排队买不到、跌停排队卖不出的日线保守成交验证。
6. 自动实盘前仍必须完成券商接口、风控、人工确认和成交可行性验证。

详细流程以 `docs/strategy_release_playbook.md` 为准。

---

## 十三、自动化守护进程（模拟盘）

### 启动 / 停止

```bash
./start.sh   # 启动守护进程 + watchdog，实时显示日志（Ctrl+C 断开日志，程序继续后台运行）
./stop.sh    # 停止所有进程
```

### 守护进程设计

核心文件：`scripts/trading_daemon.py`

每个交易日自动执行四个时间窗口的任务：

| 时间  | 任务 |
|-------|------|
| 09:20 | 集合竞价截止前：平仓检查（最高优先级）+ 读取买入计划单 |
| 13:30 | 策略 D 盘中监控启动（独立子进程，非阻塞） |
| 14:50 | 盘中：平仓检查 |
| 15:10 | 收盘后：完整数据流水线 + A+B+C 信号生成 |

收盘流水线七步：

```
① collect_all_data.py                   采集日线 + 涨停池
② clean_collected_data.py               清洗合并
③ build_dynamic_features.py             市场情绪 / 题材热度
④ score_limit_up_fill_probability.py    涨停成交概率打分
⑤ analyze_next_day_premium.py           次日溢价因子
⑥ run_paper_ab_filtered_daily_ops.py    A+B+C 信号生成
⑦ run_strategy_e2_signal.py             E2 信号生成（板块中性小市值，ABCD空闲时才触发）
```

其中 ①、②、④ 是关键步骤。关键步骤第一次失败会自动等待 10 秒重试一次；仍失败则停止本次收盘流水线，不生成计划单，避免继续使用旧信号。

清洗阶段会按交易日输出进度百分比，例如：

```text
清洗进度: 54.5% (6/11)，当前日期: 20260608，daily累计: 33066，limit累计: 463
```

Windows 启动脚本会实时转发子进程日志，并强制使用 UTF-8 输出，避免 PowerShell 中中文乱码。

### 缓存与重启口径

守护进程每天只需要在目标交易日缺少收盘结果时自动拉取数据。若当天收盘流水线已经成功，再次重启会直接使用缓存，日志会显示：

```text
已有 20260615 收盘数据缓存，直接使用
```

需要完整复验无缓存流程时，可以手动删除当天原始数据、`data/processed` 汇总文件、当天 A+B+C 输出和 `logs/post_market_done_YYYYMMDD.marker`，再重新启动守护进程。

### Windows VM 实盘守护进程

在 Windows 虚拟机 PowerShell 中执行：

```powershell
cd Z:\
py -3.11 stop_windows.py
py -3.11 start_windows.py
```

`start_windows.py` 会先停止旧进程并等待 QMT session 释放，再启动新进程。终端颜色含义：

- 绿色：QMT 连接成功、程序正常、流水线完成、计划单生成
- 黄色：警告、暂不开仓、需要关注但不一定阻塞
- 红色：失败、异常、QMT 连接错误

如果终端只想断开实时日志，按 `Ctrl+C` 即可，后台守护进程继续运行；真正停止进程必须执行 `py -3.11 stop_windows.py`。

### 安全设计原则

1. **平仓优先**：平仓逻辑完全独立于数据流水线，数据步骤报错不影响平仓。
2. **关键步骤保护**：数据采集、清洗、成交概率打分失败时会自动重试；仍失败则停止本次收盘流水线，不用旧信号生成计划。
3. **超时保护**：每个 subprocess 步骤设超时（数据步骤 10 分钟，下单步骤 1 分钟）。
4. **自动重启**：start.sh 启动外部 watchdog，每 30 秒检测进程存活，崩溃自动重启。
5. **启动即检查**：每次启动立刻扫描逾期持仓，无论因何原因重启都不会漏掉平仓。
6. **QMT 连接重试**：启动时最多自动尝试 5 次 QMT 连接，每次间隔 15 秒，降低 session 短暂未释放导致的误报。

### 持仓状态

持仓记录持久化到 `data/processed/positions.json`，包含：

- `planned_exit_date`：T+2 计划平仓日（买入日起算两个交易日）
- `status`：`open` / `sell_pending` / `closed`
- 程序重启后自动读取，不依赖内存状态

### 交易日历

读取 `data/raw/trade_calendar.csv`。若日历数据未覆盖当日，自动降级为周一至周五判断，并记录警告日志。

### 北京时间

所有时间计算统一使用 `src/utils/time_utils.py`：

```python
from src.utils.time_utils import now_beijing, today_beijing, yesterday_beijing
```

日志时间戳通过 `src/utils/logger.py` 的 `_BeijingFormatter` 强制转换为北京时间，与 Mac 系统时区无关。

### 市场开放权限

`config/strategy_config.json` 已开启：

- 北交所：`exclude_bj: false`
- 科创板：`exclude_market_segments: []`

### 前向信号生成（T日选股不依赖 T+2 数据）

`strategy_optimizer.load_trades()` 新增 `require_complete_exit` 参数：

- `True`（默认）：回测 / 优化模式，只用有完整 T+2 收益数据的历史记录
- `False`：模拟盘选股模式，当日涨停记录无需等待 T+2 即可参与选股

`paper_candidate_generator.py` 使用 `require_complete_exit=False`，确保今日涨停池当天就能生成明日候选信号。

### 策略 D（首板打板）集成

D 策略在每个交易日 13:30 由守护进程以**非阻塞子进程**启动，独立运行到 14:55 自动撤单结束。

核心逻辑：
- 扫描全市场约 5512 只股票，30 秒一轮，检测首板 multi_open 回封信号
- 10:00 回封 → 发出 **[WATCH]** 观察提醒；14:00+ 回封（或 WATCH 标的仍在封板）→ 发出 **[BUY]** 信号
- 情绪要求：全市场当日累计涨停数 ≥ 100（强势市场）
- 14:55 自动撤销所有未成交的 D 委托（`cancel_order_stock`）
- 检测到 ABC 有持仓时跳过 D（资金冲突防护）

回测结果（近 2 年）：

| 策略组合 | 资金倍数 | D 成交笔数 |
|---|---:|---:|
| 纯 A+B+C | 110x | — |
| A+B+C+D（仅 NO_CANDIDATE 日）| 235x | 22 笔 |
| A+B+C+D（扩展，当前落地版） | **303x** | **36 笔** |
| A+B+C+D+E2（板块中性小市值） | **3640x** | **62 笔** |

详细设计见 `docs/strategy_d.md`。

### 策略 E2（板块中性小市值）集成

E2 策略在 A+B+C+D 均未占用资金时触发，每日收盘后（建议 15:30+）运行信号脚本。

**触发条件：**
- 板块今日处于中性回撤状态（`segment_retreat_state_bucket = neutral`）
- 候选股：非 ST、成交概率可靠（`allow_buy_reliable=True`、`is_fill_score_reliable=True`）
- 选取流通市值最小（`circ_mv` 升序取第一）

**买卖时机：**
- T+1 开盘买入（80% 仓位），T+2 收盘卖出

**每日运行：**
```bash
python scripts/run_strategy_e2_signal.py
```

输出：
- `reports/strategy_e2/e2_signal_YYYYMMDD.json` — 今日信号
- `reports/strategy_e2/e2_signal_YYYYMMDD_candidates.csv` — 所有候选

详细设计见 `docs/strategy_e2.md`。

### 备用 cron 脚本

`run_daily.sh`：手动触发或 cron 备用，执行同样的六步流水线。若守护进程正常运行，此脚本不需要单独配置 cron。
