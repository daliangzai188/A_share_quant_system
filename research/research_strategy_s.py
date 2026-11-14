"""
策略S研究脚本 - SZ/创业板动量补充策略（V2）

策略定位：
  - 独立于ABCDE2涨停板机制之外
  - 仅限深圳股票（.SZ：深主板 sz_main + 创业板 chi_next）
  - 在ABCDE2无可用SZ候选时，作为补充信号
  - 基于3大假设体系，穷举参数找最优配置

假设体系：
  [H1] 近涨停动量：今日涨幅5-9.9% + 放量，次日惯性延续
  [H2] 趋势突破：今日收盘突破N日新高 + 放量，次日延续
  [H3] 强势回踩：近15日累计涨幅>15%，今日回调2-6% + 缩量，次日反弹

执行模型：
  - 仓位：80%
  - 买入：T+1开盘 + 0.1%滑点
  - 卖出：T+2收盘 - 0.1%滑点
  - 费用：佣金0.03%双边 + 印花税0.1%卖出
  - 每次持仓1只，不并发（正确的"卖出后才能开新仓"逻辑）

回测窗口：
  - 主窗口：20240612 ~ 20260617（recent_2y，与ABCD对齐）
  - 热身期：从20231101加载（用于滚动指标计算）

输出：
  reports/strategy_s/
    s_search_summary.csv
    s_search_yearly.csv
    s_best_audit.md
    s_best_trades.csv
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np

DAILY_DIR = PROJECT_ROOT / "data" / "raw" / "daily"
BASIC_DIR = PROJECT_ROOT / "data" / "raw" / "daily_basic"
LIMIT_DIR = PROJECT_ROOT / "data" / "raw" / "limit_list"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_s"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.8
BUY_SLIPPAGE = 0.001
SELL_SLIPPAGE = 0.001
COMMISSION = 0.0003
STAMP_TAX = 0.001

WARMUP_START = "20231101"
TEST_START = "20240612"
TEST_END = "20260617"


# ── 交易日工具 ────────────────────────────────────────────────────────────────

def load_trade_calendar() -> list[str]:
    cal = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    if "is_open" in cal.columns:
        cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True", "true"})]
    return sorted(cal["cal_date"].astype(str).tolist())


def next_trade_day(date_str: str, calendar: list[str], n: int = 1) -> str | None:
    future = [d for d in calendar if d > date_str]
    return future[n - 1] if len(future) >= n else None


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_all_daily(start: str, end: str) -> pd.DataFrame:
    files = sorted(DAILY_DIR.glob("*.csv"))
    dfs = []
    for f in files:
        date = f.stem
        if date < start or date > end:
            continue
        try:
            df = pd.read_csv(f, dtype={"ts_code": str, "trade_date": str})
            dfs.append(df)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True)


def load_all_basic(start: str, end: str) -> pd.DataFrame:
    files = sorted(BASIC_DIR.glob("*.csv"))
    dfs = []
    for f in files:
        date = f.stem
        if date < start or date > end:
            continue
        try:
            df = pd.read_csv(
                f,
                dtype={"ts_code": str, "trade_date": str},
                usecols=["ts_code", "trade_date", "turnover_rate", "circ_mv"],
            )
            dfs.append(df)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True)


def load_all_limitup(start: str, end: str) -> dict[str, set[str]]:
    files = sorted(LIMIT_DIR.glob("*.csv"))
    result: dict[str, set[str]] = {}
    for f in files:
        date = f.stem
        if date < start or date > end:
            continue
        try:
            df = pd.read_csv(f, dtype={"ts_code": str})
            if "limit" in df.columns:
                up_codes = set(df.loc[df["limit"] == "U", "ts_code"])
            else:
                up_codes = set(df["ts_code"])
            result[date] = up_codes
        except Exception:
            result[date] = set()
    return result


# ── 滚动特征计算 ──────────────────────────────────────────────────────────────

def compute_rolling_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    为每只股票计算：
    - vol_ma5: 5日平均成交量（前N日）
    - vol_ma20: 20日平均成交量（前N日）
    - volume_ratio5: 今日量比（vs 5日均量）
    - high20: 过去20日最高收盘价
    - high60: 过去60日最高收盘价
    - cum_return15: 过去15日累计涨幅
    - pct_chg_ma5: 过去5日平均涨幅
    """
    df = df.sort_values(["ts_code", "trade_date"]).copy()

    def rolling_on_prev(series, window, func="mean"):
        if func == "mean":
            return series.shift(1).rolling(window, min_periods=1).mean()
        elif func == "max":
            return series.shift(1).rolling(window, min_periods=1).max()

    df["vol_ma5"] = df.groupby("ts_code")["vol"].transform(
        lambda x: rolling_on_prev(x, 5)
    )
    df["vol_ma20"] = df.groupby("ts_code")["vol"].transform(
        lambda x: rolling_on_prev(x, 20)
    )
    df["volume_ratio5"] = (df["vol"] / df["vol_ma5"].replace(0, np.nan)).clip(0, 30)

    df["high20"] = df.groupby("ts_code")["close"].transform(
        lambda x: rolling_on_prev(x, 20, "max")
    )
    df["high60"] = df.groupby("ts_code")["close"].transform(
        lambda x: rolling_on_prev(x, 60, "max")
    )

    # 过去15日累计涨幅：close(today) / close(15天前) - 1
    df["close_15d_ago"] = df.groupby("ts_code")["close"].transform(
        lambda x: x.shift(15)
    )
    df["cum_return15"] = (df["close"] / df["close_15d_ago"].replace(0, np.nan) - 1) * 100

    # 近5日平均涨幅（衡量最近势头）
    df["pct_chg_ma5"] = df.groupby("ts_code")["pct_chg"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )

    return df


# ── 候选生成 ──────────────────────────────────────────────────────────────────

def classify_segment(ts_code: str) -> str:
    if not ts_code.endswith(".SZ"):
        return "other"
    prefix = ts_code[:3]
    if prefix in ("300", "301"):
        return "chi_next"
    return "sz_main"


