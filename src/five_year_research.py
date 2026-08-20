from __future__ import annotations

"""五年严格 as-of 研究数据底座。

本模块与实盘加工目录完全隔离。它只从 ``data/raw`` 读取，并只写入调用方
指定的研究目录；任何一步失败都不会改动 ``data/processed`` 或实盘配置。
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.data_cleaner import DataCleaner
from src.fill_model import FillProbabilityEstimator
from src.market_emotion import MarketEmotionBuilder
from src.strategy_optimizer import StrategyConditionOptimizer
from src.strict_asof import PointInTimeContract, audit_point_in_time_frame
from src.theme_heat import ThemeHeatBuilder
from src.utils.config import get_project_root, load_json_config, mkdir_p


EXPECTED_FILL_METHOD = "asof_turnover_space_proxy_v2"


@dataclass(frozen=True)
class ResearchPaths:
    root: Path
    compact_daily: Path
    limit_up_merged: Path
    market_sentiment: Path
    market_emotion: Path
    theme_heat: Path
    strict_fill: Path
    feature_pool: Path
    manifest: Path
    strict_audit: Path

    @classmethod
    def under(cls, root: Path) -> "ResearchPaths":
        return cls(
            root=root,
            compact_daily=root / "daily_factor_history.csv",
            limit_up_merged=root / "limit_up_merged.csv",
            market_sentiment=root / "market_sentiment.csv",
            market_emotion=root / "market_emotion_features.csv",
            theme_heat=root / "theme_heat_features.csv",
            strict_fill=root / "limit_up_fill_scored_asof.csv",
            feature_pool=root / "strict_feature_pool.csv",
            manifest=root / "dataset_manifest.json",
            strict_audit=root / "strict_asof_audit.json",
        )


def normalize_date_series(values: pd.Series) -> pd.Series:
    return values.astype(str).str.replace(r"\.0$", "", regex=True)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class FiveYearResearchDatasetBuilder:
    """流式构建2019年至今的独立研究底座。"""

    COMPACT_DAILY_COLUMNS = [
        "trade_date",
        "ts_code",
        "pct_chg",
        "amount",
        "turnover_rate",
    ]

    def __init__(
        self,
        *,
        config_path: str | Path = "config/config.json",
        research_root: str | Path = "data/research/five_year_strict",
    ) -> None:
        self.project_root = get_project_root()
        self.config_path = Path(config_path)
        self.config = load_json_config(config_path)
        root = Path(research_root)
        if not root.is_absolute():
            root = self.project_root / root
        self.paths = ResearchPaths.under(root)
        self.cleaner = DataCleaner(config_path)
        self.emotion_builder = MarketEmotionBuilder(config_path)

        # 研究路径必须与任何实盘/通用加工路径分离；这里既校验又覆盖实例属性，
        # 避免 DataCleaner 的默认全量输出误伤 data/processed。
        self._assert_isolated_paths()
        self.cleaner.processed_dir = self.paths.root
        self.cleaner.daily_merged_path = self.paths.compact_daily
        self.cleaner.limit_up_merged_path = self.paths.limit_up_merged
        self.cleaner.market_sentiment_path = self.paths.market_sentiment

    def _assert_isolated_paths(self) -> None:
        processed = (self.project_root / "data" / "processed").resolve()
        root = self.paths.root.resolve()
        if root == processed or processed in root.parents or root in processed.parents:
            raise ValueError("五年研究目录必须与 data/processed 完全隔离")
        if root == self.project_root.resolve():
            raise ValueError("禁止把项目根目录作为五年研究输出目录")

    def discover_complete_dates(
        self,
        start_date: str,
        end_date: str | None,
    ) -> list[str]:
        daily_dates = {path.stem for path in self.cleaner.daily_dir.glob("*.csv")}
        basic_dates = {path.stem for path in self.cleaner.daily_basic_dir.glob("*.csv")}
        dates = sorted(daily_dates & basic_dates)
        selected = [
            date
            for date in dates
            if date >= str(start_date) and (end_date is None or date <= str(end_date))
        ]
        if not selected:
            raise RuntimeError("没有同时具备 daily 与 daily_basic 的研究日期")
        return selected

    @staticmethod
    def _unlink_outputs(paths: ResearchPaths) -> None:
        mkdir_p(paths.root)
        for value in asdict(paths).values():
            path = Path(value)
            if path == paths.root:
                continue
            if path.exists():
                path.unlink()

    @staticmethod
    def _append(frame: pd.DataFrame, path: Path) -> None:
        if frame.empty:
            return
        DataCleaner._append_csv(frame, path)

    def build_base_tables(
        self,
        *,
        start_date: str,
        end_date: str | None,
        overwrite: bool,
    ) -> dict[str, Any]:
        dates = self.discover_complete_dates(start_date, end_date)
        if overwrite:
            self._unlink_outputs(self.paths)
        elif any(
            path.exists()
            for path in (
                self.paths.compact_daily,
                self.paths.limit_up_merged,
                self.paths.market_sentiment,
            )
        ):
            raise FileExistsError("研究底座已存在；重建时必须显式传入 overwrite=True")

        market_rows: list[dict[str, Any]] = []
        emotion_rows: list[dict[str, Any]] = []
        daily_rows = 0
        limit_rows = 0
        skipped_daily_dates: list[str] = []
        segments = list(self.emotion_builder.SEGMENTS)

        for index, trade_date in enumerate(dates, start=1):
            daily = self.cleaner.clean_daily_by_date(trade_date)
            if daily.empty:
                skipped_daily_dates.append(trade_date)
                continue
            compact = daily.reindex(columns=self.COMPACT_DAILY_COLUMNS)
            self._append(compact, self.paths.compact_daily)
            daily_rows += len(compact)

            limit_up = self.cleaner.clean_limit_up_by_date(trade_date, daily)
            if not limit_up.empty:
                self._append(limit_up, self.paths.limit_up_merged)
                limit_rows += len(limit_up)

            # limit_list_d 在2019-11-28之前没有历史文件。情绪构造器允许空池，
            # 但分市场方法仍需要一个带 schema 的空表才能执行布尔筛选。
            limit_for_features = limit_up
            if limit_for_features.empty and "market_segment" not in limit_for_features.columns:
                limit_for_features = pd.DataFrame(
                    columns=[
                        "trade_date",
                        "ts_code",
                        "market_segment",
                        "limit_times",
                        "open_times",
                        "limit_data_quality",
                        "strategy_compatible",
                    ]
                )

            market_rows.append(
                self.cleaner.build_market_sentiment_row(trade_date, daily, limit_up)
            )
            global_features = self.emotion_builder.build_global_features(
                daily, limit_for_features
            )
            for segment in segments:
                emotion_rows.append(
                    {
                        "trade_date": trade_date,
                        "market_segment": segment,
                        **global_features,
                        **self.emotion_builder.build_segment_features(
                            daily, limit_for_features, segment
                        ),
                    }
                )

            if index == 1 or index % 100 == 0 or index == len(dates):
                print(
                    f"FIVE_YEAR_DATA_PROGRESS {index}/{len(dates)} "
                    f"date={trade_date} daily_rows={daily_rows} limit_rows={limit_rows}",
                    flush=True,
                )

        if not market_rows or not self.paths.compact_daily.exists():
            raise RuntimeError("五年研究日线底座构建失败")
        pd.DataFrame(market_rows).to_csv(
            self.paths.market_sentiment, index=False, encoding="utf-8-sig"
        )
        emotion = self.emotion_builder.add_state_features(pd.DataFrame(emotion_rows))
        emotion.to_csv(self.paths.market_emotion, index=False, encoding="utf-8-sig")
        return {
            "available_date_count": len(dates),
            "first_date": dates[0],
            "last_date": dates[-1],
            "daily_rows": daily_rows,
            "limit_rows": limit_rows,
            "skipped_daily_dates": skipped_daily_dates,
        }

    def build_strict_features(self, planned_buy_amount: float) -> dict[str, Any]:
        if not self.paths.limit_up_merged.exists():
            raise FileNotFoundError(self.paths.limit_up_merged)
        estimator = FillProbabilityEstimator(self.config_path)
        estimator.score_limit_up_table_asof(
            self.paths.limit_up_merged,
            self.paths.strict_fill,
            planned_buy_amount=float(planned_buy_amount),
            market_sentiment_path=self.paths.market_sentiment,
        )

        theme_builder = ThemeHeatBuilder(self.config_path)
        theme_builder.build(
            input_path=self.paths.limit_up_merged,
            output_path=self.paths.theme_heat,
        )

        source = pd.read_csv(
            self.paths.strict_fill,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        strict_audit = audit_point_in_time_frame(
            source,
            PointInTimeContract(
                dataset_name="five_year_research_fill_source",
                expected_method=EXPECTED_FILL_METHOD,
            ),
        ).to_dict()
        self.paths.strict_audit.write_text(
            json.dumps(strict_audit, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        optimizer = StrategyConditionOptimizer(
            self.config_path,
            optimization_config_key="five_year_walk_forward_research",
        )
        optimizer.input_trades_path = self.paths.strict_fill
        optimizer.input_daily_merged_path = self.paths.compact_daily
        optimizer.optional_market_emotion_features_path = self.paths.market_emotion
        optimizer.optional_theme_heat_features_path = self.paths.theme_heat
        missing_root = self.paths.root / "optional_features_not_used"
        optimizer.optional_auction_features_path = missing_root / "auction.csv"
        optimizer.optional_open_5m_features_path = missing_root / "open_5m.csv"
        optimizer.optional_sector_moneyflow_features_path = missing_root / "moneyflow.csv"
        optimizer.optional_top_list_features_path = missing_root / "top_list.csv"
        enriched = optimizer.load_trades(require_complete_exit=False)
        enriched["trade_date"] = normalize_date_series(enriched["trade_date"])
        enriched = enriched.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        enriched.to_csv(self.paths.feature_pool, index=False, encoding="utf-8-sig")
        return {
            "strict_fill_rows": int(len(source)),
            "strict_reliable_rows": int(
                source["is_fill_score_reliable"].fillna(False).astype(bool).sum()
            ),
            "strict_allow_rows": int(
                source["allow_buy_reliable"].fillna(False).astype(bool).sum()
            ),
            "feature_pool_rows": int(len(enriched)),
            "strict_asof_audit": strict_audit,
        }

    def _validate_feature_pool(self) -> dict[str, Any]:
        pool = pd.read_csv(
            self.paths.feature_pool,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        pool["trade_date"] = normalize_date_series(pool["trade_date"])
        duplicate_count = int(pool.duplicated(["trade_date", "ts_code"]).sum())
        if duplicate_count:
            raise RuntimeError(f"五年特征池存在重复键：{duplicate_count}")
        if not (
            normalize_date_series(pool["model_training_end_date"])
            < pool["trade_date"]
        ).all():
            raise RuntimeError("五年特征池出现 model_training_end_date >= trade_date")
        required = {
            "trade_date",
            "ts_code",
            "allow_buy_reliable",
            "is_fill_score_reliable",
            "segment_retreat_state_bucket",
            "market_chain_count_bucket",
            "segment_limit_max_height_bucket",
            "volume_ratio_bucket",
        }
        missing = sorted(required.difference(pool.columns))
        if missing:
            raise RuntimeError(f"五年特征池缺少策略字段：{missing}")
        return {
            "first_signal_date": str(pool["trade_date"].min()),
            "last_signal_date": str(pool["trade_date"].max()),
            "duplicate_key_count": duplicate_count,
            "column_count": int(len(pool.columns)),
        }

    def build(
        self,
        *,
        start_date: str = "20190101",
        end_date: str | None = None,
        planned_buy_amount: float | None = None,
        overwrite: bool = False,
    ) -> Path:
        amount = float(
            planned_buy_amount
            if planned_buy_amount is not None
            else self.config.get("fill_model", {}).get(
                "default_planned_buy_amount", 412_500
            )
        )
        base = self.build_base_tables(
            start_date=start_date,
            end_date=end_date,
            overwrite=overwrite,
        )
        strict = self.build_strict_features(amount)
        validation = self._validate_feature_pool()
        files: dict[str, Any] = {}
        for name, path in asdict(self.paths).items():
            candidate = Path(path)
            if name == "root" or not candidate.exists():
                continue
            files[name] = {
                "path": str(candidate.relative_to(self.project_root)),
                "size_bytes": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
        manifest = {
            "schema_version": 1,
            "purpose": "isolated_five_year_nested_walk_forward_research",
            "live_paths_modified": False,
            "start_date_requested": str(start_date),
            "end_date_requested": str(end_date or "latest_complete_raw_date"),
            "planned_buy_amount": amount,
            "base_tables": base,
            "strict_features": strict,
            "validation": validation,
            "files": files,
        }
        self.paths.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.paths.manifest
