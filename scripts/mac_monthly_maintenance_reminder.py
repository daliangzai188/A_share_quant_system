#!/usr/bin/env python3
"""每月例行维护提醒(Mac launchd,每周六 10:00 触发,仅本月第一个周六实际推送)。

维护内容(人工约5分钟,推送里附清单):
  1. Windows 设置→Windows更新→检查并安装全部更新→立即重启
  2. 重启后:登录 QMT(独立交易)
  3. PowerShell: cd C:\\A_System; py -3.11 start_windows.py
  4. 确认手机收到"程序与账户已恢复正常"Bark 推送

方案甲复核提醒(同一个周六再推一条):
  1/4/7/10 月第一个周六提醒发"甲·季度体检"；1 月改为"甲·年度复核"(年度复核已含当季体检)。
launchd: com.asystem.maintenance (StartCalendarInterval 周六10:00)
"""
from __future__ import annotations

import datetime
import urllib.parse
import urllib.request

ENVF = "/Users/user/Desktop/A_System/.env"
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


def _push(url: str, title: str, body: str) -> None:
    try:
        urllib.request.urlopen(
            f"{url}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}?group=A股实盘",
            timeout=20,
        )
    except Exception:
        pass


def main() -> None:
    today = datetime.date.today()
    if today.weekday() != 5 or today.day > 7:
        return  # 只在每月第一个周六执行
    url = ""
    with open(ENVF) as f:
        for line in f:
            if line.strip().startswith("BARK_URL="):
                url = line.strip().split("=", 1)[1].strip().strip('"').rstrip("/")
    if not url:
        return
    _push(
        url,
        "🔧 每月例行维护日",
        "5分钟维护清单:①Windows更新→装完→立即重启 "
        "②重启后登录QMT(独立交易) "
        "③PowerShell: cd C:\\A_System 后 py -3.11 start_windows.py "
        "④确认收到'程序与账户已恢复正常'推送。完成后本月不用再管。",
    )
    review = plan_jia_review_message(today)
    if review:
        _push(url, *review)


if __name__ == "__main__":
    main()
