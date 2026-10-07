#!/usr/bin/env python3
"""Deterministic local V2 dry run.

No secrets, network, Telegram, or email. This exercises the release pipeline:
snapshot → MarketContext → structured public draft → validation → private brief.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_context import build_market_context
from generate_report import deliver_validated_report, public_telegram_payload
from models import PortfolioQuoteCoverage, QuoteObservation, Snapshot
from validate_report import (validate_numeric_provenance, validate_render_matches_draft,
                             validate_rendered_report_structure)
from portfolio_context import EncryptedPortfolioProvider
from structured_reports import (
    build_action_brief, build_public_draft, render_action_brief,
    validate_action_brief, validate_public_draft,
)
from portfolio_decisions import build_decision_brief, render_decision_brief, validate_decision_brief, decision_telegram_chunks
from market_session import get_target_market_date


def _obs(symbol: str, currency: str = "USD", session: str = "REGULAR",
         price: float = 100, prev: float = 99,
         now: datetime | None = None) -> QuoteObservation:
    now = now or datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    return QuoteObservation(
        quote_id=f"{symbol}:dry:{session}:fixture",
        instrument_id=symbol,
        canonical_symbol=symbol,
        price=price,
        currency=currency,
        session=session,
        market_date=now.strftime("%Y-%m-%d"),
        observed_at=now,
        provider_timestamp=now.astimezone(ZoneInfo("America/New_York")) if currency in {"USD", "percent"} else now,
        retrieved_at=now,
        provider="fixture",
        quote_type="OFFICIAL_CLOSE" if session in ("REGULAR", "PREVIOUS_CLOSE") else "TRADE",
        is_delayed=False,
        quality_status="VALID",
        previous_regular_close=prev,
        change_pct=round((price - prev) / prev * 100, 2),
    )


def _snapshot(report_type: str) -> Snapshot:
    session = "PREVIOUS_CLOSE" if report_type.endswith("open") and report_type.startswith("tw") else "REGULAR"
    tw_time = datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc)
    us_time = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
    observed = tw_time if report_type.startswith("tw") else us_time
    generated = (datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
                 if report_type == "tw_open" else datetime(2026, 10, 5, 7, 5, tzinfo=timezone.utc)
                 if report_type == "tw_close" else datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)
                 if report_type == "us_open" else datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc))
    us_date = get_target_market_date(report_type, "US", now=generated)
    observed = datetime.fromisoformat(us_date).replace(hour=13 if report_type == "us_open" else 20, minute=5 if report_type == "us_open" else 0, tzinfo=timezone.utc)
    us_session = "PREMARKET" if report_type == "us_open" else session
    observations = {
        "NVDA": _obs("NVDA", session=us_session, now=observed),
        "GOOG": _obs("GOOG", session=us_session, now=observed),
        "VOO": _obs("VOO", session=us_session, now=observed),
        "VTI": _obs("VTI", session=us_session, now=observed),
        "DRAM": _obs("DRAM", session=us_session, now=observed),
        "TNX": _obs("TNX", currency="percent", session=us_session, now=observed),
    }
    if report_type.startswith("tw"):
        observations["2330"] = _obs("2330", currency="TWD", session=session, now=tw_time)
        observations["TAIEX"] = _obs("TAIEX", currency="TWD", session=session, now=tw_time)
    fx_time = datetime.fromisoformat(get_target_market_date(report_type, "TW", now=generated)).replace(hour=5, minute=30, tzinfo=timezone.utc)
    observations["USDTWD"] = _obs("USDTWD", currency="TWD", session="PREVIOUS_CLOSE", price=31.8, prev=31.7, now=fx_time).model_copy(update={"retrieved_at": generated})
    return Snapshot(
        generated_at=generated,
        report_type=report_type,
        fetch_coverage=1.0,
        market_context_coverage=1.0,
        portfolio_quote_coverage=PortfolioQuoteCoverage(
            expected_positions=0, covered_positions=0, coverage_ratio=1.0,
            as_of=generated, status="NOT_APPLICABLE",
        ),
        quote_observations=observations,
        us_markets={},
    )


def run_one(report_type: str) -> dict:
    snapshot = _snapshot(report_type)
    context = build_market_context(snapshot, report_type, run_id=f"dry:{report_type}", now=snapshot.generated_at)
    public = build_public_draft(context, "dry-run narrative")
    public_ok, public_reason = validate_public_draft(public, context)
    numeric_ok, numeric_errors = validate_numeric_provenance(public.rendered_markdown, context)
    structure_ok, structure_errors = validate_rendered_report_structure(public.rendered_markdown, report_type)
    render_ok, render_reason = validate_render_matches_draft(public.rendered_markdown, public)
    simulated = set()
    key = f"{report_type}:{context.market_date.replace('-', '')}"
    first = deliver_validated_report(public.rendered_markdown, report_type, context, public,
                                     snapshot, "fixture", key, dry_run=True, simulated_deliveries=simulated)
    second = deliver_validated_report(public.rendered_markdown, report_type, context, public,
                                      snapshot, "fixture", key, dry_run=True, simulated_deliveries=simulated)
    delivery_ok = first["public_delivery"] == "SIMULATED" and second["public_delivery"] == "SKIPPED"
    payload_ok = bool(public_telegram_payload(public.rendered_markdown, report_type))
    provider = EncryptedPortfolioProvider()
    original = provider.load_raw
    provider.load_raw = lambda: {
        "us_positions": [
            {"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100},
            {"ticker": "GOOG", "name": "Alphabet", "shares": 1, "cost_basis": 100},
            {"ticker": "VOO", "name": "VOO", "shares": 1, "cost_basis": 100},
            {"ticker": "VTI", "name": "VTI", "shares": 1, "cost_basis": 100},
            {"ticker": "DRAM", "name": "DRAM", "shares": 1, "cost_basis": 100},
        ],
        "tw_positions": [],
    }
    try:
        portfolio = provider.load()
    finally:
        provider.load_raw = original
    brief = build_decision_brief(context, portfolio)
    private_ok, private_reason = validate_decision_brief(brief, context, portfolio)
    return {
        "report_type": report_type,
        "public_report_valid": public_ok,
        "public_reason": public_reason,
        "private_advice_valid": private_ok,
        "private_reason": private_reason,
        "numeric_provenance_valid": numeric_ok,
        "numeric_errors": numeric_errors,
        "structure_valid": structure_ok,
        "structure_errors": structure_errors,
        "render_valid": render_ok,
        "render_reason": render_reason,
        "public_delivery_simulation_valid": delivery_ok,
        "payload_valid": payload_ok,
        "telegram_messages_sent": 0,
        "rendered_public_chars": len(public.rendered_markdown),
        "rendered_private_chars": len(render_decision_brief(brief)),
        "private_decision_count": len(brief.decisions),
        "private_telegram_chunks": len(decision_telegram_chunks(brief)),
    }


def main() -> None:
    results = [run_one(t) for t in ("tw_open", "tw_close", "us_open", "us_close")]
    out = {"ok": all(r["public_report_valid"] and r["private_advice_valid"]
                     and r["numeric_provenance_valid"] and r["structure_valid"]
                     and r["render_valid"] and r["public_delivery_simulation_valid"]
                     and r["payload_valid"] and r["telegram_messages_sent"] == 0 for r in results),
           "results": results}
    path = Path(__file__).parent.parent / "data" / "dry_run_v2_results.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not out["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
