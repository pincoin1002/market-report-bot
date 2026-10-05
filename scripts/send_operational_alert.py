#!/usr/bin/env python3
"""Send idempotent operational failure alerts for report workflows."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ALERTS_DIR = ROOT / "reports" / ".alerts"
NY = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")

MESSAGES = {
    "TW_OPEN_INTENT_EXPIRED": (
        "⚠️ 台股開盤戰報未送出\n"
        "原因：排程在 09:00 後才進入執行，已超過開盤前有效時間。\n"
        "未使用前一交易日收盤資料冒充今日開盤。"
    ),
    "SCHEDULER_WINDOW_EXPIRED": (
        "⚠️ 美股開盤日報未送出\n"
        "原因：排程觸發過晚，已超過有效盤前時間。\n"
        "正常市場內容未生成，避免使用過期資料。"
    ),
    "INTENT_EXPIRED": (
        "⚠️ 美股開盤日報未送出\n"
        "原因：排程觸發過晚，已超過有效盤前時間。\n"
        "正常市場內容未生成，避免使用過期資料。"
    ),
    "DISPATCH_FAILED": (
        "⚠️ 美股開盤日報未送出\n"
        "原因：排程觸發 GitHub Actions 失敗。\n"
        "正常市場內容未生成，請檢查排程器狀態。"
    ),
    "VALIDATION_BLOCKED": (
        "⚠️ 美股開盤日報未送出\n"
        "原因：資料驗證未通過。\n"
        "正常市場內容未送出，請查看執行紀錄。"
    ),
    "DELIVERY_FAILED": (
        "⚠️ 美股開盤日報未送出\n"
        "原因：報表傳送失敗。\n"
        "請查看執行紀錄。"
    ),
}


def get_market_date(report_type: str) -> str:
    tz = NY if report_type.startswith("us_") else TPE
    env_date = os.getenv(f"{report_type.upper()}_INTENDED_MARKET_DATE", "").strip()
    if env_date:
        return env_date
    return datetime.now(tz=tz).strftime("%Y-%m-%d")


def already_alerted(report_type: str, market_date: str) -> bool:
    marker = ALERTS_DIR / f"{report_type}_{market_date}.sent"
    return marker.exists()


def record_alert(report_type: str, market_date: str, state: str) -> Path:
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    marker = ALERTS_DIR / f"{report_type}_{market_date}.sent"
    marker.write_text(
        json.dumps({
            "report_type": report_type,
            "market_date": market_date,
            "terminal_state": state,
            "sent_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return marker


def send_telegram_alert(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram credentials missing; skipping operational alert send")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError("Telegram API rejected operational alert")
        return True


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: send_operational_alert.py <report_type> <terminal_state> [reason]", file=sys.stderr)
        sys.exit(1)
    report_type = sys.argv[1]
    terminal_state = sys.argv[2]
    market_date = get_market_date(report_type)

    if already_alerted(report_type, market_date):
        print(f"Operational alert already sent for {report_type}:{market_date}; skipping duplicate.")
        sys.exit(0)

    text = MESSAGES.get(terminal_state, f"⚠️ {report_type} 執行未完成：{terminal_state}")
    try:
        sent = send_telegram_alert(text)
        record_alert(report_type, market_date, terminal_state)
        print(f"Operational alert processed (sent={sent}) for {report_type}:{market_date} state={terminal_state}")
    except Exception as e:
        print(f"Failed to send operational alert: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
