#!/usr/bin/env python3
"""Write a privacy-safe terminal status artifact for US-open workflow runs."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


TERMINAL_STATES = {
    "DELIVERED",
    "MARKET_CLOSED",
    "INTENT_EXPIRED",
    "VALIDATION_BLOCKED",
    "DELIVERY_FAILED",
}


def write_status(state: str, reason: str = "") -> Path:
    if state not in TERMINAL_STATES:
        raise ValueError(f"unsupported US-open terminal state: {state}")
    root = Path(__file__).parent.parent
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    payload = {
        "report_type": "us_open",
        "terminal_state": state,
        "reason": reason or None,
        "intended_market_date": os.getenv("US_OPEN_INTENDED_MARKET_DATE", "").strip() or None,
        "intended_market_time": os.getenv("US_OPEN_INTENDED_TIME", "").strip() or "09:05",
        "trigger_source": os.getenv("US_OPEN_TRIGGER_SOURCE", "").strip() or "github_schedule",
        "scheduler_triggered_at": os.getenv("US_OPEN_SCHEDULER_TRIGGERED_AT", "").strip() or None,
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    path = data_dir / "us_open_run_status.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"US Open terminal state: {state}")
    return path


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit("Usage: us_open_run_status.py <terminal-state> [reason]")
    write_status(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "")


if __name__ == "__main__":
    main()
