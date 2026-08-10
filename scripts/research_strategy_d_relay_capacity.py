#!/usr/bin/env python3
"""评估8笔D接力在不同资金规模下的集合竞价容量与收益敏感性。

本脚本是研究工具，不读取账户、不下单，也不会修改实盘配置。它先回答两个问题：

1. 09:23整仓D卖单占当时虚拟匹配量多少，哪些资金规模还能安全整仓接力；
2. D卖价被自身冲击0.5%~10%时，8笔接力和完整132笔组合会损失多少。

只有tick和一分钟数据16个角色（8笔×D/NEXT）全部完整时才允许出容量结论。
容量不足的样本标记为 ``PAIRED_POV_REQUIRED``，留给下一阶段的“卖D确认资金后
再买A/C/E2”资金中性成对POV回放；数据异常则标记 ``CANCEL_RELAY``。

Windows采集完成后运行：

    py -3.11 scripts\research_strategy_d_relay_capacity.py

输出：

    reports/strategy_d/relay_capacity/d_relay_capacity_detail.csv
    reports/strategy_d/relay_capacity/d_relay_capacity_summary.csv
    reports/strategy_d/relay_capacity/d_relay_impact_sensitivity.csv
    reports/strategy_d/relay_capacity/d_relay_capacity_report.md
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_current_executable_portfolio import POSITION_PCT  # noqa: E402
from scripts.research_strategy_d_relay_fetch import (  # noqa: E402
    EXPECTED_RELAY_COUNT,
    ONE_MINUTE_PATH,
    TICK_PATH,
    complete_one_minute_keys,
    complete_tick_keys,
    load_existing,
    load_relay_targets,
)
from scripts.research_strategy_d_relay_tushare_fetch import (  # noqa: E402
    AUCTION_PROXY_PATH,
    complete_auction_proxy_keys,
)


PORTFOLIO_PATH = (
    PROJECT_ROOT / "reports" / "current_portfolio_alignment" / "portfolio_trades.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_d" / "relay_capacity"
DETAIL_PATH = OUTPUT_DIR / "d_relay_capacity_detail.csv"
SUMMARY_PATH = OUTPUT_DIR / "d_relay_capacity_summary.csv"
SENSITIVITY_PATH = OUTPUT_DIR / "d_relay_impact_sensitivity.csv"
REPORT_PATH = OUTPUT_DIR / "d_relay_capacity_report.md"
# 2026-08-07 A/C改用逐日独立候选后组合笔数变化（旧口径A/C被裁时为132）。
EXPECTED_PORTFOLIO_COUNT = 135
EXPECTED_PORTFOLIO_MULTIPLE = 7677.946823375038
DEFAULT_POSITION_AMOUNTS = "250000,500000,1000000,3000000,5000000,10000000"


def parse_amounts(raw: str) -> tuple[float, ...]:
    values = tuple(float(value.strip()) for value in str(raw).split(",") if value.strip())
    if not values or any(value <= 0 for value in values):
        raise ValueError("资金规模必须是逗号分隔的正数")
    return values


def floor_lot(quantity: float, lot_size: int = 100) -> int:
    if not np.isfinite(quantity) or quantity <= 0:
        return 0
    return int(quantity // lot_size) * lot_size


def compound(returns: pd.Series) -> float:
    return float((1.0 + pd.to_numeric(returns, errors="raise")).prod())


def load_portfolio() -> pd.DataFrame:
    portfolio = pd.read_csv(PORTFOLIO_PATH, low_memory=False)
    if len(portfolio) != EXPECTED_PORTFOLIO_COUNT:
        raise ValueError(
            f"组合必须为{EXPECTED_PORTFOLIO_COUNT}笔，实际{len(portfolio)}笔"
        )
    portfolio["account_return"] = pd.to_numeric(
        portfolio["account_return"], errors="raise"
    )
    multiple = compound(portfolio["account_return"])
    if abs(multiple - EXPECTED_PORTFOLIO_MULTIPLE) > 1e-8:
        raise ValueError(f"完整组合复利基准漂移：{multiple}")
    return portfolio


def validate_inputs(
    targets: pd.DataFrame,
    tick: pd.DataFrame,
    one_minute: pd.DataFrame,
) -> None:
    """8笔×2角色必须全部完整；缺一个角色就禁止认证。"""

    expected_keys = {
        f"{row.signal_date}|{role}"
        for row in targets.itertuples(index=False)
        for role in ("D", "NEXT")
    }
    tick_keys = complete_tick_keys(tick)
    one_keys = complete_one_minute_keys(one_minute)
    if tick_keys != expected_keys:
        raise ValueError(
            f"D接力tick不完整：缺少{sorted(expected_keys-tick_keys)}，"
            f"多出{sorted(tick_keys-expected_keys)}"
        )
    if one_keys != expected_keys:
        raise ValueError(
            f"D接力1分钟数据不完整：缺少{sorted(expected_keys-one_keys)}，"
            f"多出{sorted(one_keys-expected_keys)}"
        )


def validate_proxy_inputs(
    targets: pd.DataFrame,
    auction_proxy: pd.DataFrame,
    one_minute: pd.DataFrame,
) -> None:
    """无历史tick时，要求16个分钟角色和8笔最终竞价代理全部完整。"""

    expected_roles = {
        f"{row.signal_date}|{role}"
        for row in targets.itertuples(index=False)
        for role in ("D", "NEXT")
    }
    one_keys = complete_one_minute_keys(one_minute)
    if one_keys != expected_roles:
        raise ValueError(
            f"D接力1分钟数据不完整：缺少{sorted(expected_roles-one_keys)}，"
            f"多出{sorted(one_keys-expected_roles)}"
        )
    expected_signals = set(targets["signal_date"].astype(str))
    proxy_keys = complete_auction_proxy_keys(auction_proxy)
    if proxy_keys != expected_signals:
        raise ValueError(
            f"D接力竞价容量代理不完整：缺少{sorted(expected_signals-proxy_keys)}，"
            f"多出{sorted(proxy_keys-expected_signals)}"
        )


def snapshot_at_0923(group: pd.DataFrame) -> pd.Series:
    """取09:23及以前最后一个有效竞价快照，不使用09:23之后数据。"""

    frame = group.copy()
    frame["hhmm"] = frame["hhmm"].astype(str).str.zfill(4)
    frame = frame[frame["hhmm"].between("0915", "0923")].copy()
    for column in (
        "bid_price_1",
        "ask_price_1",
        "bid_volume_1",
        "ask_volume_1",
        "bid_volume_2",
        "ask_volume_2",
        "pre_close",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.empty:
        raise ValueError(f"{group.iloc[0]['signal_date']} D缺少09:23前竞价快照")
    # 数据存在但价格/数量异常时仍返回最后快照，由容量分流明确标记CANCEL_RELAY；
    # 不能在这里抛错后让其他7笔也失去诊断结果。
    return frame.sort_values(["hhmm", "bar_time"]).iloc[-1]


def infer_book_volume_unit(tick: pd.DataFrame) -> int:
    """用QMT原始成交量与标准成交量比值识别盘口数量单位。

    部分QMT版本的volume按手、pvolume按股，盘口bidVol/askVol也随volume按手；
    另一些版本直接按股。只有中位比值明确接近1或100才接受，模糊时失败关闭。
    """

    required = {"volume", "pvolume"}
    if tick.empty or not required.issubset(tick.columns):
        raise ValueError(
            "tick缺少volume/pvolume，无法自动确认盘口数量单位；"
            "请重新采集，或核对QMT原始字段后显式传--book-volume-unit"
        )
    volume = pd.to_numeric(tick["volume"], errors="coerce")
    pvolume = pd.to_numeric(tick["pvolume"], errors="coerce")
    ratios = (pvolume[volume.gt(0)] / volume[volume.gt(0)]).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    ratios = ratios[ratios.gt(0)]
    if ratios.empty:
        raise ValueError("volume/pvolume没有正值，无法自动确认盘口数量单位")
    median_ratio = float(ratios.median())
    if 0.8 <= median_ratio <= 1.2:
        return 1
    if 80.0 <= median_ratio <= 120.0:
        return 100
    raise ValueError(
        f"QMT成交量单位比值中位数={median_ratio:.4f}，既不接近1也不接近100，"
        "禁止猜测盘口数量单位"
    )


def infer_auction_snapshot(
    row: pd.Series,
    *,
    book_volume_unit: int,
) -> dict[str, float]:
    """按开放式集合竞价行情映射读取参考价、匹配量与未匹配量。

    竞价时买一/卖一价格应同时显示虚拟参考价，买一/卖一数量显示匹配量；
    买二或卖二数量显示对应方向未匹配量。这里只读取真实行情，不推测未来开盘。
    """

    bid_price = float(row["bid_price_1"])
    ask_price = float(row["ask_price_1"])
    reference = (bid_price + ask_price) / 2.0
    price_spread_pct = abs(bid_price - ask_price) / max(reference, 0.01)
    match_raw = min(float(row["bid_volume_1"]), float(row["ask_volume_1"]))
    matched_qty = max(match_raw * book_volume_unit, 0.0)
    unmatched_buy_qty = max(float(row.get("bid_volume_2", 0.0) or 0.0), 0.0) * book_volume_unit
    unmatched_sell_qty = max(float(row.get("ask_volume_2", 0.0) or 0.0), 0.0) * book_volume_unit
    return {
        "snapshot_hhmm": str(row["hhmm"]),
        "auction_reference_price": reference,
        "auction_price_spread_pct": price_spread_pct,
        "matched_qty": matched_qty,
        "matched_amount": matched_qty * reference,
        "unmatched_buy_qty": unmatched_buy_qty,
        "unmatched_sell_qty": unmatched_sell_qty,
        "unmatched_sell_to_match": unmatched_sell_qty / matched_qty if matched_qty > 0 else np.inf,
    }


def build_capacity_replay(
    targets: pd.DataFrame,
    tick: pd.DataFrame,
    *,
    position_amounts: tuple[float, ...],
    book_volume_unit: int,
    max_auction_participation: float,
    max_sell_unmatched_ratio: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐笔逐资金档判断整仓竞价、成对POV或取消接力。"""

    d_tick = tick[tick["role"].astype(str).eq("D")].copy()
    tick_groups = {
        str(key): group.copy() for key, group in d_tick.groupby("signal_date")
    }
    rows: list[dict[str, Any]] = []
    for target in targets.itertuples(index=False):
        snapshot = infer_auction_snapshot(
            snapshot_at_0923(tick_groups[str(target.signal_date)]),
            book_volume_unit=book_volume_unit,
        )
        valid_snapshot = bool(
            snapshot["auction_reference_price"] > 0
            and snapshot["matched_qty"] > 0
            and snapshot["auction_price_spread_pct"] <= 0.001
        )
        sell_imbalance_ok = bool(
            snapshot["unmatched_sell_to_match"] <= max_sell_unmatched_ratio
        )
        safe_qty = floor_lot(snapshot["matched_qty"] * max_auction_participation)
        for amount in position_amounts:
            target_qty = floor_lot(amount / float(target.d_entry_price))
            exit_mark = target_qty * snapshot["auction_reference_price"]
            participation = (
                target_qty / snapshot["matched_qty"]
                if snapshot["matched_qty"] > 0
                else np.inf
            )
            if not valid_snapshot:
                action = "CANCEL_RELAY"
                reason = "竞价参考价或匹配量无效，失败关闭"
                auction_sell_qty = 0
            elif not sell_imbalance_ok:
                action = "CANCEL_RELAY"
                reason = "卖方未匹配量过大，禁止在弱竞价中增加卖压"
                auction_sell_qty = 0
            elif target_qty <= safe_qty:
                action = "FULL_AUCTION_RELAY"
                reason = "整仓D不超过虚拟匹配量安全比例，允许保持原接力"
                auction_sell_qty = target_qty
            else:
                action = "PAIRED_POV_REQUIRED"
                reason = "整仓超过竞价安全容量，只允许安全部分参与竞价"
                auction_sell_qty = min(target_qty, safe_qty)
            paired_pov_sell_qty = max(target_qty - auction_sell_qty, 0)
            rows.append(
                {
                    "signal_date": target.signal_date,
                    "relay_date": target.relay_date,
                    "strategy_leg": target.strategy_leg,
                    "d_ts_code": target.d_ts_code,
                    "d_name": target.d_name,
                    "next_ts_code": target.next_ts_code,
                    "next_name": target.next_name,
                    "position_amount": amount,
                    "account_equity_at_82_5pct": amount / POSITION_PCT,
                    "target_qty": target_qty,
                    "exit_mark_amount": exit_mark,
                    "safe_auction_qty": safe_qty,
                    "auction_sell_qty": auction_sell_qty,
                    "auction_sell_amount": (
                        auction_sell_qty * snapshot["auction_reference_price"]
                    ),
                    "paired_pov_sell_qty": paired_pov_sell_qty,
                    "auction_sell_fraction": (
                        auction_sell_qty / target_qty if target_qty > 0 else 0.0
                    ),
                    "auction_participation": participation,
                    "book_volume_unit": book_volume_unit,
                    **snapshot,
                    "sell_imbalance_ok": sell_imbalance_ok,
                    "recommended_action": action,
                    "reason": reason,
                }
            )
    detail = pd.DataFrame(rows)
    summary = (
        detail.groupby("position_amount", as_index=False)
        .agg(
            account_equity_at_82_5pct=("account_equity_at_82_5pct", "first"),
            samples=("signal_date", "size"),
            full_auction_count=(
                "recommended_action",
                lambda values: int((values == "FULL_AUCTION_RELAY").sum()),
            ),
            paired_pov_count=(
                "recommended_action",
                lambda values: int((values == "PAIRED_POV_REQUIRED").sum()),
            ),
            cancel_count=(
                "recommended_action",
                lambda values: int((values == "CANCEL_RELAY").sum()),
            ),
            mean_auction_participation=("auction_participation", "mean"),
            median_auction_participation=("auction_participation", "median"),
            max_auction_participation=("auction_participation", "max"),
            mean_auction_sell_fraction=("auction_sell_fraction", "mean"),
            min_auction_sell_fraction=("auction_sell_fraction", "min"),
        )
        .sort_values("position_amount")
    )
    summary["full_auction_rate"] = summary["full_auction_count"] / summary["samples"]
    summary["paired_pov_rate"] = summary["paired_pov_count"] / summary["samples"]
    return detail, summary


