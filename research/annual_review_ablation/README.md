# 年度复核：条件有效性检验

`docs/equity_curve_stop.md` 第六节“条件有效性检验”、第八节第2小节年度复核第5步用这里的脚本：
对现行每个条件逐个去掉检验，并对 A/C/E/M 重做规律性检验。只用来找问题，不用来挑方案；
判定规则写在各脚本开头，看结果之前就定好。

2026-11-22 从 git 外搬进来（用户要求：会影响以后策略方向的东西都放进 git），只改了两处：

- 路径集中到 `_paths.py`，用环境变量指定（下面的表）；输出目录改成脚本的第一个参数，不再写到脚本所在目录；
- 冻结代码里的研究模块用 `frozen_module()` 找：2026-11-14 起在 `research/`，以前在 `scripts/` 或 `src/`。

来源：`~/A_System_复核/2026-11-13_逐个去掉条件检验/`（第二轮逐个去掉）、`2026-11-13_全腿规律性检验/`、
`2026-11-13_A腿策略复核/inputs/dump_research_inputs.py`。第三轮“简化重建”（`rebuild_*`、`gap_check.py`）是
11-13 一次性的研究，没有搬，留在原目录；第一次检验的结论也在原目录的 `结论.md`。

## 运行

所有脚本用同一个输出目录，按顺序跑（后面的读前面的结果）：

| 顺序 | 脚本 | 做什么 | 产出（在输出目录里） |
|---|---|---|---|
| 1 | `dump_research_inputs.py` | 导出 ACE 计划、M 候选和逐笔结果（来自 M 研究面板） | `research_inputs.pkl` |
| 2 | `ablate_ace.py` | A/C/E 每次去掉一个条件，其余不动（约3分钟） | `ablate_ace.pkl`、`empty_d_events.csv` |
| 3 | `ablate_m.py` | M 每次去掉一个条件 | `ablate_m.pkl` |
| 4 | `analyze.py` | 按事先写死的规则判定有用 / 不确定 / 拟合嫌疑 | `analyze.json`，并打印判定表 |
| 5 | `zero_effect_check.py`、`redundancy_check.py` | 核对“去掉后结果不变”的条件是不是逻辑上多余 | 打印 |
| 6 | `audit_ace.py` | A/C/E 规律性检验 | `audit_ace.json`、`forward_hold2/3.pkl`（缓存） |
| 7 | `audit_m.py` | M 规律性检验 | 打印 |

```bash
PYTHONDONTWRITEBYTECODE=1 ~/A_System_Rebuild/main_venv/bin/python research/annual_review_ablation/ablate_ace.py 输出目录
```

| 环境变量 | 含义 | 默认（2026-11-13 第一次检验用的） |
|---|---|---|
| `ABLATION_CODE_ROOT` | 冻结的整份代码，含 `config/`、`data/raw/`（`audit_ace.py` 要用日线和复权因子） | `~/A_System_复核/2026-11-11_甲触发条件核对/code` |
| `ABLATION_DATASET` | 冻结的研究池：`strict_feature_pool.csv`、`market_sentiment.csv` | `ABLATION_CODE_ROOT` 旁边的 `dataset/` |
| `ABLATION_END` | 数据截止日 | `20261110` |
| `M_RESEARCH_DATA` | M 的研究面板（见 `research/ace_m_benchmark/README.md`） | `~/A_System_复核/2026-11-11_M大法研究` |

## 搬进来时的核对（2026-11-22）

按上面的顺序完整重跑两遍，和 11-13 原目录里的结果逐个比：

1. 默认冻结代码（11-11 的旧目录结构）；
2. 现在仓库的代码（`git archive` 导出、研究模块在 `research/` 的新目录结构；日线和复权因子链接到同一份冻结数据）。

两遍都一样：`research_inputs.pkl`、`ablate_ace.pkl`、`ablate_m.pkl`、`analyze.json`、`audit_ace.json`、`forward_hold2/3.pkl`、
`empty_d_events.csv` 和 `zero_effect_check`、`redundancy_check`、`audit_ace`、`ablate_ace`（去掉耗时）的打印输出，逐字节相同。
`audit_m` 的打印多出第 1 行，是基准回放脚本开头自己打印的“M 候选重建 324 只、与研究版相同”，11-13 保存的输出里没有这一行；
其余逐字节相同。

## 下次用之前要注意

- 逐个去掉的条件清单（`ablate_ace.py`、`ablate_m.py`、`audit_*.py` 里）是按 2026-11-13 的现行规则写的；规则改过，先改清单。
- M 的两项检验和 `research_inputs.pkl` 用的是 M 研究面板（行情截至 2026-11-10）。要用新数据，得先重建面板；
  建面板的脚本（`build_panel.py`、`build_st_mask.py` 等）还在 `~/A_System_复核/2026-11-11_M大法研究/scripts/`，没有搬进 git。
