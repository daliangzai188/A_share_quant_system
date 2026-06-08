# QMT / miniQMT 实盘接入说明

本文档说明当前项目如何接入券商 QMT / miniQMT。当前实盘层默认关闭，必须先完成只读检查和计划单预览。

## 当前分工

| 层级 | 当前实现 |
|---|---|
| 历史数据 | Tushare Pro + 本地 CSV |
| 策略信号 | A+B+C 每日操作台 |
| 实时行情 | QMT `xtdata` |
| 资金/持仓/委托/成交 | QMT `xttrader` |
| 下单/撤单 | QMT `xttrader`，默认关闭 |

## 配置环境变量

复制 `.env.example` 为 `.env` 后补充：

```text
QMT_ACCOUNT_ID=你的证券资金账号
QMT_ACCOUNT_TYPE=STOCK
QMT_PATH=你的QMT客户端userdata_mini路径
QMT_SESSION_ID=1001
```

`QMT_PATH` 通常由券商 QMT 客户端安装目录决定，不同券商和电脑路径不同。

## 第一步：开启只读连接

先在 `config/config.json` 中只开启连接能力：

```json
{
  "broker_adapter_enabled": true,
  "qmt_enabled": true,
  "broker": {
    "enabled": true,
    "adapter": "qmt"
  }
}
```

不要把 `trade_mode` 改成 `live`，不要打开 `live_trade.real_order_enabled`。

在审批通过前，可以先运行本地准备度检查：

```bash
.venv/bin/python -B scripts/check_qmt_live_readiness.py
```

该脚本不连接券商、不提交委托、不打印真实账号。

运行：

```bash
.venv/bin/python -B scripts/qmt_account_check.py
```

验证生成：

```text
reports/live_trade/qmt_account_check_account.csv
reports/live_trade/qmt_account_check_positions.csv
reports/live_trade/qmt_account_check_orders.csv
reports/live_trade/qmt_account_check_trades.csv
```

## 第二步：生成 A+B+C 每日计划单

```bash
.venv/bin/python -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10
```

重点看：

```text
reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv
```

如果当天没有计划单，则不应该下单。

## 第三步：实盘预览

如果账户还没审批通过，可以先用模拟行情验证计划单到实盘预览的流程：

```bash
.venv/bin/python -B scripts/mock_live_order_preview.py --planned-orders latest --available-cash 50000
```

正式 QMT 预览使用：

```bash
.venv/bin/python -B scripts/preview_live_orders.py --planned-orders latest
```

预览会检查：

- QMT 实时行情是否可取
- 是否涨停无法买入
- 是否跌停无法卖出
- 是否资金不足
- 是否超过单笔金额上限
- 是否有重复活跃委托
- 是否不是 100 股整数倍

输出：

```text
reports/live_trade/qmt_live_order_preview.csv
```

只有 `validation_status=PASS` 的订单才可能进入真实提交。

## 第四步：真实下单闸门

真实下单必须同时满足：

```text
trade_mode = live
broker_adapter_enabled = true
qmt_enabled = true
broker.enabled = true
live_trade.enabled = true
live_trade.real_order_enabled = true
命令行 --confirm 文本完全匹配
```

默认配置不会真实下单。

真实提交命令格式：

```bash
.venv/bin/python -B scripts/submit_live_orders.py \
  --preview reports/live_trade/qmt_live_order_preview.csv \
  --confirm A_SYSTEM_REAL_ORDER_CONFIRMED
```

第一次只能用小资金和 100 股测试，不要直接满仓。
