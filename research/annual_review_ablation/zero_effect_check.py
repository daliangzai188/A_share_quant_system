"""核对“零影响清理”：把逐个去掉时结果完全不变的7个条件一起去掉（E规则里6种附加条件 + C分支3~5的跌停数条件），
其余全部不动（E的“涨停<30”门禁保留），看计划与账户是否与现行逐笔完全一致。"""
import copy, os, sys
from pathlib import Path
from _paths import DS, END, FROZEN, enter_frozen, frozen_module, out_dir
OUT = out_dir()
enter_frozen()
import pandas as pd
build_variant_plan = frozen_module("optimize_acde_rolling_three_year").build_variant_plan
_research = frozen_module("acde_monthly_research")
_context, _execution_kwargs = _research._context, _research._execution_kwargs
_variant_sets, load_monthly_config = _research._variant_sets, _research.load_monthly_config
from src.acde_rolling_candidates import StaticOutcomeCache, VariantDefinition
from src.acde_rolling_framework import FIXED_PRIORITY, ResearchWindow, replay_action_date_cash_portfolio
from src.equity_curve_stop import shadow_nav_from_detail
monthly = load_monthly_config(FROZEN / "config/acde_rolling_optimization.json")
calendar_path = FROZEN / "data/raw/trade_calendar.csv"
calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
opened = sorted(calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str))
eligible = [d for d in opened[1:] if d >= "20190101"]
context = _context(window=ResearchWindow("ZERO", eligible[0], END, "shadow", False),
                   feature_path=DS / "strict_feature_pool.csv", sentiment_path=DS / "market_sentiment.csv",
                   d_event_path=OUT / "empty_d_events.csv", calendar_path=calendar_path,
                   minimum_limit_up_count=int(monthly["market_controller"]["minimum_limit_up_count"]))
baselines, _ = _variant_sets()
cache = StaticOutcomeCache()
execution = _execution_kwargs(monthly)
def plan(leg, payload):
    return build_variant_plan(VariantDefinition(leg, "ZERO_" + leg, "零影响清理", payload, 1, True, "零影响清理"),
                              signal_pool=context["signal_pool"], d_events=context["d_events"],
                              allowed_action_dates=context["allowed_actions"], cutoff=END, outcome_cache=cache)
def nav_of(legs):
    full = {leg: legs.get(leg, pd.DataFrame()) for leg in ("A", "C", "E", "D")}
    detail = replay_action_date_cash_portfolio(full, action_dates=context["action_dates"], priority=FIXED_PRIORITY, **execution)
    return shadow_nav_from_detail(detail, context["action_dates"], initial_cash=float(execution["initial_cash"]))
REDUNDANT = {"limit_times_bucket", "limit_times_detail_bucket", "market_emotion_state_bucket", "segment_limit_down_count_bucket",
             "segment_limit_height_rank_bucket", "segment_market_sentiment_level"}
e = copy.deepcopy(baselines["E"].payload)
seen, scen = set(), []
for sc in e["scenarios"]:
    sc = copy.deepcopy(sc)
    sc["conditions"] = {k: v for k, v in sc["conditions"].items() if k not in REDUNDANT}
    key = (tuple(sorted(sc["conditions"].items())), sc.get("sort_rule"), sc.get("exit_rule"))
    if key not in seen:
        seen.add(key); scen.append(sc)
e["scenarios"] = scen
c = copy.deepcopy(baselines["C"].payload)
cs = c["paper_ab_filtered_strategy"]["c_strategy"]
for i in (2, 3, 4):
    cs["condition_profiles"][i]["conditions"] = [x for x in cs["condition_profiles"][i]["conditions"]
                                                 if x["column"] != "market_limit_down_count_bucket"]
cur = {leg: plan(leg, baselines[leg].payload) for leg in ("A", "C", "E")}
new = {"A": cur["A"], "C": plan("C", c), "E": plan("E", e)}
def keyset(p):
    cols = [k for k in ("action_date", "trade_date", "signal_date", "ts_code") if k in p.columns]
    return set(map(tuple, p[cols].astype(str).to_numpy())), cols
print("E规则：%d条 → %d条（合并去掉条件后重复的规则）" % (len(baselines["E"].payload["scenarios"]), len(scen)))
for leg in ("C", "E"):
    a, cols = keyset(cur[leg]); b, _ = keyset(new[leg])
    na, nb = nav_of({leg: cur[leg]}), nav_of({leg: new[leg]})
    print("%s：计划 现行%d / 清理后%d，按%s逐笔相同：%s；单独账户 %.4f倍 / %.4f倍，逐日最大差 %.2e" % (
        leg, len(cur[leg]), len(new[leg]), "+".join(cols), a == b, na.iloc[-1], nb.iloc[-1], float((na - nb).abs().max())))
sa, sb = nav_of(cur), nav_of(new)
print("ACE影子账：现行 %.4f倍 / 清理后 %.4f倍，逐日最大差 %.2e" % (sa.iloc[-1], sb.iloc[-1], float((sa - sb).abs().max())))
