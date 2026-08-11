"""账户级风险总闸的影子状态机。

只基于完整真实成交更新策略净值并给出假设暂停动作，永远不返回或执行真实下单
许可。后续若要接入实盘，必须另行实现且通过复利发布门禁。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


STATE_SCHEMA_VERSION = 1


class AccountRiskShadowError(ValueError):
    """影子总闸配置或状态无效。"""


def _date(value: Any, field: str) -> str:
    text = str(value or "").strip().replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        raise AccountRiskShadowError(f"{field}必须为YYYYMMDD")
    try:
        dt.datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise AccountRiskShadowError(f"{field}不是有效日期") from exc
    return text


def validate_shadow_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """锁定影子属性并校验研究阈值。"""

    if int(policy.get("schema_version", 0) or 0) != 1:
        raise AccountRiskShadowError("账户风险影子配置版本必须为1")
    if str(policy.get("status", "")).upper() != "SHADOW":
        raise AccountRiskShadowError("账户风险配置状态必须为SHADOW")
    if policy.get("enforce_live_gate") is not False:
        raise AccountRiskShadowError("影子总闸禁止设置enforce_live_gate=true")
    policy_id = str(policy.get("policy_id", "")).strip()
    if not policy_id:
        raise AccountRiskShadowError("账户风险影子配置缺少policy_id")
    start = _date(policy.get("observation_start_date"), "observation_start_date")

    daily_loss = float(policy.get("max_daily_realized_loss_pct", 0.0) or 0.0)
    drawdown = float(policy.get("max_account_drawdown_pct", 0.0) or 0.0)
    streak = int(policy.get("max_consecutive_losses", 0) or 0)
    cooldown = int(policy.get("suggested_cooldown_trade_days", 0) or 0)
    minimum = int(
        policy.get("minimum_complete_trades_for_activation_review", 0) or 0
    )
    if not 0 < daily_loss < 1:
        raise AccountRiskShadowError("日亏损阈值必须在(0,1)内")
    if not 0 < drawdown < 1:
        raise AccountRiskShadowError("账户回撤阈值必须在(0,1)内")
    if streak < 1 or cooldown < 1 or minimum < 1:
        raise AccountRiskShadowError("连续亏损、冷静期和激活复核样本数必须为正整数")

    paths: dict[str, str] = {}
    for field in ("state_path", "latest_status_path", "bootstrap_equity_path"):
        value = str(policy.get(field, "")).strip()
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise AccountRiskShadowError(f"{field}必须是项目内相对路径")
        paths[field] = value
    return {
        "policy_id": policy_id,
        "observation_start_date": start,
        "daily_loss_limit": daily_loss,
        "drawdown_limit": drawdown,
        "streak_limit": streak,
        "cooldown_trade_days": cooldown,
        "minimum_samples": minimum,
        **paths,
    }


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountRiskShadowError(f"无法读取{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise AccountRiskShadowError(f"{path}根节点不是对象")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _trades(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_key", "exit_date", "net_pnl"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise AccountRiskShadowError("完整成交缺少字段：" + "、".join(missing))
    result = frame.copy()
    result["trade_key"] = result["trade_key"].fillna("").astype(str)
    result["exit_date"] = (
        result["exit_date"]
        .fillna("")
        .astype(str)
        .str.replace("-", "", regex=False)
        .str[:8]
    )
    result["net_pnl"] = pd.to_numeric(result["net_pnl"], errors="coerce")
    invalid = (
        result["trade_key"].eq("")
        | ~result["exit_date"].str.fullmatch(r"\d{8}")
        | result["net_pnl"].isna()
    )
    if invalid.any():
        raise AccountRiskShadowError("完整成交中存在空trade_key、无效exit_date或net_pnl")
    if result["trade_key"].duplicated().any():
        raise AccountRiskShadowError("完整成交trade_key重复")
    return result.sort_values(["exit_date", "trade_key"]).reset_index(drop=True)


def _new_state(policy: Mapping[str, Any], bootstrap_equity: float, trades: pd.DataFrame) -> dict[str, Any]:
    if bootstrap_equity <= 0:
        raise AccountRiskShadowError("影子总闸首次建立需要有效的策略净值基线")
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "policy_id": policy["policy_id"],
        "mode": "SHADOW_ONLY",
        "enforce_live_gate": False,
        "observation_start_date": policy["observation_start_date"],
        "baseline_equity": bootstrap_equity,
        "current_equity": bootstrap_equity,
        "peak_equity": bootstrap_equity,
        "realized_pnl": 0.0,
        "processed_trade_keys": sorted(trades["trade_key"].tolist()),
        "observed_complete_trade_count": 0,
        "consecutive_losses": 0,
        "maximum_consecutive_losses": 0,
        "daily_realized_pnl": {},
        "initialized_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def update_account_risk_shadow(
    *,
    state_path: Path,
    latest_status_path: Path,
    policy: Mapping[str, Any],
    complete_trades: pd.DataFrame,
    bootstrap_equity: float,
    as_of_date: str,
) -> dict[str, Any]:
    """幂等更新影子净值并输出假设触发结果。"""

    normalized = validate_shadow_policy(policy)
    today = _date(as_of_date, "as_of_date")
    trades = _trades(complete_trades)
    initialized_now = not state_path.exists()
    if initialized_now:
        state = _new_state(normalized, float(bootstrap_equity), trades)
    else:
        state = load_json_object(state_path)
        if int(state.get("schema_version", 0) or 0) != STATE_SCHEMA_VERSION:
            raise AccountRiskShadowError("影子总闸状态版本不受支持，拒绝覆盖")
        if str(state.get("policy_id", "")) != normalized["policy_id"]:
            raise AccountRiskShadowError("影子总闸状态与当前policy_id不一致")
        if state.get("enforce_live_gate") is not False:
            raise AccountRiskShadowError("影子总闸状态出现实盘执行标记，拒绝运行")

    processed = {str(value) for value in state.get("processed_trade_keys", [])}
    eligible = trades[
        trades["exit_date"].ge(normalized["observation_start_date"])
        & ~trades["trade_key"].isin(processed)
    ].copy()
    ignored_prestart = trades[
        trades["exit_date"].lt(normalized["observation_start_date"])
        & ~trades["trade_key"].isin(processed)
    ]
    processed.update(ignored_prestart["trade_key"].tolist())

    equity = float(state.get("current_equity", 0.0) or 0.0)
    peak = float(state.get("peak_equity", 0.0) or 0.0)
    realized = float(state.get("realized_pnl", 0.0) or 0.0)
    streak = int(state.get("consecutive_losses", 0) or 0)
    max_streak = int(state.get("maximum_consecutive_losses", 0) or 0)
    daily = {
        str(key): float(value)
        for key, value in dict(state.get("daily_realized_pnl", {})).items()
    }
    for row in eligible.itertuples(index=False):
        pnl = float(row.net_pnl)
        equity += pnl
        realized += pnl
        peak = max(peak, equity)
        streak = streak + 1 if pnl < 0 else 0
        max_streak = max(max_streak, streak)
        daily[str(row.exit_date)] = daily.get(str(row.exit_date), 0.0) + pnl
        processed.add(str(row.trade_key))

    state.update(
        {
            "current_equity": equity,
            "peak_equity": peak,
            "realized_pnl": realized,
            "processed_trade_keys": sorted(processed),
            "observed_complete_trade_count": int(
                state.get("observed_complete_trade_count", 0) or 0
            )
            + int(len(eligible)),
            "consecutive_losses": streak,
            "maximum_consecutive_losses": max_streak,
            "daily_realized_pnl": dict(sorted(daily.items())),
            "last_new_complete_trade_count": int(len(eligible)),
            "last_ignored_prestart_trade_count": int(len(ignored_prestart)),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "updated_as_of_date": today,
        }
    )
    if equity <= 0 or peak <= 0:
        raise AccountRiskShadowError("影子净值或峰值不大于0，拒绝生成误导结论")

    today_pnl = float(daily.get(today, 0.0))
    equity_before_today = equity - today_pnl
    daily_return = today_pnl / equity_before_today if equity_before_today > 0 else 0.0
    drawdown = equity / peak - 1.0
    triggers: list[str] = []
    if daily_return <= -normalized["daily_loss_limit"]:
        triggers.append("DAILY_REALIZED_LOSS")
    if drawdown <= -normalized["drawdown_limit"]:
        triggers.append("ACCOUNT_DRAWDOWN")
    if streak >= normalized["streak_limit"]:
        triggers.append("CONSECUTIVE_LOSSES")

    sample_count = int(state["observed_complete_trade_count"])
    activation_eligible = sample_count >= normalized["minimum_samples"]
    if initialized_now:
        status = "BOOTSTRAPPED"
        reason = "已用当前策略净值建立影子基线；历史成交仅标记为已包含，不重复累计。"
    elif triggers and not activation_eligible:
        status = "EARLY_WARNING_TRIGGERED"
        reason = "影子阈值已触发，但新增完整样本不足；只告警和记录，不改变实盘。"
    elif triggers:
        status = "SHADOW_TRIGGERED"
        reason = "账户级影子阈值已触发；当前仍只记录假设暂停动作，不改变实盘。"
    elif not activation_eligible:
        status = "INSUFFICIENT_SAMPLE"
        reason = (
            f"影子新增完整成交{sample_count}笔，少于激活复核门槛"
            f"{normalized['minimum_samples']}笔；继续观察，不改变实盘。"
        )
    else:
        status = "NORMAL"
        reason = "影子样本达到复核门槛，当前未触发账户级风险阈值。"

    suggested_action = "NONE"
    if "ACCOUNT_DRAWDOWN" in triggers or "CONSECUTIVE_LOSSES" in triggers:
        suggested_action = (
            f"HYPOTHETICAL_PAUSE_NEW_ENTRIES_{normalized['cooldown_trade_days']}_TRADE_DAYS"
        )
    elif "DAILY_REALIZED_LOSS" in triggers:
        suggested_action = "HYPOTHETICAL_PAUSE_NEW_ENTRIES_UNTIL_NEXT_TRADE_DAY"
    result = {
        "schema_version": 1,
        "policy_id": normalized["policy_id"],
        "status": status,
        "reason": reason,
        "mode": "SHADOW_ONLY",
        "enforce_live_gate": False,
        "changes_live_orders": False,
        "initialized_now": initialized_now,
        "as_of_date": today,
        "new_complete_trade_count": int(len(eligible)),
        "observed_complete_trade_count": sample_count,
        "minimum_complete_trades_for_activation_review": normalized["minimum_samples"],
        "activation_review_eligible": activation_eligible,
        "current_equity": equity,
        "peak_equity": peak,
        "account_drawdown": drawdown,
        "account_drawdown_limit": -normalized["drawdown_limit"],
        "today_realized_pnl": today_pnl,
        "today_realized_return_on_start_equity": daily_return,
        "daily_realized_loss_limit": -normalized["daily_loss_limit"],
        "consecutive_losses": streak,
        "consecutive_loss_limit": normalized["streak_limit"],
        "triggers": triggers,
        "suggested_action": suggested_action,
        "release_requirement": (
            "任何实盘接入必须先做历史/样本外验证，并通过当前组合总复利70%硬底线；"
            "本影子结果本身没有下单权限。"
        ),
    }
    _atomic_json(state_path, state)
    _atomic_json(latest_status_path, result)
    return result
