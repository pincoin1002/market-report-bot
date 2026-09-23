#!/usr/bin/env python3
"""Build deterministic MarketContext from a validated Snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

from market_session import classify_tw_session, classify_us_session, report_market_date
from models import MarketContext, ProviderHealth, Snapshot, TaiexMarketSummary


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
    # The report date comes from the fetch contract, never from an older
    # serialized report or the renderer's wall clock.  A primary-market quote
    # with a mismatched trading date cannot be presented as today's quote.
    primary_market = "TW" if report_type.startswith("tw_") else "US"
    if snapshot.report_market_date:
        market_date = snapshot.report_market_date
    else:
        # Legacy fixtures lack an explicit fetch contract.  Derive their date
        # from the actual primary-market observations rather than today's clock.
        primary_dates = [q.market_date for q in snapshot.quote_observations.values()
                         if q.market == primary_market and q.quality_status == "VALID"]
        market_date = max(primary_dates) if primary_dates else report_market_date(report_type, now)
    quotes = {}
    data_quality = dict(snapshot.data_quality)
    for key, observation in snapshot.quote_observations.items():
        if (observation.market == primary_market and observation.quality_status == "VALID"
                and observation.market_date != market_date):
            observation = observation.model_copy(update={
                "quality_status": "DATE_MISMATCH",
                "quality_notes": observation.quality_notes + [
                    f"report date {market_date} differs from quote trading date {observation.market_date}"
                ],
            })
            data_quality[key] = "DATE_MISMATCH"
        quotes[key] = observation
    provider_counts: dict[str, ProviderHealth] = {}
    for obs in quotes.values():
        health = provider_counts.setdefault(obs.provider, ProviderHealth(provider=obs.provider))
        health.attempted += 1
        if obs.quality_status == "VALID":
            health.succeeded += 1
        else:
            health.failed += 1

    taiex_sum = snapshot.taiex_summary
    if taiex_sum is None and "TAIEX" in quotes and quotes["TAIEX"].quality_status == "VALID":
        q = quotes["TAIEX"]
        taiex_sum = TaiexMarketSummary(
            close=q.price,
            point_change=round(q.price - q.previous_regular_close, 2),
            change_pct=q.change_pct,
        )

    fx = quotes.get("USDTWD")
    portfolio_health = "PASS"
    if snapshot.portfolio_quote_coverage and not snapshot.portfolio_quote_coverage.is_full:
        portfolio_health = "DEGRADED"
    health = {
        "market_quotes": "PASS" if snapshot.market_context_coverage >= 1 else "DEGRADED",
        "portfolio_quotes": portfolio_health,
        "fx": "PASS" if fx and fx.quality_status == "VALID" else "DEGRADED",
        "derived_metrics": "PASS",
        "narrative_qc": "PASS",
    }
    final_status = "FULL" if all(value == "PASS" for value in health.values()) else "DEGRADED"

    return MarketContext(
        run_id=run_id or f"{report_type}:{market_date}",
        report_type=report_type,
        market_date=market_date,
        generated_at=snapshot.generated_at,
        market_session=market_session,
        quotes=quotes,
        macro_observations={k: v for k, v in quotes.items() if k in {"SPX", "NDX", "DJI", "RUT", "SOX", "VIX", "TNX", "US2Y", "DXY", "BTC", "TAIEX", "GC", "CL", "USDTWD"}},
        market_quote_coverage=snapshot.market_context_coverage,
        portfolio_quote_coverage=snapshot.portfolio_quote_coverage,
        portfolio_snapshot_id=snapshot.portfolio_snapshot_id,
        portfolio_snapshot_as_of=snapshot.portfolio_snapshot_as_of,
        portfolio_source=snapshot.portfolio_source,
        provider_health=list(provider_counts.values()),
        data_quality=data_quality,
        material_changes=[],
        missing_required_items=snapshot.missing_required_items,
        degraded_mode=degraded_mode,
        pipeline_health=health,
        final_status=final_status,
        taiex_summary=taiex_sum,
        institutional_flows=snapshot.institutional_flows,
    )
