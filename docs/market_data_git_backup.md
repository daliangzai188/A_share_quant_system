# 必需行情进入 Git 与换机克隆验收

用户要求：必需数据与代码一起提交，换电脑可一次克隆恢复。仓库设置保持现状。
本流程不变更策略、腿序、仓位或交易权限，也不启动交易。

## 文件与方法

| 文件 | 位置 / 方法 | 操作与原因 |
|---|---|---|
| `.gitignore` | 原始数据段、文件末尾 | 替换日线/基本面/涨停 CSV 忽略规则，新增复权因子、分钟行情和正式五年研究输入白名单；防止只提交代码却丢掉运行输入 |
| `.gitattributes` | 原具名参考输入规则之后 | 新增行情 CSV 的 `filter=lfs diff=lfs merge=lfs -text`，大文件进入 LFS 且保持原始字节；原小型冻结参考文件仍保留普通 Git |
| `config/market_data_backup.json` | `daily_groups/required_files/optional_market_patterns` | 新增可恢复范围与字段清单，配置不包含凭据 |
| `scripts/market_data_backup.py` | `audit` | 新增逐开市日覆盖、非空、字段、日期、重复键检查及 SHA-256 清单；早期市场涨停计数仍缺失时返回 `INCOMPLETE` |
| 同文件 | `main` 的 `create` 分支 | 新增完整验收后原子生成清单；不完整时不覆盖原有成功清单 |
| 同文件 | `verify/file_record/safe_path` | 新增克隆后的字节哈希、LFS实体、路径边界检查；只有指针时提示 `git lfs pull` |
| `tests/test_market_data_backup.py` | 各测试方法 | 新增复制恢复、哈希篡改、缺日、空池、指针、早期计数缺失、秘密排除和真实Git规则验证 |
| `README.md`、重建说明 | 克隆说明 | 替换“历史数据另行迁移”的说明，增加 LFS 拉取和验收命令 |

完整可运行代码保存在上述脚本中，不需要手工拼接片段。全量测试仍只在同步目录外导出副本运行。

## 保存什么

- 逐日日线、每日基本面、复权因子和完整涨停明细；原始价格与复权因子配套保存，收益研究按既有复权规则计算。
- 交易日历、含退市状态的股票基本表、成交概率参考表。
- 正式五年研究底座，尤其是有来源的早期历史市场情绪和严格因子池；已有的分钟行情也放行，未采集的分钟或 L2 不能凭清单冒充已具备。
- 已有的清洗日线分片、历史情绪、动态因子和严格成交概率输入。

`.env`、Token、Bark Key、券商账号、持仓/成交/执行意图、运行 SQLite、私有 QMT 执行账本不进入本清单，沿用单独受控迁移。
Git忽略与Syncthing忽略用途不同。当前 Mac `sendonly` 不会把 Windows 新增文件送回 Mac；在 Windows 采集后，必须将验收过的行情副本受控复制到准备提交的仓库，不能仅凭同步网页“最新”判断已备份。

## 首次与增量备份

先在隔离目录补齐历史并通过质量及来源口径验收。大文件安装Git LFS后纳入仓库，历史数据按日保存，避免每次更新都上传一份巨大聚合文件。

```sh
git lfs install --local
python3 scripts/market_data_backup.py audit --end-date 20261008
python3 scripts/market_data_backup.py create --end-date 20261008
```

Windows相同命令使用 `py -3.11 -X utf8` 替换 `python3`。日期必须填写实际最后收盘日。
`BACKUP_COMPLETE`要求请求范围内日线/基本面/复权因子完整、正式起点之后涨停明细完整、所需参考文件存在且非空、有来源的历史情绪表覆盖完整日期。
这只是可恢复性检查；数值质量、来源差异、金额单位和策略认证仍按原流程验收。
早期涨停明细API不能提供时，不缩短停手回放历史，不补假计数。先修复真实输入，失败时保持关闭开仓。

检查待提交清单，只加入本次已验收的市场数据与备份清单；提交后推送：

```sh
git add .gitattributes .gitignore config/market_data_backup.json scripts/market_data_backup.py
git add data/raw/daily data/raw/daily_basic data/raw/adj_factor data/raw/limit_list
git add data/raw/trade_calendar.csv data/raw/stock_basic/stock_basic_all.csv
git add data/processed/fill_rate_table.csv data/processed/fill_rate_fallback.csv
git add data/research/five_year_strict data/market_backup/manifest.json
git diff --cached --stat
git lfs fsck
git commit -m "backup: retain verified market inputs for machine rebuild"
git push
```

已采集的分钟目录和其他允许的清洗输入按实际存在的文件加入同一提交。没有文件的目录无需强制 `git add`。
不要用 `git add -f` 放行账户文件，也不要对缺失数据写一个假成功清单。
只有 `git push` 完成、LFS对象上传成功，才能称为远端已备份；本机文件存在或 `git commit` 完成均不等于云端已具备数据。
GitHub的LFS存储和下载量另计，上传前核对实际体积及账户剩余额度；不自动开启付费预算。

## 换电脑

在新电脑安装 Git、Git LFS、Python，并配置自己对现有仓库的访问方式：

```sh
git lfs install
git clone git@github.com:daliangzai188/A_share_quant_system.git A_System
cd A_System
git lfs pull
python3 scripts/market_data_backup.py verify
```

Windows验收为：

```powershell
py -3.11 -X utf8 scripts\market_data_backup.py verify
```

必须输出 `RESTORE_VERIFIED`，退出码为0。只有Git LFS指针、缺日、空表、重复键、哈希不符、清单不存在均失败。
清单不存在说明该版本尚未完成完整行情备份，不能认为克隆已经恢复历史。清单日期是备份截止日；晚于该日期的数据正常增量采集，并重新验收、提交和推送。

行情恢复成功后，按[新电脑重建说明](new_mac_rebuild_migration.md)填写本机凭据、部署只读 QMT、核对运行账本及恢复门禁。正式配置可能为live，不直接运行交易启动器。恢复实盘仍先小资金验证。
