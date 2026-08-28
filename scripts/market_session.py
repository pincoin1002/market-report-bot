#!/usr/bin/env python3
"""Exchange session and market-date helpers."""

from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

import holidays

from models import Session

TPE = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")


def is_nyse_trading_day(dt: datetime) -> bool:
    local = dt.astimezone(NY)
    return local.weekday() < 5 and local.date() not in holidays.NYSE()


def classify_us_session(now: datetime | None = None,
                        extended_quote_available: bool = False) -> Session:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    if not is_nyse_trading_day(local):
        return "CLOSED_REFERENCE"

    t = local.time()
    if time(9, 30) <= t < time(16, 0):
        return "REGULAR"
    if t < time(9, 30):
        return "PREMARKET" if extended_quote_available else "CLOSED_REFERENCE"
    if time(16, 0) <= t < time(20, 0):
        return "AFTER_HOURS" if extended_quote_available else "CLOSED_REFERENCE"
    return "CLOSED_REFERENCE"


def classify_tw_session(now: datetime | None = None,
                        report_type: str | None = None) -> Session:
    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    if local.weekday() >= 5 or local.date() in holidays.Taiwan():
        return "CLOSED_REFERENCE"
    if report_type == "tw_open":
        return "PREVIOUS_CLOSE"
    if time(9, 0) <= local.time() <= time(13, 35):
        return "REGULAR"
    return "PREVIOUS_CLOSE"


def report_market_date(report_type: str, now: datetime | None = None) -> str:
    if report_type.startswith("us_"):
        local = (now or datetime.now(tz=NY)).astimezone(NY)
        return local.strftime("%Y-%m-%d")
    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    return local.strftime("%Y-%m-%d")


def us_open_should_run(now: datetime | None = None) -> bool:
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return is_nyse_trading_day(local) and local.hour == 9


def us_open_idempotency_key(now: datetime | None = None) -> str:
    return f"us_open:{report_market_date('us_open', now)}"


def human_session_label(report_type: str, session: Session) -> str:
    if report_type == "us_open" and session == "REGULAR":
        return "美股開盤後更新"
    labels = {
        "PREMARKET": "盤前",
        "REGULAR": "正式盤",
        "AFTER_HOURS": "盤後",
        "PREVIOUS_CLOSE": "前一正式收盤",
        "CLOSED_REFERENCE": "休市參考價",
    }
    return labels[session]
