"""仅策略临时复核：复用已验收行情，不读取真实成交或连接券商。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent
DEFAULT_INPUT = OUT.parent / "2026-10-10_继续补齐复核数据"


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(name: str, value: dict) -> None:
    path = OUT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def factor_evidence(code: Path, dataset: Path, config: dict, dates: list[str], as_of: str) -> dict:
    """沿用正式因子健康口径，并核验返回样本没有使用缺失复权因子的兜底值。"""
    from src.factor_health import (attach_forward_returns, condition_mask, load_settings,
                                   monthly_edge, parse_condition_profiles, rolling_health)

    settings = load_settings(config)
    assert settings.enabled, "正式因子健康监控未启用"
    pool = pd.read_csv(dataset / "strict_feature_pool.csv",
                       dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    months_back = int(config["factor_health"]["months_back"])
    start_month = (dt.datetime.strptime(as_of[:6] + "01", "%Y%m%d")
                   - dt.timedelta(days=31 * months_back)).strftime("%Y%m")
    pool = pool[pool.trade_date.str[:6].ge(start_month) & pool.trade_date.le(as_of)].copy()
    frame = attach_forward_returns(pool, calendar_dates=dates, daily_dir=code / "data/raw/daily",
                                   adj_dir=code / "data/raw/adj_factor")
    assert len(frame) and frame.exit_date.max() <= as_of
    assert np.isfinite(frame.ret).all()
    assert frame.entry_open.gt(0).all() and frame.exit_close.gt(0).all()
    assert not frame.duplicated(["trade_date", "ts_code"]).any()

    # attach_forward_returns 内部允许缺复权因子时回退1；本次证据必须逐个核对原件。
    requests = pd.concat([
        frame[["ts_code", "entry_date", "entry_adj"]].rename(
            columns={"entry_date": "trade_date", "entry_adj": "used_adj"}),
        frame[["ts_code", "exit_date", "exit_adj"]].rename(
            columns={"exit_date": "trade_date", "exit_adj": "used_adj"}),
    ], ignore_index=True).drop_duplicates()
    for date, wanted in requests.groupby("trade_date", sort=True):
        path = code / "data/raw/adj_factor" / f"{date}.csv"
        assert path.exists(), f"缺少复权原件：{date}"
        actual = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str},
                             usecols=["trade_date", "ts_code", "adj_factor"])
        matched = wanted.merge(actual, on=["trade_date", "ts_code"], how="left", validate="many_to_one")
        assert matched.adj_factor.notna().all() and matched.adj_factor.gt(0).all(), date
        assert np.allclose(matched.used_adj, matched.adj_factor, rtol=1e-12, atol=1e-12), date

    profiles = parse_condition_profiles(read_json(code / "config/strategy_config.json"))
    health = {}
    for leg, branches in profiles.items():
        assert branches, f"{leg}条件分支为空"
        mask, used = condition_mask(frame, branches)
        assert used == len(branches), f"{leg}有条件分支未参与计算"
        monthly = monthly_edge(frame, mask)
        item = rolling_health(monthly, window=settings.window, min_periods=settings.min_periods,
                              min_samples=settings.min_samples, line=settings.lines.get(leg),
                              consecutive_months=settings.consecutive_months,
                              min_month_samples=settings.min_month_samples)
        assert item.get("value") is not None, f"{leg}不足以计算因子健康"
        item.update(branches_used=used, branches_total=len(branches))
        health[leg] = item
        monthly.to_csv(OUT / f"factor_monthly_{leg}.csv", index_label="signal_month", encoding="utf-8-sig")

    positions = {date: i for i, date in enumerate(dates)}
    matured = pool.trade_date.map(lambda date: positions[date] + 2 < len(dates)
                                  and dates[positions[date] + 2] <= as_of)
    retained = pd.MultiIndex.from_frame(frame[["trade_date", "ts_code"]])
    all_keys = pd.MultiIndex.from_frame(pool[["trade_date", "ts_code"]])
    excluded = pool.loc[matured & ~all_keys.isin(retained), ["trade_date", "ts_code", "name"]]
    excluded.to_csv(OUT / "factor_matured_unpriced_exclusions.csv", index=False, encoding="utf-8-sig")
    context = excluded.merge(pool, on=["trade_date", "ts_code"], how="left",
                             validate="one_to_one", suffixes=("_excluded", ""))
    for leg, branches in profiles.items():
        context["matches_" + leg] = condition_mask(context, branches)[0]
    stock_basic = pd.read_csv(code / "data/raw/stock_basic/stock_basic_all.csv", dtype=str)
    context = context.merge(stock_basic[["ts_code", "list_status", "delist_date"]],
                             on="ts_code", how="left", validate="many_to_one")
    context_columns = ["trade_date", "ts_code", "name", "matches_A", "matches_C", "list_status",
                       "delist_date", "strategy_compatible", "is_st", "allow_buy_reliable", "is_fill_score_reliable"]
    context[context_columns].to_csv(OUT / "factor_exclusion_context.csv", index=False, encoding="utf-8-sig")
    return {
        "health": health, "pool_rows_in_diagnostic_window": len(pool),
        "return_observed_rows": len(frame), "not_yet_matured_rows": int((~matured).sum()),
        "matured_rows_without_both_forward_quotes_excluded": len(excluded),
        "unpriced_excluded_matches_A": int(context.matches_A.sum()),
        "unpriced_excluded_matches_C": int(context.matches_C.sum()),
        "observed_signal_start": str(frame.trade_date.min()),
        "observed_signal_end": str(frame.trade_date.max()),
        "observed_exit_end": str(frame.exit_date.max()),
        "verified_actual_adjustment_stock_days": len(requests),
        "adjustment_fallbacks_in_retained_samples": 0,
        "metric": "月度匹配均值减不匹配均值，再对最近12个月差额等权平均；T+1开盘至T+2收盘复权毛收益",
        "tradeable_net_return_metric": False,
    }


def render_report(result: dict) -> str:
    def pct(value):
        return "不适用" if value is None else f"{100 * value:+.2f}%"

    rows = [
        "方案甲临时复核——仅策略，2026-10-10生成，截至2026-10-09收盘",
        "",
        "结论：②需观察，维持方案甲规则，继续按MA60、滞后6个交易日停手。",
        "本次已完成近期策略复核。旧真实成交和私人账户净值不是本次输入，也不是判断策略的前提。",
        "用户假设没有历史持仓、没有真实成交；研究交易仍须符合费用、滑点、成交限制、T+1和现金占用。",
        "所有下述收益都是模型或条件诊断数字，不能解释为用户实际资金的盈亏。",
        "",
        f"冻结版本：{result['base_commit']}；A>C>E>D；仓位82.5%；不选月度冠军，不修改参数。",
        "行情已验收到2026-10-09；研究池77,195行，来源和内容哈希核验通过。",
        "生产同步目录零写入；没有券商调用、通知、委托或Git提交。",
        "",
        "策略失败线核对：",
        "项目 | 本次结果 | 冻结失败线 | 判定",
    ]
    for item in result["rolling_returns"]:
        rows.append(f"影子账滚动{item['trading_days']}交易日（{item['base_date']}→{item['end_date']}） | "
                    f"{pct(item['return'])} | {pct(item['failure_line'])} | "
                    f"{'触发' if item['triggered'] else '未触发'}")
    stall = result["regime_stall"]
    rows.append(f"正常行情连续非正收益月 | {stall['streak']}/{stall['need_months']}个月 | "
                "平均涨停≥40、连续3个已完成自然月 | " + ("触发" if stall["triggered"] else "未触发"))
    stop = result["current_stop_episode"]
    rows.append(f"本段停手期间影子收益（{stop['start_date']}起，{stop['trading_days']}交易日） | "
                f"{pct(stop['shadow_return'])} | 踏空≥+26.90% | "
                + ("需记录" if result["stop_missed_gain_triggered"] else "未触发"))
    rows += ["", "正常行情核对（10月未结束，不计入连续月数）：",
             "月份 | 影子收益 | 日均涨停"]
    for item in result["completed_months"]:
        rows.append(f"{item['ym']} | {pct(item['shadow_return'])} | {item['limit_up_mean']:.2f}")
    rows += ["", "A/C因子健康："]
    for leg, item in result["factor_diagnostics"]["health"].items():
        rows.append(f"{leg}：截至{item['as_of']}，滚动优势{pct(item['value'])}，冻结线{pct(item['line'])}，"
                    f"滚动匹配样本{item['samples']}，连续跌破{item['months_below']}/{item['need_months']}个月，"
                    f"{'报警' if item['triggered'] else '未报警'}；分支{item['branches_used']}/{item['branches_total']}全部参与。")
    diag = result["factor_diagnostics"]
    rows += [
        "这里的优势是条件集T+1开盘→T+2收盘复权毛收益差，不是扣费后可交易收益，也不替代C的正式T+3退出。",
        f"有效收益样本{diag['return_observed_rows']}；尚未到T+2样本{diag['not_yet_matured_rows']}；"
        f"已成熟但缺双边后续报价、未强填收益的样本{diag['matured_rows_without_both_forward_quotes_excluded']}。",
        f"原件复权核对{diag['verified_actual_adjustment_stock_days']}股票日；有效样本复权兜底0个。",
        "82条成熟无双边报价样本已另查停牌/退市：164个开平仓股票日中，129个由官方全天停牌记录解释、4个在退市之后、29个已有单边报价。",
        "另外2个股票日属广道旧代码记录，按北交所公告的5日停牌期与交易日历推断为停牌；明确标为公告推断，未冒称接口直接返回。",
        "参考：https://tushare.pro/document/2?doc_id=214 （停复牌）；https://tushare.pro/document/2?doc_id=27 （停牌期无日线报价）。",
        "北交所公告：https://www.bse.cn/disclosure/2025/2025-07-23/1493e8a1141c41758be3d7fc313a752f.pdf 。",
        "A的报警说明需重点观察并检验条件优势，不等于已证明删A或改阈值更好。已有年度流程和换规则四条硬门槛仍适用。",
        "",
        "甲上线后的假设空仓区间：",
        f"2026-09-21→2026-10-09共{result['formal_forward']['trading_days']}交易日，"
        f"{result['formal_forward']['stop_days']}日全部禁止A/C/E/D新开仓。",
        "以无历史持仓为假设，期间应无新买入，组合交易收益0、回撤0；这是冻结门禁的直接结果。",
        "作为对照，同期不停手的A/C/E影子账3笔，胜率33.33%，扣费滑点后净收益−9.01%。",
        f"这3笔的历史换手率模型成交概率范围{pct(result['forward_fill_probability']['minimum'])}~"
        f"{pct(result['forward_fill_probability']['maximum'])}，均标记可靠且允许买入；这是历史代理评分，不是实盘排队成交保证。",
        "只有9个前向交易日、3笔影子交易，样本很少，不能据此认定策略长期有效或永久失效。",
        f"下一行动日{result['next_action']['action_date']}："
        + ("允许开新仓。" if result['next_action']['allowed'] else "继续停手。"),
        "",
        "近期模型统计（按action_date；每笔收益是82.5%仓位下的账户收益；最大回撤沿用正式开仓日记账口径）：",
        "区间/模型 | 笔数 | 胜率 | 均值 | 中位 | 复利净收益 | 最大回撤 | 最佳单笔收益 | 最差单笔收益 | 盈亏比 | 最大连亏",
    ]
    for period in result["period_statistics"]:
        for model in ("gated_ace", "shadow"):
            item = period[model]
            pl = "不适用" if item['profit_loss_ratio'] is None else f"{item['profit_loss_ratio']:.3f}"
            rows.append(f"{item['start']}~{item['end']}/{'停手后A/C/E' if model == 'gated_ace' else '不停手A/C/E'} | "
                        f"{item['sample_count']} | {pct(item['win_rate'])} | {pct(item['average_account_return'])} | "
                        f"{pct(item['median_account_return'])} | {pct(item['compound_return'])} | "
                        f"{pct(item['max_drawdown'])} | {pct(item['max_trade_profit'])} | {pct(item['max_trade_loss'])} | "
                        f"{pl} | {item['maximum_consecutive_losses']}")
            rows.append(f"  费用/开仓前权益之和：{pct(item['fees_relative_to_entry_equity_sum'])}；"
                        f"滑点/开仓前权益之和：{pct(item['slippage_relative_to_entry_equity_sum'])}。已计入净收益；不能直接作为区间收益减项。")
    rows += [
        "",
        "佣金0.03%、最低5元、过户费0.001%、买卖各0.1%滑点；卖出印花税沿用冻结历史分段表。",
        "复用正式已验收回放的成交判定、股数取整、T+1、涨跌停/停牌和退出日现金占用；没有把未成交计划强记为买入。",
        "2019起的A/C/E影子账是停手指标定义；上述历史停手后A/C/E诊断不含D，不能冠名完整ACDE收益。",
        "甲上线以前以现行规则回放属于反事实；2026-09-21以后的冻结前向区间单列。",
        "冻结训练期为2023-08~2026-07；2026-08起为文档规定的样本外延长段；63/126日监控窗口跨越训练与样本外，不整体冒充独立样本外。",
        "2018起完整ACDE旧基准的再认证另受早期价格/封单口径与D路径覆盖限制；不影响本次近期策略复核完成。",
        "不以这些限制要求恢复旧真实成交，也不把它们转成整个策略复核NOT_READY。",
        "IPO不足5个此前交易日的量比保持不适用，未填0；当前规则不使用缺失题材字段，未用现今题材反填历史。",
        "甲本身来自大规模条件研究，存在数据挖掘和过拟合风险；历史影子收益不能作为未来收益承诺。",
        "历史换手率代理模型无法恢复每笔下单时的真实Level-2队列；固定比例滚动复利也不证明同等收益能在大额资金下保持。",
        "结论依据：3/6个月失败线未触发，正常行情连续非正收益为1/3个月，A条件优势报警仍在，C未报警。",
        "因此归类②需观察，维持甲和现有停手；本次没有新的合格替代方案证据，不调整策略。",
        "",
        "本次工具改动（全部在同步目录外）：",
        f"文件：{OUT / 'review_strategy_only.py'}。完整可运行源码已保存。",
        "方法：main。新增范围约束、验收哈希/净值一致性核对、独立MA计算、失败线核对、假设空仓输出；替换/删除正式代码：无。",
        "方法：factor_evidence。新增正式条件健康复算、逐股票日原件复权核对、成熟/未成熟排除统计、分支完整性核对。",
        "方法：render_report。新增只含策略证据的报告；原因是移除私人账本依赖，明确模型、条件诊断与完整ACDE的边界。",
        f"文件：{OUT / 'verify_unpriced_samples.py'}；方法：capture_suspensions、main。新增现有权限HTTPS停复牌原件补采、缓存哈希核验和无报价分类；正式文件无替换、无删除。",
        "旧代码接口查询因TLS握手超时/连接重置未成功，改用已读官方公告和冻结日历解释两条；不关闭证书验证，不伪造接口返回或历史成交价。",
        "运行方式：",
        f"/Users/eass/A_System_Rebuild/2026-10-06/test_venv/bin/python '{OUT / 'review_strategy_only.py'}'",
        "验证结果：strategy_review.json状态COMPLETE_STRATEGY_ONLY_RECENT_REVIEW；研究池/来源哈希相符，独立MA差异0，复权兜底0，条件分支全部参与，无报价排除均已有来源解释（含明确标记的2条公告推断）。",
        "失败先检查冻结输入是否被改动、验收哈希是否一致、已有行情与复权文件是否完备；脚本只读已有权限内的落地证据。",
    ]
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args()
    base = args.input.resolve()
    code = base / "code"
    assert code.exists() and not OUT.is_relative_to(code)
    assert not OUT.is_relative_to(Path("/Users/eass/Desktop/A_System"))
    os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"
    sys.path.insert(0, str(code))
    from src.equity_curve_stop import (allowed_flags, completed_month_returns, current_stop_episode,
                                      decision_for_next_action_date, monthly_limit_up_mean,
                                      next_open_date, regime_stall_streak)
    from src.post_market_freshness import producer_identity

    config = read_json(code / "config/config.json")
    accepted = read_json(base / "evidence/rebuilt_current_dataset.json")
    previous = read_json(base / "evidence/completed_input_replay.json")
    assert accepted["status"] == "PASS_CURRENT_PRODUCTION_START_RESEARCH_INPUTS"
    assert previous["status"] == "PASS_CURRENT_2019_START_SHADOW_AND_STOP_REPLAY"
    as_of = accepted["end"]
    identity = producer_identity(code, as_of, config)
    assert identity == accepted["producer_identity"] == previous["next_action"]["producer_identity"]
    dataset = code / "data/research/review_refresh_20261010"
    assert sha256(dataset / "strict_feature_pool.csv") == accepted["pool_sha256"]
    print("INPUT_IDENTITY_AND_POOL_HASH_PASS", flush=True)

    calendar_path = code / "data/raw/trade_calendar.csv"
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str})
    dates = sorted(calendar[pd.to_numeric(calendar.is_open).eq(1)].cal_date.unique())
    nav_frame = pd.read_csv(base / "replay/shadow_nav_recomputed.csv", dtype={"action_date": str})
    labels, values = nav_frame.action_date.tolist(), nav_frame.shadow_nav.to_numpy()
    assert not nav_frame.action_date.duplicated().any()
    assert labels == sorted(labels) and labels[-1] == as_of
    assert labels == [date for date in dates if labels[0] <= date <= as_of]
    assert np.isfinite(values).all() and (values > 0).all()
    detail = pd.read_csv(base / "replay/shadow_detail.csv", dtype={"action_date": str}, low_memory=False)
    gated = pd.read_csv(base / "replay/gated_ace_detail.csv", dtype={"action_date": str}, low_memory=False)
    initial = read_json(code / "config/acde_rolling_optimization.json")["execution"]["initial_cash"]
    assert detail.action_date.tolist() == labels == gated.action_date.tolist()
    assert np.allclose(detail.equity_after / initial, values, rtol=1e-12, atol=1e-12)
    settings = config["equity_curve_stop"]
    ma, lag = int(settings["ma_window"]), int(settings["lag_trading_days"])
    assert ma == 60 and lag == 6 and settings["enabled"]
    flags = allowed_flags(values, ma_window=ma, lag=lag)
    independent = np.array([True if i - lag < ma else
                            values[i - lag] >= np.mean(values[i - lag - ma + 1:i - lag + 1])
                            for i in range(len(values))])
    assert np.array_equal(flags, independent)
    next_action = decision_for_next_action_date(labels, values, next_open_date(calendar_path, as_of),
                                                ma_window=ma, lag=lag)
    for key, value in next_action.items():
        recorded = previous["next_action"][key]
        # CSV十进制序列化再读取会产生末位浮点差异，判定和日期仍要求逐项相同。
        if isinstance(value, float):
            assert np.isclose(value, recorded, rtol=1e-13, atol=1e-10), key
        else:
            assert value == recorded, key

    weekly = settings["weekly_report"]
    rolling = []
    for name in ("3m", "6m"):
        window = int(weekly["window_" + name])
        change = float(values[-1] / values[-1 - window] - 1)
        line = float(weekly["shadow_" + name + "_fail"])
        rolling.append(dict(trading_days=window, base_date=labels[-1-window], end_date=as_of,
                            return_=change, failure_line=line, triggered=change <= line))
        rolling[-1]["return"] = rolling[-1].pop("return_")
    completed = [dict(ym=ym, shadow_return=ret,
                      limit_up_mean=monthly_limit_up_mean(dataset / "market_sentiment.csv", ym))
                 for ym, ret in completed_month_returns(labels, values, count=4)]
    assert all(row["limit_up_mean"] is not None for row in completed)
    stall = regime_stall_streak(completed, min_limit_up=weekly["regime_stall_min_limit_up"],
                                need_months=weekly["regime_stall_months"])
    stop = current_stop_episode(labels, values, ma_window=ma, lag=lag)
    assert stop is not None
    forward = nav_frame.action_date.ge("20260921")
    forward_trades = detail[detail.action_date.ge("20260921") & detail.status.eq("EXECUTED")].copy()
    for column in ("signal_date", "exit_date"):
        forward_trades[column] = forward_trades[column].astype(str).str.replace(r"\.0$", "", regex=True)
    probability_pool = pd.read_csv(dataset / "strict_feature_pool.csv",
                                   usecols=["trade_date", "ts_code", "fill_probability", "allow_buy_reliable",
                                            "is_fill_score_reliable"],
                                   dtype={"trade_date": str, "ts_code": str})
    forward_trades = forward_trades.merge(probability_pool.rename(columns={"trade_date": "signal_date"}),
                                          on=["signal_date", "ts_code"], how="left", validate="one_to_one")
    assert len(forward_trades) == previous["period_model_statistics"][1]["shadow"]["sample_count"]
    assert forward_trades.fill_probability.notna().all()
    assert forward_trades.allow_buy_reliable.all() and forward_trades.is_fill_score_reliable.all()
    forward_trades.to_csv(OUT / "hypothetical_shadow_forward_trades.csv", index=False, encoding="utf-8-sig")
    gated_forward_trades = gated[gated.action_date.ge("20260921") & gated.status.eq("EXECUTED")]
    assert not flags[forward.to_numpy()].any() and gated_forward_trades.empty
    print("MA_STOP_RULES_AND_FORWARD_EMPTY_ACCOUNT_PASS", flush=True)

    diagnostics = factor_evidence(code, dataset, config, dates, as_of)
    from verify_unpriced_samples import main as verify_unpriced
    exclusions = verify_unpriced()
    assert exclusions["unexplained_stock_days"] == 0
    assert producer_identity(code, as_of, config) == identity
    selected_files = [base / "replay/shadow_nav_recomputed.csv", base / "replay/shadow_detail.csv",
                      base / "replay/gated_ace_detail.csv", dataset / "strict_feature_pool.csv",
                      code / "config/config.json", code / "config/strategy_config.json",
                      code / "config/acde_rolling_optimization.json", Path(__file__)]
    selected_files += [base / "evidence/completed_input_replay.json",
                       base / "evidence/rebuilt_current_dataset.json", base / "base_commit.json",
                       code / "data/raw/stock_basic/stock_basic_all.csv",
                       OUT / "verify_unpriced_samples.py", OUT / "factor_exclusion_context.csv",
                       OUT / "factor_unpriced_classification.csv", OUT / "suspend_d_raw.csv",
                       OUT / "suspend_d_receipt.json", OUT / "bse_historic_code_exclusion_evidence.json"]
    result = {
        "status": "COMPLETE_STRATEGY_ONLY_RECENT_REVIEW",
        "conclusion": "②需观察：维持甲规则，A条件优势继续报警，按MA60滞后6日停手",
        "as_of": as_of, "base_commit": read_json(base / "base_commit.json")["commit"],
        "scope": "仅策略历史与冻结前向假设复核，不要求私人真实成交或账户净值",
        "producer_identity": identity, "source_and_research_pool_verified": True,
        "independent_ma_flag_differences": int((flags != independent).sum()),
        "rolling_returns": rolling, "completed_months": completed, "regime_stall": stall,
        "current_stop_episode": stop,
        "stop_missed_gain_triggered": stop["shadow_return"] >= weekly["worst_stop_missed_gain"],
        "formal_forward": {"start": "20260921", "end": as_of, "trading_days": int(forward.sum()),
                           "stop_days": int((~flags[forward.to_numpy()]).sum()),
                           "blocked_new_entry_legs": ["A", "C", "E", "D"],
                           "assumed_initial_positions": 0, "hypothetical_new_buys": 0,
                           "hypothetical_trade_return": 0.0, "hypothetical_drawdown": 0.0},
        "next_action": next_action, "factor_diagnostics": diagnostics,
        "forward_fill_probability": {"sample_count": len(forward_trades),
                                     "minimum": float(forward_trades.fill_probability.min()),
                                     "maximum": float(forward_trades.fill_probability.max()),
                                     "mean": float(forward_trades.fill_probability.mean()),
                                     "scope": "冻结历史换手率代理评分，不是实际排队概率"},
        "unpriced_exclusion_classification": exclusions,
        "period_statistics": previous["period_model_statistics"],
        "limitations": ["A/C/E影子账不含D，历史停手后诊断不代表完整ACDE收益",
                        "2018起完整旧基准未重新认证，不妨碍本次近期策略判断",
                        "因子健康是条件诊断毛收益口径，不是可交易净收益",
                        "当前版本前向样本少，存在过拟合风险"],
        "inputs_sha256": {str(path): sha256(path) for path in selected_files},
        "requires_actual_account_trades": False, "production_writes": False,
        "broker_calls_sent": False, "notifications_sent": False,
    }
    save_json("strategy_review.json", result)
    (OUT / "策略临时复核报告.txt").write_text(render_report(result), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "conclusion", "rolling_returns",
                                                  "formal_forward", "factor_diagnostics")},
                     ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
