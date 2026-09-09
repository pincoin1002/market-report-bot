#!/usr/bin/env python3
"""Deterministic quote quality checks."""

from __future__ import annotations

import math
from datetime import datetime

from models import InstrumentSpec, QualityStatus, QuoteObservation, Session


SEVERITY: dict[QualityStatus, int] = {
    "UNAVAILABLE": 6,
    "CONFLICTING": 5,
    "SUSPECT": 4,
    "DATE_MISMATCH": 3,
    "DATA_BLOCKED": 3,
    "STALE": 2,
    "VALID": 1,
}


def _worse_quality(current: QualityStatus, candidate: QualityStatus) -> QualityStatus:
    return candidate if SEVERITY.get(candidate, 0) > SEVERITY.get(current, 0) else current


def is_scale_error(price: float, previous_close: float) -> bool:
    if price <= 0 or previous_close <= 0:
        return False
    ratio = price / previous_close
    return any(abs(ratio - scale) / scale <= 0.03 for scale in (10, 100, 0.1, 0.01))


def validate_observation(obs: QuoteObservation, spec: InstrumentSpec,
                         expected_session: Session | None = None,
                         secondary: QuoteObservation | None = None,
                         expected_date: str | None = None) -> QuoteObservation:
    quality: QualityStatus = "VALID"
    notes = list(obs.quality_notes)

    if obs.canonical_symbol != spec.canonical_symbol or obs.instrument_id != spec.canonical_symbol:
        quality = _worse_quality(quality, "CONFLICTING")
        notes.append("instrument identity does not match registry")
    if obs.currency != spec.currency:
        quality = _worse_quality(quality, "CONFLICTING")
        notes.append(f"currency {obs.currency} does not match registry {spec.currency}")
    if not math.isfinite(obs.price) or obs.price <= 0:
        quality = _worse_quality(quality, "UNAVAILABLE")
        notes.append("nonpositive or non-finite price")
    if obs.session not in spec.session_support and spec.asset_type != "MACRO":
        quality = _worse_quality(quality, "SUSPECT")
        notes.append(f"session {obs.session} is not supported for instrument")
    if expected_session == "PREMARKET" and obs.session == "PREVIOUS_CLOSE":
        notes.append("premarket unavailable; explicit previous close fallback")
    if expected_session in ("PREMARKET", "AFTER_HOURS") and obs.session == "REGULAR":
        notes.append("extended-hours quote unavailable; regular quote fallback")
    if is_scale_error(obs.price, obs.previous_regular_close):
        if secondary and secondary.quality_status == "VALID":
            diff = abs(obs.price - secondary.price) / secondary.price
            if diff <= 0.02:
                notes.append("large move confirmed by secondary provider")
            else:
                quality = _worse_quality(quality, "CONFLICTING")
                notes.append("large move conflicts with secondary provider")
        elif obs.corporate_action_note:
            notes.append(f"large move explained by corporate action: {obs.corporate_action_note}")
        else:
            quality = _worse_quality(quality, "SUSPECT")
            notes.append("possible 10x/100x scale error or unconfirmed extreme move")

    # Deterministic date/session alignment verification
    if expected_date is not None:
        if obs.market_date != expected_date:
            quality = _worse_quality(quality, "DATE_MISMATCH")
            notes.append(f"quote trading date {obs.market_date} does not match expected session date {expected_date}")

    try:
        observed_date = datetime.strptime(obs.market_date[:10].replace("/", "-"), "%Y-%m-%d").date()
        age_days = (obs.retrieved_at.date() - observed_date).days
        max_age = 4 if spec.market == "US" else 3
        if age_days > max_age:
            quality = _worse_quality(quality, "STALE")
            notes.append(f"quote date older than {max_age} calendar days")
    except ValueError:
        quality = _worse_quality(quality, "SUSPECT")
        notes.append("invalid market_date")

    market = obs.market or spec.market
    return obs.model_copy(update={
        "quality_status": quality,
        "quality_notes": sorted(set(notes)),
        "market": market,
    })