def build_capacity_replay_from_proxy(
    targets: pd.DataFrame,
    auction_proxy: pd.DataFrame,
    *,
    position_amounts: tuple[float, ...],
    max_auction_participation: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """用09:30单一价格bar回放最终竞价容量，不伪造09:23未匹配量。"""

    fake_tick_rows: list[dict[str, Any]] = []
    proxy_by_signal = auction_proxy.set_index(
        auction_proxy["signal_date"].astype(str),
        drop=False,
    )
    for target in targets.itertuples(index=False):
        proxy = proxy_by_signal.loc[str(target.signal_date)]
        if isinstance(proxy, pd.DataFrame):
            proxy = proxy.iloc[-1]
        price = float(proxy["auction_reference_price"])
        quantity = float(proxy["matched_qty"])
        fake_tick_rows.append(
            {
                "signal_date": target.signal_date,
                "relay_date": target.relay_date,
                "role": "D",
                "ts_code": target.d_ts_code,
                "bar_time": str(target.relay_date) + "093000",
                "hhmm": "0923",
                "bid_price_1": price,
                "ask_price_1": price,
                "bid_volume_1": quantity,
                "ask_volume_1": quantity,
                "bid_volume_2": 0.0,
                "ask_volume_2": 0.0,
                "pre_close": price,
            }
        )
    detail, summary = build_capacity_replay(
        targets,
        pd.DataFrame(fake_tick_rows),
        position_amounts=position_amounts,
        book_volume_unit=1,
        max_auction_participation=max_auction_participation,
        # 最终竞价代理没有未匹配量，因此这里只做容量回放；实盘仍必须读取
        # 09:23真实卖方未匹配量，缺失时取消接力。
        max_sell_unmatched_ratio=1.0,
    )
    detail["auction_snapshot_source"] = "TUSHARE_0930_FINAL_PROXY"
    detail["unmatched_volume_available"] = False
    detail["proxy_note"] = (
        "09:30单一价格bar仅代理最终竞价容量；不含09:23未匹配量，不能单独认证实盘"
    )
    return detail, summary


def build_impact_sensitivity(
    targets: pd.DataFrame,
    portfolio: pd.DataFrame,
) -> pd.DataFrame:
    """把D卖价额外冲击带入8笔接力并复算132笔组合。"""

    baseline_relay = compound(targets["combined_account_return"])
    baseline_portfolio = compound(portfolio["account_return"])
    rows: list[dict[str, Any]] = []
    for impact in (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10):
        d_after_impact = targets["d_t1_account_return"] - POSITION_PCT * impact
        combined_after = (
            (1.0 + d_after_impact) * (1.0 + targets["next_account_return"]) - 1.0
        )
        relay_multiple = compound(combined_after)
        portfolio_multiple = baseline_portfolio / baseline_relay * relay_multiple
        rows.append(
            {
                "additional_d_price_impact": impact,
                "relay_multiple": relay_multiple,
                "relay_change": relay_multiple / baseline_relay - 1.0,
                "portfolio_multiple": portfolio_multiple,
                "portfolio_change": portfolio_multiple / baseline_portfolio - 1.0,
            }
        )
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    view = frame.copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(
                lambda value: "" if pd.isna(value) else f"{float(value):.6f}"
            )
        else:
            view[column] = view[column].fillna("").astype(str)
    lines = [
        "| " + " | ".join(view.columns) + " |",
        "| " + " | ".join(["---"] * len(view.columns)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in view.astype(str).values)
    return "\n".join(lines)


def write_report(
    summary: pd.DataFrame,
    sensitivity: pd.DataFrame,
    *,
    book_volume_unit: int,
    max_auction_participation: float,
    max_sell_unmatched_ratio: float,
    snapshot_source: str,
    proxy_haircut: float,
) -> None:
    is_proxy = snapshot_source == "TUSHARE_0930_FINAL_PROXY"
    volume_note = (
        "- 成交量统一按股；代理数据用成交额÷单一成交价反推，规避QMT股/手差异。"
        if is_proxy
        else f"- QMT盘口数量单位：{book_volume_unit}股/原始单位。"
    )
    imbalance_note = (
        "- 代理不含买卖未匹配量；历史只筛容量，实盘缺少09:23未匹配盘口时必须取消接力。"
        if is_proxy
        else f"- 卖方未匹配量上限：虚拟匹配量×{max_sell_unmatched_ratio:.1%}。"
    )
    timing_note = (
        "- 09:30最终竞价代理只用于历史容量筛查，不作为09:23实盘可见数据。"
        if is_proxy
        else "- 09:23只使用当时已见快照，不使用09:25真实开盘结果倒推决策。"
    )
    lines = [
        "# D接力集合竞价容量研究",
        "",
        "## 固定口径",
        "",
        f"- 锁定样本：{EXPECTED_RELAY_COUNT}笔（D→A 2笔、D→C 6笔、D→E2 0笔）。",
        volume_note,
        f"- 容量快照来源：{snapshot_source}。",
        f"- 整仓竞价安全上限：虚拟匹配量×{max_auction_participation:.1%}。",
        imbalance_note,
        timing_note,
        "- 数据异常一律CANCEL_RELAY；容量不足进入PAIRED_POV_REQUIRED，禁止整仓跌停价卖。",
        "",
        "## 不同D仓位金额的容量分流",
        "",
        markdown_table(summary),
        "",
        "## D卖价额外冲击敏感性",
        "",
        markdown_table(sensitivity),
        "",
        "## 下一阶段",
        "",
        "- FULL_AUCTION_RELAY：保留现有小仓位整仓接力。",
        "- PAIRED_POV_REQUIRED：继续回放09:30~10:30卖D确认资金后再买A/C/E2。",
        "- CANCEL_RELAY：数据不可验证或价格保护失败时，D恢复普通T+2退出。",
        "- 本报告不等同于实盘成交承诺；样本仅8笔，不能据此无限调参。",
    ]
    if is_proxy:
        lines[7:7] = [
            f"- 代理折扣：原参与率再乘{proxy_haircut:.1%}；代理只用于历史容量筛查。",
            "- 代理没有09:23未匹配量，不能取代实盘当时盘口门禁。",
        ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D接力集合竞价容量与收益冲击回放")
    parser.add_argument("--tick", type=Path, default=TICK_PATH)
    parser.add_argument("--one-minute", type=Path, default=ONE_MINUTE_PATH)
    parser.add_argument("--auction-proxy", type=Path, default=AUCTION_PROXY_PATH)
    parser.add_argument("--position-amounts", default=DEFAULT_POSITION_AMOUNTS)
    parser.add_argument(
        "--book-volume-unit",
        choices=("auto", "1", "100"),
        default="auto",
        help="QMT bidVol/askVol原始数量单位；默认从pvolume/volume自动核验",
    )
    parser.add_argument("--max-auction-participation", type=float, default=0.05)
    parser.add_argument("--max-sell-unmatched-ratio", type=float, default=0.05)
    parser.add_argument(
        "--proxy-haircut",
        type=float,
        default=0.50,
        help="使用09:30最终竞价代理时对安全参与率再打折，默认50%",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 < args.max_auction_participation <= 0.20:
        raise ValueError("竞价安全参与率必须在0~20%之间")
    if not 0 <= args.max_sell_unmatched_ratio <= 1.0:
        raise ValueError("卖方未匹配比例必须在0~100%之间")
    if not 0 < args.proxy_haircut <= 1.0:
        raise ValueError("竞价代理折扣必须在0~100%之间")
    targets = load_relay_targets()
    portfolio = load_portfolio()
    tick = load_existing(args.tick)
    one_minute = load_existing(args.one_minute)
    auction_proxy = load_existing(args.auction_proxy)
    expected_tick_keys = {
        f"{row.signal_date}|{role}"
        for row in targets.itertuples(index=False)
        for role in ("D", "NEXT")
    }
    if complete_tick_keys(tick) == expected_tick_keys:
        validate_inputs(targets, tick, one_minute)
        book_volume_unit = (
            infer_book_volume_unit(tick)
            if args.book_volume_unit == "auto"
            else int(args.book_volume_unit)
        )
        effective_participation = args.max_auction_participation
        snapshot_source = "QMT_0923_TICK"
        detail, summary = build_capacity_replay(
            targets,
            tick,
            position_amounts=parse_amounts(args.position_amounts),
            book_volume_unit=book_volume_unit,
            max_auction_participation=effective_participation,
            max_sell_unmatched_ratio=args.max_sell_unmatched_ratio,
        )
    else:
        validate_proxy_inputs(targets, auction_proxy, one_minute)
        book_volume_unit = 1
        effective_participation = (
            args.max_auction_participation * args.proxy_haircut
        )
        snapshot_source = "TUSHARE_0930_FINAL_PROXY"
        detail, summary = build_capacity_replay_from_proxy(
            targets,
            auction_proxy,
            position_amounts=parse_amounts(args.position_amounts),
            max_auction_participation=effective_participation,
        )
    sensitivity = build_impact_sensitivity(targets, portfolio)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail.to_csv(DETAIL_PATH, index=False, encoding="utf-8-sig")
    summary.to_csv(SUMMARY_PATH, index=False, encoding="utf-8-sig")
    sensitivity.to_csv(SENSITIVITY_PATH, index=False, encoding="utf-8-sig")
    write_report(
        summary,
        sensitivity,
        book_volume_unit=book_volume_unit,
        max_auction_participation=effective_participation,
        max_sell_unmatched_ratio=args.max_sell_unmatched_ratio,
        snapshot_source=snapshot_source,
        proxy_haircut=args.proxy_haircut,
    )
    print("D接力容量研究完成")
    print(summary.to_string(index=False))
    print("\n卖价冲击敏感性")
    print(sensitivity.to_string(index=False))


if __name__ == "__main__":
    main()