# 全局市场情绪（每日涨停数量）
def build_market_sentiment(limitup_by_date: dict) -> dict[str, int]:
    return {d: len(codes) for d, codes in limitup_by_date.items()}


def build_all_candidates(
    daily_feat: pd.DataFrame,
    basic_df: pd.DataFrame,
    limitup_by_date: dict,
    market_sentiment: dict,
    calendar: list[str],
) -> pd.DataFrame:
    """
    对 TEST_START~TEST_END 窗口内每个交易日，计算所有SZ股票的特征，
    返回完整候选池（未过滤条件，让回测时过滤）。
    """
    # 只取SZ股票：新账号当前只允许深主板 + 创业板。
    df = daily_feat[daily_feat["ts_code"].str.endswith(".SZ")].copy()
    prefix = df["ts_code"].str.slice(0, 3)
    df["segment"] = np.where(prefix.isin(["300", "301"]), "chi_next", "sz_main")

    # 合并 daily_basic
    df = df.merge(basic_df, on=["ts_code", "trade_date"], how="left")

    # 只取测试窗口
    df = df[(df["trade_date"] >= TEST_START) & (df["trade_date"] <= TEST_END)].copy()

    # 标记今日是否涨停：用集合查询替代逐行 DataFrame.apply。
    limit_pairs = {
        (trade_date, ts_code)
        for trade_date, codes in limitup_by_date.items()
        for ts_code in codes
    }
    df["is_limitup"] = [
        pair in limit_pairs
        for pair in zip(df["trade_date"].astype(str), df["ts_code"].astype(str))
    ]

    # 标记市场情绪（今日涨停总数）
    df["market_limit_count"] = df["trade_date"].map(market_sentiment).fillna(0).astype(int)

    # 生成信号日、买入日、卖出日：预先建映射，避免每行扫描交易日历。
    buy_date_map = {d: calendar[i + 1] for i, d in enumerate(calendar[:-1])}
    sell_date_map = {d: calendar[i + 2] for i, d in enumerate(calendar[:-2])}
    df["signal_date"] = df["trade_date"]
    df["buy_date"] = df["signal_date"].map(buy_date_map)
    df["sell_date"] = df["signal_date"].map(sell_date_map)

    return df


# ── 回测核心 ──────────────────────────────────────────────────────────────────

def net_return_calc(buy_price: float, sell_price: float) -> float:
    actual_buy = buy_price * (1 + BUY_SLIPPAGE)
    actual_sell = sell_price * (1 - SELL_SLIPPAGE)
    gross = (actual_sell - actual_buy) / actual_buy
    cost = COMMISSION * 2 + STAMP_TAX
    return gross - cost


def apply_conditions(pool: pd.DataFrame, params: dict) -> pd.DataFrame:
    """对候选池施加策略条件过滤。"""
    hypothesis = params["hypothesis"]
    universe = params.get("universe", "all_sz")
    amt_min = params.get("amount_min_yi", 2.0) * 10000  # 亿→万元
    mv_max = params.get("circ_mv_max_yi", 200.0) * 10000
    mv_min = params.get("circ_mv_min_yi", 0.0) * 10000
    mkt_filter = params.get("market_min_limit_count", 0)

    df = pool.copy()

    # 宇宙过滤
    if universe == "chi_next":
        df = df[df["segment"] == "chi_next"]
    elif universe == "sz_main":
        df = df[df["segment"] == "sz_main"]

    # 排除今日涨停（已被ABCDE2覆盖）
    df = df[~df["is_limitup"]]

    # 市值过滤
    df = df[(df["circ_mv"] >= mv_min) & (df["circ_mv"] <= mv_max)]

    # 成交额过滤
    df = df[df["amount"] >= amt_min]

    # 市场情绪过滤
    if mkt_filter > 0:
        df = df[df["market_limit_count"] >= mkt_filter]

    # 换手率
    tr_min = params.get("turnover_min", 0.0)
    if tr_min > 0:
        df = df[df["turnover_rate"] >= tr_min]

    # 阳线过滤（可选）
    if params.get("require_bullish", False):
        df = df[df["close"] >= df["open"]]

    if hypothesis == "H1":
        # [H1] 近涨停动量：涨幅5-9.9% + 量比>=vr_min
        pct_lo = params.get("pct_lo", 5.0)
        pct_hi = params.get("pct_hi", 9.9)
        vr_min = params.get("vol_ratio_min", 2.0)
        df = df[
            (df["pct_chg"] >= pct_lo) &
            (df["pct_chg"] <= pct_hi) &
            (df["volume_ratio5"] >= vr_min)
        ]

    elif hypothesis == "H4":
        # [H4] 组合：近涨停 + 突破N日新高（双重确认）
        pct_lo = params.get("pct_lo", 6.0)
        pct_hi = params.get("pct_hi", 9.9)
        vr_min = params.get("vol_ratio_min", 2.0)
        bw = params.get("breakout_window", 20)
        high_col = "high20" if bw == 20 else "high60"
        df = df[
            (df["pct_chg"] >= pct_lo) &
            (df["pct_chg"] <= pct_hi) &
            (df["volume_ratio5"] >= vr_min) &
            (df["close"] > df[high_col])
        ]

    elif hypothesis == "H2":
        # [H2] 趋势突破：收盘突破N日新高 + 量比>=vr_min + 今日涨幅>pct_lo
        breakout_window = params.get("breakout_window", 20)
        vr_min = params.get("vol_ratio_min", 1.5)
        pct_lo = params.get("pct_lo", 2.0)
        if breakout_window == 20:
            df = df[df["close"] > df["high20"]]
        else:
            df = df[df["close"] > df["high60"]]
        df = df[
            (df["volume_ratio5"] >= vr_min) &
            (df["pct_chg"] >= pct_lo)
        ]

    elif hypothesis == "H3":
        # [H3] 强势回踩：近15日涨幅>15%，今日回调2-6% + 量比<=1.0（缩量）
        cum_min = params.get("cum_return_min", 15.0)
        pullback_lo = params.get("pullback_lo", 2.0)
        pullback_hi = params.get("pullback_hi", 6.0)
        vr_max = params.get("vol_ratio_max", 1.0)
        df = df[
            (df["cum_return15"] >= cum_min) &
            (df["pct_chg"] <= -pullback_lo) &
            (df["pct_chg"] >= -pullback_hi) &
            (df["volume_ratio5"] <= vr_max)
        ]

    return df


