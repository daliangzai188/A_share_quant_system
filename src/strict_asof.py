from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from src.utils.config import get_project_root, mkdir_p


STRICT_ASOF_STANDARD_ID = "A_SYSTEM_STRICT_ASOF_V1"
STRICT_MODE = "STRICT"
STRICT_DISCOVERY = "STRICT_DISCOVERY"
LOCKED_OOS = "LOCKED_OOS"
WALK_FORWARD = "WALK_FORWARD"
ALLOWED_PROTOCOLS = {STRICT_DISCOVERY, LOCKED_OOS, WALK_FORWARD}


class StrictAsOfError(RuntimeError):
    """严格 as-of 门禁失败。失败时不得继续生成策略收益结论。"""


@dataclass(frozen=True)
class PointInTimeContract:
    dataset_name: str
    signal_date_column: str = "trade_date"
    as_of_date_column: str = "as_of_date"
    key_columns: tuple[str, ...] = ("trade_date", "ts_code")
    require_as_of_equal_signal: bool = True
    reliability_column: str | None = "is_fill_score_reliable"
    model_training_end_date_column: str | None = "model_training_end_date"
    method_column: str | None = "fill_probability_method"
    expected_method: str | None = "asof_turnover_space_proxy_v2"
    availability_date_columns: tuple[str, ...] = ()
    allow_empty: bool = False


@dataclass(frozen=True)
class StrictAsOfAudit:
    standard_id: str
    dataset_name: str
    row_count: int
    signal_date_count: int
    first_signal_date: str
    last_signal_date: str
    duplicate_key_count: int
    invalid_signal_date_count: int
    invalid_as_of_date_count: int
    as_of_after_signal_count: int
    as_of_mismatch_count: int
    reliable_method_bad_count: int
    reliable_training_end_missing_count: int
    reliable_training_not_prior_count: int
    availability_date_invalid_count: int
    availability_after_signal_count: int
    passed: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FORBIDDEN_SELECTION_COLUMNS = {
    "account_return",
    "buy_executed",
    "buy_price",
    "buy_price_before_slippage",
    "buy_trade_date",
    "daily_return",
    "equity",
    "exit_close",
    "exit_open",
    "exit_price",
    "exit_price_before_slippage",
    "exit_trade_date",
    "final_equity",
    "future_return",
    "gross_return",
    "is_win",
    "net_return",
    "next_close",
    "next_high",
    "next_low",
    "next_open",
    "next_trade_date",
    "realized_pnl",
    "sell_executed",
    "sell_price",
    "stock_return_before_fees",
    "trade_pnl",
    "weighted_return",
}
FORBIDDEN_SELECTION_PATTERNS = (
    re.compile(r"^d\d+_(?:trade_date|open|high|low|close|pre_close|pct_chg|vol|amount)$"),
    re.compile(r"^(?:future|forward|next)_"),
    re.compile(r"^exit_(?:trade_date|open|high|low|close|price)"),
    re.compile(r"^(?:realized|post_trade)_"),
    re.compile(r"^historical_reference_"),
)


