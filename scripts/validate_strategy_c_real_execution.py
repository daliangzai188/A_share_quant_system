"""
策略C 真实执行验证

验证口径：
1. 成交可行性：T+1 开盘是否为一字涨停（买不到）
2. 买入成本：基于实际资金规模占当日成交额的比例，动态估算真实滑点
3. 卖出成本：T+2 收盘是否跌停（卖不出），否则动态估算卖出滑点
4. 完整复利：从 50 万初始资金，80% 仓位，单持仓，T+2 收盘卖出
5. ST 股票：照常参与（用户已接受）

输出：
- 逐笔明细（含买入阻塞/卖出阻塞/真实滑点/真实净收益/账户权益）
- 年度汇总
- 与固定 0.1% 滑点的对比
- 流动性压力分析
"""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.optimize_recent_2y_fresh import Recent2YFreshOptimizer
from src.utils.config import load_json_config


# ── 配置 ───────────────────────────────────────────────────────────────
INITIAL_CASH      = 500_000          # 初始资金 50 万
POSITION_PCT      = 0.80             # 每笔目标仓位 80%
MAX_BUY_AMT_RATIO = 0.05             # 单笔买入最多占 T+1 成交额 5%（容量上限）
COMMISSION        = 0.0003           # 佣金（买卖各）
STAMP_TAX         = 0.001            # 印花税（卖出）
TRANSFER          = 0.00001          # 过户费（买卖各）
FEE_FIXED         = COMMISSION * 2 + TRANSFER * 2 + STAMP_TAX   # 0.00162

# 动态滑点估算表（成交额占比 → 滑点率）
SLIPPAGE_TIERS = [
    (0.005, 0.001),   # ≤0.5%：0.1%
    (0.010, 0.002),   # ≤1.0%：0.2%
    (0.020, 0.005),   # ≤2.0%：0.5%
    (0.050, 0.010),   # ≤5.0%：1.0%
    (0.100, 0.030),   # ≤10% ：3.0%
    (0.200, 0.050),   # ≤20% ：5.0%
    (1.000, 0.080),   # >20% ：8.0%
]


def est_slippage(amount_ratio: float) -> float:
    for threshold, slip in SLIPPAGE_TIERS:
        if amount_ratio <= threshold:
            return slip
    return SLIPPAGE_TIERS[-1][1]


def is_limit_up(pct: float, limit_pct: float) -> bool:
    """判断当天是否涨停（T+1 开盘涨停 = 一字板，买不到）。"""
    return pct >= limit_pct * 100 * 0.99


def is_limit_down(pct: float, limit_pct: float) -> bool:
    """判断当天是否跌停（T+2 跌停 = 卖不出）。"""
    return pct <= -limit_pct * 100 * 0.99


