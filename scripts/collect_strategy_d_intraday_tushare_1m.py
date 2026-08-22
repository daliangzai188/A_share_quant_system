#!/usr/bin/env python3
"""断点回填D完整触板母池的Tushare历史1分钟K。

这只是盘中价格路径层，不是排队成交认证。``stk_mins``没有历史买一队列，
所以即使6,848个目标全部得到241根一分钟K，也不能把始终封板的委托记为成交。

为降低低频接口请求量，本脚本把同一股票、最多33个连续交易日跨度内的目标
合并为一次请求（33*241=7,953，小于接口单次8,000行上限），返回后只保留
目标交易日。每个请求完成后立即保存状态，支持中断续跑。项目于2026-08-22
购买并实测通过A股历史分钟权限；采集速率由独立研究配置锁定，并在触发限频时
机械退避，不能把价格路径数据冒充完整L2队列数据。

运行：

    python3 scripts/collect_strategy_d_intraday_tushare_1m.py --dry-run
    python3 scripts/collect_strategy_d_intraday_tushare_1m.py --limit-jobs 1
    python3 scripts/collect_strategy_d_intraday_tushare_1m.py
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.secret_config import ensure_tushare_token  # noqa: E402
from src.strategy_d_intraday_ledger import normalize_minute_bars  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


TARGET_PATH = ROOT / "data/research/strategy_d_intraday/minute_target_manifest.csv"
CALENDAR_PATH = ROOT / "data/raw/trade_calendar.csv"
COLLECTION_CONFIG_PATH = ROOT / "config/strategy_d_intraday_collection.json"
OUTPUT_PATH = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
PARTS_DIR = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare_parts"
STATUS_PATH = ROOT / "data/research/strategy_d_intraday/tushare_1m_status.csv"
JOB_STATUS_PATH = ROOT / "data/research/strategy_d_intraday/tushare_1m_job_status.csv"
SUMMARY_PATH = ROOT / "reports/strategy_d_intraday_research/tushare_1m_collection.json"
EXPECTED_TARGET_COUNT = 6848
MAX_OPEN_DAYS_PER_REQUEST = 33
MIN_COMPLETE_BAR_COUNT = 230
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.15
FIELDS = "ts_code,trade_time,open,close,high,low,vol,amount"
LOGGER = logging.getLogger("collect_strategy_d_intraday_tushare_1m")


@dataclass(frozen=True)
class CollectionPolicy:
    access_tier: str
    request_limit_per_minute: int
    request_interval_seconds: float
    max_attempts_per_job: int
    rate_limit_backoff_seconds: float
    transient_backoff_seconds: float

    @property
    def minimum_request_interval_seconds(self) -> float:
        return 60.0 / self.request_limit_per_minute


def load_collection_policy(path: Path = COLLECTION_CONFIG_PATH) -> CollectionPolicy:
    """读取并校验已购买的历史分钟采集档位，拒绝超过供应商频次。"""

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    section = payload.get("tushare_stock_minute", {})
    policy = CollectionPolicy(
        access_tier=str(section.get("access_tier", "")).strip(),
        request_limit_per_minute=int(section.get("request_limit_per_minute", 0)),
        request_interval_seconds=float(section.get("request_interval_seconds", 0)),
        max_attempts_per_job=int(section.get("max_attempts_per_job", 0)),
        rate_limit_backoff_seconds=float(section.get("rate_limit_backoff_seconds", 0)),
        transient_backoff_seconds=float(section.get("transient_backoff_seconds", 0)),
    )
    if policy.access_tier != "PAID_A_SHARE_HISTORY_MINUTE":
        raise ValueError("D一分钟采集必须显式配置PAID_A_SHARE_HISTORY_MINUTE权限档位")
    if policy.request_limit_per_minute <= 0:
        raise ValueError("request_limit_per_minute必须为正整数")
    if policy.request_interval_seconds < policy.minimum_request_interval_seconds:
        raise ValueError(
            "request_interval_seconds超过供应商频次："
            f"configured={policy.request_interval_seconds} "
            f"minimum={policy.minimum_request_interval_seconds}"
        )
    if policy.max_attempts_per_job <= 0:
        raise ValueError("max_attempts_per_job必须为正整数")
    if policy.rate_limit_backoff_seconds < 60:
        raise ValueError("rate_limit_backoff_seconds不得小于60秒")
    if policy.transient_backoff_seconds < 0:
        raise ValueError("transient_backoff_seconds不得小于0秒")
    return policy


def load_targets(path: Path = TARGET_PATH) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str, "target_key": str},
        low_memory=False,
    )
    required = {"trade_date", "ts_code", "target_key", "market_segment"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D一分钟目标缺少字段：{missing}")
    frame["trade_date"] = frame["trade_date"].str.replace(r"\.0$", "", regex=True)
    frame = frame.drop_duplicates("target_key", keep="last")
    if len(frame) != EXPECTED_TARGET_COUNT:
        raise RuntimeError(
            f"D一分钟目标漂移：expected={EXPECTED_TARGET_COUNT} actual={len(frame)}"
        )
    return frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_open_dates(path: Path = CALENDAR_PATH) -> list[str]:
    frame = pd.read_csv(path, dtype={"cal_date": str}, low_memory=False)
    required = {"cal_date", "is_open"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"交易日历缺少字段：{missing}")
    opened = frame[pd.to_numeric(frame["is_open"], errors="coerce").eq(1)]
    return sorted(opened["cal_date"].astype(str).str.replace(r"\.0$", "", regex=True).unique())


def build_cluster_jobs(
    targets: pd.DataFrame,
    open_dates: list[str],
    *,
    max_open_days: int = MAX_OPEN_DAYS_PER_REQUEST,
) -> pd.DataFrame:
    """按股票和交易日跨度合并请求，保证理论返回行数不超过8,000。"""

    if max_open_days <= 0 or max_open_days * 241 > 8000:
        raise ValueError("max_open_days必须满足1~33，防止stk_mins单次超过8000行")
    date_index = {date: index for index, date in enumerate(open_dates)}
    rows: list[dict[str, Any]] = []
    for ts_code, group in targets.groupby("ts_code", sort=True):
        dates = sorted(set(group["trade_date"].astype(str)))
        unknown = sorted(set(dates) - set(date_index))
        if unknown:
            raise ValueError(f"{ts_code}目标日期不在交易日历：{unknown[:3]}")
        cluster: list[str] = []
        cluster_start_index = -1
        for trade_date in dates:
            current_index = date_index[trade_date]
            if cluster and current_index - cluster_start_index >= max_open_days:
                rows.append(_job_row(str(ts_code), cluster, date_index))
                cluster = []
            if not cluster:
                cluster_start_index = current_index
            cluster.append(trade_date)
        if cluster:
            rows.append(_job_row(str(ts_code), cluster, date_index))
    return pd.DataFrame(rows).sort_values(["start_date", "ts_code"]).reset_index(drop=True)


def _job_row(ts_code: str, dates: list[str], date_index: dict[str, int]) -> dict[str, Any]:
    start_date = dates[0]
    end_date = dates[-1]
    span = date_index[end_date] - date_index[start_date] + 1
    return {
        "job_key": f"{ts_code}|{start_date}|{end_date}",
        "ts_code": ts_code,
        "start_date": start_date,
        "end_date": end_date,
        "open_day_span": int(span),
        "target_count": int(len(dates)),
        "target_dates": ";".join(dates),
    }


def normalize_tushare_bars(raw: pd.DataFrame | None, *, target_dates: set[str]) -> pd.DataFrame:
    columns = [
        "trade_date", "ts_code", "hhmm", "open", "high", "low", "close",
        "volume", "amount", "source",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns)
    frame = raw.copy()
    required = {"trade_time", "open", "high", "low", "close", "vol", "amount"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"stk_mins缺少字段：{missing}")
    parsed = pd.to_datetime(frame["trade_time"], errors="coerce")
    frame["trade_date"] = parsed.dt.strftime("%Y%m%d")
    frame["hhmm"] = parsed.dt.strftime("%H%M")
    frame = frame[frame["trade_date"].isin(target_dates)].copy()
    frame = frame.rename(columns={"vol": "volume"})
    frame["source"] = "TUSHARE_STK_MINS_1M_UNADJUSTED"
    normalized = normalize_minute_bars(frame)
    normalized["source"] = "TUSHARE_STK_MINS_1M_UNADJUSTED"
    return normalized[columns]


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def job_part_path(job: Any, parts_dir: Path = PARTS_DIR) -> Path:
    """生成同时兼容macOS和Windows的稳定分片名。"""

    filename = f"{job.ts_code}_{job.start_date}_{job.end_date}.csv"
    return parts_dir / filename


def consolidate_minute_parts(
    *,
    output_path: Path = OUTPUT_PATH,
    parts_dir: Path = PARTS_DIR,
) -> pd.DataFrame:
    """把已有总表和请求分片去重合并；分片保留用于断点审计。"""

    frames: list[pd.DataFrame] = []
    existing = load_csv(
        output_path,
        dtype={"trade_date": str, "ts_code": str, "hhmm": str},
    )
    if not existing.empty:
        frames.append(existing)
    if parts_dir.exists():
        for path in sorted(parts_dir.glob("*.csv")):
            part = load_csv(
                path,
                dtype={"trade_date": str, "ts_code": str, "hhmm": str},
            )
            if not part.empty:
                frames.append(part)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(["trade_date", "ts_code", "hhmm"], keep="last")
    result = result.sort_values(["trade_date", "ts_code", "hhmm"]).reset_index(drop=True)
    atomic_write_csv(result, output_path)
    return result


def recover_missing_parts_from_output(
    jobs: pd.DataFrame,
    *,
    output_path: Path = OUTPUT_PATH,
    parts_dir: Path = PARTS_DIR,
) -> int:
    """把分片机制启用前已写入总表的完整job恢复为可独立重建的分片。"""

    missing_jobs = [
        job
        for job in jobs.itertuples(index=False)
        if not job_part_path(job, parts_dir).exists()
    ]
    if not missing_jobs:
        return 0
    existing = load_csv(
        output_path,
        dtype={"trade_date": str, "ts_code": str, "hhmm": str},
    )
    if existing.empty:
        return 0
    recovered = 0
    for job in missing_jobs:
        target_dates = set(str(job.target_dates).split(";"))
        part = existing[
            existing["ts_code"].astype(str).eq(str(job.ts_code))
            & existing["trade_date"].astype(str).isin(target_dates)
        ].copy()
        counts = part.groupby("trade_date").size() if not part.empty else pd.Series(dtype=int)
        if not all(int(counts.get(date, 0)) >= MIN_COMPLETE_BAR_COUNT for date in target_dates):
            continue
        atomic_write_csv(
            part.sort_values(["trade_date", "ts_code", "hhmm"]),
            job_part_path(job, parts_dir),
        )
        recovered += 1
    return recovered


def load_csv(path: Path, *, dtype: dict[str, Any] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=dtype, low_memory=False)


def upsert_rows(existing: pd.DataFrame, additions: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if additions.empty:
        return existing
    merged = pd.concat([existing, additions], ignore_index=True)
    return merged.drop_duplicates(keys, keep="last")


def fetch_job(source: Any, job: Any) -> pd.DataFrame:
    start = f"{job.start_date[:4]}-{job.start_date[4:6]}-{job.start_date[6:]} 09:30:00"
    end = f"{job.end_date[:4]}-{job.end_date[4:6]}-{job.end_date[6:]} 15:00:00"
    target_dates = set(str(job.target_dates).split(";"))
    raw = source.get_stock_minute_bars(
        str(job.ts_code), start, end, freq="1min", fields=FIELDS
    )
    bars = normalize_tushare_bars(raw, target_dates=target_dates)
    returned_dates = set(bars["trade_date"].astype(str)) if not bars.empty else set()
    # stk_mins长区间即使理论行数低于8,000，仍可能漏掉区间首日。聚合请求后
    # 只对缺失的冻结目标日做精确单日补取，禁止把供应商截断误记成永久空表。
    for trade_date in sorted(target_dates - returned_dates):
        exact_start = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:30:00"
        )
        exact_end = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 15:00:00"
        exact_raw = source.get_stock_minute_bars(
            str(job.ts_code), exact_start, exact_end, freq="1min", fields=FIELDS
        )
        exact = normalize_tushare_bars(exact_raw, target_dates={trade_date})
        bars = upsert_rows(bars, exact, ["trade_date", "ts_code", "hhmm"])
    if bars.empty:
        return bars
    return bars.sort_values(["trade_date", "ts_code", "hhmm"]).reset_index(drop=True)


def is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    markers = (
        "频率超限",
        "timeout",
        "timed out",
        "connection",
        "remote disconnected",
        "temporarily unavailable",
        "502",
        "503",
        "504",
    )
    return any(marker in message for marker in markers)


def fetch_job_with_retry(
    source: Any,
    job: Any,
    *,
    policy: CollectionPolicy,
    sleep_fn: Any = time.sleep,
) -> tuple[pd.DataFrame, str, int]:
    """仅重试限频/网络瞬时错误；无权限和参数错误立即失败并留证。"""

    last_error = ""
    for attempt in range(1, policy.max_attempts_per_job + 1):
        try:
            return fetch_job(source, job), "", attempt
        except Exception as exc:  # noqa: BLE001 - 采集状态必须保留供应商原始错误
            last_error = f"{type(exc).__name__}:{exc}"
            if attempt >= policy.max_attempts_per_job or not is_retryable_fetch_error(exc):
                return pd.DataFrame(), last_error, attempt
            is_rate_limit = "频率超限" in str(exc)
            delay = (
                policy.rate_limit_backoff_seconds
                if is_rate_limit
                else policy.transient_backoff_seconds
            )
            LOGGER.warning(
                "%s第%d次请求失败，%.1f秒后重试：%s",
                job.job_key,
                attempt,
                delay,
                last_error,
            )
            sleep_fn(delay)
    return pd.DataFrame(), last_error, policy.max_attempts_per_job


def target_status_rows(job: Any, bars: pd.DataFrame, error: str = "") -> pd.DataFrame:
    rows = []
    for trade_date in str(job.target_dates).split(";"):
        count = int(bars["trade_date"].astype(str).eq(trade_date).sum()) if not bars.empty else 0
        if error:
            status = "RETRYABLE_ERROR"
        elif count >= MIN_COMPLETE_BAR_COUNT:
            status = "COMPLETE_1M_NO_QUEUE_DEPTH"
        elif count:
            status = "INCOMPLETE_1M"
        else:
            status = "EMPTY"
        rows.append(
            {
                "target_key": f"{trade_date}|{job.ts_code}",
                "trade_date": trade_date,
                "ts_code": str(job.ts_code),
                "job_key": str(job.job_key),
                "status": status,
                "bar_count": count,
                "error_message": error[:500],
                "updated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            }
        )
    return pd.DataFrame(rows)


def write_summary(
    targets: pd.DataFrame,
    jobs: pd.DataFrame,
    status: pd.DataFrame,
    *,
    request_interval: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    policy: CollectionPolicy | None = None,
) -> dict[str, Any]:
    counts = (
        {str(k): int(v) for k, v in status["status"].value_counts().items()}
        if not status.empty and "status" in status.columns
        else {}
    )
    complete = int(counts.get("COMPLETE_1M_NO_QUEUE_DEPTH", 0))
    payload = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "strategy": "D",
        "source": "TUSHARE_STK_MINS_1M_UNADJUSTED",
        "formal_rule_modified": False,
        "target_count": int(len(targets)),
        "clustered_request_count": int(len(jobs)),
        "access_tier": policy.access_tier if policy else "UNKNOWN",
        "request_limit_per_minute": (
            int(policy.request_limit_per_minute) if policy else 0
        ),
        "request_interval_seconds": float(request_interval),
        "estimated_hours_at_current_rate": round(
            len(jobs) * float(request_interval) / 3600, 2
        ),
        "complete_target_count": complete,
        "pending_target_count": int(len(targets) - complete),
        "status_counts": counts,
        "path_layer_complete": bool(complete == len(targets)),
        "queue_depth_layer_complete": False,
        "certification_eligible": False,
        "limitations": [
            "一分钟OHLCV仍无法确定同一分钟内多次炸板和回封的先后顺序。",
            "stk_mins不含历史买一排队顺序，始终封板委托不能证明成交。",
            "当前6,848目标只覆盖现行D最终strong日，不能单独还原所有交易日盘中情绪。",
        ],
    }
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填D完整触板母池Tushare历史1分钟K")
    parser.add_argument("--targets", type=Path, default=TARGET_PATH)
    parser.add_argument("--calendar", type=Path, default=CALENDAR_PATH)
    parser.add_argument("--collection-config", type=Path, default=COLLECTION_CONFIG_PATH)
    parser.add_argument("--limit-jobs", type=int, default=0)
    parser.add_argument(
        "--consolidate-only",
        action="store_true",
        help="不请求网络，只把已落盘分片合并为总分钟文件。",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=None,
        help="覆盖配置中的请求间隔；不得小于60/每分钟请求上限。",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_collection_policy(args.collection_config)
    request_interval = (
        policy.request_interval_seconds
        if args.request_interval is None
        else float(args.request_interval)
    )
    if request_interval < policy.minimum_request_interval_seconds:
        raise ValueError(
            "请求间隔超过已购权限频次："
            f"requested={request_interval} minimum={policy.minimum_request_interval_seconds}"
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    targets = load_targets(args.targets)
    jobs = build_cluster_jobs(targets, load_open_dates(args.calendar))
    status = load_csv(
        STATUS_PATH,
        dtype={"target_key": str, "trade_date": str, "ts_code": str, "job_key": str},
    )
    completed_keys = set(
        status.loc[
            status.get("status", pd.Series(dtype=str)).astype(str).eq("COMPLETE_1M_NO_QUEUE_DEPTH"),
            "target_key",
        ].astype(str)
    ) if not status.empty else set()
    pending_jobs = jobs[
        jobs.apply(
            lambda row: any(
                f"{date}|{row.ts_code}" not in completed_keys
                for date in str(row.target_dates).split(";")
            ),
            axis=1,
        )
    ].copy()
    payload = write_summary(
        targets, jobs, status, request_interval=request_interval, policy=policy
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"待请求job：{len(pending_jobs)}/{len(jobs)}")
    if args.dry_run:
        return 0
    if args.consolidate_only:
        recovered = recover_missing_parts_from_output(jobs)
        if recovered:
            print(f"已从总表恢复旧分片：{recovered}个")
        consolidated = consolidate_minute_parts()
        print(f"已合并一分钟总表：{len(consolidated)}行")
        return 0
    if args.limit_jobs > 0:
        pending_jobs = pending_jobs.head(args.limit_jobs)

    config = load_json_config(ROOT / "config/config.json")
    ensure_tushare_token(config, project_root=ROOT)
    from src.data_source import TushareDataSource

    source = TushareDataSource(ROOT / "config/config.json")
    job_status = load_csv(JOB_STATUS_PATH, dtype={"job_key": str})
    for index, job in enumerate(pending_jobs.itertuples(index=False), start=1):
        fetched, error, attempt_count = fetch_job_with_retry(
            source,
            job,
            policy=policy,
        )
        if error:
            LOGGER.error("%s失败：%s", job.job_key, error)
        if not fetched.empty:
            atomic_write_csv(fetched, job_part_path(job))
        additions = target_status_rows(job, fetched, error)
        # 同一聚合请求里可能同时包含已完成和待补目标；失败或空返回不得把
        # 之前完整的目标降级成ERROR/EMPTY。
        additions = additions[~additions["target_key"].astype(str).isin(completed_keys)]
        status = upsert_rows(status, additions, ["target_key"])
        completed_keys.update(
            additions.loc[
                additions["status"].eq("COMPLETE_1M_NO_QUEUE_DEPTH"), "target_key"
            ].astype(str)
        )
        atomic_write_csv(status.sort_values(["trade_date", "ts_code"]), STATUS_PATH)
        job_row = pd.DataFrame(
            [{
                "job_key": job.job_key,
                "ts_code": job.ts_code,
                "start_date": job.start_date,
                "end_date": job.end_date,
                "target_count": job.target_count,
                "returned_target_bar_count": int(len(fetched)),
                "attempt_count": int(attempt_count),
                "status": "ERROR" if error else "DONE",
                "error_message": error[:500],
                "updated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            }]
        )
        job_status = upsert_rows(job_status, job_row, ["job_key"])
        atomic_write_csv(job_status.sort_values("job_key"), JOB_STATUS_PATH)
        write_summary(
            targets, jobs, status, request_interval=request_interval, policy=policy
        )
        LOGGER.info("进度 %d/%d：%s bars=%d", index, len(pending_jobs), job.job_key, len(fetched))
        if index < len(pending_jobs):
            time.sleep(request_interval)
    recovered = recover_missing_parts_from_output(jobs)
    if recovered:
        LOGGER.info("已从总表恢复旧分片：jobs=%d", recovered)
    consolidated = consolidate_minute_parts()
    LOGGER.info("一分钟分片合并完成：bars=%d", len(consolidated))
    print(
        json.dumps(
            write_summary(
                targets, jobs, status, request_interval=request_interval, policy=policy
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
