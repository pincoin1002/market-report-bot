#!/usr/bin/env python3
"""Fetch verified market prices → data/market_snapshot.json / market_context.json.

Run before generate_report.py so the report has anchored, verified prices.
If this script fails, the workflow blocks delivery rather than guessing prices.

Exit codes:
  0 = snapshot saved successfully
  1 = unexpected error
  2 = TW market closed today (holiday / weekend) → skip TW report
  3 = US market closed today (holiday / weekend) → send notice instead
  5 = US-open intended premarket snapshot no longer observable → fail closed
"""

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
from google import genai
from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from logging_config import setup_logging
from instrument_registry import build_universe, quote_symbol
from market_context import build_market_context
from market_session import (
    classify_tw_session, classify_us_session, get_target_market_date,
    us_open_snapshot_contract_status,
)
from models import NamedQuote, PortfolioQuoteCoverage, PortfolioQuoteCoverageItem, Snapshot
from portfolio_context import load_authoritative_portfolio
from providers import fetch_session_observations

log = logging.getLogger("fetch")

TPE = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")

# Refuse to write a snapshot anchoring the LLM to badly incomplete data;
# Missing/invalid snapshots block delivery; prices are never guessed via search.
MIN_COVERAGE = 0.70

# These labels are observability-only.  The existing primary-market validation
# below remains the authority that decides whether a public report can render.
PUBLIC_CORE_SYMBOLS = {
    "tw_open": {"TAIEX", "2330", "2317", "2454"},
    "tw_close": {"TAIEX", "2330", "2317", "2454"},
    "us_open": {"SPX", "NDX", "DJI", "SOX", "VIX"},
    "us_close": {"SPX", "NDX", "DJI", "SOX", "VIX"},
}


def build_portfolio_quote_coverage(portfolio, observations: dict, universe: dict,
                                   as_of: datetime, expected_dates: dict[str, str]) -> PortfolioQuoteCoverage:
    """Resolve every active canonical position exactly once.

    The result is a runtime diagnostic.  It intentionally distinguishes a
    stale/invalid quote from no quote and never drops a position just because
    its ticker is absent from a provider response.
    """
    items: list[PortfolioQuoteCoverageItem] = []
    for position in portfolio.positions:
        symbol = position.ticker
        spec = universe.get(symbol)
        if spec is None or not spec.provider_symbols:
            items.append(PortfolioQuoteCoverageItem(
                position_id=position.position_id, instrument_id=position.instrument_id,
                canonical_symbol=symbol, state="UNSUPPORTED",
                reason="instrument has no configured quote provider",
            ))
            continue
        obs = observations.get(symbol)
        if obs is None:
            items.append(PortfolioQuoteCoverageItem(
                position_id=position.position_id, instrument_id=position.instrument_id,
                canonical_symbol=symbol, quote_identifier=quote_symbol(spec), state="MISSING",
                reason="provider returned no quote observation",
            ))
            continue
        expected_date = expected_dates.get(spec.market)
        date_matches = not expected_date or obs.market_date == expected_date
        state = "QUOTED" if obs.quality_status == "VALID" and date_matches else (
            "STALE" if obs.quality_status in {"STALE", "DATE_MISMATCH"} else "MISSING"
        )
        if not date_matches:
            state = "STALE"
        items.append(PortfolioQuoteCoverageItem(
            position_id=position.position_id, instrument_id=position.instrument_id,
            canonical_symbol=symbol, quote_identifier=obs.quote_id,
            state=state,
            reason="validated quote" if state == "QUOTED" else (
                f"market date {obs.market_date} differs from expected {expected_date}" if not date_matches
                else f"quote quality: {obs.quality_status}"
            ),
            provider=obs.provider, quote_timestamp=obs.provider_timestamp or obs.observed_at,
            market_date=obs.market_date,
        ))
    covered = sum(item.state == "QUOTED" for item in items)
    expected = len(items)
    ratio = round(covered / expected, 3) if expected else 1.0
    missing = [item for item in items if item.state == "MISSING"]
    stale = [item for item in items if item.state == "STALE"]
    unsupported = [item for item in items if item.state == "UNSUPPORTED"]
    status = "NOT_APPLICABLE" if not expected else ("FULL" if covered == expected else "DEGRADED")
    return PortfolioQuoteCoverage(
        expected_positions=expected, covered_positions=covered, coverage_ratio=ratio,
        as_of=as_of, status=status, items=items, missing=missing, stale=stale,
        unsupported=unsupported,
    )


