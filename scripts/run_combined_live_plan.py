"""
D>A>E>C>N 组合实盘计划生成（M已退役）。

只生成组合状态机报告，不提交真实委托。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.combined_live_engine import CombinedLiveEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 D>A>E>C>N 组合实盘计划。")
    parser.add_argument("--config", default="config/config.json", help="运行时配置文件。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = CombinedLiveEngine(args.config)
    paths = engine.write_plan()
    print("D>A>E>C>N 组合实盘计划已生成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