def simulate(
    pool: pd.DataFrame,
    price_lookup: dict,
    params: dict,
) -> dict:
    """
    正确的回测模拟：
    - 按信号日顺序遍历
    - 持有期间（T+1 ~ T+2）不开新仓
    - 仓位80%，T+1开盘买，T+2收盘卖
    """
    sort_by = params.get("sort_by", "circ_mv_asc")
    equity = INITIAL_EQUITY
    trades = []
    position_sell_date = None  # 当前持仓的卖出日

    candidates = apply_conditions(pool, params)
    if candidates.empty:
        return {"trades": [], "final_equity": equity}

    all_signal_dates = sorted(candidates["signal_date"].unique())

    for sig_date in all_signal_dates:
        # 持仓中则跳过（卖出日之前的信号日不开新仓）
        if position_sell_date is not None and sig_date < position_sell_date:
            continue

        day_cands = candidates[candidates["signal_date"] == sig_date].copy()
        if day_cands.empty:
            continue

        # 候选排序
        if sort_by == "circ_mv_asc":
            day_cands = day_cands.sort_values("circ_mv", ascending=True)
        elif sort_by == "vol_ratio_desc":
            day_cands = day_cands.sort_values("volume_ratio5", ascending=False)
        elif sort_by == "turnover_desc":
            day_cands = day_cands.sort_values("turnover_rate", ascending=False)
        elif sort_by == "pct_chg_desc":
            day_cands = day_cands.sort_values("pct_chg", ascending=False)

        executed = False
        for _, row in day_cands.iterrows():
            buy_date = row.get("buy_date")
            sell_date = row.get("sell_date")
            if not buy_date or not sell_date or pd.isna(buy_date) or pd.isna(sell_date):
                continue

            buy_key = (row["ts_code"], buy_date)
            sell_key = (row["ts_code"], sell_date)
            if buy_key not in price_lookup or sell_key not in price_lookup:
                continue

            buy_open, _ = price_lookup[buy_key]
            _, sell_close = price_lookup[sell_key]
            if buy_open <= 0 or sell_close <= 0:
                continue

            ret = net_return_calc(buy_open, sell_close)
            account_return = POSITION_PCT * ret
            equity_before = equity
            equity *= (1 + account_return)

            trades.append({
                "signal_date": sig_date,
                "buy_date": buy_date,
                "sell_date": sell_date,
                "ts_code": row["ts_code"],
                "segment": row.get("segment", ""),
                "pct_chg": round(row.get("pct_chg", 0), 4),
                "volume_ratio5": round(row.get("volume_ratio5", 0), 2),
                "turnover_rate": round(row.get("turnover_rate", 0), 4),
                "circ_mv_yi": round(row.get("circ_mv", 0) / 10000, 2),
                "cum_return15": round(row.get("cum_return15", 0), 2),
                "market_limit_count": row.get("market_limit_count", 0),
                "buy_price": buy_open,
                "sell_price": sell_close,
                "net_return": ret,
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
                "is_win": ret > 0,
                "year": str(sig_date)[:4],
            })
            position_sell_date = sell_date
            executed = True
            break  # 每日只取第1只

        if not executed:
            pass  # 本日无法执行则跳过

    return {"trades": trades, "final_equity": equity}


def build_price_lookup(daily_all: pd.DataFrame) -> dict[tuple[str, str], tuple[float, float]]:
    df = daily_all[["ts_code", "trade_date", "open", "close"]].dropna()
    return {
        (ts_code, trade_date): (open_price, close_price)
        for ts_code, trade_date, open_price, close_price
        in zip(df["ts_code"], df["trade_date"], df["open"], df["close"])
    }


def compute_max_drawdown(equity_series: list[float]) -> float:
    if not equity_series:
        return 0.0
    peak = equity_series[0]
    max_dd = 0.0
    for v in equity_series:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def summarize_result(result: dict, params: dict) -> dict:
    trades = result["trades"]
    final_eq = result["final_equity"]
    base = {k: v for k, v in params.items()}
    if not trades:
        return {**base, "trade_count": 0, "equity_multiple": 0.0,
                "win_rate": 0.0, "avg_net_return": 0.0, "max_drawdown": 0.0}
    df = pd.DataFrame(trades)
    return {
        **base,
        "trade_count": len(trades),
        "final_equity": round(final_eq, 2),
        "equity_multiple": round(final_eq / INITIAL_EQUITY, 4),
        "win_rate": round(df["is_win"].mean(), 4),
        "avg_net_return": round(df["net_return"].mean(), 6),
        "avg_account_return": round(df["account_return"].mean(), 6),
        "max_profit": round(df["net_return"].max(), 4),
        "max_loss": round(df["net_return"].min(), 4),
        "max_drawdown": round(compute_max_drawdown(df["equity_after"].tolist()), 4),
    }


def yearly_breakdown(result: dict, params: dict) -> list[dict]:
    trades = result["trades"]
    if not trades:
        return []
    df = pd.DataFrame(trades)
    rows = []
    eq_by_year = {}
    for year, group in df.groupby("year"):
        first_eq = group["equity_before"].iloc[0]
        last_eq = group["equity_after"].iloc[-1]
        rows.append({
            **{k: v for k, v in params.items()},
            "year": year,
            "trade_count": len(group),
            "first_equity": round(first_eq, 2),
            "last_equity": round(last_eq, 2),
            "period_return": round((last_eq - first_eq) / first_eq, 4),
            "win_rate": round(group["is_win"].mean(), 4),
            "avg_net_return": round(group["net_return"].mean(), 6),
        })
    return rows


