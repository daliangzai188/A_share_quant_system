#!/usr/bin/env python3
"""断点回填D完整触板母池的Tushare历史1分钟K。

这只是盘中价格路径层，不是排队成交认证。``stk_mins``没有历史买一队列，
所以即使40,336个目标全部得到241根一分钟K，也不能把始终封板的委托记为成交。

为平衡调用次数和无关行下载量，本脚本按交易日横截面聚合，单次最多33只
股票（33*241=7,953，严格小于接口8,000行上限）。聚合返回缺某只时，
只对该股该日精确补取。每25个job原子保存状态，支持中断续跑。项目于2026-08-22
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
import hashlib
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


TARGET_PATH = ROOT / "data/research/strategy_d_intraday/minute_target_manifest_full_window.csv"
CALENDAR_PATH = ROOT / "data/raw/trade_calendar.csv"
COLLECTION_CONFIG_PATH = ROOT / "config/strategy_d_intraday_collection.json"
KNOWN_GAPS_PATH = ROOT / "config/strategy_d_intraday_known_data_gaps.json"
OUTPUT_PATH = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
PARTS_DIR = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare_parts"
STATUS_PATH = ROOT / "data/research/strategy_d_intraday/tushare_1m_status.csv"
JOB_STATUS_PATH = ROOT / "data/research/strategy_d_intraday/tushare_1m_job_status.csv"
SUMMARY_PATH = ROOT / "reports/strategy_d_intraday_research/tushare_1m_collection.json"
EXPECTED_TARGET_COUNT = 40336
MAX_OPEN_DAYS_PER_REQUEST = 5
MIN_COMPLETE_BAR_COUNT = 241
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
    request_mode: str
    max_codes_per_request: int
    checkpoint_every_jobs: int

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
        request_mode=str(section.get("request_mode", "")).strip(),
        max_codes_per_request=int(section.get("max_codes_per_request", 0)),
        checkpoint_every_jobs=int(section.get("checkpoint_every_jobs", 0)),
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
    if policy.request_mode != "CROSS_SECTION_BY_TRADE_DATE":
        raise ValueError("D全窗口采集必须使用CROSS_SECTION_BY_TRADE_DATE")
    if not 1 <= policy.max_codes_per_request <= 33:
        raise ValueError("max_codes_per_request必须为1~33，防止单次超过8000行")
    if policy.checkpoint_every_jobs <= 0:
        raise ValueError("checkpoint_every_jobs必须为正整数")
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


def load_known_data_gaps(
    targets: pd.DataFrame,
    path: Path = KNOWN_GAPS_PATH,
) -> pd.DataFrame:
    """加载精确数据商缺口；只用于报告和fail-closed重放，不伪装完成。"""

    columns = [
        "target_key",
        "trade_date",
        "ts_code",
        "name",
        "market",
        "source",
        "status",
        "handling",
        "evidence",
    ]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload.get("gaps", [])
    gaps = pd.DataFrame(rows)
    required = set(columns)
    missing = sorted(required - set(gaps.columns))
    if missing:
        raise ValueError(f"D一分钟已知缺口缺少字段：{missing}")
    gaps = gaps[columns].copy()
    if gaps["target_key"].duplicated().any():
        raise ValueError("D一分钟已知缺口target_key重复")
    expected_keys = set(targets["target_key"].astype(str))
    unknown = sorted(set(gaps["target_key"].astype(str)) - expected_keys)
    if unknown:
        raise ValueError(f"D一分钟已知缺口不在冻结目标中：{unknown}")
    rebuilt_keys = gaps["trade_date"].astype(str) + "|" + gaps["ts_code"].astype(str)
    if not rebuilt_keys.eq(gaps["target_key"].astype(str)).all():
        raise ValueError("D一分钟已知缺口的日期/代码与target_key不一致")
    allowed_status = {"CONFIRMED_TUSHARE_VENDOR_DATE_MARKET_GAP"}
    if not set(gaps["status"].astype(str)).issubset(allowed_status):
        raise ValueError("D一分钟已知缺口状态未经认证")
    if not gaps["handling"].astype(str).eq("FAIL_CLOSED_KEEP_IN_DENOMINATOR").all():
        raise ValueError("D一分钟已知缺口必须fail-closed且保留在分母")
    return gaps.sort_values("target_key").reset_index(drop=True)


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
        "target_keys": ";".join(f"{date}|{ts_code}" for date in dates),
        "theoretical_return_rows": int(span * 241),
    }


def build_cross_section_jobs(
    targets: pd.DataFrame,
    *,
    max_codes_per_request: int = 33,
) -> pd.DataFrame:
    """同一交易日最多33股一次请求，严格控制在8,000行以内。"""

    if not 1 <= max_codes_per_request <= 33:
        raise ValueError("max_codes_per_request必须为1~33")
    rows: list[dict[str, Any]] = []
    for trade_date, group in targets.groupby("trade_date", sort=True):
        codes = sorted(set(group["ts_code"].astype(str)))
        for offset in range(0, len(codes), max_codes_per_request):
            chunk = codes[offset : offset + max_codes_per_request]
            target_keys = [f"{trade_date}|{code}" for code in chunk]
            digest = hashlib.sha256(";".join(target_keys).encode("utf-8")).hexdigest()[:12]
            chunk_index = offset // max_codes_per_request + 1
            rows.append(
                {
                    "job_key": (
                        f"CROSS_SECTION|{trade_date}|{chunk_index:04d}|{digest}"
                    ),
                    "ts_code": ",".join(chunk),
                    "start_date": str(trade_date),
                    "end_date": str(trade_date),
                    "open_day_span": 1,
                    "target_count": len(chunk),
                    "target_dates": str(trade_date),
                    "target_keys": ";".join(target_keys),
                    "theoretical_return_rows": len(chunk) * 241,
                    "part_filename": (
                        f"cross_{trade_date}_{chunk_index:04d}_{digest}.csv"
                    ),
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["start_date", "job_key"]).reset_index(drop=True)


def job_target_pairs(job: Any) -> list[tuple[str, str]]:
    raw_keys = str(getattr(job, "target_keys", "") or "")
    if raw_keys:
        pairs = []
        for key in raw_keys.split(";"):
            trade_date, ts_code = key.split("|", 1)
            pairs.append((trade_date, ts_code))
        return pairs
    dates = str(job.target_dates).split(";")
    codes = str(job.ts_code).split(",")
    return [(date, code) for date in dates for code in codes]


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

    filename = str(getattr(job, "part_filename", "") or "")
    if not filename:
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
        target_pairs = job_target_pairs(job)
        target_keys = {f"{date}|{code}" for date, code in target_pairs}
        existing_keys = (
            existing["trade_date"].astype(str)
            + "|"
            + existing["ts_code"].astype(str)
        )
        part = existing[existing_keys.isin(target_keys)].copy()
        counts = (
            part.groupby(["trade_date", "ts_code"]).size()
            if not part.empty
            else pd.Series(dtype=int)
        )
        if not all(
            int(counts.get((date, code), 0)) >= MIN_COMPLETE_BAR_COUNT
            for date, code in target_pairs
        ):
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


def csv_data_row_count(path: Path) -> int:
    """不加载873MB分钟总表，流式计算数据行数供覆盖报告核对。"""

    if not path.exists() or path.stat().st_size == 0:
        return 0
    line_count = 0
    last_byte = b""
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            line_count += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if last_byte and last_byte != b"\n":
        line_count += 1
    return max(line_count - 1, 0)


def upsert_rows(existing: pd.DataFrame, additions: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if additions.empty:
        return existing
    merged = pd.concat([existing, additions], ignore_index=True)
    return merged.drop_duplicates(keys, keep="last")


def fetch_job(source: Any, job: Any) -> pd.DataFrame:
    start = f"{job.start_date[:4]}-{job.start_date[4:6]}-{job.start_date[6:]} 09:30:00"
    end = f"{job.end_date[:4]}-{job.end_date[4:6]}-{job.end_date[6:]} 15:00:00"
    target_pairs = job_target_pairs(job)
    target_dates = {date for date, _code in target_pairs}
    expected_keys = {f"{date}|{code}" for date, code in target_pairs}
    raw = source.get_stock_minute_bars(
        str(job.ts_code), start, end, freq="1min", fields=FIELDS
    )
    bars = normalize_tushare_bars(raw, target_dates=target_dates)
    returned_keys = (
        set(bars["trade_date"].astype(str) + "|" + bars["ts_code"].astype(str))
        if not bars.empty
        else set()
    )
    # stk_mins长区间即使理论行数低于8,000，仍可能漏掉区间首日。聚合请求后
    # 只对缺失的冻结目标日做精确单日补取，禁止把供应商截断误记成永久空表。
    for target_key in sorted(expected_keys - returned_keys):
        trade_date, ts_code = target_key.split("|", 1)
        exact_start = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:30:00"
        )
        exact_end = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 15:00:00"
        exact_raw = source.get_stock_minute_bars(
            ts_code, exact_start, exact_end, freq="1min", fields=FIELDS
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
    for trade_date, ts_code in job_target_pairs(job):
        count = (
            int(
                (
                    bars["trade_date"].astype(str).eq(trade_date)
                    & bars["ts_code"].astype(str).eq(ts_code)
                ).sum()
            )
            if not bars.empty
            else 0
        )
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
                "target_key": f"{trade_date}|{ts_code}",
                "trade_date": trade_date,
                "ts_code": ts_code,
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
    known_gaps: pd.DataFrame | None = None,
    merged_bar_count: int | None = None,
) -> dict[str, Any]:
    counts = (
        {str(k): int(v) for k, v in status["status"].value_counts().items()}
        if not status.empty and "status" in status.columns
        else {}
    )
    complete = int(counts.get("COMPLETE_1M_NO_QUEUE_DEPTH", 0))
    complete_keys = (
        set(
            status.loc[
                status["status"].astype(str).eq("COMPLETE_1M_NO_QUEUE_DEPTH"),
                "target_key",
            ].astype(str)
        )
        if not status.empty and {"target_key", "status"}.issubset(status.columns)
        else set()
    )
    gaps = known_gaps if known_gaps is not None else pd.DataFrame()
    known_gap_keys = set(gaps.get("target_key", pd.Series(dtype=str)).astype(str))
    unresolved_known_gap_keys = known_gap_keys - complete_keys
    unresolved_non_known_gap_count = max(
        len(targets) - complete - len(unresolved_known_gap_keys), 0
    )
    prior = {}
    if SUMMARY_PATH.exists():
        try:
            prior = json.loads(SUMMARY_PATH.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            prior = {}
    actual_bar_count = (
        int(merged_bar_count)
        if merged_bar_count is not None
        else int(prior.get("merged_bar_count", 0) or 0)
    )
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
        "request_mode": policy.request_mode if policy else "UNKNOWN",
        "max_codes_per_request": (
            int(policy.max_codes_per_request) if policy else 0
        ),
        "checkpoint_every_jobs": (
            int(policy.checkpoint_every_jobs) if policy else 1
        ),
        "estimated_hours_at_current_rate": round(
            len(jobs) * float(request_interval) / 3600, 2
        ),
        "target_bar_count": int(len(targets) * 241),
        "theoretical_download_row_count": int(
            jobs["theoretical_return_rows"].sum()
        ),
        "download_row_amplification": round(
            float(jobs["theoretical_return_rows"].sum())
            / max(len(targets) * 241, 1),
            2,
        ),
        "complete_target_count": complete,
        "pending_target_count": int(len(targets) - complete),
        "known_vendor_gap_target_count": int(len(unresolved_known_gap_keys)),
        "unresolved_non_known_gap_target_count": int(unresolved_non_known_gap_count),
        "price_path_coverage_rate": round(complete / max(len(targets), 1), 8),
        "merged_bar_count": actual_bar_count,
        "status_counts": counts,
        "path_layer_complete": bool(complete == len(targets)),
        "research_replay_fail_closed_ready": bool(
            complete + len(unresolved_known_gap_keys) == len(targets)
            and unresolved_non_known_gap_count == 0
        ),
        "queue_depth_layer_complete": False,
        "certification_eligible": False,
        "known_vendor_gaps": (
            gaps[gaps["target_key"].astype(str).isin(unresolved_known_gap_keys)]
            .to_dict("records")
            if not gaps.empty
            else []
        ),
        "limitations": [
            "一分钟OHLCV仍无法确定同一分钟内多次炸板和回封的先后顺序。",
            "stk_mins不含历史买一排队顺序，始终封板委托不能证明成交。",
            "40,336目标覆盖484日全窗口；盘中市场情绪仍须按同分钟全市场封板状态重建。",
            "2024-11-28北交所4个目标为Tushare数据商单日市场缺口；留在母样本分母且重放时按缺数无信号处理，禁止删除或插值。",
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
        "--recover-missing-parts",
        action="store_true",
        help="合并前从旧总表恢复缺失分片；仅迁移旧采集格式时人工启用。",
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
    known_gaps = load_known_data_gaps(targets)
    # 仍加载并校验交易日历，防止目标日期漂移；实际请求按同日多股横截面批量。
    open_dates = set(load_open_dates(args.calendar))
    unknown_dates = sorted(set(targets["trade_date"].astype(str)) - open_dates)
    if unknown_dates:
        raise ValueError(f"D一分钟目标日期不在交易日历：{unknown_dates[:5]}")
    jobs = build_cross_section_jobs(
        targets,
        max_codes_per_request=policy.max_codes_per_request,
    )
    status = load_csv(
        STATUS_PATH,
        dtype={"target_key": str, "trade_date": str, "ts_code": str, "job_key": str},
    )
    existing_merged_bar_count = csv_data_row_count(OUTPUT_PATH)
    completed_keys = set(
        status.loc[
            status.get("status", pd.Series(dtype=str)).astype(str).eq("COMPLETE_1M_NO_QUEUE_DEPTH"),
            "target_key",
        ].astype(str)
    ) if not status.empty else set()
    pending_targets = targets[
        ~targets["target_key"].astype(str).isin(completed_keys)
    ].copy()
    pending_jobs = build_cross_section_jobs(
        pending_targets,
        max_codes_per_request=policy.max_codes_per_request,
    )
    payload = write_summary(
        targets,
        jobs,
        status,
        request_interval=request_interval,
        policy=policy,
        known_gaps=known_gaps,
        merged_bar_count=existing_merged_bar_count,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"待请求job：{len(pending_jobs)}/{len(jobs)}")
    if args.dry_run:
        return 0
    if args.consolidate_only:
        recovered = (
            recover_missing_parts_from_output(jobs)
            if args.recover_missing_parts
            else 0
        )
        if recovered:
            print(f"已从总表恢复旧分片：{recovered}个")
        consolidated = consolidate_minute_parts()
        print(f"已合并一分钟总表：{len(consolidated)}行")
        write_summary(
            targets,
            jobs,
            status,
            request_interval=request_interval,
            policy=policy,
            known_gaps=known_gaps,
            merged_bar_count=len(consolidated),
        )
        return 0
    if args.limit_jobs > 0:
        pending_jobs = pending_jobs.head(args.limit_jobs)

    config = load_json_config(ROOT / "config/config.json")
    ensure_tushare_token(config, project_root=ROOT)
    from src.data_source import TushareDataSource

    source = TushareDataSource(ROOT / "config/config.json")
    job_status = load_csv(JOB_STATUS_PATH, dtype={"job_key": str})
    processed = 0
    try:
        for index, job in enumerate(pending_jobs.itertuples(index=False), start=1):
            fetched, error, attempt_count = fetch_job_with_retry(
                source, job, policy=policy
            )
            processed = index
            if error:
                LOGGER.error("%s失败：%s", job.job_key, error)
            if not fetched.empty:
                atomic_write_csv(fetched, job_part_path(job))
            additions = target_status_rows(job, fetched, error)
            # 同一聚合请求里可能同时包含已完成和待补目标；失败或空返回不得把
            # 之前完整的目标降级成ERROR/EMPTY。
            additions = additions[
                ~additions["target_key"].astype(str).isin(completed_keys)
            ]
            status = upsert_rows(status, additions, ["target_key"])
            completed_keys.update(
                additions.loc[
                    additions["status"].eq("COMPLETE_1M_NO_QUEUE_DEPTH"),
                    "target_key",
                ].astype(str)
            )
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
            checkpoint = (
                index % policy.checkpoint_every_jobs == 0
                or index == len(pending_jobs)
            )
            if checkpoint:
                atomic_write_csv(
                    status.sort_values(["trade_date", "ts_code"]), STATUS_PATH
                )
                atomic_write_csv(job_status.sort_values("job_key"), JOB_STATUS_PATH)
                write_summary(
                    targets,
                    jobs,
                    status,
                    request_interval=request_interval,
                    policy=policy,
                    known_gaps=known_gaps,
                    merged_bar_count=existing_merged_bar_count,
                )
                LOGGER.info(
                    "采集进度 %d/%d：complete=%d last=%s bars=%d",
                    index,
                    len(pending_jobs),
                    len(completed_keys),
                    job.job_key,
                    len(fetched),
                )
            if index < len(pending_jobs):
                time.sleep(request_interval)
    finally:
        if processed:
            atomic_write_csv(
                status.sort_values(["trade_date", "ts_code"]), STATUS_PATH
            )
            atomic_write_csv(job_status.sort_values("job_key"), JOB_STATUS_PATH)
            write_summary(
                targets,
                jobs,
                status,
                request_interval=request_interval,
                policy=policy,
                known_gaps=known_gaps,
                merged_bar_count=existing_merged_bar_count,
            )
    if args.limit_jobs > 0:
        LOGGER.info("小批采集完成，不执行全量分片合并")
        return 0
    consolidated = consolidate_minute_parts()
    LOGGER.info("一分钟分片合并完成：bars=%d", len(consolidated))
    print(
        json.dumps(
            write_summary(
                targets,
                jobs,
                status,
                request_interval=request_interval,
                policy=policy,
                known_gaps=known_gaps,
                merged_bar_count=len(consolidated),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
