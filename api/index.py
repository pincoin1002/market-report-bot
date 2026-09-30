"""Vercel Python entrypoint for the protected US-open scheduler routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from scheduler.us_open_dispatch import handle_cron_request


class handler(BaseHTTPRequestHandler):
    """One Vercel-recognized entrypoint; rewrites select the UTC candidate slot."""

    def do_GET(self) -> None:  # noqa: N802 - Vercel's Python handler contract
        slot = parse_qs(urlparse(self.path).query).get("slot", [""])[0]
        if slot not in {"edt", "est"}:
            self._respond(404, {"ok": False, "status": "NOT_FOUND"})
            return
        status, payload = handle_cron_request(slot, self.headers.get("Authorization", ""))
        # This is intentionally payload-only: no authorization header or
        # dispatch credential can enter Vercel logs.  It gives operators the
        # actual invocation timestamp for future incident reconstruction.
        print(json.dumps({
            "scheduler_invoked_at": datetime.now(tz=timezone.utc).isoformat(),
            "slot": slot,
            "status": payload.get("status"),
        }))
        self._respond(status, payload)

    def _respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not copy request headers into logs.
        return
