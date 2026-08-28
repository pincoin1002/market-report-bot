#!/usr/bin/env python3
"""Deterministic local V2 dry run.

No secrets, network, Telegram, or email. This exercises the release pipeline:
snapshot → MarketContext → structured public draft → validation → private brief.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from market_context import build_market_context
from models import QuoteObservation, Snapshot
from portfolio_context import EncryptedPortfolioProvider
from structured_reports import (
    build_action_brief, build_public_draft, render_action_brief,
    validate_action_brief, validate_public_draft,
)


def _obs(symbol: str, currency: str = "USD", session: str = "REGULAR",
         price: float = 100, prev: float = 99) -> QuoteObservation:
    now = datetime.now(tz=timezone.utc)
    return QuoteObservation(
        quote_id=f"{symbol}:dry:{session}:fixture",
        instrument_id=symbol,
        canonical_symbol=symbol,
        price=price,
        currency=currency,
        session=session,
        market_date=now.strftime("%Y-%m-%d"),
        observed_at=now,
        provider_timestamp=now,
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
    observations = {
        "NVDA": _obs("NVDA", session=session),
        "GOOG": _obs("GOOG", session=session),
        "VOO": _obs("VOO", session=session),
        "VTI": _obs("VTI", session=session),
        "DRAM": _obs("DRAM", session=session),
        "TNX": _obs("TNX", currency="percent", session=session),
    }
    return Snapshot(
        generated_at=datetime.now(tz=timezone.utc),
        report_type=report_type,
        fetch_coverage=1.0,
        market_context_coverage=1.0,
        portfolio_quote_coverage=1.0,
        quote_observations=observations,
        us_markets={},
    )


def run_one(report_type: str) -> dict:
    snapshot = _snapshot(report_type)
    context = build_market_context(snapshot, report_type, run_id=f"dry:{report_type}")
    public = build_public_draft(context, "dry-run narrative")
    public_ok, public_reason = validate_public_draft(public, context)
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
    brief = build_action_brief(context, portfolio)
    private_ok, private_reason = validate_action_brief(brief, context, portfolio)
    return {
        "report_type": report_type,
        "public_report_valid": public_ok,
        "public_reason": public_reason,
        "private_advice_valid": private_ok,
        "private_reason": private_reason,
        "rendered_public_chars": len(public.rendered_markdown),
        "rendered_private_chars": len(render_action_brief(brief)),
    }


def main() -> None:
    results = [run_one(t) for t in ("tw_open", "tw_close", "us_open", "us_close")]
    out = {"ok": all(r["public_report_valid"] and r["private_advice_valid"] for r in results),
           "results": results}
    path = Path(__file__).parent.parent / "data" / "dry_run_v2_results.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not out["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
