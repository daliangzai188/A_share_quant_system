from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_emotion import MarketEmotionBuilder
from src.theme_heat import ThemeHeatBuilder
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建动态市场情绪和题材热度特征。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--skip-market-emotion", action="store_true", help="跳过市场情绪特征。")
    parser.add_argument("--skip-theme-heat", action="store_true", help="跳过题材热度特征。")
    parser.add_argument("--start-date", help="开始日期 YYYYMMDD。传入后按日线分片增量更新，避免读取 daily_merged.csv 大文件。")
    parser.add_argument("--end-date", help="结束日期 YYYYMMDD。传入后按日线分片增量更新，避免读取 daily_merged.csv 大文件。")
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
    outputs = {}
    if not args.skip_market_emotion:
        outputs["market_emotion"] = MarketEmotionBuilder(config_path=args.config).build(
            start_date=args.start_date,
            end_date=args.end_date,
        )
    if not args.skip_theme_heat:
        outputs["theme_heat"] = ThemeHeatBuilder(config_path=args.config).build()

    print("动态特征构建完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
