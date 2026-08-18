"""
采集全局股票题材参考表。

第一阶段先使用 Tushare stock_basic.industry 作为“行业主线”代理。
这不是完整题材/概念归因，但可供全项目统一使用行业热度代理。

输出：
  data/raw/stock_basic/stock_basic_all.csv
  data/processed/stock_theme_reference.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_source import TushareDataSource
from src.utils.config import mkdir_p


RAW_OUTPUT = PROJECT_ROOT / "data" / "raw" / "stock_basic" / "stock_basic_all.csv"
PROCESSED_OUTPUT = PROJECT_ROOT / "data" / "processed" / "stock_theme_reference.csv"


def normalize_industry(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "none", "nan", "null"}:
        return "unknown"
    return text


def main() -> None:
    source = TushareDataSource("config/config.json")
    frames: list[pd.DataFrame] = []
    for status in ["L", "D", "P"]:
        try:
            data = source.get_stock_basic(
                list_status=status,
                fields="ts_code,name,industry,market,list_date,delist_date",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"stock_basic({status}) 拉取失败: {exc}")
            continue
        if data.empty:
            continue
        data["list_status"] = status
        frames.append(data)

    if not frames:
        raise RuntimeError("stock_basic 未返回任何数据，无法构建题材参考表。")

    raw = pd.concat(frames, ignore_index=True)
    raw = raw.sort_values(["ts_code", "list_status"]).drop_duplicates("ts_code", keep="first")

    mkdir_p(RAW_OUTPUT.parent)
    raw.to_csv(RAW_OUTPUT, index=False, encoding="utf-8-sig")

    ref = raw[["ts_code", "name", "industry", "market", "list_status", "list_date", "delist_date"]].copy()
    ref["industry"] = ref["industry"].map(normalize_industry)
    ref["theme_name"] = ref["industry"]
    ref["theme_source_column"] = "stock_basic.industry"
    ref["theme_data_available"] = ref["theme_name"].ne("unknown")

    mkdir_p(PROCESSED_OUTPUT.parent)
    ref.to_csv(PROCESSED_OUTPUT, index=False, encoding="utf-8-sig")

    available = int(ref["theme_data_available"].sum())
    print("题材参考表已生成")
    print(f"- raw: {RAW_OUTPUT} 行数={len(raw)}")
    print(f"- processed: {PROCESSED_OUTPUT} 行数={len(ref)} 可用={available} ({available / len(ref):.2%})")


if __name__ == "__main__":
    main()
