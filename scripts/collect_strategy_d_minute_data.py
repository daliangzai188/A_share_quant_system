"""
策略D（首板打板）分钟K线数据采集脚本

目标：为每一条首板打板候选（limit_times==1, multi_open板型）拉取当日分钟K线，
      计算打板时刻特征，用于更准确的D策略回测。

输出：data/processed/strategy_d_minute_features.csv
      每行对应一个首板候选，新增字段：
        reseal_time     - 炸板后重新封板的时间（最后一次涨停）
        reseal_strength - 重封时封单强度（相对当日成交量的比例）
        tail_sealed     - 是否在14:00后封板（尾盘封板，次日溢价更稳）
        open_vol_ratio  - 炸板阶段成交量 / 全天总量（越小越好，说明队列消化快）
        minute_ok       - 是否成功拉到分钟数据

用法：
    python scripts/collect_strategy_d_minute_data.py
    python scripts/collect_strategy_d_minute_data.py --sentiment strong --start-date 20240101
    python scripts/collect_strategy_d_minute_data.py --dry-run   # 只看要拉多少条
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.utils.logger import setup_logger
from src.utils.config import load_json_config

logger = setup_logger(
    log_dir=PROJECT_ROOT / "logs",
    log_file="strategy_d_minute.log",
    level="INFO",
)

LIMIT_UP_PCT = {
    "star": 0.20,
    "chi_next": 0.20,
    "bj": 0.30,
    "sh_main": 0.10,
    "sz_main": 0.10,
    "other": 0.10,
}

OUT_PATH = PROJECT_ROOT / "data" / "processed" / "strategy_d_minute_features.csv"
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "minute_d"
RAW_DIR.mkdir(parents=True, exist_ok=True)


def load_candidates(sentiment_filter: str | None, start_date: str, end_date: str,
                    d_trades_path: str = "") -> pd.DataFrame:
    df = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "next_day_premium_trades.csv")
    df = df[(df["limit_times"] == 1) & (~df["is_st"].astype(bool)) & (df["board_type"] == "multi_open")]
    if sentiment_filter:
        df = df[df["market_sentiment_level"] == sentiment_filter]
    if start_date:
        df = df[df["trade_date"].astype(str) >= start_date]
    if end_date:
        df = df[df["trade_date"].astype(str) <= end_date]

    # --d-trades-dates 模式：只拉回测中D实际触发日期的所有候选（不止1只，用于重新选股）
    if d_trades_path:
        try:
            d_trades = pd.read_csv(d_trades_path)
            fired_dates = set(d_trades["signal_date"].astype(str))
            df = df[df["trade_date"].astype(str).isin(fired_dates)]
            logger.info("仅拉取D触发日期的候选: %d 天，共 %d 条", len(fired_dates), len(df))
        except Exception as e:
            logger.warning("读取d_trades失败: %s，忽略过滤", e)

    df = df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    return df


def get_limit_up_price(row: pd.Series) -> float:
    return float(row["limit_close"])


def analyze_minute_bars(bars: pd.DataFrame, limit_price: float) -> dict:
    """从分钟K推断打板相关特征"""
    if bars.empty:
        return {"reseal_time": None, "reseal_strength": None, "tail_sealed": False,
                "open_vol_ratio": None, "minute_ok": False}

    # 按时间排序，过滤出交易时段
    bars = bars.copy()
    bars["trade_time"] = pd.to_datetime(bars["trade_time"])
    bars = bars.sort_values("trade_time").reset_index(drop=True)

    total_vol = bars["vol"].sum()
    if total_vol == 0:
        return {"reseal_time": None, "reseal_strength": None, "tail_sealed": False,
                "open_vol_ratio": None, "minute_ok": True}

    # 判断每分钟是否处于涨停封板状态（收盘价 == 涨停价，允许0.01误差）
    bars["at_limit"] = (bars["close"] - limit_price).abs() < 0.015

    # 找最后一次"从非涨停 → 涨停"的时刻（即重封时刻）
    reseal_time = None
    reseal_idx = None
    for i in range(1, len(bars)):
        if bars.at[i, "at_limit"] and not bars.at[i - 1, "at_limit"]:
            reseal_time = bars.at[i, "trade_time"]
            reseal_idx = i

    # 炸板阶段成交量（非涨停分钟的成交量总和）
    open_vol = bars[~bars["at_limit"]]["vol"].sum()
    open_vol_ratio = open_vol / total_vol if total_vol > 0 else None

    # 重封时封单强度：用重封后首分钟成交量相对比较
    reseal_strength = None
    if reseal_idx is not None:
        # 重封后的封板分钟的量（越小说明队列越难消化 = 封板越稳）
        post_reseal_vol = bars.iloc[reseal_idx:]["vol"].sum()
        reseal_strength = post_reseal_vol / total_vol if total_vol > 0 else None

    # 是否尾盘封板（14:00之后重封）
    tail_sealed = False
    if reseal_time is not None:
        tail_sealed = reseal_time.hour >= 14

    return {
        "reseal_time": reseal_time.strftime("%H:%M") if reseal_time else None,
        "reseal_strength": round(reseal_strength, 4) if reseal_strength is not None else None,
        "tail_sealed": tail_sealed,
        "open_vol_ratio": round(open_vol_ratio, 4) if open_vol_ratio is not None else None,
        "minute_ok": True,
    }


def pull_and_analyze(data_source, row: pd.Series, overwrite: bool) -> dict:
    ts_code = row["ts_code"]
    trade_date = str(int(row["trade_date"]))
    cache_file = RAW_DIR / f"{trade_date}_{ts_code.replace('.', '_')}.csv"

    limit_price = get_limit_up_price(row)

    # 读缓存
    if cache_file.exists() and not overwrite:
        try:
            bars = pd.read_csv(cache_file)
            return analyze_minute_bars(bars, limit_price)
        except Exception:
            pass

    # 拉分钟数据
    start_dt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:30:00"
    end_dt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 15:00:00"
    try:
        bars = data_source.get_minute_bars(ts_code, start_dt, end_dt, freq="1min")
        if bars is not None and not bars.empty:
            bars.to_csv(cache_file, index=False)
        else:
            bars = pd.DataFrame()
    except Exception as e:
        logger.warning("拉取分钟数据失败 %s %s: %s", ts_code, trade_date, e)
        bars = pd.DataFrame()

    return analyze_minute_bars(bars, limit_price)


def main() -> None:
    parser = argparse.ArgumentParser(description="策略D首板打板分钟特征采集")
    parser.add_argument("--sentiment", default="strong", help="市场情绪筛选，默认strong。留空=全量")
    parser.add_argument("--start-date", default="", help="开始日期 YYYYMMDD")
    parser.add_argument("--end-date", default="", help="结束日期 YYYYMMDD")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有缓存")
    parser.add_argument("--dry-run", action="store_true", help="只统计数量，不拉数据")
    parser.add_argument("--limit", type=int, default=0, help="限制条数，用于测试")
    parser.add_argument("--d-trades-dates", default="", metavar="D_TRADES_CSV",
                        help="只拉D回测触发日的所有候选（传入d_trades.csv路径），约150条≈2.5小时")
    args = parser.parse_args()

    candidates = load_candidates(
        sentiment_filter=args.sentiment or None,
        start_date=args.start_date,
        end_date=args.end_date,
        d_trades_path=args.d_trades_dates,
    )
    if args.limit:
        candidates = candidates.head(args.limit)

    logger.info("D策略分钟采集目标: %d 条，日期 %s ~ %s，情绪=%s",
                len(candidates),
                candidates["trade_date"].min() if len(candidates) else "-",
                candidates["trade_date"].max() if len(candidates) else "-",
                args.sentiment or "全量")

    if args.dry_run:
        print(f"\n待采集: {len(candidates)} 条")
        print(f"日期范围: {candidates['trade_date'].min()} ~ {candidates['trade_date'].max()}")
        print(f"预计请求次数: {len(candidates)} 次（每次1只股票1天的分钟K）")
        print(f"预计数据量: 约 {len(candidates) * 240:,} 行")
        print(f"预计耗时（500ms/次）: 约 {len(candidates) * 0.5 / 60:.0f} 分钟")
        by_year = candidates.groupby(candidates["trade_date"].astype(str).str[:4]).size()
        print("\n按年分布:")
        for yr, n in by_year.items():
            print(f"  {yr}: {n:,} 条")
        return

    # 加载数据源
    token = os.getenv("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("未找到 TUSHARE_TOKEN 环境变量，请在 .env 中配置或 export TUSHARE_TOKEN=xxx")

    from src.data_source import TushareDataSource
    data_source = TushareDataSource(config_path=str(PROJECT_ROOT / "config" / "config.json"))

    # 读已有结果（支持断点续传）
    existing_keys: set = set()
    if OUT_PATH.exists() and not args.overwrite:
        try:
            existing = pd.read_csv(OUT_PATH)
            existing_keys = set(existing["trade_date"].astype(str) + "_" + existing["ts_code"])
            logger.info("已有结果 %d 条，将跳过", len(existing_keys))
        except Exception:
            pass

    results = []
    total = len(candidates)
    for i, (_, row) in enumerate(candidates.iterrows()):
        key = f"{int(row['trade_date'])}_{row['ts_code']}"
        if key in existing_keys:
            continue

        features = pull_and_analyze(data_source, row, args.overwrite)
        result = {
            "trade_date": int(row["trade_date"]),
            "ts_code": row["ts_code"],
            "name": row.get("name", ""),
            "market_sentiment_level": row.get("market_sentiment_level", ""),
            "first_time_bucket": row.get("first_time_bucket", ""),
            "open_times": row.get("open_times", 0),
            "net_return": row.get("net_return", 0),
            "is_win": row.get("is_win", False),
            **features,
        }
        results.append(result)

        if (i + 1) % 10 == 0 or i == total - 1:
            done = i + 1 - len(existing_keys)
            elapsed_min = done * 62 / 60
            remaining_min = (total - i - 1) * 62 / 60
            logger.info("进度: %d/%d (%.1f%%) | 已用约%.0f分 | 剩余约%.0f分",
                        i + 1, total, (i + 1) / total * 100, elapsed_min, remaining_min)

        # stk_mins 接口限频 1次/分钟，等待62秒
        if i < total - 1:
            time.sleep(62)

    if not results:
        logger.info("无新增结果")
        return

    new_df = pd.DataFrame(results)

    # 合并已有结果
    if OUT_PATH.exists() and not args.overwrite:
        try:
            old_df = pd.read_csv(OUT_PATH)
            new_df = pd.concat([old_df, new_df], ignore_index=True)
        except Exception:
            pass

    new_df.to_csv(OUT_PATH, index=False)
    logger.info("D策略分钟特征已保存: %s (%d 行)", OUT_PATH, len(new_df))
    print(f"\n完成: {OUT_PATH}")
    print(f"总行数: {len(new_df)}")
    ok = new_df["minute_ok"].sum() if "minute_ok" in new_df.columns else 0
    print(f"成功拉到分钟数据: {ok} 条")


if __name__ == "__main__":
    main()
