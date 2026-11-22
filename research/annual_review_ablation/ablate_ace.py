"""逐个去掉条件检验（A、C、E）：每次只去掉一个条件，其余全部不变；冻结生产代码与研究池（行情至20261110）。
每个方案输出：这条腿单独的计划与单独账户（不停手）、整个ACE影子账（该腿换成方案，从不停手，给停手判定用）。

事先写死的判定（结果出来前）：比较“去掉后”与“现行”这条腿单独账户2020~2026每年收益；
去掉后至少5年变差=有用；最多2年变差=拟合嫌疑；其余=不确定。另报单笔中位数与整个ACE+M的结果作佐证。"""
import copy, os, pickle, sys, time
from pathlib import Path
from _paths import DS, END, FROZEN, enter_frozen, frozen_module, out_dir
OUT = out_dir()
enter_frozen()
import pandas as pd
build_variant_plan = frozen_module("optimize_acde_rolling_three_year").build_variant_plan
_research = frozen_module("acde_monthly_research")
_context, _execution_kwargs = _research._context, _research._execution_kwargs
_variant_sets, load_monthly_config = _research._variant_sets, _research.load_monthly_config
from src.acde_rolling_candidates import StaticOutcomeCache, VariantDefinition, _profiles_from_axes
from src.acde_rolling_framework import FIXED_PRIORITY, ResearchWindow, replay_action_date_cash_portfolio
from src.equity_curve_stop import shadow_nav_from_detail

t0 = time.time()
monthly = load_monthly_config(FROZEN / "config/acde_rolling_optimization.json")
empty = OUT / "empty_d_events.csv"
pd.DataFrame(columns=["trade_date", "ts_code"]).to_csv(empty, index=False)
calendar_path = FROZEN / "data/raw/trade_calendar.csv"
calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
opened = sorted(calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str))
eligible = [d for d in opened[1:] if d >= "20190101"]
context = _context(window=ResearchWindow("ABLATION", eligible[0], END, "shadow", False),
                   feature_path=DS / "strict_feature_pool.csv", sentiment_path=DS / "market_sentiment.csv",
                   d_event_path=empty, calendar_path=calendar_path,
                   minimum_limit_up_count=int(monthly["market_controller"]["minimum_limit_up_count"]))
baselines, _ = _variant_sets()
cache = StaticOutcomeCache()
execution = _execution_kwargs(monthly)
POOL = context["signal_pool"]
EMPTY = pd.DataFrame()

def plan(variant, pool=None):
    return build_variant_plan(variant, signal_pool=POOL if pool is None else pool, d_events=context["d_events"],
                              allowed_action_dates=context["allowed_actions"], cutoff=END, outcome_cache=cache)

def nav_of(legs):
    full = {leg: legs.get(leg, EMPTY) for leg in ("A", "C", "E", "D")}
    detail = replay_action_date_cash_portfolio(full, action_dates=context["action_dates"], priority=FIXED_PRIORITY, **execution)
    return shadow_nav_from_detail(detail, context["action_dates"], initial_cash=float(execution["initial_cash"]))

base_plans = {leg: plan(baselines[leg]) for leg in ("A", "C", "E")}
print("现行A/C/E计划：%s（%.0f秒）" % ({k: len(v) for k, v in base_plans.items()}, time.time() - t0), flush=True)

# ---------- A ----------
A0 = baselines["A"].payload
ALL_SEG = ["lt_5", "5_10", "10_20", "20_40", "40_80", "gte_80"]
ALL_CHAIN = ["lt_3", "3_8", "8_15", "15_30", "gte_30"]
ALL_FD = ["lt_0_1pct", "0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct", "1pct_2pct", "2pct_5pct", "gte_5pct"]
CUR_FD = ["0_5pct_1pct", "0_3pct_0_5pct", "0_1pct_0_3pct"]

def a_axes(segments=("lt_5",), chains=("8_15",), fds=CUR_FD):
    c = copy.deepcopy(A0)
    c["candidate_filters"]["condition_profiles"] = _profiles_from_axes(prefix="ABL", segment_values=list(segments),
                                                                       chain_values=list(chains), fd_values=list(fds))
    return c

