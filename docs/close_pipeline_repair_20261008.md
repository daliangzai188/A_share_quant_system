# 2026-10-08 换机后收盘流水线故障与历史数据恢复

本次属于程序缺陷修复和历史数据恢复，不改变正式方案甲规则。A>C>E>D、仓位、费用、滑点、T+1、MA60和滞后6个交易日保持原值。没有调用券商或发送委托。

## 根因

1. ⑪：新机的实盘成交表只有表头、没有完成交易。`completed_live_trades` 的空结果没有费用、净盈亏和净收益列，下游报告索取这些列时崩溃。零样本应输出 `INSUFFICIENT_SAMPLE`，不能伪造成交或回测交易替代实盘样本。
2. ⑬：新机只恢复了近期行情，影子账从2019年起完整回放所需的历史日线、基本面、涨停市场计数和原冻结认证底座缺失。程序报“缺少20190102涨停数”是缺真实运行输入，不能靠放宽停手门禁修复。
3. `limit_list_d` 的历史起点为20191128，重采该接口不能恢复前220个开市日；早期计数须用有来源的官方KPL榜单，并标记仅计数可用。
4. 官方涨停源有3条停牌占位记录（close=0、pct_chg=-100且时刻/封单均0），同日官方日线没有该代码。它们保留在原始CSV并标注来源异常，清洗时依据真实日线排除，不能计为可交易涨停。

## 完整文件与具体修改

| 文件 | 方法 / 位置 | 操作 | 原因 |
|---|---|---|---|
| `src/live_performance.py` | `completed_live_trades` 空结果返回之前 | 新增 `estimated_fees/net_pnl/net_return` 浮点空列 | 空成交报告保持完整schema |
| `src/execution_data_quality.py` | 明细构造和报告汇总 | 新增固定20列表头与0样本说明 | 无执行样本时仍可生成质量报告 |
| `src/data_cleaner.py` | `build_market_sentiment_row` 两处返回之前；新增 `_apply_historical_limit_count` | 新增早期来源可核验的全市场及分市场计数，未知执行字段为空 | 修复早期市场门禁输入，避免补造封单/炸板等信息 |
| 同文件 | `clean_limit_up_by_date` 读取raw之后 | 新增源占位检查和当日日线代码证据 | 保留raw并排除确实无报价的占位，不误删正常票 |
| `src/historical_limit_counts.py` | `load_historical_limit_count` | 新增日期、哈希、榜单与重算计数校验 | 早期计数可追溯 |
| `src/limit_source_quality.py` | `suspended_placeholder_mask` | 新增严格占位形态与缺失报价的联合判断 | 防止用不完整基本面误判停牌 |
| `src/equity_curve_stop_history.py` | `audit_history/require_history` | 新增完整历史覆盖前置门禁 | 缺日、空表、字段或源证据异常时先明确报错 |
| `scripts/update_equity_curve_stop.py` | `build_dataset` 构造Builder之前 | 新增 `require_history` | 防止只用最近行情重建错误影子净值；保留fail-closed |
| `scripts/certify_plan_jia_release.py` | `formal_context/run/CODE_FILES` | 新增正式D历史失败关闭上下文和数据相关代码哈希；替换通用D研究事件输入调用 | 正式认证D本来固定0笔，不依赖丢失且不使用的D研究事件；原锁定指标不动 |
| `scripts/rebuild_equity_curve_stop_inputs.py` | `validate/worker/main` | 新增隔离断点续传、schema/日期/重复键、原子落地、源缺口及哈希清单 | 原始数据必须可审计，重试不能写假空成功 |
| `scripts/rebuild_historical_limit_counts.py` | `main` | 新增官方早期榜单采集与断点续传 | 补足20190102到20191127计数 |
| `scripts/market_data_backup.py` | `audit/create/verify` | 新增必需行情清单及冷克隆验证 | 换电脑必须能恢复实际数据，不仅代码或LFS指针 |
| `.gitignore/.gitattributes/config/market_data_backup.json` | 指定行情白名单与LFS规则 | 替换必需行情忽略规则，新增源证据及冻结认证底座 | 数据随Git恢复，保留原始字节 |
| 配套测试 | 各测试方法 | 新增缺日/空样本/来源异常/哈希篡改等回归；调整原专用mock | 验证实际故障和失败关闭，全部在同步目录外运行 |

