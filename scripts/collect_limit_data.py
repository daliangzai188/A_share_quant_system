from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config
from src.utils.time_utils import yesterday_beijing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集 A股涨停池数据 limit_list_d。")
    parser.add_argument("--start-date", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的本地 CSV。")
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="要求 end-date 必须采集到 limit_list_d 完整口径，否则报错。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    start_date = args.start_date or config["data"]["start_date"]
    end_date = args.end_date or yesterday_yyyymmdd()
    ensure_tushare_token(config)

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    data_source = TushareDataSource(config_path=args.config)
    collector = DataCollector(data_source=data_source, config_path=args.config)
    collector.collect_limit_data(start_date=start_date, end_date=end_date, overwrite=args.overwrite)
    if args.require_full:
        limit_path = collector.limit_list_dir / f"{end_date}.csv"
        if not csv_has_full_limit_data(limit_path):
            raise RuntimeError(
                f"{end_date} 未采集到 limit_list_d 完整涨停池数据: {limit_path}。"
                "请不要用 stk_limit 基础口径替代历史最佳策略口径。"
            )


def ensure_tushare_token(config: dict) -> None:
    token_env = config.get("data_source", {}).get("token_env", "TUSHARE_TOKEN")
    if os.getenv(token_env):
        return
    stored = str(config.get("data_source", {}).get("token", "")).strip()
    if stored:
        os.environ[token_env] = stored
        return
    token = getpass.getpass(f"请输入 Tushare Pro Token（不会显示，且不会保存到本地）: ").strip()
    if not token:
        raise RuntimeError("Tushare Token 不能为空。")
    os.environ[token_env] = token


def yesterday_yyyymmdd() -> str:
    return yesterday_beijing().strftime("%Y%m%d")


def csv_has_full_limit_data(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        import pandas as pd

        data = pd.read_csv(path, dtype=str, nrows=20)
    except Exception:
        return False
    if data.empty:
        return False
    if "limit_data_quality" not in data.columns:
        return True
    return bool(data["limit_data_quality"].fillna("full").astype(str).eq("full").all())


if __name__ == "__main__":
    main()
