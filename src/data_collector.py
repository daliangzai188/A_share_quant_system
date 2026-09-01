from __future__ import annotations

from pathlib import Path
import time
from typing import Iterable

import pandas as pd

from src.data_source import TushareDataSource
from src.trading_calendar import TradingCalendar
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class DataCollector:
    """本地数据采集模块，负责从 Tushare 拉取原始数据并保存 CSV。"""

    def __init__(
        self,
        data_source: TushareDataSource,
        config_path: str | Path = "config/config.json",
    ) -> None:
        self.data_source = data_source
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("data_collector")
        self.calendar = TradingCalendar(data_source=data_source, config_path=config_path)

        data_config = self.config["data"]
        self.daily_dir = self.project_root / data_config.get("daily_dir", "data/raw/daily")
        self.daily_basic_dir = self.project_root / data_config.get("daily_basic_dir", "data/raw/daily_basic")
        self.adj_factor_dir = self.project_root / data_config.get(
            "adj_factor_dir", "data/raw/adj_factor"
        )
        self.limit_list_dir = self.project_root / data_config.get("limit_list_dir", "data/raw/limit_list")
        self._stock_names_cache: dict[str, str] | None = None
        self._mkdir_with_retry(self.daily_dir)
        self._mkdir_with_retry(self.daily_basic_dir)
        self._mkdir_with_retry(self.adj_factor_dir)
        self._mkdir_with_retry(self.limit_list_dir)

    def collect_daily_data(
        self,
        start_date: str,
        end_date: str,
        overwrite: bool | None = None,
        include_daily_basic: bool = True,
        include_adj_factor: bool = True,
    ) -> dict[str, int]:
        overwrite = self._resolve_overwrite(overwrite)
        trade_dates = self.calendar.get_open_dates(start_date=start_date, end_date=end_date)
        self.logger.info("开始采集日线数据，交易日数量: %s", len(trade_dates))

        stats = {
            "daily_saved": 0,
            "daily_skipped": 0,
            "daily_basic_saved": 0,
            "daily_basic_skipped": 0,
            "adj_factor_saved": 0,
            "adj_factor_skipped": 0,
        }
        total = len(trade_dates)
        for index, trade_date in enumerate(trade_dates, 1):
            self.logger.info("日线采集进度 %d/%d: trade_date=%s", index, total, trade_date)
            daily_saved = self.collect_daily_by_date(trade_date=trade_date, overwrite=overwrite)
            stats["daily_saved" if daily_saved else "daily_skipped"] += 1

            if include_daily_basic:
                self.logger.info("每日基本面采集进度 %d/%d: trade_date=%s", index, total, trade_date)
                try:
                    basic_saved = self.collect_daily_basic_by_date(trade_date=trade_date, overwrite=overwrite)
                    stats["daily_basic_saved" if basic_saved else "daily_basic_skipped"] += 1
                except Exception as exc:
                    if self._is_permission_error(exc):
                        self.logger.warning(
                            "当前 Tushare 账号没有 daily_basic 权限，本次日线采集将跳过每日基本面。"
                            "后续可以使用 --no-daily-basic 显式跳过。原始错误: %s",
                            exc,
                        )
                        include_daily_basic = False
                    else:
                        raise

            if include_adj_factor:
                self.logger.info("复权因子采集进度 %d/%d: trade_date=%s", index, total, trade_date)
                try:
                    factor_saved = self.collect_adj_factor_by_date(
                        trade_date=trade_date,
                        overwrite=overwrite,
                    )
                    stats["adj_factor_saved" if factor_saved else "adj_factor_skipped"] += 1
                except Exception as exc:
                    if self._is_permission_error(exc):
                        self.logger.warning(
                            "当前 Tushare 账号没有 adj_factor 权限，本次日线采集将停止采集复权因子。"
                            "月度ACDE研究的数据门禁仍会失败关闭。原始错误: %s",
                            exc,
                        )
                        include_adj_factor = False
                    else:
                        raise

        self.logger.info("日线采集完成: %s", stats)
        return stats

    def collect_limit_data(
        self,
        start_date: str,
        end_date: str,
        overwrite: bool | None = None,
    ) -> dict[str, int]:
        overwrite = self._resolve_overwrite(overwrite)
        trade_dates = self.calendar.get_open_dates(start_date=start_date, end_date=end_date)
        self.logger.info("开始采集涨停池数据，交易日数量: %s", len(trade_dates))

        stats = {"limit_saved": 0, "limit_skipped": 0}
        total = len(trade_dates)
        for index, trade_date in enumerate(trade_dates, 1):
            self.logger.info("涨停池采集进度 %d/%d: trade_date=%s", index, total, trade_date)
            saved = self.collect_limit_by_date(trade_date=trade_date, overwrite=overwrite)
            stats["limit_saved" if saved else "limit_skipped"] += 1

        self.logger.info("涨停池采集完成: %s", stats)
        return stats

    def collect_daily_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.daily_dir / f"{trade_date}.csv"
        self.logger.info("检查本地日线行情文件: %s", output_path)
        if self._should_skip(output_path, overwrite):
            self.logger.info("跳过日线行情: %s 已存在且有数据", output_path)
            return False

        fields = self.config["collection"].get("daily_fields")
        started = time.monotonic()
        self.logger.info("请求 Tushare daily: trade_date=%s", trade_date)
        daily = self.data_source.get_daily(trade_date=trade_date, fields=fields)
        if daily.empty:
            self.logger.warning(
                "Tushare 未返回 %s 日线数据，不保存（下次重试可重新采集），用时 %.1f 秒",
                trade_date,
                time.monotonic() - started,
            )
            return False
        self._save_dataframe(daily, output_path)
        self.logger.info("保存日线行情: %s, 行数: %s, 用时 %.1f 秒", output_path, len(daily), time.monotonic() - started)
        return True

    def collect_daily_basic_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.daily_basic_dir / f"{trade_date}.csv"
        self.logger.info("检查本地每日基本面文件: %s", output_path)
        if self._should_skip(output_path, overwrite):
            self.logger.info("跳过每日基本面: %s 已存在且有数据", output_path)
            return False

        fields = self.config["collection"].get("daily_basic_fields")
        started = time.monotonic()
        self.logger.info("请求 Tushare daily_basic: trade_date=%s", trade_date)
        daily_basic = self.data_source.get_daily_basic(trade_date=trade_date, fields=fields)
        if daily_basic.empty:
            self.logger.warning(
                "Tushare 未返回 %s 基本面数据，不保存（下次重试可重新采集），用时 %.1f 秒",
                trade_date,
                time.monotonic() - started,
            )
            return False
        self._save_dataframe(daily_basic, output_path)
        self.logger.info("保存每日基本面: %s, 行数: %s, 用时 %.1f 秒", output_path, len(daily_basic), time.monotonic() - started)
        return True

    def collect_adj_factor_data(
        self,
        start_date: str,
        end_date: str,
        overwrite: bool | None = None,
    ) -> dict[str, int]:
        """按交易日断点续传复权因子，不读取或改写策略产物。"""

        overwrite = self._resolve_overwrite(overwrite)
        trade_dates = self.calendar.get_open_dates(start_date=start_date, end_date=end_date)
        stats = {"adj_factor_saved": 0, "adj_factor_skipped": 0}
        total = len(trade_dates)
        for index, trade_date in enumerate(trade_dates, 1):
            self.logger.info("复权因子采集进度 %d/%d: trade_date=%s", index, total, trade_date)
            saved = self.collect_adj_factor_by_date(
                trade_date=trade_date,
                overwrite=overwrite,
            )
            stats["adj_factor_saved" if saved else "adj_factor_skipped"] += 1
        self.logger.info("复权因子采集完成: %s", stats)
        return stats

    def collect_adj_factor_by_date(
        self,
        trade_date: str,
        overwrite: bool = False,
    ) -> bool:
        output_path = self.adj_factor_dir / f"{trade_date}.csv"
        self.logger.info("检查本地复权因子文件: %s", output_path)
        if self._should_skip(output_path, overwrite):
            self.logger.info("跳过复权因子: %s 已存在且有数据", output_path)
            return False

        started = time.monotonic()
        self.logger.info("请求 Tushare adj_factor: trade_date=%s", trade_date)
        factors = self.data_source.get_adj_factor(trade_date=trade_date)
        required = {"ts_code", "trade_date", "adj_factor"}
        missing = sorted(required.difference(factors.columns))
        if factors.empty:
            self.logger.warning(
                "Tushare 未返回 %s 复权因子，不保存（下次重试可重新采集），用时 %.1f 秒",
                trade_date,
                time.monotonic() - started,
            )
            return False
        if missing:
            raise ValueError(f"{trade_date}复权因子缺少字段: {missing}")
        factors = factors[["ts_code", "trade_date", "adj_factor"]].copy()
        factors["trade_date"] = factors["trade_date"].astype(str).str.replace(
            r"\.0$", "", regex=True
        )
        if not factors["trade_date"].eq(str(trade_date)).all():
            raise ValueError(f"{trade_date}复权因子返回了其他交易日")
        if factors["ts_code"].astype(str).duplicated().any():
            raise ValueError(f"{trade_date}复权因子存在重复股票代码")
        values = pd.to_numeric(factors["adj_factor"], errors="coerce")
        if values.isna().any() or values.le(0).any():
            raise ValueError(f"{trade_date}复权因子存在缺失或非正值")
        factors["adj_factor"] = values
        self._save_dataframe(factors.sort_values("ts_code"), output_path)
        self.logger.info(
            "保存复权因子: %s, 行数: %s, 用时 %.1f 秒",
            output_path,
            len(factors),
            time.monotonic() - started,
        )
        return True

    def collect_limit_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.limit_list_dir / f"{trade_date}.csv"
        self.logger.info("检查本地涨停池文件: %s", output_path)
        if self._should_skip_limit_file(output_path, overwrite):
            self.logger.info("跳过涨停池: %s 已存在且为完整 limit_list_d 口径", output_path)
            return False

        fields = self.config["collection"].get("limit_list_fields")
        limit_type = self.config["collection"].get("limit_type", "U")
        limit_list, fetch_method = self.fetch_limit_list_d_full(
            trade_date=trade_date,
            limit_type=limit_type,
            fields=fields,
        )
        if limit_list.empty:
            if bool(self.config["collection"].get("enable_stk_limit_fallback", True)):
                fallback = self.build_limit_list_from_stk_limit(trade_date=trade_date)
                if not fallback.empty:
                    self._save_dataframe(fallback, output_path)
                    self.logger.warning(
                        "Tushare limit_list_d 未返回 %s 涨停池数据，已改用 stk_limit + daily.close 生成备用涨停池: %s, 行数: %s。"
                        "备用口径只确认收盘封住涨停，不包含首次封板时间、炸板次数、封单金额。",
                        trade_date,
                        output_path,
                        len(fallback),
                    )
                    return True
            self.logger.warning("Tushare 未返回 %s 涨停池数据，不保存（下次重试可重新采集）", trade_date)
            return False
        limit_list["limit_data_source"] = "limit_list_d"
        limit_list["limit_data_quality"] = "full"
        limit_list["strategy_compatible"] = True
        self._save_dataframe(limit_list, output_path)
        self.logger.info("保存涨停池: %s, 行数: %s, fetch_method=%s", output_path, len(limit_list), fetch_method)
        return True

    def fetch_limit_list_d_full(
        self,
        trade_date: str,
        limit_type: str,
        fields: str | None,
    ) -> tuple[pd.DataFrame, str]:
        probes = [
            (
                "limit_list_d_trade_date",
                lambda: self.data_source.get_limit_list(
                    trade_date=trade_date,
                    limit_type=limit_type,
                    fields=fields,
                ),
            ),
            (
                "limit_list_d_range_same_day",
                lambda: self.data_source.get_limit_list_range(
                    start_date=trade_date,
                    end_date=trade_date,
                    limit_type=limit_type,
                    fields=fields,
                ),
            ),
            (
                "query_limit_list_d_trade_date",
                lambda: self.data_source.query_limit_list(
                    trade_date=trade_date,
                    limit_type=limit_type,
                    fields=fields,
                ),
            ),
            (
                "query_limit_list_d_range_same_day",
                lambda: self.data_source.query_limit_list_range(
                    start_date=trade_date,
                    end_date=trade_date,
                    limit_type=limit_type,
                    fields=fields,
                ),
            ),
        ]
        for method, loader in probes:
            try:
                started = time.monotonic()
                self.logger.info("请求 Tushare %s: trade_date=%s", method, trade_date)
                data = loader()
            except Exception as exc:
                self.logger.warning("Tushare %s 获取 %s 涨停池失败: %s", method, trade_date, exc)
                continue
            data = self._filter_trade_date(data, trade_date)
            if not data.empty:
                self.logger.info(
                    "Tushare %s 返回 %s 涨停池数据，行数=%s，用时 %.1f 秒",
                    method,
                    trade_date,
                    len(data),
                    time.monotonic() - started,
                )
                return data, method
            self.logger.info("Tushare %s 未返回 %s 涨停池数据，用时 %.1f 秒", method, trade_date, time.monotonic() - started)
        return pd.DataFrame(), "none"

    @staticmethod
    def _filter_trade_date(data: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        if data.empty:
            return data
        if "trade_date" not in data.columns:
            return data
        return data[data["trade_date"].astype(str) == str(trade_date)].copy()

    def _should_skip_limit_file(self, output_path: Path, overwrite: bool) -> bool:
        if overwrite:
            return False
        if not self._exists_with_retry(output_path):
            return False
        if not self._csv_has_data_row(output_path):
            return False
        try:
            header = pd.read_csv(output_path, nrows=0).columns.tolist()
            if "limit_data_quality" not in header:
                return True
            sample = pd.read_csv(output_path, usecols=["limit_data_quality"], nrows=20)
            quality = sample["limit_data_quality"].fillna("full").astype(str)
            return bool(quality.eq("full").all())
        except (OSError, pd.errors.EmptyDataError, ValueError):
            return False

    def build_limit_list_from_stk_limit(self, trade_date: str) -> pd.DataFrame:
        stk_limit = self.data_source.get_stk_limit(
            trade_date=trade_date,
            fields="trade_date,ts_code,up_limit,down_limit",
        )
        if stk_limit.empty:
            self.logger.warning("Tushare stk_limit 也未返回 %s 涨跌停价，无法生成备用涨停池", trade_date)
            return pd.DataFrame()

        daily = self.load_daily_for_limit_fallback(trade_date)
        if daily.empty:
            self.logger.warning("无法生成 %s 备用涨停池：日线文件和 Tushare daily 都不可用", trade_date)
            return pd.DataFrame()

        data = daily.merge(
            stk_limit,
            on=["trade_date", "ts_code"],
            how="inner",
            validate="one_to_one",
        )
        if data.empty:
            return pd.DataFrame()

        close = pd.to_numeric(data["close"], errors="coerce")
        up_limit = pd.to_numeric(data["up_limit"], errors="coerce")
        tolerance = float(self.config["collection"].get("stk_limit_close_tolerance", 0.001))
        limit_up = data[close >= up_limit * (1 - tolerance)].copy()
        if limit_up.empty:
            return pd.DataFrame()

        daily_basic = self.load_daily_basic_for_limit_fallback(trade_date)
        if not daily_basic.empty:
            basic_columns = [
                column
                for column in ["trade_date", "ts_code", "turnover_rate", "free_share"]
                if column in daily_basic.columns
            ]
            if {"trade_date", "ts_code"}.issubset(basic_columns):
                limit_up = limit_up.merge(
                    daily_basic[basic_columns],
                    on=["trade_date", "ts_code"],
                    how="left",
                    validate="one_to_one",
                )

        stock_names = self.load_stock_names()
        limit_up["name"] = limit_up["ts_code"].map(stock_names).fillna(limit_up["ts_code"])
        limit_up["pct_chg"] = pd.to_numeric(limit_up.get("pct_chg"), errors="coerce")
        limit_up["amp"] = self.calculate_amp(limit_up)
        limit_up["limit"] = "U"
        limit_up["lu_desc"] = "stk_limit_daily_close_fallback"
        limit_up["limit_times"] = pd.NA
        limit_up["limit_data_source"] = "stk_limit"
        limit_up["limit_data_quality"] = "basic_limit_only"
        limit_up["strategy_compatible"] = False

        columns = self.limit_list_output_columns() + [
            "limit_data_source",
            "limit_data_quality",
            "strategy_compatible",
        ]
        for column in columns:
            if column not in limit_up.columns:
                limit_up[column] = pd.NA
        return limit_up[columns].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def load_daily_for_limit_fallback(self, trade_date: str) -> pd.DataFrame:
        daily_path = self.daily_dir / f"{trade_date}.csv"
        daily = self._read_csv_safely(daily_path) if daily_path.exists() else pd.DataFrame()
        if not daily.empty:
            return daily
        fields = self.config["collection"].get("daily_fields")
        return self.data_source.get_daily(trade_date=trade_date, fields=fields)

    def load_daily_basic_for_limit_fallback(self, trade_date: str) -> pd.DataFrame:
        daily_basic_path = self.daily_basic_dir / f"{trade_date}.csv"
        daily_basic = self._read_csv_safely(daily_basic_path) if daily_basic_path.exists() else pd.DataFrame()
        if not daily_basic.empty:
            return daily_basic
        fields = self.config["collection"].get("daily_basic_fields")
        try:
            return self.data_source.get_daily_basic(trade_date=trade_date, fields=fields)
        except Exception as exc:
            self.logger.warning("备用涨停池读取 daily_basic 失败，换手率字段将为空: %s", exc)
            return pd.DataFrame()

    def load_stock_names(self) -> dict[str, str]:
        if self._stock_names_cache is not None:
            return self._stock_names_cache

        names: dict[str, str] = {}
        for status in ["L", "D", "P"]:
            try:
                basic = self.data_source.get_stock_basic(list_status=status, fields="ts_code,name")
            except Exception as exc:
                self.logger.warning("读取 stock_basic(%s) 失败，备用涨停池将用 ts_code 作为名称: %s", status, exc)
                continue
            if basic.empty or not {"ts_code", "name"}.issubset(basic.columns):
                continue
            names.update(dict(zip(basic["ts_code"].astype(str), basic["name"].astype(str))))
        self._stock_names_cache = names
        return names

    def limit_list_output_columns(self) -> list[str]:
        fields = self.config["collection"].get("limit_list_fields", "")
        return [column.strip() for column in str(fields).split(",") if column.strip()]

    @staticmethod
    def calculate_amp(data: pd.DataFrame) -> pd.Series:
        high = pd.to_numeric(data.get("high"), errors="coerce")
        low = pd.to_numeric(data.get("low"), errors="coerce")
        pre_close = pd.to_numeric(data.get("pre_close"), errors="coerce").replace(0, pd.NA)
        return ((high - low) / pre_close * 100).round(4)

    @staticmethod
    def _read_csv_safely(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})
        except (OSError, pd.errors.EmptyDataError):
            return pd.DataFrame()

    @staticmethod
    def _csv_has_data_row(path: Path) -> bool:
        try:
            with path.open(encoding="utf-8-sig") as f:
                f.readline()
                return bool(f.readline().strip())
        except OSError:
            return False

    def existing_dates(self, directory: Path) -> list[str]:
        return sorted(path.stem for path in directory.glob("*.csv"))

    @staticmethod
    def missing_dates(trade_dates: Iterable[str], existing_dates: Iterable[str]) -> list[str]:
        existing = set(existing_dates)
        return [trade_date for trade_date in trade_dates if trade_date not in existing]

    def _resolve_overwrite(self, overwrite: bool | None) -> bool:
        if overwrite is not None:
            return overwrite
        return bool(self.config.get("collection", {}).get("overwrite", False))

    def _should_skip(self, output_path: Path, overwrite: bool) -> bool:
        if overwrite:
            return False
        if not self._exists_with_retry(output_path):
            return False
        # 文件存在但无数据行（上次采集 Tushare 返回空）→ 允许重新采集
        try:
            with output_path.open(encoding="utf-8-sig") as f:
                f.readline()  # 跳过表头
                return bool(f.readline().strip())  # 有数据行才跳过
        except OSError:
            return True

    def _save_dataframe(self, data: pd.DataFrame, output_path: Path) -> None:
        self._mkdir_with_retry(output_path.parent)
        self._write_csv_with_retry(data, output_path)

    def _mkdir_with_retry(self, path: Path, retries: int = 3, delay: float = 2.0) -> None:
        mkdir_p(path)

    def _exists_with_retry(self, path: Path, retries: int = 3, delay: float = 1.0) -> bool:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return path.exists()
            except OSError as exc:
                last_error = exc
                self.logger.warning(
                    "共享盘文件检查失败，第 %d/%d 次重试: %s; error=%s",
                    attempt,
                    retries,
                    path,
                    exc,
                )
                time.sleep(delay * attempt)
        if last_error is not None:
            raise last_error
        return False

    def _write_csv_with_retry(self, data: pd.DataFrame, output_path: Path, retries: int = 3, delay: float = 2.0) -> None:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                data.to_csv(output_path, index=False, encoding="utf-8-sig")
                return
            except OSError as exc:
                last_error = exc
                self.logger.warning(
                    "共享盘CSV写入失败，第 %d/%d 次重试: %s; error=%s",
                    attempt,
                    retries,
                    output_path,
                    exc,
                )
                time.sleep(delay * attempt)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _is_permission_error(exc: Exception) -> bool:
        message = str(exc)
        return "没有接口" in message or "访问权限" in message