def a_edit(fn):
    c = copy.deepcopy(A0)
    fn(c)
    return c

def drop_exclude(column):
    def fn(c):
        c["candidate_filters"]["exclude_conditions"] = [x for x in c["candidate_filters"]["exclude_conditions"]
                                                        if str(x.get("column")) != column]
        if column == "market_segment":
            c["universe"]["exclude_market_segments"] = [s for s in c["universe"].get("exclude_market_segments", []) if s != "star"]
    return fn

A_VARIANTS = {
    "A-分段热度（冷门分段）": a_axes(segments=ALL_SEG),
    "A-连板环境（整条）": a_axes(chains=ALL_CHAIN),
    "A-连板下限（≥8）": a_axes(chains=["lt_3", "3_8", "8_15"]),
    "A-连板上限（<15）": a_axes(chains=["8_15", "15_30", "gte_30"]),
    "A-封单条件（整条）": a_axes(fds=ALL_FD),
    "A-封单0.1~0.3%分支（甲新增）": a_axes(fds=["0_5pct_1pct", "0_3pct_0_5pct"]),
    "A-补位分支": a_edit(lambda c: c["candidate_filters"]["fallback_when_primary_empty"].update(enabled=False)),
    "A-排除成交额倍率0.8~1.2": a_edit(drop_exclude("amount_ratio_bucket")),
    "A-排除科创板": a_edit(drop_exclude("market_segment")),
    "A-排除北交所前日涨0~3%": a_edit(lambda c: c["candidate_filters"].update(exclude_rules=[])),
    "A-排序加分规则": a_edit(lambda c: c["ranking"].update(score_rules=[])),
    "A-排序连板高度": a_edit(lambda c: c["ranking"].update(columns=["profit_source_score"], ascending=[False])),
}
# ---------- C ----------
C0 = baselines["C"].payload
def c_edit(fn):
    c = copy.deepcopy(C0)
    fn(c["paper_ab_filtered_strategy"]["c_strategy"])
    return c
def drop_profile(idx):
    return lambda cs: cs.update(condition_profiles=[p for i, p in enumerate(cs["condition_profiles"]) if i != idx])
def drop_condition(idxs, column):
    def fn(cs):
        for i in idxs:
            cs["condition_profiles"][i]["conditions"] = [x for x in cs["condition_profiles"][i]["conditions"] if x["column"] != column]
    return fn
C_VARIANTS = {"C-分支%d（%s）" % (i + 1, p["profile_id"]): c_edit(drop_profile(i))
              for i, p in enumerate(C0["paper_ab_filtered_strategy"]["c_strategy"]["condition_profiles"])}
for col in ("market_chain_count_bucket", "segment_limit_up_count_bucket", "first_time_detail_bucket", "board_type"):
    C_VARIANTS["C-分支1条件 %s" % col] = c_edit(drop_condition([0], col))
for col in ("limit_up_count_bucket", "market_leader_rank_bucket", "fd_ratio_bucket"):
    C_VARIANTS["C-分支2条件 %s" % col] = c_edit(drop_condition([1], col))
for col in ("limit_up_count_bucket", "market_leader_rank_bucket", "fd_ratio_bucket", "market_limit_down_count_bucket"):
    C_VARIANTS["C-分支3~5条件 %s" % col] = c_edit(drop_condition([2, 3, 4], col))
for i, rule in enumerate(C0["paper_ab_filtered_strategy"]["c_strategy"].get("risk_reject_rules", [])):
    C_VARIANTS["C-风险排除 %s" % rule.get("name", i)] = c_edit(
        (lambda k: (lambda cs: cs.update(risk_reject_rules=[r for j, r in enumerate(cs["risk_reject_rules"]) if j != k])))(i))
