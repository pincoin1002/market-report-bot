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


def is_tw_trading_day(dt: datetime) -> bool:
    local = dt.astimezone(TPE)
    return local.weekday() < 5 and local.date() not in holidays.Taiwan()


def get_most_recent_completed_session(market: str, as_of: datetime | None = None) -> tuple[str, Session]:
    """Return (trading_date_str, session) for the most recent completed regular session before as_of.
    
    Market calendar aware (weekends, exchange holidays, exchange closing times, DST, after-midnight TPE).
    """
    if market.upper() in ("US", "NYSE", "NASDAQ"):
        local = (as_of or datetime.now(tz=NY)).astimezone(NY)
        # US regular session ends at 16:00 NY
        if is_nyse_trading_day(local) and local.time() >= time(16, 0):
            return local.strftime("%Y-%m-%d"), "REGULAR"
        cur = (local - timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
        while not is_nyse_trading_day(cur):
            cur -= timedelta(days=1)
        return cur.strftime("%Y-%m-%d"), "REGULAR"

    # Default to TW market
    local = (as_of or datetime.now(tz=TPE)).astimezone(TPE)
    # TW regular session ends at 13:30 TPE
    if is_tw_trading_day(local) and local.time() >= time(13, 30):
        return local.strftime("%Y-%m-%d"), "REGULAR"
    cur = (local - timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
    while not is_tw_trading_day(cur):
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d"), "REGULAR"


def get_target_market_date(report_type: str, market: str, now: datetime | None = None) -> str:
    """Deterministic expected market trading date for a given report type and market."""
    if report_type == "tw_open":
        # For TW open, US quotes must be the completed US close; TW quotes must be the previous TW close
        date_str, _ = get_most_recent_completed_session(market, as_of=now)
        return date_str
    if report_type == "tw_close":
        if market.upper() in ("US", "NYSE", "NASDAQ"):
            date_str, _ = get_most_recent_completed_session("US", as_of=now)
            return date_str
        local = (now or datetime.now(tz=TPE)).astimezone(TPE)
        return local.strftime("%Y-%m-%d")
    if report_type.startswith("us_"):
        if report_type == "us_close":
            local = (now or datetime.now(tz=NY)).astimezone(NY)
            return local.strftime("%Y-%m-%d")
        # us_open
        if market.upper() == "TW":
            date_str, _ = get_most_recent_completed_session("TW", as_of=now)
            return date_str
        local = (now or datetime.now(tz=NY)).astimezone(NY)
        return local.strftime("%Y-%m-%d")
    date_str, _ = get_most_recent_completed_session(market, as_of=now)
    return date_str


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