def audit_best(best_params: dict, best_result: dict) -> None:
    trades_df = pd.DataFrame(best_result["trades"])
    trades_df.to_csv(OUTPUT_DIR / "s_best_trades.csv", index=False, encoding="utf-8-sig")

    summary = summarize_result(best_result, best_params)
    yearly = pd.DataFrame(yearly_breakdown(best_result, best_params))

    seg_breakdown = (
        trades_df.groupby("segment")
        .agg(
            count=("ts_code", "count"),
            avg_net_return=("net_return", "mean"),
            win_rate=("is_win", "mean"),
        )
        .reset_index()
    )

    def fmt_table(df: pd.DataFrame) -> str:
        lines = [" | ".join(df.columns)]
        lines.append(" | ".join(["---"] * len(df.columns)))
        for _, row in df.iterrows():
            lines.append(" | ".join(str(v) for v in row))
        return "\n".join(lines)

    with open(OUTPUT_DIR / "s_best_audit.md", "w", encoding="utf-8") as f:
        f.write("# 策略S 最优配置审计报告\n\n")
        f.write("## 策略参数\n\n")
        for k, v in best_params.items():
            f.write(f"- **{k}**: {v}\n")
        f.write("\n## 全区间结果\n\n")
        f.write(f"- 初始资金：{INITIAL_EQUITY:,.0f}\n")
        f.write(f"- 最终资金：{summary.get('final_equity', 0):,.2f}\n")
        f.write(f"- **资金倍数：{summary.get('equity_multiple', 0):.2f}x**\n")
        f.write(f"- 成交笔数：{summary.get('trade_count', 0)}\n")
        f.write(f"- 胜率：{summary.get('win_rate', 0):.2%}\n")
        f.write(f"- 平均单笔净收益率：{summary.get('avg_net_return', 0):.2%}\n")
        f.write(f"- 平均账户收益率：{summary.get('avg_account_return', 0):.2%}\n")
        f.write(f"- 最大单笔盈利：{summary.get('max_profit', 0):.2%}\n")
        f.write(f"- 最大单笔亏损：{summary.get('max_loss', 0):.2%}\n")
        f.write(f"- 最大回撤：{summary.get('max_drawdown', 0):.2%}\n")
        if not yearly.empty:
            f.write("\n## 年度分析\n\n")
            f.write(fmt_table(yearly[["year","trade_count","period_return","win_rate","avg_net_return"]]))
        f.write("\n\n## 板块分布\n\n")
        f.write(fmt_table(seg_breakdown))
        f.write("\n\n## 样本交易明细（前20笔）\n\n")
        cols = ["signal_date","ts_code","segment","pct_chg","volume_ratio5","turnover_rate",
                "circ_mv_yi","market_limit_count","buy_price","sell_price",
                "net_return","account_return","equity_after"]
        f.write(fmt_table(trades_df[cols].head(20)))

    print(f"\n[最优配置] {best_params}")
    print(f"[结果] {summary.get('equity_multiple', 0):.2f}x "
          f"| {summary.get('trade_count', 0)}笔 "
          f"| {summary.get('win_rate', 0):.1%}胜率 "
          f"| 均值={summary.get('avg_net_return', 0):.2%} "
          f"| 最大回撤={summary.get('max_drawdown', 0):.2%}")
    if not yearly.empty:
        print(yearly[["year","trade_count","period_return","win_rate"]].to_string(index=False))


# ── 参数网格 ──────────────────────────────────────────────────────────────────

