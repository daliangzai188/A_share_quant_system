"""发布版本OOS日评日志、历史快照和周报提醒。"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from src.shadow_candidate_ledger import _date, _read_csv, _read_json, load_open_dates


NotifyFunction = Callable[..., bool]


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _atomic_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _atomic_text(content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _iso_week(value: str) -> str:
    parsed = datetime.strptime(_date(value), "%Y%m%d").date()
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def is_last_open_day_of_week(root: Path, signal_date: str) -> bool:
    signal_date = _date(signal_date)
    dates = load_open_dates(root)
    if signal_date not in dates:
        return False
    future = [date for date in dates if date > signal_date]
    return not future or _iso_week(future[0]) != _iso_week(signal_date)


def _float(value: object) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _leg_summary(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for row in payload.get("by_leg_metrics", []) or []:
        leg = str(row.get("segment", "")).replace("策略", "")
        count = int(_float(row.get("sample_count")))
        avg = _float(row.get("avg_return"))
        parts.append(f"{leg}:{count}笔/{avg:+.2%}")
    return "，".join(parts) if parts else "各腿均无已完成样本"


def build_ai_review_prompt(
    payload: Mapping[str, Any],
    signal_date: str,
    *,
    compact: bool = False,
) -> str:
    """生成不依赖具体模型的OOS人工复核任务说明。"""

    release_id = str(payload.get("release_id", ""))
    oos_start = str(payload.get("oos_start_date", ""))
    minimum = int(_float(payload.get("minimum_samples_for_review")) or 20)
    if compact:
        return (
            f"请审计A股实盘发布{release_id}（OOS起点{oos_start}，截至{_date(signal_date)}）。"
            "总目的：在尽量少损伤历史总复利的前提下，用真正样本外和真实成交判断当前实盘是否过拟合、"
            "腿序是否浪费更优机会、收益损失是否来自执行，并提升稳健性、回撤控制和真实可成交性；"
            "不是追逐最近涨得最好的策略。"
            "读取或要求我上传release_oos_report.md、release_oos_status.json、release_oos_by_leg.csv、"
            "release_oos_priority_pairs.csv、shadow_candidates.csv和trade_completion_summary.csv。"
            "先核对发布编号、日期范围、四腿覆盖、缺失/重复/未完成成交，禁止混入OOS起点前数据；"
            "再按整体、D/A/E/C、冻结优先级胜出组合、真实成交列出样本数、胜率、平均/中位收益、"
            "复利、最大回撤、盈亏比、最大盈亏、连亏、费用滑点和成交完成率；用同一真实开仓日成对反事实判断腿序，"
            "区分策略失效与执行损耗。当前腿序为A>C>E>D：A/C/E读取前一交易日收盘计划，"
            "三腿均无当日可执行计划时D才启动盘中路径；禁止把当日收盘后才产生的未来计划反向用于D门禁。"
            f"优先级影子或真实成交少于{minimum}笔、成对样本少于{minimum}笔时只能HOLD，不得据少量盈亏调参。"
            "先告诉我现在该保持、继续观察还是进入复核；每条建议必须写明改动方向、当前值→建议值、"
            "涉及文件/方法、数据依据、对降低过拟合/保留复利/改善回撤或成交的好处，以及可能损失的收益、"
            "频率和新风险。不要修改代码、配置或实盘，只给按优先级排序的待确认方案和验证步骤。"
        )
    return f"""# 发布版本样本外报告——AI复核任务

你是一名独立的A股量化实盘审计助手。请审计发布版本 `{release_id}`，样本外起点为 `{oos_start}`，本次截止信号日为 `{_date(signal_date)}`。

## 总目的

这项工作的最终目的不是单纯生成统计，也不是追逐最近涨得最好的股票或策略，而是：

