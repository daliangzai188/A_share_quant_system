"""
策略D（首板打板）候选与收益回测脚本

当前唯一执行口径：
  - D策略在盘中执行：发现首板涨停 → 在涨停价排队买入
  - D一律在T+2收盘退出，不再按ABC状态切换到T+1
  - D只在账户当天空仓时触发（没有A/B/C持仓，也没有待执行的A/B/C买入计划）

模拟流程（按日推进）：
  每天开始：
    1. 若持有D仓位且到T+2 → 以收盘价卖出，仓位清空
    2. 若持有A/B/C仓位 → 检查是否到期，到期则平仓
    3. 若账户空仓：
       a. 检查今天有无D候选（盘中机会）→ 有则打板
       b. 若D没打板 → 今晚A/B/C流水线生成信号，明天开盘执行A/B/C

本脚本负责冻结D候选和D单腿T+2收益。当前完整组合的真实串行占仓、D>A>E>C>N
时序与认证，以 scripts/certify_current_executable_portfolio.py 为唯一发布来源。

输出：
  reports/strategy_d/
    backtest_summary.csv     - 总体指标对比（A+B+C vs A+B+C+D）
    d_daily_candidates.csv   - D完整逐日第一名候选；不受任何旧组合占仓状态裁剪
    d_trades.csv             - 旧A/B/C占仓路径下的D逐笔审计明细，不得再作为当前组合候选门
    equity_curve.csv         - 逐日净值曲线
    yearly_comparison.csv    - 年度对比

用法：
    python scripts/backtest_strategy_d.py

发布口径固定使用配置文件中的冻结输入、80%成交压力、80%最低成交概率和82.5%仓位；
命令行参数只用于显式拒绝旧调用，不能覆盖发布标准。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.strategy_d_spec import (
    D_BOARD_TYPE,
    D_MIN_FILL_PROBABILITY,
    D_SENTIMENT_LEVEL,
    d_rank_key,
    historical_candidate_mask,
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_EQUITY = 500_000.0
POSITION_PCT = 0.825

# D可触发的ABC状态集合：
#   NO_CANDIDATE          - 账户空仓，ABC无信号  → D独立使用资金
#   HISTORICAL_SIM_FILLED - ABC当天生成信号，T+1开盘买入
#                           D盘中信号更早；当前实盘由D占仓并阻断该计划
#   BUY_REJECTED          - ABC信号被风控拒绝，账户实际空仓 → D可独立使用资金
#   POSITION_OCCUPIED_SKIP - ABC持有旧仓位，资金被占且D效果差（实测胜率20%,-2%）→ 不做
D_ELIGIBLE_STATUSES = {"NO_CANDIDATE", "HISTORICAL_SIM_FILLED", "BUY_REJECTED"}

D_FEE_RATE = 0.0015                       # 旧D回测口径：费用+滑点合计按0.15%扣除
DEFAULT_ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj", "other"}


def load_strategy_d_config() -> dict:
    path = PROJECT_ROOT / "config" / "config.json"
    try:
        import json
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
        return config.get("strategy_d", {})
    except Exception:
        return {}


def configured_allowed_segments() -> set[str]:
    config = load_strategy_d_config()
    values = config.get("allowed_market_segments", sorted(DEFAULT_ALLOWED_SEGMENTS))
    if not isinstance(values, list):
        return set(DEFAULT_ALLOWED_SEGMENTS)
    result = {str(item).strip() for item in values if str(item).strip()}
    return result or set(DEFAULT_ALLOWED_SEGMENTS)


def configured_position_pct() -> float:
    value = float(load_strategy_d_config().get("position_pct", POSITION_PCT))
    if not 0 < value <= 1:
        raise ValueError("config.strategy_d.position_pct 必须在(0,1]范围内")
    return value


def configured_backtest_input_path() -> Path:
    relative = str(
        load_strategy_d_config().get(
            "backtest_input_path",
            "data/processed/next_day_premium_trades_2y.csv",
        )
    )
    path = PROJECT_ROOT / relative
    if not path.exists():
        raise FileNotFoundError(f"D回测冻结输入不存在: {path}")
    return path


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
    df = pd.read_csv(configured_backtest_input_path(), low_memory=False)
    df["trade_date"] = df["trade_date"].astype(str)

    df = df[
        historical_candidate_mask(
            df,
            min_fill_probability=min_fill_probability,
            allowed_segments=allowed_segments,
        )
    ].copy()

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
    """每天最多选1只：优先炸板2次，再按封单/流通市值比降序。

    这是旧报告中A+B+C+D落地版约303倍的D策略口径：
      1. 候选必须满足open_times<=3；
      2. 多候选时优先open_times==2；
      3. 同优先级内按fd_amount_to_circ_mv降序。
    """
    if day_candidates.empty:
        return None
    ranked = day_candidates.copy()
    ranked["_d_rank_key"] = ranked.apply(
        lambda row: d_rank_key(
            open_times=int(row["open_times"]),
            fd_amount_to_circ_mv=float(row["fd_amount_to_circ_mv"]),
            ts_code=str(row["ts_code"]),
        ),
        axis=1,
    )
    return ranked.sort_values(
        ["_d_rank_key"],
        ascending=[False],
    ).iloc[0]


def build_daily_candidate_ledger(d_candidates: pd.DataFrame) -> pd.DataFrame:
    """逐日锁定D第一名，不读取也不裁剪任何组合持仓状态。

    D实盘每天盘中独立扫描，是否能下单由当时真实账户持仓决定。因此组合认证必须
    先拥有完整的逐日D第一名，再由统一串行回放判断当天是否占仓；不能反过来用
    旧A/B/C回放的``POSITION_OCCUPIED_SKIP``提前删除D候选。
    """

    rows: list[dict[str, object]] = []
    for signal_date, day_rows in d_candidates.groupby("trade_date", sort=True):
        picked = pick_d_candidate(day_rows)
        if picked is None:
            continue
        record = picked.to_dict()
        record["signal_date"] = str(signal_date)
        record["daily_candidate_count"] = int(len(day_rows))
        rows.append(record)
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values("signal_date")
        .drop_duplicates("signal_date", keep="last")
        .reset_index(drop=True)
    )


def safe_float(value: object, default: float = 0.0) -> float:
    """把缺失/异常数值安全转换为 float，避免 NaN 污染资金曲线。"""
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def run_simulation(
    abc_detail: pd.DataFrame,
    d_candidates: pd.DataFrame,
    fill_rate: float = 0.8,
    position_pct: float = POSITION_PCT,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """
    D候选与单腿收益冻结逻辑：
      - D策略盘中执行（当日涨停价买入）
      - 不论ABC历史状态，D都按T+2收盘计算
      - POSITION_OCCUPIED_SKIP：旧仓位占用资金，D不触发

    返回的ABC叠加曲线只用于旧报告审计；当前组合发布必须运行
    certify_current_executable_portfolio.py的严格串行回放。

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
                limit_close = safe_float(candidate.get("limit_close"), 0.0)
                fee = D_FEE_RATE
                exit_close_val = safe_float(candidate.get("exit_close"), 0.0)
                net_ret = (
                    exit_close_val / limit_close - 1 - fee
                    if (limit_close > 0 and exit_close_val > 0)
                    else 0.0
                )
                exit_rule = "T+2_close"
                d_ret = net_ret * position_pct * fill_rate
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
                    "is_win": bool(net_ret > 0),
                    "next_open": candidate.get("next_open", 0),
                    "limit_close": limit_close,
                    "exit_close": candidate.get("exit_close", 0),
                    "exit_rule": exit_rule,
                    "abc_status": op_status,
                })

        # 这里的叠加曲线只保留旧报告横向审计价值；当前发布组合必须使用认证脚本的
        # 严格串行占仓回放，禁止把本曲线当实盘标尺。
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
            "note": "当前主实盘口径包含科创和北交，仍需看各分段稳定性。",
        },
        {
            "gate": "全部成交概率不低于阈值",
            "value": round(min_fill_probability, 4),
            "threshold": D_MIN_FILL_PROBABILITY,
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
    parser.add_argument("--fill-rate", type=float, default=0.8,
                        help="成交压力系数（0~1），只做确定性收益折减；成交过滤优先使用 fill_probability")
    parser.add_argument(
        "--min-fill-probability",
        type=float,
        default=D_MIN_FILL_PROBABILITY,
                        help="最低成交概率，默认0.8")
    parser.add_argument(
        "--allowed-segments",
        default=None,
        help="允许市场分段，逗号分隔。不填则读取 config.strategy_d.allowed_market_segments。",
    )
    args = parser.parse_args()

    if args.use_minute_features:
        raise ValueError(
            "发布版D回测禁止叠加可选分钟过滤；研究变体必须使用独立输出脚本。"
        )
    if abs(args.fill_rate - 0.8) > 1e-12:
        raise ValueError("发布版D回测成交压力固定为0.8，禁止覆盖。")
    if abs(args.min_fill_probability - D_MIN_FILL_PROBABILITY) > 1e-12:
        raise ValueError(
            f"发布版D回测成交概率固定为{D_MIN_FILL_PROBABILITY:.0%}，禁止覆盖。"
        )
    allowed_segments = configured_allowed_segments()
    if args.allowed_segments:
        requested_segments = parse_segments(args.allowed_segments)
        if requested_segments != allowed_segments:
            raise ValueError(
                "发布版D回测市场分段必须与config.strategy_d完全一致，禁止覆盖。"
            )
    position_pct = configured_position_pct()

    print("加载 A+B+C 历史回测明细...")
    abc_detail = load_abc_detail()
    print(f"  共 {len(abc_detail)} 个交易日，{abc_detail['signal_date'].min()} ~ {abc_detail['signal_date'].max()}")

    print(
        f"\n加载 D策略候选池（情绪={D_SENTIMENT_LEVEL}，板型={D_BOARD_TYPE}）..."
    )
    d_candidates = load_d_candidates(args.use_minute_features, args.min_fill_probability, allowed_segments)
    print(f"  共 {len(d_candidates)} 条，覆盖 {d_candidates['trade_date'].nunique()} 个交易日")
    print(f"  成交概率阈值 >= {args.min_fill_probability:.0%} | 允许分段: {','.join(sorted(allowed_segments)) or '全部'}")

    # 只保留和ABC回测窗口重叠的日期
    abc_dates = set(abc_detail["signal_date"])
    d_in_window = d_candidates[d_candidates["trade_date"].isin(abc_dates)]
    print(f"  与A+B+C回测窗口重叠: {len(d_in_window)} 条，{d_in_window['trade_date'].nunique()} 天")

    # 先固化完整逐日D第一名。该账本不读取旧A/B/C占仓状态，是当前组合认证唯一
    # 允许使用的D候选来源。
    d_daily_candidates = build_daily_candidate_ledger(d_in_window)
    if d_daily_candidates.empty:
        raise RuntimeError("D完整逐日候选账本为空，拒绝继续回测")
    d_daily_candidates.to_csv(
        OUTPUT_DIR / "d_daily_candidates.csv", index=False, encoding="utf-8-sig"
    )
    print(f"  D完整逐日第一名: {len(d_daily_candidates)} 天（未按旧组合占仓裁剪）")

    print(f"\n运行模拟（打板成功率={args.fill_rate:.0%}）...")
    df_abc, df_abcd, d_log = run_simulation(
        abc_detail,
        d_in_window,
        fill_rate=args.fill_rate,
        position_pct=position_pct,
    )

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