def build_param_grid(quick: bool, focused: bool = False) -> list[dict]:
    """构建参数网格，覆盖3个假设体系。"""
    grid = []

    if focused:
        # 围绕 quick 搜索最强方向继续细化：
        # 创业板、非涨停、强势动量、T+1开盘买、T+2收盘卖。
        for pct_lo in [5.5, 6.0, 6.5, 7.0]:
            for vr_min in [1.8, 2.0, 2.2, 2.5]:
                for tr_min in [3.0, 5.0, 8.0]:
                    for amt in [1.0, 2.0]:
                        for mv_max in [50.0, 80.0, 100.0]:
                            for sort in ["circ_mv_asc", "vol_ratio_desc"]:
                                for mkt in [20, 30, 50, 80]:
                                    grid.append({
                                        "hypothesis": "H1",
                                        "pct_lo": pct_lo,
                                        "pct_hi": 9.9,
                                        "vol_ratio_min": vr_min,
                                        "turnover_min": tr_min,
                                        "amount_min_yi": amt,
                                        "circ_mv_max_yi": mv_max,
                                        "universe": "chi_next",
                                        "sort_by": sort,
                                        "market_min_limit_count": mkt,
                                        "require_bullish": False,
                                    })
        return grid

    # [H1] 近涨停动量
    for pct_lo in ([5.0, 6.0, 7.0, 8.0] if not quick else [5.0, 6.0]):
        for pct_hi in [9.9]:
            for vr_min in ([1.5, 2.0, 2.5, 3.0] if not quick else [2.0, 2.5]):
                for tr_min in ([3.0, 5.0, 8.0] if not quick else [5.0]):
                    for amt in [2.0]:
                        for mv_max in ([50.0, 100.0, 200.0] if not quick else [100.0, 200.0]):
                            for univ in (["all_sz", "chi_next"] if not quick else ["all_sz", "chi_next"]):
                                for sort in (["circ_mv_asc","vol_ratio_desc","turnover_desc","pct_chg_desc"] if not quick else ["circ_mv_asc","vol_ratio_desc"]):
                                    for mkt in ([0, 30, 50, 80] if not quick else [0, 30]):
                                        for require_bullish in ([False, True] if not quick else [False]):
                                            grid.append({
                                                "hypothesis": "H1",
                                                "pct_lo": pct_lo,
                                                "pct_hi": pct_hi,
                                                "vol_ratio_min": vr_min,
                                                "turnover_min": tr_min,
                                                "amount_min_yi": amt,
                                                "circ_mv_max_yi": mv_max,
                                                "universe": univ,
                                                "sort_by": sort,
                                                "market_min_limit_count": mkt,
                                                "require_bullish": require_bullish,
                                            })

    # [H2] 趋势突破
    for bw in ([20, 60] if not quick else [20, 60]):
        for vr_min in ([1.5, 2.0] if not quick else [1.5, 2.0]):
            for pct_lo in ([2.0, 3.0] if not quick else [2.0, 3.0]):
                for mv_max in ([100.0, 200.0] if not quick else [100.0, 200.0]):
                    for univ in (["all_sz", "chi_next"] if not quick else ["all_sz", "chi_next"]):
                        for sort in (["circ_mv_asc","vol_ratio_desc"] if not quick else ["circ_mv_asc","vol_ratio_desc"]):
                            for mkt in ([0, 30] if not quick else [0, 30]):
                                grid.append({
                                    "hypothesis": "H2",
                                    "breakout_window": bw,
                                    "vol_ratio_min": vr_min,
                                    "pct_lo": pct_lo,
                                    "amount_min_yi": 2.0,
                                    "circ_mv_max_yi": mv_max,
                                    "universe": univ,
                                    "sort_by": sort,
                                    "market_min_limit_count": mkt,
                                })

    # [H4] 组合条件：近涨停 + 突破N日新高
    for pct_lo in ([6.0, 7.0, 8.0] if not quick else [6.0]):
        for vr_min in ([2.0, 2.5, 3.0] if not quick else [2.0]):
            for bw in [20, 60]:
                for mv_max in ([50.0, 100.0, 200.0] if not quick else [100.0]):
                    for univ in (["all_sz", "chi_next"] if not quick else ["chi_next"]):
                        for sort in (["circ_mv_asc","vol_ratio_desc"] if not quick else ["circ_mv_asc"]):
                            for mkt in ([30, 50, 80] if not quick else [30]):
                                grid.append({
                                    "hypothesis": "H4",
                                    "pct_lo": pct_lo,
                                    "pct_hi": 9.9,
                                    "vol_ratio_min": vr_min,
                                    "breakout_window": bw,
                                    "turnover_min": 5.0,
                                    "amount_min_yi": 2.0,
                                    "circ_mv_max_yi": mv_max,
                                    "universe": univ,
                                    "sort_by": sort,
                                    "market_min_limit_count": mkt,
                                })

    # [H3] 强势回踩
    for cum_min in ([15.0, 25.0] if not quick else [15.0, 25.0]):
        for pb_lo in [2.0, 3.0]:
            for pb_hi in [5.0, 7.0]:
                if pb_lo >= pb_hi:
                    continue
                for vr_max in ([0.8, 1.0, 1.2] if not quick else [1.0, 1.2]):
                    for mv_max in ([100.0, 200.0] if not quick else [100.0, 200.0]):
                        for univ in (["all_sz", "chi_next"] if not quick else ["all_sz"]):
                            for sort in (["circ_mv_asc"] if not quick else ["circ_mv_asc"]):
                                for mkt in ([0, 30] if not quick else [0]):
                                    grid.append({
                                        "hypothesis": "H3",
                                        "cum_return_min": cum_min,
                                        "pullback_lo": pb_lo,
                                        "pullback_hi": pb_hi,
                                        "vol_ratio_max": vr_max,
                                        "amount_min_yi": 2.0,
                                        "circ_mv_max_yi": mv_max,
                                        "universe": univ,
                                        "sort_by": sort,
                                        "market_min_limit_count": mkt,
                                    })

    return grid


# ── 风险覆盖验证 ──────────────────────────────────────────────────────────────

def add_trade_days(date_str: str, calendar: list[str], n: int) -> str:
    future = [d for d in calendar if d > date_str]
    return future[n - 1] if len(future) >= n else "99999999"


def max_consecutive_losses(returns: list[float]) -> int:
    current = 0
    best = 0
    for value in returns:
        if value <= 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def profit_factor(returns: pd.Series) -> float:
    wins = float(returns[returns > 0].sum())
    losses = float(-returns[returns < 0].sum())
    return wins / losses if losses > 0 else float("inf")


