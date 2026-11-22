"""E规则里6种“去掉后结果不变”的附加条件：是逻辑上多余（带它的规则被另一条同排序、同卖出、条件更少的规则完全包含），
还是只是历史上恰好没碰到？逐条规则检查。"""
import os, sys
from pathlib import Path
from _paths import enter_frozen, frozen_module
enter_frozen()
_variant_sets = frozen_module("acde_monthly_research")._variant_sets
baselines, _ = _variant_sets()
SC = baselines["E"].payload["scenarios"]
RED = {"limit_times_bucket", "limit_times_detail_bucket", "market_emotion_state_bucket", "segment_limit_down_count_bucket",
       "segment_limit_height_rank_bucket", "segment_market_sentiment_level"}
def vals(v):
    return frozenset(v if isinstance(v, list) else [v])
def covers(big, small):
    """big的每个条件small都有且更窄或相同 → small匹配的股票big一定匹配。"""
    if big.get("sort_rule") != small.get("sort_rule") or big.get("exit_rule") != small.get("exit_rule"):
        return False
    return all(k in small["conditions"] and vals(small["conditions"][k]) <= vals(v) for k, v in big["conditions"].items())
for i, sc in enumerate(SC):
    hit = sorted(set(sc["conditions"]) & RED)
    if not hit:
        continue
    sup = [j for j, other in enumerate(SC) if j != i and covers(other, sc) and len(other["conditions"]) < len(sc["conditions"])]
    print("规则%2d（%s）含%s：被规则%s完全包含 → %s" % (i + 1, sc.get("sort_rule"), ",".join(hit),
          [j + 1 for j in sup] or "无", "逻辑多余" if sup else "不是多余（去掉会放宽）"))
