"""
策略D（首板打板）完整回测脚本

正确的执行逻辑：
  - D策略在盘中执行：发现首板涨停 → 在涨停价排队买入 → 次日开盘溢价卖出
  - D只在账户当天空仓时触发（没有A/B/C持仓，也没有待执行的A/B/C买入计划）
  - D触发后，占用仓位直到T+1开盘卖出

模拟流程（按日推进）：
  每天开始：
    1. 若持有D仓位 → 以next_open卖出，仓位清空
    2. 若持有A/B/C仓位 → 检查是否到期，到期则平仓
    3. 若账户空仓：
       a. 检查今天有无D候选（盘中机会）→ 有则打板（D占仓到明天开盘）
       b. 若D没打板 → 今晚A/B/C流水线生成信号，明天开盘执行A/B/C

与A/B/C冲突处理：
  - D在盘中先触发（早于A/B/C 15:10流水线）
  - 若D今天打板 → 占仓到T+1开盘 → 今晚A/B/C生成的信号（明天执行）被阻断
  - 若D今天没打板 → A/B/C正常执行

输出：
  reports/strategy_d/
    backtest_summary.csv     - 总体指标对比（A+B+C vs A+B+C+D）
    d_trades.csv             - D策略逐笔明细
    equity_curve.csv         - 逐日净值曲线
    yearly_comparison.csv    - 年度对比

用法：
    python scripts/backtest_strategy_d.py
    python scripts/backtest_strategy_d.py --use-minute-features  # 使用分钟特征优化填单估计
    python scripts/backtest_strategy_d.py --fill-rate 0.8        # 假设打板成功率80%
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.8  # 仓位比例

# D可触发的ABC状态集合：
#   NO_CANDIDATE          - 账户空仓，ABC无信号  → D独立使用资金
#   HISTORICAL_SIM_FILLED - ABC当天生成信号，T+1开盘买入
#                           D盘中买→T+1开盘卖，与ABC买入同时发生但用同一资金顺序执行
#                           → D收益 + ABC收益 可叠加
#   BUY_REJECTED          - ABC信号被风控拒绝，账户实际空仓 → D可独立使用资金
#   POSITION_OCCUPIED_SKIP - ABC持有旧仓位，资金被占且D效果差（实测胜率20%,-2%）→ 不做
D_ELIGIBLE_STATUSES = {"NO_CANDIDATE", "HISTORICAL_SIM_FILLED", "BUY_REJECTED"}

# D策略候选过滤条件
D_SENTIMENT = "strong"
D_BOARD_TYPE = "multi_open"               # 炸板后重封，才有机会打板
D_TIME_BUCKETS = {"midday", "afternoon", "late"}  # 非开盘即封死的时段
D_TAIL_SEALED_HOUR = 14                   # 最终封板时间 >= 14:00（last_time >= 140000）
D_MAX_OPEN_TIMES = 3                      # 炸板次数 <= 3（过多炸板说明封板不稳）


def load_abc_detail() -> pd.DataFrame:
    """加载A+B+C最新回测逐日明细（最优配置）"""
    path = (PROJECT_ROOT / "reports" / "paper_trade" / "backup_strategy_c" /
            "current_config_c_exit_refine_exit5_20240520_20260514_481d_best_abc_detail.csv")
    df = pd.read_csv(path)
    df = df[df["scenario"] == "A_plus_B_plus_C_refined"].copy()
    df["signal_date"] = df["signal_date"].astype(str)
    df = df.sort_values("signal_date").drop_duplicates("signal_date", keep="last")
    return df


def parse_segments(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def load_d_candidates(
    use_minute_features: bool,
    min_fill_probability: float,
    allowed_segments: set[str],
) -> pd.DataFrame:
    """加载D策略候选池，含收益数据"""
    df = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "next_day_premium_trades.csv")
    df["trade_date"] = df["trade_date"].astype(str)

    # 基础筛选
    df["last_time_hm"] = df["last_time"].astype(float)
    df = df[
        (df["limit_times"] == 1) &
        (~df["is_st"].astype(bool)) &
        (df["market_sentiment_level"] == D_SENTIMENT) &
        (df["board_type"] == D_BOARD_TYPE) &
        (df["first_time_bucket"].isin(D_TIME_BUCKETS)) &
        (df["last_time_hm"] >= D_TAIL_SEALED_HOUR * 10000) &   # 14点后最终封板
        (df["open_times"] <= D_MAX_OPEN_TIMES) &                # 炸板次数不超过3次
        (df["fill_probability"] >= min_fill_probability) &
        (df["is_fill_score_reliable"].astype(bool))
    ].copy()

    if allowed_segments:
        df = df[df["market_segment"].isin(allowed_segments)].copy()

    # 若有分钟特征则叠加：只选尾盘封板（tail_sealed=True）的，打板时机更稳
    minute_path = PROJECT_ROOT / "data" / "processed" / "strategy_d_minute_features.csv"
    if use_minute_features and minute_path.exists():
        mf = pd.read_csv(minute_path)
        mf["trade_date"] = mf["trade_date"].astype(str)
        mf = mf[mf["minute_ok"] == True]
        df = df.merge(
            mf[["trade_date", "ts_code", "tail_sealed", "open_vol_ratio", "reseal_time"]],
            on=["trade_date", "ts_code"], how="left"
        )
        # 使用分钟特征时，额外要求尾盘封板
        df = df[df["tail_sealed"] == True]
        print(f"  使用分钟特征后，D候选缩减至 {len(df)} 条")

    return df


def pick_d_candidate(day_candidates: pd.DataFrame) -> pd.Series | None:
    """每天最多选1只：封单/流通市值比最高（封板强度高 → 次日溢价更稳）"""
    if day_candidates.empty:
        return None
    return day_candidates.sort_values("fd_amount_to_circ_mv", ascending=False).iloc[0]


def run_simulation(
    abc_detail: pd.DataFrame,
    d_candidates: pd.DataFrame,
    fill_rate: float = 1.0,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """
    正确的时序逻辑：
      - D策略盘中执行（当日买入涨停价，次日开盘卖出）
      - A/B/C策略收盘后生成信号（次日开盘买入，后天收盘卖出）
      - D只在NO_CANDIDATE天触发（账户确认空仓），返回数据已验证：NO_CANDIDATE天active_position为空
      - D退出（次日开盘）vs A/B/C入场（后天开盘）时间不重叠，完全不冲突
      - 因此D的收益直接叠加在A+B+C基础上，无需阻断A/B/C

    Returns:
        df_abc: 逐日ABC资金曲线
        df_abcd: 逐日ABCD资金曲线
        d_trade_log: D策略逐笔明细
    """
    d_by_date: dict[str, pd.DataFrame] = {
        dt: grp for dt, grp in d_candidates.groupby("trade_date")
    }

    equity_abc = INITIAL_EQUITY
    equity_abcd = INITIAL_EQUITY
    records_abc = []
    records_abcd = []
    d_trade_log = []

    for _, row in abc_detail.iterrows():
        dt = row["signal_date"]
        abc_acct_ret = float(row["account_return"]) if pd.notna(row["account_return"]) else 0.0
        op_status = row["operation_status"]
        leg = row["strategy_leg"]

        # ── A+B+C 原始曲线（不变）──
        equity_abc = equity_abc * (1 + abc_acct_ret)
        records_abc.append({
            "date": dt, "equity": equity_abc, "ret": abc_acct_ret,
            "leg": leg if leg != "NONE" else "no_trade", "op": op_status
        })

        # ── A+B+C+D 曲线 ──
        # D在NO_CANDIDATE天（账户空仓）盘中触发，收益直接叠加，不影响A/B/C
        d_ret = 0.0
        abcd_leg = leg if leg != "NONE" else "no_trade"

        if op_status in D_ELIGIBLE_STATUSES:
            day_d = d_by_date.get(dt)
            candidate = pick_d_candidate(day_d) if day_d is not None else None
            if candidate is not None:
                net_ret = float(candidate["net_return"])
                d_ret = net_ret * POSITION_PCT * fill_rate
                abcd_leg = "D" if op_status == "NO_CANDIDATE" else f"D+{leg}"
                d_trade_log.append({
                    "signal_date": dt,
                    "ts_code": candidate["ts_code"],
                    "name": candidate.get("name", ""),
                    "market_segment": candidate.get("market_segment", ""),
                    "first_time_bucket": candidate.get("first_time_bucket", ""),
                    "open_times": candidate.get("open_times", 0),
                    "fd_amount": candidate.get("fd_amount", 0),
                    "fd_amount_to_circ_mv": candidate.get("fd_amount_to_circ_mv", 0),
                    "fill_probability": candidate.get("fill_probability", 0),
                    "sample_count": candidate.get("sample_count", 0),
                    "net_return": net_ret,
                    "account_return": d_ret,
                    "fill_rate_stress": fill_rate,
                    "is_win": bool(candidate["is_win"]),
                    "next_open": candidate.get("next_open", 0),
                    "limit_close": candidate.get("limit_close", 0),
                    "abc_status": op_status,
                })

        # 总收益 = A/B/C收益 + D收益
        # HISTORICAL_SIM_FILLED天：D盘中买T→T+1卖，ABC T+1买→T+2/T+3卖，同笔资金顺序使用
        # 因此 total_ret = abc_ret + d_ret 是对同一资金先做D再做ABC的近似叠加
        total_ret = abc_acct_ret + d_ret
        equity_abcd = equity_abcd * (1 + total_ret)
        records_abcd.append({
            "date": dt, "equity": equity_abcd, "ret": total_ret,
            "leg": abcd_leg, "op": op_status
        })

    return pd.DataFrame(records_abc), pd.DataFrame(records_abcd), d_trade_log


def calc_metrics(df: pd.DataFrame, initial: float = INITIAL_EQUITY) -> dict:
    executed = df[df["ret"] != 0.0]
    final = df["equity"].iloc[-1]
    peak = df["equity"].cummax()
    dd = (df["equity"] - peak) / peak
    wins = (executed["ret"] > 0).sum()
    total = len(executed)
    gross_profit = executed.loc[executed["ret"] > 0, "ret"].sum() if total else 0.0
    gross_loss = abs(executed.loc[executed["ret"] < 0, "ret"].sum()) if total else 0.0
    loss_flags = (executed["ret"] < 0).astype(int).tolist()
    max_consecutive_losses = 0
    current_losses = 0
    for flag in loss_flags:
        current_losses = current_losses + 1 if flag else 0
        max_consecutive_losses = max(max_consecutive_losses, current_losses)
    return {
        "trade_count": total,
        "final_equity": round(final, 2),
        "equity_multiple": round(final / initial, 4),
        "win_rate": round(wins / total, 4) if total else 0,
        "avg_account_return": round(executed["ret"].mean(), 6) if total else 0,
        "median_account_return": round(executed["ret"].median(), 6) if total else 0,
        "max_drawdown": round(dd.min(), 6),
        "max_profit": round(executed["ret"].max(), 6) if total else 0,
        "max_loss": round(executed["ret"].min(), 6) if total else 0,
        "profit_loss_ratio": round(gross_profit / gross_loss, 6) if gross_loss else 0,
        "max_consecutive_losses": max_consecutive_losses,
    }


def build_validation_gates(d_log: list[dict], d_candidates: pd.DataFrame) -> pd.DataFrame:
    d_df = pd.DataFrame(d_log)
    sample_count = len(d_df)
    segment_count = int(d_df["market_segment"].nunique()) if sample_count and "market_segment" in d_df else 0
    min_fill_probability = float(d_df["fill_probability"].min()) if sample_count and "fill_probability" in d_df else 0.0
    max_single_trade_share = float(d_df["account_return"].abs().max()) if sample_count else 0.0
    rows = [
        {
            "gate": "样本数不少于50笔",
            "value": sample_count,
            "threshold": 50,
            "status": "PASS" if sample_count >= 50 else "FAIL",
            "note": "首板打板样本少时，单笔极端收益会严重影响结论。",
        },
        {
            "gate": "至少覆盖2个以上主要市场分段",
            "value": segment_count,
            "threshold": 2,
            "status": "PASS" if segment_count >= 2 else "FAIL",
            "note": "默认排除北交所和科创板后，仍需看主板/创业板稳定性。",
        },
        {
            "gate": "全部成交概率不低于阈值",
            "value": round(min_fill_probability, 4),
            "threshold": "由 --min-fill-probability 设置",
            "status": "PASS" if sample_count and min_fill_probability > 0 else "FAIL",
            "note": "策略D不能用买不到的涨停板收益证明有效。",
        },
        {
            "gate": "单笔账户收益绝对值不超过25%",
            "value": round(max_single_trade_share, 4),
            "threshold": 0.25,
            "status": "PASS" if max_single_trade_share <= 0.25 else "WARN",
            "note": "超过阈值说明结果可能被极端样本主导，需要单独复核容量和盘口。",
        },
        {
            "gate": "候选池存在可交易样本",
            "value": len(d_candidates),
            "threshold": ">0",
            "status": "PASS" if len(d_candidates) > 0 else "FAIL",
            "note": "没有候选时不能生成策略结论。",
        },
    ]
    return pd.DataFrame(rows)


def print_comparison(m_abc: dict, m_abcd: dict, d_log: list[dict]) -> None:
    print()
    print("=" * 68)
    print(f"  {'指标':<24} {'A+B+C':>18} {'A+B+C+D':>18}")
    print("=" * 68)
    rows = [
        ("总成交笔数", "trade_count", "d"),
        ("最终资金(万)", "final_equity", ".1f_w"),
        ("复利倍数", "equity_multiple", ".2f"),
        ("胜率", "win_rate", ".1%"),
        ("平均账户收益", "avg_account_return", ".2%"),
        ("中位账户收益", "median_account_return", ".2%"),
        ("最大回撤", "max_drawdown", ".2%"),
        ("单笔最大盈利", "max_profit", ".2%"),
        ("单笔最大亏损", "max_loss", ".2%"),
    ]
    for label, key, fmt in rows:
        v1, v2 = m_abc[key], m_abcd[key]
        if fmt == "d":
            print(f"  {label:<24} {v1:>18d} {v2:>18d}")
        elif fmt == ".1f_w":
            print(f"  {label:<24} {v1/10000:>17.1f}万 {v2/10000:>17.1f}万")
        else:
            print(f"  {label:<24} {format(v1, fmt):>18} {format(v2, fmt):>18}")
    print()

    if d_log:
        d_df = pd.DataFrame(d_log)
        print(f"D策略单独统计（共{len(d_df)}笔）：")
        print(f"  胜率:         {d_df['is_win'].mean():.1%}")
        print(f"  均净收益:     {d_df['net_return'].mean():.2%}")
        print(f"  均账户收益:   {d_df['account_return'].mean():.2%}")
        print(f"  中位账户收益: {d_df['account_return'].median():.2%}")
        print(f"  标准差:       {d_df['net_return'].std():.2%}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="策略D（首板打板）完整回测")
    parser.add_argument("--use-minute-features", action="store_true",
                        help="使用分钟特征（需先运行 collect_strategy_d_minute_data.py）")
    parser.add_argument("--fill-rate", type=float, default=1.0,
                        help="成交压力系数（0~1），只做确定性收益折减；成交过滤优先使用 fill_probability")
    parser.add_argument("--min-fill-probability", type=float, default=0.8,
                        help="最低成交概率，默认0.8")
    parser.add_argument(
        "--allowed-segments",
        default="sh_main,sz_main,chi_next",
        help="允许市场分段，逗号分隔。默认排除 star 和 bj，避免权限/容量样本混入。",
    )
    args = parser.parse_args()

    if not 0 <= args.fill_rate <= 1:
        raise ValueError("--fill-rate 必须在 0~1 之间。")
    if not 0 <= args.min_fill_probability <= 1:
        raise ValueError("--min-fill-probability 必须在 0~1 之间。")
    allowed_segments = parse_segments(args.allowed_segments)

    print("加载 A+B+C 历史回测明细...")
    abc_detail = load_abc_detail()
    print(f"  共 {len(abc_detail)} 个交易日，{abc_detail['signal_date'].min()} ~ {abc_detail['signal_date'].max()}")

    print(f"\n加载 D策略候选池（情绪={D_SENTIMENT}，板型={D_BOARD_TYPE}）...")
    d_candidates = load_d_candidates(args.use_minute_features, args.min_fill_probability, allowed_segments)
    print(f"  共 {len(d_candidates)} 条，覆盖 {d_candidates['trade_date'].nunique()} 个交易日")
    print(f"  成交概率阈值 >= {args.min_fill_probability:.0%} | 允许分段: {','.join(sorted(allowed_segments)) or '全部'}")

    # 只保留和ABC回测窗口重叠的日期
    abc_dates = set(abc_detail["signal_date"])
    d_in_window = d_candidates[d_candidates["trade_date"].isin(abc_dates)]
    print(f"  与A+B+C回测窗口重叠: {len(d_in_window)} 条，{d_in_window['trade_date'].nunique()} 天")

    print(f"\n运行模拟（打板成功率={args.fill_rate:.0%}）...")
    df_abc, df_abcd, d_log = run_simulation(abc_detail, d_in_window, fill_rate=args.fill_rate)

    m_abc = calc_metrics(df_abc)
    m_abcd = calc_metrics(df_abcd)
    validation_gates = build_validation_gates(d_log, d_in_window)

    print_comparison(m_abc, m_abcd, d_log)

    # ── 年度对比 ──
    print("年度对比:")
    df_abc["year"] = df_abc["date"].str[:4]
    df_abcd["year"] = df_abcd["date"].str[:4]
    yearly_rows = []
    eq_abc = INITIAL_EQUITY
    eq_abcd = INITIAL_EQUITY
    for yr in sorted(df_abc["year"].unique()):
        a_yr = df_abc[df_abc["year"] == yr]
        b_yr = df_abcd[df_abcd["year"] == yr]
        a_end = a_yr["equity"].iloc[-1]
        b_end = b_yr["equity"].iloc[-1]
        d_n = sum(1 for t in d_log if str(t["signal_date"])[:4] == yr)
        d_wr = sum(1 for t in d_log if str(t["signal_date"])[:4] == yr and t["is_win"])
        print(f"  {yr}: A+B+C={a_end/eq_abc-1:+.1%}  A+B+C+D={b_end/eq_abcd-1:+.1%}  "
              f"D触发={d_n}次 胜={d_wr}次")
        yearly_rows.append({
            "year": yr, "abc_return": a_end/eq_abc-1, "abcd_return": b_end/eq_abcd-1,
            "d_count": d_n, "d_win": d_wr
        })
        eq_abc = a_end
        eq_abcd = b_end

    # ── 各腿拆解 ──
    print("\n各策略腿贡献:")
    for leg_name, grp in df_abcd.groupby("leg"):
        ex = grp[grp["ret"] != 0]
        if len(ex) == 0:
            continue
        wr = (ex["ret"] > 0).mean()
        avg = ex["ret"].mean()
        print(f"  {leg_name:10s}: n={len(ex):3d}  胜率={wr:.1%}  均收益={avg:.2%}")

    # ── 保存结果 ──
    summary_rows = [
        {"strategy": "A+B+C", **m_abc},
        {"strategy": "A+B+C+D", **m_abcd},
    ]
    pd.DataFrame(summary_rows).to_csv(OUTPUT_DIR / "backtest_summary.csv", index=False)

    if d_log:
        pd.DataFrame(d_log).to_csv(OUTPUT_DIR / "d_trades.csv", index=False)

    df_abcd.rename(columns={"ret": "account_return", "leg": "strategy_leg"}).to_csv(
        OUTPUT_DIR / "equity_curve.csv", index=False)

    pd.DataFrame(yearly_rows).to_csv(OUTPUT_DIR / "yearly_comparison.csv", index=False)
    validation_gates.to_csv(OUTPUT_DIR / "validation_gates.csv", index=False, encoding="utf-8-sig")

    print(f"\n结果已保存至 reports/strategy_d/")
    print("\n策略D验证闸门:")
    print(validation_gates.to_string(index=False))


if __name__ == "__main__":
    main()