def classify_missing_quote_roles(report_type: str, portfolio, universe: dict,
                                 observations: dict) -> dict[str, list[str]]:
    """Classify missing/invalid quotes without altering failure semantics."""
    missing = {
        symbol for symbol in universe
        if symbol not in observations or observations[symbol].quality_status != "VALID"
    }
    portfolio_symbols = {position.ticker for position in portfolio.positions}
    public_core = PUBLIC_CORE_SYMBOLS[report_type]
    portfolio_missing = missing & portfolio_symbols
    core_missing = missing & public_core
    optional_missing = missing - portfolio_missing - core_missing
    return {
        "portfolio_required": sorted(portfolio_missing),
        "public_market_core": sorted(core_missing),
        "optional_context": sorted(optional_missing),
    }

# ── Holiday / weekend helpers ──────────────────────────────────────────────────

@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=2, max=6),
    retry=retry_if_exception(lambda e: isinstance(e, Exception)),
)
def _call_gemini_market_check(market_name: str, today_date: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "OPEN"
    client = genai.Client(api_key=api_key)
    prompt = (
        f"今天是 {today_date}。請利用 Google 搜尋查證："
        f"今天「{market_name}」有開市交易嗎？是否因為颱風（Typhoon）、國定假日或任何其他緊急因素宣布休市（不交易）？\n"
        "請嚴格只回覆三個字：\n"
        "若確定休市，請回覆：CLOSED\n"
        "若照常交易，請回覆：OPEN\n"
        "若不確定或查無休市新聞，請回覆：OPEN\n"
        "不需要任何解釋說明。"
    )
    
    # Disable safety filters for standard market vocabulary checks
    safety_settings = [
        types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
        for c in [
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
        ]
    ]
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
            safety_settings=safety_settings,
        )
    )
    return response.text.strip().upper() if response and response.text else "OPEN"


def check_market_closed_via_gemini(market_name: str, today_date: str) -> bool:
    """Ask Gemini via Google Search if the market is closed today due to typhoon or other emergencies."""
    try:
        answer = _call_gemini_market_check(market_name, today_date)
        log.info(f"Gemini market closed check for {market_name} returned: {answer}")
        return "CLOSED" in answer
    except Exception as e:
        log.warning(f"Failed to check market status via Gemini for {market_name}: {e}")
        return False


def is_tw_market_closed(report_type: str) -> bool:
    """Return True if the Taiwan stock market is closed today (checks weekend, holidays, and typhoon days)."""
    if report_type in ("us_open", "us_close"):
        return False  # US-only reports skip TW holiday check
    today = datetime.now(tz=TPE)
    if today.weekday() >= 5:
        log.info("TW market closed (weekend)", extra={"date": today.strftime("%Y-%m-%d")})
        return True

    tw_cal = holidays.Taiwan()
    if today.date() in tw_cal:
        log.info("TW market closed (holiday)", extra={
            "date": today.strftime("%Y-%m-%d"), "holiday": tw_cal.get(today.date())})
        return True

    # Real-time validation for Typhoon days or unscheduled TWSE market closures
    today_str = today.strftime("%Y-%m-%d")
    if check_market_closed_via_gemini("台灣證券交易所 (TWSE)", today_str):
        log.info("TW market closed (detected typhoon / unscheduled closure via Gemini)", extra={"date": today_str})
        return True
    return False


