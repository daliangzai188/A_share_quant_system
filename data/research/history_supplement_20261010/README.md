# 2026-10-10 历史数据补充核验（冻结研究附件）

本次保存六个补采脚本、940行新增核验原件和逐项审计依据；另保留此前已验证的2018-12-24衍生量比候选与生成脚本。所有结果都用于追溯研究，不替代正式行情输入。完整复核仍为 **DATA_INCOMPLETE / NOT_READY**，正式早期基准认证仍未通过。此次提交没有修改方案甲规则、A>C>E>D腿序、仓位、停手参数或正式配置。

## 本次新增的证据

| 项目 | 数量 / 结果 |
|---|---|
| 本轮直接补采的核验原件 | 940行：备用日线2、上市资料1、分钟行情241、交易日历696；不等于填补940个旧缺口 |
| 早期目标股票日 | 20,552，范围2018-04-02至2019-11-27，主键唯一 |
| 量比不适用明细 | 837个股票日、186只股票；上市后此前有成交日不足5个，继续保留空值 |
| 官方量比原值独立对照 | 19,673条全部通过两位小数舍入容差；排除16个先前衍生值 |
| 本轮新增量比数值 | 0 |
| 此前单日衍生候选 | 2018-12-24的516个数值，已标记衍生来源，仍仅存研究目录 |
| 历史依赖清单 | 867份日线 / 每日指标原件，保留完整路径与SHA256 |
| 尚未认证的价格差异 | 4,865个股票日的OHLC差异，其中4,765个最低价差异 |
| 早期收盘价差异 | 2例与历史沪市量权收盘口径相容；缺逐笔时戳，尚未严格复原 |
| 真实历史成交 / 净值账本 | 尚未恢复；本次未执行券商历史查询 |
| 早期封单与精确开板次数 | 未认证；不能统一除1000或用分钟收盘价代替逐笔开板判断 |
| 新购买数据 / 委托 / 撤单 | 均为0 |

837处空值的逐项依据在 [IPO量比明细](continued/evidence/ipo_ratio_not_applicable.csv)，官方原值对照在 [控制明细](continued/evidence/remaining_ratio_controls.csv)，价格问题在 [差异清单](continued/evidence/remaining_price_discrepancies.csv)。这些清单不会放宽现行IPO或缺失因子过滤。

## 文件、方法与修改位置

本目录中下列文件均为新增完整文件，没有替换或删除生产方法。`continued`中的六个脚本按前次验收的字节和哈希保存；此前单日候选的脚本与验收也保留原件。

| 文件 | 方法 | 新增内容及原因 |
|---|---|---|
| `continued/legacy_tick_sources.py` | `urls / parse_text / daily_compare / fetch / main` | 查询旧SDK声明的公开分笔下载入口，识别服务下线、无数据及错误页，默认重用原回包 |
| `continued/tdx_history_sources.py` | `select_server / query / main` | 只读公开行情连接控制、分页和重复页核验；本次4个连接控制均失败，未查询历史分笔 |
| `continued/tdx_archive_sources.py` | `download / query_market_samples / main` | 按官方公开下载索引取样并验证ZIP/CRC；本次4个2018压缩包404、近期对照超时 |
| `continued/existing_permission_checks.py` | `capture / closing_diagnostics / ipo_diagnostic / main` | 用现有Tushare权限取针对性原件，核对历史收盘口径和上市首日量比 |
| `continued/supplement_remaining_ratios.py` | `calendar / source_path / analyze / build / main` | 用完整前史和此前5个成交日均量检查缺口；当日量在计算后加入历史，官方对照失败就不补值 |
| `continued/verify_supplement.py` | `verify_file / main` | 原补采验收，核对867个输入、上市日期、无未来数据、原件哈希、官方舍入控制及未完成门禁 |
| `validate_and_supplement_early_basic.py` | `main` | 保留此前单日衍生候选的生成和验证源码，防止候选在后续分析中丢失来源 |
| `verify_snapshot.py` | `verify / main` | 新增标准库只读验收入口，核对已提交附件的哈希、主键、空值依据、对照容差及未完成门禁 |

`snapshot_manifest.json`登记本次保存的原件和源码哈希。根目录`.gitignore`只放行本附件具名CSV，`.gitattributes`为附件禁用换行转换，避免Windows改变原件哈希；其中两份公开回包保留原CRLF，目标清单保留供应商股票名尾部空格，空白检查仅针对这三个原件登记相应规则。`docs/equity_curve_stop.md`仅在第九节“复核记录”追加本次结论，基准收益数字和当前版本不变。

## 运行和验证

在同步目录外的导出副本中，使用Python 3.10+运行下列命令。它只使用标准库、只读附件；成功输出 `PASS_FROZEN_RESEARCH_SNAPSHOT_CHECKS`，同时保留 `formal_baseline_certified=false` 与 `full_review_status=DATA_INCOMPLETE_NOT_READY`。

```sh
python3 data/research/history_supplement_20261010/verify_snapshot.py
```

预期输出：50份原始冻结文件加校验入口与本说明全部哈希通过，20,552个目标股票日、837处不适用、19,673条官方对照、4,765处最低价差异。附件校验通过只说明证据完整，不表示策略认证通过。

在本机历史原件目录齐全时，可以额外只读核对867个外部依赖和原股票上市资料：

```sh
python3 data/research/history_supplement_20261010/verify_snapshot.py --check-external-sources
```

外部核对失败时先检查原目录及SHA256，不能把缺失原件标成已复现。默认离线校验不读取这些外部目录，也不会重新抓取行情或计算收益。

六个原补采脚本仍保留本次实际运行时的本机路径、外部OfficialClient导入和隔离pytdx依赖；此处保存的是完整审计源码。需重新执行采集时，在同步目录外准备相同依赖和原件，具体运行方式见 [补采时原说明](continued/继续补充说明.txt)。干净克隆可以独立核验本附件，不能直接重跑全部历史采集。`tdx_history_sources.py`依赖的pytdx源码没有安装到正式环境，也没有在此重复入库。

原验收记录时间为2026-10-10 05:55:47 UTC；其中`production_worktree_clean`、`production_writes`及原说明的“未提交”描述的是补采完成、提交以前的现场，按原字节保存。此后本次Git提交仅添加研究附件与复核文档。

本附件不包含867份外部原件的副本，不包含原始账户账本、账号或密码；付费候选页面、第三方脚本和依赖源码也不在本次提交范围。完整外部交付包留在本机，其SHA256为`381a928c97313e75666cde0f8b2f6037c8e69d781614d132013761feeb1d79cf`。

后续完整复核需要补回真实历史成交与净值账本，并完成早期价格和封单认证；恢复实盘前仍先做模拟及小资金验证。
