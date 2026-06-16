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

from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="诊断 Tushare limit_list_d 当日涨停池是否可用。")
    parser.add_argument("--trade-date", required=True, help="交易日期，格式 YYYYMMDD。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    return parser.parse_args()


def ensure_tushare_token(config: dict[str, Any]) -> None:
    token_env = config.get("data_source", {}).get("token_env", "TUSHARE_TOKEN")
    if os.getenv(token_env):
        return
    stored = str(config.get("data_source", {}).get("token", "")).strip()
    if stored:
        os.environ[token_env] = stored
        return
    token = getpass.getpass("请输入 Tushare Pro Token（不会显示，且不会保存到本地）: ").strip()
    if not token:
        raise RuntimeError("Tushare Token 不能为空。")
    os.environ[token_env] = token


def local_csv_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "rows": 0, "columns": []}
    try:
        data = pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        return {"exists": True, "rows": 0, "columns": []}
    return {"exists": True, "rows": len(data), "columns": list(data.columns)}


def probe_call(label: str, func, **kwargs: Any) -> dict[str, Any]:
    try:
        data = func(**kwargs)
        if data is None:
            data = pd.DataFrame()
        return {
            "label": label,
            "status": "OK",
            "rows": len(data),
            "columns": ",".join(map(str, data.columns[:20])),
            "error": "",
        }
    except Exception as exc:
        return {
            "label": label,
            "status": "ERROR",
            "rows": 0,
            "columns": "",
            "error": str(exc),
        }


def probe_pro_api(source: Any, api_name: str, **kwargs: Any) -> dict[str, Any]:
    try:
        func = getattr(source.pro, api_name)
    except AttributeError as exc:
        return {
            "label": api_name,
            "status": "ERROR",
            "rows": 0,
            "columns": "",
            "error": f"当前 tushare SDK 未暴露接口: {exc}",
        }
    return probe_call(api_name, func, **kwargs)


def probe_pro_query(source: Any, label: str, api_name: str, **kwargs: Any) -> dict[str, Any]:
    try:
        data = source.pro.query(api_name, **kwargs)
        if data is None:
            data = pd.DataFrame()
        return {
            "label": label,
            "status": "OK",
            "rows": len(data),
            "columns": ",".join(map(str, data.columns[:20])),
            "error": "",
        }
    except Exception as exc:
        return {
            "label": label,
            "status": "ERROR",
            "rows": 0,
            "columns": "",
            "error": str(exc),
        }


