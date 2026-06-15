"""
最近2年固定滑点全策略穷举优化

设计目标：
- 只用最近2年数据统计（默认 start_date 到 end_date）
- 固定 0.1% 买入 + 0.1% 卖出滑点（研究口径，与 A5 原版一致）
- 穷举条件组合（单因子 → 双因子 → 三因子）× 多种排序规则
- 目标：找到复利倍数大于 target_equity_multiple 的最佳策略组合
- 执行口径：T+1 开盘买入，T+2 收盘卖出，80% 仓位，单持仓

注意：
- 固定 0.1% 滑点是研究口径上限，不代表实盘一定可执行
- 最终结论必须结合真实流动性、样本量、过拟合风险综合判断
- net_return 字段已包含固定滑点和费用，直接用于模拟
"""
from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.search_realistic_condition_strategy import RealisticConditionStrategySearch
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="最近2年固定滑点全策略穷举优化。")
    parser.add_argument("--config", default="config/config.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    outputs = Recent2YFreshOptimizer(config_path=args.config).optimize()
    print("最近2年固定滑点全策略穷举优化完成：")
    for name, path in outputs.items():
        print(f"  {name}: {path}")


class Recent2YFreshOptimizer:
    """
    最近2年固定滑点穷举策略优化器。

    使用 net_return 字段（已含固定 0.1% 买入+卖出滑点及所有费用）直接做
    单持仓、T+2 收盘卖出的复利仿真，搜索最优条件组合 × 排序规则。
    """

    # 参与搜索的因子列
    FACTOR_COLS = [
        "market_segment",
        "limit_pct_bucket",
        "market_sentiment_level",
        "segment_market_sentiment_level",
        "market_emotion_state_bucket",
        "segment_emotion_state_bucket",
        "market_chain_count_bucket",
        "segment_chain_count_bucket",
        "market_limit_down_count_bucket",
        "segment_limit_down_count_bucket",
        "segment_limit_down_ratio_bucket",
        "segment_limit_max_height_bucket",
        "market_leader_rank_bucket",
        "segment_market_leader_rank_bucket",
        "limit_height_rank_bucket",
        "segment_limit_height_rank_bucket",
        "first_time_bucket",
        "first_time_detail_bucket",
        "limit_times_bucket",
        "limit_times_detail_bucket",
        "open_times_bucket",
        "amount_bucket",
        "turnover_rate_bucket",
        "volume_ratio_bucket",
        "fd_ratio_bucket",
        "pct_chg_bucket",
        "limit_up_count_bucket",
        "segment_limit_up_count_bucket",
        "segment_limit_up_ratio_bucket",
        "retreat_state_bucket",
        "segment_retreat_state_bucket",
        "board_type",
    ]

    # 候选排序规则：(sort_col, ascending, label)
    SORT_RULES = [
        ("fill_probability", False, "fill_prob_desc"),
        ("amount", False, "amount_desc"),
        ("amount", True, "amount_asc"),
        ("turnover_rate", False, "turnover_desc"),
        ("turnover_rate", True, "turnover_asc"),
        ("volume_ratio", False, "vr_desc"),
        ("volume_ratio", True, "vr_asc"),
        ("circ_mv", True, "circ_mv_asc"),
        ("circ_mv", False, "circ_mv_desc"),
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.logger = get_logger("recent_2y_fresh_optimizer")
        self.project_root = PROJECT_ROOT

        cfg = self.config.get("recent_2y_fresh_optimization", {})
        self.start_date = str(cfg.get("start_date", "20240520"))
        self.end_date = str(cfg.get("end_date", "20260518"))
        self.initial_cash = float(cfg.get("initial_cash", 500000))
        self.position_pct = float(cfg.get("position_pct", 0.8))
        self.target_equity_multiple = float(cfg.get("target_equity_multiple", 50.0))
        self.min_trades = int(cfg.get("min_trades", 20))
        self.min_signals = int(cfg.get("min_signals", 15))
        self.max_signal_ratio = float(cfg.get("max_signal_ratio", 0.85))
        self.top_single = int(cfg.get("top_single", 60))
        self.top_pair = int(cfg.get("top_pair", 80))
        self.top_triple = int(cfg.get("top_triple", 60))
        self.max_detail_rows = int(cfg.get("max_detail_rows", 50))

        self.output_summary_path = PROJECT_ROOT / cfg.get(
            "output_summary_path", "reports/recent_2y_fresh_summary.csv"
        )
        self.output_yearly_path = PROJECT_ROOT / cfg.get(
            "output_yearly_path", "reports/recent_2y_fresh_yearly.csv"
        )
        self.output_detail_path = PROJECT_ROOT / cfg.get(
            "output_detail_path", "reports/recent_2y_fresh_detail.csv"
        )

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_recent_candidates(self) -> pd.DataFrame:
        searcher = RealisticConditionStrategySearch(config_path=self.config_path)
        trades = searcher.load_candidates()
        trades["trade_date"] = trades["trade_date"].astype(str)
        trades = trades[
            (trades["trade_date"] >= self.start_date) & (trades["trade_date"] <= self.end_date)
        ].copy()
        if trades.empty:
            raise RuntimeError(f"近2年候选池为空，日期范围 {self.start_date}-{self.end_date}")
        self.logger.info("近2年候选样本: %s 条，交易日: %s 天", len(trades), trades["trade_date"].nunique())
        return trades

    # ------------------------------------------------------------------
    # 仿真核心（固定滑点）
    # ------------------------------------------------------------------

    def simulate(
        self,
        df: pd.DataFrame,
        sort_col: str,
        sort_asc: bool,
    ) -> dict[str, Any]:
        """
        固定滑点单持仓仿真。
        net_return 已包含 0.1% 买入 + 0.1% 卖出滑点和所有费用。
        """
        if df.empty:
            return {}
        if sort_col not in df.columns:
            sort_col = "fill_probability"
        daily = (
            df.sort_values(["trade_date", sort_col], ascending=[True, sort_asc])
            .groupby("trade_date", as_index=False)
            .head(1)
            .reset_index(drop=True)
        )
        equity = self.initial_cash
        occupied_until = ""
        trade_rows: list[dict] = []
        for _, row in daily.iterrows():
            buy_date = str(row.get("next_trade_date", ""))
            if occupied_until and buy_date <= occupied_until:
                continue
            nr = row.get("net_return", None)
            if pd.isna(nr):
                continue
            nr = float(nr)
            acct_ret = nr * self.position_pct
            equity_before = equity
            equity = equity * (1 + acct_ret)
            occupied_until = str(row.get("exit_trade_date", ""))
            trade_rows.append(
                {
                    "year": row["trade_date"][:4],
                    "trade_date": row["trade_date"],
                    "ts_code": row.get("ts_code", ""),
                    "name": row.get("name", ""),
                    "net_return": nr,
                    "account_return": acct_ret,
                    "equity_before": equity_before,
                    "equity_after": equity,
                }
            )

        if not trade_rows:
            return {}

        trades_df = pd.DataFrame(trade_rows)
        n = len(trades_df)
        wins = (trades_df["net_return"] > 0).sum()
        trades_df["peak"] = trades_df["equity_after"].cummax()
        trades_df["drawdown"] = trades_df["equity_after"] / trades_df["peak"] - 1
        max_dd = float(trades_df["drawdown"].min())
        max_consec_loss = self._max_consecutive_loss(trades_df["net_return"].tolist())

        year_returns: dict[str, float] = {}
        prev_eq = self.initial_cash
        for yr, grp in trades_df.groupby("year"):
            yr_eq = grp["equity_after"].iloc[-1]
            year_returns[str(yr)] = yr_eq / prev_eq - 1
            prev_eq = yr_eq

        return {
            "equity_multiple": equity / self.initial_cash,
            "final_equity": equity,
            "executed_trades": n,
            "win_rate": wins / n,
            "avg_net_return": float(trades_df["net_return"].mean()),
            "median_net_return": float(trades_df["net_return"].median()),
            "max_single_win": float(trades_df["net_return"].max()),
            "max_single_loss": float(trades_df["net_return"].min()),
            "max_drawdown": max_dd,
            "max_consecutive_losses": max_consec_loss,
            "min_year_return": min(year_returns.values()) if year_returns else 0.0,
            "hit_target": equity / self.initial_cash >= self.target_equity_multiple,
            **{f"return_{yr}": ret for yr, ret in year_returns.items()},
            "_trades_df": trades_df,
        }

    @staticmethod
    def _max_consecutive_loss(returns: list[float]) -> int:
        max_cl = 0
        cur = 0
        for r in returns:
            if r <= 0:
                cur += 1
                max_cl = max(max_cl, cur)
            else:
                cur = 0
        return max_cl

    # ------------------------------------------------------------------
    # 条件搜索
    # ------------------------------------------------------------------

    def build_condition_candidates(self, df: pd.DataFrame) -> list[tuple[str, str]]:
        total = len(df)
        pairs: list[tuple[str, str]] = []
        for col in self.FACTOR_COLS:
            if col not in df.columns:
                continue
            for val in df[col].dropna().unique():
                if str(val) in ("missing", "nan", "None", "unknown"):
                    continue
                cnt = (df[col] == val).sum()
                if cnt < self.min_signals or cnt > total * self.max_signal_ratio:
                    continue
                pairs.append((col, str(val)))
        return pairs

    def apply_conditions(
        self, df: pd.DataFrame, conds: tuple[tuple[str, str], ...]
    ) -> pd.DataFrame:
        mask = pd.Series(True, index=df.index)
        for col, val in conds:
            if col in df.columns:
                mask &= df[col].astype(str) == val
        return df[mask].copy()

    def evaluate_one(
        self,
        df: pd.DataFrame,
        conds: tuple[tuple[str, str], ...],
    ) -> list[dict[str, Any]]:
        """对一个条件组合，跑所有排序规则，返回所有结果行。"""
        sub = self.apply_conditions(df, conds)
        if len(sub) < self.min_signals:
            return []
        rows: list[dict[str, Any]] = []
        for sort_col, sort_asc, sort_label in self.SORT_RULES:
            if sort_col not in sub.columns:
                continue
            result = self.simulate(sub, sort_col, sort_asc)
            if not result:
                continue
            if result["executed_trades"] < self.min_trades:
                continue
            cond_str = ";".join(f"{c}={v}" for c, v in conds)
            rows.append(
                {
                    "conditions": cond_str,
                    "condition_count": len(conds),
                    "sort_rule": sort_label,
                    "sort_col": sort_col,
                    "sort_ascending": sort_asc,
                    "signal_count": len(sub),
                    **{k: v for k, v in result.items() if k != "_trades_df"},
                }
            )
        return rows

    def optimize(self) -> dict[str, Path]:
        df = self.load_recent_candidates()
        cond_pairs = self.build_condition_candidates(df)
        self.logger.info("有效条件-值对数: %s", len(cond_pairs))

        all_results: list[dict[str, Any]] = []
        detail_map: dict[str, pd.DataFrame] = {}

        # ---------- 单因子 ----------
        self.logger.info("搜索单因子条件 (%s 个)...", len(cond_pairs))
        single_results: list[tuple[tuple, float]] = []
        for cp in cond_pairs:
            rows = self.evaluate_one(df, (cp,))
            all_results.extend(rows)
            if rows:
                best_eq = max(r["equity_multiple"] for r in rows)
                single_results.append(((cp,), best_eq))

        single_results.sort(key=lambda x: x[1], reverse=True)
        top_single = [item[0][0] for item in single_results[: self.top_single]]
        self.logger.info("单因子搜索完成，保留 top%s 用于双因子", len(top_single))

        # ---------- 双因子 ----------
        pair_combos = [
            (c1, c2)
            for c1, c2 in combinations(top_single, 2)
            if c1[0] != c2[0]
        ]
        self.logger.info("搜索双因子条件 (%s 组合)...", len(pair_combos))
        pair_results: list[tuple[tuple, float]] = []
        for i, (c1, c2) in enumerate(pair_combos, 1):
            rows = self.evaluate_one(df, (c1, c2))
            all_results.extend(rows)
            if rows:
                best_eq = max(r["equity_multiple"] for r in rows)
                pair_results.append(((c1, c2), best_eq))
            if i % 500 == 0:
                self.logger.info("双因子进度: %s/%s", i, len(pair_combos))

        pair_results.sort(key=lambda x: x[1], reverse=True)
        top_pairs = [item[0] for item in pair_results[: self.top_pair]]
        self.logger.info("双因子搜索完成，保留 top%s 用于三因子", len(top_pairs))

        # ---------- 三因子（在最优双因子上扩展单因子）----------
        triple_set: set[tuple] = set()
        triple_combos: list[tuple] = []
        for base_pair in top_pairs:
            used_factors = {c[0] for c in base_pair}
            for cp in cond_pairs:
                if cp[0] in used_factors:
                    continue
                triple = tuple(sorted(list(base_pair) + [cp], key=lambda x: x[0]))
                if triple not in triple_set:
                    triple_set.add(triple)
                    triple_combos.append(triple)

        # 限制总量避免过慢
        triple_combos = triple_combos[: self.top_triple * len(self.SORT_RULES) * 2]
        self.logger.info("搜索三因子条件 (%s 组合)...", len(triple_combos))
        for i, conds in enumerate(triple_combos, 1):
            rows = self.evaluate_one(df, conds)
            all_results.extend(rows)
            if i % 1000 == 0:
                self.logger.info("三因子进度: %s/%s", i, len(triple_combos))

        self.logger.info("全部评估完成，总结果行: %s", len(all_results))

        # ---------- 汇总排序 ----------
        if not all_results:
            raise RuntimeError("没有找到任何有效结果，请检查数据或调整参数。")

        summary = pd.DataFrame(all_results).sort_values(
            ["hit_target", "equity_multiple", "max_drawdown"],
            ascending=[False, False, True],
        ).reset_index(drop=True)

        # ---------- 年度和明细（仅 top N 方案）----------
        top_scenarios = summary.head(self.max_detail_rows)[["conditions", "sort_rule"]].values.tolist()
        top_set = {(c, s) for c, s in top_scenarios}

        yearly_rows: list[dict] = []
        detail_frames: list[pd.DataFrame] = []

        for idx, row in summary.head(self.max_detail_rows).iterrows():
            cond_str = str(row["conditions"])
            sort_label = str(row["sort_rule"])
            sort_col = str(row["sort_col"])
            sort_asc = bool(row["sort_ascending"])
            conds = tuple(
                tuple(part.split("=", 1))
                for part in cond_str.split(";")
                if "=" in part
            )
            sub = self.apply_conditions(df, conds)  # type: ignore[arg-type]
            result = self.simulate(sub, sort_col, sort_asc)
            if not result or "_trades_df" not in result:
                continue
            trades_df: pd.DataFrame = result["_trades_df"]
            for yr, grp in trades_df.groupby("year"):
                yr_start_eq = trades_df[trades_df["year"] < str(yr)]["equity_after"]
                yr_start = float(yr_start_eq.iloc[-1]) if len(yr_start_eq) > 0 else self.initial_cash
                yr_equity = grp["equity_after"].iloc[-1]
                yearly_rows.append(
                    {
                        "scenario_rank": idx + 1,
                        "conditions": cond_str,
                        "sort_rule": sort_label,
                        "year": yr,
                        "year_return": yr_equity / yr_start - 1,
                        "year_trades": len(grp),
                        "year_win_rate": (grp["net_return"] > 0).mean(),
                        "year_end_equity": yr_equity,
                    }
                )
            detail = trades_df.copy()
            detail["scenario_rank"] = idx + 1
            detail["conditions"] = cond_str
            detail["sort_rule"] = sort_label
            detail_frames.append(detail)

        yearly_df = pd.DataFrame(yearly_rows)
        detail_df = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly_df.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail_df.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")

        # ---------- 控制台摘要 ----------
        hit = summary[summary["hit_target"] == True]  # noqa: E712
        self.logger.info(
            "达到 %.0f 倍目标的方案数: %s / %s",
            self.target_equity_multiple,
            len(hit),
            len(summary),
        )
        print(f"\n{'='*60}")
        print(f"近2年固定滑点穷举结果摘要 ({self.start_date}-{self.end_date})")
        print(f"目标: ≥{self.target_equity_multiple}倍复利 | 共评估: {len(summary)} 个方案")
        print(f"达到目标: {len(hit)} 个")
        print(f"{'='*60}")
        if not hit.empty:
            print("\n【达到目标的方案 TOP-10】")
            cols = ["conditions", "sort_rule", "equity_multiple", "executed_trades",
                    "win_rate", "max_drawdown", "max_consecutive_losses"]
            for c in [f"return_{y}" for y in ["2024", "2025", "2026"]]:
                if c in hit.columns:
                    cols.append(c)
            print(hit[cols].head(10).to_string(index=False))
        else:
            print("\n【最高复利方案 TOP-10（均未达到目标）】")
            cols = ["conditions", "sort_rule", "equity_multiple", "executed_trades",
                    "win_rate", "max_drawdown"]
            print(summary[cols].head(10).to_string(index=False))

        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }


if __name__ == "__main__":
    main()
