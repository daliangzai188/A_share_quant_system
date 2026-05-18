# A股量化交易系统

本项目用于构建一套完整的 A股量化交易系统，目标是先完成数据采集、数据清洗、因子统计、策略回测和模拟交易，后续再接入 QMT / miniQMT 等券商接口进行半自动或实盘交易。

> 当前阶段：本地数据研究 + 回测系统 + 模拟交易  
> 默认不接实盘，不进行真实下单。

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
