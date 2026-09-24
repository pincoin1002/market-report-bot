#!/usr/bin/env python3
"""TW-close market-aware quote policy.

A Taiwan close report is generated before the U.S. regular session opens.  The
Taiwan leg therefore needs the just-completed TW regular close, while U.S.
instruments need the most recent completed U.S. regular close.  Treating one
session label as authoritative for every market caused same-day U.S. premarket
bars to shadow the valid prior U.S. close and fail portfolio coverage.
"""

from __future__ import annotations

from models import InstrumentSpec, QuoteObservation, Session
import providers


def fetch_tw_close_observations(
    specs: list[InstrumentSpec],
    expected_session: Session,
    expected_dates: dict[str, str] | None = None,
) -> tuple[dict[str, QuoteObservation], dict[str, str]]:
    """Fetch TW-close observations with an explicit cross-market session split.

    U.S. instruments are always resolved against PREVIOUS_CLOSE for a TW close
    report.  The expected U.S. trading date remains calendar-aware through the
    existing ``expected_dates`` contract, so weekends and NYSE holidays still
    resolve to the actual most recent completed session.  Other markets retain
    the caller's session policy (REGULAR for the TW close workflow).
    """
    us_specs = [spec for spec in specs if spec.market == "US"]
    other_specs = [spec for spec in specs if spec.market != "US"]

    observations: dict[str, QuoteObservation] = {}
    sources: dict[str, str] = {}

    if other_specs:
        other_obs, other_sources = providers.fetch_session_observations(
            other_specs,
            expected_session,
            expected_dates=expected_dates,
        )
        observations.update(other_obs)
        sources.update(other_sources)

    if us_specs:
        us_obs, us_sources = providers.fetch_session_observations(
            us_specs,
            "PREVIOUS_CLOSE",
            expected_dates=expected_dates,
        )
        observations.update(us_obs)
        sources.update(us_sources)

    return observations, sources
