from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class MinuteBarImporter:
    """导入外部分钟 K 线 CSV，并标准化为系统统一字段。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("minute_data_importer")
        self.minute_config = self.config.get("minute_data", {})
        self.standard_columns = list(
            self.minute_config.get(
                "standard_columns",
                ["ts_code", "trade_date", "trade_time", "open", "high", "low", "close", "volume", "amount"],
            )
        )
        self.column_aliases = dict(self.minute_config.get("column_aliases", {}))
        self.output_minute_bars_path = self.project_root / self.minute_config.get(
            "output_minute_bars_path", "data/processed/minute_bars.csv"
        )
        self.output_import_report_path = self.project_root / self.minute_config.get(
            "output_import_report_path", "reports/minute_data_import_report.csv"
        )
        self.template_path = self.project_root / self.minute_config.get(
            "template_path", "data/templates/minute_bars_template.csv"
        )
        self.chunk_size = int(self.minute_config.get("chunk_size", 300000))
        self.filter_regular_session = bool(self.minute_config.get("filter_regular_session", True))
        self.regular_sessions = [tuple(item) for item in self.minute_config.get("regular_sessions", [])]

    def write_template(self, output_path: str | Path | None = None) -> Path:
        path = Path(output_path) if output_path else self.template_path
        if not path.is_absolute():
            path = self.project_root / path
        template = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240102",
                    "trade_time": "09:30",
                    "open": 9.99,
                    "high": 10.05,
                    "low": 9.98,
                    "close": 10.02,
                    "volume": 100000,
                    "amount": 1002000,
                }
            ],
            columns=self.standard_columns,
        )
        mkdir_p(path.parent)
        template.to_csv(path, index=False, encoding="utf-8-sig")
        self.logger.info("分钟 K 导入模板已生成: %s", path)
        return path

    def import_files(
        self,
        input_paths: Iterable[str | Path],
        output_path: str | Path | None = None,
        overwrite: bool = False,
    ) -> dict[str, Path]:
        paths = [self.resolve_path(path) for path in input_paths]
        if not paths:
            raise ValueError("未提供分钟 K 输入文件。请使用 --input 或 --input-dir。")

        output = self.resolve_path(output_path) if output_path else self.output_minute_bars_path
        frames = []
        report_rows = []
        for path in paths:
            if not path.exists():
                report_rows.append(self.build_report_row(path, "MISSING", 0, 0, 0, 0, "", "", 0))
                continue
            file_frames, report_row = self.import_one_file(path)
            frames.extend(file_frames)
            report_rows.append(report_row)

        imported = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=self.standard_columns)
        existing = self.load_existing_output(output, overwrite=overwrite)
        frames_to_combine = [frame for frame in [existing, imported] if not frame.empty]
        combined = pd.concat(frames_to_combine, ignore_index=True) if frames_to_combine else pd.DataFrame(columns=self.standard_columns)
        before_dedup = len(combined)
        combined = self.finalize_frame(combined)
        duplicate_dropped = before_dedup - len(combined)

        mkdir_p(output.parent)
        combined.to_csv(output, index=False, encoding="utf-8-sig")

        report = pd.DataFrame(report_rows)
        summary = self.build_summary_report(output, imported, combined, duplicate_dropped)
        report = pd.concat([report, summary], ignore_index=True)
        mkdir_p(self.output_import_report_path.parent)
        report.to_csv(self.output_import_report_path, index=False, encoding="utf-8-sig")

        self.logger.info("分钟 K 标准文件已生成: %s, 行数: %s", output, len(combined))
        self.logger.info("分钟 K 导入报告已生成: %s", self.output_import_report_path)
        return {
            "minute_bars": output,
            "import_report": self.output_import_report_path,
        }

    def import_one_file(self, path: Path) -> tuple[list[pd.DataFrame], dict[str, Any]]:
        frames = []
        input_rows = 0
        valid_rows = 0
        invalid_rows = 0
        first_date = ""
        last_date = ""
        stock_codes: set[str] = set()
        try:
            reader = pd.read_csv(path, chunksize=self.chunk_size, low_memory=False)
            for chunk in reader:
                input_rows += len(chunk)
                normalized = self.normalize_frame(chunk)
                valid_rows += len(normalized)
                invalid_rows += len(chunk) - len(normalized)
                if not normalized.empty:
                    frames.append(normalized)
                    stock_codes.update(normalized["ts_code"].astype(str).unique().tolist())
                    first_date = min([date for date in [first_date, normalized["trade_date"].min()] if date])
                    last_date = max([date for date in [last_date, normalized["trade_date"].max()] if date])
        except Exception as exc:
            return [], self.build_report_row(path, f"FAILED: {exc}", input_rows, 0, input_rows, 0, "", "", 0)

        status = "IMPORTED" if valid_rows else "NO_VALID_ROWS"
        return frames, self.build_report_row(
            path,
            status,
            input_rows,
            valid_rows,
            invalid_rows,
            0,
            first_date,
            last_date,
            len(stock_codes),
        )

    def normalize_frame(self, raw: pd.DataFrame) -> pd.DataFrame:
        frame = raw.copy()
        frame.columns = [str(column).lstrip("\ufeff").strip() for column in frame.columns]
        frame = frame.rename(columns=self.build_rename_map(frame.columns))
        if "datetime" in frame.columns and ("trade_date" not in frame.columns or "trade_time" not in frame.columns):
            parsed = pd.to_datetime(frame["datetime"], errors="coerce")
            if "trade_date" not in frame.columns:
                frame["trade_date"] = parsed.dt.strftime("%Y%m%d")
            if "trade_time" not in frame.columns:
                frame["trade_time"] = parsed.dt.strftime("%H:%M")

        missing_columns = [column for column in self.standard_columns if column not in frame.columns]
        if missing_columns:
            self.logger.warning("分钟 K 输入缺少字段: %s", ",".join(missing_columns))
            return pd.DataFrame(columns=self.standard_columns)

        normalized = frame[self.standard_columns].copy()
        normalized["ts_code"] = normalized["ts_code"].map(self.normalize_ts_code)
        normalized["trade_date"] = normalized["trade_date"].map(self.normalize_trade_date)
        normalized["trade_time"] = normalized["trade_time"].map(self.normalize_trade_time)
        for column in ["open", "high", "low", "close", "volume", "amount"]:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

        normalized = normalized.dropna(subset=["ts_code", "trade_date", "trade_time", "open", "high", "low", "close"])
        normalized = normalized[
            (normalized["high"] >= normalized["low"])
            & (normalized["open"] > 0)
            & (normalized["high"] > 0)
            & (normalized["low"] > 0)
            & (normalized["close"] > 0)
        ].copy()
        if self.filter_regular_session:
            normalized = normalized[normalized["trade_time"].map(self.in_regular_session)].copy()
        return normalized[self.standard_columns]

    def build_rename_map(self, columns: Iterable[str]) -> dict[str, str]:
        column_set = set(columns)
        rename_map = {}
        for standard_name, aliases in self.column_aliases.items():
            for alias in aliases:
                if alias in column_set and standard_name not in column_set:
                    rename_map[alias] = standard_name
                    break
        return rename_map

    @staticmethod
    def normalize_ts_code(value: object) -> str | None:
        text = str(value).strip().upper()
        if not text or text == "NAN":
            return None
        if "." in text:
            symbol, exchange = text.split(".", 1)
            return f"{symbol.zfill(6)}.{exchange}"
        if text.startswith(("SZ", "SH")) and len(text) >= 8:
            return f"{text[2:].zfill(6)}.{text[:2]}"
        if text.isdigit() and len(text) <= 6:
            exchange = "SH" if text.startswith(("6", "9")) else "SZ"
            return f"{text.zfill(6)}.{exchange}"
        return text

    @staticmethod
    def normalize_trade_date(value: object) -> str | None:
        text = str(value).strip()
        if not text or text == "nan":
            return None
        digits = "".join(char for char in text if char.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        return None

    @staticmethod
    def normalize_trade_time(value: object) -> str | None:
        text = str(value).strip()
        if not text or text == "nan":
            return None
        if ":" in text:
            parts = text.split(":")
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        digits = "".join(char for char in text if char.isdigit())
        if len(digits) >= 4:
            return f"{int(digits[:2]):02d}:{int(digits[2:4]):02d}"
        return None

    def in_regular_session(self, trade_time: str | None) -> bool:
        if trade_time is None:
            return False
        return any(start <= trade_time <= end for start, end in self.regular_sessions)

    def load_existing_output(self, output_path: Path, overwrite: bool) -> pd.DataFrame:
        if overwrite or not output_path.exists():
            return pd.DataFrame(columns=self.standard_columns)
        return pd.read_csv(output_path, dtype={"ts_code": str, "trade_date": str, "trade_time": str}, low_memory=False)

    def finalize_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=self.standard_columns)
        finalized = frame[self.standard_columns].copy()
        finalized = finalized.drop_duplicates(subset=["ts_code", "trade_date", "trade_time"], keep="last")
        finalized = finalized.sort_values(["ts_code", "trade_date", "trade_time"]).reset_index(drop=True)
        return finalized

    def build_summary_report(
        self,
        output_path: Path,
        imported: pd.DataFrame,
        combined: pd.DataFrame,
        duplicate_dropped: int,
    ) -> pd.DataFrame:
        if combined.empty:
            first_date = ""
            last_date = ""
            stock_count = 0
        else:
            first_date = str(combined["trade_date"].min())
            last_date = str(combined["trade_date"].max())
            stock_count = int(combined["ts_code"].nunique())
        return pd.DataFrame(
            [
                self.build_report_row(
                    output_path,
                    "OUTPUT_SUMMARY",
                    len(imported),
                    len(combined),
                    0,
                    duplicate_dropped,
                    first_date,
                    last_date,
                    stock_count,
                )
            ]
        )

    @staticmethod
    def build_report_row(
        path: Path,
        status: str,
        input_rows: int,
        valid_rows: int,
        invalid_rows: int,
        duplicate_dropped: int,
        first_date: str,
        last_date: str,
        stock_count: int,
    ) -> dict[str, Any]:
        return {
            "path": str(path),
            "status": status,
            "input_rows": int(input_rows),
            "valid_rows": int(valid_rows),
            "invalid_rows": int(invalid_rows),
            "duplicate_dropped": int(duplicate_dropped),
            "first_date": first_date,
            "last_date": last_date,
            "stock_count": int(stock_count),
        }

    def resolve_path(self, path: str | Path | None) -> Path:
        if path is None:
            raise ValueError("路径不能为空。")
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = self.project_root / resolved
        return resolved
