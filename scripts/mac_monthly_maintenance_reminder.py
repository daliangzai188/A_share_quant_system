#!/usr/bin/env python3
"""每月例行维护提醒(Mac launchd,每周六 10:00 触发,仅本月第一个周六实际推送)。

维护内容(人工约5分钟,推送里附清单):
  1. Windows 设置→Windows更新→检查并安装全部更新→立即重启
  2. 重启后:登录 QMT(独立交易)
  3. PowerShell: cd C:\\A_System; py -3.11 start_windows.py
  4. 确认手机收到"账户连接成功"Bark 推送
launchd: com.asystem.maintenance (StartCalendarInterval 周六10:00)
"""
import datetime
import urllib.parse
import urllib.request

ENVF = "/Users/user/Desktop/A_System/.env"


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
    title = urllib.parse.quote("🔧 每月例行维护日")
    body = urllib.parse.quote(
        "5分钟维护清单:①Windows更新→装完→立即重启 "
        "②重启后登录QMT(独立交易) "
        "③PowerShell: cd C:\\A_System 后 py -3.11 start_windows.py "
        "④确认收到'账户连接成功'推送。完成后本月不用再管。"
    )
    try:
        urllib.request.urlopen(f"{url}/{title}/{body}?group=A股实盘", timeout=20)
    except Exception:
        pass


if __name__ == "__main__":
    main()
