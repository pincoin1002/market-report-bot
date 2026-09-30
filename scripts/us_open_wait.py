#!/usr/bin/env python3
"""Wait only until the explicit 09:05 New York US-open intent."""

from __future__ import annotations

import os
import time
from datetime import datetime

from market_session import NY, us_open_snapshot_contract_status, us_open_wait_seconds


def main() -> None:
    intended_market_date = os.getenv("US_OPEN_INTENDED_MARKET_DATE", "").strip() or None
    intended_market_time = os.getenv("US_OPEN_INTENDED_TIME", "").strip() or None
    now = datetime.now(tz=NY)
    status = us_open_snapshot_contract_status(
        now, intended_market_date=intended_market_date,
        intended_market_time=intended_market_time,
    )
    if status != "INTENT_PENDING":
        print(f"US Open intent wait not required: {status}")
        return
    seconds = us_open_wait_seconds(
        now, intended_market_date=intended_market_date,
        intended_market_time=intended_market_time,
    )
    # The external dispatcher triggers during the 08:00-08:59 New York staging window.
    # The maximum expected wait until canonical 09:05 NY is ~65 minutes.
    # A larger wait means malformed metadata or a stale queued event; do not keep a runner alive.
    if seconds > 70 * 60:
        raise SystemExit("US Open intent is more than 70 minutes in the future")
    print(f"Waiting {seconds}s for canonical US Open intent")
    time.sleep(seconds)


if __name__ == "__main__":
    main()
