"""实盘告警推送（Bark）。

设计原则：
- 失败安全：推送异常绝不抛回交易主流程，最多打日志。
- 适度脱敏：账号仅显示后2位（****03）；金额用「万」单位2位小数；标的可含代码+名称。
  由调用方按此口径组织正文。
- 节流：相同(event+title+body)在 throttle_sec 内只推一次，避免刷屏。

配置：
- 开关与事件项在 config.json 的 "notify" 段。
- Bark 推送地址放 .env 的 BARK_URL（形如 https://api.day.app/<DeviceKey>）。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger

_logger = get_logger("notify")
_lock = threading.Lock()
_last_sent: dict[str, float] = {}   # key -> 上次推送的单调时钟时间


def _load_notify_config() -> dict[str, Any]:
    try:
        config = load_json_config(get_project_root() / "config" / "config.json")
        return config.get("notify", {}) or {}
    except Exception:
        return {}


def _bark_url() -> str:
    return (os.getenv("BARK_URL", "") or "").strip().rstrip("/")


def _should_throttle(key: str, throttle_sec: float) -> bool:
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and (now - last) < throttle_sec:
            return True
        _last_sent[key] = now
    return False


def notify(event: str, title: str, body: str = "", *, level: str = "active",
           call: bool = False) -> bool:
    """推送一条告警。event 用于按配置开关过滤与节流。返回是否实际推送成功。

    level: Bark 通知级别 active/timeSensitive/critical（critical 可在勿扰模式下响铃）。
    call: True 时持续响铃约30秒（警报式，用于重大错误），需 App 已开启「重要提醒」权限。
    """
    cfg = _load_notify_config()
    if not cfg.get("enabled", False):
        return False

    events = cfg.get("events", {})
    # 未显式配置的事件默认放行，避免新增告警点被静默
    if event in events and not events.get(event, True):
        return False

    if cfg.get("channel", "bark") != "bark":
        _logger.warning("notify: 暂只支持 bark 渠道，当前=%s", cfg.get("channel"))
        return False

    base = _bark_url()
    if not base:
        _logger.warning("notify: 未配置 BARK_URL（.env），跳过推送：%s", title)
        return False

    throttle_sec = float(cfg.get("throttle_sec", 300))
    # 节流键含 level/call：同一事件的"普通通知"和"警报"分别计数，互不挤占
    key = f"{event}|{title}|{body}|{level}|{int(call)}"
    if _should_throttle(key, throttle_sec):
        _logger.info("notify: 节流跳过（%.0fs 内重复）：%s", throttle_sec, title)
        return False

    safe_title = urllib.parse.quote(title, safe="")
    safe_body = urllib.parse.quote(body or " ", safe="")
    params = {
        "group": cfg.get("bark_group", "A股实盘"),
        "level": level,
    }
    sound = cfg.get("bark_sound")
    if sound:
        params["sound"] = sound
    if call:
        # 持续响铃（警报式），并按配置设置重要提醒音量
        params["call"] = "1"
        vol = cfg.get("bark_critical_volume")
        if vol is not None:
            params["volume"] = str(vol)
    query = urllib.parse.urlencode(params)
    url = f"{base}/{safe_title}/{safe_body}?{query}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
        if ok:
            _logger.info("notify: 已推送 [%s] %s", event, title)
        else:
            _logger.warning("notify: 推送返回非2xx [%s] %s", event, title)
        return ok
    except Exception as e:
        _logger.warning("notify: 推送失败 [%s] %s：%s", event, title, e)
        return False
