#!/usr/bin/env python3
"""生产链对照标准（2026-11-14，项目优化第1步“安全网”）。

用一份冻结数据（默认 ~/A_System_golden_data/20261110：Tushare原始行情 + 严格研究池，带逐文件校验清单）
把生产链跑一遍，把业务输出和 tests/golden/expected/ 里记下的对照标准逐字节比较：

0. 研究池：用当前代码从冻结原始行情重建严格研究池和市场情绪表（数据清洗、情绪、成交概率、严格特征都在这一段），
   后面各项都用重建出来的研究池，所以是从原始行情一路锁到计划和日报；
1. 影子账：每日净值、A/C/E每条计划（停手判定、逐笔对账、三级警告共用的那条链）；
2. 停手判定：每个行动日是否允许开仓（60日均线、滞后6日）；
3. 实盘组合状态机：同一批候选交给 CombinedLiveEngine，每个行动日下哪一笔单、D是否授权；
   实盘收盘流水线⑥的A/C选股函数逐日跑同一份研究池，选出的股票必须和影子账计划逐日相同；
4. M候选单（QMT外重算，src.strategy_m_reference）：check时抽查约十分之一的日子加最近30天，--full 全部重算；
5. 三级警告用到的全部数值（ACE+M按现行规则和仓位的回放、各腿近12个月、回撤、信号断流）和推出来的警告；
6. 因子健康（A/C/E）；
7. 策略日报全文（按冻结数据，没有实盘成交）；
8. 实盘仓位规则（src.portfolio_ace_m）在各种情形下给出的目标仓位和M可用金额。

以后每一步优化，check 必须全部相同才能提交；确实应该变的（例如改规则）要单独说明并重新 record。
只读：不联网、不推送、不写生产状态。运行时先把当前代码复制到临时目录，临时目录的 data/raw 指向冻结行情：
有些模块按自己文件的位置找 data/raw（例如 research/build_ac_daily_candidates.py 导入时就读交易日历），
不复制的话会读到仓库里的行情，对照就不可靠。

用法：python tests/golden/golden_master.py check [--full] [--data DIR]
      python tests/golden/golden_master.py record [--data DIR]   （只在确认输出应当改变时用）
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODE_DIRS = ("src", "scripts", "config")
# 在git里、和代码一起发布的模型表：研究池的成交概率打分要读（不是行情，属于代码的一部分）
MODEL_TABLES = ("data/processed/fill_rate_table.csv", "data/processed/fill_rate_fallback.csv")
EXPECTED = Path(__file__).resolve().parent / "expected"
DEFAULT_DATA = Path.home() / "A_System_golden_data" / "20261110"
END = "20261110"
MANIFEST_SHA256 = "5a94234bd85a2930db86b2570e1992d6c4fdae6a1390abf963b368dce5043f1a"
PLAN_COLUMNS = ["action_date", "signal_date", "buy_date", "exit_date", "ts_code", "strategy_leg", "status",
                "hold_offset", "exit_rule", "entry_price", "exit_price", "stock_return_before_fees", "account_return",
                "position_opened", "outcome_observable", "position_open_until", "matched_condition_profile_ids",
                "resolved_exit_profile_id"]


def fmt(value) -> str:
    """浮点按17位有效数字写（往返无损），其余按字符串；NaN/None 写空。"""
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if value != value else format(value, ".17g")
    try:
        import numpy as np
        if isinstance(value, np.floating):
            v = float(value)
            return "" if v != v else format(v, ".17g")
        if isinstance(value, np.integer):
            return str(int(value))
        if isinstance(value, np.bool_):
            return str(bool(value))
    except ImportError:
        pass
    return str(value)


def table(rows, columns) -> str:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(fmt(row.get(c)).replace(",", ";").replace("\n", " ") for c in columns))
    return "\n".join(lines) + "\n"


def frame_text(frame, columns=None, sort=None) -> str:
    if frame is None or len(frame) == 0:
        return "(empty)\n"
    cols = [c for c in (columns or list(frame.columns)) if c in frame.columns]
    data = frame[cols].copy()
    if sort:
        keys = [c for c in sort if c in data.columns]
        data = data.astype({k: str for k in keys}).sort_values(keys, kind="mergesort")
    return table(data.to_dict("records"), cols)


def json_text(obj) -> str:
    def norm(o):
        if isinstance(o, dict):
            return {str(k): norm(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
        if isinstance(o, (list, tuple)):
            return [norm(v) for v in o]
        if isinstance(o, (str, bool)) or o is None:
            return o
        if isinstance(o, int):
            return o
        return fmt(o)
    return json.dumps(norm(obj), ensure_ascii=False, indent=1) + "\n"


def verify_data(data: Path) -> None:
    manifest = data / "manifest.sha256"
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != MANIFEST_SHA256:
        raise SystemExit("对照数据的校验清单被改过：%s" % manifest)
    bad = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        path = data / rel
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            bad.append(rel)
            if len(bad) >= 5:
                break
    if bad:
        raise SystemExit("对照数据被改动或缺失：%s" % bad)


def runtime_root(data: Path, tmp: Path) -> Path:
    """临时运行根：当前代码的副本 + 指向冻结行情的 data/raw（不碰仓库自己的 data/）。必须在导入任何 src/scripts 之前调用。"""
    import shutil
    root = tmp / "root"
    for name in CODE_DIRS:
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "data" / "processed").mkdir(parents=True)
    for rel in MODEL_TABLES:
        shutil.copy2(REPO / rel, root / rel)
    (root / "data" / "raw").symlink_to(data / "raw")
    sys.path.insert(0, str(root))
    os.chdir(root)
    return root


# ---- 实盘组合状态机的喂数辅助：与 research/verify_live_engine_matches_certify.py、
# research/validate_other_live_strategies_strict.py 里的同名函数一字不差（2026-11-14 抄进来，
# 让对照标准只依赖生产代码，研究代码以后怎么改都不影响它）。


def action_candidate_map(frame, leg: str) -> dict:
    """把候选映射到真实资金开仓日：A/C/E 在 buy_date 开盘执行，D 在信号日盘中开仓。"""
    normalized_leg = str(leg).strip().upper()
    if normalized_leg not in {"A", "C", "D", "E"}:
        raise ValueError(f"未知策略腿：{leg}")
    valid = frame[frame["status"].astype(str).eq("OK")].copy()
    if valid.empty:
        return {}
    entry_column = "signal_date" if normalized_leg == "D" else "buy_date"
    if entry_column not in valid.columns:
        raise KeyError(f"{normalized_leg}候选缺少真实开仓日字段{entry_column}")
    date_text = lambda series: series.astype(str).str.replace(r"\.0$", "", regex=True)
    valid["signal_date"] = date_text(valid["signal_date"])
    valid["action_date"] = date_text(valid[entry_column])
    valid["exit_date"] = date_text(valid["exit_date"])
    valid["strategy_leg"] = normalized_leg
    if valid["action_date"].eq("").any() or valid["action_date"].isna().any():
        raise ValueError(f"{normalized_leg}候选存在空真实开仓日")
    return {str(row["action_date"]): row.to_dict() for _, row in valid.drop_duplicates("action_date", keep="last").iterrows()}


def make_engine(project_root: Path):
    """只读计划引擎；所有外部状态都由逐日数据显式注入。"""
    from src.combined_live_engine import CombinedLiveEngine
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = project_root
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "A_C_E_D"},
    }
    engine.load_positions = lambda: []
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "A_C_E_D"
    engine.is_b_strategy_removed = lambda: True
    engine.load_today_e_signal = lambda _today: None
    engine.compute_e_preview = lambda _today: {"has_candidate": False, "has_scored_data": True, "neutral_segs": []}
    return engine


def ac_order(row: dict, action_date: str) -> dict:
    return {
        "paper_order_id": f"VERIFY-{row['strategy_leg']}-{action_date}-{row['ts_code']}",
        "signal_date": str(row.get("signal_date", "")),
        "strategy_leg": str(row["strategy_leg"]),
        "planned_order_date": action_date,
        "side": "BUY",
        "ts_code": str(row["ts_code"]),
        "name": str(row.get("name", "")),
        "reference_price": 10.0,
        "round_lot_shares": 10_000,
        "estimated_shares": 10_000,
        "planned_position_pct": 0.825,
    }


def e_signal(row: dict) -> dict:
    return {
        "signal_date": str(row.get("signal_date", "")),
        "ts_code": str(row["ts_code"]),
        "name": str(row.get("name", "")),
        "limit_close": 10.0,
        "exit_offset": int(row.get("hold_offset", 2) or 2),
    }


def live_engine_rows(legs, action_dates) -> list[dict]:
    """同一批候选交给实盘组合状态机（与 research/verify_live_engine_matches_certify.py 同一做法），记录每日决定。"""
    import pandas as pd

    maps = {leg: (action_candidate_map(legs[leg], leg) if legs.get(leg) is not None and len(legs[leg]) else {})
            for leg in ("A", "C", "E")}
    dates = sorted(set(str(d) for d in action_dates) | {d for m in maps.values() for d in m})
    engine = make_engine(Path(os.getcwd()))
    rows = []
    for action_date in dates:
        ac = pd.DataFrame([ac_order(maps[leg][action_date], action_date) for leg in ("A", "C") if action_date in maps[leg]])
        engine.load_latest_abc_orders = lambda frame=ac: (REPO / "reports" / "verification" / "synthetic_ac_orders.csv", frame.copy())
        payload = e_signal(maps["E"][action_date]) if action_date in maps["E"] else None
        engine.load_yesterday_e_signal = lambda _today, p=payload: p
        _state, decisions, orders = engine.build_mode1_plan(action_date)
        actions = sorted(set(decisions.get("action", pd.Series(dtype=str)).astype(str))) if len(decisions) else []
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")] if len(orders) and "side" in orders else []
        buy = ";".join("%s/%s/%s" % (r.get("strategy_leg", ""), r.get("ts_code", ""), fmt(r.get("planned_position_pct")))
                       for r in (buys.to_dict("records") if len(buys) else []))
        rows.append({"action_date": action_date, "actions": "|".join(actions), "buy": buy})
    return rows


def live_selection_text(root: Path, ds: Path, calendar: Path, start: str, shadow, tmp: Path) -> str:
    """实盘收盘流水线⑥的A/C选股函数逐日跑同一份研究池，选出的股票必须和影子账A/C计划逐日相同。

    2026-11-14 起锁定“实盘和回测用同一套选股”：哪天有人只改了一边，这一项立刻不同。
    """
    import copy
    import pandas as pd
    from scripts.run_paper_ab_filtered_daily_ops import apply_and_rank, build_c_shadow_candidates, build_generator, selected_candidate
    from src.acde_plan import load_monthly_config, plan_context, variant_sets
    from src.acde_rolling_framework import ResearchWindow

    monthly = load_monthly_config(root / "config/acde_rolling_optimization.json")
    empty_d = tmp / "live_selection_empty_d.csv"
    pd.DataFrame(columns=["trade_date", "ts_code"]).to_csv(empty_d, index=False)
    cal = pd.read_csv(calendar, dtype={"cal_date": str}, low_memory=False)
    opened = sorted(cal[pd.to_numeric(cal["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str))
    first = [d for d in opened[1:] if d >= str(start)][0]
    context = plan_context(window=ResearchWindow("GOLDEN_LIVE_SELECTION", first, END, "shadow", False),
                           feature_path=ds / "strict_feature_pool.csv", sentiment_path=ds / "market_sentiment.csv",
                           d_event_path=empty_d, calendar_path=calendar,
                           minimum_limit_up_count=int(monthly["market_controller"]["minimum_limit_up_count"]))
    pool = context["signal_pool"]
    config = variant_sets()[0]["A"].payload
    strategy_path = root / "config" / "strategy_config.json"
    action = config.get("paper_candidate", {}).get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    a_generator = build_generator(strategy_path, copy.deepcopy(config))
    a_filtered = a_generator.apply_strategy_filters(pool)
    live = {"A": set(), "C": set()}
    for day, frame in pool.groupby(pool["trade_date"].astype(str).str.replace(".0", "", regex=False), sort=True):
        a = apply_and_rank(a_generator, a_filtered, day, top_n=10)
        pick = selected_candidate(a_generator.apply_post_pick_entry_gate(a), action) if not a.empty else None
        if pick is not None:
            live["A"].add((day, str(pick["ts_code"])))
        _rules, _cands, pick_c, _rej = build_c_shadow_candidates(strategy_path, config, frame, day, action, top_n=10)
        if pick_c is not None:
            live["C"].add((day, str(pick_c["ts_code"])))
    lines = []
    for leg in ("A", "C"):
        plan = shadow["legs"].get(leg)
        expected = {(str(r["signal_date"]).replace(".0", ""), str(r["ts_code"])) for r in plan.to_dict("records")}
        diff = sorted(expected ^ live[leg])
        lines.append("%s：影子账 %d 笔，实盘流水线⑥选股函数 %d 笔，%s" % (
            leg, len(expected), len(live[leg]), "逐日相同" if not diff else "不同 %d 处：%s" % (len(diff), diff[:10])))
    return "\n".join(lines) + "\n"


def sizing_rows() -> tuple[list[dict], list[dict]]:
    from src import portfolio_ace_m as p
    total = 1_000_000.0
    ace = []
    for mode in (p.GATE_FULL, p.GATE_REDUCED, p.GATE_BLOCKED):
        for m_pct in (0.0, 0.1, 0.175, 0.5, 0.825, 0.99):
            r = p.ace_limits(mode, total, total * m_pct)
            ace.append(dict(mode=mode, m_pct_in=m_pct, **r))
    m = []
    for active in (False, True):
        for ace_pct in (0.0, 0.2, 0.825):
            for cash_pct in (1.0, 0.5, 0.1):
                for unfilled_pct in (0.0, 0.1):
                    r = p.m_budget(total_asset=total, available_cash=total * cash_pct, ace_active_today=active,
                                   ace_value=total * ace_pct, ace_unfilled_target=total * unfilled_pct, cash_buffer=1000.0)
                    m.append(dict(ace_active_today=active, ace_pct=ace_pct, cash_pct=cash_pct, unfilled_pct=unfilled_pct, **r))
    return ace, m


def build(data: Path, tmp: Path, *, record: bool, full: bool, log, notes: dict) -> dict[str, str]:
    root = runtime_root(data, tmp)
    import pandas as pd
    import scripts.update_equity_curve_stop as prod
    from src.daily_report import build_daily_report, value_lines
    from src.equity_curve_stop import allowed_flags, load_settings, load_weekly_report_settings
    from src.review_triggers import FINAL_STANDARD, active_acknowledgements
    from src.strategy_m_reference import MReference
    from src.strategy_warnings import run_strategy_warnings
    from src.utils.config import load_json_config

    if Path(prod.__file__).resolve().parents[1] != root.resolve():
        raise RuntimeError("生产代码没有从临时副本导入：%s" % prod.__file__)
    raw = root / "data" / "raw"
    calendar = raw / "trade_calendar.csv"
    config = load_json_config(root / "config" / "config.json")
    settings = load_settings(config, root)
    out: dict[str, str] = {}

    t0 = time.time()
    from src.five_year_research import FiveYearResearchDatasetBuilder
    ds = tmp / "dataset"
    builder = FiveYearResearchDatasetBuilder(research_root=ds)
    builder.build_base_tables(start_date=settings.history_start, end_date=END, overwrite=True)
    builder.build_strict_features(float(config.get("fill_model", {}).get("default_planned_buy_amount", 412_500)))
    pending = prod.check_volume_ratio_coverage(
        ds / "strict_feature_pool.csv", END,
        float((config.get("equity_curve_stop", {}) or {}).get("research_max_volume_ratio_null", 0.5)))
    pool_summary = {}
    for name in ("strict_feature_pool.csv", "market_sentiment.csv"):
        content = (ds / name).read_bytes()
        frame = pd.read_csv(ds / name, dtype={"trade_date": str}, low_memory=False)
        pool_summary[name] = {"rows": len(frame), "columns": len(frame.columns), "first": str(frame["trade_date"].min()),
                              "last": str(frame["trade_date"].max()), "sha256": hashlib.sha256(content).hexdigest(),
                              "same_as_frozen_20261111": hashlib.sha256((data / "dataset" / name).read_bytes()).hexdigest()
                              == hashlib.sha256(content).hexdigest()}
    pool_summary["volume_ratio_pending"] = pending
    pool_summary["model_tables_sha256"] = {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in MODEL_TABLES}
    out["research_pool.json"] = json_text(pool_summary)
    log("从原始行情重建研究池 %.0fs" % (time.time() - t0))

    t0 = time.time()
    shadow = prod.build_shadow_replay(project_root=root, feature_path=ds / "strict_feature_pool.csv",
                                      sentiment_path=ds / "market_sentiment.csv", calendar_path=calendar,
                                      start=settings.history_start, end=END, work_dir=tmp / "work")
    nav = shadow["nav"]
    out["shadow_nav.csv"] = table([{"date": str(d), "nav": float(v)} for d, v in nav.items()], ["date", "nav"])
    for leg in ("A", "C", "E"):
        out["plans_%s.csv" % leg] = frame_text(shadow["legs"].get(leg), PLAN_COLUMNS, sort=["action_date", "ts_code"])
    flags = allowed_flags(nav.to_numpy(), ma_window=settings.ma_window, lag=settings.lag_trading_days)
    out["stop_decisions.csv"] = table([{"date": str(d), "allowed": bool(f)} for d, f in zip(nav.index, flags)], ["date", "allowed"])
    log("影子账与停手判定 %.0fs" % (time.time() - t0))

    t0 = time.time()
    out["live_engine.csv"] = table(live_engine_rows(shadow["legs"], shadow["action_dates"]), ["action_date", "actions", "buy"])
    log("实盘组合状态机 %.0fs" % (time.time() - t0))

    t0 = time.time()
    out["live_selection.txt"] = live_selection_text(root, ds, calendar, settings.history_start, shadow, tmp)
    log("实盘流水线⑥选股逐日对照影子账 %.0fs" % (time.time() - t0))

    t0 = time.time()
    health, error = prod.compute_factor_health_checked(config, ds, calendar)
    if error:
        raise RuntimeError("因子健康没算成：%s" % error)
    out["factor_health.json"] = json_text(health)
    log("因子健康 %.0fs" % (time.time() - t0))

    t0 = time.time()
    reference = MReference(raw)
    days = [str(d) for d in shadow["action_dates"] if "20191202" <= str(d) <= END]
    cache = tmp / "m_cache.json"
    expected_m = EXPECTED / "m_candidates.csv"
    if not record:
        # 用记下的M候选单预置缓存：记下的日子用记下的结果，出错的日子记为出错，其余日子当时就没有候选。
        # 预置后三级警告只重算最近20个交易日；另行抽查约十分之一的日子加最近30天（--full 全部）重算比对。
        recorded = {}
        for line in expected_m.read_text(encoding="utf-8").splitlines()[1:]:
            day, codes = line.split(",", 1)
            recorded[day] = [c for c in codes.split(";") if c]
        errors = {d: "ERROR" for d, c in recorded.items() if c == ["ERROR"]}
        seeded = {d: recorded.get(d, []) for d in days if d not in errors}
        cache.write_text(json.dumps({"version": 1, "candidates": seeded, "errors": errors, "outcomes": {}}), encoding="utf-8")
        sample = days if full else sorted(set(days[::10]) | set(days[-30:]))
        for day in sample:
            result = reference(day)
            codes = [str(c["ts_code"]) for c in result.get("candidates") or []] if str(result.get("status")) == "OK" else ["ERROR"]
            if codes != recorded.get(day, []):
                notes["m_mismatch"].append((day, recorded.get(day, []), codes))
        notes["m_sample_days"] = len(sample)
        log("M候选单抽查%d天 %.0fs" % (len(sample), time.time() - t0))
    triggers, metrics = run_strategy_warnings(shadow, health, config, raw_dir=raw, cache_path=cache, today=END,
                                              ma_window=settings.ma_window, lag=settings.lag_trading_days)
    if record:  # M候选单单独记一份，供 check 预置缓存（不参与比较）
        saved = json.loads(cache.read_text(encoding="utf-8"))
        lines = ["day,codes"] + ["%s,%s" % (d, ";".join(saved["candidates"][d])) for d in sorted(saved["candidates"])
                                 if saved["candidates"][d]] + ["%s,ERROR" % d for d in sorted(saved["errors"])]
        notes["m_candidates.csv"] = "\n".join(lines) + "\n"
    out["warnings_metrics.json"] = json_text(metrics)
    out["warnings_triggers.csv"] = table([{"key": t.key, "tier": getattr(t, "tier", 1), "detail": t.detail} for t in triggers],
                                        ["key", "tier", "detail"])
    log("三级警告与ACE+M回放 %.0fs" % (time.time() - t0))

    section = dict(config.get("review_triggers", {}) or {})
    entries = section.get("acknowledged") or []
    acknowledged = active_acknowledgements(entries, END)
    recheck = {str(e.get("key", "")): str(e.get("recheck_by", "")) for e in entries if str(e.get("key", "")) in acknowledged}
    weekly = load_weekly_report_settings(config)
    navs = nav.to_numpy()
    change = lambda n: float(navs[-1] / navs[-1 - n] - 1.0) if len(navs) > n else None
    rows, limit_up = prod.pool_coverage_inputs(ds, END)
    warn_section = dict(config.get("strategy_warnings", {}) or {})
    values = {
        "alignment": None, "m_slippage": None, "ace_slippage": None, "live_loss": None,
        "coverage": {"pool_rows": rows, "limit_up_count": limit_up, "min_ratio": float(section.get("pool_coverage_min_ratio", 0.3))},
        "signals": metrics.get("signals"), "health": health, "warnings": metrics,
        "thresholds": {"combo_middle": (warn_section.get("middle") or {}).get("combo_drawdown", -0.315),
                       "combo_final": (warn_section.get("final") or {}).get("combo_drawdown", -0.5)},
        "shadow": {"r3": change(weekly.window_3m), "r6": change(weekly.window_6m),
                   "fail_3m": weekly.shadow_3m_fail, "fail_6m": weekly.shadow_6m_fail},
        "stall": prod.compute_regime_stall(config, nav, ds / "market_sentiment.csv", END),
    }
    report = build_daily_report(END, triggers, acknowledged, recheck, {}, value_lines(values), final_standard=FINAL_STANDARD)
    out["daily_report.txt"] = report["title"] + "\n" + report["body"] + "\n"

    ace, m = sizing_rows()
    out["sizing_ace.csv"] = table(ace, list(ace[0].keys()))
    out["sizing_m.csv"] = table(m, list(m[0].keys()))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "record"))
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--full", action="store_true", help="check 时M候选单全部重算（约8分钟）")
    args = parser.parse_args()
    data = Path(args.data).expanduser()
    started = time.time()
    log = lambda text: print("  " + text, flush=True)
    verify_data(data)
    log("对照数据校验通过（%s）" % data)
    notes = {"m_mismatch": [], "m_sample_days": 0}
    with tempfile.TemporaryDirectory(prefix="golden_") as tmp:
        try:
            out = build(data, Path(tmp), record=args.mode == "record", full=args.full, log=log, notes=notes)
        finally:
            os.chdir(REPO)
    if args.mode == "record":
        EXPECTED.mkdir(parents=True, exist_ok=True)
        for name, text in dict(out, **{"m_candidates.csv": notes["m_candidates.csv"]}).items():
            (EXPECTED / name).write_text(text, encoding="utf-8")
        print("RECORDED %d 项 -> %s（%.0f秒）" % (len(out), EXPECTED, time.time() - started))
        return 0
    failed = []
    for name in sorted(out):
        expected_path = EXPECTED / name
        expected = expected_path.read_text(encoding="utf-8") if expected_path.exists() else None
        if expected == out[name]:
            print("  一致  %s" % name)
            continue
        failed.append(name)
        print("  不同  %s" % name)
        if expected is None:
            print("        （没有对照标准）")
            continue
        for line in list(difflib.unified_diff(expected.splitlines(), out[name].splitlines(), "对照", "现在", n=0, lineterm=""))[:12]:
            print("        " + line[:220])
    mismatch = notes["m_mismatch"]
    print("  %s  M候选单抽查%d天%s" % ("一致" if not mismatch else "不同", notes["m_sample_days"],
                                   "" if not mismatch else "：" + "；".join("%s 对照%s 现在%s" % m for m in mismatch[:5])))
    ok = not failed and not mismatch
    print("RESULT %s：%d 项一致，%d 项不同，M候选单抽查%d天%s（%.0f秒）" % (
        "PASS" if ok else "FAIL", len(out) - len(failed), len(failed), notes["m_sample_days"],
        "全部一致" if not mismatch else "有%d天不同" % len(mismatch), time.time() - started))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
