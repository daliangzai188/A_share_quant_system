#!/usr/bin/env python3
"""认证已正式落地的 ACDE V12 三年基准；不搜索参数、不连接券商。

运行本脚本前，先用 ``optimize_acde_rolling_three_year.py`` 在固定三年底座上
重建正式配置基准。本脚本只做三件事：

1. 核对 C/E/D 正式发布身份与 A>C>E>D 固定顺序；
2. 核对三年基准逐笔账本确实复现 176 笔、6046.316593512633 倍；
3. 核对优化产物清单中的配置、代码、输入和报告哈希没有漂移。

历史结果属于 STRICT_DISCOVERY。用户已明确要求将该版本用于当前实盘，但这不把
开发窗口伪装成冻结样本外，也不构成未来收益承诺。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.live_certification import (  # noqa: E402
    certification_config_sha256,
    certification_files_sha256,
)
from src.mechanical_compound import MECHANICAL_COMPOUND_STANDARD_ID  # noqa: E402
from src.strict_asof import STRICT_ASOF_STANDARD_ID, STRICT_DISCOVERY  # noqa: E402


RELEASE_ID = "ACDE_CED_V12_6046_20260630"
SCENARIO = "acde_ced_v12_6046_formal"
PRIORITY = ["A", "C", "E", "D"]
WINDOW_START = "20230701"
WINDOW_END = "20260630"
EXPECTED_TRADE_COUNT = 176
EXPECTED_EQUITY_MULTIPLE = 6046.316593512633
EXPECTED_MAX_DRAWDOWN = -0.24337404018667597
EXPECTED_LEG_COUNTS = {"A": 78, "C": 46, "E": 36, "D": 16}
EXPECTED_RELEASES = {
    "C": "C_LEADER_RANK23_LD_LT30_20260630_V12",
    "E": "E_R1_RISK_LEADER11_30_OR_LU120_180_20260630_V12",
    "D": "D_QUALITY_BREAK25_75_TOUCH_LT40_20260630_V12",
}

SOURCE_DIR = (
    ROOT
    / "reports"
    / "acde_rolling_optimization"
    / "20260630_v12_formal_baseline_verification"
)
SUMMARY_PATH = SOURCE_DIR / "optimization_summary.json"
REPLAY_PATH = SOURCE_DIR / "baseline_portfolio_replay.csv"
MANIFEST_PATH = SOURCE_DIR / "artifact_manifest.json"
OUTPUT_DIR = ROOT / "reports" / "current_portfolio_alignment"
CERTIFICATION_PATH = OUTPUT_DIR / "live_certification.json"
AUDIT_PATH = OUTPUT_DIR / "strict_asof_audit.json"
REPORT_PATH = OUTPUT_DIR / "strict_asof_portfolio_report.md"

CODE_FILES = [
    "config/acde_rolling_optimization.json",
    "config/config.json",
    "config/strategy_config.json",
    "config/strategy_e_r1_scenarios.json",
    "config/strategy_d_factor_release.json",
    "scripts/certify_acde_v12_release.py",
    "scripts/optimize_acde_rolling_three_year.py",
    "scripts/run_paper_ab_filtered_daily_ops.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/monitor_strategy_d_intraday.py",
    "scripts/trading_daemon.py",
    "src/acde_rolling_candidates.py",
    "src/acde_rolling_framework.py",
    "src/paper_candidate_generator.py",
    "src/strategy_e.py",
    "src/strategy_d_factor_rules.py",
    "src/live_order_gateway.py",
    "src/live_certification.py",
    "src/mechanical_compound.py",
    "src/strict_asof.py",
]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON根节点不是对象：{path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_close(actual: float, expected: float, label: str) -> None:
    # CSV十进制往返会产生约1e-11的二进制浮点差异；1e-9仍远小于一分钱
    # 对应的账户收益精度，并能阻断任何真实指标漂移。
    if not np.isclose(actual, expected, rtol=0.0, atol=1e-9):
        raise RuntimeError(f"{label}漂移：期望{expected}，实际{actual}")


def _release_identity() -> dict[str, str]:
    strategy = _load_json(ROOT / "config" / "strategy_config.json")
    e_spec = _load_json(ROOT / "config" / "strategy_e_r1_scenarios.json")
    d_release = _load_json(ROOT / "config" / "strategy_d_factor_release.json")
    actual = {
        "C": str(
            strategy["paper_ab_filtered_strategy"]["c_strategy"]["release_id"]
        ),
        "E": str(e_spec.get("release_id", "")),
        "D": str(d_release.get("release_id", "")),
    }
    if actual != EXPECTED_RELEASES:
        raise RuntimeError(f"C/E/D正式发布身份漂移：期望{EXPECTED_RELEASES}，实际{actual}")
    runtime = _load_json(ROOT / "config" / "config.json")
    priority = [
        str(value).upper()
        for value in runtime["portfolio_certification"]["strategy_priority_order"]
    ]
    if priority != PRIORITY:
        raise RuntimeError(f"固定腿序漂移：期望{PRIORITY}，实际{priority}")
    return actual


def _verify_manifest() -> dict[str, Any]:
    manifest = _load_json(MANIFEST_PATH)
    for section in ("code_config_fingerprints", "input_fingerprints", "artifact_fingerprints"):
        entries = manifest.get(section, {})
        if not isinstance(entries, dict) or not entries:
            raise RuntimeError(f"V12产物清单缺少{section}")
        for key, value in entries.items():
            if section == "input_fingerprints":
                if not isinstance(value, dict):
                    raise RuntimeError(f"V12输入指纹格式非法：{key}")
                relative = str(value.get("path", ""))
                expected = str(value.get("sha256", ""))
            else:
                relative = str(key)
                expected = str(value)
            if not relative or not expected:
                raise RuntimeError(f"V12清单指纹缺失：{section}/{key}")
            path = SOURCE_DIR / relative if section == "artifact_fingerprints" else ROOT / relative
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(path)
            actual = _sha256(path)
            if actual != str(expected):
                raise RuntimeError(f"V12清单哈希漂移：{relative}")
    return manifest


def _replay_metrics() -> tuple[dict[str, Any], pd.DataFrame]:
    frame = pd.read_csv(REPLAY_PATH, dtype={"action_date": str}, low_memory=False)
    executed = frame[frame["status"].astype(str).eq("EXECUTED")].copy()
    returns = pd.to_numeric(executed["account_return"], errors="raise")
    equity = (1.0 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    metrics = {
        "trade_count": int(len(executed)),
        "equity_multiple": float(equity.iloc[-1]),
        "max_drawdown": float(drawdown.min()),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "leg_counts": {
            leg: int((executed["strategy_leg"].astype(str) == leg).sum())
            for leg in PRIORITY
        },
    }
    if metrics["trade_count"] != EXPECTED_TRADE_COUNT:
        raise RuntimeError(f"V12成交数漂移：{metrics['trade_count']}")
    _assert_close(metrics["equity_multiple"], EXPECTED_EQUITY_MULTIPLE, "V12复利")
    _assert_close(metrics["max_drawdown"], EXPECTED_MAX_DRAWDOWN, "V12最大回撤")
    if metrics["leg_counts"] != EXPECTED_LEG_COUNTS:
        raise RuntimeError(f"V12分腿样本漂移：{metrics['leg_counts']}")
    return metrics, executed


def certify() -> dict[str, Any]:
    for path in (SUMMARY_PATH, REPLAY_PATH, MANIFEST_PATH):
        if not path.exists():
            raise FileNotFoundError(f"缺少V12正式基准产物：{path}")
    releases = _release_identity()
    manifest = _verify_manifest()
    summary = _load_json(SUMMARY_PATH)
    if summary.get("priority") != PRIORITY:
        raise RuntimeError("V12研究汇总腿序漂移")
    baseline = summary["baseline"]["portfolio"]["main"]
    if int(baseline["trade_count"]) != EXPECTED_TRADE_COUNT:
        raise RuntimeError("V12研究汇总成交数漂移")
    _assert_close(float(baseline["equity_multiple"]), EXPECTED_EQUITY_MULTIPLE, "汇总复利")
    metrics, trades = _replay_metrics()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    audit = {
        "schema_version": 1,
        "release_id": RELEASE_ID,
        "standard_id": STRICT_ASOF_STANDARD_ID,
        "strict_asof_passed": True,
        "research_protocol": STRICT_DISCOVERY,
        "release_eligible": False,
        "user_approved_for_current_live": True,
        "generated_at": generated_at,
        "window": f"{WINDOW_START}~{WINDOW_END}",
        "strategy_priority_order": PRIORITY,
        "formal_strategy_releases": releases,
        "metrics": metrics,
        "source_manifest": str(MANIFEST_PATH.relative_to(ROOT)),
        "source_manifest_sha256": _sha256(MANIFEST_PATH),
        "limitations": [
            "三年窗口参与规则发现，属于STRICT_DISCOVERY，不是冻结样本外。",
            "历史机械复利不代表大资金可成交收益，也不保证未来收益。",
            "2026-07~08冻结前向只有10笔且未产生组合收益差异。",
            "最大单笔亏损为-18.5129%，实盘必须继续执行既有仓位和退出风控。",
        ],
    }
    AUDIT_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    runtime = _load_json(ROOT / "config" / "config.json")
    input_files = [
        str(item["path"])
        for item in manifest["input_fingerprints"].values()
        if isinstance(item, dict) and item.get("path")
    ]
    certificate = {
        "schema_version": 2,
        "status": "PASS",
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "current_executable": True,
        "release_eligible": False,
        "user_approved_for_current_live": True,
        "metric_scope": "STRICT_ASOF_ACTION_DATE_SINGLE_ACCOUNT_V12",
        "generated_at": generated_at,
        "input_start_date": WINDOW_START,
        "input_end_date": WINDOW_END,
        "strategy_priority_order": PRIORITY,
        "executed_trade_count": metrics["trade_count"],
        "equity_multiple": metrics["equity_multiple"],
        "total_compound_return": metrics["equity_multiple"] - 1.0,
        "win_rate": metrics["win_rate"],
        "avg_return": metrics["avg_account_return"],
        "median_return": metrics["median_account_return"],
        "max_drawdown": metrics["max_drawdown"],
        "max_profit": metrics["max_profit"],
        "max_loss": metrics["max_loss"],
        "leg_counts": metrics["leg_counts"],
        "position_pct": float(runtime["portfolio_certification"]["position_pct"]),
        "strict_asof_standard_id": STRICT_ASOF_STANDARD_ID,
        "strict_asof_passed": True,
        "research_protocol": STRICT_DISCOVERY,
        "compound_standard_id": MECHANICAL_COMPOUND_STANDARD_ID,
        "strict_asof_audit_path": str(AUDIT_PATH.relative_to(ROOT)),
        "strict_asof_audit_sha256": _sha256(AUDIT_PATH),
        "config_sha256": certification_config_sha256(runtime),
        "code_files": CODE_FILES,
        "code_sha256": certification_files_sha256(ROOT, CODE_FILES),
        "input_files": input_files,
        "input_sha256": certification_files_sha256(ROOT, input_files),
        "baseline_replay_path": str(REPLAY_PATH.relative_to(ROOT)),
        "baseline_replay_sha256": _sha256(REPLAY_PATH),
        "note": (
            "用户于2026-09-01明确要求落地三年6046.3166倍C/E/D组合。"
            "认证精确复现正式配置基准，但协议仍是STRICT_DISCOVERY；"
            "该标记不等于冻结样本外通过，也不构成未来收益承诺。"
        ),
    }
    CERTIFICATION_PATH.write_text(
        json.dumps(certificate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_PATH.write_text(
        "\n".join(
            [
                "# ACDE C/E/D V12 正式配置认证",
                "",
                f"- 发布：{RELEASE_ID}",
                f"- 窗口：{WINDOW_START}～{WINDOW_END}",
                "- 顺序：A>C>E>D",
                f"- 成交：{metrics['trade_count']}笔",
                f"- 三年机械复利：{metrics['equity_multiple']:.12f}倍",
                f"- 胜率：{metrics['win_rate']:.2%}",
                f"- 最大回撤：{metrics['max_drawdown']:.2%}",
                f"- 最大单笔亏损：{metrics['max_loss']:.2%}",
                f"- 分腿：{metrics['leg_counts']}",
                "",
                "本版本由用户明确要求落地；研究协议仍为STRICT_DISCOVERY，历史结果不保证未来收益。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # 防止空账本或顺序读取错误被静默认证。
    if trades.empty or list(certificate["strategy_priority_order"]) != PRIORITY:
        raise RuntimeError("V12认证输出完整性检查失败")
    return certificate


def main() -> int:
    result = certify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
