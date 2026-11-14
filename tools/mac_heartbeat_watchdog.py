#!/usr/bin/env python3
"""Mac 端独立心跳看门狗:daemon 之死的最后一道告警。

背景(2026-07-13 事故):Windows Update 周日 06:27 自动重启虚拟机,daemon
被杀后无人知晓,躺尸 28 小时,错过周一金利华电买入窗口。Bark 告警由
daemon 自己发送,daemon 死了告警也死了——需要一个独立于虚拟机的哨兵。

原理:daemon 每分钟写心跳日志,Syncthing 秒级同步到 Mac。本脚本由 Mac 的
launchd 每 5 分钟运行一次,检查日志文件的修改时间:
  - 落后超过 15 分钟 → 调 Bark 告警(读 .env 的 BARK_URL);
  - 每小时最多告警一次(防轰炸);恢复后自动复位。
虚拟机整机死亡/Syncthing 断链/daemon 崩溃,任何一环断了都会触发。
launchd 配置: ~/Library/LaunchAgents/com.asystem.watchdog.plist
"""
import os
import time
import urllib.parse
import urllib.request

LOG = "/Users/user/Desktop/A_System/logs/trading_daemon.log"
ENVF = "/Users/user/Desktop/A_System/.env"
STATE = os.path.expanduser("~/.asystem_watchdog_last_alert")
STALE_SEC = 15 * 60      # 心跳落后阈值
ALERT_GAP = 60 * 60      # 重复告警间隔


def bark_url() -> str:
    with open(ENVF) as f:
        for line in f:
            if line.strip().startswith("BARK_URL="):
                return line.strip().split("=", 1)[1].strip().strip('"').rstrip("/")
    return ""


def main() -> None:
    now = time.time()
    try:
        mtime = os.path.getmtime(LOG)
    except OSError:
        mtime = 0
    age = now - mtime
    if age <= STALE_SEC:
        # 心跳正常;清除告警状态使下次故障能立即告警
        if os.path.exists(STATE):
            os.remove(STATE)
        return
    last = 0.0
    try:
        with open(STATE) as f:
            last = float(f.read().strip())
    except (OSError, ValueError):
        pass
    if now - last < ALERT_GAP:
        return
    url = bark_url()
    if not url:
        return
    mins = int(age // 60)
    title = urllib.parse.quote("🛑 交易系统心跳丢失")
    body = urllib.parse.quote(
        f"daemon日志已 {mins} 分钟无更新。虚拟机可能被重启/关机/断同步,"
        f"请立即检查:①虚拟机开机状态 ②QMT登录 ③daemon运行。"
    )
    try:
        urllib.request.urlopen(
            f"{url}/{title}/{body}?group=A股实盘&sound=alarm&level=critical",
            timeout=20,
        )
        with open(STATE, "w") as f:
            f.write(str(now))
    except Exception:
        pass  # 网络失败下个周期重试


if __name__ == "__main__":
    main()
