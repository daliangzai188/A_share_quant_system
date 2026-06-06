"""
回测 bj_cap_2pct 候选策略的风控叠加规则。

文件作用：
1. 固定 bj_cap_2pct 候选策略，不改变选股条件和排序。
2. 测试科创板/创业板降仓、连续亏损后降仓、回撤阈值降仓、回撤阈值冷却暂停。
3. 单独诊断 2026 最大回撤窗口过滤对收益和回撤的影响。
4. 输出风控规则后的全区间、年度、2026 样本外指标。

本脚本只读取本地逐笔交易报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_RULE = "bj_cap_2pct"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回测 bj_cap_2pct 风控叠加规则。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_bj_filter_rules_detail.csv",
        help="BJ 规则回测逐笔明细。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_risk_overlay",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns:
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def load_rows(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[data["rule_name"] == TARGET_RULE].copy()
    if data.empty:
        raise RuntimeError(f"没有找到 {TARGET_RULE}: {path}")
    data["selected_order"] = pd.to_numeric(data["selected_order"], errors="coerce")
    data["trade_date"] = data["trade_date"].map(normalize_date)
    data["buy_trade_date"] = data["buy_trade_date"].map(normalize_date)
    data["exit_trade_date"] = data["exit_trade_date"].map(normalize_date)
    data["year"] = data["exit_trade_date"].str[:4]
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    return data.sort_values("selected_order").reset_index(drop=True)


def overlay_rules() -> list[dict[str, Any]]:
    return [
        {"overlay_name": "baseline", "description": "不叠加额外风控"},
        {
            "overlay_name": "star_half_position",
            "description": "科创板仓位降到40%",
            "segment_position_caps": {"star": 0.4},
        },
        {
            "overlay_name": "chi_next_star_half_position",
            "description": "创业板和科创板仓位降到40%",
            "segment_position_caps": {"chi_next": 0.4, "star": 0.4},
        },
        {
            "overlay_name": "after_2_losses_half_until_win",
            "description": "连续亏损2笔后降到40%，直到下一笔盈利恢复",
            "loss_streak_reduce_threshold": 2,
            "reduced_position_pct": 0.4,
        },
        {
            "overlay_name": "drawdown_15_half_position",
            "description": "当前回撤达到15%后仓位降到40%",
            "drawdown_reduce_threshold": -0.15,
            "reduced_position_pct": 0.4,
        },
        {
            "overlay_name": "drawdown_20_half_position",
            "description": "当前回撤达到20%后仓位降到40%",
            "drawdown_reduce_threshold": -0.20,
            "reduced_position_pct": 0.4,
        },
        {
            "overlay_name": "drawdown_20_cooldown_3",
            "description": "单笔后回撤达到20%则暂停后续3个信号",
            "drawdown_cooldown_threshold": -0.20,
            "cooldown_signals": 3,
        },
        {
            "overlay_name": "diagnostic_skip_2026_drawdown_window",
            "description": "诊断规则：跳过2026最大回撤窗口信号，不作为实盘规则",
            "skip_trade_date_between": ["20260316", "20260429"],
        },
    ]


def should_skip(row: pd.Series, rule: dict[str, Any], cooldown_left: int) -> str:
    if cooldown_left > 0:
        return "risk_cooldown"
    if not bool(row.get("rule_executed", False)):
        return str(row.get("rule_skip_reason", "original_not_executed"))
    window = rule.get("skip_trade_date_between")
    if window:
        trade_date = str(row.get("trade_date", ""))
        if str(window[0]) <= trade_date <= str(window[1]):
            return "skip_trade_date_window"
    return ""


def position_scale(
    row: pd.Series,
    rule: dict[str, Any],
    current_drawdown: float,
    current_loss_streak: int,
) -> tuple[float, str]:
    scale = 1.0
    reasons: list[str] = []

    segment_caps = rule.get("segment_position_caps", {})
    segment = str(row.get("market_segment", ""))
    if segment in segment_caps:
        original_pct = float(row.get("rule_actual_position_pct", 0.0))
        cap_pct = float(segment_caps[segment])
        if original_pct > 0:
            scale = min(scale, cap_pct / original_pct)
            reasons.append(f"{segment}_cap_{cap_pct}")

    dd_threshold = rule.get("drawdown_reduce_threshold")
    if dd_threshold is not None and current_drawdown <= float(dd_threshold):
        original_pct = float(row.get("rule_actual_position_pct", 0.0))
        reduced_pct = float(rule.get("reduced_position_pct", 0.4))
        if original_pct > 0:
            scale = min(scale, reduced_pct / original_pct)
            reasons.append(f"drawdown_reduce_{dd_threshold}")

    loss_threshold = rule.get("loss_streak_reduce_threshold")
    if loss_threshold is not None and current_loss_streak >= int(loss_threshold):
        original_pct = float(row.get("rule_actual_position_pct", 0.0))
        reduced_pct = float(rule.get("reduced_position_pct", 0.4))
        if original_pct > 0:
            scale = min(scale, reduced_pct / original_pct)
            reasons.append(f"loss_streak_reduce_{loss_threshold}")

    return max(0.0, min(1.0, scale)), ";".join(reasons)


def simulate_overlay(rows: pd.DataFrame, rule: dict[str, Any]) -> tuple[dict[str, Any], pd.DataFrame]:
    initial_cash = float(rows["rule_equity_before"].dropna().iloc[0])
    equity = initial_cash
    equity_peak = initial_cash
    cooldown_left = 0
    loss_streak = 0
    detail_rows: list[dict[str, Any]] = []
    trade_count = 0

    for _, row in rows.iterrows():
        result = row.to_dict()
        result["overlay_name"] = rule["overlay_name"]
        result["overlay_description"] = rule["description"]
        result["overlay_equity_before"] = equity
        current_drawdown = equity / equity_peak - 1.0 if equity_peak else 0.0
        result["overlay_current_drawdown_before"] = current_drawdown
        result["overlay_loss_streak_before"] = loss_streak

        skip_reason = should_skip(row, rule, cooldown_left)
        if cooldown_left > 0:
            cooldown_left -= 1
        if skip_reason:
            result.update(
                {
                    "overlay_executed": False,
                    "overlay_skip_reason": skip_reason,
                    "overlay_position_scale": 0.0,
                    "overlay_account_return": 0.0,
                    "overlay_equity_after": equity,
                    "overlay_drawdown_after": current_drawdown,
                }
            )
            detail_rows.append(result)
            continue

        scale, scale_reason = position_scale(row, rule, current_drawdown, loss_streak)
        base_return = float(row.get("rule_account_return", 0.0))
        overlay_return = base_return * scale
        equity_after = equity * (1.0 + overlay_return)
        equity_peak = max(equity_peak, equity_after)
        drawdown_after = equity_after / equity_peak - 1.0 if equity_peak else 0.0

        trade_count += 1
        if overlay_return <= 0:
            loss_streak += 1
        else:
            loss_streak = 0

        dd_cooldown_threshold = rule.get("drawdown_cooldown_threshold")
        if dd_cooldown_threshold is not None and drawdown_after <= float(dd_cooldown_threshold):
            cooldown_left = max(cooldown_left, int(rule.get("cooldown_signals", 0)))

        result.update(
            {
                "overlay_executed": True,
                "overlay_trade_order": trade_count,
                "overlay_skip_reason": "",
                "overlay_position_scale": scale,
                "overlay_scale_reason": scale_reason,
                "overlay_account_return": overlay_return,
                "overlay_equity_after": equity_after,
                "overlay_drawdown_after": drawdown_after,
            }
        )
        equity = equity_after
        detail_rows.append(result)

    detail = pd.DataFrame(detail_rows)
    executed = detail[detail["overlay_executed"] == True].copy()  # noqa: E712
    returns = executed["overlay_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    final_equity = float(detail["overlay_equity_after"].iloc[-1]) if not detail.empty else initial_cash
    summary = {
        "overlay_name": rule["overlay_name"],
        "description": rule["description"],
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_cash if initial_cash else 0.0,
        "executed_trade_count": int(len(executed)),
        "skipped_count": int((detail["overlay_executed"] != True).sum()),  # noqa: E712
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["overlay_equity_after"]) if len(executed) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
        "reduced_trade_count": int((executed["overlay_position_scale"] < 0.999).sum()) if len(executed) else 0,
    }
    return summary, detail


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["overlay_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["exit_trade_date"].map(normalize_date).str[:4]
    rows = []
    for (overlay_name, year), group in executed.groupby(["overlay_name", "year"]):
        first_equity = float(group["overlay_equity_before"].iloc[0])
        last_equity = float(group["overlay_equity_after"].iloc[-1])
        returns = group["overlay_account_return"].astype(float)
        rows.append(
            {
                "overlay_name": overlay_name,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["overlay_equity_after"]),
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    rows = load_rows(PROJECT_ROOT / args.input)
    summaries = []
    detail_frames = []
    for rule in overlay_rules():
        summary, detail = simulate_overlay(rows, rule)
        summaries.append(summary)
        detail_frames.append(detail)

    summary_report = pd.DataFrame(summaries).sort_values(["max_drawdown", "equity_multiple"], ascending=[False, False])
    detail_report = pd.concat(detail_frames, ignore_index=True)
    yearly_report = build_yearly(detail_report)
    skip_report = (
        detail_report[detail_report["overlay_executed"] != True]  # noqa: E712
        .groupby(["overlay_name", "overlay_skip_reason"])
        .size()
        .reset_index(name="count")
        .sort_values(["overlay_name", "count"], ascending=[True, False])
    )

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    skip_path = output_prefix.with_name(output_prefix.name + "_skips.csv")
    summary_report.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_report.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail_report.to_csv(detail_path, index=False, encoding="utf-8-sig")
    skip_report.to_csv(skip_path, index=False, encoding="utf-8-sig")

    print("bj_cap_2pct 风控叠加回测完成")
    print(
        summary_report[
            [
                "overlay_name",
                "equity_multiple",
                "executed_trade_count",
                "skipped_count",
                "win_rate",
                "max_drawdown",
                "max_loss",
                "reduced_trade_count",
            ]
        ].to_string(index=False)
    )
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- detail: {detail_path}")
    print(f"- skips: {skip_path}")


if __name__ == "__main__":
    main()
