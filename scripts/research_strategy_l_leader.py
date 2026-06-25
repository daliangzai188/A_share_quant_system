"""
研究 L 策略：真正龙头战法第一版。

本脚本只做离线研究，不接实盘，不修改 ABC/E2/D。

当前数据限制：
  - 历史明细里没有有效题材字段，theme_* 全部 unknown/NaN。
  - 因此第一版 L 只验证“市场空间龙头 / 分段龙头 / 连板梯队龙头”。
  - 后续若补齐题材归因，才能升级为“题材主线龙头”。

输出：
  reports/strategy_l/leader_strategy_summary.csv
  reports/strategy_l/leader_strategy_trades.csv
  reports/strategy_l/leader_strategy_yearly.csv
  reports/strategy_l/leader_strategy_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
SOURCE_PATH = PROJECT_ROOT / "reports" / "recent_2y_full_strategy_exclude_st_exclude_amount_ratio_top500_detail.csv"
THEME_FEATURE_PATH = PROJECT_ROOT / "data" / "processed" / "theme_heat_features.csv"
THEME_REFERENCE_PATH = PROJECT_ROOT / "data" / "processed" / "stock_theme_reference.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
INITIAL_EQUITY = 500_000.0


@dataclass(frozen=True)
class LeaderRule:
    name: str
    description: str
    predicate: Callable[[pd.DataFrame], pd.Series]
    sort_columns: list[str]
    ascending: list[bool]


def to_numeric(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    max_count = 0
    current = 0
    for value in returns.fillna(0.0):
        if float(value) < 0:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def load_source() -> pd.DataFrame:
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(f"找不到L策略研究数据源: {SOURCE_PATH}")
    data = pd.read_csv(SOURCE_PATH, low_memory=False)
    data["trade_date"] = data["trade_date"].astype(str)

    numeric_columns = [
        "limit_times",
        "market_leader_rank",
        "segment_market_leader_rank",
        "limit_height_rank",
        "segment_limit_height_rank",
        "first_time_minutes",
        "fd_amount_to_circ_mv",
        "turnover_rate",
        "amount",
        "circ_mv",
        "dynamic_account_return",
        "net_return",
    ]
    for column in numeric_columns:
        if column in data.columns:
            data[column] = to_numeric(data[column])

    if "dynamic_account_return" not in data.columns:
        data["dynamic_account_return"] = to_numeric(data.get("net_return", pd.Series(0.0, index=data.index))) * 0.8

    # 研究口径只使用可成交回放中真实形成收益的候选。
    if "scenario_executed" in data.columns:
        executed = data["scenario_executed"].fillna(False).astype(str).str.lower().isin({"true", "1"})
        data = data[executed].copy()
    elif "buy_executed" in data.columns:
        executed = data["buy_executed"].fillna(False).astype(str).str.lower().isin({"true", "1"})
        data = data[executed].copy()

    if "is_st" in data.columns:
        data = data[~data["is_st"].fillna(False).astype(str).str.lower().isin({"true", "1"})].copy()
    if "strategy_compatible" in data.columns:
        data = data[data["strategy_compatible"].fillna(True).astype(str).str.lower().isin({"true", "1"})].copy()

    data = attach_latest_theme_features(data)
    return data


def attach_latest_theme_features(data: pd.DataFrame) -> pd.DataFrame:
    """合并最新题材/行业热度特征，覆盖旧报告里的 unknown theme 字段。"""
    theme_columns = [
        "theme_data_available",
        "theme_source_column",
        "theme_name",
        "theme_limit_count",
        "theme_limit_height",
        "theme_chain_count",
        "theme_fd_amount_sum",
        "theme_open_times_sum",
        "theme_open_rate",
        "theme_heat_score",
        "theme_heat_rank",
        "theme_leader_rank",
        "theme_height_rank",
        "theme_is_mainline",
        "same_theme_limit_count",
    ]
    clean = data.drop(columns=[c for c in theme_columns if c in data.columns], errors="ignore")
    merged = clean.copy()

    if THEME_FEATURE_PATH.exists():
        theme = pd.read_csv(THEME_FEATURE_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        if not theme.empty:
            available_columns = ["trade_date", "ts_code", *[c for c in theme_columns if c in theme.columns]]
            merged = clean.merge(theme[available_columns], on=["trade_date", "ts_code"], how="left")
            merged = normalize_theme_columns(merged)
            if "theme_data_available" in merged.columns and merged["theme_data_available"].any():
                return merged

    return build_industry_theme_features(clean)


def normalize_theme_columns(data: pd.DataFrame) -> pd.DataFrame:
    merged = data.copy()
    for column in [
        "theme_limit_count",
        "theme_limit_height",
        "theme_chain_count",
        "theme_heat_score",
        "theme_heat_rank",
        "theme_leader_rank",
        "theme_height_rank",
        "same_theme_limit_count",
    ]:
        if column in merged.columns:
            merged[column] = to_numeric(merged[column])
    if "theme_data_available" in merged.columns:
        merged["theme_data_available"] = merged["theme_data_available"].map(parse_bool)
    if "theme_is_mainline" in merged.columns:
        merged["theme_is_mainline"] = merged["theme_is_mainline"].map(parse_bool)
    return merged


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def build_industry_theme_features(data: pd.DataFrame) -> pd.DataFrame:
    """用 stock_basic.industry 在研究源内部重算行业热度。"""
    if not THEME_REFERENCE_PATH.exists():
        return data
    ref = pd.read_csv(THEME_REFERENCE_PATH, dtype={"ts_code": str}, low_memory=False)
    if ref.empty or "theme_name" not in ref.columns:
        return data
    ref = ref[["ts_code", "theme_name"]].copy()
    ref["theme_name"] = ref["theme_name"].fillna("unknown").astype(str).str.strip()
    ref = ref.drop_duplicates("ts_code", keep="first")

    enriched = data.merge(ref, on="ts_code", how="left")
    enriched["theme_name"] = enriched["theme_name"].fillna("unknown")
    available = enriched["theme_name"].ne("unknown") & enriched["theme_name"].ne("")
    enriched["theme_data_available"] = available
    enriched["theme_source_column"] = "stock_basic.industry"

    calc = enriched[available].copy()
    if calc.empty:
        return enriched

    calc["limit_times_numeric"] = to_numeric(calc.get("limit_times", pd.Series(0, index=calc.index)))
    calc["fd_amount_numeric"] = to_numeric(calc.get("fd_amount", pd.Series(0, index=calc.index)))
    calc["open_times_numeric"] = to_numeric(calc.get("open_times", pd.Series(0, index=calc.index)))
    calc["first_time_minutes_for_rank"] = to_numeric(calc.get("first_time_minutes", pd.Series(9999, index=calc.index)), 9999)

    stats = (
        calc.groupby(["trade_date", "theme_name"], dropna=False)
        .agg(
            theme_limit_count=("ts_code", "count"),
            theme_limit_height=("limit_times_numeric", "max"),
            theme_chain_count=("limit_times_numeric", lambda series: int((series >= 2).sum())),
            theme_fd_amount_sum=("fd_amount_numeric", "sum"),
            theme_open_times_sum=("open_times_numeric", "sum"),
            theme_opened_count=("open_times_numeric", lambda series: int((series > 0).sum())),
        )
        .reset_index()
    )
    stats["theme_open_rate"] = stats["theme_opened_count"] / stats["theme_limit_count"].replace(0, pd.NA)
    stats["theme_heat_score"] = (
        stats["theme_limit_count"] * 3
        + stats["theme_limit_height"] * 2
        + stats["theme_chain_count"] * 2
        + stats["theme_fd_amount_sum"].rank(pct=True) * 2
        - stats["theme_open_rate"].fillna(0) * 2
    )
    stats["theme_heat_rank"] = stats.groupby("trade_date")["theme_heat_score"].rank(method="dense", ascending=False)
    stats["theme_is_mainline"] = stats["theme_heat_rank"] <= 3

    calc = calc.merge(stats, on=["trade_date", "theme_name"], how="left", validate="many_to_one")
    calc["theme_leader_rank"] = (
        calc.sort_values(
            ["trade_date", "theme_name", "limit_times_numeric", "first_time_minutes_for_rank", "open_times_numeric", "amount"],
            ascending=[True, True, False, True, False, False],
        )
        .groupby(["trade_date", "theme_name"], dropna=False)
        .cumcount()
        + 1
    )
    calc["theme_height_rank"] = calc.groupby(["trade_date", "theme_name"], dropna=False)["limit_times_numeric"].rank(
        method="dense",
        ascending=False,
    )
    calc["same_theme_limit_count"] = calc["theme_limit_count"]

    theme_cols = [
        "trade_date",
        "ts_code",
        "theme_limit_count",
        "theme_limit_height",
        "theme_chain_count",
        "theme_fd_amount_sum",
        "theme_open_times_sum",
        "theme_open_rate",
        "theme_heat_score",
        "theme_heat_rank",
        "theme_is_mainline",
        "theme_leader_rank",
        "theme_height_rank",
        "same_theme_limit_count",
    ]
    enriched = enriched.drop(columns=[c for c in theme_cols if c not in {"trade_date", "ts_code"} and c in enriched.columns], errors="ignore")
    enriched = enriched.merge(calc[theme_cols], on=["trade_date", "ts_code"], how="left")
    return normalize_theme_columns(enriched)


def theme_quality(data: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {
        "theme_columns_present": False,
        "theme_rows": 0,
        "theme_available_rows": 0,
        "theme_available_ratio": 0.0,
        "theme_source_columns": "",
        "theme_note": "当前数据没有有效题材字段，L第一版只能验证空间/分段/梯队龙头。",
    }
    theme_cols = [c for c in data.columns if c.startswith("theme_") or c in {"same_theme_limit_count"}]
    if not theme_cols:
        return result
    result["theme_columns_present"] = True
    result["theme_rows"] = int(len(data))
    if "theme_data_available" in data.columns:
        available = data["theme_data_available"].fillna(False).astype(bool)
    elif "theme_is_mainline_bucket" in data.columns:
        available = ~data["theme_is_mainline_bucket"].fillna("unknown").astype(str).eq("unknown")
    elif "theme_heat_score" in data.columns:
        available = data["theme_heat_score"].notna()
    else:
        available = pd.Series(False, index=data.index)
    result["theme_available_rows"] = int(available.sum())
    result["theme_available_ratio"] = float(available.mean()) if len(available) else 0.0
    if "theme_source_column" in data.columns:
        result["theme_source_columns"] = ",".join(sorted(data["theme_source_column"].dropna().astype(str).unique().tolist()))
    if result["theme_available_ratio"] > 0:
        result["theme_note"] = (
            "当前已用 stock_basic.industry 构建行业主线代理；"
            "这可以研究行业龙头，但还不是精细题材/概念龙头。"
        )
    return result


def build_rules() -> list[LeaderRule]:
    def market_top(n: int) -> Callable[[pd.DataFrame], pd.Series]:
        return lambda df: df["market_leader_rank"].between(1, n)

    def segment_top(n: int) -> Callable[[pd.DataFrame], pd.Series]:
        return lambda df: df["segment_market_leader_rank"].between(1, n)

    def limit_height_top(n: int) -> Callable[[pd.DataFrame], pd.Series]:
        return lambda df: df["limit_height_rank"].between(1, n)

    def theme_available(df: pd.DataFrame) -> pd.Series:
        if "theme_data_available" not in df.columns:
            return pd.Series(False, index=df.index)
        return df["theme_data_available"].fillna(False).astype(bool)

    def theme_mainline(df: pd.DataFrame) -> pd.Series:
        if "theme_is_mainline" not in df.columns:
            return pd.Series(False, index=df.index)
        return theme_available(df) & df["theme_is_mainline"].fillna(False).astype(bool)

    def theme_rank_top(column: str, n: int) -> Callable[[pd.DataFrame], pd.Series]:
        return lambda df: theme_available(df) & df[column].between(1, n)

    base_sort = [
        "limit_times",
        "market_leader_rank",
        "segment_market_leader_rank",
        "first_time_minutes",
        "fd_amount_to_circ_mv",
    ]
    base_ascending = [False, True, True, True, False]
    theme_sort = [
        "theme_heat_rank",
        "theme_leader_rank",
        "theme_height_rank",
        "limit_times",
        "first_time_minutes",
        "fd_amount_to_circ_mv",
    ]
    theme_ascending = [True, True, True, False, True, False]

    return [
        LeaderRule(
            "L_theme_mainline_leader",
            "行业主线龙头：行业热度排名<=3，且个股为行业内龙头排序第1。",
            lambda df: theme_mainline(df) & theme_rank_top("theme_leader_rank", 1)(df),
            theme_sort,
            theme_ascending,
        ),
        LeaderRule(
            "L_theme_mainline_height_leader",
            "行业主线高度龙头：行业热度排名<=3，且个股为行业内高度排名第1。",
            lambda df: theme_mainline(df) & theme_rank_top("theme_height_rank", 1)(df),
            theme_sort,
            theme_ascending,
        ),
        LeaderRule(
            "L_theme_top5_leader",
            "行业热度Top5龙头：行业热度排名<=5，且行业内龙头排序第1。",
            lambda df: theme_available(df) & df["theme_heat_rank"].between(1, 5) & df["theme_leader_rank"].between(1, 1),
            theme_sort,
            theme_ascending,
        ),
        LeaderRule(
            "L_theme_chain_leader",
            "行业连板龙头：行业内至少2只连板股，且个股为行业内龙头排序第1。",
            lambda df: theme_available(df) & (df["theme_chain_count"] >= 2) & df["theme_leader_rank"].between(1, 1),
            theme_sort,
            theme_ascending,
        ),
        LeaderRule(
            "L_market_space_top3",
            "市场空间龙头Top3：全市场龙头排名<=3，优先连板高度、市场排名、分段排名、涨停时间。",
            market_top(3),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_market_space_top5",
            "市场空间龙头Top5：全市场龙头排名<=5。",
            market_top(5),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_segment_leader_top1",
            "分段唯一龙头：所属市场分段龙头排名=1。",
            segment_top(1),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_segment_leader_top2",
            "分段龙头Top2：所属市场分段龙头排名<=2。",
            segment_top(2),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_dual_leader_m5_s1",
            "双重龙头：全市场龙头排名<=5且分段排名=1。",
            lambda df: market_top(5)(df) & segment_top(1)(df),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_dual_leader_m10_s2",
            "双重龙头宽口径：全市场龙头排名<=10且分段排名<=2。",
            lambda df: market_top(10)(df) & segment_top(2)(df),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_height_rank_top1",
            "连板高度排名第1：优先市场最高板/空间板。",
            limit_height_top(1),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_high_board_dual",
            "高位双龙头：连板数>=2、全市场龙头排名<=10、分段排名<=2。",
            lambda df: (df["limit_times"] >= 2) & market_top(10)(df) & segment_top(2)(df),
            base_sort,
            base_ascending,
        ),
        LeaderRule(
            "L_first_board_segment_leader",
            "首板分段龙头：首板中分段排名=1，偏新主线启动观察。",
            lambda df: (df["limit_times"] == 1) & segment_top(1)(df),
            base_sort,
            base_ascending,
        ),
    ]


def pick_daily(rule: LeaderRule, data: pd.DataFrame) -> pd.DataFrame:
    filtered = data[rule.predicate(data)].copy()
    if filtered.empty:
        return filtered

    sort_columns = [c for c in rule.sort_columns if c in filtered.columns]
    ascending = [rule.ascending[rule.sort_columns.index(c)] for c in sort_columns]
    filtered = filtered.sort_values(["trade_date", *sort_columns], ascending=[True, *ascending])
    picked = filtered.groupby("trade_date", as_index=False).head(1).copy()
    picked["l_rule"] = rule.name
    picked["l_rule_description"] = rule.description
    picked["l_account_return"] = to_numeric(picked["dynamic_account_return"])
    return picked


def evaluate(rule: LeaderRule, data: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    trades = pick_daily(rule, data)
    if trades.empty:
        return {
            "rule": rule.name,
            "description": rule.description,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
            "start_date": "",
            "end_date": "",
        }, trades

    returns = trades["l_account_return"].fillna(0.0)
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    trades["l_equity"] = equity
    return {
        "rule": rule.name,
        "description": rule.description,
        "trade_count": int(len(trades)),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY),
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": max_consecutive_losses(returns),
        "start_date": str(trades["trade_date"].min()),
        "end_date": str(trades["trade_date"].max()),
    }, trades


def build_yearly(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for (rule, year), group in trades.groupby(["l_rule", trades["trade_date"].astype(str).str[:4]]):
        returns = group["l_account_return"].fillna(0.0)
        rows.append({
            "rule": rule,
            "year": year,
            "trade_count": int(len(group)),
            "win_rate": float((returns > 0).mean()),
            "avg_account_return": float(returns.mean()),
            "median_account_return": float(returns.median()),
            "year_return": float((1.0 + returns).prod() - 1.0),
            "max_loss": float(returns.min()),
        })
    return pd.DataFrame(rows)


def markdown_table(data: pd.DataFrame) -> str:
    """不依赖 tabulate 的简易 Markdown 表格。"""
    if data.empty:
        return "无数据。"
    text = data.copy()
    for column in text.columns:
        if pd.api.types.is_float_dtype(text[column]):
            text[column] = text[column].map(lambda value: f"{float(value):.6g}" if pd.notna(value) else "")
        else:
            text[column] = text[column].fillna("").astype(str)
    headers = list(text.columns)
    rows = text.astype(str).values.tolist()
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, trades: pd.DataFrame, theme_info: dict[str, Any]) -> None:
    best = summary.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, False]).head(5)
    lines = [
        "# L 龙头策略第一版研究报告",
        "",
        "## 数据质量",
        "",
        f"- 数据源：`{SOURCE_PATH.relative_to(PROJECT_ROOT)}`",
        f"- 题材特征：`{THEME_FEATURE_PATH.relative_to(PROJECT_ROOT)}`",
        f"- 题材字段存在：{theme_info['theme_columns_present']}",
        f"- 题材来源：{theme_info.get('theme_source_columns', '')}",
        f"- 题材可用行数：{theme_info['theme_available_rows']} / {theme_info['theme_rows']} ({theme_info['theme_available_ratio']:.2%})",
        f"- 结论：{theme_info['theme_note']}",
        "",
        "## 当前L策略定义",
        "",
        "L第一版不接实盘，当前验证四类龙头代理：",
        "",
        "1. 市场空间龙头：`market_leader_rank` 靠前。",
        "2. 分段龙头：`segment_market_leader_rank` 靠前。",
        "3. 连板高度龙头：`limit_height_rank` 靠前。",
        "4. 行业主线龙头：`theme_heat_rank/theme_leader_rank/theme_height_rank` 靠前。",
        "",
        "若题材特征来自 stock_basic.industry，则只能代表行业主线，仍不是精细概念题材。",
        "",
        "## 最优候选",
        "",
        markdown_table(best),
        "",
        "## 风险判断",
        "",
        "- 若样本数过少，不能接实盘。",
        "- 若最大回撤明显大于当前 ABC/E2/D 组合，需要继续过滤。",
        "- 若收益主要来自少数极端单笔，需要拆解最大盈利和最大亏损。",
        "- 下一步必须补齐题材/概念归因，否则仍不是完整龙头战法。",
        "",
    ]
    if not trades.empty:
        cols = [
            "trade_date",
            "ts_code",
            "name",
            "l_rule",
            "limit_times",
            "market_leader_rank",
            "segment_market_leader_rank",
            "limit_height_rank",
            "market_segment",
            "l_account_return",
        ]
        available = [c for c in cols if c in trades.columns]
        lines.extend([
            "## 交易样例",
            "",
            markdown_table(trades.sort_values("l_account_return", ascending=False).head(20)[available]),
            "",
        ])
    (OUTPUT_DIR / "leader_strategy_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_source()
    theme_info = theme_quality(data)
    rules = build_rules()

    summaries: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    for rule in rules:
        row, trades = evaluate(rule, data)
        summaries.append(row)
        if not trades.empty:
            trade_frames.append(trades)

    summary = pd.DataFrame(summaries).sort_values("equity_multiple", ascending=False)
    all_trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    yearly = build_yearly(all_trades)

    summary.to_csv(OUTPUT_DIR / "leader_strategy_summary.csv", index=False, encoding="utf-8-sig")
    all_trades.to_csv(OUTPUT_DIR / "leader_strategy_trades.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "leader_strategy_yearly.csv", index=False, encoding="utf-8-sig")
    write_report(summary, all_trades, theme_info)

    print("L策略研究完成")
    print(f"数据源: {SOURCE_PATH}")
    print(f"题材可用率: {theme_info['theme_available_ratio']:.2%}")
    print(summary.head(10).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
