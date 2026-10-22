# A_System QMT 原生版

2026-10-23 起国金禁止 QMT 内置 Python 读写文件和联网。本目录把仓库里的整套系统（守护进程、
qmt_inner 执行引擎、全部收盘流水线）不改策略逻辑地打包成**一个 QMT 策略文件**，在 QMT 内置
Python 里全自动运行。切换步骤见 `切换上线手册_QMT原生版.md`。

## 目录

| 路径 | 内容 |
|---|---|
| `build_inqmt.py` | 打包器：仓库代码 → `inqmt/qn_core_full.py`（AST 兼容转换到 Python 3.6 / pandas 0.22） |
| `build_qmt_strategy.py` | 生成 QMT 策略文件（`shadow` 模拟账户 / `live` 实盘），内含种子文件 |
| `build_eq_seed.py` | 第⑬步增量停手的种子（来自生产⑬数据集） |
| `prepare_live.sh` | 晚上切换用：解包虚拟机上传 → 种子 → ⑬与生产逐位核对 → 生成实盘版 |
| `inqmt/qn_runtime.py` | 内存文件系统、模块加载与兼容层、线程生命周期、推送出口 |
| `inqmt/qn_host.py` | QMT 宿主：状态存储（QMT 键值库）、守护线程、接管保护、夜间规则、导出 |
| `inqmt/qn_paper_broker.py` | 影子模式的模拟券商 |
| `inqmt/qn_native.py`、`native_limit_reconstruct.py` | QMT 原生行情：日线、涨停池、连板 |
| `inqmt/replacements/` | 替换的流水线步骤：①原生采集、⑤跳过、⑬增量停手 |
| `inqmt/test_*.py`、`inqmt/run_regression.sh` | 回归测试（Python 3.6 + 模拟 QMT） |
| `vm_scripts/` | 虚拟机脚本：上传、推送转发、QMT 外检查、计划任务安装（部署副本在仓库 `qmt_native_phase0/`） |

## 不在 Git 里的东西

- **QMT 策略文件**（`A_SYSTEM_QMT_NATIVE_*.py`）：内含资金账号和生产状态，按仓库规则不提交，
  用 `build_qmt_strategy.py` / `prepare_live.sh` 重新生成。
- **账号与模拟资金**：`local.env`（`QMT_ACCOUNT_ID=…`、`QN_PAPER_CASH=…`），本机自建。
- **数据与环境**（工作目录里）：`venv/`、`tools/qmt36`（x86_64 Python 3.6.8 + pandas 0.22，
  与 QMT 内置一致，用于测试）、`vm_raw*`、`vm_state`、`eqseed_*`、`eq_full_*`、`received/`。
  这些由虚拟机上传脚本（`vm_scripts/upload_*.ps1`、`state_inventory.ps1`）重新取得。

## 常用命令（在工作目录执行）

```bash
venv/bin/python build_inqmt.py /Users/eass/Desktop/A_System inqmt/qn_core_full.py full
QN_EQ_SEED=$PWD/eqseed_prod_full venv/bin/python build_qmt_strategy.py shadow "$QMT_ACCOUNT_ID" "$QN_PAPER_CASH" inqmt/A_SYSTEM_QMT_NATIVE_shadow.py
sh inqmt/run_regression.sh inqmt/A_SYSTEM_QMT_NATIVE_shadow.py inqmt/test_live_tmp.py inqmt/regress_out
sh prepare_live.sh 20261022
sh publish_to_a_system.sh "提交说明"
```

## 代码同步

开发在工作目录 `~/A_System_Rebuild/2026-10-19/qmt_native_full`（本地 Git）。`publish_to_a_system.sh`
把已提交的文件原样导出到仓库 `A_System/qmt_native/`，连同 `qmt_native_phase0/` 一起提交并推送到 GitHub。
换电脑时克隆仓库即可拿到全部代码。
