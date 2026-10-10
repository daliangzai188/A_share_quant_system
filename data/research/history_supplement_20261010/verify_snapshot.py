"""只读校验冻结研究附件，不联网、不导入补采脚本、不打开账户账本。"""
import argparse
import ast
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def verify(snapshot, check_external=False):
    snapshot = snapshot.resolve()
    manifest = read_json(snapshot / "snapshot_manifest.json")
    require(manifest["artifact_type"] == "FROZEN_RESEARCH_EVIDENCE_NOT_PRODUCTION_INPUT",
            "附件用途必须保持为研究证据")
    require(manifest["formal_baseline_certified"] is False
            and manifest["full_review_status"] == "DATA_INCOMPLETE_NOT_READY",
            "附件不能被改写为正式基准认证通过")
    frozen = {}
    for record in manifest["files"]:
        relative = Path(record["file"])
        path = (snapshot / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(snapshot),
                "冻结文件路径超出附件目录")
        require(record["file"] not in frozen, "清单包含重复文件")
        require(path.is_file() and path.stat().st_size == record["bytes"],
                "冻结文件缺失或大小改变：" + record["file"])
        require(sha(path) == record["sha256"], "冻结文件哈希不符：" + record["file"])
        if "rows" in record:
            require(len(read_csv(path)) == record["rows"],
                    "CSV行数与清单不符：" + record["file"])
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        frozen[record["file"]] = record

    continued = snapshot / "continued"
    evidence = continued / "evidence"
    acceptance = read_json(evidence / "继续补充验收.json")
    ratios = read_json(evidence / "remaining_ratio_supplement.json")
    require(acceptance["formal_baseline_certified"] is False
            and acceptance["full_review_status"] == "DATA_INCOMPLETE_NOT_READY"
            and ratios["formal_baseline_certified"] is False,
            "原验收记录不能放宽正式认证")
    for key in ("historical_equity_ledger_restored", "historical_seal_measurement_certified",
                "exact_final_trade_window_certified", "paid_data_purchased",
                "orders_or_cancellations_sent", "broker_history_query_executed"):
        require(acceptance[key] is False, "原验收状态被改变：" + key)
    require(acceptance["new_ratio_values_written"] == ratios["new_derived_values_written"] == 0,
            "本轮没有新增可用量比数值")
    for record in acceptance["script_files"]:
        require(sha(continued / record["file"]) == record["sha256"],
                "补采脚本与原验收版本不符")

    target_path = snapshot / "evidence/target_stock_days.csv"
    require(sha(target_path) == ratios["target_manifest_sha256"], "目标清单不是原核验输入")
    targets = read_csv(target_path)
    keys = {(row["ts_code"], row["trade_date"]) for row in targets}
    require(len(keys) == len(targets) == ratios["target_stock_days"] == 20552,
            "目标股票日数量或主键不符")

    gap_path = evidence / "remaining_ratio_gaps.json"
    control_path = evidence / "remaining_ratio_controls.csv"
    require(sha(gap_path) == ratios["gap_details_sha256"]
            and sha(control_path) == ratios["control_sha256"], "量比审计与原回执不符")
    ipo_path = evidence / "ipo_ratio_not_applicable.csv"
    require(sha(ipo_path) == acceptance["ipo_reason_file_sha256"], "IPO依据与原验收不符")
    gaps = read_json(gap_path)
    ipo = read_csv(ipo_path)
    gap_keys = {(row["ts_code"], row["trade_date"]): row for row in gaps}
    ipo_keys = {(row["ts_code"], row["trade_date"]) for row in ipo}
    require(len(gap_keys) == len(gaps) == len(ipo_keys) == len(ipo) == 837
            and ipo_keys == set(gap_keys) and ipo_keys <= keys, "IPO缺口主键或数量不符")
    require(len({row["ts_code"] for row in ipo}) == 186, "IPO股票数量不符")
    for row in ipo:
        gap = gap_keys[(row["ts_code"], row["trade_date"])]
        prior = row["prior_traded_dates"].split("|") if row["prior_traded_dates"] else []
        listed, date = row["official_list_date"], row["trade_date"]
        require(row["ratio_value"] == ""
                and row["classification"] == "NOT_APPLICABLE_INSUFFICIENT_PRIOR_5_TRADED_DAYS",
                "IPO量比必须保留不适用空值")
        require("20180101" <= listed <= date and len(prior) == len(set(prior))
                and len(prior) == int(row["prior_traded_days"]) < 5
                and all(listed <= day < date for day in prior), "IPO前史含未来或上市前数据")
        require(gap["status"] == "INSUFFICIENT_PRIOR_5_TRADED_DAYS"
                and gap["unrounded_derived_ratio"] is None
                and gap["prior_volume_dates"] == prior
                and gap["prior_observed_days"] == len(prior)
                and gap["source_daily_basic_sha256"] == row["source_daily_basic_sha256"],
                "IPO依据与量比缺口记录不一致")

    controls = read_csv(control_path)
    control_keys = {(row["ts_code"], row["trade_date"]) for row in controls}
    require(len(control_keys) == len(controls) == 19673
            and control_keys <= keys and not control_keys.intersection(ipo_keys),
            "官方对照存在重复、非目标证券或IPO空值")
    for row in controls:
        computed, official, recorded = (Decimal(row[name]) for name in
                                        ("computed_ratio", "official_ratio", "difference"))
        require(all(value.is_finite() for value in (computed, official, recorded))
                and row["control_source"] in ("official_original", "tushare_daily_basic_original")
                and row["match"] == "True"
                and abs(computed - official) <= Decimal("0.0050001")
                and abs(recorded - abs(computed - official)) <= Decimal("0.000000000001"),
                "官方量比舍入对照失败或混入衍生值")

    checks = read_json(evidence / "existing_permission_checks.json")
    captured_rows = 0
    for record in checks["queries"] + [ratios["calendar_source"]]:
        path = continued / record["file"]
        require(sha(path) == record["sha256"]
                and len(read_csv(path)) == record["rows"], "本轮原件回执不符")
        captured_rows += record["rows"]
    require(captured_rows == acceptance["new_official_source_rows_captured"] == 940,
            "新增核验原件行数不符")
    calendar_rows = read_csv(continued / ratios["calendar_source"]["file"])
    first, last = dt.date(2018, 1, 1), dt.date(2019, 11, 27)
    expected_dates = {(first + dt.timedelta(days=n)).strftime("%Y%m%d")
                      for n in range((last - first).days + 1)}
    require(len(calendar_rows) == len(expected_dates) == 696
            and {row["cal_date"] for row in calendar_rows} == expected_dates
            and all(row["exchange"] == "SSE" and row["is_open"] in ("0", "1")
                    for row in calendar_rows), "官方交易日历覆盖不完整")

    discrepancies = read_csv(evidence / "remaining_price_discrepancies.csv")
    require(len({(row["ts_code"], row["trade_date"]) for row in discrepancies})
            == len(discrepancies) == 4865, "价格差异主键或数量不符")
    low_count = sum(abs(Decimal(row["low_traded_difference"])) > Decimal("0.005000001")
                    for row in discrepancies)
    require(low_count == acceptance["remaining_low_difference_stock_days"] == 4765
            and all(row["certification_status"] == "NEEDS_ORIGINAL_TICK_RECONCILIATION"
                    for row in discrepancies), "分钟价格差异不能被标记为已修复")
    earlier = read_json(snapshot / "evidence/early_basic_supplement_acceptance.json")
    candidate = snapshot / earlier["candidate_file"]
    require(sha(candidate) == earlier["candidate_sha256"]
            and earlier["derived_values_written"] == 516
            and earlier["status"] == "DERIVED_FIELD_VALIDATED_NOT_FORMAL_RELEASE"
            and earlier["full_2018_limit_pool_certified"] is False,
            "此前单日衍生候选只能作为研究附件保留")

    external_count = 0
    dependencies = ratios["source_dependencies"]
    require(len(dependencies) == acceptance["source_dependency_files_verified"] == 867,
            "完整历史依赖记录数量不符")
    if check_external:
        for record in dependencies:
            path = Path(record["file"])
            require(path.is_absolute() and path.is_file()
                    and sha(path) == record["sha256"], "外部历史原件缺失或哈希改变：" + str(path))
            external_count += 1
        stock_path = Path("/Users/eass/Desktop/A_System/data/raw/stock_basic/stock_basic_all.csv")
        require(sha(stock_path) == acceptance["stock_listing_source_sha256"], "原股票上市资料已改变")
    return {
        "status": "PASS_FROZEN_RESEARCH_SNAPSHOT_CHECKS",
        "frozen_files_verified": len(frozen),
        "new_official_rows_captured": captured_rows,
        "target_stock_days": len(targets),
        "ipo_not_applicable_stock_days": len(ipo),
        "official_ratio_controls_passed": len(controls),
        "prior_isolated_20181224_derived_values": earlier["derived_values_written"],
        "external_source_files_verified_this_run": external_count,
        "external_source_checks_requested": check_external,
        "remaining_low_price_differences": low_count,
        "formal_baseline_certified": False,
        "full_review_status": "DATA_INCOMPLETE_NOT_READY",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--check-external-sources", action="store_true",
                        help="另行只读核对原本机867个历史依赖；干净克隆未包含这些外部原件")
    args = parser.parse_args()
    try:
        result = verify(args.snapshot, args.check_external_sources)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        print(json.dumps({"status": "FAIL_SNAPSHOT_CHECKS", "error": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
