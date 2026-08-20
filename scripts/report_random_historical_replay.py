#!/usr/bin/env python3
"""生成当前冻结发布版本的随机连续历史区间伪实盘压力测试。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_current_executable_portfolio import (  # noqa: E402
    load_sources,
    replay,
    summarize,
)
from src.historical_random_replay import (  # noqa: E402
    build_market_context,
    run_random_windows,
)
from src.live_certification import (  # noqa: E402
    certification_config_sha256,
    certification_files_sha256,
    validate_strategy_release_freeze,
)


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "historical_random_replay.json"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
CERTIFICATION_PATH = (
    PROJECT_ROOT / "reports" / "current_portfolio_alignment" / "live_certification.json"
)
def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON根节点不是对象：{path}")
    return payload


def _resolve(value: Any) -> Path:
    path = Path(str(value or "").strip())
    return path if path.is_absolute() else PROJECT_ROOT / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _markdown_table(frame: pd.DataFrame, percent: set[str] | None = None) -> str:
    if frame.empty:
        return "暂无数据。"
    percent = percent or set()
    view = frame.copy()
    for column in view.columns:
        if column in percent:
            view[column] = pd.to_numeric(view[column], errors="coerce").map(
                lambda value: f"{value:.2%}"
            )
        elif pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(lambda value: f"{value:.4f}")
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(map(str, row)) + " |"
        for row in view.fillna("").values.tolist()
    )
    return "\n".join(lines)


def _validate_release_binding(
    runtime_config: dict[str, Any], certification: dict[str, Any]
) -> dict[str, Any]:
    portfolio_config = runtime_config.get("portfolio_certification", {})
    check = validate_strategy_release_freeze(
        PROJECT_ROOT, portfolio_config, certification
    )
    if not check.ok:
        raise RuntimeError("冻结发布版本校验失败：" + check.reason)
    expected_config_hash = str(certification.get("config_sha256", ""))
    if certification_config_sha256(runtime_config) != expected_config_hash:
        raise RuntimeError("当前策略配置已偏离冻结认证，拒绝生成随机回放报告")
    code_files = [str(value) for value in certification.get("code_files", [])]
    if certification_files_sha256(PROJECT_ROOT, code_files) != str(
        certification.get("code_sha256", "")
    ):
        raise RuntimeError("当前策略代码已偏离冻结认证，拒绝生成随机回放报告")
    input_files = [str(value) for value in certification.get("input_files", [])]
    if certification_files_sha256(PROJECT_ROOT, input_files) != str(
        certification.get("input_sha256", "")
    ):
        raise RuntimeError("当前历史输入已偏离冻结认证，拒绝生成随机回放报告")
    return check.payload


def _build_report(
    *,
    config: dict[str, Any],
    release: dict[str, Any],
    certification: dict[str, Any],
    result: dict[str, pd.DataFrame],
    market_source: Path,
) -> str:
    summary = result["summary"]
    regimes = result["regimes"]
    legs = result["legs"]
    short_windows = summary[summary["window_length"].eq(20)]
    weak_short_windows = regimes[
        regimes["window_length"].eq(20)
        & regimes["market_regime"].astype(str).eq("weak_lt_50")
    ]
    findings: list[str] = []
    if not short_windows.empty:
        row = short_windows.iloc[0]
        findings.append(
            f"- 20日随机窗口盈利比例{float(row['positive_window_rate']):.2%}，"
            f"收益倍数P10={float(row['compound_multiple_p10']):.4f}，"
            f"回撤P10={float(row['max_drawdown_p10']):.2%}。"
        )
    if not weak_short_windows.empty:
        row = weak_short_windows.iloc[0]
        findings.append(
            f"- 弱市20日窗口盈利比例{float(row['positive_window_rate']):.2%}，"
            f"收益倍数P10={float(row['compound_multiple_p10']):.4f}，"
            "短周期弱市是本轮最明确的历史脆弱区，应继续做反事实研究，不能据此直接加过滤。"
        )
    lines = [
        "# 当前冻结发布版本随机历史区间伪实盘压力测试",
        "",
        f"- 发布编号：`{release.get('release_id', '')}`",
        f"- 认证场景：`{certification.get('scenario', '')}`",
        f"- 冻结研究区间：{certification.get('input_start_date', '')}~{certification.get('input_end_date', '')}",
        f"- 随机种子：{config.get('random_seed')}",
        f"- 窗口长度：{config.get('window_lengths')}个连续信号日；每档最多抽取{config.get('samples_per_length')}段",
        f"- 抽样方式：{config.get('sampling_mode')}（按起始年份均衡后随机、同一长度不重复起点）",
        f"- 行情来源：`{market_source.relative_to(PROJECT_ROOT)}`，SHA256=`{_sha256(market_source)}`",
        "",
        "## 本轮发现",
        "",
        *(findings or ["- 暂无足够随机窗口形成分布结论。"]),
        "",
        "## 结果分布",
        "",
        _markdown_table(
            summary,
            {
                "positive_window_rate",
                "max_drawdown_p10",
                "max_drawdown_p50",
                "market_data_coverage",
            },
        ),
        "",
        "## 行情环境分层",
        "",
        _markdown_table(
            regimes,
            {"positive_window_rate", "max_drawdown_p10", "max_drawdown_p50"},
        ),
        "",
        "## 各策略腿唯一历史交易",
        "",
        _markdown_table(
            legs,
            {"unique_win_rate", "unique_avg_return", "unique_median_return"},
        ),
        "",
        "## 正确解释",
        "",
        "1. 这是当前冻结规则在既有历史上的路径压力测试，能快速检验换起点、换持有路径和换行情后是否仍稳定。",
        "2. 随机窗口会重叠，因此窗口数不能当成相互独立的新增交易样本；各腿统计同时保留全历史唯一交易数，避免重复计算误导。",
        "3. 每个窗口只纳入窗口结束日前已经完整退出的交易；右边界未退出交易只计数、不读取其未来收益。",
        "4. 窗口沿用完整历史回放在起点之前形成的持仓与M回撤状态，模拟系统持续运行到随机日期后的表现，不是每段重新调参或重置规则。",
        "5. 这类报告不能冒充真正样本外，也不能替代QMT真实滑点、部分成交和资金容量验证；它不会自动修改任何实盘规则。",
        "",
    ]
    return "\n".join(lines)


def generate_report(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = _read_json(config_path)
    if int(config.get("schema_version", 0)) != 1:
        raise ValueError("随机历史回放配置版本必须为1")
    runtime_config = _read_json(RUNTIME_CONFIG_PATH)
    certification = _read_json(CERTIFICATION_PATH)
    release = _validate_release_binding(runtime_config, certification)

    sources = load_sources()
    detail = replay(
        sources,
        entry_gate_enabled=True,
    )
    replay_metrics = summarize(detail, "random_replay_source_validation")
    expected_count = int(certification.get("executed_trade_count", 0))
    expected_multiple = float(certification.get("equity_multiple", 0.0))
    if replay_metrics["executed_trade_count"] != expected_count:
        raise RuntimeError("随机回放来源与认证成交笔数不一致")
    if abs(replay_metrics["equity_multiple"] - expected_multiple) > 1e-9:
        raise RuntimeError("随机回放来源与认证复利不一致")

    market_source = _resolve(config.get("market_emotion_path"))
    market_features = pd.read_csv(market_source, low_memory=False)
    market_context = build_market_context(market_features)
    result = run_random_windows(
        detail,
        window_lengths=config.get("window_lengths", []),
        samples_per_length=int(config.get("samples_per_length", 0)),
        random_seed=int(config.get("random_seed", 0)),
        sampling_mode=str(config.get("sampling_mode", "balanced_start_year")),
        market_context=market_context,
        regime_breakpoints=config.get("market_regime_breakpoints", []),
        regime_labels=config.get("market_regime_labels", []),
    )
    output = _resolve(config.get("output_dir"))
    _atomic_csv(output / "random_windows.csv", result["windows"])
    _atomic_csv(output / "random_window_trades.csv", result["trades"])
    _atomic_csv(output / "random_window_summary.csv", result["summary"])
    _atomic_csv(output / "random_window_regime_summary.csv", result["regimes"])
    _atomic_csv(output / "random_window_leg_summary.csv", result["legs"])
    _atomic_text(
        output / "random_window_report.md",
        _build_report(
            config=config,
            release=release,
            certification=certification,
            result=result,
            market_source=market_source,
        ),
    )
    status = {
        "schema_version": 1,
        "status": "REPORT_READY",
        "evidence_class": "HISTORICAL_PSEUDO_LIVE_OVERLAPPING_WINDOWS",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release_id": str(release.get("release_id", "")),
        "certification_scenario": str(certification.get("scenario", "")),
        "research_start_date": str(certification.get("input_start_date", "")),
        "research_end_date": str(certification.get("input_end_date", "")),
        "random_seed": int(config.get("random_seed", 0)),
        "window_lengths": [int(value) for value in config.get("window_lengths", [])],
        "sampled_window_count": int(len(result["windows"])),
        "source_executed_trade_count": int(replay_metrics["executed_trade_count"]),
        "source_equity_multiple": float(replay_metrics["equity_multiple"]),
        "automatic_live_change": False,
        "decision": "RESEARCH_EVIDENCE_ONLY",
        "summary_by_window_length": json.loads(
            result["summary"].to_json(orient="records")
        ),
        "summary_by_market_regime": json.loads(
            result["regimes"].to_json(orient="records")
        ),
        "limitations": [
            "随机窗口互相重叠，不是独立样本",
            "既有历史可能参与过策略研发，不是真正样本外",
            "不能替代真实QMT滑点、成交和容量验证",
        ],
        "outputs": {
            "windows": str((output / "random_windows.csv").relative_to(PROJECT_ROOT)),
            "trades": str((output / "random_window_trades.csv").relative_to(PROJECT_ROOT)),
            "summary": str((output / "random_window_summary.csv").relative_to(PROJECT_ROOT)),
            "regimes": str((output / "random_window_regime_summary.csv").relative_to(PROJECT_ROOT)),
            "legs": str((output / "random_window_leg_summary.csv").relative_to(PROJECT_ROOT)),
            "report": str((output / "random_window_report.md").relative_to(PROJECT_ROOT)),
        },
    }
    _atomic_text(
        output / "random_window_status.json",
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
    )
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description="当前冻结版本随机历史时段回放")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="独立研究配置文件，不影响实盘认证配置",
    )
    args = parser.parse_args()
    print(json.dumps(generate_report(args.config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
