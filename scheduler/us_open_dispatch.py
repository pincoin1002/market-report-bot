"""Vercel Cron → GitHub Actions dispatcher for the canonical US-open intent."""

from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, time as clock_time, timezone
from http.server import BaseHTTPRequestHandler
from typing import Literal

from zoneinfo import ZoneInfo

from market_session import is_nyse_trading_day


NY = ZoneInfo("America/New_York")
UTC = timezone.utc
INTENDED_HOUR = 8
STAGING_START = clock_time(8, 0)
STAGING_END = clock_time(9, 0)
REPOSITORY = "pincoin1002/market-report-bot"
WORKFLOW = "us-open.yml"
SchedulerSlot = Literal["edt", "est"]


def intended_scheduler_time(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return local.replace(hour=INTENDED_HOUR, minute=0, second=0, microsecond=0)


def slot_matches_new_york_offset(slot: SchedulerSlot, now: datetime | None = None) -> bool:
    """Whether this UTC cron slot is the current 08:55 New York occurrence."""
    expected_utc_hour = intended_scheduler_time(now).astimezone(UTC).hour
    return (slot == "edt" and expected_utc_hour == 12) or (slot == "est" and expected_utc_hour == 13)


def scheduler_decision(slot: SchedulerSlot, now: datetime | None = None) -> str:
    """Classify a cron candidate without ever creating a late normal intent."""
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    if not slot_matches_new_york_offset(slot, local):
        return "IGNORED_DST_SLOT"
    if not is_nyse_trading_day(local):
        return "MARKET_CLOSED"
    if STAGING_START <= local.time() < STAGING_END:
        return "DISPATCH"
    return "OUTSIDE_STAGING_WINDOW"


def should_dispatch(slot: SchedulerSlot, now: datetime | None = None) -> bool:
    return scheduler_decision(slot, now) == "DISPATCH"


def dispatch_payload(now: datetime | None = None, scheduler_terminal_state: str | None = None) -> dict[str, object]:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    inputs: dict[str, str] = {
        "send_telegram": "true",
        "intended_market_date": local.strftime("%Y-%m-%d"),
        "intended_market_time": "09:05",
        "trigger_source": "vercel_cron",
        "scheduler_triggered_at": local.astimezone(UTC).isoformat(),
    }
    if scheduler_terminal_state:
        inputs["scheduler_terminal_state"] = scheduler_terminal_state
    return {
        "ref": "main",
        "inputs": inputs,
    }


def dispatch_workflow(token: str, payload: dict[str, object], attempts: int = 2) -> int:
    """Dispatch once, retrying only an unaccepted transient GitHub request."""
    if not token:
        raise ValueError("GITHUB_WORKFLOW_DISPATCH_TOKEN is not configured")
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "market-report-us-open-scheduler",
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
            # Authentication/authorization failures cannot be solved by retrying.
            if 400 <= exc.code < 500:
                raise RuntimeError(f"GitHub dispatch rejected with HTTP {exc.code}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(1)
    raise RuntimeError("GitHub workflow dispatch unavailable after bounded retry") from last_error


def handle_cron_request(
    slot: SchedulerSlot,
    authorization: str,
    now: datetime | None = None,
) -> tuple[int, dict[str, object]]:
    """Return a safe HTTP result for one Vercel Cron candidate request."""
    configured_secret = os.getenv("CRON_SECRET", "")
    if not configured_secret or not hmac.compare_digest(
        authorization, f"Bearer {configured_secret}"
    ):
        return 401, {"ok": False, "status": "UNAUTHORIZED"}

    current = (now or datetime.now(tz=NY)).astimezone(NY)
    decision = scheduler_decision(slot, current)
    if decision in {"IGNORED_DST_SLOT", "MARKET_CLOSED"}:
        return 200, {
            "ok": True,
            "status": decision,
            "trigger_source": "vercel_cron",
        }
    if decision == "OUTSIDE_STAGING_WINDOW":
        try:
            dispatch_workflow(
                os.getenv("GITHUB_WORKFLOW_DISPATCH_TOKEN", ""),
                dispatch_payload(current, scheduler_terminal_state="SCHEDULER_WINDOW_EXPIRED"),
            )
        except Exception:
            return 502, {"ok": False, "status": "DISPATCH_FAILED"}
        return 202, {
            "ok": True,
            "status": "SCHEDULER_WINDOW_EXPIRED",
            "trigger_source": "vercel_cron",
        }
    try:
        dispatch_workflow(
            os.getenv("GITHUB_WORKFLOW_DISPATCH_TOKEN", ""), dispatch_payload(current)
        )
    except Exception:
        # Do not echo exception details: provider responses may include
        # sensitive request metadata. Vercel's status code remains the
        # operational signal for a failed external dispatch.
        return 502, {"ok": False, "status": "DISPATCH_FAILED"}
    return 202, {
        "ok": True,
        "status": "DISPATCHED",
        "trigger_source": "vercel_cron",
        "intended_market_date": current.strftime("%Y-%m-%d"),
        "intended_market_time": "09:05",
    }


def make_handler(slot: SchedulerSlot):
    class USOpenSchedulerHandler(BaseHTTPRequestHandler):
        def _respond(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - Vercel's Python handler contract
            status, payload = handle_cron_request(slot, self.headers.get("Authorization", ""))
            self._respond(status, payload)

        def log_message(self, _format: str, *_args: object) -> None:
            # Vercel already records request status. Never mirror headers.
            return

    return USOpenSchedulerHandler
