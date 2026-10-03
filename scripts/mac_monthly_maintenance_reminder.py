#!/usr/bin/env python3
"""每月例行维护提醒(Mac launchd,每周六 10:00 触发,仅本月第一个周六实际推送)。

维护内容(人工约5分钟,推送里附清单):
  1. Windows 设置→Windows更新→检查并安装全部更新→立即重启
  2. 重启后:登录 QMT(独立交易)
  3. PowerShell: cd C:\\A_System; py -3.11 start_windows.py
  4. 确认手机收到"程序与账户已恢复正常"Bark 推送

方案甲复核提醒(同一个周六再推一条):
  1/4/7/10 月第一个周六提醒发"甲·季度体检"；1 月改为"甲·年度复核"(年度复核已含当季体检)。

运行位置：必须由 scripts/install_mac_maintenance_reminder.py 安装到
~/Library/Application Support/A_System/ 后由 launchd 执行。macOS 不允许 launchd 后台进程
读取 Desktop：旧任务直接执行桌面上的本脚本，每次都以退出码2失败，2026-10-03 第一次季度
体检提醒因此没有发出。Bark 地址存在同目录权限0600的配置里，不再读桌面上的 .env。
同一天只推送一次；推送失败以非零退出码结束，并写入安装目录的日志。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "A_System"
CONFIG_PATH = SUPPORT_DIR / "maintenance_reminder.json"
STATE_PATH = SUPPORT_DIR / "maintenance_reminder_state.json"
REVIEW_MONTHS = {1, 4, 7, 10}


def plan_jia_review_message(today: datetime.date) -> tuple[str, str] | None:
    """返回当天该推送的方案甲复核提醒(标题, 正文)；不是复核月份返回None。"""

    if today.month not in REVIEW_MONTHS:
        return None
    stamp = today.strftime("%Y-%m-%d")
    if today.month == 1:
        return (
            "📋 方案甲年度复核日",
            f"请在Claude发送：甲·年度复核，截至 {stamp}。"
            "会用最新数据延长样本外、按写死的硬门槛判断是否换规则；只出报告，你确认才落地。",
        )
    return (
        "📋 方案甲季度体检日",
        f"请在Claude发送：甲·季度体检，截至 {stamp}。"
        "会做实盘与回测逐笔对账、核对停手是否按规则触发、检查数据质量；不改规则。",
    )


def maintenance_messages(today: datetime.date) -> list[tuple[str, str]]:
    """本月第一个周六返回要推送的全部消息；其他日子返回空列表。"""

    if today.weekday() != 5 or today.day > 7:
        return []
    messages = [(
        "🔧 每月例行维护日",
        "5分钟维护清单:①Windows更新→装完→立即重启 "
        "②重启后登录QMT(独立交易) "
        "③PowerShell: cd C:\\A_System 后 py -3.11 start_windows.py "
        "④确认收到'程序与账户已恢复正常'推送。完成后本月不用再管。",
    )]
    review = plan_jia_review_message(today)
    if review:
        messages.append(review)
    return messages


def load_bark_url(config_path: Path = CONFIG_PATH) -> str:
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(value.get("bark_url", "") if isinstance(value, dict) else "").strip().rstrip("/")


def _push(url: str, title: str, body: str) -> bool:
    try:
        with urllib.request.urlopen(
            f"{url}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}?group=A股实盘",
            timeout=20,
        ) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - 失败写日志并以非零退出
        print(f"推送失败：{title}：{exc}", file=sys.stderr)
        return False


def _already_sent(today: datetime.date, state_path: Path) -> bool:
    try:
        return json.loads(state_path.read_text(encoding="utf-8")).get("last_sent_date") == today.isoformat()
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def run(today: datetime.date, *, config_path: Path = CONFIG_PATH, state_path: Path = STATE_PATH,
        push=_push) -> int:
    messages = maintenance_messages(today)
    if not messages:
        return 0
    if _already_sent(today, state_path):
        print(f"{today} 的提醒已推送过，跳过")
        return 0
    url = load_bark_url(config_path)
    if not url:
        print(f"缺少Bark配置：{config_path}（请重新运行安装脚本）", file=sys.stderr)
        return 1
    sent = [title for title, body in messages if push(url, title, body)]
    if len(sent) != len(messages):
        return 1
    state_path.write_text(
        json.dumps({"last_sent_date": today.isoformat(), "titles": sent}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{today} 已推送：{'、'.join(sent)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每月维护与方案甲复核提醒")
    parser.add_argument("--check", action="store_true", help="只检查运行环境和配置，不推送")
    args = parser.parse_args(argv)
    if args.check:
        if not load_bark_url():
            print(f"MAINTENANCE_REMINDER_NOT_READY 缺少Bark配置：{CONFIG_PATH}")
            return 1
        print(f"MAINTENANCE_REMINDER_READY {Path(__file__).resolve()}")
        return 0
    return run(datetime.date.today())


if __name__ == "__main__":
    raise SystemExit(main())
