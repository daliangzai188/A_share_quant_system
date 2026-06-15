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
    parser = argparse.ArgumentParser(description="基于历史换手率表估算涨停板排队成交概率。")
    parser.add_argument("--ts-code", required=True, help="股票代码，例如 000001.SZ。")
    parser.add_argument("--trade-date", required=True, help="交易日期，格式 YYYYMMDD。")
    parser.add_argument("--limit-times", required=True, type=int, help="连板天数。")
    parser.add_argument("--board-type", required=True, help="板型：one_word / t_board / multi_open / unknown。")
    parser.add_argument("--first-time-bucket", required=True, help="首次涨停时间段。")
    parser.add_argument("--market-sentiment-level", required=True, help="市场情绪：weak / neutral / strong / very_strong。")
    parser.add_argument("--circ-mv", required=True, type=float, help="流通市值，单位万元，使用 Tushare circ_mv。")
    parser.add_argument("--current-queue-amount", required=True, type=float, help="当前排在前面的封单金额，单位元。")
    parser.add_argument("--planned-buy-amount", required=True, type=float, help="计划买入金额，单位元。")
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

    estimator = FillProbabilityEstimator(config_path=args.config)
    result = estimator.estimate(
        ts_code=args.ts_code,
        trade_date=args.trade_date,
        limit_times=args.limit_times,
        board_type=args.board_type,
        first_time_bucket=args.first_time_bucket,
        market_sentiment_level=args.market_sentiment_level,
        circ_mv=args.circ_mv,
        current_queue_amount=args.current_queue_amount,
        planned_buy_amount=args.planned_buy_amount,
    )

    print("成交概率估算结果：")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
