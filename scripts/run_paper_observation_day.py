"""
运行单日模拟盘观察闭环。

文件作用：
1. 一键运行每日操作台 scripts/run_paper_daily_ops.py。
2. 一键刷新累计观察汇总 scripts/summarize_paper_daily_ops.py。
3. 生成本次观察日运行报告，明确当天状态和累计观察是否达标。

本脚本只调用本地模拟盘脚本，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行单日模拟盘观察闭环。")
    parser.add_argument("--signal-date", default=None, help="信号日期，格式 YYYYMMDD。不传则使用最新候选日期。")
    parser.add_argument("--top-n", type=int, default=None, help="候选输出数量，不传则读取配置。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument(
        "--runtime-config",
        default="config/config.json",
        help="运行时通用配置文件路径。",
    )
    parser.add_argument(
        "--daily-ops-output-prefix",
        default="reports/paper_trade/daily_ops/a_clean_exclude_star_prev0_3_bj",
        help="每日操作台输出文件前缀。",
    )
    parser.add_argument(
        "--summary-output-prefix",
        default="reports/paper_trade/daily_ops_summary/a_clean_exclude_star_prev0_3_bj",
        help="累计观察汇总输出文件前缀。",
    )
    parser.add_argument(
        "--min-observation-days",
        type=int,
        default=20,
        help="模拟盘最少观察交易日数。",
    )
    parser.add_argument(
        "--run-output-prefix",
        default="reports/paper_trade/observation_runs/a_clean_exclude_star_prev0_3_bj",
        help="本次观察日运行报告输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def build_daily_ops_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-B",
        "scripts/run_paper_daily_ops.py",
        "--strategy-config",
        args.strategy_config,
        "--runtime-config",
        args.runtime_config,
        "--output-prefix",
        args.daily_ops_output_prefix,
    ]
    if args.signal_date:
        command.extend(["--signal-date", args.signal_date])
    if args.top_n is not None:
        command.extend(["--top-n", str(args.top_n)])
    return command


def build_summary_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-B",
        "scripts/summarize_paper_daily_ops.py",
        "--min-observation-days",
        str(args.min_observation_days),
        "--output-prefix",
        args.summary_output_prefix,
    ]


def latest_checklist_path(daily_ops_output_prefix: str | Path) -> Path | None:
    prefix_path = resolve_path(daily_ops_output_prefix)
    directory = prefix_path.parent
    prefix = prefix_path.name
    candidates = sorted(directory.glob(f"{prefix}_*_checklist.csv"), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def summary_path(summary_output_prefix: str | Path) -> Path:
    prefix_path = resolve_path(summary_output_prefix)
    return prefix_path.with_name(prefix_path.name + "_summary.csv")


def extract_daily_result(checklist_path: Path | None) -> dict[str, Any]:
    if checklist_path is None:
        return {
            "signal_date": "",
            "operation_status": "NO_CHECKLIST",
            "next_action": "未找到每日操作台 checklist。",
            "top_ts_code": "",
            "top_name": "",
            "manual_review_required": False,
            "review_decision_status": "NOT_REQUIRED",
            "paper_observation_allowed": False,
            "live_order_enabled": False,
        }
    checklist = read_csv_if_exists(checklist_path)
    if checklist.empty:
        return {
            "signal_date": "",
            "operation_status": "EMPTY_CHECKLIST",
            "next_action": "每日操作台 checklist 为空。",
            "top_ts_code": "",
            "top_name": "",
            "manual_review_required": False,
            "review_decision_status": "NOT_REQUIRED",
            "paper_observation_allowed": False,
            "live_order_enabled": False,
        }
    row = checklist.iloc[0]
    return {
        "signal_date": normalize_date(row.get("signal_date", "")),
        "operation_status": row.get("operation_status", ""),
        "next_action": row.get("next_action", ""),
        "top_ts_code": row.get("top_ts_code", ""),
        "top_name": row.get("top_name", ""),
        "manual_review_required": str(row.get("manual_review_required", "")).lower() in {"true", "1"},
        "review_decision_status": row.get("review_decision_status", "NOT_REQUIRED"),
        "paper_observation_allowed": str(row.get("paper_observation_allowed", "")).lower() in {"true", "1"},
        "live_order_enabled": False,
        "checklist_path": str(checklist_path),
    }


def extract_summary_result(path: Path) -> dict[str, Any]:
    summary = read_csv_if_exists(path)
    if summary.empty:
        return {
            "observation_day_count": 0,
            "min_observation_days": 0,
            "observation_requirement_met": False,
            "verification_status": "NO_SUMMARY",
            "equity_multiple": 0.0,
            "max_drawdown": 0.0,
            "summary_path": str(path),
        }
    row = summary.iloc[0]
    return {
        "observation_day_count": int(pd.to_numeric(row.get("observation_day_count", 0), errors="coerce") or 0),
        "min_observation_days": int(pd.to_numeric(row.get("min_observation_days", 0), errors="coerce") or 0),
        "observation_requirement_met": str(row.get("observation_requirement_met", "")).lower() in {"true", "1"},
        "verification_status": row.get("verification_status", ""),
        "equity_multiple": float(pd.to_numeric(row.get("equity_multiple", 0.0), errors="coerce") or 0.0),
        "max_drawdown": float(pd.to_numeric(row.get("max_drawdown", 0.0), errors="coerce") or 0.0),
        "summary_path": str(path),
    }


def resolve_run_status(daily_status: str, summary_status: str, observation_met: bool) -> str:
    if daily_status in {"NO_CHECKLIST", "EMPTY_CHECKLIST"}:
        return "FAILED_DAILY_OBSERVATION"
    if summary_status == "NO_SUMMARY":
        return "FAILED_SUMMARY"
    if not observation_met:
        return "CONTINUE_PAPER_OBSERVATION"
    return "OBSERVATION_DAYS_MET_REVIEW_REQUIRED"


def build_run_report(
    daily_process: subprocess.CompletedProcess[str],
    summary_process: subprocess.CompletedProcess[str],
    daily_command: list[str],
    summary_command: list[str],
    daily_result: dict[str, Any],
    summary_result: dict[str, Any],
) -> pd.DataFrame:
    run_status = resolve_run_status(
        str(daily_result.get("operation_status", "")),
        str(summary_result.get("verification_status", "")),
        bool(summary_result.get("observation_requirement_met", False)),
    )
    return pd.DataFrame(
        [
            {
                "signal_date": daily_result.get("signal_date", ""),
                "run_status": run_status,
                "operation_status": daily_result.get("operation_status", ""),
                "next_action": daily_result.get("next_action", ""),
                "top_ts_code": daily_result.get("top_ts_code", ""),
                "top_name": daily_result.get("top_name", ""),
                "manual_review_required": daily_result.get("manual_review_required", False),
                "review_decision_status": daily_result.get("review_decision_status", "NOT_REQUIRED"),
                "paper_observation_allowed": daily_result.get("paper_observation_allowed", False),
                "observation_day_count": summary_result.get("observation_day_count", 0),
                "min_observation_days": summary_result.get("min_observation_days", 0),
                "observation_requirement_met": summary_result.get("observation_requirement_met", False),
                "verification_status": summary_result.get("verification_status", ""),
                "equity_multiple": summary_result.get("equity_multiple", 0.0),
                "max_drawdown": summary_result.get("max_drawdown", 0.0),
                "daily_ops_returncode": daily_process.returncode,
                "summary_returncode": summary_process.returncode,
                "daily_ops_command": command_text(daily_command),
                "summary_command": command_text(summary_command),
                "checklist_path": daily_result.get("checklist_path", ""),
                "summary_path": summary_result.get("summary_path", ""),
                "live_order_enabled": False,
                "safety_note": "只做模拟盘观察；达到观察天数也不代表可以实盘，还需分钟K、盘口五档、滑点、人工复核和模拟盘稳定性验证。",
            }
        ]
    )


def output_paths(run_output_prefix: str | Path, signal_date: str) -> dict[str, Path]:
    prefix = resolve_path(run_output_prefix)
    date = signal_date or "unknown"
    return {
        "run_report": prefix.with_name(prefix.name + f"_{date}_run_report.csv"),
        "markdown": prefix.with_name(prefix.name + f"_{date}.md"),
    }


def write_markdown(path: Path, run_report: pd.DataFrame, daily_process: subprocess.CompletedProcess[str], summary_process: subprocess.CompletedProcess[str]) -> None:
    stdout_rows = pd.DataFrame(
        [
            {
                "step": "daily_ops",
                "returncode": daily_process.returncode,
                "stdout_tail": "\n".join(daily_process.stdout.strip().splitlines()[-12:]),
                "stderr_tail": "\n".join(daily_process.stderr.strip().splitlines()[-8:]),
            },
            {
                "step": "summary",
                "returncode": summary_process.returncode,
                "stdout_tail": "\n".join(summary_process.stdout.strip().splitlines()[-12:]),
                "stderr_tail": "\n".join(summary_process.stderr.strip().splitlines()[-8:]),
            },
        ]
    )
    content = f"""# 单日模拟盘观察闭环报告