def simulate_risk_overlay(trades: pd.DataFrame, calendar: list[str], config: dict) -> tuple[dict, pd.DataFrame]:
    equity = INITIAL_EQUITY
    peak = INITIAL_EQUITY
    loss_streak = 0
    pause_until = ""
    rows = []

    base_position_pct = float(config.get("base_position_pct", POSITION_PCT))
    reduced_position_pct = float(config.get("reduced_position_pct", base_position_pct))
    drawdown_reduce_threshold = float(config.get("drawdown_reduce_threshold", -1.0))
    drawdown_pause_threshold = float(config.get("drawdown_pause_threshold", -1.0))
    drawdown_pause_days = int(config.get("drawdown_pause_days", 0))
    loss_streak_pause = int(config.get("loss_streak_pause", 0))
    loss_pause_days = int(config.get("loss_pause_days", 0))

    for _, trade in trades.sort_values("signal_date").iterrows():
        signal_date = str(trade["signal_date"])
        sell_date = str(trade.get("sell_date", signal_date))
        current_drawdown = equity / peak - 1 if peak > 0 else 0.0

        if pause_until and signal_date <= pause_until:
            rows.append({
                **trade.to_dict(),
                "executed": False,
                "skip_reason": "PAUSED",
                "overlay_position_pct": 0.0,
                "overlay_account_return": 0.0,
                "overlay_equity_before": equity,
                "overlay_equity_after": equity,
                "overlay_drawdown_before": current_drawdown,
            })
            continue

        if drawdown_pause_threshold > -1.0 and current_drawdown <= -abs(drawdown_pause_threshold):
            pause_until = add_trade_days(signal_date, calendar, drawdown_pause_days)
            rows.append({
                **trade.to_dict(),
                "executed": False,
                "skip_reason": "DRAWDOWN_PAUSE",
                "overlay_position_pct": 0.0,
                "overlay_account_return": 0.0,
                "overlay_equity_before": equity,
                "overlay_equity_after": equity,
                "overlay_drawdown_before": current_drawdown,
            })
            continue

        position_pct = base_position_pct
        if drawdown_reduce_threshold > -1.0 and current_drawdown <= -abs(drawdown_reduce_threshold):
            position_pct = reduced_position_pct

        equity_before = equity
        net_return = float(trade["net_return"])
        account_return = position_pct * net_return
        equity *= 1 + account_return
        peak = max(peak, equity)

        if net_return <= 0:
            loss_streak += 1
            if loss_streak_pause > 0 and loss_streak >= loss_streak_pause:
                pause_until = add_trade_days(sell_date, calendar, loss_pause_days)
                loss_streak = 0
        else:
            loss_streak = 0

        rows.append({
            **trade.to_dict(),
            "executed": True,
            "skip_reason": "",
            "overlay_position_pct": position_pct,
            "overlay_account_return": account_return,
            "overlay_equity_before": equity_before,
            "overlay_equity_after": equity,
            "overlay_drawdown_before": current_drawdown,
        })

    detail = pd.DataFrame(rows)
    executed = detail[detail["executed"] == True].copy()  # noqa: E712
    if executed.empty:
        summary = {
            **config,
            "trade_count": 0,
            "skipped_count": int((detail["executed"] == False).sum()) if not detail.empty else 0,  # noqa: E712
            "equity_multiple": 1.0,
            "final_equity": INITIAL_EQUITY,
            "win_rate": 0.0,
            "avg_net_return": 0.0,
            "median_net_return": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "max_consecutive_losses": 0,
        }
        return summary, detail

    summary = {
        **config,
        "trade_count": int(len(executed)),
        "skipped_count": int((detail["executed"] == False).sum()),  # noqa: E712
        "equity_multiple": float(executed["overlay_equity_after"].iloc[-1] / INITIAL_EQUITY),
        "final_equity": float(executed["overlay_equity_after"].iloc[-1]),
        "win_rate": float((executed["net_return"] > 0).mean()),
        "avg_net_return": float(executed["net_return"].mean()),
        "median_net_return": float(executed["net_return"].median()),
        "max_profit": float(executed["net_return"].max()),
        "max_loss": float(executed["net_return"].min()),
        "max_drawdown": float(compute_max_drawdown(executed["overlay_equity_after"].tolist())),
        "profit_factor": float(profit_factor(executed["net_return"])),
        "max_consecutive_losses": int(max_consecutive_losses(executed["net_return"].tolist())),
    }
    return summary, detail


def risk_overlay_grid() -> list[dict]:
    return [
        {"overlay_name": "baseline", "base_position_pct": 0.8},
        {"overlay_name": "loss3_pause5", "base_position_pct": 0.8, "loss_streak_pause": 3, "loss_pause_days": 5},
        {"overlay_name": "loss3_pause10", "base_position_pct": 0.8, "loss_streak_pause": 3, "loss_pause_days": 10},
        {"overlay_name": "loss4_pause5", "base_position_pct": 0.8, "loss_streak_pause": 4, "loss_pause_days": 5},
        {"overlay_name": "dd10_half", "base_position_pct": 0.8, "drawdown_reduce_threshold": 0.10, "reduced_position_pct": 0.4},
        {"overlay_name": "dd15_half", "base_position_pct": 0.8, "drawdown_reduce_threshold": 0.15, "reduced_position_pct": 0.4},
        {"overlay_name": "dd15_pause10", "base_position_pct": 0.8, "drawdown_pause_threshold": 0.15, "drawdown_pause_days": 10},
        {"overlay_name": "loss3_pause5_dd10_half", "base_position_pct": 0.8, "loss_streak_pause": 3, "loss_pause_days": 5, "drawdown_reduce_threshold": 0.10, "reduced_position_pct": 0.4},
        {"overlay_name": "loss3_pause10_dd10_half", "base_position_pct": 0.8, "loss_streak_pause": 3, "loss_pause_days": 10, "drawdown_reduce_threshold": 0.10, "reduced_position_pct": 0.4},
        {"overlay_name": "loss4_pause5_dd15_half", "base_position_pct": 0.8, "loss_streak_pause": 4, "loss_pause_days": 5, "drawdown_reduce_threshold": 0.15, "reduced_position_pct": 0.4},
    ]


