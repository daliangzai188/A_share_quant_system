"""
构建日成交额轻量查询表。

文件作用：
1. 从 data/processed/daily_merged.csv 分块读取成交额字段。
2. 只保留后续真实成交容量回测需要的 trade_date、ts_code、amount_yuan、market_segment。
3. 输出轻量 CSV，避免策略搜索每次读取 1.8GB 日线合并表。
4. 不调用外部接口，不接实盘，不写入任何敏感信息。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建日成交额轻量查询表。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--input",
        default=None,
        help="输入日线合并表路径，默认读取 config.analysis.input_daily_merged_path。",
    )
    parser.add_argument(
        "--output",
        default="data/processed/daily_amount_lookup.csv",
        help="输出轻量成交额查询表路径。",
    )
    parser.add_argument("--chunksize", type=int, default=300000, help="分块读取行数。")
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def resolve_path(path: str | Path) -> Path:
    result = Path(path)
    if not result.is_absolute():
        result = PROJECT_ROOT / result
    return result


def build_lookup(input_path: Path, output_path: Path, chunksize: int) -> dict[str, int]:
    logger = get_logger("build_daily_amount_lookup")
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    if chunksize <= 0:
        raise ValueError("--chunksize 必须大于 0")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    usecols = ["trade_date", "ts_code", "amount", "market_segment"]
    total_input_rows = 0
    total_output_rows = 0
    chunk_count = 0
    wrote_header = False

    for chunk in pd.read_csv(
        input_path,
        dtype={"trade_date": str, "ts_code": str, "market_segment": str},
        usecols=usecols,
        chunksize=chunksize,
        low_memory=False,
    ):
        chunk_count += 1
        total_input_rows += len(chunk)
        result = chunk.copy()
        result["trade_date"] = result["trade_date"].map(normalize_date)
        result["ts_code"] = result["ts_code"].astype(str)
        result["market_segment"] = result["market_segment"].fillna("unknown").astype(str)
        result["amount_yuan"] = pd.to_numeric(result["amount"], errors="coerce") * 1000.0
        result = result[["trade_date", "ts_code", "amount_yuan", "market_segment"]]
        result = result.dropna(subset=["trade_date", "ts_code", "amount_yuan"])
        result = result[result["amount_yuan"] >= 0].copy()
        result.to_csv(
            temp_path,
            mode="a",
            header=not wrote_header,
            index=False,
            encoding="utf-8-sig",
        )
        wrote_header = True
        total_output_rows += len(result)
        if chunk_count % 10 == 0:
            logger.info(
                "成交额缓存构建进度: chunks=%s, input_rows=%s, output_rows=%s",
                chunk_count,
                total_input_rows,
                total_output_rows,
            )

    os.replace(temp_path, output_path)
    logger.info(
        "成交额缓存已生成: %s, chunks=%s, input_rows=%s, output_rows=%s",
        output_path,
        chunk_count,
        total_input_rows,
        total_output_rows,
    )
    return {
        "chunk_count": chunk_count,
        "input_rows": total_input_rows,
        "output_rows": total_output_rows,
    }


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    analysis_config = config.get("analysis", {})
    input_path = resolve_path(args.input or analysis_config.get("input_daily_merged_path", "data/processed/daily_merged.csv"))
    output_path = resolve_path(args.output)
    summary = build_lookup(input_path=input_path, output_path=output_path, chunksize=args.chunksize)
    print("日成交额轻量查询表生成完成：")
    print(f"- output: {output_path}")
    print(f"- input_rows: {summary['input_rows']}")
    print(f"- output_rows: {summary['output_rows']}")
    print(f"- chunks: {summary['chunk_count']}")


if __name__ == "__main__":
    main()
