from __future__ import annotations

"""四腿嵌套 walk-forward 研究引擎。

选择规则只读取外层测试年之前的交易。外层测试结果永远不参与当年版本选择；
所有输出均为研究产物，不修改实盘策略或发布状态。
"""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from scripts.run_paper_ab_filtered_daily_ops import (
    condition_strategy_config,
    configured_c_condition_profiles,
    configured_c_conditions,
    reject_strategy_risk_mask,
)
from src.adjusted_returns import linked_forward_adjusted_return
from src.market_rules import (
    fixed_close_sell_executable,
    fixed_open_buy_executable,
    listing_trade_day_number,
    price_limit_pct,
)
from src.mechanical_compound import mechanical_compound
from src.paper_candidate_generator import PaperCandidateGenerator
from src.strategy_e import (
    build_r1_universe_from_pool,
    load_e_spec,
    resolve_exit_offset as resolve_e_exit_offset,
    select_e_daily_picks,
)
from src.strict_asof import assert_selection_columns_strict
from src.trading_fees import account_return_after_fees
from src.utils.config import get_project_root, load_json_config, mkdir_p


LEGS = ("D", "A", "E", "C")
DAILY_PRIORITY = ("A", "E", "C")
BASELINE_VARIANT = {
    "D": "D_CURRENT_BASELINE",
    "A": "A_CURRENT_BASELINE",
    "E": "E_CURRENT_BASELINE",
    "C": "C_CURRENT_BASELINE",
}
EXPECTED_FILL_METHOD = "asof_turnover_space_proxy_v2"


def normalize_date(value: object) -> str:
    return str(value or "").replace(".0", "")


def canonical_text(values: pd.Series) -> pd.Series:
    return values.fillna("missing").astype(str).str.replace(r"\.0$", "", regex=True)


def truth_series(frame: pd.DataFrame, column: str, default: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="bool")
    return frame[column].fillna(default).astype(str).str.lower().isin(
        {"1", "true", "yes"}
    )


def max_consecutive_losses(values: Iterable[float]) -> int:
    current = maximum = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def wilson_interval(wins: int, count: int) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 0.0
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(
        p * (1 - p) / count + z * z / (4 * count * count)
    ) / denominator
    return center - radius, center + radius


def return_metrics(
    returns: pd.Series | Iterable[float],
    *,
    bootstrap: bool = False,
    seed: int = 20260820,
) -> dict[str, Any]:
    values = pd.to_numeric(pd.Series(returns, dtype="float64"), errors="coerce").dropna().to_numpy()
    if len(values) == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
            "win_rate_wilson_95_lower": 0.0,
            "win_rate_wilson_95_upper": 0.0,
            "avg_return_bootstrap_95_lower": 0.0,
            "avg_return_bootstrap_95_upper": 0.0,
        }
    compound = mechanical_compound(values)
    positive = values[values > 0]
    negative = values[values < 0]
    wins = int((values > 0).sum())
    wilson_low, wilson_high = wilson_interval(wins, len(values))
    bootstrap_low = bootstrap_high = float(values.mean())
    if bootstrap:
        rng = np.random.default_rng(seed)
        means: list[np.ndarray] = []
        # 分块生成，避免样本数较大时一次创建过大的二维数组。
        for size in (1000, 1000, 1000, 1000, 1000):
            indexes = rng.integers(0, len(values), size=(size, len(values)))
            means.append(values[indexes].mean(axis=1))
        bootstrap_low, bootstrap_high = np.quantile(np.concatenate(means), [0.025, 0.975])
    return {
        "trade_count": int(len(values)),
        "win_rate": float(wins / len(values)),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "equity_multiple": compound.equity_multiple,
        "max_drawdown": compound.max_drawdown,
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(values),
        "win_rate_wilson_95_lower": float(wilson_low),
        "win_rate_wilson_95_upper": float(wilson_high),
        "avg_return_bootstrap_95_lower": float(bootstrap_low),
        "avg_return_bootstrap_95_upper": float(bootstrap_high),
    }


def dataframe_to_markdown(frame: pd.DataFrame) -> str:
    """无第三方tabulate依赖的紧凑Markdown表格。"""

    if frame.empty:
        return "无数据。"
    columns = [str(column) for column in frame.columns]

    def cell(value: object) -> str:
        if pd.isna(value):
            return ""
        if isinstance(value, float):
            text = f"{value:.6g}"
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    buy_date: str = ""
    exit_date: str = ""
    stock_return_before_fees: float | None = None
    account_return: float | None = None
    exit_rule: str = ""


