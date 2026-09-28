"""Vercel Cron → GitHub Actions dispatcher for the canonical US-open intent."""

from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Literal

from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
UTC = timezone.utc
INTENDED_HOUR = 8
INTENDED_MINUTE = 55
REPOSITORY = "pincoin1002/market-report-bot"
WORKFLOW = "us-open.yml"
SchedulerSlot = Literal["edt", "est"]


def intended_scheduler_time(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return local.replace(hour=INTENDED_HOUR, minute=INTENDED_MINUTE, second=0, microsecond=0)


def slot_matches_new_york_offset(slot: SchedulerSlot, now: datetime | None = None) -> bool:
    """Whether this UTC cron slot is the current 08:55 New York occurrence."""
    expected_utc_hour = intended_scheduler_time(now).astimezone(UTC).hour
    return (slot == "edt" and expected_utc_hour == 12) or (slot == "est" and expected_utc_hour == 13)


def should_dispatch(slot: SchedulerSlot, now: datetime | None = None) -> bool:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return slot_matches_new_york_offset(slot, local) and local >= intended_scheduler_time(local)


def dispatch_payload(now: datetime | None = None) -> dict[str, object]:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return {
        "ref": "main",
        "inputs": {
            "send_telegram": "true",
            "intended_market_date": local.strftime("%Y-%m-%d"),
            "intended_market_time": "09:05",
            "trigger_source": "vercel_cron",
            "scheduler_triggered_at": local.astimezone(UTC).isoformat(),
        },
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
            configured_secret = os.getenv("CRON_SECRET", "")
            authorization = self.headers.get("Authorization", "")
            if not configured_secret or not hmac.compare_digest(
                authorization, f"Bearer {configured_secret}"
            ):
                self._respond(401, {"ok": False, "status": "UNAUTHORIZED"})
                return

            now = datetime.now(tz=NY)
            if not should_dispatch(slot, now):
                self._respond(200, {
                    "ok": True,
                    "status": "IGNORED_DST_SLOT",
                    "trigger_source": "vercel_cron",
                })
                return
            try:
                dispatch_workflow(os.getenv("GITHUB_WORKFLOW_DISPATCH_TOKEN", ""), dispatch_payload(now))
            except Exception:
                # Do not echo exception details: provider responses may include
                # sensitive request metadata. Vercel's status code remains the
                # operational signal for a failed external dispatch.
                self._respond(502, {"ok": False, "status": "DISPATCH_FAILED"})
                return
            self._respond(202, {
                "ok": True,
                "status": "DISPATCHED",
                "trigger_source": "vercel_cron",
                "intended_market_date": now.strftime("%Y-%m-%d"),
                "intended_market_time": "09:05",
            })

        def log_message(self, _format: str, *_args: object) -> None:
            # Vercel already records request status. Never mirror headers.
            return

    return USOpenSchedulerHandler
