from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class ExecutionDataAuditor:
    """审计当前数据是否足够支撑真实成交验证。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("execution_data_audit")
        self.audit_config = self.config.get("execution_audit", {})
        self.output_report_path = self.project_root / self.audit_config.get(
            "output_report_path", "reports/execution_data_audit.csv"
        )
        self.output_summary_path = self.project_root / self.audit_config.get(
            "output_summary_path", "reports/execution_data_audit_summary.csv"
        )

    def audit(self) -> dict[str, Path]:
        data_status = self.audit_data_sets()
        report = self.audit_validation_items(data_status)
        summary = self.build_summary(data_status, report)

        mkdir_p(self.output_report_path.parent)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")

        self.logger.info("成交真实性数据审计报告已生成: %s", self.output_report_path)
        self.logger.info("成交真实性数据审计汇总已生成: %s", self.output_summary_path)
        return {
            "report": self.output_report_path,
            "summary": self.output_summary_path,
        }

    def audit_data_sets(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for item in self.audit_config.get("required_data_sets", []):
            data_key = item["data_key"]
            path = self.project_root / item["path"]
            required_columns = list(item.get("required_columns", []))
            status[data_key] = self.inspect_file(
                data_key=data_key,
                data_name=item.get("data_name", data_key),
                path=path,
                required_columns=required_columns,
            )
        return status

    def inspect_file(
        self,
        data_key: str,
        data_name: str,
        path: Path,
        required_columns: list[str],
    ) -> dict[str, Any]:
        if not path.exists():
            return {
                "data_key": data_key,
                "data_name": data_name,
                "path": str(path),
                "exists": False,
                "has_required_columns": False,
                "row_count": 0,
                "missing_columns": ",".join(required_columns),
                "available_columns": "",
                "status": "MISSING",
            }

        columns = self.read_header(path)
        missing_columns = [column for column in required_columns if column not in columns]
        row_count = self.count_csv_rows(path)
        has_required_columns = not missing_columns
        status = "READY" if has_required_columns and row_count > 0 else "INCOMPLETE"
        return {
            "data_key": data_key,
            "data_name": data_name,
            "path": str(path),
            "exists": True,
            "has_required_columns": has_required_columns,
            "row_count": row_count,
            "missing_columns": ",".join(missing_columns),
            "available_columns": ",".join(columns),
            "status": status,
        }

    @staticmethod
    def read_header(path: Path) -> list[str]:
        try:
            columns = pd.read_csv(path, nrows=0, low_memory=False).columns.tolist()
        except pd.errors.EmptyDataError:
            return []
        return [str(column).lstrip("\ufeff") for column in columns]

    @staticmethod
    def count_csv_rows(path: Path) -> int:
        if path.suffix.lower() != ".csv":
            return 0
        with path.open("rb") as file:
            line_count = sum(1 for _ in file)
        return max(line_count - 1, 0)

    def audit_validation_items(self, data_status: dict[str, dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for item in self.audit_config.get("validation_items", []):
            can_use_keys = list(item.get("can_use_data_keys", []))
            need_keys = list(item.get("needs_data_keys", []))
            available_keys = [key for key in can_use_keys if self.is_ready(data_status, key)]
            missing_keys = [key for key in need_keys if not self.is_ready(data_status, key)]

            if not missing_keys:
                verification_level = "CAN_VERIFY"
                conclusion = "当前数据可以支撑该项验证。"
                next_action = "用现有数据纳入成交回放。"
            elif available_keys:
                verification_level = "PARTIAL_PROXY"
                conclusion = "当前只能做保守近似，不能做盘口级精确验证。"
                next_action = f"补充数据: {self.describe_data_keys(data_status, missing_keys)}"
            else:
                verification_level = "CANNOT_VERIFY"
                conclusion = "当前数据不足，不能把该项作为实盘真实性证据。"
                next_action = f"补充数据: {self.describe_data_keys(data_status, missing_keys)}"

            rows.append(
                {
                    "validation_item": item["item"],
                    "verification_level": verification_level,
                    "current_model": item.get("current_model", ""),
                    "available_data_keys": ",".join(available_keys),
                    "missing_data_keys": ",".join(missing_keys),
                    "conclusion": conclusion,
                    "next_action": next_action,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def is_ready(data_status: dict[str, dict[str, Any]], data_key: str) -> bool:
        return data_status.get(data_key, {}).get("status") == "READY"

    @staticmethod
    def describe_data_keys(data_status: dict[str, dict[str, Any]], data_keys: list[str]) -> str:
        names = []
        for key in data_keys:
            info = data_status.get(key, {})
            names.append(info.get("data_name", key))
        return "、".join(names)

    def build_summary(self, data_status: dict[str, dict[str, Any]], report: pd.DataFrame) -> pd.DataFrame:
        data_rows = [
            {
                "summary_type": "data_set",
                "name": info["data_name"],
                "status": info["status"],
                "row_count": info["row_count"],
                "detail": info["path"],
            }
            for info in data_status.values()
        ]
        validation_rows = [
            {
                "summary_type": "validation_item",
                "name": row.validation_item,
                "status": row.verification_level,
                "row_count": 0,
                "detail": row.conclusion,
            }
            for row in report.itertuples(index=False)
        ]
        return pd.DataFrame(data_rows + validation_rows)
