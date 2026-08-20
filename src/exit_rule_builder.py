from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.strict_asof import (
    PointInTimeContract,
    add_audit_columns,
    validate_strict_research_frame,
    write_audit_json,
)
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


@dataclass(frozen=True)
class ExitRule:
    name: str
    rule_type: str
    max_hold_days: int
    exit_price_field: str = "close"
    stop_loss: float | None = None
    take_profit: float | None = None
    high_reversal_trigger: float | None = None
    high_reversal_floor: float | None = None
    is_executable: bool = True


class ExitRuleTradeBuilder:
    """基于同一批涨停买入信号，生成多种卖出规则的收益样本。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("exit_rule_builder")
        analysis_config = self.config.get("analysis", {})
        self.analysis_config = analysis_config
        self.daily_merged_path = self.project_root / analysis_config.get(
            "input_daily_merged_path", "data/processed/daily_merged.csv"
        )
        self.limit_up_fill_scored_path = self.project_root / analysis_config.get(
            "input_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"
        )
        self.output_trades_path = self.project_root / analysis_config.get(
            "output_exit_rule_trades_path", "data/processed/exit_rule_trade_samples.csv"
        )
        self.output_strict_asof_audit_path = self.project_root / analysis_config.get(
            "output_exit_rule_strict_asof_audit_path",
            "reports/strict_asof/exit_rule_samples_audit.json",
        )
        self.commission_rate = float(analysis_config.get("commission_rate", 0.0003))
        self.stamp_tax_rate = float(analysis_config.get("stamp_tax_rate", 0.001))
        self.transfer_fee_rate = float(analysis_config.get("transfer_fee_rate", 0.00001))
        self.slippage_rate = float(analysis_config.get("slippage_rate", 0.001))

    def build(self) -> Path:
        daily = self.load_daily_with_forward_prices()
        signals = pd.read_csv(
            self.limit_up_fill_scored_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        audit = validate_strict_research_frame(
            signals,
            contract=PointInTimeContract(dataset_name="exit_rule_fill_source"),
            selection_columns=[
                "allow_buy_reliable",
                "is_fill_score_reliable",
                "fill_probability",
            ],
            section_config=self.analysis_config,
            context="ExitRuleTradeBuilder.build",
            project_root=self.project_root,
        )
        signals = signals[signals["allow_buy_reliable"] == True].copy()  # noqa: E712
        samples = signals.merge(daily, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        samples = samples[samples["buy_open"].notna()].copy()

        rules = self.build_exit_rules()
        frames = [self.apply_exit_rule(samples, rule) for rule in rules]
        result = pd.concat(frames, ignore_index=True)
        result = add_audit_columns(result, audit)
        mkdir_p(self.output_trades_path.parent)
        result.to_csv(self.output_trades_path, index=False, encoding="utf-8-sig")
        write_audit_json(self.output_strict_asof_audit_path, audit)
        self.logger.info("卖出规则交易样本已生成: %s, 行数: %s", self.output_trades_path, len(result))
        return self.output_trades_path

    def load_daily_with_forward_prices(self) -> pd.DataFrame:
        daily = pd.read_csv(
            self.daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "open", "high", "low", "close"],
            low_memory=False,
        )
        daily = daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        grouped = daily.groupby("ts_code")
        daily["buy_trade_date"] = grouped["trade_date"].shift(-1)
        daily["buy_open"] = grouped["open"].shift(-1)
        for offset in range(1, 6):
            daily[f"d{offset}_trade_date"] = grouped["trade_date"].shift(-offset)
            daily[f"d{offset}_open"] = grouped["open"].shift(-offset)
            daily[f"d{offset}_high"] = grouped["high"].shift(-offset)
            daily[f"d{offset}_low"] = grouped["low"].shift(-offset)
            daily[f"d{offset}_close"] = grouped["close"].shift(-offset)
        return daily.drop(columns=["open", "high", "low", "close"])

    @staticmethod
    def build_exit_rules() -> list[ExitRule]:
        rules = [
            ExitRule(name="diagnostic_t1_close_not_executable", rule_type="fixed", max_hold_days=1, is_executable=False),
            ExitRule(name="t2_open", rule_type="fixed", max_hold_days=2, exit_price_field="open"),
            ExitRule(name="t2_close", rule_type="fixed", max_hold_days=2, exit_price_field="close"),
            ExitRule(name="hold_3d_close", rule_type="fixed", max_hold_days=3, exit_price_field="close"),
            ExitRule(name="hold_5d_close", rule_type="fixed", max_hold_days=5, exit_price_field="close"),
            ExitRule(
                name="high_reversal_5_to_2_hold3",
                rule_type="high_reversal",
                max_hold_days=3,
                high_reversal_trigger=0.05,
                high_reversal_floor=0.02,
            ),
            ExitRule(
                name="high_reversal_8_to_3_hold3",
                rule_type="high_reversal",
                max_hold_days=3,
                high_reversal_trigger=0.08,
                high_reversal_floor=0.03,
            ),
        ]
        for max_hold_days in [3, 5]:
            for stop_loss in [-0.03, -0.05]:
                for take_profit in [0.05, 0.10, 0.15]:
                    rules.append(
                        ExitRule(
                            name=(
                                f"stop_{abs(int(stop_loss * 100))}"
                                f"_tp_{int(take_profit * 100)}"
                                f"_hold{max_hold_days}"
                            ),
                            rule_type="stop_take",
                            max_hold_days=max_hold_days,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                        )
                    )
        return rules

    def apply_exit_rule(self, samples: pd.DataFrame, rule: ExitRule) -> pd.DataFrame:
        rows = []
        for row in samples.itertuples(index=False):
            exit_info = self.resolve_exit(row, rule)
            if exit_info is None:
                continue
            gross_return = exit_info["exit_price"] / row.buy_open - 1
            rows.append(
                {
                    **row._asdict(),
                    "exit_rule": rule.name,
                    "is_executable_exit": rule.is_executable,
                    "exit_reason": exit_info["exit_reason"],
                    "exit_trade_date": exit_info["exit_trade_date"],
                    "exit_price": exit_info["exit_price"],
                    "next_trade_date": row.buy_trade_date,
                    "next_open": row.buy_open,
                    "gross_return": gross_return,
                    "fee_rate": self.fee_rate,
                    "net_return": gross_return - self.fee_rate,
                    "is_win": gross_return - self.fee_rate > 0,
                    "holding_days_rule": rule.name,
                }
            )
        return pd.DataFrame(rows)

    def resolve_exit(self, row: object, rule: ExitRule) -> dict[str, object] | None:
        if rule.rule_type == "fixed":
            return self.resolve_fixed_exit(row, rule)
        if rule.rule_type == "stop_take":
            return self.resolve_stop_take_exit(row, rule)
        if rule.rule_type == "high_reversal":
            return self.resolve_high_reversal_exit(row, rule)
        raise ValueError(f"未知卖出规则: {rule.rule_type}")

    @staticmethod
    def resolve_fixed_exit(row: object, rule: ExitRule) -> dict[str, object] | None:
        price = getattr(row, f"d{rule.max_hold_days}_{rule.exit_price_field}")
        trade_date = getattr(row, f"d{rule.max_hold_days}_trade_date")
        if pd.isna(price) or pd.isna(trade_date):
            return None
        return {"exit_trade_date": trade_date, "exit_price": price, "exit_reason": rule.name}

    def resolve_stop_take_exit(self, row: object, rule: ExitRule) -> dict[str, object] | None:
        for offset in range(2, rule.max_hold_days + 1):
            trade_date = getattr(row, f"d{offset}_trade_date")
            high = getattr(row, f"d{offset}_high")
            low = getattr(row, f"d{offset}_low")
            close = getattr(row, f"d{offset}_close")
            if pd.isna(trade_date) or pd.isna(high) or pd.isna(low) or pd.isna(close):
                return None
            stop_price = row.buy_open * (1 + rule.stop_loss)
            take_profit_price = row.buy_open * (1 + rule.take_profit)
            if low <= stop_price:
                return {"exit_trade_date": trade_date, "exit_price": stop_price, "exit_reason": "stop_loss"}
            if high >= take_profit_price:
                return {"exit_trade_date": trade_date, "exit_price": take_profit_price, "exit_reason": "take_profit"}
        return self.resolve_fixed_exit(
            row,
            ExitRule(name=rule.name, rule_type="fixed", max_hold_days=rule.max_hold_days, exit_price_field="close"),
        )

    def resolve_high_reversal_exit(self, row: object, rule: ExitRule) -> dict[str, object] | None:
        for offset in range(2, rule.max_hold_days + 1):
            trade_date = getattr(row, f"d{offset}_trade_date")
            high = getattr(row, f"d{offset}_high")
            close = getattr(row, f"d{offset}_close")
            if pd.isna(trade_date) or pd.isna(high) or pd.isna(close):
                return None
            high_return = high / row.buy_open - 1
            close_return = close / row.buy_open - 1
            if high_return >= rule.high_reversal_trigger and close_return <= rule.high_reversal_floor:
                return {"exit_trade_date": trade_date, "exit_price": close, "exit_reason": "high_reversal"}
        return self.resolve_fixed_exit(
            row,
            ExitRule(name=rule.name, rule_type="fixed", max_hold_days=rule.max_hold_days, exit_price_field="close"),
        )

    @property
    def fee_rate(self) -> float:
        return (
            self.commission_rate
            + self.transfer_fee_rate
            + self.commission_rate
            + self.transfer_fee_rate
            + self.stamp_tax_rate
            + self.slippage_rate * 2
        )
