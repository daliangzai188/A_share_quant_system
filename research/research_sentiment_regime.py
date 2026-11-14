"""情绪周期 × 策略收益 研究脚本。

目的：把每笔历史成交，按"入场当天的市场情绪阶段"分桶，统计 A/B/C、D、E2 三策略
在不同情绪阶段下的胜率与平均收益，用数据回答"哪个阶段该重仓、哪个阶段该空仓"。

情绪阶段（regime）定义：
- 用全历史每日"涨停家数"（市场赚钱效应/广度的最稳健代理）按分位数分 4 档：
  冰点(<=25%) / 温和(25-50%) / 活跃(50-75%) / 高潮(>=75%)
- 另用每日"最高连板高度"做辅助二维观察。

数据来源：
- 每日情绪：data/raw/limit_list/*.csv（limit=='U' 计数 + limit_times 最大值），全历史
- 各策略逐笔：见 STRATEGY_CONFIGS

注意：这是研究/统计脚本，只读不下单。结论需结合样本量看（D/E2 样本较小）。
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
LIMIT_DIR = PROJECT_ROOT / "data" / "raw" / "limit_list"
OUT_DIR = PROJECT_ROOT / "reports" / "strategy_sentiment_regime"
SENTIMENT_CACHE = OUT_DIR / "daily_sentiment.csv"

# 各策略逐笔成交文件配置：(策略名, 文件, 日期列, 收益列, 是否剔除0收益的无交易日)
STRATEGY_CONFIGS = [
    (
        "A+B+C",
        "reports/paper_trade/ab_filtered/"
        "a_strict_plus_b0018_filtered_2y_full_20240520_20260514_1000d_a_plus_b_filtered_detail.csv",
        "signal_date", "account_return", True,
    ),
    (
        "D",
        "reports/strategy_d/d_trades.csv",
        "signal_date", "net_return", False,
    ),
    (
        "E2",
        "reports/strategy_expansion/abcd_expansion_selected_e2_trades.csv",
        "trade_date", "net_return", False,
    ),
]

REGIME_ORDER = ["冰点", "温和", "活跃", "高潮"]


def build_daily_sentiment(use_cache: bool = True) -> pd.DataFrame:
    """从全历史涨停池构建每日情绪指标：涨停家数 limit_up_count、最高连板 max_height。"""
    if use_cache and SENTIMENT_CACHE.exists():
        return pd.read_csv(SENTIMENT_CACHE)

    rows = []
    files = sorted(glob.glob(str(LIMIT_DIR / "*.csv")))
    for fp in files:
        try:
            df = pd.read_csv(fp, low_memory=False)
        except Exception:
            continue
        if "limit" not in df.columns:
            continue
        up = df[df["limit"].astype(str).str.upper() == "U"]
        if up.empty:
            continue
        date = int(up["trade_date"].iloc[0]) if "trade_date" in up.columns else int(Path(fp).stem)
        height = int(up["limit_times"].max()) if "limit_times" in up.columns else 0
        rows.append({"trade_date": date, "limit_up_count": len(up), "max_height": height})

    sent = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sent.to_csv(SENTIMENT_CACHE, index=False, encoding="utf-8-sig")
    return sent


def assign_regime(sent: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """按涨停家数分位数给每天打情绪标签。返回(带regime的df, 分位阈值)。"""
    q = sent["limit_up_count"].quantile([0.25, 0.50, 0.75]).to_dict()
    q25, q50, q75 = q[0.25], q[0.50], q[0.75]

    def label(c: float) -> str:
        if c <= q25:
            return "冰点"
        if c <= q50:
            return "温和"
        if c <= q75:
            return "活跃"
        return "高潮"

    sent = sent.copy()
    sent["regime"] = sent["limit_up_count"].apply(label)
    return sent, {"q25": q25, "q50": q50, "q75": q75}


def load_strategy_trades(name: str, rel_path: str, date_col: str,
                         ret_col: str, drop_zero: bool) -> pd.DataFrame | None:
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        print(f"⚠️  {name} 成交文件不存在，跳过：{rel_path}")
        return None
    df = pd.read_csv(path, low_memory=False)
    if date_col not in df.columns or ret_col not in df.columns:
        print(f"⚠️  {name} 缺少列 {date_col}/{ret_col}，跳过")
        return None
    out = pd.DataFrame({
        "trade_date": pd.to_numeric(df[date_col], errors="coerce"),
        "ret": pd.to_numeric(df[ret_col], errors="coerce"),
    }).dropna()
    out["trade_date"] = out["trade_date"].astype(int)
    if drop_zero:
        out = out[out["ret"] != 0.0]  # 剔除无候选/未开仓的0收益日
    out["strategy"] = name
    return out


def summarize(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    g = trades.groupby(by, observed=True)["ret"]
    res = pd.DataFrame({
        "笔数": g.size(),
        "胜率%": (trades.assign(win=trades["ret"] > 0).groupby(by, observed=True)["win"].mean() * 100).round(1),
        "平均收益%": (g.mean() * 100).round(2),
        "中位收益%": (g.median() * 100).round(2),
        "累计收益%": (g.sum() * 100).round(1),
    })
    return res


def main() -> None:
    print("构建每日情绪指标（涨停家数 / 最高连板）...")
    sent = build_daily_sentiment()
    sent, thr = assign_regime(sent)
    print(f"情绪分档阈值（涨停家数）：冰点<= {thr['q25']:.0f} < 温和<= {thr['q50']:.0f} "
          f"< 活跃<= {thr['q75']:.0f} < 高潮")
    print(f"情绪数据覆盖：{sent['trade_date'].min()} ~ {sent['trade_date'].max()}，共{len(sent)}个交易日\n")

    regime_map = dict(zip(sent["trade_date"], sent["regime"]))
    height_map = dict(zip(sent["trade_date"], sent["max_height"]))

    all_trades = []
    for name, path, dcol, rcol, drop0 in STRATEGY_CONFIGS:
        t = load_strategy_trades(name, path, dcol, rcol, drop0)
        if t is None or t.empty:
            continue
        t["regime"] = t["trade_date"].map(regime_map)
        t["max_height"] = t["trade_date"].map(height_map)
        missing = t["regime"].isna().sum()
        if missing:
            print(f"⚠️  {name} 有 {missing} 笔成交日无情绪数据（已剔除）")
        t = t.dropna(subset=["regime"])
        all_trades.append(t)

    if not all_trades:
        print("无可用成交数据。")
        return

    trades = pd.concat(all_trades, ignore_index=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print("【一】各策略 × 情绪阶段（按涨停家数分档）")
    print("=" * 70)
    out_rows = []
    for name in [c[0] for c in STRATEGY_CONFIGS]:
        sub = trades[trades["strategy"] == name]
        if sub.empty:
            continue
        res = summarize(sub, "regime").reindex(REGIME_ORDER).dropna(how="all")
        print(f"\n◆ {name}（共{len(sub)}笔）")
        print(res.to_string())
        r = res.reset_index().rename(columns={"index": "regime"})
        r.insert(0, "strategy", name)
        out_rows.append(r)

    print("\n" + "=" * 70)
    print("【二】全策略合计 × 情绪阶段")
    print("=" * 70)
    print(summarize(trades, "regime").reindex(REGIME_ORDER).dropna(how="all").to_string())

    print("\n" + "=" * 70)
    print("【三】各策略 × 最高连板高度（辅助视角）")
    print("=" * 70)
    trades["height_bucket"] = pd.cut(
        trades["max_height"], bins=[-1, 2, 4, 99], labels=["低(<=2板)", "中(3-4板)", "高(>=5板)"]
    )
    for name in [c[0] for c in STRATEGY_CONFIGS]:
        sub = trades[trades["strategy"] == name]
        if sub.empty:
            continue
        print(f"\n◆ {name}")
        print(summarize(sub, "height_bucket").to_string())

    if out_rows:
        out_df = pd.concat(out_rows, ignore_index=True)
        out_path = OUT_DIR / "strategy_by_regime.csv"
        out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\n✅ 明细已保存：{out_path}")

    print("\n提示：D/E2 样本量小，结论参考性弱；A+B+C 样本较多更可信。"
          "重仓/空仓决策务必再做样本外验证，避免过拟合。")


if __name__ == "__main__":
    main()
