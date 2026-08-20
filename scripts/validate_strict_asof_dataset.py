"""验证策略研究数据是否满足 A_SYSTEM_STRICT_ASOF_V1。

该脚本只读输入数据并写审计 JSON，不生成收益、不修改策略配置或实盘状态。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strict_asof import (  # noqa: E402
    PointInTimeContract,
    validate_strict_research_frame,
    write_audit_json,
)
from src.utils.config import load_json_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计严格as-of策略研究数据。")
    parser.add_argument(
        "--input",
        default="data/processed/limit_up_fill_scored_asof.csv",
        help="待审计CSV路径。",
    )
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--config-section",
        default="analysis",
        help="读取asof_mode/research_protocol的配置段。",
    )
    parser.add_argument("--dataset-name", default="manual_strict_asof_audit")
    parser.add_argument(
        "--selection-columns",
        default="allow_buy_reliable,is_fill_score_reliable,fill_probability",
        help="实际用于过滤/排序的字段，逗号分隔。",
    )
    parser.add_argument(
        "--output",
        default="reports/strict_asof/manual_dataset_audit.json",
        help="审计JSON输出路径。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    section = config.get(args.config_section, {})
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path
    frame = pd.read_csv(input_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    audit = validate_strict_research_frame(
        frame,
        contract=PointInTimeContract(dataset_name=args.dataset_name),
        selection_columns=[
            value.strip() for value in args.selection_columns.split(",") if value.strip()
        ],
        section_config=section,
        context="validate_strict_asof_dataset.py",
        project_root=PROJECT_ROOT,
    )
    output = write_audit_json(args.output, audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print(f"严格as-of审计通过：{output}")


if __name__ == "__main__":
    main()
