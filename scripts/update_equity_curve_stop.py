#!/usr/bin/env python3
"""收盘流水线⑬：方案甲停手门禁——重算影子净值并写出下一个行动日的判定。

流程：
1. 用与月度研究/认证完全相同的 ``FiveYearResearchDatasetBuilder`` 从
   ``history_start`` 起重建严格as-of研究池（输出到本机临时目录，不进同步盘）；
2. 用正式A/C/E规则回放影子账（不含D、不停手），得到影子净值；
3. 按 ``src.equity_curve_stop`` 的唯一定义判定下一个行动日是否停手；
4. 写出判定文件与影子净值；停手/恢复状态变化时推送Bark；
5. 每周最后一个开市日推送空仓期周报（影子账表现、停手踏空/躲掉、离恢复还差多少、
   失败条件是否触发）。周报只读已算好的影子净值，失败不影响判定。
6. 每个交易日做一次临时复核触发检查（src.review_triggers）：影子账失败线、行情停滞、
   因子健康、实盘连续亏损、实盘与正式规则逐笔对账、检查本身是否算成；命中即单独推送
   “请发起甲·临时复核”。检查失败不影响已写出的判定。

任何一步失败都不写判定文件：次日组合状态机会因判定缺失或过期按fail-closed
不开新仓，并由本脚本推送失败告警。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time
import traceback

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.equity_curve_stop import (  # noqa: E402
    allowed_flags,
    build_shadow_replay,
    current_stop_episode,
    gated_replay,
    decision_for_next_action_date,
    format_weekly_report,
    is_week_last_open_day,
    load_decision,
    completed_month_returns,
    load_regime_buckets,
    monthly_limit_up_mean,
    load_settings,
    load_weekly_report_settings,
    next_open_date,
    open_dates_from_calendar,
    regime_calibration,
    regime_stall_streak,
    utc_now_iso,
    weekly_report_payload,
    write_decision,
    write_shadow_nav,
)
from src.factor_health import (  # noqa: E402
    attach_forward_returns,
    condition_mask,
    load_settings as load_factor_health_settings,
    monthly_edge,
    parse_condition_profiles,
    rolling_health,
)
from src.review_triggers import (  # noqa: E402
    active_acknowledgements,
    alignment_triggers,
    check_failure_trigger,
    factor_health_triggers,
    live_loss_streak_triggers,
    monthly_realized_pnl,
    plan_push,
    regime_stall_triggers,
    shadow_triggers,
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


def refresh_recent_daily_basic(signal_date: str, days: int = 10) -> list[str]:
    """重拉最近几个交易日中关键字段不完整的每日基本面（完整的文件会被跳过）。

    收盘流水线①在15:52左右采集，此时Tushare的量比尚未填充；⑬约在17:50运行，
    这里再拉一次，让研究池与影子账用上已经发布的完整数据。
    """

    from src.data_collector import DataCollector
    from src.data_source import TushareDataSource

    config_path = PROJECT_ROOT / "config" / "config.json"
    collector = DataCollector(data_source=TushareDataSource(config_path=config_path), config_path=config_path)
    calendar_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
    recent = [date for date in open_dates_from_calendar(calendar_path) if date <= str(signal_date)][-int(days):]
    for date in recent:
        collector.collect_daily_basic_by_date(trade_date=date, overwrite=False)
    return list(collector.daily_basic_incomplete_dates)


def check_volume_ratio_coverage(feature_path: Path, signal_date: str, max_null_ratio: float) -> list[str]:
    """研究池量比覆盖率门禁：除最新信号日外，任何一天整列缺失都直接报错。

    2026-07-31~09-30 量比全空却无人察觉，影子账因此缺了依赖量比的E腿规则。
    最新信号日若Tushare仍未发布只告警：它只影响6个交易日之后的停手判定，下次运行会补齐。
    """

    pool = pd.read_csv(feature_path, usecols=["trade_date", "volume_ratio"], dtype={"trade_date": str})
    ratios = pool.groupby(pool["trade_date"].astype(str))["volume_ratio"].apply(lambda s: float(s.isna().mean()))
    bad = [date for date, ratio in ratios.items() if ratio > float(max_null_ratio) and date != str(signal_date)]
    if bad:
        raise RuntimeError(
            f"研究池量比缺失{len(bad)}个交易日（{bad[0]}~{bad[-1]}），影子账会漏算依赖量比的E腿规则；"
            "请先补齐 data/raw/daily_basic 后重跑⑬"
        )
    latest = ratios.get(str(signal_date))
    if latest is not None and latest > float(max_null_ratio):
        print(f"EQUITY_CURVE_STOP_VOLUME_RATIO_PENDING {signal_date} 空值率{latest:.0%}，下次运行补齐", flush=True)
        return [str(signal_date)]
    return []


def build_dataset(settings, signal_date: str) -> Path:
    from src.five_year_research import FiveYearResearchDatasetBuilder

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    try:
        pending = refresh_recent_daily_basic(signal_date)
        if pending:
            print(f"EQUITY_CURVE_STOP_DAILY_BASIC_INCOMPLETE {','.join(pending)}", flush=True)
    except Exception as exc:  # 重拉失败不阻断：由下面的覆盖率门禁决定能否继续
        print(f"EQUITY_CURVE_STOP_DAILY_BASIC_REFRESH_FAILED {type(exc).__name__}: {exc}", flush=True)
    amount = float(config.get("fill_model", {}).get("default_planned_buy_amount", 412_500))
    root = settings.work_dir / "dataset"
    builder = FiveYearResearchDatasetBuilder(research_root=root)
    builder.build_base_tables(start_date=settings.history_start, end_date=signal_date, overwrite=True)
    builder.build_strict_features(amount)
    check_volume_ratio_coverage(
        root / "strict_feature_pool.csv",
        signal_date,
        float((config.get("equity_curve_stop", {}) or {}).get("research_max_volume_ratio_null", 0.5)),
    )
    return root


def notify(title: str, body: str, *, level: str = "active") -> bool:
    """返回是否实际推送成功；调用方据此决定是否写状态（失败则下次重发）。"""
    try:
        from src.notify import notify as _notify

        return bool(_notify("equity_curve_stop", title, body, level=level))
    except Exception:
        traceback.print_exc()
        return False


def compute_factor_health(config, dataset_root: Path, calendar_path: Path) -> dict:
    """用整个涨停池算各腿条件集的滚动优势；任何异常都只记录，返回空表不影响周报。"""

    return compute_factor_health_checked(config, dataset_root, calendar_path)[0]


def compute_factor_health_checked(config, dataset_root: Path, calendar_path: Path) -> tuple[dict, str]:
    """同上，另返回错误信息；临时复核检查据此把“没算成”当作一条必须推送的问题。"""

    try:
        settings = load_factor_health_settings(config)
        if not settings.enabled:
            return {}, ""
        pool_path = dataset_root / "strict_feature_pool.csv"
        if not pool_path.exists():
            return {}, f"研究池不存在：{pool_path}"
        pool = pd.read_csv(pool_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        months_back = int((config.get("factor_health", {}) or {}).get("months_back", 15))
        if len(pool):
            last_month = str(pool["trade_date"].astype(str).max())[:6]
            start = (
                dt.datetime.strptime(last_month + "01", "%Y%m%d") - dt.timedelta(days=31 * months_back)
            ).strftime("%Y%m")
            pool = pool[pool["trade_date"].astype(str).str[:6] >= start]
        calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
        dates = sorted(
            calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str)
        )
        frame = attach_forward_returns(
            pool,
            calendar_dates=dates,
            daily_dir=PROJECT_ROOT / "data" / "raw" / "daily",
            adj_dir=PROJECT_ROOT / "data" / "raw" / "adj_factor",
        )
        strategy_config = load_json_config(PROJECT_ROOT / "config" / "strategy_config.json")
        profiles = parse_condition_profiles(strategy_config)
        out = {}
        for leg, branches in profiles.items():
            if not branches:
                continue
            mask, used = condition_mask(frame, branches)
            item = rolling_health(
                monthly_edge(frame, mask),
                window=settings.window,
                min_periods=settings.min_periods,
                min_samples=settings.min_samples,
                line=settings.lines.get(leg),
                consecutive_months=settings.consecutive_months,
                min_month_samples=settings.min_month_samples,
            )
            item.update(branches_used=used, branches_total=len(branches))
            out[leg] = item
        return out, ""
    except Exception as exc:
        traceback.print_exc()
        print("FACTOR_HEALTH_FAILED", flush=True)
        return {}, f"{type(exc).__name__}: {exc}"


def compute_regime_stall(config, nav, sentiment_path: Path, signal_date: str) -> dict | None:
    """正常行情连续不赚钱的进度（周报与临时复核检查共用）。"""

    section = dict((config.get("equity_curve_stop", {}) or {}).get("weekly_report", {}) or {})
    need_months = int(section.get("regime_stall_months", 3))
    labels = [str(date) for date in nav.index]
    rows = [
        {"ym": ym, "shadow_return": ret, "limit_up_mean": monthly_limit_up_mean(sentiment_path, ym)}
        for ym, ret in completed_month_returns(labels, nav.to_numpy(), count=need_months + 1)
    ]
    return regime_stall_streak(
        rows,
        min_limit_up=float(section.get("regime_stall_min_limit_up", 40)),
        need_months=need_months,
    )


def push_weekly_report(
    config,
    settings,
    nav,
    decision,
    calendar_path: Path,
    signal_date: str,
    sentiment_path: Path | None = None,
    factor_health: dict | None = None,
) -> bool:
    """每周最后一个开市日推送周报；任何异常只记录，不影响已写出的判定。"""

    try:
        weekly = load_weekly_report_settings(config)
        if not weekly.enabled:
            return False
        opens = open_dates_from_calendar(calendar_path)
        if not is_week_last_open_day(opens, signal_date):
            return False
        regime = None
        stall = None
        if sentiment_path is not None:
            buckets, min_months = load_regime_buckets(config)
            labels = [str(date) for date in nav.index]
            month = str(signal_date)[:6]
            start = next((i for i in range(len(labels) - 1, -1, -1) if labels[i][:6] < month), None)
            values = nav.to_numpy()
            shadow_month = float(values[-1] / values[start] - 1.0) if start is not None else None
            regime = regime_calibration(
                monthly_limit_up_mean(sentiment_path, month),
                shadow_month,
                buckets,
                min_months=min_months,
            )
            stall = compute_regime_stall(config, nav, sentiment_path, signal_date)
        acknowledged = active_acknowledgements(
            (config.get("review_triggers", {}) or {}).get("acknowledged"), str(signal_date)
        )
        payload = weekly_report_payload(
            list(nav.index),
            nav.to_numpy(),
            decision,
            weekly,
            ma_window=settings.ma_window,
            lag=settings.lag_trading_days,
            future_open_dates=[date for date in opens if date > str(signal_date)],
            regime=regime,
            regime_stall=stall,
            factor_health=(
                factor_health
                if factor_health is not None
                else compute_factor_health(
                    config,
                    sentiment_path.parent if sentiment_path is not None else PROJECT_ROOT,
                    calendar_path,
                )
            ),
            acknowledged=acknowledged,
        )
        title, body = format_weekly_report(payload)
        notify(title, body, level="timeSensitive" if payload["failures"] else "active")
        print(f"EQUITY_CURVE_STOP_WEEKLY {signal_date} failures={len(payload['failures'])}", flush=True)
        return True
    except Exception:
        traceback.print_exc()
        print(f"EQUITY_CURVE_STOP_WEEKLY_FAILED {signal_date}", flush=True)
        return False


def _live_trades(config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """实盘真实买入（全部腿）与已完整平仓交易（按统一费率估算费用后的净盈亏）。"""

    from src.live_performance import completed_live_trades
    from src.strategy_equity_ledger import _report_config
    from src.strategy_identity import normalize_strategy_frame

    path = PROJECT_ROOT / "reports" / "execution_tracking" / "trade_completion_summary.csv"
    if not path.exists():
        raise FileNotFoundError(f"实盘成交汇总不存在：{path}")
    raw = pd.read_csv(path, dtype={"trade_key": str, "ts_code": str}, low_memory=False)
    if raw.empty:
        return raw, raw
    filled = raw[pd.to_numeric(raw["entry_filled_qty"], errors="coerce").fillna(0).gt(0)].copy()
    filled = normalize_strategy_frame(filled)
    filled["entry_date"] = filled["entry_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    report = _report_config(config)
    report["active_legs"] = sorted({str(leg) for leg in filled["strategy_leg"].fillna("") if str(leg)})
    complete, _quality = completed_live_trades(filled, report)
    return filled, complete


def _completed_months(signal_date: str, count: int = 12) -> list[str]:
    """信号日所在月之前的count个完整自然月（升序）。"""

    first = dt.date(int(signal_date[:4]), int(signal_date[4:6]), 1)
    months: list[str] = []
    for _ in range(count):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        months.append(first.strftime("%Y%m"))
    return sorted(months)


def run_review_trigger_check(
    config,
    settings,
    shadow: dict,
    calendar_path: Path,
    signal_date: str,
    sentiment_path: Path,
    factor_health: dict,
    factor_health_error: str,
) -> bool:
    """每个交易日检查全部临时复核条件；命中就单独推送，返回是否完成（含推送成功）。"""

    section = dict(config.get("review_triggers", {}) or {})
    if not section.get("enabled", False):
        return False
    weekly = load_weekly_report_settings(config)
    nav = shadow["nav"]
    labels = [str(date) for date in nav.index]
    values = nav.to_numpy()
    triggers = []

    def guarded(name: str, func) -> None:
        try:
            triggers.extend(func())
        except Exception as exc:  # 检查自己失败也必须推送，不能当成“没问题”
            traceback.print_exc()
            triggers.append(check_failure_trigger(name, f"{type(exc).__name__}: {exc}"))

    guarded("影子账失败线", lambda: shadow_triggers(
        values,
        window_3m=weekly.window_3m,
        window_6m=weekly.window_6m,
        fail_3m=weekly.shadow_3m_fail,
        fail_6m=weekly.shadow_6m_fail,
        stop_episode=current_stop_episode(
            labels, values, ma_window=settings.ma_window, lag=settings.lag_trading_days
        ),
        worst_missed_gain=weekly.worst_stop_missed_gain,
    ))
    guarded("正常行情连续不赚钱", lambda: regime_stall_triggers(
        compute_regime_stall(config, nav, sentiment_path, signal_date)
    ))
    if factor_health_error:
        triggers.append(check_failure_trigger("因子健康", factor_health_error))
    else:
        triggers.extend(factor_health_triggers(factor_health))

    try:
        filled, complete = _live_trades(config)
    except Exception as exc:
        traceback.print_exc()
        triggers.append(check_failure_trigger("实盘成交汇总", f"{type(exc).__name__}: {exc}"))
    else:
        guarded("实盘连续亏损", lambda: live_loss_streak_triggers(
            monthly_realized_pnl(complete, _completed_months(signal_date)),
            need_months=int(section.get("live_loss_months", 3)),
        ))
        start = str(section.get("alignment_start_date", "") or "")
        if start:
            def _alignment():
                flags = allowed_flags(values, ma_window=settings.ma_window, lag=settings.lag_trading_days)
                return alignment_triggers(
                    gated_replay(shadow, ma_window=settings.ma_window, lag=settings.lag_trading_days),
                    filled,
                    stop_dates={date for date, ok in zip(labels, flags) if not ok},
                    start=start,
                    end=str(signal_date),
                )
            guarded("实盘与正式规则逐笔对账", _alignment)

    state_path = PROJECT_ROOT / str(section.get("state_path", "data/state/review_trigger_state.json"))
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    acknowledged = active_acknowledgements(section.get("acknowledged"), str(signal_date))
    plan = plan_push(
        triggers,
        state,
        today=str(signal_date),
        open_dates=open_dates_from_calendar(calendar_path),
        remind_every_open_days=int(section.get("remind_every_open_days", 5)),
        acknowledged=acknowledged,
    )
    print(
        f"REVIEW_TRIGGERS {signal_date} active={len(triggers)} acknowledged="
        f"{sum(1 for t in triggers if t.key in acknowledged)} push={plan.kind or 'none'}："
        + ("；".join(t.detail for t in triggers) or "无"),
        flush=True,
    )
    if plan.kind and not notify(plan.title, plan.body, level="timeSensitive"):
        print(f"REVIEW_TRIGGERS_PUSH_FAILED {signal_date}（状态未更新，下个交易日重发）", flush=True)
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan.state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_path)
    return True


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
    shadow = build_shadow_replay(
        project_root=PROJECT_ROOT,
        feature_path=dataset_root / "strict_feature_pool.csv",
        sentiment_path=dataset_root / "market_sentiment.csv",
        calendar_path=calendar_path,
        start=settings.history_start,
        end=signal_date,
        work_dir=settings.work_dir,
    )
    nav = shadow["nav"]
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
    sentiment_path = dataset_root / "market_sentiment.csv"
    factor_health, factor_health_error = compute_factor_health_checked(config, dataset_root, calendar_path)
    push_weekly_report(
        config,
        settings,
        nav,
        decision,
        calendar_path,
        signal_date,
        sentiment_path=sentiment_path,
        factor_health=factor_health,
    )
    try:
        run_review_trigger_check(
            config, settings, shadow, calendar_path, signal_date,
            sentiment_path, factor_health, factor_health_error,
        )
    except Exception as exc:  # 判定已写出；检查整体失败也要让用户知道
        traceback.print_exc()
        notify(
            "⚠️ 临时复核触发检查没有完成",
            f"{type(exc).__name__}: {exc}。今天的失败条件没有被检查，次日停手判定不受影响。"
            "请在Claude发送：甲·临时复核（先修好这项检查）。",
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
