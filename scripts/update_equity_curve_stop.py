#!/usr/bin/env python3
"""收盘流水线⑬：方案甲停手门禁——重算影子净值并写出下一个行动日的判定。

流程：
1. 用与月度研究/认证完全相同的 ``FiveYearResearchDatasetBuilder`` 从
   ``history_start`` 起重建严格as-of研究池（输出到本机临时目录，不进同步盘）；
2. 用正式A/C/E规则回放影子账（不含D、不停手），得到影子净值；
3. 按 ``src.equity_curve_stop`` 的唯一定义判定下一个行动日是否停手；
4. 写出判定文件与影子净值；停手/恢复状态变化时推送Bark。

任何一步失败都不写判定文件：次日组合状态机会因判定缺失或过期按fail-closed
不开新仓，并由本脚本推送失败告警。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
import traceback


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.equity_curve_stop import (  # noqa: E402
    build_shadow_nav,
    decision_for_next_action_date,
    load_decision,
    load_settings,
    next_open_date,
    utc_now_iso,
    write_decision,
    write_shadow_nav,
)
from src.utils.config import load_json_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="方案甲停手门禁：重算影子净值并判定下一个行动日")
    parser.add_argument("--signal-date", required=True, help="最新收盘日YYYYMMDD（判定其下一个交易日）")
    parser.add_argument(
        "--reuse-dataset",
        action="store_true",
        help="复用工作目录里已有的研究池（仅用于排查；正式流水线每天全量重建）",
    )
    return parser.parse_args()


def build_dataset(settings, signal_date: str) -> Path:
    from src.five_year_research import FiveYearResearchDatasetBuilder

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    amount = float(config.get("fill_model", {}).get("default_planned_buy_amount", 412_500))
    root = settings.work_dir / "dataset"
    builder = FiveYearResearchDatasetBuilder(research_root=root)
    builder.build_base_tables(start_date=settings.history_start, end_date=signal_date, overwrite=True)
    builder.build_strict_features(amount)
    return root


def notify(title: str, body: str, *, level: str = "active") -> None:
    try:
        from src.notify import notify as _notify

        _notify("equity_curve_stop", title, body, level=level)
    except Exception:
        pass


def main() -> int:
    args = parse_args()
    signal_date = str(args.signal_date)
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    settings = load_settings(config, PROJECT_ROOT)
    if not settings.enabled:
        print("EQUITY_CURVE_STOP_DISABLED", flush=True)
        return 0
    started = time.time()
    calendar_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
    next_date = next_open_date(calendar_path, signal_date)
    dataset_root = settings.work_dir / "dataset"
    if not (args.reuse_dataset and (dataset_root / "strict_feature_pool.csv").exists()):
        dataset_root = build_dataset(settings, signal_date)
    nav = build_shadow_nav(
        project_root=PROJECT_ROOT,
        feature_path=dataset_root / "strict_feature_pool.csv",
        sentiment_path=dataset_root / "market_sentiment.csv",
        calendar_path=calendar_path,
        start=settings.history_start,
        end=signal_date,
        work_dir=settings.work_dir,
    )
    if nav.empty or str(nav.index[-1]) != signal_date:
        raise RuntimeError(
            f"影子净值最后一个行动日={nav.index[-1] if len(nav) else '空'}，不是收盘日{signal_date}"
        )
    previous = load_decision(settings)
    decision = decision_for_next_action_date(
        list(nav.index),
        nav.to_numpy(),
        next_date,
        ma_window=settings.ma_window,
        lag=settings.lag_trading_days,
    )
    decision.update(
        signal_date=signal_date,
        computed_at=utc_now_iso(),
        history_start=settings.history_start,
        elapsed_seconds=round(time.time() - started, 1),
    )
    write_shadow_nav(settings, nav)
    write_decision(settings, decision)
    state = "允许开仓" if decision["allowed"] else "停手"
    print(f"EQUITY_CURVE_STOP {next_date} {state}：{decision['reason']}", flush=True)

    was_allowed = previous.get("allowed") if previous else None
    if was_allowed is not None and bool(was_allowed) != bool(decision["allowed"]):
        if decision["allowed"]:
            notify(
                f"✅ 方案甲恢复开仓：{next_date}",
                f"{decision['reason']}。{next_date}起A/C/E/D按规则正常开仓。",
                level="timeSensitive",
            )
        else:
            notify(
                f"⏸ 方案甲停手：{next_date}起不开新仓",
                f"{decision['reason']}。已有持仓照常到期卖出；影子净值回到均线上方后自动恢复。",
                level="timeSensitive",
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        notify(
            "⛔ 方案甲停手门禁计算失败",
            f"{type(exc).__name__}: {exc}。次日将按fail-closed不开新仓，请检查收盘流水线⑬。",
            level="timeSensitive",
        )
        raise SystemExit(1)
