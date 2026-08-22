#!/usr/bin/env python3
"""断点采集D3新增的“达到7%但未触板”历史一分钟路径。

仅下载D3相对现有首板触板账本新增的失败分母；已触板4万只次继续复用现有
``minute_1m_tushare.csv``。按交易日每33只股票横截面请求并逐job原子落盘，
中断后可直接重跑。该脚本只读行情，不连接券商、不下单。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any
import urllib.request

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_strategy_d_intraday_tushare_1m import (  # noqa: E402
    CollectionPolicy,
    atomic_write_csv,
    build_cross_section_jobs,
    fetch_job_with_retry,
    job_part_path,
    load_collection_policy,
    load_csv,
    target_status_rows,
    upsert_rows,
)
from src.secret_config import ensure_tushare_token  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


LOGGER = logging.getLogger("collect_strategy_d3_halfway_tushare_1m")
BASE_DIR = ROOT / "data/research/strategy_d3_halfway"
TARGET_PATH = BASE_DIR / "minute_target_manifest_new_non_touch.csv"
PARTS_DIR = BASE_DIR / "minute_1m_tushare_new_parts"
STATUS_PATH = BASE_DIR / "minute_1m_tushare_new_status.csv"
JOB_STATUS_PATH = BASE_DIR / "minute_1m_tushare_new_job_status.csv"
SUMMARY_PATH = ROOT / "reports/strategy_d3_halfway/minute_collection_summary.json"
CONFIG_PATH = ROOT / "config/strategy_d_intraday_collection.json"


class RawTushareMinuteSource:
    """无第三方tushare包时直接调用同一官方Pro HTTP端点。"""

    def __init__(self, token: str, *, timeout_seconds: float = 5.0) -> None:
        self.token = str(token).strip()
        self.timeout_seconds = float(timeout_seconds)
        if not self.token:
            raise ValueError("Tushare Token为空")

    def get_stock_minute_bars(
        self,
        ts_code: str,
        start_dt: str,
        end_dt: str,
        *,
        freq: str = "1min",
        fields: str = "",
    ) -> pd.DataFrame:
        payload = {
            "api_name": "stk_mins",
            "token": self.token,
            "params": {
                "ts_code": str(ts_code),
                "start_date": str(start_dt),
                "end_date": str(end_dt),
                "freq": str(freq),
            },
            "fields": str(fields),
        }
        request = urllib.request.Request(
            "http://api.tushare.pro",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        if int(result.get("code", -1)) != 0:
            raise RuntimeError(str(result.get("msg", "Tushare请求失败")))
        data = result.get("data") or {}
        return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])


def load_targets(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str, "target_key": str},
        low_memory=False,
    )
    required = {"trade_date", "ts_code", "target_key", "market_segment"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D3分钟目标缺少字段：{missing}")
    frame["trade_date"] = frame["trade_date"].str.replace(r"\.0$", "", regex=True)
    rebuilt = frame["trade_date"] + "|" + frame["ts_code"].astype(str)
    if not rebuilt.eq(frame["target_key"].astype(str)).all():
        raise ValueError("D3分钟目标target_key与日期代码不一致")
    if frame["target_key"].duplicated().any():
        raise ValueError("D3分钟目标重复")
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def write_summary(
    targets: pd.DataFrame,
    jobs: pd.DataFrame,
    status: pd.DataFrame,
    policy: CollectionPolicy,
) -> dict[str, Any]:
    counts = (
        {str(key): int(value) for key, value in status["status"].value_counts().items()}
        if not status.empty and "status" in status.columns
        else {}
    )
    complete = int(counts.get("COMPLETE_1M_NO_QUEUE_DEPTH", 0))
    empty = int(counts.get("EMPTY", 0))
    payload = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "strategy_school": "D3_HALF_WAY_7_TO_9_PERCENT",
        "source": "TUSHARE_STK_MINS_1M_UNADJUSTED",
        "formal_strategy_modified": False,
        "target_count": int(len(targets)),
        "request_job_count": int(len(jobs)),
        "complete_target_count": complete,
        "empty_vendor_target_count": empty,
        "pending_target_count": int(len(targets) - len(status)),
        "status_counts": counts,
        "coverage_rate": float(complete / max(len(targets), 1)),
        "request_interval_seconds": policy.request_interval_seconds,
        "max_codes_per_request": policy.max_codes_per_request,
        "estimated_request_minutes": float(
            len(jobs) * policy.request_interval_seconds / 60.0
        ),
        "parts_dir": str(PARTS_DIR.relative_to(ROOT)),
        "path_layer_complete_or_fail_closed": bool(
            complete + empty == len(targets)
        ),
        "release_eligible": False,
        "limitations": [
            "一分钟路径只负责信号和价格成交重放，不含盘口队列。",
            "EMPTY目标保留在母样本分母并按无信号fail-closed，不删除、不插值。",
        ],
    }
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集D3新增失败分母历史一分钟路径")
    parser.add_argument("--targets", type=Path, default=TARGET_PATH)
    parser.add_argument("--limit-jobs", type=int, default=0)
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="并行等待网络的线程数；请求启动仍按配置的0.15秒全局限频",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    target_path = args.targets if args.targets.is_absolute() else ROOT / args.targets
    targets = load_targets(target_path)
    policy = load_collection_policy(CONFIG_PATH)
    jobs = build_cross_section_jobs(
        targets, max_codes_per_request=policy.max_codes_per_request
    )
    status = load_csv(
        STATUS_PATH,
        dtype={"target_key": str, "trade_date": str, "ts_code": str, "job_key": str},
    )
    completed_keys = set(
        status.loc[
            status.get("status", pd.Series(dtype=str)).astype(str).isin(
                {"COMPLETE_1M_NO_QUEUE_DEPTH", "EMPTY"}
            ),
            "target_key",
        ].astype(str)
    ) if not status.empty else set()
    pending = targets[~targets["target_key"].astype(str).isin(completed_keys)]
    pending_jobs = build_cross_section_jobs(
        pending, max_codes_per_request=policy.max_codes_per_request
    )
    print(json.dumps(write_summary(targets, jobs, status, policy), ensure_ascii=False, indent=2))
    print(f"待请求job：{len(pending_jobs)}/{len(jobs)}")
    if args.dry_run:
        return 0
    if args.limit_jobs > 0:
        pending_jobs = pending_jobs.head(args.limit_jobs)
    if not 1 <= args.workers <= 16:
        raise ValueError("workers必须为1~16")

    config = load_json_config(ROOT / "config/config.json")
    token = ensure_tushare_token(config, project_root=ROOT)
    source = RawTushareMinuteSource(token)
    job_status = load_csv(JOB_STATUS_PATH, dtype={"job_key": str})
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    pending_list = list(pending_jobs.itertuples(index=False))
    processed = 0
    checkpoint_size = policy.checkpoint_every_jobs
    try:
        # 每批最多checkpoint_every_jobs个。提交动作之间仍机械等待0.15秒，
        # 线程只覆盖HTTP等待时间，不提高Tushare每分钟请求启动频率。
        for offset in range(0, len(pending_list), checkpoint_size):
            batch = pending_list[offset : offset + checkpoint_size]
            future_jobs: dict[Any, Any] = {}
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for submit_index, job in enumerate(batch):
                    future = executor.submit(
                        fetch_job_with_retry, source, job, policy=policy
                    )
                    future_jobs[future] = job
                    if submit_index + 1 < len(batch):
                        time.sleep(policy.request_interval_seconds)
                for future in as_completed(future_jobs):
                    job = future_jobs[future]
                    fetched, error, attempts = future.result()
                    if not fetched.empty:
                        atomic_write_csv(fetched, job_part_path(job, PARTS_DIR))
                    additions = target_status_rows(job, fetched, error)
                    status = upsert_rows(status, additions, ["target_key"])
                    job_row = pd.DataFrame(
                        [
                            {
                                "job_key": job.job_key,
                                "target_count": job.target_count,
                                "returned_bar_count": int(len(fetched)),
                                "attempt_count": int(attempts),
                                "status": "ERROR" if error else "DONE",
                                "error_message": error[:500],
                                "updated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
                            }
                        ]
                    )
                    job_status = upsert_rows(job_status, job_row, ["job_key"])
                    processed += 1
                    if error:
                        LOGGER.error("%s失败：%s", job.job_key, error)
            atomic_write_csv(status.sort_values(["trade_date", "ts_code"]), STATUS_PATH)
            atomic_write_csv(job_status.sort_values("job_key"), JOB_STATUS_PATH)
            payload = write_summary(targets, jobs, status, policy)
            LOGGER.info(
                "D3分钟采集 %d/%d complete=%d empty=%d last_batch=%d",
                processed,
                len(pending_list),
                payload["complete_target_count"],
                payload["empty_vendor_target_count"],
                len(batch),
            )
    finally:
        if not status.empty:
            atomic_write_csv(status.sort_values(["trade_date", "ts_code"]), STATUS_PATH)
            atomic_write_csv(job_status.sort_values("job_key"), JOB_STATUS_PATH)
            write_summary(targets, jobs, status, policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
