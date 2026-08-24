#!/usr/bin/env python3
"""生成当前A>C>E>D唯一正式的真实开仓日严格as-of机械复利证书。

本脚本固定当前规则，不优化参数；只从逐日as-of成交评分源重建候选，并按
单账户真实开仓日与占仓顺序执行 ``equity *= 1 + account_return``。输出只用于研究统计，
不参与实盘程序启停或BUY控制。

首次建立或明确接受输入变化时：
    python3 scripts/certify_strict_asof_portfolio.py --refresh-input-manifest

日常复核：
    python3 scripts/certify_strict_asof_portfolio.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import certify_current_executable_portfolio as legacy  # noqa: E402
from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.optimize_strategy_d_factor_union import (  # noqa: E402
    build_incumbent_and_other_legs,
    load_events as load_d_events,
)
from src.live_certification import (  # noqa: E402
    certification_config_sha256,
    certification_file_sha256,
    certification_file_size,
    certification_files_sha256,
)
from src.mechanical_compound import (  # noqa: E402
    MECHANICAL_COMPOUND_STANDARD_ID,
    mechanical_compound_frame,
)
from src.strategy_d_factor_rules import (  # noqa: E402
    add_factor_values as add_d_factor_values,
    load_factor_release as load_d_factor_release,
)
from src.strict_asof import (  # noqa: E402
    STRICT_ASOF_STANDARD_ID,
    STRICT_DISCOVERY,
)


OUTPUT_DIR = ROOT / "reports" / "current_portfolio_alignment"
CERTIFICATION_PATH = OUTPUT_DIR / "live_certification.json"
AUDIT_PATH = OUTPUT_DIR / "strict_asof_audit.json"
REPORT_PATH = OUTPUT_DIR / "strict_asof_portfolio_report.md"
MANIFEST_PATH = OUTPUT_DIR / "strict_asof_input_manifest.json"
DAILY_DIR = ROOT / "data" / "raw" / "daily"
DAILY_BASIC_DIR = ROOT / "data" / "raw" / "daily_basic"
EXPECTED_PRIORITY = ["A", "C", "E", "D"]
EXPECTED_TRADE_COUNT = 136
EXPECTED_EQUITY_MULTIPLE = 1023.791243962826
EXPECTED_MAX_DRAWDOWN = -0.14119813241960621
EXPECTED_LEG_COUNTS = {"A": 42, "C": 47, "E": 36, "D": 11}
LOGGER = logging.getLogger("strict_portfolio_certifier")
# 2026-06-30 信号的 C/E T+3 退出在极端跌停时最多继续检查 4 个交易日；
# 输入清单保守锁到 2026-07-10，覆盖正常退出和延期卖出行情。
INPUT_END_BUFFER = "20260710"
D_EVENT_PATH = ROOT / "reports/strategy_d_reseal_combinations/all_reseal_signal_events.csv"
D_RELEASE_PATH = ROOT / "config/strategy_d_factor_release.json"

CODE_FILES = [
    "scripts/certify_strict_asof_portfolio.py",
    "scripts/validate_other_live_strategies_strict.py",
    "scripts/certify_current_executable_portfolio.py",
    "scripts/backtest_strategy_d.py",
    "scripts/build_ac_daily_candidates.py",
    "scripts/optimize_strategy_d_factor_union.py",
    "scripts/optimize_strict_acde_from_official_baseline.py",
    "scripts/research_strategy_d_explosion_features.py",
    "scripts/research_strategy_d_reseal_combinations.py",
    "scripts/run_paper_ab_filtered_daily_ops.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/verify_strategy_e_alignment.py",
    "src/adjusted_returns.py",
    "src/live_certification.py",
    "src/market_rules.py",
    "src/mechanical_compound.py",
    "src/paper_candidate_generator.py",
    "src/strategy_d_spec.py",
    "src/strategy_d_factor_rules.py",
    "src/strategy_e.py",
    "src/strategy_optimizer.py",
    "src/strict_asof.py",
    "src/trading_fees.py",
]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_strict_report(
    certification: dict[str, Any],
    leg_standalone_metrics: dict[str, dict[str, Any]],
) -> None:
    """输出与正式JSON同口径的可读报告，禁止复用旧身份回放Markdown。"""

    lines = [
        "# A>C>E>D 真实开仓日严格 as-of 组合证书",
        "",
        f"- 锚点窗口：{certification['input_start_date']}～{certification['input_end_date']}",
        "- 复利口径：单账户逐笔机械复利，真实开仓日按A>C>E>D裁决，占仓82.5%",
        "- 研究口径：STRICT_DISCOVERY（只作统计审计，不参与实盘BUY控制）",
        f"- 组合成交：{certification['executed_trade_count']}笔",
        f"- 组合复利：{certification['equity_multiple']:.12f}倍",
        f"- 胜率：{certification['win_rate']:.4%}",
        f"- 平均/中位账户收益：{certification['avg_return']:.4%} / {certification['median_return']:.4%}",
        f"- 最大回撤：{certification['max_drawdown']:.4%}",
        f"- 最大单笔盈利/亏损：{certification['max_profit']:.4%} / {certification['max_loss']:.4%}",
        f"- 盈亏比：{certification['profit_loss_ratio']:.6f}",
        f"- 最大连续亏损：{certification['max_consecutive_losses']}笔",
        "",
        "| 策略腿 | 独立成交数 | 独立单账户复利 | 胜率 | 平均收益 | 最大回撤 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for leg in EXPECTED_PRIORITY:
        item = leg_standalone_metrics[leg]
        lines.append(
            f"| {leg} | {int(item['trade_count'])} | {float(item['equity_multiple']):.6f}倍 | "
            f"{float(item['win_rate']):.2%} | {float(item['avg_account_return']):.2%} | "
            f"{float(item['max_drawdown']):.2%} |"
        )
    lines.extend(
        [
            "",
            "## 口径与限制",
            "",
            "- 2024-06-30为非交易日，首个可用信号日自然为2024-07-01；自然日边界未改写。",
            "- A/C/E使用上一交易日收盘后计划并在buy_date开仓；三腿都无计划时D才在action_date盘中运行。",
            "- 持仓在退出日收盘后才释放，退出日不允许同一账户再开新仓。",
            "- 费用、滑点、涨跌停、T+1、跌停延期卖出和D成交压力折扣均已保留。",
            "- 用户于2026-08-24明确接受A>E>C>D的1164.500295倍下降为A>C>E>D的1023.791244倍，作为提升C优先级的人工覆盖决定。",
            "- 本窗口参与规则研究，属于STRICT_DISCOVERY；尚未完成冻结样本外或walk-forward发布认证。",
            "- 历史机械复利不等于大资金可成交收益，也不代表未来收益。",
            "",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _input_files() -> list[Path]:
    direct = [
        strict.STRICT_SOURCE,
        ROOT / "config" / "config.json",
        strict.STRATEGY_CONFIG,
        D_RELEASE_PATH,
        D_EVENT_PATH,
        legacy.E_SPEC_PATH,
        legacy.TRADE_CALENDAR_PATH,
        legacy.STOCK_BASIC_PATH,
    ]
    daily = sorted(
        path
        for path in DAILY_DIR.glob("????????.csv")
        if strict.START <= path.stem <= INPUT_END_BUFFER
    )
    daily_basic = sorted(
        path
        for path in DAILY_BASIC_DIR.glob("????????.csv")
        if strict.START <= path.stem <= INPUT_END_BUFFER
    )
    return direct + daily + daily_basic


def write_or_verify_input_manifest(*, refresh: bool) -> Path:
    files = _input_files()
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise FileNotFoundError("严格as-of认证输入缺失：" + "；".join(missing))
    rows = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "size": certification_file_size(path),
            "sha256": certification_file_sha256(path),
        }
        for path in files
    ]
    manifest = {
        "schema_version": 1,
        "standard_id": STRICT_ASOF_STANDARD_ID,
        "compound_standard_id": MECHANICAL_COMPOUND_STANDARD_ID,
        "window": f"{strict.START}~{strict.END}",
        "input_end_buffer": INPUT_END_BUFFER,
        "strategy_priority_order": EXPECTED_PRIORITY,
        "file_count": len(rows),
        "files": rows,
    }
    legacy.lock_or_verify_input_manifest(MANIFEST_PATH, manifest, refresh=refresh)
    return MANIFEST_PATH


def build_strict_snapshot() -> tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]]:
    # D已经发布为固定因子版本，不能再用旧D条件重建正式证书。这里与D/C研究
    # 的逐腿替换口径共用同一发布读取器，并由下方136笔/1023.7912倍锚点锁死。
    d_events, _d_event_audit = load_d_events(
        D_EVENT_PATH, strict.START, strict.END
    )
    strategy_d, other_legs, source_audit = build_incumbent_and_other_legs(
        load_d_factor_release(D_RELEASE_PATH),
        add_d_factor_values(d_events),
        strict.START,
        strict.END,
    )
    if not bool(source_audit.get("passed")):
        raise RuntimeError("严格as-of源审计未通过，拒绝生成组合证书")
    legs = {"D": strategy_d, **other_legs}
    daily = strict.replay_by_action_date(legs, EXPECTED_PRIORITY)
    metrics = strict.combo_metrics(daily)
    trades = daily[
        daily["status"].eq("EXECUTED")
        & daily["signal_date"].between(strict.START, strict.END)
    ].copy()
    mechanical = mechanical_compound_frame(trades)
    if metrics["compound_standard_id"] != mechanical.standard_id:
        raise RuntimeError("严格组合没有使用统一机械复利标准")
    if int(metrics["trade_count"]) != EXPECTED_TRADE_COUNT:
        raise RuntimeError(
            f"严格组合成交数漂移：期望{EXPECTED_TRADE_COUNT}，实际{metrics['trade_count']}"
        )
    if abs(float(metrics["equity_multiple"]) - EXPECTED_EQUITY_MULTIPLE) > 1e-9:
        raise RuntimeError(
            "严格组合机械复利漂移："
            f"期望{EXPECTED_EQUITY_MULTIPLE}，实际{metrics['equity_multiple']}"
        )
    if abs(float(metrics["max_drawdown"]) - EXPECTED_MAX_DRAWDOWN) > 1e-9:
        raise RuntimeError("严格组合最大回撤漂移，拒绝静默改写正式口径")
    actual_counts = {leg: int(metrics["leg_counts"].get(leg, 0)) for leg in EXPECTED_PRIORITY}
    if actual_counts != EXPECTED_LEG_COUNTS:
        raise RuntimeError(
            f"严格组合分腿样本漂移：期望{EXPECTED_LEG_COUNTS}，实际{actual_counts}"
        )
    return source_audit, daily, legs


def certify(*, refresh_input_manifest: bool = False) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "开始严格组合认证：window=%s~%s priority=%s position=82.5%% refresh_manifest=%s",
        strict.START,
        strict.END,
        ">".join(EXPECTED_PRIORITY),
        refresh_input_manifest,
    )
    _atomic_json(
        CERTIFICATION_PATH,
        {
            "schema_version": 2,
            "scenario": "current_a_c_e_d",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "note": "严格as-of研究统计重建中；不参与实盘程序启停或BUY控制。",
        },
    )
    manifest_path = write_or_verify_input_manifest(refresh=refresh_input_manifest)
    LOGGER.info("输入清单校验通过：%s", manifest_path)
    source_audit, daily, legs = build_strict_snapshot()
    # 日账本必须保留NO_CANDIDATE与SKIP_OCCUPIED，不能再用signal_date筛掉；
    # 组合候选本身已在build_incumbent_and_other_legs中锁定研究窗口。
    sample = daily.copy()
    trades = sample[sample["status"].eq("EXECUTED")].copy()
    metrics = strict.combo_metrics(sample)
    mechanical = mechanical_compound_frame(trades)
    LOGGER.info(
        "严格回放复现：action_days=%d executed=%d multiple=%.12f max_drawdown=%.6f legs=%s",
        len(sample),
        int(metrics["trade_count"]),
        mechanical.equity_multiple,
        mechanical.max_drawdown,
        metrics["leg_counts"],
    )

    sample.to_csv(OUTPUT_DIR / "strict_asof_portfolio_daily.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "strict_asof_portfolio_trades.csv", index=False, encoding="utf-8-sig")
    leg_candidate_metrics: dict[str, dict[str, Any]] = {}
    leg_standalone_metrics: dict[str, dict[str, Any]] = {}
    for leg, frame in legs.items():
        executed = frame[frame["status"].astype(str).eq("OK")]
        leg_candidate_metrics[leg] = strict.return_metrics(executed["account_return"])
        # 独立策略仍必须遵守自己的持仓期和资金释放日；不能将重叠候选全部连乘。
        leg_standalone_metrics[leg] = strict.combo_metrics(
            strict.replay_by_action_date({leg: frame}, (leg,))
        )
        LOGGER.info(
            "%s腿指标：候选池=%d笔/%.12f倍；独立单账户=%d笔/%.12f倍",
            leg,
            int(leg_candidate_metrics[leg]["trade_count"]),
            float(leg_candidate_metrics[leg]["equity_multiple"]),
            int(leg_standalone_metrics[leg]["trade_count"]),
            float(leg_standalone_metrics[leg]["equity_multiple"]),
        )

    audit = {
        "schema_version": 1,
        "standard_id": STRICT_ASOF_STANDARD_ID,
        "asof_mode": "STRICT",
        "strict_asof_passed": True,
        "research_protocol": STRICT_DISCOVERY,
        "result_scope": "DISCOVERY_ONLY",
        "compound_standard_id": MECHANICAL_COMPOUND_STANDARD_ID,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window": f"{strict.START}~{strict.END}",
        "source_audit": source_audit,
        "selection_policy": "固定当前A/C/E/D规则；候选只读各自决策时点可见字段，不按结果回头选规则",
        "execution_policy": (
            "A/C/E为上一交易日收盘计划、buy_date开盘；三腿均无计划时D才在当日盘中运行；"
            "D按信号日涨停价；跌停卖出延期；前复权链接；"
            "双边滑点、佣金、过户费、日期化印花税"
        ),
        "portfolio_policy": "真实开仓日按A>C>E>D单账户占仓；每天最多一笔实际成交；退出日收盘后才释放资金",
        "mechanical_compound_formula": "equity_t = equity_(t-1) * (1 + account_return_t)",
        "strict_combo": metrics,
        "strict_leg_candidate_metrics": leg_candidate_metrics,
        "strict_leg_standalone_metrics": leg_standalone_metrics,
        "limitations": [
            "当前规则在该历史窗口内研究或精修，因此属于STRICT_DISCOVERY，不是untouched OOS。",
            "机械复利不代表大资金可按同倍数成交，也不是未来收益承诺。",
            "严格as-of通过只证明没有使用决策时点之后的数据，不自动证明策略可实盘发布。",
        ],
    }
    _atomic_json(AUDIT_PATH, audit)

    runtime_config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
    manifest_rel = manifest_path.relative_to(ROOT).as_posix()
    audit_rel = AUDIT_PATH.relative_to(ROOT).as_posix()
    certification = {
        "schema_version": 2,
        "scenario": "current_a_c_e_d",
        "metric_scope": "STRICT_ASOF_MECHANICAL_COMPOUND",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input_start_date": strict.START,
        "input_end_date": strict.END,
        "signal_day_count": int(len(strict.baseline_dates())),
        "action_day_count": int(len(sample)),
        "executed_trade_count": int(metrics["trade_count"]),
        "a_trade_count": int(metrics["leg_counts"].get("A", 0)),
        "c_trade_count": int(metrics["leg_counts"].get("C", 0)),
        "d_trade_count": int(metrics["leg_counts"].get("D", 0)),
        "e_trade_count": int(metrics["leg_counts"].get("E", 0)),
        "strategy_priority_order": EXPECTED_PRIORITY,
        "equity_multiple": mechanical.equity_multiple,
        "total_compound_return": mechanical.total_compound_return,
        "win_rate": float(metrics["win_rate"]),
        "avg_return": float(metrics["avg_account_return"]),
        "median_return": float(metrics["median_account_return"]),
        "max_drawdown": mechanical.max_drawdown,
        "max_profit": float(metrics["max_profit"]),
        "max_loss": float(metrics["max_loss"]),
        "profit_loss_ratio": float(metrics["profit_loss_ratio"]),
        "max_consecutive_losses": int(metrics["max_consecutive_losses"]),
        "initial_equity": float(runtime_config["portfolio_certification"]["initial_equity"]),
        "position_pct": float(runtime_config["portfolio_certification"]["position_pct"]),
        "strict_asof_standard_id": STRICT_ASOF_STANDARD_ID,
        "strict_asof_passed": True,
        "compound_standard_id": MECHANICAL_COMPOUND_STANDARD_ID,
        "research_protocol": STRICT_DISCOVERY,
        "strict_asof_audit_path": audit_rel,
        "strict_asof_audit_sha256": certification_file_sha256(AUDIT_PATH),
        "config_sha256": certification_config_sha256(runtime_config),
        "code_files": CODE_FILES,
        "code_sha256": certification_files_sha256(ROOT, CODE_FILES),
        "input_files": [manifest_rel],
        "input_sha256": certification_files_sha256(ROOT, [manifest_rel]),
        "legacy_identity_alignment_path": (
            "reports/current_portfolio_alignment/legacy_identity_alignment.json"
        ),
        "note": (
            "这是当前组合唯一正式统计口径：严格as-of、单账户、逐笔机械复利。"
            "A/C/E按上一交易日收盘计划映射到真实buy_date，三腿均无计划才启动D。"
            "用户已明确接受组合历史复利从A>E>C>D的1164.500295倍下降到"
            "A>C>E>D的1023.791244倍。"
            "当前协议仍是STRICT_DISCOVERY；研究结果不参与实盘程序启停或BUY控制，"
            "旧信号日排序1375.623853倍与同日重排1463.912878倍不得用于正式收益或比较。"
        ),
    }
    _atomic_json(CERTIFICATION_PATH, certification)
    _write_strict_report(certification, leg_standalone_metrics)
    LOGGER.info(
        "STRICT_DISCOVERY统计完成；不参与实盘BUY控制。certificate=%s audit=%s report=%s",
        CERTIFICATION_PATH,
        AUDIT_PATH,
        REPORT_PATH,
    )
    return certification


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="生成当前组合严格as-of机械复利正式证书")
    parser.add_argument(
        "--refresh-input-manifest",
        action="store_true",
        help="确认输入变化后显式刷新严格认证输入清单",
    )
    args = parser.parse_args()
    result = certify(refresh_input_manifest=args.refresh_input_manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
