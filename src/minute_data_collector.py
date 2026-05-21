from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from src.data_source import TushareDataSource
from src.minute_data_importer import MinuteBarImporter
from src.trade_replay import ConservativeTradeReplay
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class StrategyMinuteDataCollector:
    """按当前策略信号采集所需分钟 K 线，不做任何交易。"""

    def __init__(
        self,
        data_source: TushareDataSource | None,
        config_path: str | Path = "config/config.json",
    ) -> None:
        self.data_source = data_source
        self.project_root = get_project_root()
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.logger = get_logger("minute_data_collector")
        self.collection_config = self.config.get("minute_collection", {})
        self.raw_minute_dir = self.project_root / self.collection_config.get("raw_minute_dir", "data/raw/minute")
        self.output_target_preview_path = self.project_root / self.collection_config.get(
            "output_target_preview_path", "reports/minute_data_target_preview.csv"
        )
        self.output_collect_report_path = self.project_root / self.collection_config.get(
            "output_collect_report_path", "reports/minute_data_collect_report.csv"
        )
        self.max_hold_days = int(self.collection_config.get("max_hold_days", 5))
        self.freq = self.collection_config.get("freq", "1min")
        self.asset = self.collection_config.get("asset", "E")
        self.adj = self.collection_config.get("adj")
        self.start_time = self.collection_config.get("start_time", "09:30:00")
        self.end_time = self.collection_config.get("end_time", "15:00:00")
        self.default_overwrite = bool(self.collection_config.get("overwrite", False))
        self.request_sleep_seconds = float(self.collection_config.get("request_sleep_seconds", 0.3))
        self.stop_on_api_limit_error = bool(self.collection_config.get("stop_on_api_limit_error", True))

    def collect_for_strategy(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
        overwrite: bool | None = None,
        import_after_collect: bool = True,
    ) -> dict[str, Path]:
        overwrite = self.default_overwrite if overwrite is None else overwrite
        if self.data_source is None:
            raise RuntimeError("正式采集分钟 K 需要 TushareDataSource。请不要在 dry-run 模式下调用 collect_for_strategy。")
        targets = self.build_strategy_targets(start_date=start_date, end_date=end_date)
        if limit is not None:
            targets = targets.head(limit).copy()
        if targets.empty:
            raise ValueError("没有找到需要采集的策略分钟 K 目标。请先生成策略信号。")

        self.raw_minute_dir.mkdir(parents=True, exist_ok=True)
        report_rows = []
        raw_paths = []
        for target in targets.itertuples(index=False):
            output_path = self.raw_path_for(target.ts_code, target.trade_date)
            raw_paths.append(output_path)
            if output_path.exists() and not overwrite:
                report_rows.append(self.report_row(target, output_path, "SKIPPED_EXISTS", 0, ""))
                continue
            try:
                bars = self.fetch_one_target(target.ts_code, target.trade_date)
                bars = self.normalize_tushare_minute_bars(bars, target.ts_code)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                bars.to_csv(output_path, index=False, encoding="utf-8-sig")
                report_rows.append(self.report_row(target, output_path, "SAVED", len(bars), ""))
                self.logger.info("保存分钟 K: %s, 行数: %s", output_path, len(bars))
            except Exception as exc:
                status = self.classify_error(exc)
                report_rows.append(self.report_row(target, output_path, status, 0, str(exc)))
                self.logger.warning("采集分钟 K 失败: %s %s, %s", target.ts_code, target.trade_date, exc)
                if status in {"FAILED_PERMISSION", "FAILED_API_LIMIT"}:
                    if self.stop_on_api_limit_error:
                        self.logger.warning("分钟 K 接口权限或频率受限，本次采集提前停止，避免继续消耗接口次数。")
                        break
                if status == "FAILED_PERMISSION":
                    break
            if self.request_sleep_seconds > 0:
                time.sleep(self.request_sleep_seconds)

        report = pd.DataFrame(report_rows)
        self.output_collect_report_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_collect_report_path, index=False, encoding="utf-8-sig")
        self.logger.info("分钟 K 采集报告已生成: %s", self.output_collect_report_path)

        outputs = {"collect_report": self.output_collect_report_path}
        if import_after_collect:
            saved_paths = [Path(row["raw_path"]) for row in report_rows if row["status"] in {"SAVED", "SKIPPED_EXISTS"}]
            if saved_paths:
                import_outputs = MinuteBarImporter(config_path=self.config_path).import_files(
                    input_paths=saved_paths,
                    overwrite=False,
                )
                outputs.update(import_outputs)
        return outputs

    def write_target_preview(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> Path:
        targets = self.build_strategy_targets(start_date=start_date, end_date=end_date)
        if limit is not None:
            targets = targets.head(limit).copy()
        self.output_target_preview_path.parent.mkdir(parents=True, exist_ok=True)
        targets.to_csv(self.output_target_preview_path, index=False, encoding="utf-8-sig")
        self.logger.info("分钟 K 采集目标预览已生成: %s, 行数: %s", self.output_target_preview_path, len(targets))
        return self.output_target_preview_path

    def build_strategy_targets(self, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
        replay_adapter = ConservativeTradeReplay(config_path=self.config_path)
        replay_adapter.replay_config = {
            **replay_adapter.replay_config,
            "base_conditions": self.collection_config.get("base_conditions", {}),
            "max_hold_days": self.max_hold_days,
        }
        replay_adapter.max_hold_days = self.max_hold_days
        signals = replay_adapter.load_strategy_signals()
        if start_date:
            signals = signals[signals["trade_date"].astype(str) >= start_date].copy()
        if end_date:
            signals = signals[signals["trade_date"].astype(str) <= end_date].copy()
        forward_prices = replay_adapter.load_forward_prices()
        samples = signals.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")

        rows: list[dict[str, Any]] = []
        collect_buy_day = bool(self.collection_config.get("collect_buy_day", True))
        collect_exit_days = bool(self.collection_config.get("collect_exit_days", True))
        offsets = []
        if collect_buy_day:
            offsets.append(1)
        if collect_exit_days:
            offsets.extend(range(2, self.max_hold_days + 1))

        for row in samples.itertuples(index=False):
            for offset in offsets:
                date_value = getattr(row, f"d{offset}_trade_date", None)
                if pd.isna(date_value):
                    continue
                rows.append(
                    {
                        "signal_trade_date": row.trade_date,
                        "ts_code": row.ts_code,
                        "trade_date": str(date_value),
                        "offset": offset,
                    }
                )
        targets = pd.DataFrame(rows)
        if targets.empty:
            return pd.DataFrame(columns=["signal_trade_date", "ts_code", "trade_date", "offset"])
        return (
            targets.drop_duplicates(subset=["ts_code", "trade_date"])
            .sort_values(["trade_date", "ts_code"])
            .reset_index(drop=True)
        )

    def fetch_one_target(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        start_datetime = f"{trade_date} {self.start_time}"
        end_datetime = f"{trade_date} {self.end_time}"
        return self.data_source.get_minute_bars(
            ts_code=ts_code,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            freq=self.freq,
            asset=self.asset,
            adj=self.adj,
        )

    @staticmethod
    def normalize_tushare_minute_bars(bars: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        if bars.empty:
            return pd.DataFrame(columns=["ts_code", "trade_date", "trade_time", "open", "high", "low", "close", "volume", "amount"])
        frame = bars.copy()
        if "ts_code" not in frame.columns:
            frame["ts_code"] = ts_code
        if "vol" in frame.columns and "volume" not in frame.columns:
            frame = frame.rename(columns={"vol": "volume"})
        if "trade_time" in frame.columns:
            parsed = pd.to_datetime(frame["trade_time"], errors="coerce")
            frame["trade_date"] = parsed.dt.strftime("%Y%m%d")
            frame["trade_time"] = parsed.dt.strftime("%H:%M")
        elif "datetime" in frame.columns:
            parsed = pd.to_datetime(frame["datetime"], errors="coerce")
            frame["trade_date"] = parsed.dt.strftime("%Y%m%d")
            frame["trade_time"] = parsed.dt.strftime("%H:%M")
        columns = ["ts_code", "trade_date", "trade_time", "open", "high", "low", "close", "volume", "amount"]
        for column in columns:
            if column not in frame.columns:
                frame[column] = pd.NA
        return frame[columns]

    def raw_path_for(self, ts_code: str, trade_date: str) -> Path:
        safe_code = ts_code.replace(".", "_")
        return self.raw_minute_dir / trade_date / f"{safe_code}.csv"

    @staticmethod
    def report_row(target: object, raw_path: Path, status: str, row_count: int, message: str) -> dict[str, Any]:
        return {
            "signal_trade_date": target.signal_trade_date,
            "ts_code": target.ts_code,
            "trade_date": target.trade_date,
            "offset": int(target.offset),
            "raw_path": str(raw_path),
            "status": status,
            "row_count": int(row_count),
            "message": message,
        }

    @staticmethod
    def classify_error(exc: Exception) -> str:
        message = str(exc)
        if "没有接口" in message or "访问权限" in message or "权限" in message:
            return "FAILED_PERMISSION"
        if "频率超限" in message or "每分钟" in message or "每天" in message:
            return "FAILED_API_LIMIT"
        if message.strip() == "ERROR.":
            return "FAILED_API_LIMIT"
        return "FAILED"
