"""QMT连接与账户：建连/退避/重连、唯一执行通道、账户快照校验、账户心跳、本地与券商对账、
启动时按券商事实恢复交易意图和持仓。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import csv
import json
import math
import os
import threading
import time
import uuid
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.broker_execution_service import IntentBrokerExecutionService
from src.trade_recovery import RecoveryOutcome, TradeRecoveryCoordinator, normalize_broker_positions
from src.daemon.common import (
    PROJECT_ROOT,
    _ACCOUNT_STATUS_DAILY_CACHE,
    _PROCESS_RECOVERY_EVENT,
    _clear_qmt_last_success,
    _critical_execution_state_failure,
    _fmt_position_time,
    _is_socket_resource_exhausted,
    _load_qmt_last_success,
    _mask_account,
    _normalize_ts_code,
    _notify,
    _notify_once_per,
    _positions_file_lock,
    _request_process_recovery,
    _save_qmt_last_success,
    _track_execution,
    _ts_code_aliases,
    is_trade_day,
    logger,
    prev_n_trade_days,
    write_broker_health,
    write_heartbeat,
)
from src.daemon.state import (
    _d_relay_pair_load_state,
    _exit_account_fingerprint,
    _load_exit_execution_state,
    _pov_load_state,
    _qmt_execution_init_lock,
    _recover_sell_price_from_fills,
    _strategy_identity_aliases,
    _trade_intent_store,
    load_pending_buys,
    load_positions,
    record_buy,
    restore_ghost_cleared_strategy_positions,
    save_positions,
)
from src.daemon.plans import _maybe_resume_pipeline_after_trade, has_open_local_position


_DAEMON_BOOT_ID = uuid.uuid4().hex


def qmt_is_critical_window() -> bool:
    """判断 QMT 心跳是否处于必须严格告警的窗口。

    非交易日/夜间 QMT 账户查询偶发卡住很常见，不能按一次超时就推送“账户断连”。
    但交易日前后、持仓中、盘前买单待确认时，QMT 可用性会直接影响买卖执行，
    必须保持严格检测和快速重连。
    """
    now = now_beijing()
    if has_open_local_position() or load_pending_buys():
        return True
    if not is_trade_day(now.date()):
        return False
    return datetime.time(9, 0) <= now.time() <= datetime.time(15, 10)


_broker_missing_streak: dict[str, int] = {}   # {ts_code: 连续查无此票的次数}


def reconcile_missing_local_positions(
    broker_positions: Any,
    source: str,
    *,
    snapshot_complete: bool = False,
) -> int:
    """逐票同步:本地 open 但券商连续 N 次查无此票 → 标 closed(2026-07-23 用户要求)。

    背景(京基智农事故):原 clear_local_positions_when_broker_empty 只在【全账户
    空仓】才触发。账户有打新中签可转债等外部持仓时永不空仓,导致手动卖出的策略
    持仓(如京基智农 B 腿 manual_exit)在券商侧已消失、本地却永远 open,持续
    BLOCK 次日开仓、卡住整个组合。改为逐票核对:某本地持仓 ts_code 连续
    broker_missing_confirm_count(默认3)次不在券商持仓集合里 → 标 closed。

    安全三重(与全账户清理同等严格):
      ① 调用方必须保证 query_positions() 成功(接口失败/QMT未启用严禁调用);
      ② 只有账户+持仓快照通过结构、code与资产自洽校验后才累计；
         连续 N 次完整快照均缺票，才按用户手动处理同步；
      ③ 当日买入(buy_date==今日)持仓豁免——QMT 可能尚未同步新成交,不误标。
    券商侧仍在的票即时归零其计数;标 closed 保留原字段并打同步来源标记。
    """
    with _positions_file_lock:
        positions = load_positions()
        open_like = [p for p in positions
                     if str(p.get("status", "")).lower() in {"open", "sell_pending"}]
        if not open_like:
            _broker_missing_streak.clear()
            return 0
        if not snapshot_complete:
            logger().error(
                "⚠️ [逐票同步] 本轮券商快照未完成结构/code/资产自洽认证，"
                "不累计本地持仓缺失次数（来源=%s）。",
                source,
            )
            return 0
        try:
            cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
            need = max(
                int(cfg.get("live_trade", {}).get("broker_missing_confirm_count", 3)),
                3,
            )
        except Exception:
            need = 3
        # 券商侧全部持仓的别名集合(volume>0)
        broker_aliases: set[str] = set()
        for bp in (broker_positions or []):
            if int(getattr(bp, "volume", 0) or 0) > 0:
                broker_aliases.update(_ts_code_aliases(getattr(bp, "ts_code", "")))
        today = today_beijing().strftime("%Y%m%d")
        now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        changed = 0
        rows_by_code: dict[str, list[dict[str, Any]]] = {}
        for pos in open_like:
            code = _normalize_ts_code(pos.get("ts_code", ""))
            if code:
                rows_by_code.setdefault(code, []).append(pos)
        seen_codes = set(rows_by_code)
        for code, rows in rows_by_code.items():
            in_broker = any(al in broker_aliases for al in _ts_code_aliases(code))
            if in_broker:
                _broker_missing_streak.pop(code, None)   # 券商重新出现→立即归零
                continue
            if any(str(pos.get("buy_date", "")) == today for pos in rows):
                continue   # 当日买入豁免:QMT 可能延迟,不误标
            # 同一股票可能有竞价+POV多条分片。本轮完整快照只能累计一次，
            # 不能按本地分片条数累计而在一次查询内提前达到3次。
            _broker_missing_streak[code] = _broker_missing_streak.get(code, 0) + 1
            n = _broker_missing_streak[code]
            name = str(rows[0].get("name", "") or "")
            if n < need:
                logger().warning(
                    "⚠️ [逐票同步] %s %s 券商完整快照查无此持仓"
                    "(本地%d条分片仍open,第%d/%d次,来源=%s)。"
                    "暂不同步；连续%d次完整快照均缺失后才按手动处理标closed。",
                    code, name, len(rows), n, need, source, need,
                )
                continue
            for pos in rows:
                pos["status"] = "closed"
                pos["sell_date"] = pos.get("sell_date") or today
                pos["sell_price"] = _recover_sell_price_from_fills(pos)
                pos["ghost_cleared_at"] = now_str
                pos["ghost_clear_source"] = f"逐票同步-{source}"
                pos["broker_confirmed_absent"] = True
                pos["broker_confirmed_absent_at"] = now_str
                pos["ghost_clear_reason"] = (
                    f"QMT连续{need}次完整且资产自洽的账户/持仓快照确认无此股票"
                    "（按手动卖出/已平仓同步）"
                )
                changed += 1
            _broker_missing_streak.pop(code, None)
            logger().warning(
                "🧹 [逐票同步] %s %s 券商连续%d次完整快照均查无此持仓，"
                "已将本地%d条分片标closed(来源=%s)，按手动处理解除开仓占用。",
                code, name, need, len(rows), source,
            )
        # 清理已消失(已 closed 或不再 open)的 code 的计数,防泄漏
        for code in list(_broker_missing_streak):
            if code not in seen_codes:
                _broker_missing_streak.pop(code, None)
        if changed:
            try:
                save_positions(positions)
            except Exception as exc:
                _critical_execution_state_failure(
                    f"逐票同步本地持仓({source})",
                    exc,
                )
    if changed:
        _track_execution("rebuild_summary")
        # 逐票同步可能正是尾盘卖出后最先确认“券商已无持仓”的路径。
        # 本地到期仓已经关闭时必须立刻解除交易优先暂停，不能继续每15秒
        # 打印“等待平仓”直到守护进程重启。
        _maybe_resume_pipeline_after_trade()
    return changed


def clear_local_positions_when_broker_empty(
    source: str,
    *,
    snapshot_complete: bool = False,
) -> int:
    """兼容旧调用名；空账户也统一走逐股票、连续3次完整快照状态机。"""

    return reconcile_missing_local_positions(
        [],
        source,
        snapshot_complete=snapshot_complete,
    )


def reconcile_positions_with_broker() -> None:
    """收盘全量对账：比对本地 positions.json(open) 与券商实际持仓，差异告警。

    只读不改账（避免误判自动改账带来风险），发现差异打日志 + Bark 告警，由人工核对。
    覆盖逐单成交确认漏掉的边角：成交确认超时后才成交、手动交易、券商端撤废修正等。
    """
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_on = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled")) \
            and bool(config.get("broker", {}).get("enabled"))
    except Exception as e:
        log.error("[收盘对账] 读取配置失败：%s", e)
        return

    if not qmt_on:
        log.info("[收盘对账] QMT 未启用，跳过。")
        return

    log.info("===== 收盘持仓对账 =====")
    broker_cfg = config.get("broker", {})

    # 券商实际持仓（volume>0）
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            broker_positions = adapter.query_positions()
    except Exception as e:
        log.error("[收盘对账] 查询券商持仓失败：%s", e)
        _notify("reconcile", "⚠️ 收盘对账失败", "查询券商持仓失败，无法核对账实一致，请回终端检查。",
                level="timeSensitive")
        return

    broker_map: dict[str, dict[str, Any]] = {}
    for bp in broker_positions:
        vol = int(getattr(bp, "volume", 0) or 0)
        if vol <= 0:
            continue
        code = str(getattr(bp, "ts_code", ""))
        broker_map[code] = {"volume": vol, "name": str(getattr(bp, "name", ""))}

    # 本地 open 持仓按 ts_code 汇总股数
    local_map: dict[str, dict[str, Any]] = {}
    for p in load_positions():
        if str(p.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        code = str(p.get("ts_code", ""))
        entry = local_map.setdefault(code, {"shares": 0, "name": str(p.get("name", ""))})
        entry["shares"] += int(p.get("shares", 0) or 0)

    diffs: list[str] = []
    for code in sorted(set(local_map) | set(broker_map)):
        local_qty = local_map.get(code, {}).get("shares", 0)
        broker_qty = broker_map.get(code, {}).get("volume", 0)
        name = local_map.get(code, {}).get("name") or broker_map.get(code, {}).get("name", "")
        if local_qty == broker_qty:
            continue
        if local_qty > 0 and broker_qty == 0:
            diffs.append(f"{code} {name} 本地{local_qty}股/券商无（已卖未标记或幽灵持仓）")
        elif broker_qty > 0 and local_qty == 0:
            diffs.append(f"{code} {name} 券商{broker_qty}股/本地无（实际持有未记账）")
        else:
            diffs.append(f"{code} {name} 数量不符 本地{local_qty}/券商{broker_qty}")

    if not diffs:
        log.info("✅ [收盘对账] 账实一致：本地与券商持仓完全匹配（共%d只）。", len(broker_map))
        return

    for d in diffs:
        log.warning("⚠️ [收盘对账] 差异：%s", d)
    summary = "；".join(diffs[:5])
    if len(diffs) > 5:
        summary += f"；…共{len(diffs)}处"
    _notify("reconcile", "⚠️ 收盘对账发现差异",
            f"本地与券商持仓不一致：{summary}。请回终端核对并手动修正。",
            level="timeSensitive")


_qmt_reconnect_count: int = 0       # 累计重连次数，成功后归零
_qmt_transport_busy_count: int = 0  # 内置文件通道连续占用次数；不等同账户断连
_qmt_adapter: Any = None             # 持久连接，程序生命周期内保持
_qmt_execution_service: IntentBrokerExecutionService | None = None
_qmt_last_verified_at: str = ""      # 最近一次 query_account/query_positions 成功时间
_last_account_has_position: bool = False  # 最近一次券商账户心跳是否确认有持仓
_qmt_lock = threading.RLock()        # 保护 _qmt_adapter 并发访问（可重入：平仓路径内嵌撤止盈单）

# QMT建连是有资源成本的操作。xtquant在connect=-1时不一定能立即释放
# WaitingFreeWriter/套接字；因此所有业务线程共用同一个进程级退避门，
# 禁止账户心跳、买单确认、POV等在连接故障时各自密集新建会话。
_qmt_connect_failure_streak: int = 0
_qmt_connect_retry_not_before: float = 0.0
_qmt_connect_last_error: str = ""


class QMTReconnectBackoffError(RuntimeError):
    """上一次QMT建连已失败，当前仍在统一退避窗口。"""


def _is_qmt_inner_file_transport_busy(error: BaseException) -> bool:
    """识别内置 spool 的 Windows 文件占用，避免主动拆掉仍正常的QMT会话。"""
    winerror = getattr(error, "winerror", None)
    if not isinstance(error, PermissionError) and winerror not in {5, 32, 33}:
        return False
    normalized = str(error).lower().replace("\\", "/")
    return "/qmt_inner/spool/" in normalized


# 内置桥接失联告警与恢复必须成对：推过🛑就一定要推✅，否则用户只看到失联、
# 看不到自愈（2026-09-23 08:51告警、08:52恢复却静默，用户等了71分钟才看到例行播报）。
# 进程内保存即可：daemon若在失联期间重启，keeper会另外推“程序与账户已恢复正常”。
_qmt_inner_outage: dict[str, Any] = {}
# 客户端现场与失联告警同频刷新：首次失联立即查，之后每次重复告警前更新一次。
_QMT_CLIENT_DIAGNOSIS_INTERVAL_SEC = 1800


def _is_qmt_inner_heartbeat_stale(error: BaseException | str) -> bool:
    """兼容原始错误及连接退避包装；心跳失效不能推断为客户端崩溃或未登录。"""
    return "qmt inner bridge heartbeat is stale" in str(error).lower()


def _qmt_client_processes() -> list[dict[str, Any]] | None:
    from src.qmt_client_probe import list_qmt_client_processes

    return list_qmt_client_processes()


def _diagnose_qmt_client(log: Any) -> dict[str, str]:
    """只读查看客户端进程现场并写日志，作为每次失联的证据。

    2026-09-15~09-28客户端每天08:51/20:52自行重启，遇到验证码时停在登录页，
    桥接一直断到人工登录。告警必须说清是"要人工登录"还是"模型没在运行"。
    查询失败只记日志，不影响原有阻断和告警。
    """
    try:
        from src.qmt_client_probe import classify_qmt_client_state, describe_processes

        processes = _qmt_client_processes()
        last_verified = None
        if _qmt_last_verified_at:
            last_verified = datetime.datetime.strptime(
                _qmt_last_verified_at, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=BEIJING_TZ)
        diagnosis = classify_qmt_client_state(processes, last_verified_at=last_verified)
        log.warning(
            "QMT客户端现场：%s。%s（%s；最后验证成功=%s）",
            diagnosis["summary"], diagnosis["action"],
            describe_processes(processes), _qmt_last_verified_at or "本进程尚未验证成功",
        )
        return diagnosis
    except Exception as exc:
        log.warning("QMT客户端现场查询失败（不影响交易阻断）：%s", exc)
        return {}


def _record_qmt_inner_heartbeat_stale(error: BaseException, log: Any) -> bool:
    """内置模型无新心跳时明确告警，保留现场，等待本机模型恢复。

    重启外部daemon无法启动客户端内的模型，更不能代替登录。此处只记录
    不可用状态和限频告警，不启动/停止客户端，不放宽交易门禁。
    夜间也必须提示一次，避免失联整夜直到次日开盘才暴露。
    """
    if not _is_qmt_inner_heartbeat_stale(error):
        return False
    write_broker_health("unavailable", error=error, failure_count=_qmt_reconnect_count)
    log.error(
        "🛑 QMT内置桥接心跳已过期：账户尚不可验证；等待内置模型恢复。"
        "不因该错误重启客户端或daemon；外部进程存活不代表交易连接恢复。"
        "请核对客户端登录和A_SYSTEM_QMT_INNER运行状态；"
        "客户端退出/模型停止的触发原因尚需另查。原始错误：%s", error,
    )
    now_ts = time.time()
    since_ts = float(_qmt_inner_outage.setdefault("since_ts", now_ts))
    if now_ts - float(_qmt_inner_outage.get("diagnosed_ts") or 0.0) >= _QMT_CLIENT_DIAGNOSIS_INTERVAL_SEC:
        _qmt_inner_outage["diagnosis"] = _diagnose_qmt_client(log)
        _qmt_inner_outage["diagnosed_ts"] = now_ts
    diagnosis = _qmt_inner_outage.get("diagnosis") or {}
    if diagnosis.get("summary"):
        scene = f"{diagnosis['summary']}。{diagnosis['action']}"
    else:
        scene = "请核对客户端登录和A_SYSTEM_QMT_INNER运行状态。"
    minutes = (now_ts - since_ts) / 60.0
    span = f"已失联{minutes:.0f}分钟" if minutes >= 1 else "刚刚失联"
    if _notify_once_per(
        "qmt_inner_heartbeat_stale", 1800,
        "🛑 内置桥接失联，交易连接未恢复",
        f"内置模型心跳已过期（{span}），系统无法验证账户，交易调用继续阻断。{scene}"
        "系统不会因此自动退出或重启客户端。",
        level="timeSensitive",
    ):
        _qmt_inner_outage["alerted"] = True
    return True


def _notify_qmt_inner_outage_recovered(log: Any) -> bool:
    """账户重新可验证后，与🛑告警成对推送一次恢复；未推过告警则只清状态。

    必须在QMT锁外调用：推送走网络，不能占用唯一券商通道。
    """
    if not _qmt_inner_outage:
        return False
    alerted = bool(_qmt_inner_outage.get("alerted"))
    since_ts = float(_qmt_inner_outage.get("since_ts") or 0.0)
    _qmt_inner_outage.clear()
    if not alerted:
        return False
    minutes = max(0.0, (time.time() - since_ts) / 60.0) if since_ts else 0.0
    span = f"{minutes:.0f}分钟" if minutes >= 1 else "不到1分钟"
    log.info("内置桥接已恢复，失联%s，推送恢复通知。", span)
    return bool(
        _notify(
            "connection",
            "✅ 内置桥接已恢复，交易连接正常",
            f"内置模型心跳恢复，本次失联{span}，账户可验证，交易调用已放行。"
            "失联期间系统未发送任何委托。",
            level="timeSensitive",
        )
    )


def _qmt_connect_backoff_seconds(failure_streak: int) -> float:
    """连接失败的进程级退避；最长5分钟，成功后立即归零。"""

    schedule = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
    index = min(max(int(failure_streak), 1), len(schedule)) - 1
    return schedule[index]


def _broker_config_with_qmt_override(
    broker_config: dict,
    *,
    qmt_path: str = "",
    session_id: str = "",
) -> dict:
    """按上次成功会话覆盖 QMT path/session，避免重复连接已知失败的默认 session。"""
    if not qmt_path and not session_id:
        return broker_config
    cfg = dict(broker_config)
    if qmt_path:
        os.environ[str(cfg.get("qmt_path_env", "QMT_PATH"))] = qmt_path
    if session_id:
        os.environ[str(cfg.get("session_id_env", "QMT_SESSION_ID"))] = str(session_id)
    return cfg


def _should_clear_qmt_cache_for_error(error_text: str) -> bool:
    """判断是否真的要清除 QMT 成功会话缓存。

    QMT 的 connect=-1 在实盘环境里可能是短暂忙，不一定代表 session 永久失效；
    日志里已经出现过同一 session 先 connect=-1、随后完整扫描又成功的情况。
    因此首选 session 单次失败不清缓存，完整扫描成功后会自然刷新缓存。
    只有缓存路径本身明显不可用时才清除。
    """
    text = str(error_text)
    hard_fail_markers = ["No such file", "不存在", "路径不存在", "not found"]
    return any(marker in text for marker in hard_fail_markers)


def _qmt_connect_once(
    broker_config: dict,
    *,
    preferred_only: bool,
    timeout_sec: float,
    qmt_path: str = "",
    session_id: str = "",
) -> Any:
    """在唯一QMT执行线程内同步建立连接。

    超时由外层BrokerExecutionService负责；若xtquant C++调用卡死，
    外层会标记通道中毒并终止整个daemon。这里不再创建一条无法
    回收的连接线程，避免超时后旧线程仍在抢QMT session。
    """
    del timeout_sec  # 仅保留参数兼容旧调用，真正超时在唯一执行通道。
    from src.qmt_adapter import QMTBrokerAdapter

    adapter = QMTBrokerAdapter.from_config(
        _broker_config_with_qmt_override(
            broker_config,
            qmt_path=qmt_path,
            session_id=session_id,
        )
    )
    adapter.connect(preferred_only=preferred_only)
    if hasattr(adapter, "assert_execution_ready"):
        adapter.assert_execution_ready()
    return adapter


def _qmt_connect(broker_config: dict, *, allow_full_scan: bool | None = None) -> Any:
    """建立新 QMT 连接。不持有 _qmt_lock，调用方按需加锁。

    连接原则：
    1. 启动门禁/关键重连使用 allow_full_scan=True，只做一次完整扫描。
       不先试缓存再叠加完整扫描，避免首选 session 超时线程未退出时又抢 QMT。
    2. 轻量心跳使用缓存优先，失败后交给下一轮关键重连处理。
    """
    global _qmt_connect_failure_streak
    global _qmt_connect_retry_not_before
    global _qmt_connect_last_error
    global _LAST_QMT_ERROR_TEXT

    monotonic_now = time.monotonic()
    if monotonic_now < _qmt_connect_retry_not_before:
        remaining = _qmt_connect_retry_not_before - monotonic_now
        raise QMTReconnectBackoffError(
            f"QMT建连统一退避中，{remaining:.1f}秒后才允许下一次真实连接；"
            f"上次错误={_qmt_connect_last_error or '未记录'}"
        )

    errors: list[str] = []
    cached = _load_qmt_last_success()
    attempts: list[dict[str, Any]] = []

    if allow_full_scan is True:
        # 一次完整扫描本身就会先试传入的缓存path/session，再遍历备用
        # session。旧实现先单独新建一个“缓存session”连接对象，失败后又
        # 新建第二个完整扫描对象；connect=-1时前一个对象的底层资源可能
        # 尚未释放，反而会干扰真正的备用session扫描。
        attempts.append({
            "label": "完整备用path/session",
            "preferred_only": False,
            "timeout_sec": 55.0,
            "qmt_path": str(cached.get("qmt_path", "")),
            "session_id": str(cached.get("session_id", "")),
        })
    elif cached:
        attempts.append({
            "label": "上次成功path/session",
            "preferred_only": True,
            "timeout_sec": 12.0,
            "qmt_path": str(cached.get("qmt_path", "")),
            "session_id": str(cached.get("session_id", "")),
        })
    else:
        attempts.append({
            "label": "完整备用path/session",
            "preferred_only": False,
            "timeout_sec": 25.0,
            "qmt_path": "",
            "session_id": "",
        })

    for attempt in attempts:
        try:
            adapter = _qmt_connect_once(
                broker_config,
                preferred_only=bool(attempt["preferred_only"]),
                timeout_sec=float(attempt["timeout_sec"]),
                qmt_path=str(attempt.get("qmt_path", "")),
                session_id=str(attempt.get("session_id", "")),
            )
            _save_qmt_last_success(
                qmt_path=str(getattr(adapter, "_active_qmt_path", "")),
                session_id=str(getattr(adapter, "_active_session_id", "")),
            )
            _qmt_connect_failure_streak = 0
            _qmt_connect_retry_not_before = 0.0
            _qmt_connect_last_error = ""
            return adapter
        except Exception as exc:
            errors.append(f"{attempt['label']}: {exc}")
            if attempt["label"] == "上次成功path/session" and _should_clear_qmt_cache_for_error(str(exc)):
                _clear_qmt_last_success(str(exc))
    error_text = "QMT连接失败: " + " | ".join(errors)
    _qmt_connect_failure_streak += 1
    _qmt_connect_last_error = error_text
    _LAST_QMT_ERROR_TEXT = error_text
    _qmt_connect_retry_not_before = (
        time.monotonic() + _qmt_connect_backoff_seconds(_qmt_connect_failure_streak)
    )
    raise RuntimeError(error_text)


def _get_qmt_execution_service() -> IntentBrokerExecutionService:
    global _qmt_execution_service
    with _qmt_execution_init_lock:
        if _qmt_execution_service is None:
            _qmt_execution_service = IntentBrokerExecutionService(
                intent_store=_trade_intent_store(),
                account_fingerprint_provider=_exit_account_fingerprint,
                business_date_provider=lambda: today_beijing().strftime("%Y%m%d"),
                # 建连在_qmt_get里单独给90秒；建连成功后的账户/委托/
                # 行情/下单均应在35秒内返回，否则线程已不可信。
                default_timeout=35.0,
                timeout_callback=_on_qmt_execution_timeout,
                recovery_required_callback=_on_broker_result_unknown,
            )
        return _qmt_execution_service


def _qmt_get_raw(broker_config: dict, *, allow_full_scan: bool | None = None) -> Any:
    """仅供唯一执行线程获取原始持久QMT连接。"""
    global _qmt_adapter
    if _qmt_adapter is None:
        effective_full_scan = allow_full_scan
        if effective_full_scan is None:
            # 2026-09-08事故：持久连接一次只读查询返回None后被释放，之后
            # 每轮都先试缓存session并失败；“第3轮完整扫描”在同轮第二次
            # 调用时又被刚设置的退避门拦截，导致完整扫描永远执行不到。
            # 在唯一建连入口按真实connect失败次数升级，第3次实际建连直接
            # 执行一次完整扫描，业务任务与账户心跳都无法绕过这条规则。
            effective_full_scan = _qmt_connect_failure_streak == 2
        _qmt_adapter = _qmt_connect(
            broker_config,
            allow_full_scan=bool(effective_full_scan),
        )
    return _qmt_adapter


def _qmt_get(broker_config: dict, *, allow_full_scan: bool | None = None) -> Any:
    """返回统一意图执行代理；业务线程永远拿不到原始QMT adapter。"""

    if _PROCESS_RECOVERY_EVENT.is_set():
        raise RuntimeError("进程级交易恢复已启动，拒绝继续访问QMT")
    service = _get_qmt_execution_service()
    if _qmt_adapter is None:
        service.call_function(
            lambda: _qmt_get_raw(
                broker_config,
                allow_full_scan=allow_full_scan,
            ),
            operation="connect_qmt",
            timeout=90.0,
        )
    return service.proxy(
        lambda: _qmt_get_raw(broker_config, allow_full_scan=allow_full_scan)
    )


class SharedQMTBrokerProxy:
    """D监控使用的共享QMT代理。

    架构约束：一个进程生命周期内只允许主守护进程持有 QMT 连接。
    D监控不能再作为独立子进程连接 QMT，否则会和账户心跳/下单互抢 session。
    该代理把 D 监控需要的 broker 方法统一转发到交易意图状态机和唯一QMT执行线程。
    """

    def __init__(self, broker_config: dict[str, Any]) -> None:
        self.broker_config = broker_config

    def query_account(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_account()

    def query_positions(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_positions()

    def query_orders(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_orders()

    def query_trades(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_trades()

    def get_full_tick(self, ts_codes: list[str]) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).get_full_tick(ts_codes)

    def get_minute_bars(
        self,
        ts_codes: list[str],
        *,
        start_time: str,
        end_time: str,
    ) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).get_minute_bars(
                ts_codes,
                start_time=start_time,
                end_time=end_time,
            )

    def place_order(self, request: Any) -> Any:
        side = str(getattr(request, "side", "")).strip().upper()
        if side == "BUY":
            # D盘中BUY走共享QMT代理，不经过A/C/E的计划单执行入口；因此必须
            # 在唯一落单点补同一份发布认证门禁。认证失效只阻断BUY，SELL、撤单
            # 和成交回写继续可用，避免退役/换版时出现“静态腿停了但D仍能买”。
            from src.live_order_gateway import LiveOrderGateway

            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            confirm = config.get("live_trade", {}).get(
                "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"
            )
            LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(
                str(confirm),
                side="BUY",
            )
        with _qmt_lock:
            return _qmt_get(self.broker_config).place_order(request)

    def cancel_order(self, order_id: str) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).cancel_order(order_id)

    def get_order_fill(self, order_id: str) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).get_order_fill(order_id)

    def disconnect(self) -> None:
        """共享连接由主守护进程管理，D监控退出时不能断开。"""
        return None


_TRANSIENT_QMT_READ_ERROR_MARKERS = (
    "QMT账户查询返回None",
    "QMT账户查询返回空对象",
    "QMT持仓查询返回None",
    "QMT委托查询返回None",
    "QMT成交查询返回None",
    "QMT行情查询返回None",
)


def _is_transient_qmt_read_error(error: BaseException | str) -> bool:
    """只识别QMT明确的瞬时只读空返回，不放宽非法结构或账务矛盾。"""

    text = str(error)
    return any(marker in text for marker in _TRANSIENT_QMT_READ_ERROR_MARKERS)


def _qmt_query_account_positions(adapter: Any, *, timeout_sec: float = 25.0) -> tuple[Any, Any]:
    """账户连接验证：资产和持仓均必须经唯一QMT队列成功返回。

    账户在活动买单期间会出现 ``total_asset > available_cash + market_value``；
    先用QMT直接返回的frozen_cash认证。只有这一轮因“缺失策略持仓+
    资产差额”被拒绝时，才额外查一次当日委托，用非终态BUY的剩余金额
    解释冻结资金。不为每分钟心跳无条件增加QMT查询。
    """
    del timeout_sec  # 业务调用超时由执行服务统一管理，不再额外生成孤儿线程。
    for attempt_no in (1, 2):
        try:
            account = adapter.query_account()
            positions = adapter.query_positions()
            try:
                _sanitize_account_snapshot(
                    account,
                    positions,
                    log_inconsistent=False,
                )
            except BrokerSnapshotInconsistentError:
                # 资产和持仓查询本身已成功；只为识别活动买单冻结金额补查委托。
                active_orders = adapter.query_orders()
                _sanitize_account_snapshot(
                    account,
                    positions,
                    active_orders=active_orders,
                    log_inconsistent=True,
                )
            return account, positions
        except BrokerSnapshotInconsistentError:
            # 账务语义矛盾不是瞬时传输空返回，必须立即fail-closed。
            raise
        except Exception as exc:
            if attempt_no == 1 and _is_transient_qmt_read_error(exc):
                logger().warning(
                    "QMT只读查询瞬时空返回，保留当前持久连接并原会话复查1次：%s",
                    exc,
                )
                time.sleep(0.2)
                continue
            raise
    raise RuntimeError("QMT账户/持仓查询重试流程异常结束")


_ASSET_SANITY_ALERT_DAY = ""


class BrokerSnapshotInconsistentError(RuntimeError):
    """券商查询有返回，但同一组账户/持仓快照自相矛盾。"""


_ACTIVE_QMT_ORDER_STATUSES = frozenset({48, 49, 50, 51, 52, 55})


def _active_buy_order_reserved_cash(active_orders: Any) -> float:
    """估算QMT当日活动买单尚未成交部分占用的资金。

    只认QMT明确的非终态状态和BUY/STOCK_BUY方向。字段缺失时返回0，
    不猜测、不用此函数放宽真实的持仓缺失。
    """

    from src.qmt_adapter import first_present, object_to_dict, to_float, to_int

    reserved = 0.0
    for raw_order in active_orders or []:
        data = object_to_dict(raw_order)
        status = to_int(
            first_present(data, ["order_status", "m_nOrderStatus", "status"], -1),
            -1,
        )
        if status not in _ACTIVE_QMT_ORDER_STATUSES:
            continue
        order_type = to_int(
            first_present(data, ["order_type", "m_nOrderType"], -1),
            -1,
        )
        side = str(
            first_present(data, ["side", "order_side", "direction"], "")
        ).strip().upper()
        if order_type != 23 and side not in {"BUY", "B", "买入"}:
            continue
        ordered = max(
            to_int(
                first_present(
                    data,
                    ["order_volume", "m_nOrderVolume", "volume", "quantity"],
                    0,
                )
            ),
            0,
        )
        traded = max(
            to_int(
                first_present(
                    data,
                    ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"],
                    0,
                )
            ),
            0,
        )
        price = max(
            to_float(
                first_present(
                    data,
                    ["price", "order_price", "m_dPrice", "limit_price"],
                    0.0,
                )
            ),
            0.0,
        )
        remaining = max(ordered - traded, 0)
        if remaining > 0 and price > 0:
            reserved += remaining * price
    return reserved


def _sanitize_account_snapshot(
    account: Any,
    positions: Any,
    *,
    active_orders: Any = None,
    log_inconsistent: bool = True,
) -> None:
    """总资产自洽性校验（2026-07-25 事故：非交易日QMT先返回0、后返回1亿）。

    QMT 偶发返回脏的 total_asset，而它直接决定单票/总仓位上限：
      - total_asset=0   → 仓位上限=0 → 当日买入全部被拒（漏开仓）；
      - total_asset=1亿 → 误触发容量墙里程碑，且7.30取消单笔硬顶后会超买。
    校验用两个独立字段交叉验证：total_asset ≈ available_cash + Σ持仓市值。
    偏离一倍以上判定脏读，改用 implied 值（可信度更高：由两个独立字段算出），
    并推送告警。implied 也不可用时保持原值不动，交由上层风控/人工处理。
    """
    global _ASSET_SANITY_ALERT_DAY
    try:
        total = float(getattr(account, "total_asset", 0.0) or 0.0)
        cash = float(getattr(account, "available_cash", 0.0) or 0.0)
        mv = 0.0
        broker_aliases: set[str] = set()
        for p in (positions or []):
            try:
                volume = max(int(getattr(p, "volume", 0) or 0), 0)
                if volume <= 0:
                    continue
                mv += float(getattr(p, "market_value", 0.0) or 0.0)
                broker_aliases.update(_ts_code_aliases(getattr(p, "ts_code", "")))
            except (TypeError, ValueError):
                continue
        base_implied = cash + mv
        reported_frozen = max(
            float(getattr(account, "frozen_cash", 0.0) or 0.0),
            0.0,
        )
        order_reserved = _active_buy_order_reserved_cash(active_orders)
        # 冻结金额只能解释 total 中已经存在的正差额，不能反向把过期/
        # 延迟委托金额加到账户总资产上。如此既兼容QMT frozen_cash字段缺失，
        # 又不会用委托表掩盖 total_asset=0 等脏读。
        positive_gap = max(total - base_implied, 0.0)
        recognized_reserved = min(
            max(reported_frozen, order_reserved),
            positive_gap,
        )
        implied = base_implied + recognized_reserved

        # 2026-08-18事故：QMT在08:06/08:07连续两轮瞬时返回空持仓，
        # 但total_asset仍比cash多出整仓市值。旧逻辑错把total_asset改小，
        # 随后又把真实策略仓当幽灵仓清掉。如今只要本地有活跃/近期误清策略身份，
        # 券商快照缺该票，且资产差额又证明“持仓列表可能不完整”，整组快照直接作废。
        expected_aliases = _strategy_identity_aliases(include_recent_ghost=True)
        missing_aliases = expected_aliases - broker_aliases
        unexplained_asset = total - implied
        if missing_aliases and (
            (total <= 0 and implied <= 0)
            or unexplained_asset > max(abs(total) * 0.05, 1_000.0)
        ):
            missing_codes = sorted(
                {
                    alias.split(".")[0]
                    for alias in missing_aliases
                    if alias and alias.split(".")[0].isdigit()
                }
            )
            raise BrokerSnapshotInconsistentError(
                "QMT持仓快照与策略账本/资产不自洽："
                f"缺失策略代码={missing_codes or sorted(missing_aliases)} "
                f"总资产={total:.2f} 可用资金={cash:.2f} 已返回持仓市值={mv:.2f} "
                f"认证冻结资金={recognized_reserved:.2f} "
                f"未解释差额={unexplained_asset:.2f}。"
                "本轮禁止清理本地持仓或执行新买入。"
            )
        if implied <= 0:
            return   # 两个字段都不可用，无从校正，保持原值
        # 容差：total 落在 implied 的 [0.5, 2] 倍内视为正常（含日内浮盈亏/冻结资金差异）
        if 0.5 * implied <= total <= 2.0 * implied:
            return
        today = today_beijing().strftime("%Y%m%d")
        corrected = total < implied
        action_text = (
            f"已改用{implied / 10000:.2f}万"
            if corrected
            else "保留券商total_asset，不用较小的cash+mv反向改写"
        )
        logger().error(
            "⚠️ [账户资产脏读] QMT返回总资产%.2f万，与可用资金%.2f万+"
            "持仓市值%.2f万+认证冻结%.2f万=%.2f万严重不符；%s。",
            total / 10000,
            cash / 10000,
            mv / 10000,
            recognized_reserved / 10000,
            implied / 10000,
            action_text,
        )
        # 持仓市值已返回、但total_asset短暂为0时，可以用cash+mv保守修正。
        # 反向的total>cash+mv可能是冻结资金或持仓列表不完整，绝不再擅自把总资产改小。
        if corrected:
            try:
                setattr(account, "total_asset", implied)
            except Exception:
                return
        if _ASSET_SANITY_ALERT_DAY != today:
            _ASSET_SANITY_ALERT_DAY = today
            _notify(
                "system_error", "⚠️ 账户资产快照不自洽",
                f"QMT返回总资产{total / 10000:.2f}万，与可用{cash / 10000:.2f}万+"
                f"市值{mv / 10000:.2f}万+认证冻结{recognized_reserved / 10000:.2f}万"
                f"={implied / 10000:.2f}万不符，{action_text}。若持续出现请检查QMT终端。",
                level="timeSensitive",
            )
    except BrokerSnapshotInconsistentError as exc:
        # 必须传递给心跳/业务调用者：本轮只记录失败并下轮重试，
        # 不允许继续把矛盾快照当成空仓、清账或放行新买入。
        if log_inconsistent:
            logger().error("❌ 券商快照无效：%s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger().warning("账户资产自洽性校验异常（不影响主流程）：%s", exc)


_LAST_QMT_ERROR_TEXT = ""
# 套接字资源耗尽时的退出码（2026-07-27 事故）。注意：daemon 由 start_windows.py 以
# DETACHED_PROCESS 启动，keeper 拿不到本退出码——它只按"进程不在就拉起"工作，行为一致。
# 本退出码用于人工排查（事件查看器/手动前台运行时可见），以及未来若改为非 detached 启动。
EXIT_CODE_SOCKET_EXHAUSTED = 87
EXIT_CODE_QMT_CHANNEL_POISONED = 88
EXIT_CODE_BROKER_RESULT_UNKNOWN = 91
EXIT_CODE_QMT_RECONNECT_STALLED = 92


def _on_qmt_execution_timeout(reason: str, sequence: int) -> None:
    """QMT C++调用无法安全中断时，用进程重启回收整条通道。"""

    message = f"QMT唯一执行通道超时中毒：{reason} sequence={sequence}"
    _request_process_recovery(
        reason=message,
        exit_code=EXIT_CODE_QMT_CHANNEL_POISONED,
        heartbeat_status="qmt_channel_poisoned_restarting",
        title="🔄 QMT执行通道卡死，自动重启中",
        body=(
            f"{reason}。未知结果委托不会盲目重发；keeper拉起新进程后会先用"
            "QMT持仓、委托和成交恢复，再启动交易线程。"
        ),
        clear_qmt_cache=True,
    )


def _on_broker_result_unknown(reason: str, sequence: int) -> None:
    """下单/撤单调用异常且结果未知时，立即切换到券商事实恢复。"""

    message = f"券商交易动作结果未知：{reason} sequence={sequence}"
    _request_process_recovery(
        reason=message,
        exit_code=EXIT_CODE_BROKER_RESULT_UNKNOWN,
        heartbeat_status="broker_result_recovery_restarting",
        title="🔄 券商交易结果未知，程序自动恢复",
        body=(
            f"{reason}。系统不会盲目重发；keeper拉起新进程后会先核对QMT"
            "委托、成交、持仓和事务意图，再恢复开平仓线程。"
        ),
        clear_qmt_cache=True,
    )


def _record_broker_snapshot_inconsistent(
    error: BrokerSnapshotInconsistentError,
    log: Any,
    *,
    context: str,
) -> None:
    """记录业务快照不一致，但绝不重建QMT连接。

    账户、持仓（以及必要时的委托）查询已经有返回，证明传输通道可用。
    这类错误必须fail-closed阻断本轮派生交易，但若把它当成掉线反复
    connect，会主动销毁仍正常的持久会话并耗尽xtquant资源。
    """

    write_broker_health(
        "snapshot_inconsistent",
        error=error,
        failure_count=0,
    )
    log.error(
        "⛔ QMT查询通道仍可用，但券商快照未通过账务认证（%s）：%s。"
        "本轮禁止持仓清理和新买入，保留原QMT会话，不执行断连/重连。",
        context,
        error,
    )
    _notify_once_per(
        "broker_snapshot_inconsistent",
        300,
        "⚠️ 账户快照校验未通过",
        "QMT连接仍可查询，但账户/持仓/委托快照不自洽。"
        "系统已拒绝本轮持仓清理和新买入，不会重建QMT会话；"
        "请核对QMT账户、活动委托与持仓。",
        level="timeSensitive",
    )


def _request_qmt_resource_recovery(error: BaseException) -> bool:
    """xtquant连接器/套接字已耗尽时，只允许整进程回收一次。"""

    error_text = str(error)
    if not _is_socket_resource_exhausted(error_text):
        return False
    return _request_process_recovery(
        reason=f"QMT底层连接资源已耗尽:{error_text}",
        exit_code=EXIT_CODE_SOCKET_EXHAUSTED,
        heartbeat_status="qmt_resources_exhausted_restarting",
        title="🔄 QMT资源耗尽，程序自动恢复",
        body=(
            "xtquant已报WaitingFreeWriter/套接字资源耗尽。"
            "daemon已熔断新建会话并退出，keeper将启动全新进程；"
            "新进程会先核对真实委托、成交和持仓，不会盲目重发。"
        ),
        clear_qmt_cache=False,
    )


def _request_qmt_reconnect_stalled_recovery(error: BaseException) -> bool:
    """旧miniQMT多次建连失败时只回收daemon，绝不重启客户端。"""

    # 内置模型属于客户端进程；重启daemon不能恢复其心跳。包括统一退避
    # 包装中的同一错误都应保持原进程等待，不能误用miniQMT资源回收路径。
    if _is_qmt_inner_heartbeat_stale(error):
        return False

    if _qmt_connect_failure_streak < 3:
        return False
    error_text = str(error)
    if "connect=-1" not in error_text and "QMT连接失败" not in error_text:
        return False
    return _request_process_recovery(
        reason=(
            "QMT持久连接丢失且本进程连续建连失败，完整备用session扫描仍未恢复:"
            f"{error_text}"
        ),
        exit_code=EXIT_CODE_QMT_RECONNECT_STALLED,
        heartbeat_status="qmt_reconnect_stalled_restarting",
        title="🔄 QMT重连卡死，程序自动恢复",
        body=(
            "QMT持久连接丢失后，当前daemon的首选及备用session均未恢复。"
            "系统已停止继续交易并退出当前进程；keeper拉起全新进程后会先核对"
            "真实委托、成交和持仓，再恢复调度，禁止盲目重发。"
        ),
        clear_qmt_cache=False,
    )


def _qmt_reset() -> None:
    """通过唯一执行线程断开并清除持久连接。"""

    service = _get_qmt_execution_service()
    service.call_function(_qmt_reset_raw, operation="reset_qmt_connection", timeout=30)


def _qmt_reset_raw() -> None:
    """只允许唯一执行线程调用的原始连接清理。"""
    global _qmt_adapter
    if _qmt_adapter is not None:
        try:
            _qmt_adapter.disconnect()
        except Exception:
            pass  # 可以忽略：断线时断开连接常会失败，连接对象随后丢弃重建
        _qmt_adapter = None


CAPACITY_WALL_STATE = PROJECT_ROOT / "data" / "state" / "capacity_wall_milestone.json"


def _check_capacity_wall_milestone(total_asset: float, config: dict, log: Any) -> None:
    """容量墙里程碑提醒：82.5%目标仓位下账户总资产约达到1700万时推送。

    背景:历史两年全链重放(docs/capital_roadmap_2026.md)的80%口径阈值为1750万；
    当前82.5%目标仓位达到同一1400万容量约需1700万，之后容量渐进摊薄。
    达标时提醒用户启动扩容研究：
    用届时积累的实盘逐片执行数据,扩大票域开发大容量新腿。
    每周最多提醒一次,直到用户处理(关闭=config.live_trade.capacity_wall_alert_enabled=false)。
    """
    try:
        lt = config.get("live_trade", {})
        if not bool(lt.get("capacity_wall_alert_enabled", True)):
            return
        threshold = float(lt.get("capacity_wall_alert_threshold", 17_000_000))
        if total_asset < threshold:
            return
        today_s = today_beijing().strftime("%Y%m%d")
        state = {}
        if CAPACITY_WALL_STATE.exists():
            state = json.loads(CAPACITY_WALL_STATE.read_text(encoding="utf-8"))
        last = str(state.get("last_alert", ""))
        if last and (datetime.datetime.strptime(today_s, "%Y%m%d")
                     - datetime.datetime.strptime(last, "%Y%m%d")).days < 7:
            return
        mkdir_p(CAPACITY_WALL_STATE.parent)
        CAPACITY_WALL_STATE.write_text(
            json.dumps({"last_alert": today_s, "total_asset": total_asset}), encoding="utf-8")
        log.warning("🏁 [容量墙里程碑] 总资产%.0f万 ≥ %.0f万,当前策略进入容量摊薄区,推送提醒。",
                    total_asset / 1e4, threshold / 1e4)
        _notify("system_error", "🏁 里程碑:账户达到当前容量墙",
                f"总资产{total_asset / 1e4:.0f}万。当前小票策略池的满速复利区已跑完"
                f"(82.5%目标仓位开始超过中位票2%容量),此后每笔收益率将随资金增长摊薄"
                f"(渐近线约1亿,见docs/capital_roadmap_2026.md)。是时候启动新阶段研究:"
                f"用这段时间积累的实盘逐片执行数据,研究容量墙之后的扩容策略"
                f"(扩大票域/大容量新腿)。请找Claude开工。",
                level="timeSensitive")
    except Exception as e:
        log.warning("容量墙里程碑检查异常(不影响交易):%s", e)


def _strategy_only_market_value(
    broker_positions: Any,
    *,
    exclude_due_today: bool = False,
    as_of_date: str = "",
    family: str | None = None,
) -> float:
    """QMT持仓中仅属于本策略系统的市值(2026-07-17 用户拍板:打新中签的
    债券/股票、人工买入等一切非策略持仓,不进入交易系统逻辑)。

    识别规则:QMT持仓代码能匹配本地策略持仓(open/sell_pending)=策略仓;
    其余(申购中签/人工/理财)=外部持仓,不计入仓位口径,防止其市值挤占
    total_position_cap 的策略额度。本地持仓文件只由策略成交写入(record_buy),
    因此"在本地"是策略身份的唯一判据，覆盖 ACDEL 全部当前腿；历史B腿只用于退役识别。

    exclude_due_today/as_of_date 仅为兼容旧调用签名保留，现已不再排除到期仓。
    新口径要求旧策略仓实际清空前禁止新开仓，因此今日到期、逾期、sell_pending
    与普通未到期仓都必须计入策略市值。
    """
    # 近期被自动幽灵清理误关、但券商又返回的持仓仍属于策略仓。
    # 在恢复写盘完成前也要计入市值，不允许短暂降级成外部仓而腾出仓位。
    # family="ACE"/"M" 只计对应策略族（ACE与M资金分开计算仓位上限）。
    local_aliases = _strategy_identity_aliases(include_recent_ghost=True, family=family)
    total = 0.0
    for p in (broker_positions or []):
        if int(getattr(p, "volume", 0) or 0) <= 0:
            continue
        if any(al in local_aliases for al in _ts_code_aliases(getattr(p, "ts_code", ""))):
            total += float(getattr(p, "market_value", 0.0) or 0.0)
    return total


def _broker_has_strategy_position(broker_positions: Any) -> bool:
    """券商持仓中是否有匹配本地策略持仓(positions.json)的票。

    2026-07-23 用户重申:打新中签的债券/股票、人工买入等外部持仓,绝不占用
    交易逻辑。_last_account_has_position 原从券商全部持仓(含转债)设,会让
    "账户只剩一个打新转债"被误判为"有持仓"→影响轮询/盯盘判定。改用策略口径:
    仅当券商持有能匹配本地 open/sell_pending 记录的票才算 True。
    """
    local_aliases = _strategy_identity_aliases(include_recent_ghost=True)
    if not local_aliases:
        return False
    for p in (broker_positions or []):
        if int(getattr(p, "volume", 0) or 0) <= 0:
            continue
        if any(al in local_aliases for al in _ts_code_aliases(getattr(p, "ts_code", ""))):
            return True
    return False


def _broker_has_preexisting_strategy_position(
    broker_positions: Any,
    as_of_date: str = "",
    family: str | None = "ACE",
) -> bool:
    """券商是否仍实际持有“本次开仓日前已存在”的系统策略仓。

    本地 open/sell_pending 记录提供策略身份，券商 volume>0 提供实际持仓事实；
    两者必须同时匹配才阻断，因此打新中签、债券和人工仓不会误触发。
    buy_date 等于检查日的记录是当日本次集合竞价/POV部分成交，允许继续完成同一笔；
    buy_date 早于检查日或缺失的记录都视为旧仓，含今日到期、逾期和待卖状态。
    """
    check_date = as_of_date or today_beijing().strftime("%Y%m%d")
    # 最终买入门禁不能只信status=open。若QMT瞬时空快照曾将真实仓误标closed，
    # 近期自动误清记录仍是策略身份证据；只要券商又返回该票，就必须阻断新仓。
    # 2026-11-12 ACE+M：ACE的串行单仓门禁只看A/C/E/D旧仓，M持仓不阻断ACE；
    # M自己的门禁传 family="M"。
    preexisting_aliases = _strategy_identity_aliases(
        include_recent_ghost=True,
        preexisting_on=check_date,
        family=family,
    )
    if not preexisting_aliases:
        return False
    return any(
        int(getattr(position, "volume", 0) or 0) > 0
        and any(
            alias in preexisting_aliases
            for alias in _ts_code_aliases(getattr(position, "ts_code", ""))
        )
        for position in (broker_positions or [])
    )


def _account_status_pre_close_map(
    ts_codes: list[str],
    as_of_date: datetime.date | None = None,
) -> dict[str, float]:
    """从本地日线取得昨收，只用于账户状态展示。

    账户心跳的连接结论只能来自账户与持仓查询。旧实现为了展示“今日涨跌幅”，
    每轮又调用一次同步 ``get_full_tick``；该非必要行情调用一旦卡住，会污染唯一
    QMT执行通道并触发daemon重启。这里改读收盘流水线已有的日线文件：当日文件
    已落地时读取 ``pre_close``，盘中尚无当日文件时读取上一交易日 ``close``。
    文件缺失或格式异常只省略今日涨跌幅，不影响账户验证、风控或交易任务。
    """
    requested = {str(code).strip().upper() for code in ts_codes if str(code).strip()}
    if not requested:
        return {}

    trade_date = as_of_date or today_beijing()
    daily_dir = PROJECT_ROOT / "data" / "raw" / "daily"
    current_path = daily_dir / f"{trade_date:%Y%m%d}.csv"
    if current_path.exists():
        source_path = current_path
        price_column = "pre_close"
    else:
        previous_date = prev_n_trade_days(trade_date, 1)
        source_path = daily_dir / f"{previous_date:%Y%m%d}.csv"
        price_column = "close"

    try:
        mtime_ns = source_path.stat().st_mtime_ns
        cache_hit = (
            _ACCOUNT_STATUS_DAILY_CACHE.get("path") == str(source_path)
            and _ACCOUNT_STATUS_DAILY_CACHE.get("mtime_ns") == mtime_ns
            and _ACCOUNT_STATUS_DAILY_CACHE.get("price_column") == price_column
        )
        if cache_hit:
            all_prices = dict(_ACCOUNT_STATUS_DAILY_CACHE.get("prices") or {})
        else:
            all_prices: dict[str, float] = {}
            with source_path.open("r", encoding="utf-8-sig", newline="") as file:
                for row in csv.DictReader(file):
                    code = str(row.get("ts_code", "")).strip().upper()
                    if not code:
                        continue
                    try:
                        price = float(row.get(price_column, 0.0) or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if price > 0:
                        all_prices[code] = price
            _ACCOUNT_STATUS_DAILY_CACHE.update(
                {
                    "path": str(source_path),
                    "mtime_ns": mtime_ns,
                    "price_column": price_column,
                    "prices": all_prices,
                }
            )
        return {code: all_prices[code] for code in requested if code in all_prices}
    except (OSError, csv.Error):
        return {}


def _print_account_status(log: Any) -> None:
    """账户信息轮询（后台线程）：复用持久连接，查询无需重新握手。
    只有 query_account/query_positions 成功返回，才算账户连接已验证可用。
    关键交易窗口账户不可用时立刻重连和告警；非交易时段只做低频恢复尝试。"""
    global _qmt_reconnect_count, _qmt_transport_busy_count
    global _qmt_last_verified_at, _last_account_has_position
    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                  and config.get("broker", {}).get("enabled"))
        if not qmt_on:
            write_broker_health("disabled")
            log.info("✅ [账户] %s | 程序正常 | QMT未启用", now_str)
            return
        broker_cfg = config.get("broker", {})
    except Exception as e:
        write_broker_health("unavailable", error=f"读取配置失败：{e}")
        log.error("❌ 读取配置失败：%s", e)
        return

    # 平仓窗口(14:54:30~14:58)心跳静默：不与主平仓、撤单和竞价兜底抢QMT锁。
    _t = now_beijing().time()
    if datetime.time(14, 54, 30) <= _t < datetime.time(14, 58):
        return
    account = positions = None
    with _qmt_lock:
        had_persistent_adapter = _qmt_adapter is not None
        try:
            adapter = _qmt_get(broker_cfg)
            account, positions = _qmt_query_account_positions(adapter)
            _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
            if _qmt_reconnect_count > 0:
                log.info("✅ QMT连接已恢复（第%d次心跳失败后恢复）", _qmt_reconnect_count)
                # 已推过内置桥接失联告警时，统一由锁外的成对恢复通知发送，避免重复推送。
                if qmt_is_critical_window() and not _qmt_inner_outage.get("alerted"):
                    _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
            _qmt_transport_busy_count = 0
        except BrokerSnapshotInconsistentError as snapshot_err:
            # 账户/持仓/委托查询已经有返回，这是账务语义门失败，
            # 不是连接断开。原会话必须保留，否则会把可用QMT主动打成掉线。
            _qmt_reconnect_count = 0
            _qmt_transport_busy_count = 0
            _record_broker_snapshot_inconsistent(
                snapshot_err,
                log,
                context="账户心跳",
            )
            return
        except QMTReconnectBackoffError as backoff_err:
            if _record_qmt_inner_heartbeat_stale(backoff_err, log):
                return
            write_broker_health(
                "unavailable",
                error=backoff_err,
                failure_count=_qmt_reconnect_count,
            )
            log.info("QMT连接故障仍在统一退避窗口，本轮不新建会话：%s", backoff_err)
            if qmt_is_critical_window():
                _request_qmt_reconnect_stalled_recovery(backoff_err)
            return
        except Exception as first_err:
            if _is_qmt_inner_heartbeat_stale(first_err):
                _qmt_reconnect_count += 1
                # 仅丢弃外部适配器引用，使模型恢复后能重新握手；不操作客户端。
                _qmt_reset()
                _record_qmt_inner_heartbeat_stale(first_err, log)
                return
            if _request_qmt_resource_recovery(first_err):
                return
            if _is_qmt_inner_file_transport_busy(first_err):
                # 文件通道本轮确实不可用，所以保持 fail-closed；但内置模型、账户
                # 和柜台会话并未断开，不能调用 _qmt_reset 主动拆掉持久适配器。
                _qmt_transport_busy_count += 1
                write_broker_health(
                    "transport_busy",
                    error=first_err,
                    failure_count=_qmt_transport_busy_count,
                )
                log.warning(
                    "QMT内置文件通道短暂被占用（连续%d次），本轮账户派生动作已阻断；"
                    "保留现有QMT会话，下轮继续验证：%s",
                    _qmt_transport_busy_count,
                    first_err,
                )
                if _qmt_transport_busy_count >= 3 and qmt_is_critical_window():
                    _notify_once_per(
                        "qmt_inner_transport_busy",
                        300,
                        "⚠️ QMT本机通道持续被占用",
                        "QMT账户仍保持登录，但本机文件通道连续无法读写。"
                        "系统已阻断本轮开平仓派生动作并保留原会话，请检查Windows文件占用。",
                        level="timeSensitive",
                    )
                return
            _qmt_transport_busy_count = 0
            _qmt_reset()
            _qmt_reconnect_count += 1
            write_broker_health(
                "unavailable",
                error=first_err,
                failure_count=_qmt_reconnect_count,
            )
            critical_window = qmt_is_critical_window()
            last_ok = _qmt_last_verified_at or "本轮启动后尚未验证成功"
            if not critical_window and _qmt_reconnect_count < 3:
                log.info(
                    "QMT账户连接状态=未验证/不可用（非交易时段第%d/3次，最后验证成功=%s；暂不推送断连，稍后低频重试）：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )
                return

            if critical_window:
                log.warning(
                    "⚠️ QMT账户连接状态=不可用（第%d次，最后验证成功=%s），立刻重连：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )
            else:
                log.info(
                    "QMT账户连接状态=未验证/不可用（非交易时段连续%d次，最后验证成功=%s），开始后台静默重连：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )

            # 关键窗口刚断连时告警；非关键时段只写日志，避免周末/夜间刷通知。
            if critical_window and _qmt_reconnect_count == 1:
                _notify("connection", "🔌 账户断连", "QMT连接断开，正在自动重连，请关注。",
                        level="critical", call=False)
            if not had_persistent_adapter:
                # 本轮第一次动作已经是真实connect并且失败，_qmt_connect已设置
                # 统一退避。旧代码紧接着再调用一次_qmt_get，只会100%被自己刚
                # 设置的退避门拒绝，也让“第3轮完整扫描”永远没有机会成为下一次
                # 真实连接。等待下一轮；_qmt_get_raw会在第3次真实建连自动升级。
                if critical_window:
                    _request_qmt_reconnect_stalled_recovery(first_err)
                return
            try:
                if critical_window:
                    log.info("QMT自动重连开始（第%d次）", _qmt_reconnect_count)
                    # 前两轮只复用最近成功session；第3轮才唯一一次
                    # 扫备用path/session。之后回到首选session并服从进程级退避，
                    # 避免一次故障每15秒新建十个XtQuantTrader。
                    allow_full_scan = _qmt_reconnect_count == 3
                else:
                    allow_full_scan = _qmt_reconnect_count == 3
                    scan_desc = "完整扫描备用session/path" if allow_full_scan else "首选session"
                    log.info(
                        "QMT非交易时段后台静默重连开始（第%d次，%s）",
                        _qmt_reconnect_count,
                        scan_desc,
                    )
                adapter = _qmt_get(broker_cfg, allow_full_scan=allow_full_scan)
                account, positions = _qmt_query_account_positions(adapter)
                _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
                log.info("✅ QMT重连成功（第%d次恢复）", _qmt_reconnect_count)
                if critical_window and not _qmt_inner_outage.get("alerted"):
                    _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
            except BrokerSnapshotInconsistentError as snapshot_err:
                # connect/query已经恢复，只是快照语义不自洽；结束连接重试计数。
                _qmt_reconnect_count = 0
                _record_broker_snapshot_inconsistent(
                    snapshot_err,
                    log,
                    context="连接恢复后快照复核",
                )
                return
            except QMTReconnectBackoffError as backoff_err:
                write_broker_health(
                    "unavailable",
                    error=backoff_err,
                    failure_count=_qmt_reconnect_count,
                )
                log.info("QMT统一退避门拒绝本轮重复建连：%s", backoff_err)
                if critical_window:
                    _request_qmt_reconnect_stalled_recovery(backoff_err)
                return
            except Exception as retry_err:
                if _request_qmt_resource_recovery(retry_err):
                    return
                write_broker_health(
                    "unavailable",
                    error=retry_err,
                    failure_count=_qmt_reconnect_count,
                )
                if critical_window:
                    log.warning("⚠️ QMT重连失败（第%d次），等待下次重试：%s",
                                _qmt_reconnect_count, retry_err)
                else:
                    log.info("QMT非交易时段静默重连未恢复（第%d次），下次低频重试：%s",
                             _qmt_reconnect_count, retry_err)
                # 仅关键窗口连续多次失败时告警；非交易时段不升级为持续响铃。
                if critical_window and _qmt_reconnect_count >= 3:
                    _notify("system_error", "❌ QMT持续掉线",
                            "QMT连接已连续多次重连失败，实盘下单/平仓可能受影响，请立即检查。",
                            level="critical", call=True)
                    _request_qmt_reconnect_stalled_recovery(retry_err)
                return

    # 只有账户与持仓查询都通过语义/自洽校验后，才允许用券商真实股数恢复
    # 近期被自动误清的策略身份。放在QMT锁外执行，避免持仓文件写入和通知
    # 占用唯一券商通道；恢复仍早于旧仓门禁、展示及平仓线程下一轮读取。
    try:
        restore_ghost_cleared_strategy_positions(positions, "账户心跳/重连")
    except Exception as exc:
        # 本轮不能确认策略身份就停止本轮账户派生动作；只记录并等待下一轮，
        # 不让后台线程异常退出，也不把该票降级成外部仓或放行开仓。
        log.exception("策略仓身份恢复异常，本轮停止并等待下一轮重试：%s", exc)
        return
    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    acct_id = str(account.account_id or "")
    write_broker_health("verified", account_id=acct_id)
    _notify_qmt_inner_outage_recovered(log)
    masked_acct = _mask_account(acct_id)
    total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
    _check_capacity_wall_milestone(total_asset, config, log)
    live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
    pre_close_map = _account_status_pre_close_map(
        [str(getattr(position, "ts_code", "")) for position in live_positions]
    )
    # 只认策略持仓:打新中签转债/股票等外部持仓不算"有持仓"(2026-07-23 用户重申)
    _last_account_has_position = _broker_has_strategy_position(positions)
    if live_positions:
        # 逐票同步(2026-07-23):账户非空(如有打新中签转债)时,原全账户清理不触发,
        # 手动卖出的策略持仓会成永久幽灵仓卡住开仓。此处逐票核对券商实际持仓,
        # 连续N次查无的本地持仓自动标 closed(query_positions 已成功,可安全核对)。
        try:
            reconcile_missing_local_positions(
                positions,
                "账户心跳",
                snapshot_complete=True,
            )
        except Exception as _e:
            log.error("逐票持仓同步异常(不影响交易):%s", _e)
        local_positions = load_positions()
        # 同一只票可能有多条记录：09:20竞价种子单 + 09:30起的POV补单分片。
        # 直接按alias覆盖会让后一片顶掉前一片，导致用单片买价去算整笔收益
        # （2026-08-05华之杰：200股@46.38顶掉4800股@45.47，收益-4.44% vs 实际-2.61%）。
        # 这里按票加权合并成本，其余字段沿用最早那片（买入时间、计划平仓日等）。
        merged_by_code: dict[str, dict[str, Any]] = {}
        for lp in local_positions:
            if str(lp.get("status", "")).lower() not in {"open", "sell_pending"}:
                continue
            code = str(lp.get("ts_code", ""))
            try:
                shares = int(float(lp.get("shares", 0) or 0))
                price = float(lp.get("buy_price", 0) or 0)
            except (TypeError, ValueError):
                shares, price = 0, 0.0
            order_id = str(lp.get("order_id", ""))
            if code not in merged_by_code:
                merged = dict(lp)
                merged["_total_shares"] = shares
                merged["_total_cost"] = shares * price
                merged["_slices"] = 1
                # 告警去重状态要写回全部分片，只写第一片会让其余片下次重复推送。
                merged["_slice_order_ids"] = [order_id] if order_id else []
                notified = lp.get("notified_loss_thresholds") or []
                merged["notified_loss_thresholds"] = list(notified)
                merged_by_code[code] = merged
            else:
                merged = merged_by_code[code]
                merged["_total_shares"] += shares
                merged["_total_cost"] += shares * price
                merged["_slices"] += 1
                if order_id:
                    merged["_slice_order_ids"].append(order_id)
                # 任一分片已推送过的档位都算已推送，避免合并后重复告警。
                merged["notified_loss_thresholds"] = sorted(
                    {
                        str(x)
                        for x in (
                            list(merged.get("notified_loss_thresholds") or [])
                            + list(lp.get("notified_loss_thresholds") or [])
                        )
                    },
                    key=lambda x: float(x),
                    reverse=True,
                )
        for merged in merged_by_code.values():
            if merged["_total_shares"] > 0 and merged["_total_cost"] > 0:
                merged["shares"] = merged["_total_shares"]
                merged["buy_price"] = merged["_total_cost"] / merged["_total_shares"]

        local_pos_map: dict[str, dict[str, Any]] = {}
        for code, merged in merged_by_code.items():
            for alias in _ts_code_aliases(code):
                local_pos_map[alias] = merged
        notify_cfg = config.get("notify", {}) if isinstance(config, dict) else {}
        loss_thresholds = notify_cfg.get("position_loss_alert_thresholds_pct", [-5, -10, -15, -20, -30])
        try:
            loss_thresholds = sorted({float(x) for x in loss_thresholds if float(x) < 0}, reverse=True)
        except Exception:
            loss_thresholds = [-5.0, -10.0, -15.0, -20.0, -30.0]
        positions_dirty = False
        position_metadata_updates: dict[str, list[str]] = {}
        pos_parts = []
        ext_parts = []
        for p in live_positions:
            current_price = p.market_value / p.volume
            lp = {}
            for alias in _ts_code_aliases(p.ts_code):
                if alias in local_pos_map:
                    lp = local_pos_map[alias]
                    break
            if not lp:
                # 外部持仓(打新中签/人工买入,2026-07-17 用户拍板):不参与策略
                # 逻辑,不触发浮亏告警(转债波动误报+无本地记录导致每轮重复推),
                # 单独分段展示,与策略持仓彻底隔离。
                ext_parts.append(
                    f"{p.ts_code} ×{int(p.volume)} 市值{p.market_value / 10000:.2f}万")
                continue
            strategy_leg = str(lp.get("strategy_leg", "未知") or "未知").upper()
            name_s = str(lp.get("name") or getattr(p, "name", "") or "")
            buy_price = float(lp.get("buy_price", 0) or 0)
            if buy_price <= 0:
                buy_price = float(getattr(p, "cost_price", 0.0) or 0.0)
            # 成交回报时间才是复盘开仓时点；buy_time只是本地确认/补登记时刻。
            buy_time_text = _fmt_position_time(
                lp.get("traded_at") or lp.get("buy_time") or lp.get("buy_date")
            )
            exit_time_text = _fmt_position_time(
                lp.get("planned_exit_time") or lp.get("planned_exit_date"),
                default_time="14:56",
                trim_zero_seconds=True,
            )
            time_text = ""
            if buy_time_text:
                time_text += f"开{buy_time_text} "
            if exit_time_text:
                time_text += f"～{exit_time_text}平 "

            # 今日涨跌幅只依赖本地日线昨收；账户心跳不得为展示字段同步请求QMT行情。
            pre_close = float(pre_close_map.get(str(p.ts_code).upper(), 0.0) or 0.0)
            if pre_close > 0:
                chg_pct = (current_price - pre_close) / pre_close * 100
                chg_sign = "+" if chg_pct >= 0 else ""
                chg_str = f"今日{chg_sign}{chg_pct:.2f}% "
            else:
                chg_str = ""

            if buy_price > 0:
                pnl_pct = (current_price - buy_price) / buy_price * 100
                pnl_sign = "+" if pnl_pct >= 0 else ""
                notified_thresholds = {
                    str(x)
                    for x in (lp.get("notified_loss_thresholds") or [])
                }
                for threshold in loss_thresholds:
                    threshold_key = str(int(threshold)) if float(threshold).is_integer() else str(threshold)
                    if pnl_pct <= threshold and threshold_key not in notified_thresholds:
                        level = "critical" if threshold <= -15 else "timeSensitive"
                        call = threshold <= -15
                        _notify(
                            "position_risk",
                            f"⚠️ 持仓浮亏达到{threshold_key}%",
                            (
                                f"策略={strategy_leg} {p.ts_code} {lp.get('name', '')} 当前收益{pnl_pct:.2f}% "
                                f"买入{buy_price:.2f} 现价{current_price:.2f} "
                                f"持仓{int(p.volume)}股 市值{p.market_value / 10000:.2f}万"
                            ),
                            level=level,
                            call=call,
                        )
                        notified_thresholds.add(threshold_key)
                        lp["notified_loss_thresholds"] = sorted(
                            notified_thresholds,
                            key=lambda x: float(x),
                            reverse=True,
                        )
                        # 分片持仓要把去重状态写回全部分片，否则下次仍会重复推送。
                        slice_oids = [
                            oid for oid in (lp.get("_slice_order_ids") or []) if oid
                        ] or [str(lp.get("order_id", ""))]
                        for local_oid in slice_oids:
                            if local_oid:
                                position_metadata_updates[local_oid] = list(
                                    lp["notified_loss_thresholds"]
                                )
                        positions_dirty = True
                pos_parts.append(
                    f"策略={strategy_leg} {p.ts_code} {name_s} ×{int(p.volume)}股 "
                    f"{time_text}"
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"收益{pnl_sign}{pnl_pct:.2f}% "
                    f"市值{p.market_value / 10000:.2f}万"
                )
            else:
                pos_parts.append(
                    f"策略={strategy_leg} {p.ts_code} {name_s} ×{int(p.volume)}股 "
                    f"{time_text}"
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"市值{p.market_value / 10000:.2f}万"
                )
        if positions_dirty:
            # 心跳计算期间POV/主平仓可能已减少股数或关闭持仓。只把本轮新增的
            # 告警元数据合并到最新文件，禁止用旧快照整文件覆盖真实成交回写。
            with _positions_file_lock:
                fresh_positions = load_positions()
                for fresh in fresh_positions:
                    oid = str(fresh.get("order_id", ""))
                    if oid in position_metadata_updates:
                        fresh["notified_loss_thresholds"] = position_metadata_updates[oid]
                save_positions(fresh_positions)
        ext_text = ("  | 外部持仓(打新/人工,不参与策略)：" + "  ".join(ext_parts)) if ext_parts else ""
        log.info("✅ [账户] %s 总资产%.2f万 | 持仓：%s%s",
                 masked_acct, total_asset / 10000,
                 "  ".join(pos_parts) if pos_parts else "(无策略持仓)", ext_text)
    else:
        reconcile_missing_local_positions(
            positions,
            "账户心跳",
            snapshot_complete=True,
        )
        log.info("✅ [账户] %s | 账户%s 总资产%.2f万 | 无持仓",
                 now_str, masked_acct, total_asset / 10000)


def check_qmt_connection(*, allow_full_scan: bool | None = None) -> bool:
    global _qmt_last_verified_at, _last_account_has_position, _qmt_reconnect_count

    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (config.get("broker_adapter_enabled") and config.get("qmt_enabled") and
                config.get("broker", {}).get("enabled")):
            log.info("QMT 未启用，跳过连接检查")
            return True

        # 启动门禁必须建立主进程自己的持久连接，而不是只用子进程探测。
        # 否则会出现“启动验证成功，但D监控/账户心跳第一次使用QMT又重新连接并超时”的双连接口径。
        broker_config = config.get("broker", {})
        scan_desc = "完整扫描备用path/session" if allow_full_scan else "上次成功path/session优先"
        log.info("QMT启动门禁：建立主进程持久连接并验证账户/持仓（%s；不并发抢QMT）。", scan_desc)
        with _qmt_lock:
            adapter = _qmt_get(broker_config, allow_full_scan=allow_full_scan)
            account, positions = _qmt_query_account_positions(adapter)

        adapter_config = adapter.get_serialized_attribute("config", None)
        account_id = str(
            getattr(account, "account_id", "")
            or getattr(adapter_config, "account_id", "")
        )
        available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
        _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        # 只认策略持仓:外部持仓(打新中签/人工)不算(2026-07-23 用户重申)
        _last_account_has_position = _broker_has_strategy_position(positions)
        _qmt_reconnect_count = 0
        log.info(
            "✅ QMT连接成功且账户已验证：账户 %s，可用资金 %.0f 元（主进程持久连接，path=%s session=%s）",
            _mask_account(account_id),
            available_cash,
            adapter.get_serialized_attribute("_active_qmt_path", ""),
            adapter.get_serialized_attribute("_active_session_id", ""),
        )
        write_broker_health("verified", account_id=account_id)
        return True
    except Exception as e:
        global _LAST_QMT_ERROR_TEXT
        _LAST_QMT_ERROR_TEXT = str(e)   # 供门禁判断是否为套接字资源耗尽
        inner_stale = _record_qmt_inner_heartbeat_stale(e, log)
        write_broker_health("unavailable", error=e)
        if not inner_stale:
            log.error("❌ QMT主连接验证失败：%s", e)
        try:
            with _qmt_lock:
                _qmt_reset()
        except Exception:
            pass  # 可以忽略：已在连接失败处理中，重置失败由下一轮重连处理
        # 失败连接对象可能仍被引用（xtquant 内部持有线程/套接字）。主动触发一次
        # 回收，尽量把 Python 侧能释放的释放掉；底层 C++ 资源仍依赖库自身实现。
        try:
            import gc
            gc.collect()
        except Exception:
            pass  # 可以忽略：只是尽力回收内存，失败无影响
        # 启动门禁会持续重试；外层 keeper 仅在连续不可用达到配置阈值后统一告警，
        # 避免一次瞬时握手失败就发送维护误报，也避免 daemon/keeper 双重重复通知。
        return False


def wait_for_qmt_startup_gate() -> None:
    """QMT 启动门禁：账户未验证成功前，不进入启动检查和主循环。

    这里验证的是主守护进程自己的持久 QMT 连接。
    启动成功后，账户心跳、盘前下单和D线程都复用同一个连接，避免多进程互抢QMT session。
    """
    log = logger()
    round_no = 0
    blocked_since = time.monotonic()
    try:
        startup_alert_after = max(
            30,
            int(
                load_json_config(PROJECT_ROOT / "config" / "config.json")
                .get("broker", {})
                .get("startup_outage_alert_after_sec", 120)
            ),
        )
    except Exception:
        startup_alert_after = 120
    while True:
        round_no += 1
        # 退避策略（2026-07-27 事故修复）：QMT 长时间不可用时，旧实现每 10 秒做一次
        # 全量扫描（10 个 path/session 组合），38 小时累计 13756 轮 ≈ 13.7 万次 connect，
        # 把 Windows 套接字资源耗尽（WinError 10055 系统缓冲区/队列已满），导致 QMT
        # 恢复登录后程序反而连不上，交易日 09:20/09:30 开仓窗口全部空过。
        #   前 3 轮：全量扫描 + 10 秒（覆盖"刚 stop 旧 daemon、session 被占"的正常场景）
        #   4~10 轮：只试首选 session + 30 秒（少开套接字，给终端留登录窗口）
        #   10 轮以后：只试首选 session + 300 秒（QMT 确实不在，纯等待，不再消耗资源）
        if round_no <= 3:
            allow_full_scan, wait_sec = True, 10
        elif round_no <= 10:
            allow_full_scan, wait_sec = False, 30
        else:
            allow_full_scan, wait_sec = False, 300
        log.info("QMT启动门禁：第%d轮验证账户连接（%s，失败后等%d秒），验证成功前不执行启动检查/下次任务。",
                 round_no, "全量扫描" if allow_full_scan else "仅首选session", wait_sec)
        if check_qmt_connection(allow_full_scan=allow_full_scan):
            log.info("QMT启动门禁：账户连接已验证，继续启动流程。")
            return
        write_heartbeat("qmt_blocked")
        if (not _is_qmt_inner_heartbeat_stale(_LAST_QMT_ERROR_TEXT)
                and time.monotonic() - blocked_since >= startup_alert_after):
            _notify_once_per(
                "qmt_startup_blocked",
                1800,
                "⚠️ QMT启动门禁持续阻断",
                f"账户已连续{startup_alert_after // 60}分钟以上无法完成QMT查询验证。"
                "daemon自己会持续重连，交易调度尚未启动；请检查QMT是否登录。"
                "keeper只负责进程心跳，不参与本判断。",
                level="timeSensitive",
            )
        # 资源耗尽自锁检测（2026-07-27 事故根治）：xtquant 的 XtQuantTrader 在
        # start()/connect() 失败时不会完全回收其内部套接字/线程（第三方库行为，
        # 我们已调用 stop() 但无法保证其内部释放）。一旦系统报套接字资源耗尽，
        # 继续重试既徒劳（资源没了必然再失败）又会加剧泄漏，把自己越锁越死——
        # 必须立刻停止高频重试、退到最长间隔，并强告警要求人工重启虚拟机。
        if _is_socket_resource_exhausted(_LAST_QMT_ERROR_TEXT):
            # 2026-07-27 实测：套接字资源由本进程持有，进程退出即释放（当天 stop→start
            # 未重启虚拟机即恢复）。因此不再"退到长间隔干等人工"，而是主动退出，
            # 由 keeper 拉起全新进程 = 资源清零后继续重试，实现无人值守自愈。
            log.error(
                "🛑 QMT启动门禁：检测到套接字资源耗尽（WinError 10055 等）。"
                "本进程资源已污染，继续重试无意义；主动退出，由 keeper 拉起全新进程自愈。"
            )
            _notify_once_per(
                "qmt_socket_exhausted", 1800,
                "🔄 资源耗尽，程序自动重启中",
                "QMT连接报套接字资源耗尽，daemon 主动退出并由守护脚本拉起全新进程"
                "（资源清零后继续重连）。若持续未恢复，请检查QMT是否登录；"
                "持仓到期时如程序仍未恢复，请用手机App手动平仓。",
                level="timeSensitive",
            )
            write_broker_health(
                "restarting",
                error=_LAST_QMT_ERROR_TEXT,
                failure_count=round_no,
            )
            write_heartbeat("qmt_exhausted_restarting")
            time.sleep(3)
            raise SystemExit(EXIT_CODE_SOCKET_EXHAUSTED)
        log.error("QMT启动门禁：账户连接未验证成功，%d秒后继续重试；不会进入启动检查和任务调度。"
                  "（长时间不通请检查QMT是否登录）", wait_sec)
        time.sleep(wait_sec)


def _recover_trade_execution_state_once() -> RecoveryOutcome:
    """在任何交易线程启动前，用券商真实状态恢复统一交易意图。"""

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        # 启动期尚未启动任何策略线程；所有查询又都进唯一QMT队列，
        # 因此此处取到的是一组无本程序并发下单穿插的券商事实快照。
        positions = adapter.query_positions()
        orders = adapter.query_orders()
        trades = adapter.query_trades()
        # 再读一次委托/仓位，吸收首轮查询期间可能刚到达的成交回报。
        orders = adapter.query_orders()
        # 最终持仓不能孤立查询；必须连同账户资产走统一结构/code/资产自洽认证。
        # 否则启动恢复恰逢QMT假空仓时，仍可能绕开心跳侧的完整快照门禁。
        _, positions = _qmt_query_account_positions(adapter)
    # 启动恢复门禁已经拿到两轮明确成功的券商事实快照。先恢复近期自动误清的
    # 策略仓，再做事务意图对账和持仓投影；这样后续启动平仓检查、止盈预挂、
    # 旧仓阻断读取到的都是恢复后的权威本地状态。
    restore_ghost_cleared_strategy_positions(positions, "启动交易恢复")
    outcome = TradeRecoveryCoordinator(_trade_intent_store()).recover(
        daemon_boot_id=_DAEMON_BOOT_ID,
        account_fingerprint=_exit_account_fingerprint(),
        business_date=today_beijing().strftime("%Y%m%d"),
        positions=positions or [],
        orders=orders or [],
        trades=trades or [],
    )
    if outcome.status == "PASS":
        _project_recovered_buy_intents(positions or [])
    return outcome


def _recovered_buy_exit_n_days(intent: dict[str, Any]) -> int:
    """恢复冻结退出周期；旧E竞价意图必须有原POV证据，不能套用两天默认值。"""
    metadata = dict(intent.get("metadata") or {})
    raw = metadata.get("exit_n_days")
    if raw is None and not intent.get("planned_exit_date") and str(intent.get("strategy_leg", "")).upper() == "E":
        state = _pov_load_state()
        matching = [item for item in state.get("items", [])
                    if str(state.get("date", "")) == str(intent.get("business_date", ""))
                    and str(item.get("strategy_leg", "")).upper() == "E"
                    and _normalize_ts_code(item.get("ts_code", "")) == _normalize_ts_code(intent.get("ts_code", ""))
                    and str(item.get("signal_date", "")) == str(intent.get("signal_date", ""))]
        if not matching or any(item.get("exit_n") is None for item in matching):
            raise RuntimeError("E已成意图缺少冻结退出周期，禁止按默认两天回写持仓")
        periods = {str(item["exit_n"]) for item in matching}
        if len(periods) != 1:
            raise RuntimeError("E已成意图冻结退出周期存在分歧")
        raw = next(iter(periods))
    if raw is None:
        return 2  # 有明确退出日期时不会使用此值；其他旧意图保持原恢复行为。
    value = float(raw)
    if not math.isfinite(value) or value < 1 or not value.is_integer():
        raise RuntimeError("已成意图冻结退出周期非法")
    return int(value)


def _project_recovered_buy_intents(broker_positions: Any) -> int:
    """将已由QMT确认成交的BUY意图补写到positions.json。

    这是“券商已成交、daemon在record_buy前退出”的通用恢复闭环。
    只使用意图累计成交量和券商真实持仓交叉确认；数量对不上就抛错，
    让启动恢复门禁持续阻断，不会猜测归属。
    """
    store = _trade_intent_store()
    account = _exit_account_fingerprint()
    intents = store.list_buy_intents_requiring_position_projection(
        account_fingerprint=account
    )
    if not intents:
        return 0
    broker_by_code = {
        str(item["ts_code"]): item
        for item in normalize_broker_positions(broker_positions or [])
    }
    projected_count = 0
    for intent in intents:
        intent_id = str(intent["intent_id"])
        order_id = str(intent.get("broker_order_id", "") or "")
        ts_code = _normalize_ts_code(intent.get("ts_code", ""))
        filled_qty = max(int(intent.get("filled_qty", 0) or 0), 0)
        metadata = dict(intent.get("metadata") or {})
        with _positions_file_lock:
            local_positions = load_positions()
        same_order = next(
            (
                p for p in local_positions
                if str(p.get("order_id", "")) == order_id
                or order_id in {str(item) for item in p.get("component_order_ids", [])}
            ),
            None,
        )
        already_local = max(
            int((same_order or {}).get("entry_shares", (same_order or {}).get("shares", 0)) or 0),
            0,
        )
        if already_local >= filled_qty:
            store.mark_position_projected(intent_id, filled_qty)
            projected_count += 1
            continue

        broker_position = broker_by_code.get(ts_code)
        broker_qty = max(int((broker_position or {}).get("volume", 0) or 0), 0)
        other_local_qty = sum(
            max(int(p.get("shares", 0) or 0), 0)
            for p in local_positions
            if str(p.get("status", "")).lower() in {"open", "sell_pending"}
            and _normalize_ts_code(p.get("ts_code", "")) == ts_code
            and str(p.get("order_id", "")) != order_id
        )
        attributable_qty = max(broker_qty - other_local_qty, 0)
        if attributable_qty < filled_qty:
            raise RuntimeError(
                f"已成买入意图无法唯一投影到券商持仓:"
                f"intent={intent_id} order={order_id} {ts_code} "
                f"已成={filled_qty} 券商持仓={broker_qty} 其他本地归属={other_local_qty}"
            )
        average_price = float(intent.get("avg_fill_price", 0.0) or 0.0)
        if average_price <= 0:
            average_price = float((broker_position or {}).get("cost_price", 0.0) or 0.0)
        if average_price <= 0:
            raise RuntimeError(
                f"已成买入意图缺少可验证成交价:intent={intent_id} order={order_id}"
            )
        record_buy(
            order_id=order_id,
            ts_code=ts_code,
            name=str(metadata.get("name", "") or ""),
            signal_date=str(intent.get("signal_date", "") or intent.get("business_date", "")),
            buy_date=str(intent.get("business_date", "")),
            shares=filled_qty,
            buy_price=average_price,
            strategy_leg=str(intent.get("strategy_leg", "") or "").upper(),
            exit_n_days=_recovered_buy_exit_n_days(intent),
            traded_at=str(metadata.get("traded_at", "") or ""),
            execution_channel="启动交易恢复",
            planned_order_qty=int(intent.get("target_qty", 0) or filled_qty),
            planned_exit_date_override=str(intent.get("planned_exit_date", "") or ""),
            strategy_branch_id=str(metadata.get("strategy_branch_id", "") or ""),
            condition_profile_ids=str(
                metadata.get("condition_profile_ids", "") or ""
            ),
            exit_rule=str(metadata.get("exit_rule", "") or ""),
        )
        # 必须先成功落positions.json，才允许在事务账本标记“已投影”。
        store.mark_position_projected(intent_id, filled_qty)
        projected_count += 1
        logger().warning(
            "✅ [交易恢复] 已将券商真实买入成交补写为策略持仓: %s %s %d股 order_id=%s",
            intent.get("strategy_leg", ""), ts_code, filled_qty, order_id,
        )
    return projected_count


def _validate_local_execution_state_for_startup() -> dict[str, int]:
    """交易门禁放行前完整读取全部权威本地状态。

    券商意图对账PASS只说明SQLite与QMT一致；positions、盘前待确认、退出批次、
    D接力和买入POV任一文件损坏，同样可能造成重复委托或漏平仓。因此这些状态
    必须在打印“门禁通过”和启动任何交易线程之前全部可读。
    """

    positions = load_positions()
    pending_buys = load_pending_buys()
    exit_state = _load_exit_execution_state()
    d_relay_state = _d_relay_pair_load_state()
    pov_state = _pov_load_state()
    return {
        "positions": len(positions),
        "pending_buys": len(pending_buys),
        "exit_intents": len(exit_state.get("intents", [])),
        "d_relay_active": int(bool(d_relay_state)),
        "pov_items": len(pov_state.get("items", [])),
    }


def wait_for_trade_recovery_gate() -> None:
    """恢复门禁未通过时fail-closed，不启动开仓、平仓或看门狗线程。"""

    log = logger()
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    if not (
        config.get("broker_adapter_enabled")
        and config.get("qmt_enabled")
        and config.get("broker", {}).get("enabled")
    ):
        log.info("交易恢复门禁：QMT未启用，无实盘意图需恢复。")
        return

    round_no = 0
    while True:
        round_no += 1
        try:
            outcome = _recover_trade_execution_state_once()
            if outcome.status == "PASS":
                local_state = _validate_local_execution_state_for_startup()
                log.info(
                    "✅ 交易恢复门禁通过：待恢复%d笔，已对账%d笔，"
                    "仍在券商活跃%d笔，recovery_id=%s；"
                    "本地状态校验=持仓%d/待确认买单%d/退出意图%d/"
                    "D接力%d/POV条目%d。",
                    outcome.recoverable_count,
                    outcome.recovered_count,
                    outcome.active_count,
                    outcome.recovery_id,
                    local_state["positions"],
                    local_state["pending_buys"],
                    local_state["exit_intents"],
                    local_state["d_relay_active"],
                    local_state["pov_items"],
                )
                return
            short_reasons = "；".join(
                f"{item.get('ts_code', '')}:{item.get('reason', '')}"
                for item in outcome.unresolved[:3]
            )
            log.critical(
                "🛑 交易恢复门禁阻断：%d笔意图无唯一券商证据；%s；"
                "禁止启动任何交易任务。",
                outcome.unresolved_count,
                short_reasons,
            )
            write_broker_health("trade_recovery_blocked", error=short_reasons)
            write_heartbeat("trade_recovery_blocked")
            _notify_once_per(
                "trade_recovery_blocked",
                1800,
                "🛑 交易恢复门禁已阻断",
                f"{outcome.unresolved_count}笔未终态交易意图无法与QMT真实委托/成交唯一对应。"
                "系统已停在交易线程启动前，不会盲目重发。请核对控制台恢复日志和QMT委托。",
                level="critical",
                call=True,
            )
        except Exception as exc:
            log.exception("交易恢复门禁第%d轮异常：%s", round_no, exc)
            write_broker_health("trade_recovery_error", error=exc, failure_count=round_no)
            write_heartbeat("trade_recovery_error")
            _notify_once_per(
                "trade_recovery_error",
                1800,
                "🛑 交易恢复异常",
                f"无法完成QMT仓位/委托/成交事务对账：{exc}。"
                "交易线程未启动，系统将继续重试。",
                level="critical",
                call=True,
            )
        time.sleep(30)
