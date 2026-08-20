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
EXPECTED_PORTFOLIO_TRADE_COUNT = 166
EXPECTED_N_TRADE_COUNT = 24
EXPECTED_RELEASE_ID = "portfolio-20260820-n-v4-volume-gate-live-v9.1"


def n_v4_live_config_errors(config: Mapping[str, Any]) -> list[str]:
    """返回阻止 N v4 正式实盘加载的结构化配置错误。"""

    n_config = config.get("strategy_n", {})
    portfolio = config.get("portfolio_certification", {})
    metrics = portfolio.get("live_candidate_metrics", {})
    errors: list[str] = []
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
        errors.append("正式组合交易笔数不是166")
    if int(metrics.get("n_trade_count", 0) or 0) != EXPECTED_N_TRADE_COUNT:
        errors.append("正式组合N交易笔数不是24")
    if bool(metrics.get("capacity_certified", True)):
        errors.append("容量状态异常：当前必须明确为未认证")
    return errors


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    errors = n_v4_live_config_errors(config)
    if errors:
        raise SystemExit("N v4实盘配置门禁失败：" + "；".join(errors))

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