class ExecutionEngine:
    """有界缓存的前复权、涨跌停、费用和滑点执行器。"""

    def __init__(self, config_path: str | Path, cache_size: int = 24) -> None:
        self.root = get_project_root()
        self.config = load_json_config(config_path)
        calendar = pd.read_csv(
            self.root / "data/raw/trade_calendar.csv", dtype={"cal_date": str}
        )
        if "is_open" in calendar.columns:
            calendar = calendar[
                calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
            ]
        self.trade_dates = sorted(calendar["cal_date"].map(normalize_date).tolist())
        self.date_index = {date: index for index, date in enumerate(self.trade_dates)}
        basic_path = self.root / "data/raw/stock_basic/stock_basic_all.csv"
        self.stock_basic = (
            pd.read_csv(
                basic_path,
                dtype={"ts_code": str, "name": str, "list_date": str},
                low_memory=False,
            )
            .drop_duplicates("ts_code", keep="last")
            .set_index("ts_code")
        )
        self.daily_dir = self.root / "data/raw/daily"
        self.cache_size = cache_size
        self.day_cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self.result_cache: dict[tuple[str, str, str, int], ExecutionResult] = {}
        analysis = self.config.get("analysis", {})
        self.position_pct = 0.825
        self.d_fill_stress = 0.80
        self.commission_rate = float(analysis.get("commission_rate", 0.0003))
        self.transfer_fee_rate = float(analysis.get("transfer_fee_rate", 0.00001))
        self.stamp_tax_schedule = analysis.get("stamp_tax_schedule")

    def day(self, trade_date: str) -> pd.DataFrame | None:
        if trade_date in self.day_cache:
            frame = self.day_cache.pop(trade_date)
            self.day_cache[trade_date] = frame
            return None if frame.empty else frame
        path = self.daily_dir / f"{trade_date}.csv"
        frame = (
            pd.read_csv(path, dtype={"ts_code": str}, low_memory=False).set_index("ts_code")
            if path.exists()
            else pd.DataFrame()
        )
        self.day_cache[trade_date] = frame
        while len(self.day_cache) > self.cache_size:
            self.day_cache.popitem(last=False)
        return None if frame.empty else frame

    def stock_meta(self, code: str, name: str) -> tuple[str, str]:
        resolved_name = name
        list_date = ""
        if code in self.stock_basic.index:
            row = self.stock_basic.loc[code]
            resolved_name = resolved_name or str(row.get("name", "") or "")
            list_date = normalize_date(row.get("list_date", ""))
        return resolved_name, list_date

    def account_return(self, stock_return: float, exit_date: str, position_pct: float) -> float:
        return account_return_after_fees(
            stock_return_before_fees=stock_return,
            exit_date=exit_date,
            position_pct=position_pct,
            commission_rate=self.commission_rate,
            transfer_fee_rate=self.transfer_fee_rate,
            stamp_tax_schedule=self.stamp_tax_schedule,
        )

    def execute(self, row: pd.Series, entry_model: str, hold: int) -> ExecutionResult:
        signal_date = normalize_date(row["trade_date"])
        code = str(row["ts_code"])
        key = (entry_model, signal_date, code, int(hold))
        if key in self.result_cache:
            return self.result_cache[key]
        if entry_model == "d_limit":
            result = self._execute_d(row, hold)
        elif entry_model == "t1_open":
            result = self._execute_open(row, hold)
        else:
            raise ValueError(f"未知入场模型：{entry_model}")
        self.result_cache[key] = result
        return result

    def _sell(
        self,
        *,
        signal_date: str,
        code: str,
        name: str,
        list_date: str,
        buy_date: str,
        buy_price: float,
        hold: int,
        position_pct: float,
    ) -> ExecutionResult:
        signal_index = self.date_index.get(signal_date)
        if signal_index is None:
            return ExecutionResult("NO_CALENDAR")
        last_status = "SELL_UNRESOLVED"
        for offset in range(hold, hold + 4):
            if signal_index + offset >= len(self.trade_dates):
                break
            exit_date = self.trade_dates[signal_index + offset]
            frame = self.day(exit_date)
            if frame is None or code not in frame.index:
                last_status = "NO_PRICE"
                continue
            exit_row = frame.loc[code]
            pre_close = float(exit_row.get("pre_close", 0) or 0)
            close = float(exit_row.get("close", 0) or 0)
            listing_day = listing_trade_day_number(
                list_date, exit_date, self.trade_dates
            )
            limit_pct = price_limit_pct(
                code,
                name=name,
                trade_date=exit_date,
                listing_day_number=listing_day,
            )
            if pre_close > 0 and not fixed_close_sell_executable(
                pre_close=pre_close, close_price=close, limit_pct=limit_pct
            ):
                last_status = "LIMIT_DOWN_DELAY"
                continue
            if close <= 0:
                last_status = "BAD_PRICE"
                continue
            try:
                stock_return = linked_forward_adjusted_return(
                    ts_code=code,
                    buy_date=buy_date,
                    buy_price=buy_price,
                    sell_date=exit_date,
                    sell_price=close * 0.999,
                    trade_dates=self.trade_dates,
                    daily_loader=self.day,
                )
            except ValueError:
                last_status = "NO_ADJUSTED_PRICE"
                continue
            return ExecutionResult(
                "OK",
                buy_date=buy_date,
                exit_date=exit_date,
                stock_return_before_fees=float(stock_return),
                account_return=self.account_return(stock_return, exit_date, position_pct),
                exit_rule="FIXED_CLOSE" if offset == hold else "LIMIT_DOWN_DELAYED_CLOSE",
            )
        return ExecutionResult(last_status, buy_date=buy_date)

    def _execute_open(self, row: pd.Series, hold: int) -> ExecutionResult:
        signal_date = normalize_date(row["trade_date"])
        signal_index = self.date_index.get(signal_date)
        if signal_index is None or signal_index + 1 >= len(self.trade_dates):
            return ExecutionResult("NO_CALENDAR")
        buy_date = self.trade_dates[signal_index + 1]
        code = str(row["ts_code"])
        name, list_date = self.stock_meta(code, str(row.get("name", "") or ""))
        frame = self.day(buy_date)
        if frame is None or code not in frame.index:
            return ExecutionResult("NO_PRICE", buy_date=buy_date)
        buy_row = frame.loc[code]
        open_price = float(buy_row.get("open", 0) or 0)
        pre_close = float(buy_row.get("pre_close", 0) or 0)
        if open_price <= 0:
            return ExecutionResult("BAD_PRICE", buy_date=buy_date)
        listing_day = listing_trade_day_number(list_date, buy_date, self.trade_dates)
        limit_pct = price_limit_pct(
            code, name=name, trade_date=buy_date, listing_day_number=listing_day
        )
        if pre_close > 0 and not fixed_open_buy_executable(
            pre_close=pre_close, open_price=open_price, limit_pct=limit_pct
        ):
            return ExecutionResult("LIMIT_UP_UNBUYABLE", buy_date=buy_date)
        return self._sell(
            signal_date=signal_date,
            code=code,
            name=name,
            list_date=list_date,
            buy_date=buy_date,
            buy_price=open_price * 1.001,
            hold=hold,
            position_pct=self.position_pct,
        )

    def _execute_d(self, row: pd.Series, hold: int) -> ExecutionResult:
        signal_date = normalize_date(row["trade_date"])
        code = str(row["ts_code"])
        buy_price = float(row.get("limit_close", 0) or 0)
        if buy_price <= 0:
            return ExecutionResult("BAD_PRICE", buy_date=signal_date)
        name, list_date = self.stock_meta(code, str(row.get("name", "") or ""))
        return self._sell(
            signal_date=signal_date,
            code=code,
            name=name,
            list_date=list_date,
            buy_date=signal_date,
            buy_price=buy_price,
            hold=hold,
            position_pct=self.position_pct * self.d_fill_stress,
        )