1. 在尽量少损伤历史总复利的前提下，判断当前实盘组合是否存在过拟合、失效或市场适应性下降。
2. 判断 `A > C > E > D` 的资金优先级是否造成可重复的机会成本，而不是被一两次个案误导。
3. 区分选股策略问题与真实执行问题，避免因为滑点、部分成交或卖出损耗错误删除有效策略。
4. 找出能够提高样本外稳定性、降低最大回撤、改善资金利用率和真实可成交性的改动方向。
5. 最终给用户一份可人工确认的实盘优化方案，并明确用户现在应该“保持现状、继续积累数据、启动专项复核”中的哪一种；本轮不直接改实盘。

## 输入资料

请直接读取下列文件；如果你无法访问项目文件，先要求我上传这些文件，不要凭通知摘要猜结论：

1. `reports/oos_evaluation/release_oos_report.md`
2. `reports/oos_evaluation/release_oos_status.json`
3. `reports/oos_evaluation/release_oos_metrics.csv`
4. `reports/oos_evaluation/release_oos_by_leg.csv`
5. `reports/oos_evaluation/release_oos_priority_pairs.csv`
6. `reports/oos_evaluation/release_oos_coverage.csv`
7. `reports/oos_shadow/shadow_candidates.csv`
8. `reports/execution_tracking/trade_completion_summary.csv`
9. `config/strategy_release_freeze.json`
10. `reports/current_portfolio_alignment/live_certification.json`

## 必须完成的检查

1. 数据质量：核对发布编号、OOS起点、日期范围、四腿覆盖率、缺失值、重复记录、未完成交易、一字涨停买不到、跌停卖出顺延；禁止混入OOS起点之前或其他发布版本的数据。
2. 收益与风险：分别对全部影子候选、D/A/E/C、账户空仓时冻结优先级胜出组合、实际进入实盘的候选和真实完整成交，列出样本数、胜率、平均收益、中位数收益、复利、最大回撤、盈亏比、最大盈利、最大亏损、连续亏损、费用、滑点及成交完成率。
3. 腿序反事实：只能使用同一真实开仓日（`planned_buy_date/action_date`）的成对样本比较高低优先级，报告平均收益差、中位数收益差、胜出率、复利差和回撤差，不能把相同信号日但实际在不同日期开仓的候选强行配对。
4. 原因归因：区分策略信号失效、优先级机会成本、成交概率不足、开盘滑点、卖出损耗、仓位/容量问题和偶然波动，禁止把执行问题误判为选股问题。
5. D策略时序：A/C/E使用前一交易日收盘后已确定、当日可执行的计划，按A>C>E取第一条；三腿均无当日计划时，D才启动盘中回封路径。当日收盘后新产生的A/C/E计划属于下一交易日，禁止反向阻断当日D。若建议D与静态腿切换，必须按相同真实开仓日重放费用、滑点、成交可行性和收益，不得只比较两只股票涨幅。
6. 过拟合约束：优先级影子样本或真实完整成交少于{minimum}笔时，只能标记 `HOLD_RELEASE`；任何腿序调整的同一真实开仓日成对样本少于{minimum}笔时，不得换序。不得根据少量连亏新增过滤条件或追逐近期最优参数。
7. 建议分类：只能从“保持现状、复核腿序、复核仓位、优化执行、复核停腿、证据不足”中选择，并逐条给出支持证据、反证、风险和置信程度。

## 每条改动建议必须写清楚

如果认为需要优化，每一条建议都必须单独说明：

1. **改动方向**：属于腿序、仓位、执行方式、候选过滤、持有期、风控还是策略开关。
2. **具体改动内容**：当前规则或参数是什么，建议改成什么；涉及哪个配置项、文件和方法。不能只写“建议优化D”之类的空话。
3. **数据依据**：使用了多少样本，样本外收益、回撤、盈亏比、同日成对差、成交率和滑点分别如何。
4. **改动的好处**：预计解决什么问题，例如降低过拟合、减少错误优先级占资、降低回撤、改善成交、保留更多复利或提高可维护性。
5. **代价和风险**：可能牺牲多少交易频率、右尾盈利、历史复利或行情适应性；是否可能把正常回撤误判为失效。
6. **证据强度**：标明高/中/低，并说明还差多少样本或验证。
7. **用户下一步**：明确写“现在无需操作”“继续积累到多少笔”“建议让开发助手先做专项回测但不要改实盘”或“证据充分，可进入代码修改审批”。