本报告只记录本地模拟盘观察流程，不接实盘，不调用 QMT，不下真实订单。

## 运行结果

{run_report.to_markdown(index=False)}

## 命令输出摘要

{stdout_rows.to_markdown(index=False)}

## 判断限制

- `CONTINUE_PAPER_OBSERVATION` 表示继续累计观察样本，不能实盘。
- `OBSERVATION_DAYS_MET_REVIEW_REQUIRED` 只表示观察天数达标，仍需重新评估回撤、人工复核、分钟 K、盘口和滑点。
- `live_order_enabled` 必须始终为 `False`。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    daily_command = build_daily_ops_command(args)
    summary_command = build_summary_command(args)

    daily_process = run_command(daily_command)
    checklist_path = latest_checklist_path(args.daily_ops_output_prefix)
    daily_result = extract_daily_result(checklist_path)

    summary_process = run_command(summary_command)
    summary_result = extract_summary_result(summary_path(args.summary_output_prefix))

    run_report = build_run_report(
        daily_process=daily_process,
        summary_process=summary_process,
        daily_command=daily_command,
        summary_command=summary_command,
        daily_result=daily_result,
        summary_result=summary_result,
    )
    signal_date = str(run_report.iloc[0].get("signal_date", "") or args.signal_date or "unknown")
    paths = output_paths(args.run_output_prefix, signal_date)
    paths["run_report"].parent.mkdir(parents=True, exist_ok=True)
    run_report.to_csv(paths["run_report"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], run_report, daily_process, summary_process)

    print("单日模拟盘观察闭环完成：")
    print(f"- run_report: {paths['run_report']}")
    print(f"- markdown: {paths['markdown']}")
    print(run_report.to_string(index=False))


if __name__ == "__main__":
    main()
