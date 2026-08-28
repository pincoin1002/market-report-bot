#!/usr/bin/env python3
"""Build deterministic MarketContext from a validated Snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

from market_session import classify_tw_session, classify_us_session, report_market_date
from models import MarketContext, ProviderHealth, Snapshot


def build_market_context(snapshot: Snapshot, report_type: str,
                         run_id: str | None = None,
                         now: datetime | None = None,
                         degraded_mode: bool = False) -> MarketContext:
    observed_sessions = {q.session for q in snapshot.quote_observations.values()}
    if report_type in ("us_close", "tw_close") and "REGULAR" in observed_sessions:
        market_session = "REGULAR"
    elif report_type == "tw_open" and "PREVIOUS_CLOSE" in observed_sessions:
        market_session = "PREVIOUS_CLOSE"
    elif report_type.startswith("us_"):
        market_session = classify_us_session(now, extended_quote_available=any(
            q.session in ("PREMARKET", "AFTER_HOURS") for q in snapshot.quote_observations.values()
        ))
    else:
        market_session = classify_tw_session(now, report_type)
    quotes = dict(snapshot.quote_observations)
    provider_counts: dict[str, ProviderHealth] = {}
    for obs in quotes.values():
        health = provider_counts.setdefault(obs.provider, ProviderHealth(provider=obs.provider))
        health.attempted += 1
        if obs.quality_status == "VALID":
            health.succeeded += 1
        else:
            health.failed += 1
    return MarketContext(
        run_id=run_id or f"{report_type}:{report_market_date(report_type, now)}",
        report_type=report_type,
        market_date=report_market_date(report_type, now),
        generated_at=snapshot.generated_at,
        market_session=market_session,
        quotes=quotes,
        macro_observations={k: v for k, v in quotes.items() if k in {"SPX", "NDX", "DJI", "RUT", "SOX", "VIX", "TNX", "US2Y", "DXY", "BTC", "TAIEX", "GC", "CL", "USDTWD"}},
        market_quote_coverage=snapshot.market_context_coverage,
        portfolio_quote_coverage=snapshot.portfolio_quote_coverage,
        provider_health=list(provider_counts.values()),
        data_quality=snapshot.data_quality,
        material_changes=[],
        missing_required_items=snapshot.missing_required_items,
        degraded_mode=degraded_mode,
    )
