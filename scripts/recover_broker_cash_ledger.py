"""从券商原始资金流水恢复独立现金账本，不连接券商或认证策略净值。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile


ZERO = Decimal("0")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = {
    "资金账号", "发生日期", "发生时间", "流水序号", "业务名称",
    "发生金额", "剩余金额", "币种",
}
NO_CASH_CHANGE = {
    "转存管转出", "转存管转入", "转账取预冻结", "转账冻结取消", "利息结算",
}
IPO_SETTLEMENT = {"市值申购中签扣款", "市值申购中签扣款回冲", "新股申购确认缴款"}
CATEGORIES = {
    **dict.fromkeys(NO_CASH_CHANGE, "no_cash_change"),
    **dict.fromkeys(IPO_SETTLEMENT, "ipo_settlement"),
    "银行转存": "external_capital",
    "银行转取": "external_capital",
    "证券买入": "security_settlement",
    "证券卖出": "security_settlement",
    "利息归本": "cash_interest",
}


def _date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("日期必须为 YYYYMMDD")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("日期无效") from exc
    return value


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金额无效") from exc
    if not amount.is_finite() or len(amount.as_tuple().digits) > 25:
        raise ValueError("金额非有限或精度超出范围")
    # 不静默四舍五入；券商原件允许 1.000 这样的尾随零。
    try:
        rounded = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("金额精度超出范围") from exc
    if amount != rounded:
        raise ValueError("金额包含不足一分钱的变化")
    return amount


def _write_private(path: Path, content: str, encoding: str) -> None:
    """临时文件创建时即限制权限，成功后原子替换单个输出。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(
    source: Path, output: Path, *, expected_sha256: str, opening_cash: str,
    query_start: str, query_end: str, encoding: str = "gb18030",
) -> dict:
    """检查完整原件后输出现金事实；期初现金必须来自可核验的记录。"""
    source = Path(source).resolve()
    output = Path(output).resolve()
    if output.is_relative_to(PROJECT_ROOT):
        raise ValueError("恢复输出必须位于项目及同步目录之外")
    names = ("recovered_broker_cash_ledger.json", "recovered_broker_cash_flow.csv")
    if source in {output / name for name in names}:
        raise ValueError("恢复输出不能覆盖券商原件")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("必须提供已独立核对的 SHA-256")
    query_start, query_end = _date(query_start), _date(query_end)
    if query_start > query_end:
        raise ValueError("查询起止日期顺序错误")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("券商原件哈希与预期不一致")
    reader = csv.DictReader(io.StringIO(raw.decode(encoding)), delimiter="\t")
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or not REQUIRED_FIELDS.issubset(headers):
        raise ValueError("原件缺少必需字段或包含重复字段名")
    rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("原件为空或字段数量不一致")
    accounts = {row["资金账号"].strip() for row in rows}
    if len(accounts) != 1 or not next(iter(accounts)):
        raise ValueError("原件账户为空或包含多个账户")
    if {row["币种"].strip() for row in rows} != {"人民币"}:
        raise ValueError("仅支持单一人民币现金账本")

    with localcontext() as context:
        context.prec = 50
        initial = previous = _money(opening_cash)
        totals = defaultdict(lambda: ZERO)
        counts = Counter()
        restored = []
        identities = set()
        last_date = ""
        for index, row in enumerate(rows, 1):
            date = _date(row["发生日期"])
            if not query_start <= date <= query_end or date < last_date:
                raise ValueError("原件日期超出查询范围或顺序错误")
            last_date = date
            identity = tuple(row.values())
            if identity in identities:
                raise ValueError("原件含完全重复记录")
            identities.add(identity)
            kind = row["业务名称"]
            category = CATEGORIES.get(kind)
            if category is None:
                raise ValueError("原件含未审核业务类型")
            amount, balance = _money(row["发生金额"]), _money(row["剩余金额"])
            actual = balance - previous
            expected = ZERO if kind in NO_CASH_CHANGE else amount
            if actual != expected:
                raise ValueError(f"第 {index} 条余额变化与业务发生额不匹配")
            totals[category] += actual
            counts[kind] += 1
            restored.append({
                "source_row": index, "date": date,
                "settlement_time": row["发生时间"], "serial": row["流水序号"],
                "business": kind, "category": category,
                "reported_amount": str(amount), "cash_change": str(actual),
                "cash_before": str(previous), "cash_after": str(balance),
                "currency": row["币种"],
            })
            previous = balance
        if initial + sum(totals.values(), ZERO) != previous:
            raise ValueError("现金账本总体不平衡")

    daily = {row["date"]: row["cash_after"] for row in restored}
    payload = {
        "schema_version": 1, "source": "broker_original_fund_flow",
        "source_sha256": digest, "query_period": [query_start, query_end],
        "records_period": [restored[0]["date"], restored[-1]["date"]],
        "cash_reconciliation_ready": True, "strategy_equity_certified": False,
        "history_coverage_certified": False,
        "original_row_count": len(rows), "matched_balance_changes": len(rows),
        "opening_cash": str(initial), "ending_cash": str(previous),
        "cash_changes_by_category": {k: str(v) for k, v in totals.items()},
        "business_counts": dict(counts), "daily_last_recorded_cash": daily,
        "records": restored,
        "limitations": [
            "余额核对通过不能证明券商查询覆盖全部历史",
            "资金流水不能替代含证券代码、数量、价格和费用的逐笔成交明细",
            "发生时间为清算时间，不能当作盘中成交时间",
            "中签扣款需要证券持仓和上市交易记录核对",
            "现金余额不能替代持仓期间的账户净值；证券清算净额不是策略收益",
        ],
    }
    csv_stream = io.StringIO(newline="")
    writer = csv.DictWriter(csv_stream, fieldnames=list(restored[0]))
    writer.writeheader()
    writer.writerows(restored)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(output / names[0], json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    _write_private(output / names[1], csv_stream.getvalue(), "utf-8-sig")
    return {key: payload[key] for key in (
        "cash_reconciliation_ready", "original_row_count", "matched_balance_changes",
        "ending_cash", "cash_changes_by_category", "strategy_equity_certified",
        "history_coverage_certified",
    )}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--opening-cash", required=True, help="原件第一行发生前的可核验现金余额")
    parser.add_argument("--query-start", required=True)
    parser.add_argument("--query-end", required=True)
    parser.add_argument("--encoding", choices=("gb18030", "utf-8-sig"), default="gb18030")
    args = parser.parse_args()
    try:
        result = restore(
            args.source, args.output, expected_sha256=args.expected_sha256,
            opening_cash=args.opening_cash, query_start=args.query_start,
            query_end=args.query_end, encoding=args.encoding,
        )
    except (ValueError, OSError, UnicodeError, InvalidOperation) as exc:
        parser.exit(1, f"现金恢复失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
