from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_source import TushareDataSource
from src.utils.config import load_json_config
from src.utils.logger import setup_logger, get_logger


DEFAULT_MONEYFLOW_FIELDS = (
    "trade_date,ts_code,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
    "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"
)
DEFAULT_TOP_LIST_FIELDS = "trade_date,ts_code,name,amount,l_buy,l_sell,net_amount,net_rate,reason"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建实盘增强因子：资金流、龙虎榜、竞价和开盘5分钟占位审计。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--trade-date", required=True, help="交易日期 YYYYMMDD。")
    parser.add_argument("--input-path", default="data/processed/live_limit_up_fill_scored.csv", help="当日涨停候选输入。")
    parser.add_argument("--max-trade-days", type=int, default=10, help="输出文件只保留最近N个交易日。")
    return parser.parse_args()


def ensure_tushare_token(config: dict[str, Any]) -> None:
    token_env = config.get("data_source", {}).get("token_env", "TUSHARE_TOKEN")
    if os.getenv(token_env):
        return
    stored = str(config.get("data_source", {}).get("token", "")).strip()
    if stored:
        os.environ[token_env] = stored
        return
    try:
        token = getpass.getpass("请输入 Tushare Pro Token（不会显示，且不会保存到本地）: ").strip()
    except EOFError as exc:
        raise RuntimeError(f"Tushare Token 未配置：请设置环境变量 {token_env} 或 config/config.json") from exc
    if not token:
        raise RuntimeError("Tushare Token 不能为空。")
    os.environ[token_env] = token


def setup(config: dict[str, Any]) -> None:
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )


def load_candidates(path: Path, trade_date: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"增强因子输入文件不存在: {path}")
    data = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    if "trade_date" not in data.columns or "ts_code" not in data.columns:
        raise ValueError(f"增强因子输入缺少 trade_date/ts_code: {path}")
    data = data[data["trade_date"].astype(str).eq(trade_date)].copy()
    if data.empty:
        raise RuntimeError(f"增强因子输入没有 {trade_date} 记录: {path}")
    keep = [
        c for c in [
            "trade_date", "ts_code", "name", "amount", "circ_mv", "theme_name",
            "theme_limit_count", "theme_heat_rank", "market_segment",
        ]
        if c in data.columns
    ]
    return data[keep].drop_duplicates(["trade_date", "ts_code"]).reset_index(drop=True)