def estimate_limit_up_from_daily(source: Any, trade_date: str) -> dict[str, Any]:
    daily = source.get_daily(trade_date=trade_date)
    if daily.empty:
        return {"rows": 0, "sample": pd.DataFrame()}

    names = pd.DataFrame(columns=["ts_code", "name"])
    try:
        names = source.get_stock_basic(list_status="L", fields="ts_code,name")
    except Exception:
        pass
    if not names.empty and {"ts_code", "name"}.issubset(names.columns):
        daily = daily.merge(names, on="ts_code", how="left")
    else:
        daily["name"] = ""

    def limit_threshold(row: pd.Series) -> float:
        name = str(row.get("name", "")).upper()
        if "ST" in name or "退" in name:
            return 5.0
        code = str(row.get("ts_code", "")).upper()
        prefix = code.split(".")[0]
        if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
            return 30.0
        if prefix.startswith(("300", "301", "688", "689")):
            return 20.0
        return 10.0

    daily["limit_threshold"] = daily.apply(limit_threshold, axis=1)
    pct_chg = pd.to_numeric(daily["pct_chg"], errors="coerce")
    estimated = daily[pct_chg >= daily["limit_threshold"] - 0.2].copy()
    sample_columns = [column for column in ["trade_date", "ts_code", "name", "close", "pct_chg"] if column in estimated.columns]
    return {"rows": len(estimated), "sample": estimated[sample_columns].head(10)}


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    ensure_tushare_token(config)

    from src.data_source import TushareDataSource

    source = TushareDataSource(config_path=args.config)
    trade_date = str(args.trade_date)
    collection_config = config.get("collection", {})
    fields = collection_config.get("limit_list_fields")

    print_section("1. 本地文件状态")
    data_config = config.get("data", {})
    local_paths = {
        "daily": PROJECT_ROOT / data_config.get("daily_dir", "data/raw/daily") / f"{trade_date}.csv",
        "daily_basic": PROJECT_ROOT / data_config.get("daily_basic_dir", "data/raw/daily_basic") / f"{trade_date}.csv",
        "limit_list": PROJECT_ROOT / data_config.get("limit_list_dir", "data/raw/limit_list") / f"{trade_date}.csv",
    }
    for name, path in local_paths.items():
        status = local_csv_status(path)
        print(f"{name}: exists={status['exists']} rows={status['rows']} path={path}")
        if status["columns"]:
            print(f"  columns={','.join(status['columns'][:20])}")

    print_section("2. Tushare 基础接口探测")
    base_results = [
        probe_call(
            "trade_cal",
            source.get_trade_calendar,
            start_date=trade_date,
            end_date=trade_date,
            exchange="SSE",
        ),
        probe_call("daily", source.get_daily, trade_date=trade_date),
        probe_call("daily_basic", source.get_daily_basic, trade_date=trade_date),
    ]
    for result in base_results:
        print(
            f"{result['label']}: status={result['status']} rows={result['rows']} "
            f"columns={result['columns']} error={result['error']}"
        )

    print_section("3. limit_list_d 参数矩阵探测")
    probes = [
        ("limit_list_d limit_type=U configured_fields", {"trade_date": trade_date, "limit_type": "U", "fields": fields}),
        ("limit_list_d limit_type=U default_fields", {"trade_date": trade_date, "limit_type": "U"}),
        ("limit_list_d limit_type=D default_fields", {"trade_date": trade_date, "limit_type": "D"}),
        ("limit_list_d no_limit_type default_fields", {"trade_date": trade_date}),
        ("limit_list_d no_limit_type configured_fields", {"trade_date": trade_date, "fields": fields}),
        (
            "limit_list_d same_day_range configured_fields",
            {"start_date": trade_date, "end_date": trade_date, "limit_type": "U", "fields": fields},
        ),
        (
            "limit_list_d same_day_range default_fields",
            {"start_date": trade_date, "end_date": trade_date, "limit_type": "U"},
        ),
    ]
    limit_results = []
    for label, kwargs in probes:
        limit_results.append(probe_call(label, source.pro.limit_list_d, **kwargs))
    query_probes = [
        (
            "query limit_list_d trade_date configured_fields",
            {"trade_date": trade_date, "limit_type": "U", "fields": fields},
        ),
        (
            "query limit_list_d same_day_range configured_fields",
            {"start_date": trade_date, "end_date": trade_date, "limit_type": "U", "fields": fields},
        ),
    ]
    for label, kwargs in query_probes:
        limit_results.append(probe_pro_query(source, label, "limit_list_d", **kwargs))
    for result in limit_results:
        print(
            f"{result['label']}: status={result['status']} rows={result['rows']} "
            f"columns={result['columns']} error={result['error']}"
        )

    print_section("4. Tushare 替代涨跌停接口探测")
    alternative_results = [
        probe_pro_api(source, "stk_limit", trade_date=trade_date),
        probe_pro_api(source, "limit_list", trade_date=trade_date),
        probe_pro_api(source, "limit_list_ths", trade_date=trade_date),
        probe_pro_api(source, "limit_step", trade_date=trade_date),
    ]
    for result in alternative_results:
        print(
            f"{result['label']}: status={result['status']} rows={result['rows']} "
            f"columns={result['columns']} error={result['error']}"
        )

    print_section("5. 日线涨停数量粗算")
    daily_limit = estimate_limit_up_from_daily(source, trade_date)
    print(f"daily_estimated_limit_up_rows={daily_limit['rows']}")
    sample = daily_limit["sample"]
    if not sample.empty:
        print(sample.to_string(index=False))

    print_section("6. 诊断结论")
    daily_ok = any(r["label"] == "daily" and r["status"] == "OK" and r["rows"] > 0 for r in base_results)
    configured_limit = limit_results[0]
    any_limit_rows = any(r["status"] == "OK" and r["rows"] > 0 for r in limit_results)
    any_limit_error = any(r["status"] == "ERROR" for r in limit_results)
    any_alternative_rows = any(r["status"] == "OK" and r["rows"] > 0 for r in alternative_results)

    if configured_limit["status"] == "OK" and configured_limit["rows"] > 0:
        print("结论：配置参数可以拿到涨停池数据，主流程应能采集。若主流程仍没有，请检查本地空 CSV 是否被覆盖失败。")
    elif any_limit_rows:
        print("结论：Tushare 有涨停池数据，但当前配置参数拿不到。优先对比上面的成功探测，检查 limit_type 或 fields。")
    elif any_alternative_rows:
        print("结论：limit_list_d 返回 0 行，但至少一个替代涨跌停接口有数据。")
        print("下一步应改数据源优先级：limit_list_d 失败时切到有数据的接口，并明确字段口径差异。")
    elif any_limit_error:
        print("结论：limit_list_d 调用发生错误。优先看 error 字段，常见原因是字段名变更、权限/积分不足或接口参数不支持。")
    elif daily_ok:
        print("结论：Tushare 日线已就绪，但 limit_list_d 所有探测都返回 0 行。")
        print("这不是重复重试能解决的问题，优先确认 Tushare 该接口当日是否延迟、账号权限是否覆盖 limit_list_d，或该日期是否接口侧缺失。")
    else:
        print("结论：日线也没有数据。优先确认日期是否交易日、token 是否有效、网络和 Tushare 服务是否正常。")


if __name__ == "__main__":
    main()
