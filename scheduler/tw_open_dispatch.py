"""Vercel Cron -> GitHub Actions dispatcher for Taiwan-open briefing."""

from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

from market_session import is_tw_trading_day


TPE = ZoneInfo("Asia/Taipei")
REPOSITORY = "pincoin1002/market-report-bot"
WORKFLOW = "tw-open.yml"
STAGING_START = clock_time(7, 0)
STAGING_END = clock_time(8, 45)


def scheduler_decision(now: datetime | None = None) -> str:
    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    if not is_tw_trading_day(local):
        return "MARKET_CLOSED"
    if STAGING_START <= local.time() < STAGING_END:
        return "DISPATCH"
    return "OUTSIDE_STAGING_WINDOW"


def dispatch_payload() -> dict[str, object]:
    return {
        "ref": "main",
        "inputs": {
            "send_telegram": "true",
            "trigger_source": "vercel_cron",
        },
    }


def dispatch_workflow(token: str, payload: dict[str, object], attempts: int = 2) -> int:
    if not token:
        raise ValueError("GITHUB_WORKFLOW_DISPATCH_TOKEN is not configured")
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "market-report-tw-open-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status == 204:
                    return response.status
                raise RuntimeError(f"unexpected GitHub dispatch status: {response.status}")
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise RuntimeError(f"GitHub dispatch rejected with HTTP {exc.code}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(1)
    raise RuntimeError("GitHub workflow dispatch unavailable after bounded retry") from last_error


def send_dispatch_failure_alert() -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({
            "chat_id": chat_id,
            "text": "⚠️ 台股開盤戰報未送出\n原因：外部排程觸發 GitHub Actions 失敗。\n正常市場內容未生成，請檢查排程器狀態。",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception:
        return False


def handle_cron_request(authorization: str, now: datetime | None = None) -> tuple[int, dict[str, object]]:
    configured_secret = os.getenv("CRON_SECRET", "")
    if not configured_secret or not hmac.compare_digest(
        authorization, f"Bearer {configured_secret}"
    ):
        return 401, {"ok": False, "status": "UNAUTHORIZED"}

    decision = scheduler_decision(now)
    if decision == "MARKET_CLOSED":
        return 200, {"ok": True, "status": decision, "trigger_source": "vercel_cron"}
    if decision == "OUTSIDE_STAGING_WINDOW":
        return 409, {"ok": False, "status": decision, "trigger_source": "vercel_cron"}

    try:
        dispatch_workflow(
            os.getenv("GITHUB_WORKFLOW_DISPATCH_TOKEN", ""),
            dispatch_payload(),
        )
    except Exception:
        send_dispatch_failure_alert()
        return 502, {"ok": False, "status": "DISPATCH_FAILED"}

    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    return 202, {
        "ok": True,
        "status": "DISPATCHED",
        "trigger_source": "vercel_cron",
        "market_date": local.strftime("%Y-%m-%d"),
        "canonical_time": "07:40",
    }
