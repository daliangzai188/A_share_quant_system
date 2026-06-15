from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.fill_model import FillProbabilityEstimator
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量计算涨停池历史成交概率打标。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--planned-buy-amount", type=float, help="计划买入金额，单位元。不填则读取配置。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    fill_config = config.get("fill_model", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    planned_buy_amount = args.planned_buy_amount or float(fill_config.get("default_planned_buy_amount", 100000))
    estimator = FillProbabilityEstimator(config_path=args.config)
    output_path = estimator.score_limit_up_table(
        input_path=fill_config.get("input_limit_up_path", "data/processed/limit_up_merged.csv"),
        output_path=fill_config.get("output_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"),
        planned_buy_amount=planned_buy_amount,
    )

    print(f"涨停池成交概率打标完成: {output_path}")


if __name__ == "__main__":
    main()