def rolling_write(path: Path, new_data: pd.DataFrame, trade_date: str, max_trade_days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        old = old[~old["trade_date"].astype(str).eq(trade_date)] if "trade_date" in old.columns else pd.DataFrame()
        combined = pd.concat([old, new_data], ignore_index=True, sort=False)
    else:
        combined = new_data.copy()
    if "trade_date" in combined.columns:
        dates = sorted(combined["trade_date"].dropna().astype(str).unique())
        keep_dates = set(dates[-max(1, max_trade_days):])
        combined = combined[combined["trade_date"].astype(str).isin(keep_dates)].copy()
    combined.to_csv(path, index=False, encoding="utf-8-sig")


def normalize_moneyflow_score(candidates: pd.DataFrame, moneyflow: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    base = candidates[["trade_date", "ts_code"]].copy()
    if "name" in candidates.columns:
        base["name"] = candidates["name"]
    if moneyflow.empty:
        base["sector_moneyflow_score"] = pd.NA
        base["data_available"] = False
        base["unavailable_reason"] = "Tushare moneyflow 未返回当日数据或无权限"
        return base

    mf = moneyflow.copy()
    mf["trade_date"] = mf["trade_date"].astype(str)
    mf["ts_code"] = mf["ts_code"].astype(str)
    amount_map = candidates.set_index("ts_code")["amount"] if "amount" in candidates.columns else pd.Series(dtype="float64")
    merged = base.merge(mf, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
    net = pd.to_numeric(merged.get("net_mf_amount"), errors="coerce")
    # Tushare daily.amount 单位通常为千元，moneyflow金额通常为万元；统一成百分比评分。
    daily_amount_wan = pd.to_numeric(merged["ts_code"].map(amount_map), errors="coerce") / 10.0
    merged["sector_moneyflow_score"] = (net / daily_amount_wan * 100.0).where(daily_amount_wan > 0)
    merged["data_available"] = merged["sector_moneyflow_score"].notna()
    merged["unavailable_reason"] = merged["data_available"].map({True: "", False: "未匹配到该股 moneyflow"})
    keep = [
        "trade_date", "ts_code", "sector_moneyflow_score", "data_available", "unavailable_reason",
        "net_mf_amount", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
    ]
    keep = [c for c in keep if c in merged.columns]
    return merged[keep]


def normalize_top_list_score(candidates: pd.DataFrame, top_list: pd.DataFrame) -> pd.DataFrame:
    base = candidates[["trade_date", "ts_code"]].copy()
    if "name" in candidates.columns:
        base["name"] = candidates["name"]
    base["top_list_net_buy_score"] = 0.0
    base["is_top_list"] = False
    base["data_available"] = True
    base["unavailable_reason"] = ""
    if top_list.empty:
        base["data_available"] = False
        base["unavailable_reason"] = "Tushare top_list 未返回当日数据或无权限"
        return base

    top = top_list.copy()
    top["trade_date"] = top["trade_date"].astype(str)
    top["ts_code"] = top["ts_code"].astype(str)
    # 同一股票可能因多个原因上榜，按净买额合并。
    agg_cols = {"net_amount": "sum", "amount": "sum"}
    top_agg = top.groupby(["trade_date", "ts_code"], as_index=False).agg(agg_cols)
    merged = base.drop(columns=["top_list_net_buy_score", "is_top_list"]).merge(
        top_agg,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    net = pd.to_numeric(merged.get("net_amount"), errors="coerce")
    amount = pd.to_numeric(merged.get("amount"), errors="coerce")
    merged["top_list_net_buy_score"] = (net / amount * 100.0).where((amount > 0) & net.notna(), 0.0)
    merged["is_top_list"] = net.notna()
    merged["data_available"] = True
    merged["unavailable_reason"] = ""
    keep = [
        "trade_date", "ts_code", "top_list_net_buy_score", "is_top_list",
        "data_available", "unavailable_reason", "net_amount", "amount",
    ]
    keep = [c for c in keep if c in merged.columns]
    return merged[keep]


def unavailable_intraday_features(candidates: pd.DataFrame, score_column: str, reason: str) -> pd.DataFrame:
    result = candidates[["trade_date", "ts_code"]].copy()
    if "name" in candidates.columns:
        result["name"] = candidates["name"]
    result[score_column] = pd.NA
    result["data_available"] = False
    result["unavailable_reason"] = reason
    return result


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    setup(config)
    log = get_logger("live_enhanced_features")
    ensure_tushare_token(config)

    trade_date = args.trade_date
    candidates = load_candidates(PROJECT_ROOT / args.input_path, trade_date)
    source = TushareDataSource(config_path=args.config)

    try:
        moneyflow = source.get_moneyflow(trade_date=trade_date, fields=DEFAULT_MONEYFLOW_FIELDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("moneyflow 获取失败，写入不可用标记: %s", exc)
        moneyflow = pd.DataFrame()
    try:
        top_list = source.get_top_list(trade_date=trade_date, fields=DEFAULT_TOP_LIST_FIELDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("top_list 获取失败，写入不可用标记: %s", exc)
        top_list = pd.DataFrame()

    outputs = {
        PROJECT_ROOT / "data/processed/sector_moneyflow_features.csv": normalize_moneyflow_score(
            candidates,
            moneyflow,
            trade_date,
        ),
        PROJECT_ROOT / "data/processed/top_list_features.csv": normalize_top_list_score(candidates, top_list),
        PROJECT_ROOT / "data/processed/auction_features.csv": unavailable_intraday_features(
            candidates,
            "auction_strength_score",
            "当前收盘流水线没有集合竞价明细源；需接入QMT分钟/竞价数据后才能生成真实分数",
        ),
        PROJECT_ROOT / "data/processed/open_5m_features.csv": unavailable_intraday_features(
            candidates,
            "open_5m_strength_score",
            "当前收盘流水线没有9:30-9:35分钟K源；需接入QMT分钟数据后才能生成真实分数",
        ),
    }
    for path, data in outputs.items():
        rolling_write(path, data, trade_date, args.max_trade_days)
        log.info("增强因子已生成: %s rows=%s available=%s", path, len(data), data.get("data_available", pd.Series()).sum())

    print("实盘增强因子构建完成：")
    for path, data in outputs.items():
        print(f"- {path.relative_to(PROJECT_ROOT)} rows={len(data)}")


if __name__ == "__main__":
    main()