## 输出格式

1. 先用一句话给出结论，并明确用户现在需要做什么。
2. 数据质量与样本有效性。
3. 收益、回撤及成交统计表。
4. 分策略和同日成对反事实结论。
5. 是否需要改变实盘；若需要，用表格列出“优先级、改动方向、当前→建议、数据依据、改动好处、代价风险、证据强度、用户下一步”。
6. 反对该修改的证据和最坏风险。
7. 后续验证方案：历史重新认证、样本外检查、dry-run/模拟、小资金验证、新发布编号。

本轮不要修改任何代码、配置、策略开关、腿序、仓位或实盘状态，也不要承诺收益。只提交可供人工确认的分析和修改方案。
"""


def daily_log_lines(payload: Mapping[str, Any], signal_date: str) -> list[str]:
    priority = payload.get("priority_winner_metrics", {}) or {}
    minimum = int(_float(payload.get("minimum_samples_for_review")) or 20)
    winner_count = int(_float(payload.get("priority_winner_resolved_count")))
    actual_count = int(_float(payload.get("actual_complete_trade_count")))
    return [
        (
            f"[OOS日评] 发布={payload.get('release_id', '')} 信号日={_date(signal_date)} "
            f"状态={payload.get('status', '')} 评估覆盖={_float(payload.get('evaluated_coverage')):.0%} "
            f"候选={int(_float(payload.get('candidate_count')))} 已完成={int(_float(payload.get('resolved_candidate_count')))} "
            f"优先级样本={winner_count}/{minimum} 真实成交={actual_count}/{minimum}"
        ),
        (
            f"[OOS日评] 优先级组合：平均={_float(priority.get('avg_return')):+.2%} "
            f"中位数={_float(priority.get('median_return')):+.2%} "
            f"复利={_float(priority.get('compound_multiple')):.4f}倍 "
            f"最大回撤={_float(priority.get('max_drawdown')):.2%}；分腿={_leg_summary(payload)}"
        ),
        f"[OOS日评] 结论={payload.get('reason', '')}；动作={payload.get('optimization_decision', 'HOLD_RELEASE')}（不自动改策略/不阻断下单）",
    ]


def _snapshot_row(payload: Mapping[str, Any], signal_date: str) -> dict[str, Any]:
    priority = payload.get("priority_winner_metrics", {}) or {}
    actual = payload.get("actual_metrics", {}) or {}
    return {
        "release_id": str(payload.get("release_id", "")),
        "signal_date": _date(signal_date),
        "iso_week": _iso_week(signal_date),
        "status": str(payload.get("status", "")),
        "optimization_decision": str(payload.get("optimization_decision", "")),
        "signal_day_count": int(_float(payload.get("signal_day_count"))),
        "evaluated_coverage": _float(payload.get("evaluated_coverage")),
        "candidate_count": int(_float(payload.get("candidate_count"))),
        "resolved_candidate_count": int(_float(payload.get("resolved_candidate_count"))),
        "priority_winner_resolved_count": int(_float(payload.get("priority_winner_resolved_count"))),
        "priority_avg_return": _float(priority.get("avg_return")),
        "priority_median_return": _float(priority.get("median_return")),
        "priority_compound_multiple": _float(priority.get("compound_multiple")),
        "priority_max_drawdown": _float(priority.get("max_drawdown")),
        "actual_complete_trade_count": int(_float(payload.get("actual_complete_trade_count"))),
        "actual_avg_return": _float(actual.get("avg_return")),
        "actual_max_drawdown": _float(actual.get("max_drawdown")),
        "generated_at": str(payload.get("generated_at", "")),
    }


def _upsert_history(path: Path, row: Mapping[str, Any], keys: list[str]) -> pd.DataFrame:
    old = _read_csv(path)
    incoming = pd.DataFrame([dict(row)])
    combined = pd.concat([old, incoming], ignore_index=True, sort=False) if not old.empty else incoming
    for key in keys:
        combined[key] = combined[key].fillna("").astype(str)
    combined = combined.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
    _atomic_csv(combined, path)
    return combined


def _weekly_body(payload: Mapping[str, Any], signal_date: str) -> str:
    priority = payload.get("priority_winner_metrics", {}) or {}
    pair_rows = payload.get("priority_pair_metrics", []) or []
    review_pairs = [str(row.get("challenger_leg", "")) for row in pair_rows if row.get("priority_change_evidence") == "REVIEW"]
    pair_text = "达到复核条件:" + "/".join(review_pairs) if review_pairs else "暂无腿序替换达到成对样本门槛"
    summary = (
        f"{_iso_week(signal_date)} 截至{_date(signal_date)}：观察{int(_float(payload.get('signal_day_count')))}个信号日，"
        f"评估覆盖{_float(payload.get('evaluated_coverage')):.0%}，候选{int(_float(payload.get('candidate_count')))}个/完成{int(_float(payload.get('resolved_candidate_count')))}个；"
        f"优先级样本{int(_float(payload.get('priority_winner_resolved_count')))}笔，平均{_float(priority.get('avg_return')):+.2%}，"
        f"中位数{_float(priority.get('median_return')):+.2%}，复利{_float(priority.get('compound_multiple')):.4f}倍，回撤{_float(priority.get('max_drawdown')):.2%}；"
        f"真实完整成交{int(_float(payload.get('actual_complete_trade_count')))}笔。{pair_text}。"
        f"结论:{payload.get('reason', '')} 动作:{payload.get('optimization_decision', 'HOLD_RELEASE')}，不会自动改策略。"
    )
    return summary + "\n\n【复制给AI】\n" + build_ai_review_prompt(payload, signal_date, compact=True)


def record_and_maybe_remind(
    root: Path,
    signal_date: str,
    payload: Mapping[str, Any],
    notify_func: NotifyFunction | None = None,
) -> dict[str, Any]:
    """每日落历史快照；每周最后交易日幂等推一次周报。"""

    output = root / "reports" / "oos_evaluation"
    row = _snapshot_row(payload, signal_date)
    prompt_path = output / "ai_review_prompt.md"
    _atomic_text(build_ai_review_prompt(payload, signal_date), prompt_path)
    _upsert_history(output / "release_oos_daily_history.csv", row, ["release_id", "signal_date"])
    weekly = is_last_open_day_of_week(root, signal_date)
    sent = False
    week_key = f"{row['release_id']}|{row['iso_week']}"
    console_lines: list[str] = []
    if weekly:
        _upsert_history(output / "release_oos_weekly_history.csv", row, ["release_id", "iso_week"])
        state_path = root / "data" / "state" / "oos_analysis_reminder_state.json"
        state = _read_json(state_path, {})
        state_changed = False
        if str(state.get("last_console_week_key", "")) != week_key:
            console_lines = [
                "[OOS周报复制开始] 复制以下内容给任意AI；本轮只分析，不直接修改代码或实盘。",
                *[f"[OOS周报复制] {line}" for line in build_ai_review_prompt(payload, signal_date).splitlines()],
                "[OOS周报复制结束]",
            ]
            state["last_console_week_key"] = week_key
            state["last_console_signal_date"] = _date(signal_date)
            state_changed = True
        if str(state.get("last_week_key", "")) != week_key:
            if notify_func is None:
                from src.notify import notify as notify_func
            sent = bool(notify_func(
                "daily_summary",
                "📊 发布版本样本外周报",
                _weekly_body(payload, signal_date),
                level="active",
            ))
            if sent:
                state["last_week_key"] = week_key
                state["last_signal_date"] = _date(signal_date)
                state_changed = True
        if state_changed:
            state["updated_at"] = datetime.now().isoformat()
            _atomic_json(state, state_path)
    return {
        "log_lines": daily_log_lines(payload, signal_date),
        "is_weekly_report_day": weekly,
        "weekly_notification_sent": sent,
        "week_key": week_key,
        "ai_review_prompt_path": str(prompt_path),
        "weekly_console_lines": console_lines,
    }
