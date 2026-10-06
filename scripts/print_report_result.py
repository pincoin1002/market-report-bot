#!/usr/bin/env python3
"""Emit one redacted machine-readable terminal line at the end of a workflow."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def result_line(report_type: str, data_dir: Path = ROOT / "data",
                fetch_terminal_state: str = "") -> str:
    path = data_dir / "report_result.json"
    if path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
    else:
        validation = {}
        validation_path = data_dir / "validation_results.json"
        if validation_path.exists():
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        checks = ("structural_validation", "numeric_provenance", "structured", "render")
        passed = bool(validation) and all(validation.get(name, {}).get("passed") is True for name in checks)
        zone = ZoneInfo("America/New_York" if report_type.startswith("us_") else "Asia/Taipei")
        market_date = datetime.now(tz=zone).strftime("%Y%m%d")
        if fetch_terminal_state:
            terminal_state = fetch_terminal_state
            reason = fetch_terminal_state
        else:
            terminal_state = "NOT_DELIVERED" if passed else "VALIDATION_BLOCKED"
            reason = "NO_DELIVERY_REQUESTED" if passed else "VALIDATION_OR_INPUT_FAILED"
        result = {"report_type": report_type, "market_date": market_date,
                  "public_validation": "PASS" if passed else "FAIL",
                  "public_delivery": "SKIPPED", "telegram_message_ids": [],
                  "private_advice": "SKIPPED",
                  "terminal_state": terminal_state, "reason": reason}
    fields = ("report_type", "market_date", "public_validation", "public_delivery",
              "telegram_message_ids", "private_advice", "terminal_state", "reason")
    values = []
    for key in fields:
        value = result.get(key, "unknown")
        if isinstance(value, list):
            value = ",".join(str(item) for item in value) or "none"
        values.append(f"{key}={value}")
    return "REPORT_RESULT " + " ".join(values)


if __name__ == "__main__":
    print(result_line(sys.argv[1], fetch_terminal_state=sys.argv[2] if len(sys.argv) > 2 else ""))