def is_us_market_closed(report_type: str) -> bool:
    """Return True if the US market is closed today (NYSE calendar, New York time)."""
    if report_type in ("tw_open", "tw_close"):
        return False  # TW-only reports skip US holiday check
    today = datetime.now(tz=NY)
    if today.weekday() >= 5:
        log.info("US market closed (weekend)", extra={"date": today.strftime("%Y-%m-%d")})
        return True

    nyse_cal = holidays.NYSE()
    if today.date() in nyse_cal:
        log.info("US market closed (holiday)", extra={
            "date": today.strftime("%Y-%m-%d"), "holiday": nyse_cal.get(today.date())})
        return True

    # Real-time validation for emergency US market closures
    today_str = today.strftime("%Y-%m-%d")
    if check_market_closed_via_gemini("紐約證券交易所 (NYSE)", today_str):
        log.info("US market closed (detected unscheduled closure via Gemini)", extra={"date": today_str})
        return True
    return False


def _set_github_output(key: str, value: str) -> None:
    """Write key=value to $GITHUB_OUTPUT (no-op outside GitHub Actions)."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


# ── Core fetch ─────────────────────────────────────────────────────────────────

def build_snapshot(report_type: str) -> Snapshot:
    portfolio = load_authoritative_portfolio()
    universe = build_universe(portfolio_context=portfolio)
    expected_session = _expected_session(report_type)
    retrieved_at = datetime.now(tz=TPE)
    
    us_target_date = get_target_market_date(report_type, "US", now=retrieved_at)
    tw_target_date = get_target_market_date(report_type, "TW", now=retrieved_at)
    expected_dates = {
        "US": us_target_date,
        "TW": tw_target_date,
    }
    
    observations, sources = fetch_session_observations(
        list(universe.values()), expected_session, expected_dates=expected_dates
    )

    valid_hits = sum(1 for o in observations.values() if o.quality_status == "VALID")
    requested_coverage = round(len(observations) / len(universe), 3)
    validated_coverage = round(valid_hits / len(universe), 3)
    snapshot = Snapshot(
        generated_at=retrieved_at,
        report_type=report_type,
        report_market_date=tw_target_date if report_type.startswith("tw_") else us_target_date,
        portfolio_snapshot_id=portfolio.snapshot_id,
        portfolio_snapshot_as_of=portfolio.as_of,
        portfolio_source=portfolio.source,
        fetch_coverage=requested_coverage,
        market_context_coverage=validated_coverage,
        requested_universe_coverage=requested_coverage,
        validated_universe_coverage=validated_coverage,
        sources=sources,
    )
    for key, obs in observations.items():
        spec = universe[key]
        if spec.market == "TW":
            bucket = "tw_stocks"
        elif key == "USDTWD":
            bucket = "forex"
        else:
            bucket = "us_markets"
        # Only populate named quotes in snapshot if observation passed validation!
        # Quotes with DATE_MISMATCH, STALE, or CONFLICTING are excluded from
        # verified headline pricing tables so they cannot mislead the model.
        if obs.quality_status == "VALID":
            getattr(snapshot, bucket)[key] = NamedQuote(
                name=spec.display_name,
                currency=spec.currency,
                symbol=quote_symbol(spec),
                price=obs.price,
                prev_close=obs.previous_regular_close,
                change_pct=obs.change_pct,
                data_date=obs.market_date,
            )
        snapshot.quote_observations[key] = obs

    snapshot.portfolio_quote_coverage = build_portfolio_quote_coverage(
        portfolio, observations, universe, retrieved_at, expected_dates
    )
    missing_by_role = classify_missing_quote_roles(report_type, portfolio, universe, observations)
    if any(missing_by_role.values()):
        log.warning("quote observations missing or invalid by role", extra=missing_by_role)
        # Retain the legacy aggregate for existing consumers, while persisting
        # role-aware categories so a contextual DXY gap is never read as a
        # portfolio or public-core failure.
        snapshot.missing_required_items.extend(sorted(set().union(*map(set, missing_by_role.values()))))
        snapshot.missing_portfolio_items.extend(missing_by_role["portfolio_required"])
        snapshot.missing_core_market_items.extend(missing_by_role["public_market_core"])
        snapshot.missing_optional_context_items.extend(missing_by_role["optional_context"])
    for key, obs in snapshot.quote_observations.items():
        if obs.quality_status != "VALID":
            snapshot.data_quality[key] = obs.quality_status
    return snapshot


def _expected_session(report_type: str) -> str:
    if report_type == "tw_close":
        now = datetime.now(tz=TPE)
        target = get_target_market_date(report_type, "TW", now=now)
        return "REGULAR" if target == now.strftime("%Y-%m-%d") else "PREVIOUS_CLOSE"
    if report_type == "us_close":
        now = datetime.now(tz=NY)
        target = get_target_market_date(report_type, "US", now=now)
        return "REGULAR" if target == now.strftime("%Y-%m-%d") else "PREVIOUS_CLOSE"
    if report_type.startswith("us_"):
        # us_open is a premarket snapshot contract, not a label for whichever
        # US session happens to be live when a delayed runner starts.
        return "PREMARKET" if report_type == "us_open" else classify_us_session(extended_quote_available=True)
    return classify_tw_session(report_type=report_type)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        print("Usage: fetch_market_data.py <report_type>", file=sys.stderr)
        sys.exit(1)

    report_type = sys.argv[1]
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    if is_tw_market_closed(report_type):
        _set_github_output("market_closed", "true")
        log.info("EXIT 2 — TW market closed, report skipped")
        sys.exit(2)

    if is_us_market_closed(report_type):
        _set_github_output("market_closed", "true")
        log.info("EXIT 3 — US market closed, sending notice")
        sys.exit(3)

    if report_type == "us_open":
        contract_status = us_open_snapshot_contract_status()
        if contract_status != "READY":
            log.error("US open snapshot contract unavailable — refusing to relabel later session data", extra={
                "status": contract_status,
                "ny_time": datetime.now(tz=NY).strftime("%Y-%m-%d %H:%M:%S %Z"),
            })
            _set_github_output("market_closed", "false")
            _set_github_output("intent_unavailable", "true")
            sys.exit(5)

    _set_github_output("market_closed", "false")
    _set_github_output("intent_unavailable", "false")

    log.info("building snapshot", extra={"report_type": report_type})
    snapshot = build_snapshot(report_type)

    if snapshot.fetch_coverage < MIN_COVERAGE:
        log.error("fetch coverage below threshold — refusing to write snapshot",
                  extra={"coverage": snapshot.fetch_coverage, "min": MIN_COVERAGE})
        sys.exit(1)  # workflow fails; report can rerun after providers recover

    universe = build_universe(portfolio_context=load_authoritative_portfolio())
    primary_market = "TW" if report_type.startswith("tw_") else "US"
    primary_equities_valid = sum(
        1 for key, obs in snapshot.quote_observations.items()
        if obs.market == primary_market
        and key in universe
        and universe[key].asset_type in ("EQUITY", "ETF", "INDEX")
        and obs.quality_status == "VALID"
    )
    if primary_equities_valid < 3:
        log.error("insufficient valid primary-market equities/indices — refusing to build report",
                  extra={"report_type": report_type, "primary_market": primary_market,
                         "valid_equities": primary_equities_valid})
        sys.exit(1)

    if snapshot.portfolio_quote_coverage and not snapshot.portfolio_quote_coverage.is_full:
        log.warning("portfolio quote coverage below 100% — public report may proceed; private advice will block",
                    extra={"portfolio_quote_coverage": snapshot.portfolio_quote_coverage.coverage_ratio,
                           "status": snapshot.portfolio_quote_coverage.status,
                           "missing": [item.canonical_symbol for item in snapshot.portfolio_quote_coverage.missing]})

    snapshot_path = data_dir / "market_snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    context = build_market_context(snapshot, report_type)
    (data_dir / "market_context.json").write_text(context.model_dump_json(indent=2), encoding="utf-8")
    log.info("snapshot saved", extra={
        "path": str(snapshot_path),
        "requested_universe_coverage": snapshot.requested_universe_coverage,
        "validated_universe_coverage": snapshot.validated_universe_coverage,
        "requested_quote_count": len(universe),
        "observed_quote_count": len(snapshot.quote_observations),
        "valid_quote_count": sum(
            observation.quality_status == "VALID"
            for observation in snapshot.quote_observations.values()
        ),
        "tw": len(snapshot.tw_stocks),
        "us": len(snapshot.us_markets),
        "fx": len(snapshot.forex),
        "pipeline_status": context.final_status,
        "pipeline_health": context.pipeline_health,
    })


if __name__ == "__main__":
    main()
