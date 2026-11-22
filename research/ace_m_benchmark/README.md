# ACE+M 上线基准回放

`docs/equity_curve_stop.md` 第九节“基准记录”里 ACE+M 那一行（总复利295,346x、样本内月均30.89%、样本外月均7.47%、
最大回撤−31.7%）就是这个脚本算的。年度复核“先复现最新一行”、M 的去留（ACE+M 对比只用 ACE）都用它。

2026-11-22 从 `~/A_System_复核/2026-11-11_M大法研究/scripts/` 原样移入 git（`engine.py`、`m_plans.py`、`acde_slots.py`
是它依赖的研究代码），只改了 `m14_verify.py` 里两处路径：

- 冻结数据仍在 git 外（纯数据）：环境变量 `M_RESEARCH_DATA`，默认 `~/A_System_复核/2026-11-11_M大法研究`
  （用到 `panel/`、`fetched_20261110/`、`ace/plans.pkl`、`ace/shadow_nav.csv`、`out/m7/f9_rows.pkl`）；
- 交易规则函数（下单股数、板块、印花税、方案甲停手判定）改用本仓库 `src/`：环境变量 `ACE_M_CODE_ROOT`，默认本仓库根目录
  （原来用的是 `~/A_System_复核/2026-11-11_甲触发条件核对/code` 里的代码副本）。

移入时核对：用本仓库 `src/` 跑出的 6 个结果文件与原来 `out/m7/` 里的逐字节相同。

这份数据的备份（2026-11-22起）：只备份清单 `inputs.txt` 里的 6 个文件（约936MB），用
`python tools/data_snapshot.py --set ace_m_benchmark snapshot | verify | restore-check | restore 快照名 新目录`，快照在
`~/A_System_data_snapshots/ace_m_benchmark/`。2026-11-22拍了第一份，从快照恢复到新目录、用 `M_RESEARCH_DATA` 指过去重跑，
6 个结果文件与原来逐字节相同。`tests/test_data_snapshot.py` 核对清单和本脚本用到的文件相同，改了脚本的输入要同时改清单。

运行（约3秒，输出目录要先建好）：

```bash
PYTHONDONTWRITEBYTECODE=1 ~/A_System_Rebuild/main_venv/bin/python research/ace_m_benchmark/m14_verify.py 输出目录
```

## 输入是怎么来的（2026-11-23 补齐脚本）

| 输入 | 11-11 怎么来的 | 脚本（本目录） |
|---|---|---|
| `panel/panel_20261110.npz` | 当时同步工作区的 `data/raw`（日线、每日指标、复权因子、涨停池）加虚拟机补取的 `fetched_20261110/`（2026-10-09~11-10，同一天以后者为准） | `build_panel.py` |
| `panel/st_20261110.npz` | 按 `fetched_20261110/namechange.csv`（更名记录）标出 ST、退市整理的日子 | `build_st_mask.py` |
| `fetched_20261110/` | 虚拟机用 Tushare 取：每天的日线、每日指标、复权因子、涨停池，加一次股票基本表（含退市）和更名记录，打包传回 Mac 再解压。生产数据里没有更名记录，所以 ST 标记离不开这一步 | `fetch_research_data.py`（虚拟机上跑）、`extract_fetch.py` |
| `ace/shadow_nav.csv`、`ace/plans.pkl` | 用 11-11 冻结代码（`~/A_System_复核/2026-11-11_甲触发条件核对/code`，含 `data/raw`）和它的研究池（同目录 `dataset/`）跑生产的影子账函数 | `replay_acde.py` |
| `out/m7/f9_rows.pkl` | M 研究时规则挖掘脚本顺带存的候选特征表。回放只拿它打印一行“M 候选重建 N 只、与研究版相同”，不影响任何结果 | 没有搬（挖掘脚本留在研究目录） |

11-11 当时的命令（目录是当时的）：

```bash
python build_panel.py panel/panel_20261110.npz ~/Desktop/A_System/data/raw fetched_20261110
python build_st_mask.py panel/panel_20261110.npz fetched_20261110/namechange.csv panel/st_20261110.npz
python replay_acde.py 冻结代码 工作目录 20261110 --reuse     # 结果在 工作目录/replay/
```

`build_panel.py`、`build_st_mask.py` 从 `~/A_System_复核/2026-11-11_M大法研究/scripts/` 移入，`build_panel.py` 只加了 `--end`；
`replay_acde.py`、`fetch_research_data.py`、`extract_fetch.py` 的原文件 11-22 随回收站清空，从 11-11 的会话记录原样恢复，
`replay_acde.py` 只在最后加了按本脚本格式写 `plans.pkl`。

**2026-11-23 核对**：用本目录的脚本把输入全部重建一遍——面板用现在同步工作区的 `data/raw` 加 `--end 20261110` 和原来的
`fetched_20261110/`，ACE 用冻结代码和冻结研究池——面板 24 个数组和 ST 标记与原文件内容完全相同（npz 里带写入时间，所以不比文件字节），
影子净值逐字节相同，计划与原文件里 3 个现行方案相同；拼成一份输入再跑本脚本，6 个结果文件与原来逐字节相同。
另外不加 `--reuse`、用冻结代码里的 `data/raw` 从头建研究池（约2.5分钟），`strict_feature_pool.csv`、`market_sentiment.csv`
与冻结研究池逐字节相同，回放结果也相同。

## 用新数据重跑（年度复核）

没有实际用新数据跑过；虚拟机取数这一步 11-11 之后也没再跑过。截止日记作 END：

1. 冻结代码：在同步目录外 `git archive` 导出当前版本，`data/raw` 用同步工作区那份（`cp -cR` 克隆，不占空间）。
2. 取更名记录和最新股票基本表：Mac 上把 `qmt_native/receiver.py` 复制到同步目录外启动（它写到自己旁边的 `received/`，
   放在同步目录里收到的包会同步到虚拟机），虚拟机上 `py -3.11 C:\A_System\research\ace_m_benchmark\fetch_research_data.py END END`
   （日线等 `data/raw` 已有，开始日填 END 只取一天；更名记录和股票基本表每次都全量取），用完停掉接收服务，
   再 `python extract_fetch.py 包 fetched_END`。
3. 建面板：`build_panel.py 面板.npz 同步工作区/data/raw fetched_END --end END`，`build_st_mask.py 面板.npz fetched_END/namechange.csv st.npz`。
4. ACE 输入：`replay_acde.py 冻结代码 工作目录 END`（不加 `--reuse`，先建研究池；截到 11-10 的数据约3分钟）。
5. 本脚本里截止日和文件名是写死的：输入目录里按原文件名放（`panel/panel_20261110.npz` 等，内容是新数据），`out/m7/f9_rows.pkl`
   沿用原文件（只用于那行核对，新数据下会显示有差异，可以不管）；`WIN` 的时段划分和 `simulate('20191202', '20261110', …)`
   的截止日要先改。然后 `M_RESEARCH_DATA` 指向这个目录运行。

## 还没顺手改的

`src/ace_m_replay.py`、`src/portfolio_ace_m.py` 开头的说明里，研究回放还写着 `~/A_System_复核/2026-11-11_M大法研究/scripts/m14_verify.py`
（那份文件还在，没有指错）。这两个文件在 QMT 核心包和方案甲认证清单里，改一个字也要重新认证、换 QMT，所以等下次本来就要改它们时
顺手改成本目录。