def main():
    config = load_json_config(PROJECT_ROOT / "config/config.json")

    # ── 加载策略 C 信号池 ──────────────────────────────────────────────
    opt = Recent2YFreshOptimizer()
    sub_c_full = opt.load_recent_candidates()
    conds = (
        ("market_leader_rank_bucket", "rank_gt_30"),
        ("segment_emotion_state_bucket", "ice_point"),
        ("volume_ratio_bucket", "1_2"),
    )
    sub_c = opt.apply_conditions(sub_c_full, conds)

    # 每日选换手率最高一只
    selected = (
        sub_c
        .sort_values(["trade_date", "turnover_rate"], ascending=[True, False])
        .groupby("trade_date")
        .head(1)
        .reset_index(drop=True)
    )

    # ── 原始 next_day_premium_trades（补充执行字段）──────────────────
    raw = pd.read_csv(
        PROJECT_ROOT / "data/processed/next_day_premium_trades.csv",
        low_memory=False,
        dtype={"ts_code": str, "trade_date": str},
    )
    raw["next_trade_date"] = raw["next_trade_date"].astype(str)
    raw["exit_trade_date"] = raw["exit_trade_date"].astype(str)
    raw = raw[raw["trade_date"] >= "20240520"]

    exec_cols = [
        "trade_date", "ts_code", "name",
        "next_trade_date", "next_open", "exit_trade_date", "exit_close",
        "gross_return", "net_return", "fee_rate",
        "circ_mv", "amount", "turnover_rate",
        "open_times", "board_type", "limit_pct", "limit_pct_bucket",
        "market_segment",
        "fill_probability", "current_queue_amount",
        "planned_buy_amount", "estimated_turnover_amount", "available_fill_amount",
    ]
    ec = [c for c in exec_cols if c in raw.columns]
    selected = selected.merge(
        raw[ec].drop_duplicates(["trade_date", "ts_code"]),
        on=["trade_date", "ts_code"],
        how="left",
        suffixes=("", "_r"),
    )

    # ── 日线（T+1 买入日、T+2 卖出日）──────────────────────────────
    dm = pd.read_csv(
        PROJECT_ROOT / "data/processed/daily_merged.csv",
        usecols=["ts_code", "trade_date", "open", "high", "low", "close",
                  "pct_chg", "vol", "amount"],
        low_memory=False,
        dtype={"ts_code": str, "trade_date": str},
    )

    selected["next_trade_date"] = selected["next_trade_date"].astype(str)
    selected["exit_trade_date"] = selected["exit_trade_date"].astype(str)

    t1 = dm.rename(columns={
        "trade_date": "next_trade_date",
        "open": "t1_open", "high": "t1_high", "low": "t1_low",
        "close": "t1_close", "pct_chg": "t1_pct_chg",
        "amount": "t1_amount_unit", "vol": "t1_vol",
    })
    t2 = dm.rename(columns={
        "trade_date": "exit_trade_date",
        "open": "t2_open", "high": "t2_high", "low": "t2_low",
        "close": "t2_close", "pct_chg": "t2_pct_chg",
        "amount": "t2_amount_unit", "vol": "t2_vol",
    })
    selected = selected.merge(
        t1[["ts_code", "next_trade_date", "t1_open", "t1_high", "t1_low",
            "t1_close", "t1_pct_chg", "t1_amount_unit", "t1_vol"]],
        on=["ts_code", "next_trade_date"], how="left",
    )
    selected = selected.merge(
        t2[["ts_code", "exit_trade_date", "t2_open", "t2_high", "t2_low",
            "t2_close", "t2_pct_chg", "t2_amount_unit", "t2_vol"]],
        on=["ts_code", "exit_trade_date"], how="left",
    )

    # ── 逐笔真实执行模拟 ──────────────────────────────────────────────
    equity = INITIAL_CASH
    occupied_until = ""
    rows = []
    trade_seq = 0

    for _, sig in selected.iterrows():
        # --- 占仓判断 ---
        buy_date = str(sig.get("next_trade_date", ""))
        if occupied_until and buy_date <= occupied_until:
            continue

        # --- 基础数值 ---
        name         = str(sig.get("name", ""))
        ts_code      = str(sig.get("ts_code", ""))
        signal_date  = str(sig.get("trade_date", ""))
        limit_pct    = float(sig.get("limit_pct", 0.10))
        t1_pct       = float(sig.get("t1_pct_chg", 0)) if pd.notna(sig.get("t1_pct_chg")) else 0
        t2_pct       = float(sig.get("t2_pct_chg", 0)) if pd.notna(sig.get("t2_pct_chg")) else 0
        t1_open      = float(sig.get("t1_open", 0)) if pd.notna(sig.get("t1_open")) else 0
        t1_amount_yuan = float(sig.get("t1_amount_unit", 0)) * 1000 if pd.notna(sig.get("t1_amount_unit")) else 0  # 万→元
        t2_close     = float(sig.get("t2_close", 0)) if pd.notna(sig.get("t2_close")) else 0
        t2_amount_yuan = float(sig.get("t2_amount_unit", 0)) * 1000 if pd.notna(sig.get("t2_amount_unit")) else 0

        # --- T+1 买入：一字涨停买不到 ---
        if is_limit_up(t1_pct, limit_pct) and t1_open == sig.get("t1_high", t1_open):
            # T+1 开盘即涨停，默认买不到
            rows.append({
                "seq": None, "signal_date": signal_date, "buy_date": buy_date,
                "sell_date": str(sig.get("exit_trade_date", "")),
                "ts_code": ts_code, "name": name,
                "status": "买入阻塞(T+1涨停)",
                "reject_reason": f"T+1开盘涨停 pct={t1_pct:.1f}%",
                "buy_amount": 0, "actual_position_pct": 0,
                "buy_slippage": 0, "sell_slippage": 0,
                "net_return": None, "account_return": None,
                "equity_after": equity, "drawdown": None,
                "t1_amount_yuan": t1_amount_yuan,
            })
            continue

        # --- 实际买入金额（动态容量限制）---
        target_buy = equity * POSITION_PCT
        if t1_amount_yuan > 0:
            cap = t1_amount_yuan * MAX_BUY_AMT_RATIO
            actual_buy = min(target_buy, cap)
        else:
            actual_buy = target_buy
        actual_pos_pct = actual_buy / equity

        # --- 买入滑点（动态）---
        buy_ratio = actual_buy / t1_amount_yuan if t1_amount_yuan > 0 else 0
        buy_slip  = est_slippage(buy_ratio)
        buy_price = t1_open * (1 + buy_slip) if t1_open > 0 else 0

        # --- T+2 卖出：跌停卖不出 ---
        exit_date = str(sig.get("exit_trade_date", ""))
        if is_limit_down(t2_pct, limit_pct):
            # 跌停卖不出，延迟一天（这里简化：按跌停收盘价卖出，备注标记）
            sell_price_raw = t2_close
            sell_blocked = True
        else:
            sell_price_raw = t2_close
            sell_blocked = False

        # 卖出滑点
        sell_value_before = actual_buy * (sell_price_raw / buy_price) if buy_price > 0 else actual_buy
        sell_ratio = sell_value_before / t2_amount_yuan if t2_amount_yuan > 0 else 0
        sell_slip  = est_slippage(sell_ratio)
        sell_price = sell_price_raw * (1 - sell_slip) if not sell_blocked else sell_price_raw * (1 - sell_slip)

        if buy_price <= 0 or sell_price <= 0:
            continue

        # --- 净收益 ---
        gross_ret  = sell_price / buy_price - 1
        net_ret    = gross_ret - FEE_FIXED
        acct_ret   = net_ret * actual_pos_pct
        equity_after = equity * (1 + acct_ret)

        trade_seq += 1
        rows.append({
            "seq": trade_seq,
            "signal_date": signal_date,
            "buy_date": buy_date,
            "sell_date": exit_date,
            "ts_code": ts_code,
            "name": name,
            "status": "跌停延迟卖出" if sell_blocked else "正常成交",
            "reject_reason": "",
            "target_buy_万": round(target_buy / 10000, 2),
            "actual_buy_万": round(actual_buy / 10000, 2),
            "capacity_cap_万": round(t1_amount_yuan * MAX_BUY_AMT_RATIO / 10000, 2) if t1_amount_yuan > 0 else None,
            "t1_amount_亿": round(t1_amount_yuan / 1e8, 2),
            "actual_position_pct": round(actual_pos_pct * 100, 2),
            "buy_amount_ratio_pct": round(buy_ratio * 100, 4),
            "buy_slippage_pct": round(buy_slip * 100, 3),
            "sell_slippage_pct": round(sell_slip * 100, 3),
            "total_cost_pct": round((buy_slip + sell_slip + FEE_FIXED) * 100, 3),
            "fixed_net_return": round(float(sig.get("net_return", 0)) * 100, 2) if pd.notna(sig.get("net_return")) else None,
            "real_net_return": round(net_ret * 100, 2),
            "real_account_return": round(acct_ret * 100, 2),
            "equity_万": round(equity_after / 10000, 2),
            "market_segment": str(sig.get("market_segment", "")),
            "limit_pct": limit_pct,
            "t1_pct": round(t1_pct, 2),
            "t2_pct": round(t2_pct, 2),
            "open_times": sig.get("open_times", None),
            "board_type": str(sig.get("board_type", "")),
        })

        equity = equity_after
        occupied_until = exit_date

    # ── 输出报告 ──────────────────────────────────────────────────────
    result = pd.DataFrame(rows)
    executed = result[result["seq"].notna()].copy()
    blocked  = result[result["seq"].isna()].copy()

    # 峰值回撤
    executed["peak"] = executed["equity_万"].cummax()
    executed["drawdown_pct"] = (executed["equity_万"] / executed["peak"] - 1) * 100

    # ── 逐笔明细打印 ──────────────────────────────────────────────────
    print("=" * 120)
    print("策略C 真实执行验证  |  初始资金50万  80%仓位  T+2收盘卖出  动态滑点  5%成交额容量上限")
    print("=" * 120)
    print()

    hdr = (f"{'#':>4} {'信号日':<10} {'买入日':<10} {'卖出日':<10} {'代码':<14} {'名称':<10} "
           f"{'状态':<12} {'实际仓位':<9} {'买入滑点':<9} {'卖出滑点':<9} "
           f"{'总费率':<8} {'固定净收':<9} {'真实净收':<9} {'账户收益':<9} {'权益(万)':<10} {'回撤':<7}")
    print(hdr)
    print("-" * 140)

    eq_track = INITIAL_CASH
    peak_track = INITIAL_CASH
    for _, r in result.iterrows():
        if pd.isna(r["seq"]):
            # 阻塞行
            print(f"  {'--':>3} {r['signal_date']:<10} {r['buy_date']:<10} {r['sell_date']:<10} "
                  f"{r['ts_code']:<14} {r['name']:<10} ⛔{r['status']:<10} -- 买入阻塞")
            continue
        eq_track = r["equity_万"] * 10000
        peak_track = max(peak_track, eq_track)
        dd = (eq_track / peak_track - 1) * 100
        win = "✓" if r["real_net_return"] > 0 else "✗"
        print(
            f"  {int(r['seq']):>4} {r['signal_date']:<10} {r['buy_date']:<10} {r['sell_date']:<10} "
            f"{r['ts_code']:<14} {r['name']:<10} "
            f"{'⚠跌停延迟' if r['status'] == '跌停延迟卖出' else '正常':^12} "
            f"{r['actual_position_pct']:>6.1f}%   "
            f"{r['buy_slippage_pct']:>6.3f}%   "
            f"{r['sell_slippage_pct']:>6.3f}%   "
            f"{r['total_cost_pct']:>6.3f}%  "
            f"{r['fixed_net_return']:>+6.2f}%  "
            f"{r['real_net_return']:>+7.2f}%  "
            f"{r['real_account_return']:>+7.2f}%  "
            f"{r['equity_万']:>9.2f}万  "
            f"{dd:>+5.1f}%  {win}"
        )

    print()
    print("=" * 100)
    print(f"买入阻塞（T+1涨停买不到）: {len(blocked)} 笔")
    print(f"正常成交: {len(executed[executed['status']=='正常成交'])} 笔")
    print(f"跌停延迟卖出: {len(executed[executed['status']=='跌停延迟卖出'])} 笔")
    print()

    # ── 年度汇总 ──────────────────────────────────────────────────────
    executed["year"] = executed["signal_date"].str[:4]
    print("=== 年度汇总 ===")
    print(f"  {'年份':<6} {'成交笔':<7} {'阻塞笔':<7} {'胜率':<8} {'均真实净收':<11} {'均固定净收':<11} "
          f"{'均仓位':<8} {'均买滑点':<9} {'均卖滑点':<9} {'年复利':>8} {'年末权益':>9}")
    prev_eq = 50.0
    for yr, g in executed.groupby("year"):
        blk_yr = len(blocked[blocked["signal_date"].str[:4] == yr]) if len(blocked) > 0 else 0
        wr  = (g["real_net_return"] > 0).mean()
        avg_real = g["real_net_return"].mean()
        avg_fix  = g["fixed_net_return"].mean() if g["fixed_net_return"].notna().any() else 0
        avg_pos  = g["actual_position_pct"].mean()
        avg_bsl  = g["buy_slippage_pct"].mean()
        avg_ssl  = g["sell_slippage_pct"].mean()
        yr_eq    = g["equity_万"].iloc[-1]
        yr_ret   = yr_eq / prev_eq - 1
        print(f"  {yr:<6} {len(g):<7} {blk_yr:<7} {wr:<7.1%} "
              f"{avg_real:<10.2f}%  {avg_fix:<10.2f}%  "
              f"{avg_pos:<7.1f}%  {avg_bsl:<8.3f}%  {avg_ssl:<8.3f}%  "
              f"{yr_ret*100:>+7.1f}%  {yr_eq:>8.2f}万")
        prev_eq = yr_eq

    print()

    # ── 整体对比 ──────────────────────────────────────────────────────
    final_eq = executed["equity_万"].iloc[-1] if len(executed) > 0 else 50.0
    eq_multiple = final_eq / 50.0
    win_rate    = (executed["real_net_return"] > 0).mean()
    avg_real    = executed["real_net_return"].mean()
    max_dd      = executed["drawdown_pct"].min()
    max_win     = executed["real_net_return"].max()
    max_loss    = executed["real_net_return"].min()

    def max_consec_loss(lst):
        mx, cur = 0, 0
        for v in lst:
            if v <= 0: cur += 1; mx = max(mx, cur)
            else: cur = 0
        return mx

    mcl = max_consec_loss(executed["real_net_return"].tolist())

    print("=== 整体对比（固定0.1%滑点 vs 真实动态滑点）===")
    print(f"{'指标':<22} {'固定0.1%滑点(原)':>18} {'真实动态滑点':>16}")
    print("-" * 58)
    print(f"{'总复利倍数':<22} {'58.88x':>18} {eq_multiple:>15.2f}x")
    print(f"{'最终权益':<22} {'2944万':>18} {final_eq:>14.2f}万")
    print(f"{'成交笔数':<22} {'124笔':>18} {len(executed):>15}笔")
    print(f"{'买入阻塞笔数':<22} {'0笔(未验证)':>18} {len(blocked):>15}笔")
    print(f"{'胜率':<22} {'58.1%':>18} {win_rate:>15.1%}")
    print(f"{'均净收益':<22} {'4.99%':>18} {avg_real:>15.2f}%")
    print(f"{'最大回撤':<22} {'-23.2%':>18} {max_dd:>15.1f}%")
    print(f"{'最大单笔亏损':<22} {'-19.04%':>18} {max_loss:>15.2f}%")
    print(f"{'最大单笔盈利':<22} {'+77.24%':>18} {max_win:>15.2f}%")
    print(f"{'最大连续亏损':<22} {'6笔':>18} {mcl:>15}笔")
    print()

    # ── 流动性压力分析 ──────────────────────────────────────────────
    print("=== 买入流动性分析 ===")
    tier_labels = {
        (0.0, 0.5): "极充裕 ≤0.5%",
        (0.5, 1.0): "充裕  0.5~1%",
        (1.0, 2.0): "正常  1~2%",
        (2.0, 5.0): "有压力 2~5%",
        (5.0, 999): "高压力 >5%",
    }
    executed["t1_amount_亿"] = executed.get("t1_amount_亿", pd.Series(dtype=float))
    print(f"  {'流动性档位':<18} {'笔数':<6} {'均买滑点':<10} {'均真实净收':<12}")
    for (lo, hi), lbl in tier_labels.items():
        g = executed[(executed["buy_amount_ratio_pct"] >= lo) & (executed["buy_amount_ratio_pct"] < hi)]
        if len(g) == 0:
            continue
        print(f"  {lbl:<18} {len(g):<6} {g['buy_slippage_pct'].mean():<9.3f}%  "
              f"{g['real_net_return'].mean():<10.2f}%")

    # ── 保存 ──────────────────────────────────────────────────────────
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    executed.to_csv(out_dir / "strategy_c_real_execution_detail.csv", index=False, encoding="utf-8-sig")
    print(f"\n明细已保存: reports/strategy_c_real_execution_detail.csv")

    return executed


if __name__ == "__main__":
    main()
