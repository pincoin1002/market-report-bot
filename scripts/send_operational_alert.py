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


def validation_alert(report_type: str, results_path: Path | None = None) -> str:
    """Translate verified validation failures without exposing report internals."""
    title = {"tw_open": "台股開盤戰報", "tw_close": "台股收盤日報",
             "us_open": "美股開盤日報", "us_close": "美股收盤日報"}.get(report_type, report_type)
    path = results_path or ROOT / "data" / "validation_results.json"
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
        errors = " ".join(str(x) for x in results.get("numeric_provenance", {}).get("errors", []))
    except (OSError, ValueError, TypeError):
        errors = ""
    if "GOOG" in errors and "GOOGL" in errors:
        reason = "報價驗證發現 GOOG / GOOGL 對應衝突。"
    elif "淨廣度" in errors or "前一交易日" in errors or "breadth" in errors.lower():
        reason = "市場廣度衍生數值未通過來源驗證。"
    elif errors:
        reason = "行情數字未通過來源驗證。"
    else:
        reason = "報告結構或資料驗證未通過。"
    return f"⚠️ {title}未送出\n原因：{reason}\n市場資料已取得，但為避免錯價，報告未送出。"


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

    text = (validation_alert(report_type) if terminal_state == "VALIDATION_BLOCKED"
            else MESSAGES.get(terminal_state, f"⚠️ {report_type} 執行未完成：{terminal_state}"))
    try:
        sent = send_telegram_alert(text)
        if sent:
            record_alert(report_type, market_date, terminal_state)
        print(f"Operational alert processed (sent={sent}) for {report_type}:{market_date} state={terminal_state}")
    except Exception as e:
        print(f"Failed to send operational alert: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