C_VARIANTS["C-排序换手率"] = c_edit(lambda cs: cs["ranking"].update(columns=["profit_source_score"], ascending=[False]))
# ---------- E ----------
E0 = baselines["E"].payload
def e_edit(fn):
    s = copy.deepcopy(E0)
    fn(s)
    return s
def drop_gate(column, value=None):
    def fn(s):
        ev = s["entry_gate"]["exclude_values"]
        if value is None:
            ev.pop(column, None)
        else:
            ev[column] = [v for v in ev[column] if v != value]
            if not ev[column]:
                ev.pop(column)
    return fn
def drop_scenario_condition(column):
    def fn(s):
        seen, out = set(), []
        for sc in s["scenarios"]:
            sc = copy.deepcopy(sc)
            sc["conditions"] = {k: v for k, v in sc["conditions"].items() if k != column}
            key = (tuple(sorted(sc["conditions"].items())), sc.get("sort_rule"), sc.get("exit_rule"))
            if key not in seen:
                seen.add(key)
                out.append(sc)
        s["scenarios"] = out
    return fn
E_VARIANTS = {
    "E-排除门 下午首封13:30~14:30": e_edit(drop_gate("first_time_detail_bucket")),
    "E-排除门 龙头排名11~30": e_edit(drop_gate("market_leader_rank_bucket")),
    "E-排除门 涨停120~180": e_edit(drop_gate("limit_up_count_bucket", "120_180")),
    "E-排除门 涨停<30（甲新增）": e_edit(drop_gate("limit_up_count_bucket", "lt_30")),
    "E-排除门 封单2%~5%": e_edit(drop_gate("fd_ratio_bucket")),
    "E-排除门 成交额倍率+炸板组合": e_edit(lambda s: s["entry_gate"].update(exclude_all_conditions=[])),
    "E-排序 换手率": e_edit(lambda s: s["final_ranking"].update(columns=["fd_amount_to_circ_mv"], ascending=[True], weights=[1.0])),
    "E-排序 封单比例": e_edit(lambda s: s["final_ranking"].update(columns=["turnover_rate"], ascending=[False], weights=[1.0])),
    "E-前置排除 成交额倍率0.8~1.2": e_edit(lambda s: s["universe_prefilters"].update(exclude_amount_ratio_bucket=[])),
}
for col in sorted({k for sc in E0["scenarios"] for k in sc["conditions"]}):
    E_VARIANTS["E-规则条件 %s" % col] = e_edit(drop_scenario_condition(col))
NEUTRAL_POOL = POOL.copy()
NEUTRAL_POOL["segment_retreat_state_bucket"] = "neutral"

results = {}
def run(leg, vid, payload, pool=None):
    try:
        variant = VariantDefinition(leg, vid, vid, payload, 1, True, "逐个去掉条件检验")
        p = plan(variant, pool)
        legs = dict(base_plans); legs[leg] = p
        results[vid] = {"leg": leg, "plans": p, "alone_nav": nav_of({leg: p}), "shadow_nav": nav_of(legs), "error": ""}
        print("%s：计划%d条（%.0f秒）" % (vid, len(p), time.time() - t0), flush=True)
    except Exception as exc:
        results[vid] = {"leg": leg, "error": "%s: %s" % (type(exc).__name__, exc)}
        print("%s：失败 %s" % (vid, results[vid]["error"]), flush=True)

for leg in ("A", "C", "E"):
    results["%s-现行" % leg] = {"leg": leg, "plans": base_plans[leg], "alone_nav": nav_of({leg: base_plans[leg]}),
                               "shadow_nav": nav_of(base_plans), "error": ""}
for vid, payload in A_VARIANTS.items():
    run("A", vid, payload)
for vid, payload in C_VARIANTS.items():
    run("C", vid, payload)
for vid, payload in E_VARIANTS.items():
    run("E", vid, payload)
run("E", "E-板块非退潮过滤", E0, pool=NEUTRAL_POOL)
pickle.dump({"results": results, "action_dates": list(context["action_dates"])}, open(OUT / "ablate_ace.pkl", "wb"))
print("全部完成 %.0f秒" % (time.time() - t0), flush=True)
