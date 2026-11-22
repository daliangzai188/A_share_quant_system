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
