#!/usr/bin/env python3
"""Fetch verified market prices → data/market_snapshot.json / market_context.json.

Run before generate_report.py so the report has anchored, verified prices.
If this script fails, the workflow blocks delivery rather than guessing prices.

Exit codes:
  0 = snapshot saved successfully
  1 = unexpected error
  2 = TW market closed today (holiday / weekend) → skip TW report
  3 = US market closed today (holiday / weekend) → send notice instead
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
import portfolio_store
from instrument_registry import build_universe, quote_symbol
from market_context import build_market_context
from market_session import classify_tw_session, classify_us_session
from models import NamedQuote, Snapshot
from providers import fetch_session_observations

log = logging.getLogger("fetch")

TPE = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")

# Refuse to write a snapshot anchoring the LLM to badly incomplete data;
# Missing/invalid snapshots block delivery; prices are never guessed via search.
MIN_COVERAGE = 0.70

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


def _should_skip_us_open_duplicate(report_type: str) -> bool:
    """When us_open is scheduled at both 13:00 and 14:00 UTC, only the run that
    lands in the 09:00 New York hour should continue. Manual/dispatch runs are
    always allowed."""
    if report_type != "us_open" or os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return False
    now_ny = datetime.now(tz=NY)
    if now_ny.hour == 9:
        return False
    log.info("US open schedule guard skipped duplicate/non-target run",
             extra={"ny_time": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z")})
    _set_github_output("market_closed", "false")
    _set_github_output("duplicate_skipped", "true")
    return True


# ── Core fetch ─────────────────────────────────────────────────────────────────

def build_snapshot(report_type: str) -> Snapshot:
    portfolio = portfolio_store.load_portfolio()
    universe = build_universe(portfolio)
    expected_session = _expected_session(report_type)
    observations, sources = fetch_session_observations(list(universe.values()), expected_session)
    retrieved_at = datetime.now(tz=TPE)

    snapshot = Snapshot(
        generated_at=retrieved_at,
        report_type=report_type,
        fetch_coverage=round(len(observations) / len(universe), 3),
        market_context_coverage=round(len(observations) / len(universe), 3),
        sources=sources,
    )
    portfolio_symbols = [key for key, spec in universe.items() if spec.is_portfolio_critical]
    portfolio_hits = 0
    for key, obs in observations.items():
        spec = universe[key]
        if spec.market == "TW":
            bucket = "tw_stocks"
        elif key == "USDTWD":
            bucket = "forex"
        else:
            bucket = "us_markets"
        if key in portfolio_symbols:
            portfolio_hits += 1
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

    if portfolio_symbols:
        snapshot.portfolio_quote_coverage = round(portfolio_hits / len(portfolio_symbols), 3)
    missing = [key for key in universe if key not in observations]
    if missing:
        log.warning("symbols missing after all provider tiers",
                    extra={"missing": missing})
        snapshot.missing_required_items.extend(sorted(missing))
    for key, obs in snapshot.quote_observations.items():
        if obs.quality_status != "VALID":
            snapshot.data_quality[key] = obs.quality_status
    return snapshot


def _expected_session(report_type: str) -> str:
    if report_type in ("us_close", "tw_close"):
        return "REGULAR"
    if report_type.startswith("us_"):
        return classify_us_session(extended_quote_available=True)
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

    if _should_skip_us_open_duplicate(report_type):
        log.info("EXIT 4 — US open duplicate schedule skipped")
        sys.exit(4)

    if is_tw_market_closed(report_type):
        _set_github_output("market_closed", "true")
        log.info("EXIT 2 — TW market closed, report skipped")
        sys.exit(2)

    if is_us_market_closed(report_type):
        _set_github_output("market_closed", "true")
        log.info("EXIT 3 — US market closed, sending notice")
        sys.exit(3)

    _set_github_output("market_closed", "false")
    _set_github_output("duplicate_skipped", "false")

    log.info("building snapshot", extra={"report_type": report_type})
    snapshot = build_snapshot(report_type)

    if snapshot.fetch_coverage < MIN_COVERAGE:
        log.error("fetch coverage below threshold — refusing to write snapshot",
                  extra={"coverage": snapshot.fetch_coverage, "min": MIN_COVERAGE})
        sys.exit(1)  # workflow fails; report can rerun after providers recover

    if snapshot.portfolio_quote_coverage is not None and snapshot.portfolio_quote_coverage < 1.0:
        log.warning("portfolio quote coverage below 100% — public report may proceed; private advice will block",
                    extra={"portfolio_quote_coverage": snapshot.portfolio_quote_coverage,
                           "missing": snapshot.missing_required_items})

    snapshot_path = data_dir / "market_snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    context = build_market_context(snapshot, report_type)
    (data_dir / "market_context.json").write_text(context.model_dump_json(indent=2), encoding="utf-8")
    log.info("snapshot saved", extra={
        "path": str(snapshot_path),
        "coverage": snapshot.fetch_coverage,
        "tw": len(snapshot.tw_stocks),
        "us": len(snapshot.us_markets),
        "fx": len(snapshot.forex),
    })


if __name__ == "__main__":
    main()
