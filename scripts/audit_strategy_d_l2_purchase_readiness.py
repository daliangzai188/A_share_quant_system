#!/usr/bin/env python3
"""生成策略D历史L2权限与采购准备度报告。

本脚本只读取本地探针和严格数据审计结果，不登录供应商、不发询价、不购买
数据。官方产品信息只记录公开页面及其可证明范围；没有公开证明的价格、跨度
或北交所覆盖一律标为待书面确认。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QMT_REPORT = (
    ROOT / "reports/strategy_d_intraday_research/qmt_three_market_probe.json"
)
DEFAULT_STRICT_AUDIT = (
    ROOT / "reports/strategy_d_intraday_research/strict_source_audit.json"
)
DEFAULT_SAMPLE_ACCEPTANCE = (
    ROOT / "reports/strategy_d_intraday_research/l2_vendor_sample_acceptance.json"
)
DEFAULT_OUTPUT = (
    ROOT / "reports/strategy_d_intraday_research/l2_permission_purchase_audit.json"
)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_audit(
    *,
    qmt_report_path: Path = DEFAULT_QMT_REPORT,
    strict_audit_path: Path = DEFAULT_STRICT_AUDIT,
    sample_acceptance_path: Path = DEFAULT_SAMPLE_ACCEPTANCE,
) -> dict[str, Any]:
    qmt = load_json(qmt_report_path)
    strict = load_json(strict_audit_path)
    sample_acceptance = load_json(sample_acceptance_path)
    strict_layer = strict.get("source_layers", {}).get("strict_full_market_l2", {})
    qmt_has_strict_depth = bool(
        qmt.get("target_count") == 3
        and qmt.get("tick_available_count") == 3
        and qmt.get("historical_book_available_count") == 3
    )

    return {
        "schema_version": 2,
        "strategy": "D",
        "research_only": True,
        "formal_rule_modified": False,
        "window": {
            "formal_anchor": "20240630~20260630",
            "first_required_trading_day": "20240701",
            "last_required_trading_day": "20260630",
            "open_day_count": int(strict.get("window_open_day_count", 484)),
            "required_exchanges": ["SSE", "SZSE", "BSE"],
            "expected_exchange_day_file_count": int(
                strict_layer.get("expected_file_count", 1452)
            ),
        },
        "current_permissions": {
            "sufficient_for_strict_d_replay": False,
            "status": "PURCHASE_OR_TRIAL_PERMISSION_REQUIRED",
            "qmt_three_market_read_only_probe": {
                "report": str(qmt_report_path.relative_to(ROOT)),
                "probe_date": "20260629",
                "target_count": int(qmt.get("target_count", 0)),
                "one_minute_available_count": int(
                    qmt.get("one_minute_available_count", 0)
                ),
                "tick_available_count": int(qmt.get("tick_available_count", 0)),
                "historical_book_available_count": int(
                    qmt.get("historical_book_available_count", 0)
                ),
                "strict_depth_available": qmt_has_strict_depth,
                "read_only_xtdata": bool(qmt.get("read_only_xtdata", False)),
                "account_accessed": bool(qmt.get("account_accessed", False)),
                "order_api_accessed": bool(qmt.get("order_api_accessed", False)),
            },
            "local_historical_l2_files": {
                "complete_exchange_day_file_count": int(
                    strict_layer.get("complete_file_count", 0)
                ),
                "expected_exchange_day_file_count": int(
                    strict_layer.get("expected_file_count", 1452)
                ),
            },
            "myquant_local_inventory": {
                "mac_python_sdk_installed": False,
                "windows_python_sdk_installed": False,
                "windows_client_found": False,
                "token_or_entitlement_probed": False,
                "conclusion": "当前没有可供探测的掘金SDK、客户端或已知历史L2授权",
            },
            "vendor_sample_content_gate": {
                "report": str(sample_acceptance_path.relative_to(ROOT)),
                "status": sample_acceptance.get(
                    "status", "BLOCKED_NO_VENDOR_SAMPLE_MANIFEST"
                ),
                "passed": bool(sample_acceptance.get("passed", False)),
                "passed_sample_count": int(
                    sample_acceptance.get("passed_sample_count", 0)
                ),
                "expected_sample_count": int(
                    sample_acceptance.get("expected_sample_count", 9)
                ),
            },
        },
        "official_provider_findings": {
            "sse": {
                "market_scope": "上海市场",
                "status": "OFFICIAL_PRODUCT_CANDIDATE_NEEDS_SAMPLE",
                "public_evidence": [
                    "历史Level-2公开产品表列有集合竞价、快照、逐笔成交及K线",
                    "历史产品公开表未列逐笔委托，不能据此认定可重建FIFO",
                    "实时Level-2非展示自用公开价为每数据中心24万元/年，VDE技术服务6万元/年；这不是历史数据报价",
                ],
                "historical_price_publicly_confirmed": False,
                "live_price_not_applicable_to_historical_purchase": {
                    "non_display_self_use_cny_per_year_per_data_center": 240000,
                    "vde_technical_service_cny_per_year_per_vde": 60000,
                },
                "urls": [
                    "https://www.sseinfo.com/services/assortment/historical/",
                    "https://www.sseinfo.com/services/assortment/level2/",
                    "https://www.sseinfo.com/services/cpfwjg/",
                ],
            },
            "szse": {
                "market_scope": "深圳主板和创业板",
                "status": "OFFICIAL_PRODUCT_CANDIDATE_NEEDS_QUOTE_AND_SAMPLE",
                "public_evidence": [
                    "历史增强行情从2008年起提供并支持本地落盘",
                    "数据类型含逐笔委托、逐笔成交、3秒快照和证券委托队列",
                    "公开页面未给出本项目两年窗口的确定总价",
                ],
                "price_publicly_confirmed": "NOT_FOUND_REQUEST_WRITTEN_QUOTE",
                "contact": {
                    "email": "szsi_marketdata@szse.cn",
                    "phone": "0755-81902456",
                },
                "urls": [
                    "https://www.szsi.cn/cpfw/fwsq/hq/yw-2.htm",
                    "https://www.szsi.cn/cpfw/fwsq/hq/lxfs.htm",
                ],
            },
            "bse": {
                "market_scope": "北交所股票",
                "status": "EXACT_HISTORICAL_L2_PRODUCT_NOT_PUBLICLY_CONFIRMED",
                "public_evidence": [
                    "官方公开授权指南当前可确认的是Level-1许可",
                    "未从官方公开页确认两年股票逐笔委托、逐笔成交和队列历史产品及价格",
                    "必须由中证股转科技或合规供应商书面确认，不能默认沪深产品覆盖北交所",
                ],
                "price_publicly_confirmed": "NOT_FOUND_REQUEST_WRITTEN_CONFIRMATION",
                "contact": {
                    "email": "hangqing@neeq.com.cn",
                    "phone": "400-626-3333",
                },
                "urls": [
                    "https://www.bse.cn/application/guide.html",
                    "https://www.neeq.com.cn/services/invest_service.html",
                ],
            },
            "myquant": {
                "market_scope": "官方历史L2接口表当前明确列出SSE和SZSE，未列BSE",
                "status": "SSE_SZSE_HISTORICAL_API_DOCUMENTED_BROKER_TRIAL_REQUIRED",
                "public_evidence": [
                    "文档存在历史十档、逐笔成交、逐笔委托和委托队列接口",
                    "文档标注历史SSE/SZSE逐笔数据从2016-01-04至今",
                    "L2仅支持券商内网环境，单次逐笔/队列请求只支持一天",
                    "公开接口表未列BSE，不能把沪深权限当成沪深京全覆盖",
                ],
                "price_publicly_confirmed": "NOT_FOUND_REQUEST_WRITTEN_QUOTE",
                "urls": [
                    "https://www.myquant.cn/docs2/tools/L2%E6%95%B0%E6%8D%AE.html",
                ],
            },
        },
        "prepayment_sample_gate": {
            "decision": "DO_NOT_PAY_UNTIL_ALL_REQUIRED_MARKETS_PASS",
            "required_dates": ["20240701", "20250630", "20260629"],
            "required_markets_each_date": ["SSE", "SZSE", "BSE"],
            "required_data": [
                "全市场同步快照，覆盖09:30前至至少14:55",
                "逐笔委托新增与撤单",
                "逐笔成交，保留买卖委托编号或可验证关联关系",
                "交易所/频道原始sequence，能证明缺包检测和日内顺序",
                "最优价委托总笔数、前50笔队列量及买一总量",
                "未复权原始价格、数量单位为股、时间至少精确到秒",
            ],
            "automatic_acceptance": [
                "真实CSV内容可映射到仓库样本契约，不只采信清单布尔声明",
                "按(channel_no, sequence)检查重复和时间倒序",
                "逐笔撤单必须引用已知新增委托，成交必须关联买卖委托编号",
                "同步scan_id股票宇宙一致，覆盖09:30和14:55并含买一前50队列",
            ],
            "validator_command": "python3 scripts/validate_strategy_d_l2_vendor_sample.py",
            "manifest_template": "config/strategy_d_l2_vendor_sample_manifest.example.json",
        },
        "contract_acceptance": [
            "覆盖2024-07-01至2026-06-30全部484个交易日，不按现有候选裁剪",
            "覆盖沪市主板、科创板、深市主板、创业板和北交所全部股票，含窗口内退市证券",
            "允许内部策略研究、严格回测和本地长期存储",
            "提供全量批量下载或日文件，不接受只能逐票限频查询且无法在合理时间取完",
            "书面列明历史留存起止日、市场、字段、单位、复权口径、缺失日和补发机制",
            "书面列明含税总价、授权主体、使用期限、交付方式及后续增量费用",
        ],
        "purchase_decision": {
            "permission_missing": True,
            "buy_now": False,
            "reason": "单买上海或未经验收的接口仍不能通过沪深京全市场严格门禁",
            "recommended_sequence": [
                "先申请掘金兼容券商环境试用，验证SSE/SZSE历史委托、成交和队列",
                "同时单独书面确认BSE历史逐笔来源；未确认前不得宣称三市场齐备",
                "若券商试用不满足，再分别向上证信息、深证信息和中证股转科技询价",
                "三市场样本全部通过自动验收后再比较总价并购买",
            ],
        },
        "certification_impact": {
            "current_d_reproduction_allowed": False,
            "d_standalone_compound_comparison_allowed": False,
            "acde_one_leg_replacement_allowed": False,
            "formal_d_change_allowed": False,
        },
        "next_authorized_action": "申请掘金兼容券商SSE/SZSE试用样本并单独确认BSE；提交外部申请前需用户确认，当前不付款、不改策略",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计D历史L2权限与采购准备度")
    parser.add_argument("--qmt-report", type=Path, default=DEFAULT_QMT_REPORT)
    parser.add_argument("--strict-audit", type=Path, default=DEFAULT_STRICT_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit = build_audit(
        qmt_report_path=args.qmt_report,
        strict_audit_path=args.strict_audit,
        sample_acceptance_path=DEFAULT_SAMPLE_ACCEPTANCE,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(audit["purchase_decision"], ensure_ascii=False, indent=2))
    print(f"报告：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
