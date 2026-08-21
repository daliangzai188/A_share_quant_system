from __future__ import annotations

"""重建五年严格as-of底座并运行四腿嵌套walk-forward研究。"""

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.five_year_research import FiveYearResearchDatasetBuilder  # noqa: E402
from src.nested_walk_forward import NestedWalkForwardResearch  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="五年严格as-of数据重建与嵌套walk-forward研究（永不修改实盘）"
    )
    result.add_argument("--build-data", action="store_true", help="重建独立五年研究底座")
    result.add_argument("--optimize", action="store_true", help="运行D/A/E/C嵌套walk-forward")
    result.add_argument("--all", action="store_true", help="依次执行数据重建与优化")
    result.add_argument("--start-date", default="20190101")
    result.add_argument("--end-date", default=None)
    result.add_argument(
        "--overwrite-data",
        action="store_true",
        help="允许覆盖data/research/five_year_strict；不影响data/processed",
    )
    result.add_argument(
        "--research-config",
        default="config/five_year_strategy_research.json",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    build_data = bool(args.build_data or args.all)
    optimize = bool(args.optimize or args.all)
    if not build_data and not optimize:
        raise SystemExit("必须指定 --build-data、--optimize 或 --all")

    research_config_path = Path(args.research_config)
    if not research_config_path.is_absolute():
        research_config_path = ROOT / research_config_path

    if build_data:
        import json

        research_config = json.loads(research_config_path.read_text(encoding="utf-8"))
        data_config = research_config["data"]
        builder = FiveYearResearchDatasetBuilder(
            research_root=data_config["research_root"]
        )
        manifest = builder.build(
            start_date=args.start_date,
            end_date=args.end_date,
            planned_buy_amount=float(data_config["planned_buy_amount"]),
            overwrite=bool(args.overwrite_data),
        )
        print(f"FIVE_YEAR_DATASET_READY {manifest}", flush=True)

    if optimize:
        research = NestedWalkForwardResearch(
            research_config_path=research_config_path
        )
        summary = research.run()
        print(f"FIVE_YEAR_RESEARCH_READY {summary}", flush=True)
        print("LIVE_RELEASE_STATUS NOT_LIVE_RELEASEABLE", flush=True)


if __name__ == "__main__":
    main()
