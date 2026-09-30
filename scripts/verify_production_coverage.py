#!/usr/bin/env python3
"""Run real production provider adapters across the 23 authoritative portfolio positions.
Outputs the 23-row verification matrix.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_market_data import build_portfolio_quote_coverage
from instrument_registry import resolve_instrument
from portfolio_context import load_authoritative_portfolio
from providers import fetch_session_observations


def main():
    portfolio = load_authoritative_portfolio()
    expected_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
    now = datetime(2026, 9, 29, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    specs = [resolve_instrument(p.ticker) for p in portfolio.positions]
    obs, sources = fetch_session_observations(specs, "REGULAR", expected_dates=expected_dates)
    universe = {p.ticker: resolve_instrument(p.ticker) for p in portfolio.positions}
    cov = build_portfolio_quote_coverage(portfolio, obs, universe, now, expected_dates)

    rows = []
    for p in portfolio.positions:
        spec = universe[p.ticker]
        o = obs.get(p.ticker)
        cov_item = next(it for it in cov.items if it.canonical_symbol == p.ticker)

        ticker = p.ticker
        market = spec.market
        exp_session = expected_dates.get(market, "2026-09-29")

        primary = "yfinance_batch"
        actual_source = sources.get(spec.provider_symbols.get("yfinance") or spec.canonical_symbol, "none")
        if o:
            actual_source = o.provider

        if actual_source == "yfinance_batch":
            prim_res = f"HIT ({o.price})"
            fb_prov = "TWSEProvider / YahooChart / CoinGecko / Coinbase"
            fb_res = "NOT_NEEDED"
        else:
            prim_res = "FALLBACK_TRIGGERED"
            fb_prov = actual_source
            fb_res = f"HIT ({o.price})" if o else "MISS"

        acc_session = f"{o.session} ({o.market_date})" if o else "NONE"
        acc_price = str(o.price) if o else "NONE"
        status = "PASS" if cov_item.state == "QUOTED" else "FAIL"
        fail_reason = "" if status == "PASS" else cov_item.reason

        rows.append({
            "ticker": ticker,
            "market": market,
            "expected_session": exp_session,
            "primary_provider": primary,
            "primary_result": prim_res,
            "fallback_provider": fb_prov,
            "fallback_result": fb_res,
            "accepted_quote_session": acc_session,
            "accepted_price": acc_price,
            "status": status,
            "failure_reason": fail_reason,
        })

    print("| ticker | market | expected valuation session | primary provider | primary result | fallback provider | fallback result | accepted quote session | accepted price | PASS/FAIL | failure reason |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['ticker']} | {r['market']} | {r['expected_session']} | {r['primary_provider']} | {r['primary_result']} | {r['fallback_provider']} | {r['fallback_result']} | {r['accepted_quote_session']} | {r['accepted_price']} | {r['status']} | {r['failure_reason']} |")

    print("\nSUMMARY:")
    print(f"Total positions: {len(rows)}, Covered: {cov.covered_positions}/{cov.expected_positions}, Status: {cov.status}")


if __name__ == "__main__":
    main()