def _normalise_date(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    return text.str.replace(r"[^0-9]", "", regex=True)


def _valid_date_mask(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, format="%Y%m%d", errors="coerce")
    return values.str.fullmatch(r"\d{8}", na=False) & parsed.notna()


def _truthy(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    return series.astype("string").str.strip().str.lower().isin({"true", "1", "yes", "y"})


def _required_columns(contract: PointInTimeContract) -> list[str]:
    columns = [contract.signal_date_column, contract.as_of_date_column, *contract.key_columns]
    for column in (
        contract.reliability_column,
        contract.model_training_end_date_column,
        contract.method_column,
        *contract.availability_date_columns,
    ):
        if column:
            columns.append(column)
    return list(dict.fromkeys(columns))


def audit_point_in_time_frame(
    frame: pd.DataFrame,
    contract: PointInTimeContract,
    *,
    raise_on_error: bool = True,
) -> StrictAsOfAudit:
    """审计信号特征是否在决策时点真实可见。

    结果列可以与信号列存在同一张宽表中，但选股/排序字段必须另外通过
    ``assert_selection_columns_strict``；这样既允许计算事后收益，又禁止把收益
    或未来价格送回选股逻辑。
    """

    missing = [column for column in _required_columns(contract) if column not in frame.columns]
    if missing:
        raise StrictAsOfError(
            f"严格as-of数据契约失败：{contract.dataset_name} 缺少字段 {missing}。"
        )
    if frame.empty and not contract.allow_empty:
        raise StrictAsOfError(f"严格as-of数据契约失败：{contract.dataset_name} 数据为空。")

    signal = _normalise_date(frame[contract.signal_date_column])
    as_of = _normalise_date(frame[contract.as_of_date_column])
    valid_signal = _valid_date_mask(signal)
    valid_as_of = _valid_date_mask(as_of)
    comparable = valid_signal & valid_as_of

    normalised_keys = frame.loc[:, list(contract.key_columns)].astype("string").copy()
    if contract.signal_date_column in normalised_keys.columns:
        normalised_keys[contract.signal_date_column] = signal
    duplicate_count = int(normalised_keys.duplicated().sum())
    invalid_signal_count = int((~valid_signal).sum())
    invalid_as_of_count = int((~valid_as_of).sum())
    after_count = int((comparable & as_of.gt(signal)).sum())
    mismatch_count = int((comparable & as_of.ne(signal)).sum()) if contract.require_as_of_equal_signal else 0

    reliable = (
        _truthy(frame[contract.reliability_column])
        if contract.reliability_column
        else pd.Series(True, index=frame.index)
    )
    method_bad_count = 0
    if contract.method_column and contract.expected_method:
        method_bad_count = int(
            frame.loc[reliable, contract.method_column]
            .astype("string")
            .fillna("<MISSING>")
            .ne(contract.expected_method)
            .sum()
        )

    training_missing_count = 0
    training_not_prior_count = 0
    if contract.model_training_end_date_column:
        training = _normalise_date(frame[contract.model_training_end_date_column])
        valid_training = _valid_date_mask(training)
        training_missing_count = int((reliable & ~valid_training).sum())
        training_not_prior_count = int(
            (reliable & valid_training & valid_signal & training.ge(signal)).sum()
        )

    availability_invalid_count = 0
    availability_after_count = 0
    for column in contract.availability_date_columns:
        available = _normalise_date(frame[column])
        valid_available = _valid_date_mask(available)
        availability_invalid_count += int((~valid_available).sum())
        availability_after_count += int(
            (valid_available & valid_signal & available.gt(signal)).sum()
        )

    issues: list[str] = []
    counters = {
        "重复信号键": duplicate_count,
        "非法信号日期": invalid_signal_count,
        "非法as-of日期": invalid_as_of_count,
        "as-of晚于信号日": after_count,
        "as-of不等于信号日": mismatch_count,
        "可靠样本成交评分方法错误": method_bad_count,
        "可靠样本缺少模型训练截止日": training_missing_count,
        "模型训练截止日未严格早于信号日": training_not_prior_count,
        "特征可用日期非法": availability_invalid_count,
        "特征可用日期晚于信号日": availability_after_count,
    }
    issues.extend(f"{name}={count}" for name, count in counters.items() if count)
    passed = not issues
    audit = StrictAsOfAudit(
        standard_id=STRICT_ASOF_STANDARD_ID,
        dataset_name=contract.dataset_name,
        row_count=int(len(frame)),
        signal_date_count=int(signal[valid_signal].nunique()),
        first_signal_date=str(signal[valid_signal].min()) if valid_signal.any() else "",
        last_signal_date=str(signal[valid_signal].max()) if valid_signal.any() else "",
        duplicate_key_count=duplicate_count,
        invalid_signal_date_count=invalid_signal_count,
        invalid_as_of_date_count=invalid_as_of_count,
        as_of_after_signal_count=after_count,
        as_of_mismatch_count=mismatch_count,
        reliable_method_bad_count=method_bad_count,
        reliable_training_end_missing_count=training_missing_count,
        reliable_training_not_prior_count=training_not_prior_count,
        availability_date_invalid_count=availability_invalid_count,
        availability_after_signal_count=availability_after_count,
        passed=passed,
        issues=tuple(issues),
    )
    if raise_on_error and not passed:
        raise StrictAsOfError(
            f"严格as-of门禁失败：{contract.dataset_name}；" + "；".join(issues)
        )
    return audit


def assert_selection_columns_strict(
    columns: Iterable[str],
    *,
    context: str,
) -> tuple[str, ...]:
    """禁止把未来价格、退出结果或已实现收益用于过滤、排名和选股。"""

    unique = tuple(dict.fromkeys(str(column) for column in columns if str(column)))
    forbidden = sorted(
        column
        for column in unique
        if column in FORBIDDEN_SELECTION_COLUMNS
        or any(pattern.search(column) for pattern in FORBIDDEN_SELECTION_PATTERNS)
    )
    if forbidden:
        raise StrictAsOfError(
            f"严格as-of选股字段门禁失败：{context} 使用了未来/结果字段 {forbidden}。"
        )
    return unique


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_research_protocol(
    frame: pd.DataFrame,
    section_config: Mapping[str, Any],
    *,
    signal_date_column: str = "trade_date",
    project_root: Path | None = None,
) -> dict[str, Any]:
    """验证研究协议。

    ``STRICT_DISCOVERY`` 允许寻找规则，但产物永远不能发布；正式样本外结论只
    接受冻结规则的 ``LOCKED_OOS`` 或逐折训练在先的 ``WALK_FORWARD``。
    """

    mode = str(section_config.get("asof_mode", "")).upper()
    if mode != STRICT_MODE:
        raise StrictAsOfError(
            "策略收益研究必须显式配置 asof_mode=STRICT；非严格数据只允许做无收益的数据探索。"
        )
    protocol = str(section_config.get("research_protocol", "")).upper()
    if protocol not in ALLOWED_PROTOCOLS:
        raise StrictAsOfError(
            f"research_protocol 必须是 {sorted(ALLOWED_PROTOCOLS)} 之一，当前={protocol or '未配置'}。"
        )

    signal = _normalise_date(frame[signal_date_column])
    release_eligible = False
    details: dict[str, Any] = {}
    if protocol == LOCKED_OOS:
        training_end = str(section_config.get("strategy_training_end_date", "")).replace("-", "")
        evaluation_start = str(section_config.get("evaluation_start_date", "")).replace("-", "")
        frozen_at_text = str(section_config.get("strategy_frozen_at", "")).strip()
        spec_path_text = str(section_config.get("strategy_spec_path", ""))
        expected_hash = str(section_config.get("strategy_spec_sha256", "")).lower()
        if not (_valid_date_mask(pd.Series([training_end])).iloc[0] and _valid_date_mask(pd.Series([evaluation_start])).iloc[0]):
            raise StrictAsOfError("LOCKED_OOS 必须配置有效的 strategy_training_end_date 和 evaluation_start_date。")
        if training_end >= evaluation_start:
            raise StrictAsOfError("LOCKED_OOS 要求策略训练截止日严格早于样本外起始日。")
        try:
            frozen_at = pd.Timestamp(frozen_at_text)
        except (TypeError, ValueError):
            frozen_at = pd.NaT
        if pd.isna(frozen_at):
            raise StrictAsOfError("LOCKED_OOS 必须配置有效的 strategy_frozen_at。")
        frozen_date = frozen_at.strftime("%Y%m%d")
        if training_end > frozen_date or evaluation_start <= frozen_date:
            raise StrictAsOfError("LOCKED_OOS 要求训练先结束、策略先冻结、样本外再开始。")
        if (signal < evaluation_start).any() or (signal <= frozen_date).any():
            raise StrictAsOfError("LOCKED_OOS 输入混入策略冻结日前记录，正式结果只能包含冻结后新产生的样本。")
        if not spec_path_text or not expected_hash:
            raise StrictAsOfError("LOCKED_OOS 必须锁定 strategy_spec_path 和 strategy_spec_sha256。")
        root = project_root or get_project_root()
        spec_path = Path(spec_path_text)
        if not spec_path.is_absolute():
            spec_path = root / spec_path
        if not spec_path.exists():
            raise StrictAsOfError(f"LOCKED_OOS 策略冻结文件不存在：{spec_path}")
        actual_hash = _sha256(spec_path)
        if actual_hash != expected_hash:
            raise StrictAsOfError(
                f"LOCKED_OOS 策略冻结文件哈希漂移：期望={expected_hash}，实际={actual_hash}。"
            )
        release_eligible = True
        details = {
            "strategy_training_end_date": training_end,
            "strategy_frozen_at": frozen_at.isoformat(),
            "evaluation_start_date": evaluation_start,
            "strategy_spec_path": str(spec_path),
            "strategy_spec_sha256": actual_hash,
        }
    elif protocol == WALK_FORWARD:
        training_column = str(
            section_config.get("strategy_training_end_date_column", "strategy_training_end_date")
        )
        if training_column not in frame.columns:
            raise StrictAsOfError(f"WALK_FORWARD 输入缺少逐行训练截止字段：{training_column}。")
        training = _normalise_date(frame[training_column])
        valid_training = _valid_date_mask(training)
        if (~valid_training).any() or (training >= signal).any():
            raise StrictAsOfError("WALK_FORWARD 每行策略训练截止日都必须有效且严格早于信号日。")
        release_eligible = True
        details = {"strategy_training_end_date_column": training_column}

    return {
        "standard_id": STRICT_ASOF_STANDARD_ID,
        "asof_mode": STRICT_MODE,
        "research_protocol": protocol,
        "strict_asof_passed": True,
        "release_eligible": release_eligible,
        "result_scope": "FORMAL_OOS" if release_eligible else "DISCOVERY_ONLY",
        **details,
    }


def validate_strict_research_frame(
    frame: pd.DataFrame,
    *,
    contract: PointInTimeContract,
    selection_columns: Sequence[str],
    section_config: Mapping[str, Any],
    context: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    data_audit = audit_point_in_time_frame(frame, contract)
    audited_columns = assert_selection_columns_strict(selection_columns, context=context)
    protocol = validate_research_protocol(
        frame,
        section_config,
        signal_date_column=contract.signal_date_column,
        project_root=project_root,
    )
    return {
        **protocol,
        "dataset_audit": data_audit.to_dict(),
        "selection_columns": list(audited_columns),
    }


def add_audit_columns(frame: pd.DataFrame, audit: Mapping[str, Any]) -> pd.DataFrame:
    result = frame.copy()
    for column in (
        "standard_id",
        "asof_mode",
        "research_protocol",
        "strict_asof_passed",
        "release_eligible",
        "result_scope",
    ):
        result[f"asof_{column}" if column == "standard_id" else column] = audit.get(column)
    return result


def write_audit_json(path: str | Path, audit: Mapping[str, Any]) -> Path:
    output = Path(path)
    if not output.is_absolute():
        output = get_project_root() / output
    mkdir_p(output.parent)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(audit), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return output
