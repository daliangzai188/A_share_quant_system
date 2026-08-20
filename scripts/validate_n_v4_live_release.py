#!/usr/bin/env python3
"""只读校验 N v4 正式实盘发布；不连接券商、不下单。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live_certification import validate_live_certification  # noqa: E402
from src.strategy_n import N_VERSION  # noqa: E402


CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
EXPECTED_PORTFOLIO_TRADE_COUNT = 154
EXPECTED_N_TRADE_COUNT = 39
EXPECTED_RELEASE_ID = "portfolio-d-a-e-c-n-pending"


def n_v4_live_config_errors(config: Mapping[str, Any]) -> list[str]:
    """返回阻止当前五腿研究结果被误当成正式发布的结构化配置错误。

    该部署器冻结的是D>A>E>C>N发布。组合身份改变后，不能仅因N
    自身版本仍是v4就复用旧发布包；必须让旧脚本明确失败，等待新组合重新认证。
    """

    n_config = config.get("strategy_n", {})
    portfolio = config.get("portfolio_certification", {})
    metrics = portfolio.get("live_candidate_metrics", {})
    errors: list[str] = []
    if bool(metrics.get("certification_invalidated", False)):
        errors.append("当前五腿组合尚未取得有效严格发布认证")
    if str(portfolio.get("certification_expected_scenario", "")) != "current_d_a_e_c_n":
        errors.append("当前组合发布场景不是current_d_a_e_c_n")
    if str(n_config.get("strategy_version", "")) != N_VERSION:
        errors.append("strategy_n.strategy_version不是N v4")
    if n_config.get("enabled") is not True:
        errors.append("strategy_n.enabled必须为true")
    if n_config.get("live_order_enabled") is not True:
        errors.append("strategy_n.live_order_enabled必须为true")
    if n_config.get("entry_pause") is not False:
        errors.append("strategy_n.entry_pause必须为false")
    if n_config.get("live_research_risk_accepted") is not True:
        errors.append("N样本外风险未显式接受")
    if int(metrics.get("trade_count", 0) or 0) != EXPECTED_PORTFOLIO_TRADE_COUNT:
        errors.append("严格研究组合交易笔数不是154")
    if int(metrics.get("n_trade_count", 0) or 0) != EXPECTED_N_TRADE_COUNT:
        errors.append("严格研究组合N交易笔数不是39")
    if bool(metrics.get("capacity_certified", True)):
        errors.append("容量状态异常：当前必须明确为未认证")
    return errors


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    errors = n_v4_live_config_errors(config)
    if errors:
        raise SystemExit("N v4五腿实盘配置门禁失败：" + "；".join(errors))

    portfolio = dict(config.get("portfolio_certification", {}))
    check = validate_live_certification(
        PROJECT_ROOT,
        portfolio,
        full_config=config,
    )
    if not check.ok:
        raise SystemExit(f"N v4发布认证门禁失败：{check.reason}")
    freeze_path = Path(str(portfolio.get("strategy_release_freeze_path", "")))
    if not freeze_path.is_absolute():
        freeze_path = PROJECT_ROOT / freeze_path
    freeze = json.loads(freeze_path.read_text(encoding="utf-8-sig"))
    release_id = str(freeze.get("release_id", ""))
    if release_id != EXPECTED_RELEASE_ID:
        raise SystemExit(
            f"N v4冻结发布号不一致：actual={release_id or '缺失'}，"
            f"expected={EXPECTED_RELEASE_ID}"
        )
    print(
        "N v4正式实盘发布门禁通过 | "
        f"release={release_id} | "
        f"portfolio_trades={EXPECTED_PORTFOLIO_TRADE_COUNT} | "
        f"n_trades={EXPECTED_N_TRADE_COUNT} | capacity_certified=false"
    )


if __name__ == "__main__":
    main()
