#!/usr/bin/env python3
"""Refresh the private portfolio event cache outside market-report delivery."""

from __future__ import annotations

import json
import os
from collections import Counter

from portfolio_context import load_authoritative_portfolio
from portfolio_events import fetch_portfolio_events


def main() -> int:
    portfolio = load_authoritative_portfolio()
    total = len(portfolio.positions)
    if total <= 0:
        print(json.dumps({"status": "NO_PORTFOLIO"}))
        return 2

    facts, upcoming = fetch_portfolio_events(
        portfolio=portfolio,
        model=(os.getenv("REPORT_MODEL") or "gemini-2.5-flash").strip(),
        network_scope="all",
    )

    instrument_ids = {f.instrument_id for f in facts}
    checked_ids = {
        f.instrument_id
        for f in facts
        if f.event_status in ("EVENT_CHECKED_NO_MATERIAL_CHANGE", "EVENT_MATERIAL_FOUND")
    }
    failed_ids = {f.instrument_id for f in facts if f.event_status == "EVENT_CHECK_FAILED"}
    unchecked_ids = {f.instrument_id for f in facts if f.event_status == "EVENT_UNCHECKED"}
    material_ids = {f.instrument_id for f in facts if f.event_status == "EVENT_MATERIAL_FOUND"}
    status_counts = Counter(f.event_status for f in facts)

    # Aggregate-only log output. Never emit private tickers or event text.
    print(json.dumps({
        "status": "OK" if not failed_ids and len(checked_ids) == total else "DEGRADED",
        "portfolio_positions": total,
        "event_fact_instruments": len(instrument_ids),
        "event_checked_instruments": len(checked_ids),
        "event_failed_instruments": len(failed_ids),
        "event_unchecked_instruments": len(unchecked_ids),
        "material_event_instruments": len(material_ids),
        "upcoming_event_count": len(upcoming),
        "status_counts": dict(status_counts),
    }, sort_keys=True))

    # Search grounding is an optional/degraded dependency. A provider outage
    # is reported through aggregate status and downstream event coverage, but it
    # must not fail the background workflow or any market-report delivery.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
