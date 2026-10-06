"""Deterministic event eligibility at consumption time; never performs I/O."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


def event_data(event) -> dict:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in dict(event).items()}


def event_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def event_eligibility(event, now: datetime) -> str:
    data = event_data(event)
    now = event_time(now)
    status = data.get("event_status")
    if status in {"EVENT_CHECK_FAILED", "EVENT_UNCHECKED"}:
        return "FAILED" if status == "EVENT_CHECK_FAILED" else "UNCHECKED"
    checked = event_time(data.get("checked_at"))
    if not checked or not timedelta(0) <= now - checked <= timedelta(hours=24):
        return "STALE_CHECK"
    is_upcoming = bool(data.get("is_upcoming"))
    material = status == "EVENT_MATERIAL_FOUND" or data.get("status") in {"WATCH", "ACTION_REVIEW"} or data.get("material")
    # A genuine no-event check is distinct from a LOW-severity dated article.
    if not material and not is_upcoming and not data.get("title") and not data.get("event_date"):
        return "CURRENT_CHECK" if status == "EVENT_CHECKED_NO_MATERIAL_CHANGE" else "UNCHECKED"
    if is_upcoming:
        date = event_time(data.get("event_date") or data.get("date"))
        if not date or not 0 <= (date.date() - now.date()).days <= 7:
            return "OUTSIDE_UPCOMING_WINDOW"
    source = urlparse(data.get("source_url") or "")
    if source.scheme != "https" or not source.netloc or not data.get("source_name"):
        return "UNVERIFIED_SOURCE"
    # Search redirect URLs plus model-reported dates are not publication evidence.
    if not data.get("publication_date_verified"):
        return "UNVERIFIED_EVENT_DATE"
    published = event_time(data.get("source_published_at"))
    if not published:
        return "UNVERIFIED_EVENT_DATE"
    if is_upcoming:
        return "UPCOMING" if published <= now else "UNVERIFIED_EVENT_DATE"
    if not timedelta(0) <= now - published <= timedelta(hours=48):
        return "STALE_EVENT"
    return "RECENT_MATERIAL" if material and str(data.get("severity", "LOW")).upper() != "LOW" else "CURRENT_CHECK"
