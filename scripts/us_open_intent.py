#!/usr/bin/env python3
"""Resolve the canonical US-open intent and durable same-date report guard."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from market_session import NY, us_open_intended_datetime


ROOT = Path(__file__).parent.parent


def _set_github_value(name: str, value: str, output: bool = False) -> None:
    destination = os.getenv("GITHUB_OUTPUT" if output else "GITHUB_ENV")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def has_existing_report(market_date: str) -> bool:
    return bool(list((ROOT / "reports").glob(f"us_open_{market_date.replace('-', '')}_*.md")))


def resolve_intent(now: datetime | None = None) -> tuple[str, str, bool]:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    intended = us_open_intended_datetime(
        local,
        intended_market_date=os.getenv("US_OPEN_INTENDED_MARKET_DATE", "").strip() or None,
        intended_market_time=os.getenv("US_OPEN_INTENDED_TIME", "").strip() or None,
    )
    market_date = intended.strftime("%Y-%m-%d")
    return market_date, intended.strftime("%H:%M"), has_existing_report(market_date)


def main() -> None:
    market_date, market_time, already_reported = resolve_intent()
    _set_github_value("US_OPEN_INTENDED_MARKET_DATE", market_date)
    _set_github_value("US_OPEN_INTENDED_TIME", market_time)
    _set_github_value("already_reported", str(already_reported).lower(), output=True)
    _set_github_value("idempotency_key", f"us_open:{market_date}", output=True)
    print(f"US Open intent: us_open:{market_date}; existing report: {already_reported}")


if __name__ == "__main__":
    main()