class NestedWalkForwardResearch:
    def __init__(
        self,
        *,
        research_config_path: str | Path = "config/five_year_strategy_research.json",
        runtime_config_path: str | Path = "config/config.json",
        strategy_config_path: str | Path = "config/strategy_config.json",
    ) -> None:
        self.root = get_project_root()
        self.research_config_path = self._resolve(research_config_path)
        self.runtime_config_path = self._resolve(runtime_config_path)
        self.strategy_config_path = self._resolve(strategy_config_path)
        self.config = json.loads(self.research_config_path.read_text(encoding="utf-8"))
        self.runtime_config = load_json_config(self.runtime_config_path)
        self.strategy_config = load_json_config(self.strategy_config_path)
        data_config = self.config["data"]
        self.data_root = self._resolve(data_config["research_root"])
        self.report_root = self._resolve(data_config["report_root"])
        self.pool_path = self.data_root / "strict_feature_pool.csv"
        self.manifest_path = self.data_root / "dataset_manifest.json"
        self.outer_years = [int(value) for value in data_config["outer_oos_years"]]
        self.variants = list(self.config["variants"])
        self.variant_by_id = {str(item["id"]): item for item in self.variants}
        self.execution = ExecutionEngine(self.runtime_config_path)
        self._validate_config()

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate

    def _validate_config(self) -> None:
        if self.config.get("mode") != "research_only":
            raise ValueError("五年优化必须保持 research_only")
        if bool(self.config.get("live_release_allowed", True)):
            raise ValueError("五年研究配置禁止直接发布实盘")
        ids = [str(item["id"]) for item in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("五年研究版本ID重复")
        for leg in LEGS:
            if BASELINE_VARIANT[leg] not in ids:
                raise ValueError(f"{leg}缺少当前基准版本")
            if not any(
                str(item["leg"]) == leg
                and bool(item.get("eligible_for_optimization", True))
                for item in self.variants
            ):
                raise ValueError(f"{leg}没有可参与walk-forward的版本")
        selection_columns: list[str] = []
        for item in self.variants:
            for key in (
                "conditions",
                "excludes",
                "numeric_min",
                "numeric_max",
                "post_gate_excludes",
            ):
                selection_columns.extend(str(value) for value in item.get(key, {}))
            selection_columns.extend(str(value) for value in item.get("rank_columns", []))
        # d_open_times_preference 是 open_times 的确定性派生字段，不是结果字段。
        assert_selection_columns_strict(
            [value for value in selection_columns if value != "d_open_times_preference"],
            context="NestedWalkForwardResearch.variants",
        )

    def load_pool(self) -> pd.DataFrame:
        if not self.pool_path.exists() or not self.manifest_path.exists():
            raise FileNotFoundError("缺少五年严格研究底座，请先运行 --build-data")
        pool = pd.read_csv(
            self.pool_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        pool["trade_date"] = canonical_text(pool["trade_date"])
        if pool.duplicated(["trade_date", "ts_code"]).any():
            raise RuntimeError("五年研究特征池存在重复键")
        method = pool["fill_probability_method"].astype(str)
        if not method.eq(EXPECTED_FILL_METHOD).all():
            raise RuntimeError("五年研究池混入非严格as-of成交评分")
        training_end = canonical_text(pool["model_training_end_date"])
        if not (training_end < pool["trade_date"]).all():
            raise RuntimeError("五年研究池出现成交模型同日或未来训练")
        pool["d_open_times_preference"] = pd.to_numeric(
            pool["open_times"], errors="coerce"
        ).eq(2).astype(int)
        pool["_year"] = pool["trade_date"].str[:4].astype(int)
        return pool

    @staticmethod
    def apply_common_filters(pool: pd.DataFrame) -> pd.DataFrame:
        result = pool.copy()
        result = result[truth_series(result, "allow_buy_reliable", False)]
        result = result[truth_series(result, "is_fill_score_reliable", False)]
        result = result[~truth_series(result, "is_fd_amount_abnormal", True)]
        result = result[truth_series(result, "strategy_compatible", False)]
        names = result.get("name", pd.Series("", index=result.index)).fillna("").astype(str).str.upper()
        is_st = truth_series(result, "is_st", False)
        result = result[~(is_st | names.str.contains("ST", regex=False) | names.str.contains("退", regex=False))]
        return result.copy()

    def select_generic(self, pool: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
        result = self.apply_common_filters(pool)
        for column, accepted in variant.get("conditions", {}).items():
            if column not in result.columns:
                raise RuntimeError(f"{variant['id']}条件字段缺失：{column}")
            values = {str(value) for value in accepted}
            result = result[canonical_text(result[column]).isin(values)]
        for column, excluded in variant.get("excludes", {}).items():
            if column not in result.columns:
                raise RuntimeError(f"{variant['id']}排除字段缺失：{column}")
            values = {str(value) for value in excluded}
            result = result[~canonical_text(result[column]).isin(values)]
        for column, threshold in variant.get("numeric_min", {}).items():
            result = result[pd.to_numeric(result[column], errors="coerce").ge(float(threshold))]
        for column, threshold in variant.get("numeric_max", {}).items():
            result = result[pd.to_numeric(result[column], errors="coerce").le(float(threshold))]
        if result.empty:
            return result
        columns = [str(value) for value in variant.get("rank_columns", ["fill_probability", "ts_code"])]
        ascending = [bool(value) for value in variant.get("rank_ascending", [False, True])]
        if len(columns) != len(ascending):
            raise ValueError(f"{variant['id']}排序字段与方向数量不一致")
        result = result.sort_values(
            ["trade_date", *columns],
            ascending=[True, *ascending],
            na_position="last",
        ).groupby("trade_date", as_index=False).head(1)
        for column, excluded in variant.get("post_gate_excludes", {}).items():
            values = {str(value) for value in excluded}
            result = result[~canonical_text(result[column]).isin(values)]
        return result.copy().reset_index(drop=True)

    def _current_generator(self, *, c_strategy: bool) -> PaperCandidateGenerator:
        config = self.strategy_config
        selected = (
            condition_strategy_config(
                config,
                configured_c_conditions(config),
                "five_year_c_current",
                condition_profiles=configured_c_condition_profiles(config),
            )
            if c_strategy
            else config
        )
        generator = PaperCandidateGenerator(
            self.strategy_config_path,
            input_trades_path=self.pool_path,
        )
        generator.config = selected
        generator.paper_config = selected.get("paper_candidate", {})
        generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
        return generator

    def select_current_a_or_c(self, pool: pd.DataFrame, leg: str) -> pd.DataFrame:
        generator = self._current_generator(c_strategy=leg == "C")
        filtered = generator.apply_strategy_filters(self.apply_common_filters(pool))
        picks: list[pd.DataFrame] = []
        for _, group in filtered.groupby("trade_date", sort=True):
            ranked = generator.rank_candidates(group.copy()).head(1).copy()
            if ranked.empty:
                continue
            if leg == "C":
                ranked["risk_flags"] = [
                    generator.build_risk_flags(row)
                    for row in ranked.itertuples(index=False)
                ]
                rejected = reject_strategy_risk_mask(
                    ranked, self.strategy_config, "c_strategy"
                )
                ranked = ranked[~rejected]
            if not ranked.empty:
                picks.append(ranked)
        return pd.concat(picks, ignore_index=True) if picks else pd.DataFrame()

    def select_variant(self, pool: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
        selector = str(variant["selector"])
        if selector == "generic":
            picks = self.select_generic(pool, variant)
        elif selector == "current_a":
            picks = self.select_current_a_or_c(pool, "A")
        elif selector == "current_c":
            picks = self.select_current_a_or_c(pool, "C")
        elif selector == "current_e":
            spec = load_e_spec(self.root)
            universe = build_r1_universe_from_pool(
                self.apply_common_filters(pool), spec, audit_readiness=True
            )
            picks = select_e_daily_picks(universe, spec)
            if not picks.empty:
                picks = picks.copy()
                picks["_resolved_hold"] = picks["exit_rule"].map(
                    lambda value: resolve_e_exit_offset(spec, str(value))
                )
        else:
            raise ValueError(f"未知selector：{selector}")
        if picks.empty:
            return pd.DataFrame()
        picks = picks.copy()
        picks["variant_id"] = str(variant["id"])
        picks["strategy_leg"] = str(variant["leg"])
        if "_resolved_hold" not in picks.columns:
            picks["_resolved_hold"] = int(variant["hold"])
        picks["_entry_model"] = str(variant["entry_model"])
        return picks.sort_values("trade_date").reset_index(drop=True)

    def add_outcomes(self, picks: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
        if picks.empty:
            return pd.DataFrame(
                columns=[
                    "trade_date", "strategy_leg", "variant_id", "ts_code", "status",
                    "buy_date", "exit_date", "account_return",
                ]
            )
        rows: list[dict[str, Any]] = []
        for _, row in picks.iterrows():
            hold = int(row.get("_resolved_hold", variant["hold"]))
            execution = self.execution.execute(row, str(variant["entry_model"]), hold)
            rows.append(
                {
                    "trade_date": normalize_date(row["trade_date"]),
                    "strategy_leg": str(variant["leg"]),
                    "variant_id": str(variant["id"]),
                    "ts_code": str(row["ts_code"]),
                    "name": str(row.get("name", "") or ""),
                    "hold_offset": hold,
                    "entry_model": str(variant["entry_model"]),
                    "status": execution.status,
                    "buy_date": execution.buy_date,
                    "exit_date": execution.exit_date,
                    "exit_rule": execution.exit_rule,
                    "stock_return_before_fees": execution.stock_return_before_fees,
                    "account_return": execution.account_return,
                    "fill_probability": float(row.get("fill_probability", 0) or 0),
                    "available_fill_amount": float(row.get("available_fill_amount", 0) or 0),
                    "estimated_turnover_amount": float(row.get("estimated_turnover_amount", 0) or 0),
                    "current_queue_amount": float(row.get("current_queue_amount", 0) or 0),
                    "planned_buy_amount": float(row.get("planned_buy_amount", 0) or 0),
                    "signal_day_amount_thousand_yuan": float(row.get("amount", 0) or 0),
                    "model_training_end_date": normalize_date(row.get("model_training_end_date", "")),
                }
            )
        return pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)

    @staticmethod
    def yearly_stability(trades: pd.DataFrame) -> dict[str, Any]:
        valid = trades[trades["status"].eq("OK")].copy()
        if valid.empty:
            return {
                "years_with_trades": 0,
                "positive_years": 0,
                "positive_year_ratio": 0.0,
                "minimum_year_equity_multiple": 1.0,
                "median_year_log_multiple": 0.0,
            }
        valid["year"] = valid["trade_date"].str[:4]
        multiples = valid.groupby("year")["account_return"].apply(
            lambda values: float(np.prod(1 + pd.to_numeric(values)))
        )
        return {
            "years_with_trades": int(len(multiples)),
            "positive_years": int((multiples > 1).sum()),
            "positive_year_ratio": float((multiples > 1).mean()),
            "minimum_year_equity_multiple": float(multiples.min()),
            "median_year_log_multiple": float(np.log(multiples.clip(lower=1e-12)).median()),
        }

    def score_train_candidate(
        self,
        trades: pd.DataFrame,
        *,
        test_year: int,
    ) -> dict[str, Any]:
        train = trades[
            trades["trade_date"].str[:4].astype(int).lt(test_year)
            & trades["status"].eq("OK")
        ].copy()
        validation_year = test_year - 1
        validation = train[train["trade_date"].str[:4].astype(int).eq(validation_year)]
        discovery = train[train["trade_date"].str[:4].astype(int).lt(validation_year)]
        train_metrics = return_metrics(train["account_return"])
        validation_metrics = return_metrics(validation["account_return"])
        discovery_metrics = return_metrics(discovery["account_return"])
        stability = self.yearly_stability(train)
        gate = self.config["selection_gate"]
        reasons: list[str] = []
        if train_metrics["trade_count"] < int(gate["minimum_train_trades"]):
            reasons.append("TRAIN_SAMPLE")
        if validation_metrics["trade_count"] < int(gate["minimum_inner_validation_trades"]):
            reasons.append("VALIDATION_SAMPLE")
        if stability["years_with_trades"] < int(gate["minimum_train_years_with_trades"]):
            reasons.append("TRAIN_YEAR_COVERAGE")
        if stability["positive_year_ratio"] < float(gate["minimum_positive_train_year_ratio"]):
            reasons.append("POSITIVE_YEAR_RATIO")
        if train_metrics["equity_multiple"] < float(gate["minimum_train_equity_multiple"]):
            reasons.append("TRAIN_EQUITY")
        if validation_metrics["equity_multiple"] < float(gate["minimum_inner_validation_equity_multiple"]):
            reasons.append("VALIDATION_EQUITY")
        if train_metrics["max_drawdown"] < float(gate["maximum_train_drawdown"]):
            reasons.append("TRAIN_DRAWDOWN")

        train_years = max(stability["years_with_trades"], 1)
        discovery_log = math.log(max(discovery_metrics["equity_multiple"], 1e-12))
        validation_log = math.log(max(validation_metrics["equity_multiple"], 1e-12))
        score = (
            0.35 * discovery_log / train_years
            + 0.35 * validation_log
            + 0.20 * stability["median_year_log_multiple"]
            + 0.10 * math.log(max(stability["minimum_year_equity_multiple"], 1e-12))
            + 0.75 * train_metrics["max_drawdown"]
        )
        return {
            "test_year": test_year,
            "validation_year": validation_year,
            **{f"train_{key}": value for key, value in train_metrics.items() if "95_" not in key},
            **{f"validation_{key}": value for key, value in validation_metrics.items() if "95_" not in key},
            **{f"discovery_{key}": value for key, value in discovery_metrics.items() if "95_" not in key},
            **stability,
            "selection_score": float(score),
            "selection_gate_passed": not reasons,
            "selection_gate_reasons": ";".join(reasons),
        }

    def build_all_variant_trades(self, pool: pd.DataFrame) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        for index, variant in enumerate(self.variants, start=1):
            print(
                f"VARIANT_REPLAY {index}/{len(self.variants)} {variant['id']}",
                flush=True,
            )
            picks = self.select_variant(pool, variant)
            result[str(variant["id"])] = self.add_outcomes(picks, variant)
        return result

    def select_folds(
        self,
        all_trades: dict[str, pd.DataFrame],
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        score_rows: list[dict[str, Any]] = []
        selection_rows: list[dict[str, Any]] = []
        oos_frames: list[pd.DataFrame] = []
        for test_year in self.outer_years:
            for leg in LEGS:
                candidates: list[dict[str, Any]] = []
                for variant in self.variants:
                    if str(variant["leg"]) != leg or not bool(
                        variant.get("eligible_for_optimization", True)
                    ):
                        continue
                    score = self.score_train_candidate(
                        all_trades[str(variant["id"])], test_year=test_year
                    )
                    row = {
                        "strategy_leg": leg,
                        "variant_id": str(variant["id"]),
                        **score,
                    }
                    score_rows.append(row)
                    if score["selection_gate_passed"]:
                        candidates.append(row)
                chosen = sorted(
                    candidates,
                    key=lambda row: (-float(row["selection_score"]), str(row["variant_id"])),
                )[0] if candidates else None
                selection_rows.append(
                    {
                        "test_year": test_year,
                        "strategy_leg": leg,
                        "selected_variant_id": "" if chosen is None else chosen["variant_id"],
                        "selection_status": "NO_VARIANT_PASSED" if chosen is None else "SELECTED_FROM_PAST_ONLY",
                        "training_end_date": f"{test_year - 1}1231",
                        "test_start_date": f"{test_year}0101",
                        "selection_score": None if chosen is None else chosen["selection_score"],
                    }
                )
                if chosen is None:
                    continue
                test = all_trades[str(chosen["variant_id"])].copy()
                test = test[
                    test["trade_date"].str[:4].astype(int).eq(test_year)
                    & test["status"].eq("OK")
                ].copy()
                if not test.empty:
                    test["outer_test_year"] = test_year
                    test["outer_training_end_date"] = f"{test_year - 1}1231"
                    oos_frames.append(test)
        scores = pd.DataFrame(score_rows)
        selections = pd.DataFrame(selection_rows)
        oos = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
        return scores, selections, oos

    def select_forward_freeze(
        self,
        all_trades: dict[str, pd.DataFrame],
        *,
        accepted_legs: set[str],
    ) -> list[dict[str, Any]]:
        freeze_year = max(self.outer_years) + 1
        rows: list[dict[str, Any]] = []
        for leg in LEGS:
            candidates: list[dict[str, Any]] = []
            for variant in self.variants:
                if str(variant["leg"]) != leg or not bool(
                    variant.get("eligible_for_optimization", True)
                ):
                    continue
                score = self.score_train_candidate(
                    all_trades[str(variant["id"])], test_year=freeze_year
                )
                if score["selection_gate_passed"]:
                    candidates.append({"variant": variant, "score": score})
            chosen = sorted(
                candidates,
                key=lambda item: (
                    -float(item["score"]["selection_score"]),
                    str(item["variant"]["id"]),
                ),
            )[0] if candidates else None
            if chosen is None:
                status = "NO_VARIANT_PASSED_TRAINING_GATE"
            elif leg not in accepted_legs:
                status = "REJECTED_BY_OUTER_OOS_OR_COMBINATION_MARGIN"
            else:
                status = "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY"
            rows.append(
                {
                    "strategy_leg": leg,
                    "variant_id": (
                        str(chosen["variant"]["id"])
                        if chosen is not None and leg in accepted_legs
                        else ""
                    ),
                    "status": status,
                    "frozen_rule": (
                        chosen["variant"]
                        if chosen is not None and leg in accepted_legs
                        else None
                    ),
                    "selection_score": (
                        None if chosen is None else chosen["score"]["selection_score"]
                    ),
                    "training_gate_candidate_before_outer_rejection": (
                        "" if chosen is None else str(chosen["variant"]["id"])
                    ),
                }
            )
        return rows

    def current_baseline_oos(
        self, all_trades: dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        frames = []
        allowed_years = set(self.outer_years)
        for leg, variant_id in BASELINE_VARIANT.items():
            frame = all_trades[variant_id]
            frame = frame[
                frame["status"].eq("OK")
                & frame["trade_date"].str[:4].astype(int).isin(allowed_years)
            ].copy()
            if not frame.empty:
                frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def replay_combination(
        self,
        trades: pd.DataFrame,
        *,
        enabled: set[str] | None = None,
    ) -> pd.DataFrame:
        enabled = set(LEGS) if enabled is None else set(enabled)
        start = f"{min(self.outer_years)}0101"
        raw_last = max(path.stem for path in (self.root / "data/raw/daily").glob("*.csv"))
        dates = [date for date in self.execution.trade_dates if start <= date <= raw_last]
        maps: dict[str, dict[str, dict[str, Any]]] = {leg: {} for leg in LEGS}
        if not trades.empty:
            for _, row in trades.sort_values("trade_date").iterrows():
                leg = str(row["strategy_leg"])
                maps[leg][normalize_date(row["trade_date"])] = row.to_dict()
        equity = 1.0
        occupied_until = occupied_leg = ""
        rows: list[dict[str, Any]] = []
        for signal_date in dates:
            if occupied_until and signal_date < occupied_until:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "status": "SKIP_OCCUPIED",
                        "strategy_leg": "",
                        "account_return": 0.0,
                        "equity_after": equity,
                    }
                )
                continue
            exiting_today = bool(occupied_until and signal_date == occupied_until)
            occupied_until = occupied_leg = ""
            selected: dict[str, Any] | None = None
            if "D" in enabled and not exiting_today:
                selected = maps["D"].get(signal_date)
            if selected is None:
                for leg in DAILY_PRIORITY:
                    if leg in enabled and signal_date in maps[leg]:
                        selected = maps[leg][signal_date]
                        break
            if selected is None:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "status": "NO_CANDIDATE",
                        "strategy_leg": "",
                        "account_return": 0.0,
                        "equity_after": equity,
                    }
                )
                continue
            value = float(selected["account_return"])
            equity *= 1 + value
            occupied_until = normalize_date(selected["exit_date"])
            occupied_leg = str(selected["strategy_leg"])
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "EXECUTED",
                    "strategy_leg": occupied_leg,
                    "variant_id": selected["variant_id"],
                    "ts_code": selected["ts_code"],
                    "name": selected.get("name", ""),
                    "exit_date": occupied_until,
                    "account_return": value,
                    "equity_after": equity,
                }
            )
        detail = pd.DataFrame(rows)
        detail["peak_equity"] = detail["equity_after"].cummax()
        detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1
        return detail

    @staticmethod
    def combination_metrics(detail: pd.DataFrame) -> dict[str, Any]:
        trades = detail[detail["status"].eq("EXECUTED")]
        metrics = return_metrics(trades["account_return"], bootstrap=True, seed=20260825)
        metrics["leg_counts"] = trades["strategy_leg"].value_counts().sort_index().to_dict()
        metrics["max_drawdown"] = float(detail["drawdown"].min()) if not detail.empty else 0.0
        return metrics

    def leg_metrics_and_gates(
        self,
        oos: pd.DataFrame,
        optimized_detail: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        gate = self.config["publication_gate"]
        full_metrics = self.combination_metrics(optimized_detail)
        leg_rows: list[dict[str, Any]] = []
        marginal_rows: list[dict[str, Any]] = []
        for leg in LEGS:
            sample = oos[oos["strategy_leg"].eq(leg)] if not oos.empty else pd.DataFrame()
            metrics = return_metrics(
                sample.get("account_return", pd.Series(dtype=float)),
                bootstrap=True,
                seed=20260820 + LEGS.index(leg),
            )
            yearly = self.yearly_stability(sample) if not sample.empty else self.yearly_stability(pd.DataFrame(columns=["status", "trade_date", "account_return"]))
            statistical_reasons: list[str] = []
            if metrics["trade_count"] < int(gate["minimum_oos_trades"]):
                statistical_reasons.append("OOS_SAMPLE")
            if yearly["positive_years"] < int(gate["minimum_positive_oos_years"]):
                statistical_reasons.append("POSITIVE_OOS_YEARS")
            if metrics["win_rate_wilson_95_lower"] < float(gate["minimum_win_rate_wilson_95_lower"]):
                statistical_reasons.append("WIN_RATE_CONFIDENCE")
            if metrics["avg_return_bootstrap_95_lower"] < float(gate["minimum_average_return_bootstrap_95_lower"]):
                statistical_reasons.append("AVG_RETURN_CONFIDENCE")
            if metrics["max_drawdown"] < float(gate["maximum_oos_drawdown"]):
                statistical_reasons.append("OOS_DRAWDOWN")
            without = self.replay_combination(oos, enabled=set(LEGS) - {leg})
            without_metrics = self.combination_metrics(without)
            equity_delta = full_metrics["equity_multiple"] - without_metrics["equity_multiple"]
            drawdown_delta = full_metrics["max_drawdown"] - without_metrics["max_drawdown"]
            marginal_pass = equity_delta >= -1e-12
            marginal_rows.append(
                {
                    "strategy_leg": leg,
                    "with_leg_equity_multiple": full_metrics["equity_multiple"],
                    "without_leg_equity_multiple": without_metrics["equity_multiple"],
                    "with_minus_without_equity_multiple": equity_delta,
                    "with_leg_max_drawdown": full_metrics["max_drawdown"],
                    "without_leg_max_drawdown": without_metrics["max_drawdown"],
                    "with_minus_without_drawdown": drawdown_delta,
                    "compound_non_decreasing_passed": marginal_pass,
                }
            )
            leg_rows.append(
                {
                    "strategy_leg": leg,
                    **metrics,
                    **yearly,
                    "statistical_gate_passed": not statistical_reasons,
                    "statistical_gate_reasons": ";".join(statistical_reasons),
                    "combination_compound_non_decreasing_passed": marginal_pass,
                    "research_decision": (
                        "RETIRE_FROM_RESEARCH_COMBINATION"
                        if not marginal_pass
                        else (
                            "KEEP_FOR_FORWARD_PAPER_OOS"
                            if metrics["equity_multiple"] > 1.0
                            else "KEEP_COMBINATION_MARGIN_ONLY_NO_FORWARD_FREEZE"
                        )
                    ),
                    "live_release_allowed": False,
                }
            )
        return pd.DataFrame(leg_rows), pd.DataFrame(marginal_rows)

    def leg_yearly_metrics(self, oos: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for year in self.outer_years:
            for leg in LEGS:
                sample = (
                    oos[
                        oos["strategy_leg"].eq(leg)
                        & oos["trade_date"].str[:4].astype(int).eq(year)
                    ]
                    if not oos.empty
                    else pd.DataFrame()
                )
                metrics = return_metrics(
                    sample.get("account_return", pd.Series(dtype=float))
                )
                rows.append(
                    {
                        "year": year,
                        "strategy_leg": leg,
                        "selected_variants": (
                            ";".join(sorted(sample["variant_id"].astype(str).unique()))
                            if not sample.empty
                            else ""
                        ),
                        **{key: value for key, value in metrics.items() if "95_" not in key},
                    }
                )
        return pd.DataFrame(rows)

    def combination_yearly_metrics(
        self,
        details: dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for portfolio, detail in details.items():
            executed = detail[detail["status"].eq("EXECUTED")].copy()
            executed["year"] = executed["signal_date"].str[:4].astype(int)
            for year in self.outer_years:
                sample = executed[executed["year"].eq(year)]
                metrics = return_metrics(sample["account_return"])
                rows.append(
                    {
                        "portfolio": portfolio,
                        "year": year,
                        **{key: value for key, value in metrics.items() if "95_" not in key},
                        "leg_counts": json.dumps(
                            sample["strategy_leg"].value_counts().sort_index().to_dict(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )
        return pd.DataFrame(rows)

    @staticmethod
    def capacity_proxy(oos: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for leg in LEGS:
            sample = oos[oos["strategy_leg"].eq(leg)] if not oos.empty else pd.DataFrame()
            available = pd.to_numeric(
                sample.get("available_fill_amount", pd.Series(dtype=float)), errors="coerce"
            ).dropna()
            for planned_amount in (412_500.0, 1_000_000.0, 3_000_000.0, 10_000_000.0):
                probability = (available / planned_amount).clip(lower=0, upper=1)
                rows.append(
                    {
                        "strategy_leg": leg,
                        "trade_count": int(len(sample)),
                        "stress_planned_buy_amount": planned_amount,
                        "fill_probability_min": float(probability.min()) if not probability.empty else 0.0,
                        "fill_probability_p10": float(probability.quantile(0.10)) if not probability.empty else 0.0,
                        "fill_probability_median": float(probability.median()) if not probability.empty else 0.0,
                        "fill_probability_ge_60pct_ratio": float((probability >= 0.6).mean()) if not probability.empty else 0.0,
                        "fixed_amount_fill_proxy_passed": bool((probability >= 0.6).all()) if not probability.empty else False,
                        "minute_orderbook_verified": False,
                        "real_fill_verified": False,
                        "capacity_certified": False,
                        "note": "历史as-of换手空间压力代理；未验证分钟盘口、真实排队顺序，不能替代容量认证。",
                    }
                )
        return pd.DataFrame(rows)

    def write_summary(
        self,
        *,
        leg_metrics: pd.DataFrame,
        combination: pd.DataFrame,
        marginal: pd.DataFrame,
        selections: pd.DataFrame,
        leg_yearly: pd.DataFrame,
        combination_yearly: pd.DataFrame,
        capacity: pd.DataFrame,
    ) -> None:
        lines = [
            "# 五年严格时点嵌套 Walk-Forward 研究",
            "",
            "## 结论边界",
            "",
            "- 数据、成交评分、因子和外层逐年选择均为严格 as-of。",
            "- 这是回溯式 walk-forward，不是 untouched OOS；历史因子体系已受既有研究经验影响。",
            "- 任何版本都不得据此直接写入实盘。通过者只允许冻结后进入未来模拟盘。",
            "- 当前实盘配置、策略规则和券商状态均未修改。",
            "",
            "## 各腿外层测试汇总",
            "",
            dataframe_to_markdown(leg_metrics),
            "",
            "## 组合对比",
            "",
            dataframe_to_markdown(combination),
            "",
            "## 单腿组合边际",
            "",
            dataframe_to_markdown(marginal),
            "",
            "## 每年只用过去选择的版本",
            "",
            dataframe_to_markdown(selections),
            "",
            "## 各腿逐年外层测试",
            "",
            dataframe_to_markdown(leg_yearly),
            "",
            "## 组合逐年对比",
            "",
            dataframe_to_markdown(combination_yearly),
            "",
            "## 固定资金容量代理（不是容量认证）",
            "",
            dataframe_to_markdown(capacity),
            "",
        ]
        (self.report_root / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    def run(self) -> Path:
        mkdir_p(self.report_root)
        pool = self.load_pool()
        all_trades = self.build_all_variant_trades(pool)
        scores, selections, optimized_oos = self.select_folds(all_trades)
        current_oos = self.current_baseline_oos(all_trades)
        optimized_detail = self.replay_combination(optimized_oos)
        current_detail = self.replay_combination(current_oos)
        optimized_metrics = self.combination_metrics(optimized_detail)
        current_metrics = self.combination_metrics(current_detail)
        combination = pd.DataFrame(
            [
                {"portfolio": "CURRENT_RULES_RETROSPECTIVE", **current_metrics},
                {"portfolio": "NESTED_WALK_FORWARD_OOS", **optimized_metrics},
            ]
        )
        combination["leg_counts"] = combination["leg_counts"].map(
            lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
        leg_metrics, marginal = self.leg_metrics_and_gates(
            optimized_oos, optimized_detail
        )
        leg_yearly = self.leg_yearly_metrics(optimized_oos)
        combination_yearly = self.combination_yearly_metrics(
            {
                "CURRENT_RULES_RETROSPECTIVE": current_detail,
                "NESTED_WALK_FORWARD_OOS": optimized_detail,
            }
        )
        capacity = self.capacity_proxy(optimized_oos)
        accepted_legs = set(
            leg_metrics.loc[
                leg_metrics["equity_multiple"].gt(1.0)
                & leg_metrics["combination_compound_non_decreasing_passed"].eq(True),  # noqa: E712
                "strategy_leg",
            ].astype(str)
        )
        forward_freeze = self.select_forward_freeze(
            all_trades, accepted_legs=accepted_legs
        )

        scores.to_csv(self.report_root / "variant_fold_scores.csv", index=False, encoding="utf-8-sig")
        selections.to_csv(self.report_root / "fold_selections.csv", index=False, encoding="utf-8-sig")
        optimized_oos.to_csv(self.report_root / "oos_trades.csv", index=False, encoding="utf-8-sig")
        current_oos.to_csv(self.report_root / "current_baseline_trades.csv", index=False, encoding="utf-8-sig")
        optimized_detail.to_csv(self.report_root / "optimized_combination_replay.csv", index=False, encoding="utf-8-sig")
        current_detail.to_csv(self.report_root / "current_combination_replay.csv", index=False, encoding="utf-8-sig")
        combination.to_csv(self.report_root / "combination_comparison.csv", index=False, encoding="utf-8-sig")
        leg_metrics.to_csv(self.report_root / "leg_oos_metrics.csv", index=False, encoding="utf-8-sig")
        marginal.to_csv(self.report_root / "leg_marginal_impact.csv", index=False, encoding="utf-8-sig")
        leg_yearly.to_csv(self.report_root / "leg_oos_yearly_metrics.csv", index=False, encoding="utf-8-sig")
        combination_yearly.to_csv(self.report_root / "combination_yearly_metrics.csv", index=False, encoding="utf-8-sig")
        capacity.to_csv(self.report_root / "capacity_proxy.csv", index=False, encoding="utf-8-sig")

        config_hash = hashlib.sha256(self.research_config_path.read_bytes()).hexdigest()
        manifest_hash = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        freeze_payload = {
            "schema_version": 1,
            "status": "FUTURE_PAPER_OOS_ONLY_NOT_LIVE_RELEASE",
            "frozen_at_data_end": max(path.stem for path in (self.root / "data/raw/daily").glob("*.csv")),
            "research_config_sha256": config_hash,
            "dataset_manifest_sha256": manifest_hash,
            "untouched_oos_begins_after_freeze": True,
            "candidates": forward_freeze,
        }
        (self.report_root / "forward_freeze_candidates.json").write_text(
            json.dumps(freeze_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        publication = {
            "status": "NOT_LIVE_RELEASEABLE",
            "strict_asof_passed": True,
            "nested_outer_test_passed": True,
            "untouched_oos_passed": False,
            "capacity_certified": False,
            "real_forward_samples_after_freeze": 0,
            "live_files_modified": False,
            "reason": "缺少冻结后的真实前向样本和容量认证；回溯walk-forward不能冒充untouched OOS。",
        }
        (self.report_root / "publication_gate.json").write_text(
            json.dumps(publication, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        research_summary = {
            "schema_version": 1,
            "status": publication["status"],
            "research_disclosure": self.config["research_disclosure"],
            "data_manifest": json.loads(self.manifest_path.read_text(encoding="utf-8")),
            "combination": json.loads(combination.to_json(orient="records")),
            "leg_oos_metrics": json.loads(leg_metrics.to_json(orient="records")),
            "leg_marginal_impact": json.loads(marginal.to_json(orient="records")),
            "fold_selections": json.loads(selections.to_json(orient="records")),
            "capacity_proxy": json.loads(capacity.to_json(orient="records")),
            "publication_gate": publication,
        }
        (self.report_root / "research_summary.json").write_text(
            json.dumps(research_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.write_summary(
            leg_metrics=leg_metrics,
            combination=combination,
            marginal=marginal,
            selections=selections,
            leg_yearly=leg_yearly,
            combination_yearly=combination_yearly,
            capacity=capacity,
        )
        return self.report_root / "summary.md"
