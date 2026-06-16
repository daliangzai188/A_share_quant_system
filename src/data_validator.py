from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


@dataclass(frozen=True)
class ValidationResult:
    dataset: str
    trade_date: str
    file_path: str
    exists: bool
    status: str
    row_count: int
    column_count: int
    missing_required_fields: str
    missing_optional_fields: str
    duplicate_key_count: int
    null_total: int
    null_critical_count: int
    message: str


class DataValidator:
    """检查本地采集数据的字段、样本数、重复值、缺失值和跨表对齐情况。"""

    DAILY_REQUIRED_FIELDS = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "pct_chg",
        "vol",
        "amount",
    ]
    DAILY_BASIC_REQUIRED_FIELDS = [
        "ts_code",
        "trade_date",
        "turnover_rate",
        "volume_ratio",
        "float_share",
        "total_mv",
        "circ_mv",
    ]
    LIMIT_REQUIRED_FIELDS = [
        "trade_date",
        "ts_code",
        "name",
        "close",
        "pct_chg",
        "fd_amount",
        "first_time",
        "last_time",
        "open_times",
        "limit",
        "limit_times",
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("data_validator")

        data_config = self.config["data"]
        self.daily_dir = self.project_root / data_config.get("daily_dir", "data/raw/daily")
        self.daily_basic_dir = self.project_root / data_config.get("daily_basic_dir", "data/raw/daily_basic")
        self.limit_list_dir = self.project_root / data_config.get("limit_list_dir", "data/raw/limit_list")
        self.reports_dir = self.project_root / "reports"

        collection_config = self.config.get("collection", {})
        self.daily_optional_fields = self._fields_from_config(collection_config.get("daily_fields", ""))
        self.daily_basic_optional_fields = self._fields_from_config(collection_config.get("daily_basic_fields", ""))
        self.limit_optional_fields = self._fields_from_config(collection_config.get("limit_list_fields", ""))

    def validate(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        output_dir: str | Path = "reports",
    ) -> dict[str, Path]:
        trade_dates = self.discover_trade_dates(start_date=start_date, end_date=end_date)
        if not trade_dates:
            raise RuntimeError("没有发现可校验的本地 CSV 文件，请先运行数据采集脚本。")

        file_results: list[ValidationResult] = []
        alignment_rows: list[dict[str, object]] = []

        for trade_date in trade_dates:
            file_results.append(
                self.validate_file(
                    dataset="daily",
                    trade_date=trade_date,
                    file_path=self.daily_dir / f"{trade_date}.csv",
                    required_fields=self.DAILY_REQUIRED_FIELDS,
                    optional_fields=self.daily_optional_fields,
                    critical_null_fields=["ts_code", "trade_date", "open", "high", "low", "close"],
                )
            )
            file_results.append(
                self.validate_file(
                    dataset="daily_basic",
                    trade_date=trade_date,
                    file_path=self.daily_basic_dir / f"{trade_date}.csv",
                    required_fields=self.DAILY_BASIC_REQUIRED_FIELDS,
                    optional_fields=self.daily_basic_optional_fields,
                    critical_null_fields=["ts_code", "trade_date", "turnover_rate", "circ_mv"],
                )
            )
            file_results.append(
                self.validate_file(
                    dataset="limit_list",
                    trade_date=trade_date,
                    file_path=self.limit_list_dir / f"{trade_date}.csv",
                    required_fields=self.LIMIT_REQUIRED_FIELDS,
                    optional_fields=self.limit_optional_fields,
                    critical_null_fields=["trade_date", "ts_code", "name", "first_time", "open_times", "limit"],
                )
            )
            alignment_rows.append(self.validate_daily_alignment(trade_date))

        output_path = self.project_root / output_dir
        mkdir_p(output_path)

        file_report = pd.DataFrame([result.__dict__ for result in file_results])
        alignment_report = pd.DataFrame(alignment_rows)
        summary_report = self.build_summary(file_report=file_report, alignment_report=alignment_report)

        file_report_path = output_path / "data_quality_files.csv"
        alignment_report_path = output_path / "data_quality_alignment.csv"
        summary_report_path = output_path / "data_quality_summary.csv"

        file_report.to_csv(file_report_path, index=False, encoding="utf-8-sig")
        alignment_report.to_csv(alignment_report_path, index=False, encoding="utf-8-sig")
        summary_report.to_csv(summary_report_path, index=False, encoding="utf-8-sig")

        self.logger.info("数据质量文件级报告已生成: %s", file_report_path)
        self.logger.info("数据质量对齐报告已生成: %s", alignment_report_path)
        self.logger.info("数据质量汇总报告已生成: %s", summary_report_path)

        return {
            "file_report": file_report_path,
            "alignment_report": alignment_report_path,
            "summary_report": summary_report_path,
        }

    def discover_trade_dates(self, start_date: str | None = None, end_date: str | None = None) -> list[str]:
        dates = set()
        for directory in [self.daily_dir, self.daily_basic_dir, self.limit_list_dir]:
            dates.update(path.stem for path in directory.glob("*.csv"))

        filtered_dates = []
        for trade_date in dates:
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            filtered_dates.append(trade_date)
        return sorted(filtered_dates)

    def validate_file(
        self,
        dataset: str,
        trade_date: str,
        file_path: Path,
        required_fields: Iterable[str],
        optional_fields: Iterable[str],
        critical_null_fields: Iterable[str],
    ) -> ValidationResult:
        if not file_path.exists():
            return ValidationResult(
                dataset=dataset,
                trade_date=trade_date,
                file_path=str(file_path),
                exists=False,
                status="FAIL",
                row_count=0,
                column_count=0,
                missing_required_fields=",".join(required_fields),
                missing_optional_fields="",
                duplicate_key_count=0,
                null_total=0,
                null_critical_count=0,
                message="文件不存在",
            )

        data = pd.read_csv(file_path, dtype={"trade_date": str, "ts_code": str})
        columns = set(data.columns)
        required = list(required_fields)
        optional = [field for field in optional_fields if field not in required]
        missing_required = [field for field in required if field not in columns]
        missing_optional = [field for field in optional if field not in columns]
        duplicate_key_count = self._duplicate_key_count(data)
        null_total = int(data.isna().sum().sum())
        present_critical_fields = [field for field in critical_null_fields if field in data.columns]
        null_critical_count = int(data[present_critical_fields].isna().sum().sum()) if present_critical_fields else 0

        status = "PASS"
        messages = []
        if data.empty:
            status = "WARN"
            messages.append("空文件")
        if missing_required:
            status = "FAIL"
            messages.append("缺少必需字段")
        if duplicate_key_count > 0:
            status = "FAIL"
            messages.append("存在 trade_date+ts_code 重复")
        if null_critical_count > 0:
            status = "WARN" if status == "PASS" else status
            messages.append("关键字段存在缺失")
        if missing_optional:
            messages.append("缺少增强字段")

        return ValidationResult(
            dataset=dataset,
            trade_date=trade_date,
            file_path=str(file_path),
            exists=True,
            status=status,
            row_count=len(data),
            column_count=len(data.columns),
            missing_required_fields=",".join(missing_required),
            missing_optional_fields=",".join(missing_optional),
            duplicate_key_count=duplicate_key_count,
            null_total=null_total,
            null_critical_count=null_critical_count,
            message=";".join(messages) if messages else "OK",
        )

    def validate_daily_alignment(self, trade_date: str) -> dict[str, object]:
        daily_path = self.daily_dir / f"{trade_date}.csv"
        basic_path = self.daily_basic_dir / f"{trade_date}.csv"
        if not daily_path.exists() or not basic_path.exists():
            return {
                "trade_date": trade_date,
                "status": "FAIL",
                "daily_rows": 0,
                "daily_basic_rows": 0,
                "missing_in_daily_basic": 0,
                "missing_in_daily": 0,
                "message": "daily 或 daily_basic 文件不存在",
            }

        daily = pd.read_csv(daily_path, dtype={"trade_date": str, "ts_code": str})
        basic = pd.read_csv(basic_path, dtype={"trade_date": str, "ts_code": str})
        daily_codes = set(daily["ts_code"]) if "ts_code" in daily.columns else set()
        basic_codes = set(basic["ts_code"]) if "ts_code" in basic.columns else set()
        missing_basic_codes = daily_codes - basic_codes
        missing_in_basic = len(missing_basic_codes)
        missing_in_basic_bj = len([code for code in missing_basic_codes if code.endswith(".BJ")])
        missing_in_basic_non_bj = missing_in_basic - missing_in_basic_bj
        missing_in_daily = len(basic_codes - daily_codes)
        status = "PASS" if missing_in_basic_non_bj == 0 and missing_in_daily == 0 else "WARN"
        if status == "PASS" and missing_in_basic_bj > 0:
            message = "OK，仅北交所股票缺少 daily_basic，第一阶段沪深A股研究可接受"
        elif status == "PASS":
            message = "OK"
        else:
            message = "daily 与 daily_basic 股票池不完全一致"

        return {
            "trade_date": trade_date,
            "status": status,
            "daily_rows": len(daily),
            "daily_basic_rows": len(basic),
            "missing_in_daily_basic": missing_in_basic,
            "missing_in_daily_basic_bj": missing_in_basic_bj,
            "missing_in_daily_basic_non_bj": missing_in_basic_non_bj,
            "missing_in_daily": missing_in_daily,
            "message": message,
        }

    @staticmethod
    def build_summary(file_report: pd.DataFrame, alignment_report: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for dataset, group in file_report.groupby("dataset"):
            rows.append(
                {
                    "检查项": f"{dataset} 文件质量",
                    "文件数": len(group),
                    "PASS": int((group["status"] == "PASS").sum()),
                    "WARN": int((group["status"] == "WARN").sum()),
                    "FAIL": int((group["status"] == "FAIL").sum()),
                    "总行数": int(group["row_count"].sum()),
                    "备注": "按单日 CSV 统计",
                }
            )
        rows.append(
            {
                "检查项": "daily 与 daily_basic 对齐",
                "文件数": len(alignment_report),
                "PASS": int((alignment_report["status"] == "PASS").sum()) if not alignment_report.empty else 0,
                "WARN": int((alignment_report["status"] == "WARN").sum()) if not alignment_report.empty else 0,
                "FAIL": int((alignment_report["status"] == "FAIL").sum()) if not alignment_report.empty else 0,
                "总行数": int(alignment_report["daily_rows"].sum()) if not alignment_report.empty else 0,
                "备注": "按 ts_code 对齐",
            }
        )
        return pd.DataFrame(rows)

    @staticmethod
    def _fields_from_config(fields: str) -> list[str]:
        return [field.strip() for field in fields.split(",") if field.strip()]

    @staticmethod
    def _duplicate_key_count(data: pd.DataFrame) -> int:
        if {"trade_date", "ts_code"}.issubset(data.columns):
            return int(data.duplicated(subset=["trade_date", "ts_code"]).sum())
        if "ts_code" in data.columns:
            return int(data.duplicated(subset=["ts_code"]).sum())
        return 0
