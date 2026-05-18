from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import tushare as ts
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_fixed

from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger, setup_logger


@dataclass(frozen=True)
class TushareConfig:
    token_env: str
    retry_times: int
    retry_wait_seconds: int


class TushareDataSource:
    """Tushare Pro 数据源封装，不包含任何策略和交易逻辑。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        load_dotenv(self.project_root / ".env")
        self.config = load_json_config(config_path)
        logging_config = self.config.get("logging", {})
        setup_logger(
            log_dir=self.project_root / logging_config.get("log_dir", "logs"),
            log_file=logging_config.get("log_file", "a_share_quant.log"),
            level=logging_config.get("level", "INFO"),
        )
        self.logger = get_logger("data_source")

        source_config = self.config.get("data_source", {})
        self.tushare_config = TushareConfig(
            token_env=source_config.get("token_env", "TUSHARE_TOKEN"),
            retry_times=int(source_config.get("request_retry_times", 3)),
            retry_wait_seconds=int(source_config.get("request_retry_wait_seconds", 3)),
        )
        token = os.getenv(self.tushare_config.token_env)
        if not token:
            raise RuntimeError(
                f"未找到环境变量 {self.tushare_config.token_env}。请复制 .env.example 为 .env 后填写 Tushare Token。"
            )

        ts.set_token(token)
        self.pro = ts.pro_api(token)

    def _retry_decorator(self):
        return retry(
            stop=stop_after_attempt(self.tushare_config.retry_times),
            wait=wait_fixed(self.tushare_config.retry_wait_seconds),
            reraise=True,
        )

    def _call(self, api_name: str, **kwargs: Any) -> pd.DataFrame:
        @self._retry_decorator()
        def _request() -> pd.DataFrame:
            self.logger.debug("请求 Tushare 接口: %s, 参数: %s", api_name, kwargs)
            api = getattr(self.pro, api_name)
            result = api(**kwargs)
            if result is None:
                return pd.DataFrame()
            return result

        return _request()

    def get_trade_calendar(
        self,
        start_date: str,
        end_date: str,
        exchange: str = "SSE",
        is_open: Optional[str] = None,
    ) -> pd.DataFrame:
        params: dict[str, Any] = {
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
        }
        if is_open is not None:
            params["is_open"] = is_open
        return self._call("trade_cal", **params)

    def get_stock_basic(
        self,
        list_status: str = "L",
        fields: str = "ts_code,symbol,name,area,industry,market,list_date,delist_date,is_hs",
    ) -> pd.DataFrame:
        return self._call("stock_basic", exchange="", list_status=list_status, fields=fields)

    def get_daily(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call("daily", trade_date=trade_date, fields=fields)

    def get_daily_basic(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call("daily_basic", trade_date=trade_date, fields=fields)

    def get_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return self._call("adj_factor", trade_date=trade_date)

    def get_limit_list(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call("limit_list_d", trade_date=trade_date, fields=fields)
