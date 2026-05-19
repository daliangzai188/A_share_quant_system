from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config
from src.utils.logger import setup_logger
from src.whitelist import CandidatePoolGenerator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据组合因子生成每日候选股票池。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    generator = CandidatePoolGenerator(config_path=args.config)
    output_path = generator.generate()
    print(f"候选股票池生成完成: {output_path}")


if __name__ == "__main__":
    main()
