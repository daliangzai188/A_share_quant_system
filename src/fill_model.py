from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class FillRateTableBuilder:
    """构建涨停板成交概率模型所需的历史换手率查询表。"""

    GROUP_COLUMNS = [
        "market_segment",
        "limit_times_bucket",
        "board_type",
        "first_time_bucket",
        "segment_market_sentiment_level",
    ]
    FALLBACK_COLUMNS = ["market_segment", "limit_times_bucket", "board_type", "first_time_bucket"]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("fill_model")

        fill_config = self.config.get("fill_model", {})
        self.input_limit_up_path = self.project_root / fill_config.get(
            "input_limit_up_path", "data/processed/limit_up_merged.csv"
        )
        self.input_market_sentiment_path = self.project_root / fill_config.get(
            "input_market_sentiment_path", "data/processed/market_sentiment.csv"
        )
        self.output_fill_rate_table_path = self.project_root / fill_config.get(
            "output_fill_rate_table_path", "data/processed/fill_rate_table.csv"
        )
        self.output_fill_rate_fallback_path = self.project_root / fill_config.get(
            "output_fill_rate_fallback_path", "data/processed/fill_rate_fallback.csv"
        )
        self.min_group_samples = int(fill_config.get("min_group_samples", 30))
        self.default_fill_quantile = float(fill_config.get("default_fill_quantile", 0.25))

    def build(self) -> dict[str, Path]:
        data = self.load_model_data()
        if data.empty:
            raise RuntimeError("成交概率模型输入为空，请先运行 clean_collected_data.py。")

        fill_rate_table = self.build_group_table(data=data, group_columns=self.GROUP_COLUMNS)
        fallback_table = self.build_group_table(data=data, group_columns=self.FALLBACK_COLUMNS)

        fill_rate_table["is_sample_enough"] = fill_rate_table["sample_count"] >= self.min_group_samples
        fill_rate_table["suggested_turnover_rate"] = fill_rate_table["turnover_rate_q25"]
        fill_rate_table["suggested_quantile"] = self.default_fill_quantile

        fallback_table["is_sample_enough"] = fallback_table["sample_count"] >= self.min_group_samples
        fallback_table["suggested_turnover_rate"] = fallback_table["turnover_rate_q25"]
        fallback_table["suggested_quantile"] = self.default_fill_quantile

        self.output_fill_rate_table_path.parent.mkdir(parents=True, exist_ok=True)
        fill_rate_table.to_csv(self.output_fill_rate_table_path, index=False, encoding="utf-8-sig")
        fallback_table.to_csv(self.output_fill_rate_fallback_path, index=False, encoding="utf-8-sig")

        self.logger.info("换手率查询表已生成: %s, 行数: %s", self.output_fill_rate_table_path, len(fill_rate_table))
        self.logger.info("换手率回退表已生成: %s, 行数: %s", self.output_fill_rate_fallback_path, len(fallback_table))
        return {
            "fill_rate_table": self.output_fill_rate_table_path,
            "fill_rate_fallback": self.output_fill_rate_fallback_path,
        }

    def load_model_data(self) -> pd.DataFrame:
        if not self.input_limit_up_path.exists():
            raise FileNotFoundError(f"涨停合并表不存在: {self.input_limit_up_path}")
        if not self.input_market_sentiment_path.exists():
            raise FileNotFoundError(f"市场情绪表不存在: {self.input_market_sentiment_path}")

        limit_up = pd.read_csv(
            self.input_limit_up_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        market = pd.read_csv(self.input_market_sentiment_path, dtype={"trade_date": str})
        market_columns = [column for column in market.columns if column != "trade_date"]
        data = limit_up.merge(
            market[["trade_date", *market_columns]],
            on="trade_date",
            how="left",
            validate="many_to_one",
        )
        data = self.add_segment_market_fields(data)
        data = data[data["turnover_rate"].notna()].copy()
        data = data[data["turnover_rate"] >= 0].copy()
        data["limit_times_bucket"] = data["limit_times"].apply(self.classify_limit_times_bucket)
        return data

    def build_group_table(self, data: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
        rows = []
        for keys, group in data.groupby(group_columns, dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            turnover = group["turnover_rate"].dropna()
            fd_ratio = group["fd_amount_to_circ_mv"].dropna()
            row = {column: value for column, value in zip(group_columns, keys)}
            row.update(
                {
                    "sample_count": int(len(group)),
                    "turnover_rate_mean": float(turnover.mean()),
                    "turnover_rate_median": float(turnover.median()),
                    "turnover_rate_q25": float(turnover.quantile(0.25)),
                    "turnover_rate_q30": float(turnover.quantile(0.30)),
                    "turnover_rate_q75": float(turnover.quantile(0.75)),
                    "turnover_rate_min": float(turnover.min()),
                    "turnover_rate_max": float(turnover.max()),
                    "open_times_mean": float(group["open_times"].fillna(0).mean()),
                    "fd_amount_mean": float(group["fd_amount"].fillna(0).mean()),
                    "fd_amount_median": float(group["fd_amount"].fillna(0).median()),
                    "fd_amount_to_circ_mv_median": float(fd_ratio.median()) if not fd_ratio.empty else 0.0,
                    "limit_up_count_mean": float(group["limit_up_count"].fillna(0).mean()),
                    "segment_limit_up_count_mean": float(group["segment_limit_up_count"].fillna(0).mean()),
                    "segment_limit_up_ratio_mean": float(group["segment_limit_up_ratio"].fillna(0).mean()),
                }
            )
            rows.append(row)

        result = pd.DataFrame(rows)
        return result.sort_values(group_columns).reset_index(drop=True)

    @staticmethod
    def add_segment_market_fields(data: pd.DataFrame) -> pd.DataFrame:
        data = data.copy()
        if "market_segment" not in data.columns:
            data["market_segment"] = "unknown"
        if "limit_up_count" not in data.columns:
            data["limit_up_count"] = 0
        if "market_sentiment_level" not in data.columns:
            data["market_sentiment_level"] = "unknown"

        def get_segment_value(row: pd.Series, suffix: str, default_column: str, default_value: object) -> object:
            segment = str(row.get("market_segment", "unknown"))
            column = f"{segment}_{suffix}"
            if column in row.index and not pd.isna(row.get(column)):
                return row.get(column)
            if default_column in row.index and not pd.isna(row.get(default_column)):
                return row.get(default_column)
            return default_value

        data["segment_limit_up_count"] = data.apply(
            lambda row: get_segment_value(row, "limit_up_count", "limit_up_count", 0),
            axis=1,
        )
        data["segment_stock_count"] = data.apply(
            lambda row: get_segment_value(row, "stock_count", "stock_count", 0),
            axis=1,
        )
        data["segment_limit_up_ratio"] = data.apply(
            lambda row: get_segment_value(row, "limit_up_ratio", "limit_up_ratio", 0.0),
            axis=1,
        )
        data["segment_market_sentiment_level"] = data.apply(
            lambda row: get_segment_value(row, "market_sentiment_level", "market_sentiment_level", "unknown"),
            axis=1,
        )
        return data

    @staticmethod
    def classify_limit_times_bucket(value: object) -> str:
        if pd.isna(value):
            return "unknown"
        try:
            limit_times = int(value)
        except (TypeError, ValueError):
            return "unknown"
        if limit_times <= 1:
            return "1"
        if limit_times == 2:
            return "2"
        if limit_times == 3:
            return "3"
        if limit_times == 4:
            return "4"
        return "5_plus"


class FillProbabilityEstimator:
    """基于历史换手率查询表估算涨停板排队成交概率。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("fill_probability")

        fill_config = self.config.get("fill_model", {})
        risk_config = self.config.get("risk", {})
        self.fill_rate_table_path = self.project_root / fill_config.get(
            "output_fill_rate_table_path", "data/processed/fill_rate_table.csv"
        )
        self.fill_rate_fallback_path = self.project_root / fill_config.get(
            "output_fill_rate_fallback_path", "data/processed/fill_rate_fallback.csv"
        )
        self.min_group_samples = int(fill_config.get("min_group_samples", 30))
        self.min_fill_probability = float(risk_config.get("min_fill_probability", 0.6))
        self.fd_amount_abnormal_ratio_threshold = float(fill_config.get("fd_amount_abnormal_ratio_threshold", 1.0))
        self.ignore_abnormal_fd_amount = bool(fill_config.get("ignore_abnormal_fd_amount", True))

        self.fill_rate_table = self._read_table(self.fill_rate_table_path)
        self.fill_rate_fallback = self._read_table(self.fill_rate_fallback_path)

    def score_limit_up_table(
        self,
        input_path: str | Path,
        output_path: str | Path,
        planned_buy_amount: float,
    ) -> Path:
        input_path = self.project_root / input_path if not Path(input_path).is_absolute() else Path(input_path)
        output_path = self.project_root / output_path if not Path(output_path).is_absolute() else Path(output_path)
        limit_up = pd.read_csv(input_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        if limit_up.empty:
            raise RuntimeError(f"涨停合并表为空: {input_path}")
        required_market_columns = {"market_sentiment_level", "limit_up_count", "segment_market_sentiment_level"}
        if not required_market_columns.issubset(set(limit_up.columns)):
            market = pd.read_csv(
                self.project_root / self.config.get("fill_model", {}).get(
                    "input_market_sentiment_path", "data/processed/market_sentiment.csv"
                ),
                dtype={"trade_date": str},
            )
            market_columns = [column for column in market.columns if column != "trade_date" and column not in limit_up.columns]
            limit_up = limit_up.merge(
                market[["trade_date", *market_columns]],
                on="trade_date",
                how="left",
                validate="many_to_one",
            )
        limit_up = FillRateTableBuilder.add_segment_market_fields(limit_up)

        scored = []
        for _, row in limit_up.iterrows():
            circ_mv = row.get("circ_mv")
            fd_amount = self.resolve_queue_amount(row)
            if pd.isna(circ_mv) or pd.isna(row.get("fd_amount")):
                scored.append(self._empty_score(row=row, planned_buy_amount=planned_buy_amount, reason="missing_amount"))
                continue
            try:
                result = self.estimate(
                    ts_code=str(row["ts_code"]),
                    trade_date=str(row["trade_date"]),
                    limit_times=int(row["limit_times"]),
                    board_type=str(row["board_type"]),
                    first_time_bucket=str(row["first_time_bucket"]),
                    market_sentiment_level=str(row["market_sentiment_level"]),
                    market_segment=str(row.get("market_segment", "unknown")),
                    segment_market_sentiment_level=str(row.get("segment_market_sentiment_level", "unknown")),
                    circ_mv=float(circ_mv),
                    current_queue_amount=float(fd_amount),
                    planned_buy_amount=planned_buy_amount,
                )
            except (ValueError, TypeError) as exc:
                scored.append(self._empty_score(row=row, planned_buy_amount=planned_buy_amount, reason=str(exc)))
                continue
            scored.append(result)

        scored_frame = pd.DataFrame(scored)
        output = limit_up.merge(
            scored_frame,
            on=["trade_date", "ts_code"],
            how="left",
            suffixes=("", "_fill_model"),
            validate="one_to_one",
        )
        output["is_fill_score_reliable"] = self.build_reliability_flag(output)
        output["allow_buy_reliable"] = output["allow_buy"].fillna(False) & output["is_fill_score_reliable"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output.to_csv(output_path, index=False, encoding="utf-8-sig")
        self.logger.info("涨停池成交概率打标表已生成: %s, 行数: %s", output_path, len(output))
        return output_path

    def estimate(
        self,
        ts_code: str,
        trade_date: str,
        limit_times: int,
        board_type: str,
        first_time_bucket: str,
        market_sentiment_level: str,
        market_segment: str,
        segment_market_sentiment_level: str,
        circ_mv: float,
        current_queue_amount: float,
        planned_buy_amount: float,
    ) -> dict[str, Any]:
        if planned_buy_amount <= 0:
            raise ValueError("planned_buy_amount 必须大于 0。")
        if circ_mv <= 0:
            raise ValueError("circ_mv 必须大于 0。")

        limit_times_bucket = FillRateTableBuilder.classify_limit_times_bucket(limit_times)
        matched_row, source = self.match_turnover_row(
            limit_times_bucket=limit_times_bucket,
            board_type=board_type,
            first_time_bucket=first_time_bucket,
            market_sentiment_level=market_sentiment_level,
            market_segment=market_segment,
            segment_market_sentiment_level=segment_market_sentiment_level,
        )

        if matched_row is None:
            suggested_turnover_rate = 0.0
            sample_count = 0
            is_sample_enough = False
            source = "none"
        else:
            suggested_turnover_rate = float(matched_row["suggested_turnover_rate"])
            sample_count = int(matched_row["sample_count"])
            is_sample_enough = bool(matched_row["is_sample_enough"])

        estimated_turnover_amount = self.estimate_turnover_amount(
            circ_mv=circ_mv,
            turnover_rate=suggested_turnover_rate,
        )
        available_fill_amount = max(estimated_turnover_amount - current_queue_amount, 0.0)
        fill_probability = min(available_fill_amount / planned_buy_amount, 1.0)
        position_scale = self.suggest_position_scale(fill_probability)

        return {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "limit_times": limit_times,
            "limit_times_bucket": limit_times_bucket,
            "board_type": board_type,
            "first_time_bucket": first_time_bucket,
            "market_sentiment_level": market_sentiment_level,
            "market_segment": market_segment,
            "segment_market_sentiment_level": segment_market_sentiment_level,
            "matched_source": source,
            "sample_count": sample_count,
            "is_sample_enough": is_sample_enough,
            "suggested_turnover_rate": suggested_turnover_rate,
            "circ_mv": circ_mv,
            "current_queue_amount": current_queue_amount,
            "planned_buy_amount": planned_buy_amount,
            "estimated_turnover_amount": estimated_turnover_amount,
            "available_fill_amount": available_fill_amount,
            "fill_probability": fill_probability,
            "position_scale": position_scale,
            "allow_buy": fill_probability >= self.min_fill_probability,
            "min_fill_probability": self.min_fill_probability,
        }

    def match_turnover_row(
        self,
        limit_times_bucket: str,
        board_type: str,
        first_time_bucket: str,
        market_sentiment_level: str,
        market_segment: str,
        segment_market_sentiment_level: str,
    ) -> tuple[pd.Series | None, str]:
        exact = pd.DataFrame()
        if {"market_segment", "segment_market_sentiment_level"}.issubset(self.fill_rate_table.columns):
            exact = self.fill_rate_table[
                (self.fill_rate_table["market_segment"] == market_segment)
                & (self.fill_rate_table["limit_times_bucket"].astype(str) == str(limit_times_bucket))
                & (self.fill_rate_table["board_type"] == board_type)
                & (self.fill_rate_table["first_time_bucket"] == first_time_bucket)
                & (self.fill_rate_table["segment_market_sentiment_level"] == segment_market_sentiment_level)
            ]
        elif "market_sentiment_level" in self.fill_rate_table.columns:
            exact = self.fill_rate_table[
                (self.fill_rate_table["limit_times_bucket"].astype(str) == str(limit_times_bucket))
                & (self.fill_rate_table["board_type"] == board_type)
                & (self.fill_rate_table["first_time_bucket"] == first_time_bucket)
                & (self.fill_rate_table["market_sentiment_level"] == market_sentiment_level)
            ]
        if not exact.empty:
            row = exact.iloc[0]
            if bool(row["is_sample_enough"]):
                return row, "exact"

        fallback = pd.DataFrame()
        if "market_segment" in self.fill_rate_fallback.columns:
            fallback = self.fill_rate_fallback[
                (self.fill_rate_fallback["market_segment"] == market_segment)
                & (self.fill_rate_fallback["limit_times_bucket"].astype(str) == str(limit_times_bucket))
                & (self.fill_rate_fallback["board_type"] == board_type)
                & (self.fill_rate_fallback["first_time_bucket"] == first_time_bucket)
            ]
        else:
            fallback = self.fill_rate_fallback[
                (self.fill_rate_fallback["limit_times_bucket"].astype(str) == str(limit_times_bucket))
                & (self.fill_rate_fallback["board_type"] == board_type)
                & (self.fill_rate_fallback["first_time_bucket"] == first_time_bucket)
            ]
        if not fallback.empty:
            row = fallback.iloc[0]
            source = "fallback" if exact.empty else "fallback_due_to_low_sample"
            return row, source

        if not exact.empty:
            return exact.iloc[0], "exact_low_sample"
        return None, "none"

    @staticmethod
    def estimate_turnover_amount(circ_mv: float, turnover_rate: float) -> float:
        """根据流通市值和换手率估算可成交总金额。

        Tushare circ_mv 单位为万元，turnover_rate 单位为百分比。
        输出单位为元。
        """
        return circ_mv * 10000 * turnover_rate / 100

    @staticmethod
    def suggest_position_scale(fill_probability: float) -> float:
        if fill_probability >= 0.8:
            return 1.0
        if fill_probability >= 0.5:
            return 0.5
        return 0.0

    @staticmethod
    def _read_table(path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"换手率表不存在，请先运行 scripts/build_fill_rate_table.py: {path}")
        return pd.read_csv(path, dtype={"limit_times_bucket": str})

    def resolve_queue_amount(self, row: pd.Series) -> float:
        fd_amount = row.get("fd_amount")
        fd_ratio = row.get("fd_amount_to_circ_mv")
        if pd.isna(fd_amount):
            return float("nan")
        is_abnormal = False
        if not pd.isna(fd_ratio):
            is_abnormal = float(fd_ratio) > self.fd_amount_abnormal_ratio_threshold
        if self.ignore_abnormal_fd_amount and is_abnormal:
            return 0.0
        return float(fd_amount)

    @staticmethod
    def build_reliability_flag(data: pd.DataFrame) -> pd.Series:
        no_score_error = data.get("score_error", pd.Series(index=data.index, dtype=object)).isna()
        has_match = data.get("matched_source", pd.Series(index=data.index, dtype=object)).fillna("none") != "none"
        fd_amount_ok = ~data.get("is_fd_amount_abnormal", pd.Series(False, index=data.index)).fillna(False)
        return no_score_error & has_match & fd_amount_ok

    def _empty_score(self, row: pd.Series, planned_buy_amount: float, reason: str) -> dict[str, Any]:
        return {
            "ts_code": str(row.get("ts_code", "")),
            "trade_date": str(row.get("trade_date", "")),
            "limit_times": row.get("limit_times"),
            "limit_times_bucket": FillRateTableBuilder.classify_limit_times_bucket(row.get("limit_times")),
            "board_type": row.get("board_type"),
            "first_time_bucket": row.get("first_time_bucket"),
            "market_sentiment_level": row.get("market_sentiment_level"),
            "market_segment": row.get("market_segment", "unknown"),
            "segment_market_sentiment_level": row.get("segment_market_sentiment_level", "unknown"),
            "matched_source": "none",
            "sample_count": 0,
            "is_sample_enough": False,
            "suggested_turnover_rate": 0.0,
            "circ_mv": row.get("circ_mv"),
            "current_queue_amount": row.get("fd_amount"),
            "planned_buy_amount": planned_buy_amount,
            "estimated_turnover_amount": 0.0,
            "available_fill_amount": 0.0,
            "fill_probability": 0.0,
            "position_scale": 0.0,
            "allow_buy": False,
            "min_fill_probability": self.min_fill_probability,
            "score_error": reason,
        }