def build_overlay_yearly(detail: pd.DataFrame, overlay_name: str) -> pd.DataFrame:
    executed = detail[detail["executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["signal_date"].astype(str).str[:4]
    rows = []
    for year, group in executed.groupby("year"):
        rows.append({
            "overlay_name": overlay_name,
            "year": year,
            "trade_count": int(len(group)),
            "period_return": float(group["overlay_equity_after"].iloc[-1] / group["overlay_equity_before"].iloc[0] - 1),
            "win_rate": float((group["net_return"] > 0).mean()),
            "avg_net_return": float(group["net_return"].mean()),
            "max_drawdown": float(compute_max_drawdown(group["overlay_equity_after"].tolist())),
            "max_consecutive_losses": int(max_consecutive_losses(group["net_return"].tolist())),
        })
    return pd.DataFrame(rows)


def run_risk_overlay() -> None:
    trades_path = OUTPUT_DIR / "s_best_trades.csv"
    if not trades_path.exists():
        raise FileNotFoundError(f"缺少 {trades_path}，请先运行 --focused 生成 S 最优交易明细。")

    calendar = load_trade_calendar()
    trades = pd.read_csv(trades_path, dtype={"signal_date": str, "buy_date": str, "sell_date": str, "ts_code": str})
    summaries = []
    yearly_frames = []
    best_detail = None
    best_summary = None

    for config in risk_overlay_grid():
        summary, detail = simulate_risk_overlay(trades, calendar, config)
        summaries.append(summary)
        yearly = build_overlay_yearly(detail, str(config["overlay_name"]))
        if not yearly.empty:
            yearly_frames.append(yearly)
        if best_summary is None or (
            summary["max_drawdown"] > best_summary["max_drawdown"]
            and summary["equity_multiple"] >= 5
        ):
            best_summary = summary
            best_detail = detail

    summary_df = pd.DataFrame(summaries).sort_values(
        ["max_drawdown", "equity_multiple"],
        ascending=[False, False],
    )
    yearly_df = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()
    summary_path = OUTPUT_DIR / "s_risk_overlay_summary.csv"
    yearly_path = OUTPUT_DIR / "s_risk_overlay_yearly.csv"
    detail_path = OUTPUT_DIR / "s_risk_overlay_best_detail.csv"
    md_path = OUTPUT_DIR / "s_risk_overlay_report.md"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_df.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    if best_detail is not None:
        best_detail.to_csv(detail_path, index=False, encoding="utf-8-sig")

    best = summary_df.iloc[0]
    content = [
        "# 策略S 风险覆盖验证报告",
        "",
        "## 最优风险覆盖",
        "",
        f"- 规则：{best['overlay_name']}",
        f"- 资金倍数：{best['equity_multiple']:.2f}x",
        f"- 交易笔数：{int(best['trade_count'])}",
        f"- 跳过笔数：{int(best['skipped_count'])}",
        f"- 胜率：{best['win_rate']:.2%}",
        f"- 平均单笔净收益：{best['avg_net_return']:.2%}",
        f"- 中位数单笔净收益：{best['median_net_return']:.2%}",
        f"- 最大回撤：{best['max_drawdown']:.2%}",
        f"- 最大连续亏损：{int(best['max_consecutive_losses'])}",
        f"- 盈亏比：{best['profit_factor']:.2f}",
        "",
        "## 覆盖对比 Top",
        "",
        summary_df[["overlay_name", "equity_multiple", "trade_count", "skipped_count", "win_rate", "max_drawdown", "max_consecutive_losses"]].to_markdown(index=False),
    ]
    if not yearly_df.empty:
        content.extend([
            "",
            "## 年度表现",
            "",
            yearly_df.to_markdown(index=False),
        ])
    md_path.write_text("\n".join(content), encoding="utf-8")

    print("[风险覆盖] summary:", summary_path)
    print("[风险覆盖] yearly:", yearly_path)
    print("[风险覆盖] report:", md_path)
    print(summary_df[["overlay_name", "equity_multiple", "trade_count", "skipped_count", "win_rate", "max_drawdown", "max_consecutive_losses"]].to_string(index=False))


def predefined_2026_filters() -> list[tuple[str, Any]]:
    return [
        ("baseline", lambda data: pd.Series(True, index=data.index)),
        ("vr_lte_8", lambda data: data["volume_ratio5"] <= 8),
        ("mv_gte_5_vr_lte_8", lambda data: (data["circ_mv_yi"] >= 5) & (data["volume_ratio5"] <= 8)),
        ("turnover_gte_12_vr_lte_8", lambda data: (data["turnover_rate"] >= 12) & (data["volume_ratio5"] <= 8)),
        ("vr_3_5_to_8", lambda data: (data["volume_ratio5"] >= 3.5) & (data["volume_ratio5"] <= 8)),
    ]


def run_2026_filter_probe() -> None:
    trades_path = OUTPUT_DIR / "s_best_trades.csv"
    if not trades_path.exists():
        raise FileNotFoundError(f"缺少 {trades_path}，请先运行 --focused 生成 S 最优交易明细。")

    calendar = load_trade_calendar()
    trades = pd.read_csv(trades_path, dtype={"signal_date": str, "buy_date": str, "sell_date": str, "ts_code": str})
    rows = []
    yearly_frames = []
    detail_frames = []
    overlay_variants = [
        ("no_overlay", {}),
        ("loss4_pause5", {"loss_streak_pause": 4, "loss_pause_days": 5}),
        ("loss3_pause5", {"loss_streak_pause": 3, "loss_pause_days": 5}),
        ("loss3_pause10", {"loss_streak_pause": 3, "loss_pause_days": 10}),
    ]

    for filter_name, predicate in predefined_2026_filters():
        filtered = trades[predicate(trades)].copy()
        for overlay_name, overlay_config in overlay_variants:
            config = {"overlay_name": f"{filter_name}__{overlay_name}", "base_position_pct": POSITION_PCT, **overlay_config}
            summary, detail = simulate_risk_overlay(filtered, calendar, config)
            rows.append(summary)
            yearly = build_overlay_yearly(detail, str(config["overlay_name"]))
            if not yearly.empty:
                yearly_frames.append(yearly)
            if config["overlay_name"] == "vr_3_5_to_8__loss3_pause5":
                detail_frames.append(detail.assign(probe_name=config["overlay_name"]))

    summary_df = pd.DataFrame(rows)
    yearly_df = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()
    if not yearly_df.empty:
        year2026 = yearly_df[yearly_df["year"].astype(str) == "2026"][
            ["overlay_name", "period_return", "trade_count", "win_rate", "max_drawdown"]
        ].rename(
            columns={
                "period_return": "return_2026",
                "trade_count": "trade_count_2026",
                "win_rate": "win_rate_2026",
                "max_drawdown": "max_drawdown_2026",
            }
        )
        summary_df = summary_df.merge(year2026, on="overlay_name", how="left")
    summary_df = summary_df.sort_values(
        ["return_2026", "equity_multiple"],
        ascending=[False, False],
    )

    summary_path = OUTPUT_DIR / "s_2026_filter_probe_summary.csv"
    yearly_path = OUTPUT_DIR / "s_2026_filter_probe_yearly.csv"
    detail_path = OUTPUT_DIR / "s_2026_filter_probe_best_detail.csv"
    md_path = OUTPUT_DIR / "s_2026_filter_probe_report.md"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_df.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    if detail_frames:
        pd.concat(detail_frames, ignore_index=True).to_csv(detail_path, index=False, encoding="utf-8-sig")

    selected = summary_df[summary_df["overlay_name"].eq("vr_3_5_to_8__loss3_pause5")]
    best = selected.iloc[0] if not selected.empty else summary_df.iloc[0]
    content = [
        "# 策略S 2026失效过滤探针",
        "",
        "## 推荐观察规则",
        "",
        "- 在原始S条件上增加：`3.5 <= volume_ratio5 <= 8`",
        "- 叠加风控：连续3笔亏损后暂停5个交易日",
        "",
        f"- 规则：{best['overlay_name']}",
        f"- 全区间资金倍数：{best['equity_multiple']:.2f}x",
        f"- 全区间最大回撤：{best['max_drawdown']:.2%}",
        f"- 2026收益：{best.get('return_2026', 0):.2%}",
        f"- 2026交易笔数：{int(best.get('trade_count_2026', 0))}",
        f"- 2026最大回撤：{best.get('max_drawdown_2026', 0):.2%}",
        "",
        "## 对比结果",
        "",
        summary_df[["overlay_name", "equity_multiple", "trade_count", "skipped_count", "win_rate", "max_drawdown", "return_2026", "trade_count_2026", "max_drawdown_2026"]].to_markdown(index=False),
    ]
    md_path.write_text("\n".join(content), encoding="utf-8")

    print("[2026过滤探针] summary:", summary_path)
    print("[2026过滤探针] yearly:", yearly_path)
    print("[2026过滤探针] report:", md_path)
    print(summary_df[["overlay_name", "equity_multiple", "trade_count", "skipped_count", "max_drawdown", "return_2026", "trade_count_2026", "max_drawdown_2026"]].to_string(index=False))


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="只跑核心参数组合")
    parser.add_argument("--focused", action="store_true", help="围绕quick最优方向做细化搜索")
    parser.add_argument("--risk-overlay", action="store_true", help="基于S最优交易明细验证风控覆盖")
    parser.add_argument("--probe-2026-filter", action="store_true", help="验证S策略2026失效过滤条件")
    args = parser.parse_args()

    if args.risk_overlay:
        run_risk_overlay()
        return
    if args.probe_2026_filter:
        run_2026_filter_probe()
        return

    calendar = load_trade_calendar()
    print(f"[数据] 加载交易日历，共 {len(calendar)} 个交易日")

    print(f"[数据] 加载日线数据 {WARMUP_START}~{TEST_END} ...")
    daily_all = load_all_daily(WARMUP_START, TEST_END)
    print(f"[数据] 日线行数: {len(daily_all):,}")

    print(f"[数据] 计算滚动特征（量比/新高/累计涨幅）...")
    daily_feat = compute_rolling_features(daily_all)

    print(f"[数据] 加载 daily_basic {WARMUP_START}~{TEST_END} ...")
    basic_df = load_all_basic(WARMUP_START, TEST_END)

    print(f"[数据] 加载涨停列表...")
    limitup_by_date = load_all_limitup(WARMUP_START, TEST_END)
    market_sentiment = build_market_sentiment(limitup_by_date)

    print(f"[数据] 构建候选池 ...")
    pool = build_all_candidates(daily_feat, basic_df, limitup_by_date, market_sentiment, calendar)
    print(f"[数据] 候选池行数（测试窗口SZ股票）: {len(pool):,}")
    if args.focused:
        before = len(pool)
        pool = pool[
            (pool["segment"] == "chi_next") &
            (~pool["is_limitup"]) &
            (pool["pct_chg"] >= 5.5) &
            (pool["pct_chg"] <= 9.9) &
            (pool["volume_ratio5"] >= 1.8) &
            (pool["turnover_rate"] >= 3.0) &
            (pool["amount"] >= 1.0 * 10000) &
            (pool["circ_mv"] <= 100.0 * 10000) &
            (pool["market_limit_count"] >= 20)
        ].copy()
        print(f"[数据] focused预过滤候选池: {before:,} → {len(pool):,}")

    print(f"[数据] 构建价格查找表...")
    price_lookup = build_price_lookup(daily_all)

    param_grid = build_param_grid(quick=args.quick, focused=args.focused)
    print(f"\n[搜索] 参数组合数: {len(param_grid)}")
    verbose_each = len(param_grid) <= 300

    summary_rows = []
    yearly_rows = []
    best_multiple = 0.0
    best_params = None
    best_result = None

    for i, params in enumerate(param_grid, 1):
        result = simulate(pool, price_lookup, params)
        s = summarize_result(result, params)
        yr = yearly_breakdown(result, params)
        summary_rows.append(s)
        yearly_rows.extend(yr)

        multiple = s["equity_multiple"]
        n_trades = s["trade_count"]
        wr = s["win_rate"]
        avg = s["avg_net_return"]
        hyp = params["hypothesis"]
        if multiple > best_multiple:
            best_multiple = multiple
            best_params = params
            best_result = result
            print(
                f"  [NEW BEST {i:4d}/{len(param_grid)}] {multiple:.2f}x "
                f"({n_trades}笔 {wr:.1%}胜 avg={avg:.2%}) params={params}"
            )
        elif verbose_each or i % 100 == 0:
            label = f"[{hyp}] " + " ".join(f"{k}={v}" for k, v in list(params.items())[1:6])
            print(f"  [{i:4d}/{len(param_grid)}] {label} → {multiple:.1f}x ({n_trades}笔 {wr:.0%}胜 avg={avg:.2%})")

    if not summary_rows:
        print("[ERROR] 没有任何有效结果")
        return

    summary_df = pd.DataFrame(summary_rows).sort_values("equity_multiple", ascending=False)
    yearly_df = pd.DataFrame(yearly_rows)

    summary_df.to_csv(OUTPUT_DIR / "s_search_summary.csv", index=False, encoding="utf-8-sig")
    yearly_df.to_csv(OUTPUT_DIR / "s_search_yearly.csv", index=False, encoding="utf-8-sig")

    print(f"\n[TOP 10结果]")
    show_cols = ["hypothesis","equity_multiple","trade_count","win_rate",
                 "avg_net_return","max_drawdown"]
    # Add key params to display
    show_df = summary_df.head(10).copy()
    for col in show_cols:
        if col not in show_df.columns:
            show_df[col] = ""
    print(show_df[show_cols].to_string(index=False))

    if best_params and best_result:
        audit_best(best_params, best_result)

    print(f"\n[完成] 报告已写入 {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
