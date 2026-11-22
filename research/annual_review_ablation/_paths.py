"""年度复核“条件有效性检验”脚本共用的路径（2026-11-22从~/A_System_复核移入git，见同目录README.md）。

冻结的代码和数据在git外（纯数据），用环境变量指定；默认是2026-11-13第一次检验用的那一份：
  ABLATION_CODE_ROOT  冻结的整份代码（含config/、data/raw/），默认 ~/A_System_复核/2026-11-11_甲触发条件核对/code
  ABLATION_DATASET    冻结的研究池（strict_feature_pool.csv、market_sentiment.csv），默认 ABLATION_CODE_ROOT 旁边的 dataset/
  ABLATION_END        数据截止日，默认 20261110
  M_RESEARCH_DATA     M的研究面板（见 research/ace_m_benchmark/README.md）
每个脚本的第一个参数是输出目录；所有脚本用同一个输出目录（后面的脚本读前面脚本的结果）。
"""
import importlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FROZEN = Path(os.path.expanduser(os.environ.get(
    "ABLATION_CODE_ROOT", "~/A_System_复核/2026-11-11_甲触发条件核对/code"))).resolve()
DS = Path(os.path.expanduser(os.environ.get("ABLATION_DATASET", str(FROZEN.parent / "dataset")))).resolve()
END = os.environ.get("ABLATION_END", "20261110")
M14 = REPO / "research" / "ace_m_benchmark" / "m14_verify.py"


def out_dir() -> Path:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python %s 输出目录" % sys.argv[0])
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def enter_frozen() -> None:
    """之后的 src.* 导入和相对路径都来自冻结代码。"""
    sys.path.insert(0, str(FROZEN))
    os.chdir(FROZEN)


def frozen_module(name: str):
    """冻结代码里的研究模块：2026-11-14起在 research/，之前在 scripts/ 或 src/。"""
    for package in ("research", "scripts", "src"):
        if (FROZEN / package / (name + ".py")).exists():
            return importlib.import_module(package + "." + name)
    raise ImportError("冻结代码里找不到 %s：%s" % (name, FROZEN))