本次没有删除交易风控、账户账本或密钥保护。完整可运行代码保存在对应源码文件，不需要复制拼接代码片段。

## 数据边界

早期KPL计数恢复220天、12,647条非ST沪深涨停记录；20190102为31只。早期执行所需的封单、连板、炸板等未知，`counts_only` 不可用于重构执行模型。低涨幅历史S类股票和低价涨停的最小报价单位误差保留供应商真实结果。

北交所上市前的新三板行情可能映射为现行.BJ代码而缺昨收；以官方个股上市日期为界保留原始空值并登记，不填价、不丢整个沪深日期。现有20191128至20200420封单金额单位异常保护继续生效，不因本次恢复顺便改变模型。

真实.env、Token、Bark Key、券商账户、交易意图和实盘账本不进Git。Mac sendonly/Windows receiveonly，不能把Windows新数据自动回传当作Git已备份。本次锁屏期间只通过后台文件、网络和虚拟磁盘只读接口工作，没有解除锁屏或修改虚拟磁盘。

## 运行与验收

Windows日常⑪与⑬命令：

```powershell
py -3.11 -X utf8 scripts\report_rolling_live_performance.py
py -3.11 -X utf8 scripts\check_equity_curve_stop_history.py --signal-date 20261008
py -3.11 -X utf8 scripts\update_equity_curve_stop.py --signal-date 20261008
py -3.11 -X utf8 scripts\market_data_backup.py verify
```

Mac对应使用配置了requirements依赖的Python。完整冻结认证在同步外副本执行 `python scripts/certify_plan_jia_release.py`，必须按原锁定指标PASS，不改期望值让恢复结果过关。

零样本⑪应成功退出、生成报告并标 `INSUFFICIENT_SAMPLE`。历史检查应覆盖1881个开市日。⑬应写出真实20261009行动日判定及影子净值，停手或允许都必须来自原MA60/lag6规则。冷克隆执行 `git lfs pull` 后备份验证应为 `RESTORE_VERIFIED`，退出码0。

## 已完成的验证（其余关卡继续记录）

- 原始日线：1,881天、8,991,131行；每日基本面：1,881天、8,933,717行；复权因子：1,881天、9,172,242行；现代完整涨停源：1,661天、102,746行。所有日期经过字段、日期、重复键、数值和原始字节SHA-256校验，网络漏日已重试补齐。
- 全量测试：805项，跳过11项，失败0项；部署候选12个代码/测试文件逐字节匹配测试副本。
- 正式冻结认证：20260831输入独立重建，原锁定179笔、A92/C62/E25、7个停手日及全部收益/费用/回撤指标复现，证书PASS。没有修改锁定值，也没有变更策略规则；历史结果不是未来收益承诺。
- ⑪：虚拟机真实0成交输入验收成功，输出完整空样本报告，状态为INSUFFICIENT_SAMPLE。

**延长影子账、⑬决策、虚拟机完整送达与远端冷克隆仍在验收；全部通过后才确认整项完成。**

### 重采源值差异披露

原执行组合725个行动日的成交/未成交状态、股票、价格、费用、资金、收益逐字段一致，原影子净值与停手标记CSV逐字节一致。原锁定认证指标完全复现，但部分未执行静态计划的字节签名不完全一致：A一笔买卖复权因子均由4.7652变为官方现值4.765，买卖比例不变；一笔可成交金额差573.52元（总额约10.25亿元），不改变成交。E新增20260812/301520.SZ静态计划，但该日原/新组合均因已有持仓`SKIP_OCCUPIED`，没有交易。保存原始黄金产物和差异明细，未改锁定期望值或策略条件来强行通过。
