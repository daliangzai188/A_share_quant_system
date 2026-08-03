from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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
    request_timeout_seconds: int


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
            request_timeout_seconds=int(source_config.get("request_timeout_seconds", 60)),
        )
        token = os.getenv(self.tushare_config.token_env) or source_config.get("token")
        if not token:
            raise RuntimeError(
                f"未找到环境变量 {self.tushare_config.token_env}。"
                "请运行采集脚本并按提示输入 Token，或在 config/config.json 的 data_source.token 中配置。"
            )

        self.token = token.strip()
        ts.set_token(self.token)
        # 优先把超时下沉到 http 层（新版 tushare 的 pro_api 支持 timeout 参数）；
        # 老版本不支持时回退到无参构造，仍由 _run_with_timeout 兜底应用层超时。
        request_timeout = self.tushare_config.request_timeout_seconds
        if request_timeout > 0:
            try:
                self.pro = ts.pro_api(self.token, timeout=request_timeout)
            except TypeError:
                self.pro = ts.pro_api(self.token)
        else:
            self.pro = ts.pro_api(self.token)

    def _retry_decorator(self):
        return retry(
            stop=stop_after_attempt(self.tushare_config.retry_times),
            wait=wait_fixed(self.tushare_config.retry_wait_seconds),
            reraise=True,
        )

    def _run_with_timeout(self, fn: Callable[[], pd.DataFrame], api_name: str) -> pd.DataFrame:
        """给单个 Tushare 请求套一层应用层墙钟超时。

        即便底层 http 超时被忽略（连接半开、DNS 卡住等），也能在 request_timeout_seconds
        后抛出 TimeoutError 交给上层 tenacity 重试，避免单个接口卡死拖满收盘流水线的 600s 预算。
        超时后底层线程无法强制杀掉，标记为 daemon 丢弃即可（进程退出不受其阻塞）。
        """
        timeout = self.tushare_config.request_timeout_seconds
        if timeout <= 0:
            return fn()

        result_box: dict[str, pd.DataFrame] = {}
        error_box: dict[str, BaseException] = {}

        def _worker() -> None:
            try:
                result_box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - 原样转交主线程重抛
                error_box["error"] = exc

        worker = threading.Thread(target=_worker, name=f"tushare-{api_name}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError(
                f"Tushare 接口 {api_name} 超过 {timeout}s 未返回，已中止本次请求（后台线程丢弃，交由重试处理）"
            )
        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("value", pd.DataFrame())

    def _call(self, api_name: str, **kwargs: Any) -> pd.DataFrame:
        @self._retry_decorator()
        def _request() -> pd.DataFrame:
            self.logger.debug("请求 Tushare 接口: %s, 参数: %s", api_name, kwargs)

            def _do() -> pd.DataFrame:
                api = getattr(self.pro, api_name)
                result = api(**kwargs)
                return pd.DataFrame() if result is None else result

            return self._run_with_timeout(_do, api_name)

        return _request()

    def _query(self, api_name: str, **kwargs: Any) -> pd.DataFrame:
        @self._retry_decorator()
        def _request() -> pd.DataFrame:
            self.logger.debug("请求 Tushare query 接口: %s, 参数: %s", api_name, kwargs)

            def _do() -> pd.DataFrame:
                result = self.pro.query(api_name, **kwargs)
                return pd.DataFrame() if result is None else result

            return self._run_with_timeout(_do, api_name)

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
        limit_type: str = "U",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call("limit_list_d", trade_date=trade_date, limit_type=limit_type, fields=fields)

    def get_limit_list_range(
        self,
        start_date: str,
        end_date: str,
        limit_type: str = "U",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call(
            "limit_list_d",
            start_date=start_date,
            end_date=end_date,
            limit_type=limit_type,
            fields=fields,
        )

    def query_limit_list(
        self,
        trade_date: str,
        limit_type: str = "U",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._query("limit_list_d", trade_date=trade_date, limit_type=limit_type, fields=fields)

    def query_limit_list_range(
        self,
        start_date: str,
        end_date: str,
        limit_type: str = "U",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._query(
            "limit_list_d",
            start_date=start_date,
            end_date=end_date,
            limit_type=limit_type,
            fields=fields,
        )

    def get_stk_limit(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._call("stk_limit", trade_date=trade_date, fields=fields)

    def get_moneyflow(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        """拉取个股资金流。该接口可能受 Tushare 权限和当日发布时间影响。"""

        return self._call("moneyflow", trade_date=trade_date, fields=fields)

    def get_top_list(
        self,
        trade_date: str,
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        """拉取龙虎榜。该接口通常在收盘后较晚才完整。"""

        return self._call("top_list", trade_date=trade_date, fields=fields)

    def get_minute_bars(
        self,
        ts_code: str,
        start_datetime: str,
        end_datetime: str,
        freq: str = "1min",
        asset: str = "E",
        adj: Optional[str] = None,
    ) -> pd.DataFrame:
        """拉取分钟 K 线。该接口可能需要额外 Tushare 权限。"""

        self.logger.debug(
            "请求 Tushare pro_bar 分钟 K: %s, %s-%s, freq=%s",
            ts_code,
            start_datetime,
            end_datetime,
            freq,
        )
        result = ts.pro_bar(
            ts_code=ts_code,
            start_date=start_datetime,
            end_date=end_datetime,
            freq=freq,
            asset=asset,
            adj=adj,
        )
        if result is None:
            return pd.DataFrame()
        return result

    def get_stock_minute_bars(
        self,
        ts_code: str,
        start_datetime: str,
        end_datetime: str,
        freq: str = "1min",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        """调用stk_mins获取A股历史分钟行情。

        与``pro_bar``入口分开保留，便于研究脚本严格使用官方``stk_mins``字段
        ``vol=股、amount=元``。该接口需要单独权限且限频较低，调用方必须自行
        做间隔和断点续传，不能放入实盘交易线程。
        """

        # 分钟接口的低频权限会把“频率超限”也计入请求次数。通用``_call``的
        # 三次快速重试反而可能不断延长限频窗口，因此该专用方法只请求一次，
        # 由上层采集器按60秒以上间隔重试和断点续传。
        def _do() -> pd.DataFrame:
            result = self.pro.stk_mins(
                ts_code=ts_code,
                freq=freq,
                start_date=start_datetime,
                end_date=end_datetime,
                fields=fields,
            )
            return pd.DataFrame() if result is None else result

        return self._run_with_timeout(_do, "stk_mins")
