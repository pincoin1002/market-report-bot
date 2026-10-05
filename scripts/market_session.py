#!/usr/bin/env python3
"""Exchange session and market-date helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

import holidays

from models import Session

TPE = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")
US_OPEN_INTENDED_TIME = time(9, 5)
US_OPEN_PREMARKET_CUTOFF = time(9, 30)
TW_OPEN_CUTOFF = time(9, 0)


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


def get_previous_completed_session_date(market: str, session_date: str) -> str:
    """Return the exchange-calendar session immediately before ``session_date``.

    This is used only after a quote has already been validated against the
    current completed session.  It never substitutes a calendar day for a
    closed exchange day.
    """
    try:
        date_value = datetime.fromisoformat(session_date).date()
    except ValueError as exc:
        raise ValueError(f"invalid session date: {session_date}") from exc
    if market.upper() in ("US", "NYSE", "NASDAQ"):
        current = datetime.combine(date_value, time(16), tzinfo=NY) - timedelta(days=1)
        while not is_nyse_trading_day(current):
            current -= timedelta(days=1)
        return current.strftime("%Y-%m-%d")
    current = datetime.combine(date_value, time(13, 30), tzinfo=TPE) - timedelta(days=1)
    while not is_tw_trading_day(current):
        current -= timedelta(days=1)
    return current.strftime("%Y-%m-%d")


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
        date_str, _ = get_most_recent_completed_session("TW", as_of=now)
        return date_str
    if report_type.startswith("us_"):
        if report_type == "us_close":
            date_str, _ = get_most_recent_completed_session("US", as_of=now)
            return date_str
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


def tw_open_snapshot_contract_status(now: datetime | None = None) -> str:
    """Return whether a Taiwan-open reference snapshot may still be published.

    The Taiwan-open report is a pre-open briefing based on the latest completed
    sessions. Once TWSE regular trading begins at 09:00 Taipei time, publishing
    those prior closes as a same-day "open" report would be stale and misleading.
    """
    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    if not is_tw_trading_day(local):
        return "MARKET_CLOSED"
    if local.time() < TW_OPEN_CUTOFF:
        return "READY"
    return "INTENT_EXPIRED"


def report_market_date(report_type: str, now: datetime | None = None) -> str:
    if report_type.startswith("us_"):
        local = (now or datetime.now(tz=NY)).astimezone(NY)
        return local.strftime("%Y-%m-%d")
    local = (now or datetime.now(tz=TPE)).astimezone(TPE)
    return local.strftime("%Y-%m-%d")


def us_open_scheduled_intent_is_eligible(now: datetime | None = None) -> bool:
    """Whether a scheduled US-open identity belongs to an NYSE trading date.

    This deliberately ignores the runner's actual start time: a scheduled
    GitHub Actions event may queue well after its intended New York time.
    """
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    return is_nyse_trading_day(local)


def us_open_intended_datetime(
    now: datetime | None = None,
    intended_market_date: str | None = None,
    intended_market_time: str | None = None,
) -> datetime:
    """Build the sole valid US-open intent in New York time.

    External schedulers must state which NYSE date they intend to serve.  The
    report itself is deliberately pinned to 09:05 New York time; accepting an
    arbitrary time would allow a late trigger to relabel a different session.
    """
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    if intended_market_date:
        try:
            market_date = date.fromisoformat(intended_market_date)
        except ValueError as exc:
            raise ValueError(f"invalid US-open intended market date: {intended_market_date}") from exc
    else:
        market_date = local.date()
    if intended_market_time and intended_market_time != US_OPEN_INTENDED_TIME.strftime("%H:%M"):
        raise ValueError("US-open intended time must be 09:05 America/New_York")
    # Use the supplied local clock as the construction anchor. This keeps the
    # contract testable with a patched clock and retains New York's DST offset.
    return local.replace(
        year=market_date.year,
        month=market_date.month,
        day=market_date.day,
        hour=US_OPEN_INTENDED_TIME.hour,
        minute=US_OPEN_INTENDED_TIME.minute,
        second=0,
        microsecond=0,
    )


def us_open_snapshot_contract_status(
    now: datetime | None = None,
    intended_market_date: str | None = None,
    intended_market_time: str | None = None,
) -> str:
    """Return the current state of the pinned US-open snapshot contract.

    ``INTENT_PENDING`` is only valid before the explicit 09:05 NY intent and
    lets an external dispatcher start the GitHub job a few minutes early.  It
    never permits fetching a pre-09:05 snapshot.
    """
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    intended = us_open_intended_datetime(
        local, intended_market_date=intended_market_date,
        intended_market_time=intended_market_time,
    )
    if not is_nyse_trading_day(intended):
        return "MARKET_CLOSED"
    if local.date() != intended.date():
        return "INTENT_EXPIRED"
    if local < intended:
        return "INTENT_PENDING"
    if local.time() < US_OPEN_PREMARKET_CUTOFF:
        return "READY"
    return "INTENT_EXPIRED"


def us_open_wait_seconds(
    now: datetime | None = None,
    intended_market_date: str | None = None,
    intended_market_time: str | None = None,
) -> int:
    """Seconds to wait until the pinned 09:05 intent, or zero when not pending."""
    local = (now or datetime.now(tz=NY)).astimezone(NY)
    intended = us_open_intended_datetime(
        local, intended_market_date=intended_market_date,
        intended_market_time=intended_market_time,
    )
    return max(0, int((intended - local).total_seconds()))


def us_open_should_run(now: datetime | None = None) -> bool:
    """Backward-compatible scheduled identity check; independent of queue delay."""
    return us_open_scheduled_intent_is_eligible(now)


def us_open_idempotency_key(now: datetime | None = None) -> str:
    return f"us_open:{report_market_date('us_open', now)}"


def human_session_label(report_type: str, session: Session) -> str:
    if report_type in ("tw_close", "us_close") and session == "REGULAR":
        return "正式收盤"
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
