#!/usr/bin/env python3
"""三级警告冻结阈值的计算（换版时重算；2026-11-26 为 ACE+M+L 新框架写成可复现的脚本）。

阈值的来历见 docs/equity_curve_stop.md 第九节第8小节：用 2019-12-02~截止日的生产回放，取历史极值或分位后冻结。
本脚本用冻结行情（默认 ~/A_System_golden_data/20261110，与生产链对照同一份）把生产代码跑一遍：

  --framework ace_m    复算现行框架的阈值，用来核对算法：必须与 config/config.json 里 strategy_warnings 的现行值一致；
  --framework ace_m_l  算新框架的阈值，写进 strategy_warnings.ace_m_l（由人核对后手工写入配置，本脚本不改配置）。

新框架另外需要 L 的历史首选缓存（晚间信号脚本 scripts/run_strategy_l_signal.py 生成的 l_picks_cache.json）和
Tushare 名称变更记录（判断当时是不是 ST），用 --l-cache、--namechange 指定。

计算内容（全部用生产函数，不另写一套规则）：
- A/C/E 各腿单独按规则（每笔扣 0.2%）最近 12 个月收益：以每一笔为终点逐个算，取最差（终极线）和 5% 分位（E 的中级线）；
- M、L：组合回放里按规则最近 12 个月收益的最低值（中级线固定为 0：转负即中级）；
- 早期提醒：M、L 按规则最长连亏笔数、最近 10 笔逐笔连乘的最差值；
- 组合（按成本记账）历史最深回撤（中级线）；影子账（A/C/E，不停手）历史最深回撤（终极线）；
- 组合最近 12 个月比只用 ACE 落后的最长连续交易日数（参考）；信号断流：2020-04-21 以后最长连续没有任何信号的行动日数；
- 新框架另核对：生产算出的方案甲停手日与研究回放输入（--research-inputs，可选）逐日对照。

只读：不联网、不推送、不写生产状态；运行时把当前代码复制到临时目录（同 tests/golden/golden_master.py 的做法）。
用法：python research/ace_m_l_benchmark/build_warning_thresholds.py --framework ace_m_l --l-cache PATH --namechange PATH
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODE_DIRS = ("src", "scripts", "config")
MODEL_TABLES = ("data/processed/fill_rate_table.csv", "data/processed/fill_rate_fallback.csv")
DEFAULT_DATA = Path.home() / "A_System_golden_data" / "20261110"
START = "20191202"
GAP_FROM = "20200421"   # 更早那段封单金额放大1000倍的数据错误不算（见 config strategy_warnings.file_role）


def runtime_root(args, tmp: Path) -> Path:
    root = tmp / "root"
    for name in CODE_DIRS:
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "data" / "processed").mkdir(parents=True)
    for rel in MODEL_TABLES:
        shutil.copy2(REPO / rel, root / rel)
    raw = root / "data" / "raw"
    raw.mkdir(parents=True)
    for child in (args.data / "raw").iterdir():
        (raw / child.name).symlink_to(child)
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    config["portfolio_framework"] = args.framework
    if args.framework == "ace_m_l":
        (raw / "namechange").mkdir()
        (raw / "namechange" / "namechange_all.csv").symlink_to(args.namechange)
        (root / "reports" / "strategy_l").mkdir(parents=True)
        shutil.copy2(args.l_cache, root / "reports" / "strategy_l" / "l_picks_cache.json")
    (root / "config" / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.path.insert(0, str(root))
    os.chdir(root)
    return root


def trailing_series(trades, *, window_days=365, min_trades=10):
    """以每一笔为终点的最近 window_days 天逐笔复利（不足 min_trades 笔不算），按终点日期排列。"""
    from src.strategy_warnings import trailing_return

    ordered = sorted(trades)
    out = []
    for i in range(len(ordered)):
        item = trailing_return(ordered[: i + 1], window_days=window_days, min_trades=min_trades)
        if item["value"] is not None:
            out.append((item["as_of"], float(item["value"])))
    return out


def early_stats(trades, window=10):
    ordered = sorted(trades)
    longest = streak = 0
    for _day, ret in ordered:
        streak = streak + 1 if ret < 0 else 0
        longest = max(longest, streak)
    worst = None
    for i in range(window, len(ordered) + 1):
        value = 1.0
        for _day, ret in ordered[i - window:i]:
            value *= 1.0 + ret
        value -= 1.0
        if worst is None or value < worst[1]:
            worst = (ordered[i - 1][0], value)
    return {"trades": len(ordered), "max_loss_streak": longest, "worst_window_return": worst}


def deepest_drawdown(series):
    values = series.astype(float)
    dd = values / values.cummax() - 1
    trough = dd.idxmin()
    peak = values.loc[:trough].idxmax()
    return {"value": float(dd.min()), "peak": str(peak), "trough": str(trough)}


def longest_behind(live, base, window=252):
    import pandas as pd

    joined = pd.concat([live.astype(float), base.astype(float)], axis=1, keys=["live", "base"]).dropna()
    rel = ((joined["live"] / joined["live"].shift(window)) / (joined["base"] / joined["base"].shift(window)) - 1).dropna()
    longest = run = 0
    for value in rel.tolist():
        run = run + 1 if value < 0 else 0
        longest = max(longest, run)
    return longest


def longest_gap(days, has_signal):
    longest = gap = 0
    for day in days:
        if day < GAP_FROM:
            continue
        gap = 0 if has_signal(day) else gap + 1
        longest = max(longest, gap)
    return longest


def pct(rows, q):
    """分位数，numpy 默认线性插值（与 2026-11-13 冻结现行阈值时的取法一致，由 --framework ace_m 复算核对）。"""
    import numpy as np

    values = [v for _d, v in rows]
    return float(np.percentile(values, q * 100)) if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", choices=("ace_m", "ace_m_l"), required=True)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--end", default="20261110")
    parser.add_argument("--l-cache", type=Path)
    parser.add_argument("--namechange", type=Path)
    parser.add_argument("--research-inputs", type=Path, help="研究回放输入（含 allowed、l_allowed），新框架逐日对照用")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.framework == "ace_m_l" and not (args.l_cache and args.namechange):
        parser.error("新框架需要 --l-cache 和 --namechange")
    args.data = args.data.resolve()
    if args.l_cache:
        args.l_cache = args.l_cache.resolve()
    if args.namechange:
        args.namechange = args.namechange.resolve()
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        root = runtime_root(args, tmp)
        result = compute(args, root, tmp)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


def compute(args, root: Path, tmp: Path) -> dict:
    import scripts.update_equity_curve_stop as prod
    from src.ace_m_replay import RawPrices, ace_plans_by_day, make_m_outcome, replay_ace_m, replay_ace_m_l
    from src.equity_curve_stop import allowed_flags, load_settings
    from src.strategy_m import m_sort_for_config
    from src.strategy_m_reference import MReference
    from src.strategy_warnings import _cached_m_inputs, _l_replay_inputs, leg_trades, load_warning_settings
    from src.utils.config import load_json_config

    config = load_json_config(root / "config" / "config.json")
    settings = load_settings(config, root)
    raw = root / "data" / "raw"
    ds = args.data / "dataset"
    shadow = prod.build_shadow_replay(project_root=root, feature_path=ds / "strict_feature_pool.csv",
                                      sentiment_path=ds / "market_sentiment.csv", calendar_path=raw / "trade_calendar.csv",
                                      start=settings.history_start, end=args.end, work_dir=tmp / "work")
    nav = shadow["nav"]
    flags = allowed_flags(nav.to_numpy(), ma_window=settings.ma_window, lag=settings.lag_trading_days)
    ace_allowed = {str(d): bool(ok) for d, ok in zip(nav.index, flags)}
    days = [str(d) for d in shadow["action_dates"] if START <= str(d) <= args.end]
    reference = MReference(raw, sort_by=m_sort_for_config(config))
    open_days = reference.open_days()
    candidates, errors, outcome, _save = _cached_m_inputs(
        reference, make_m_outcome(RawPrices(raw), open_days), open_days, days, None)
    analysis = dict(config.get("analysis", {}) or {})
    fees = {"commission_rate": float(analysis.get("commission_rate", 0.0003)),
            "minimum_commission": float(analysis.get("minimum_commission", 5.0)),
            "transfer_rate": float(analysis.get("transfer_fee_rate", 0.00001)),
            "stamp_schedule": analysis.get("stamp_tax_schedule")}
    plans = ace_plans_by_day(shadow["legs"])
    l_picks = None
    if args.framework == "ace_m_l":
        l_picks, l_outcome, l_allowed = _l_replay_inputs(root, raw, open_days, args.end)
        live, trades = replay_ace_m_l(days, plans, ace_allowed, candidates, outcome, l_picks, l_outcome, l_allowed, **fees)
    else:
        live, trades = replay_ace_m(days, plans, ace_allowed, candidates, outcome, **fees)
    ace_only, _ = replay_ace_m(days, plans, ace_allowed, {}, outcome, use_m=False, ace_halted_weight=0.0, **fees)

    cost = load_warning_settings({"strategy_warnings": dict(config.get("strategy_warnings", {}), enabled=False)}).trade_cost
    legs = leg_trades(shadow["legs"], cost=cost)
    out = {"framework": args.framework, "window": [days[0], days[-1]], "m_reference_error_days": len(errors),
           "combo_multiple": float(live.iloc[-1] / live.iloc[0]), "ace_only_multiple": float(ace_only.iloc[-1] / ace_only.iloc[0]),
           "trades": {fam: int((trades["family"] == fam).sum()) for fam in sorted(set(trades["family"]))}}
    for leg in ("A", "C", "E"):
        rows = [(d, r) for d, r in legs[leg] if d >= START]
        series = trailing_series(rows)
        worst = min(series, key=lambda x: x[1]) if series else None
        out["leg_%s" % leg] = {"worst_12m": worst, "p05_12m": pct(series, 0.05), "trades": len(rows)}
    families = ("M", "L") if args.framework == "ace_m_l" else ("M",)
    for fam in families:
        part = trades[trades["family"] == fam]
        rows = list(zip(part["action_date"].astype(str), part["ret"].astype(float)))
        series = trailing_series(rows)
        out["family_%s" % fam] = {"min_12m": min(series, key=lambda x: x[1]) if series else None,
                                  "early": early_stats(rows)}
    out["combo_drawdown"] = deepest_drawdown(live)
    out["shadow_drawdown"] = deepest_drawdown(nav[[str(d) >= START for d in nav.index]])
    out["longest_behind_ace_only"] = longest_behind(live, ace_only)
    leg_days = {leg: {str(d) for d in shadow["legs"][leg]["action_date"]} for leg in ("A", "C", "E")
                if shadow["legs"].get(leg) is not None and not shadow["legs"][leg].empty}
    out["longest_signal_gap"] = longest_gap(days, lambda d: any(d in s for s in leg_days.values())
                                            or bool(candidates.get(d)) or bool((l_picks or {}).get(d)))
    out["stop_days"] = sum(1 for d in days if not ace_allowed.get(d, True))
    if args.framework == "ace_m_l" and args.research_inputs:
        research = pickle.load(open(args.research_inputs, "rb"))
        diff = [d for d in days if d in research["allowed"] and bool(research["allowed"][d]) != ace_allowed.get(d)]
        diff_l = [d for d in days if d in research["l_allowed"] and bool(research["l_allowed"][d]) != l_allowed.get(d)]
        out["research_check"] = {"ace_allowed_days_compared": sum(1 for d in days if d in research["allowed"]),
                                 "ace_allowed_diff": diff[:20], "ace_allowed_diff_count": len(diff),
                                 "l_allowed_days_compared": sum(1 for d in days if d in research["l_allowed"]),
                                 "l_allowed_diff": diff_l[:20], "l_allowed_diff_count": len(diff_l)}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
