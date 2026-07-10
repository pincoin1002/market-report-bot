#!/usr/bin/env python3
"""Fetch verified market prices via yfinance → data/market_snapshot.json.

Run before generate_report.py so the report has anchored, verified prices.
If this script fails, generate_report.py falls back to pure search mode.

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

import holidays
from google import genai
from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from logging_config import setup_logging
from models import NamedQuote, Snapshot
from providers import fetch_with_failover

log = logging.getLogger("fetch")

TPE = timezone(timedelta(hours=8))

# Refuse to write a snapshot anchoring the LLM to badly incomplete data;
# generate_report.py falls back to pure-search mode when no snapshot exists.
MIN_COVERAGE = 0.70

# ── Ticker configuration ───────────────────────────────────────────────────────

TW_STOCKS: dict[str, str] = {
    "2330.TW": "台積電",
    "2317.TW": "鴻海",
    "2454.TW": "聯發科",
    "2308.TW": "台達電",
    "2382.TW": "廣達",
    "2327.TW": "國巨",
    "2303.TW": "聯電",
    "3711.TW": "日月光投控",
    "2356.TW": "英業達",
    "3231.TW": "緯創",
    "2383.TW": "台光電",
    "4958.TW": "臻鼎-KY",
    "2368.TW": "金像電",
    "3017.TW": "奇鋐",
    "3324.TWO": "雙鴻",
    "2421.TW": "建準",
    "2301.TW": "光寶科",
}

US_MARKETS: dict[str, tuple[str, str, str]] = {
    # key: (yfinance_symbol, display_name, currency_hint)
    "SPX":  ("^GSPC",    "S&P 500",     ""),
    "NDX":  ("^NDX",     "NASDAQ 100",  ""),
    "DJI":  ("^DJI",     "Dow Jones",   ""),
    "RUT":  ("^RUT",     "Russell 2000", ""),
    "SOX":  ("^SOX",     "費城半導體 SOX", ""),
    "VIX":  ("^VIX",     "VIX",         ""),
    "TNX":  ("^TNX",     "US10Y Yield", "percent"),
    "US2Y": ("2YY=F", "US2Y Yield", "percent"),
    "DXY":  ("DX-Y.NYB", "美元指數",    ""),
    "BTC":  ("BTC-USD",  "Bitcoin",     "USD"),
    "TSM":  ("TSM",      "台積電 ADR",  "USD"),
    "NVDA": ("NVDA",     "NVIDIA",      "USD"),
    "TAIEX": ("^TWII",    "加權指數",     ""),
    "GC":   ("GC=F",     "黃金 GC",     "USD"),
    "CL":   ("CL=F",     "原油 WTI",    "USD"),
    "AAPL":  ("AAPL",     "Apple",       "USD"),
    "MSFT":  ("MSFT",     "Microsoft",   "USD"),
    "META":  ("META",     "Meta",        "USD"),
    "GOOGL": ("GOOGL",    "Alphabet",    "USD"),
    "AMZN":  ("AMZN",     "Amazon",      "USD"),
    "TSLA":  ("TSLA",     "Tesla",       "USD"),
    "AVGO":  ("AVGO",     "Broadcom",    "USD"),
    "AMD":   ("AMD",      "AMD",         "USD"),
    "MRVL":  ("MRVL",     "Marvell",     "USD"),
    "MU":    ("MU",       "Micron",      "USD"),
    "ARM":   ("ARM",      "ARM",         "USD"),
    "ASML":  ("ASML",     "ASML",        "USD"),
    "SMCI":  ("SMCI",     "Supermicro",  "USD"),
    "DELL":  ("DELL",     "Dell",        "USD"),
    "HPE":   ("HPE",      "HPE",         "USD"),
    "ANET":  ("ANET",     "Arista",      "USD"),
    "VRT":   ("VRT",      "Vertiv",      "USD"),
    "COHR":  ("COHR",     "Coherent",    "USD"),
    "CEG":   ("CEG",      "Constellation Energy", "USD"),
    "VST":   ("VST",      "Vistra",      "USD"),
    "ETN":   ("ETN",      "Eaton",       "USD"),
    "GEV":   ("GEV",      "GE Vernova",  "USD"),
    "PWR":   ("PWR",      "Quanta Services", "USD"),
    "OKLO":  ("OKLO",     "Oklo",        "USD"),
    "SMR":   ("SMR",      "NuScale Power", "USD"),
    "APLD":  ("APLD",     "Applied Digital", "USD"),
    "IREN":  ("IREN",     "Iris Energy", "USD"),
    "FLNC":  ("FLNC",     "Fluence Energy", "USD"),
    "SPY":   ("SPY",      "SPY ETF",     "USD"),
    "QQQ":   ("QQQ",      "QQQ ETF",     "USD"),
    "SOXX":  ("SOXX",     "SOXX ETF",    "USD"),
    "SMH":   ("SMH",      "SMH ETF",     "USD"),
    "XLK":   ("XLK",      "XLK ETF",     "USD"),
    "ARKK":  ("ARKK",     "ARKK ETF",    "USD"),
}

FOREX: dict[str, tuple[str, str, str]] = {
    "USDTWD": ("TWD=X", "USD/TWD", "TWD"),
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
    """Return True if the US market is closed today (NYSE calendar, EST)."""
    if report_type in ("tw_open", "tw_close"):
        return False  # TW-only reports skip US holiday check
    ET = timezone(timedelta(hours=-5))  # conservative: EST year-round
    today = datetime.now(tz=ET)
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
    # symbol → (bucket, snapshot key, display name, currency)
    symbol_meta: dict[str, tuple[str, str, str, str]] = {}
    for sym, name in TW_STOCKS.items():
        symbol_meta[sym] = ("tw_stocks", sym.split(".")[0], name, "TWD")
    for key, (sym, name, cur) in US_MARKETS.items():
        symbol_meta[sym] = ("us_markets", key, name, cur)
    for key, (sym, name, cur) in FOREX.items():
        symbol_meta[sym] = ("forex", key, name, cur)

    quotes, sources = fetch_with_failover(list(symbol_meta))

    snapshot = Snapshot(
        generated_at=datetime.now(tz=TPE),
        report_type=report_type,
        fetch_coverage=round(len(quotes) / len(symbol_meta), 3),
        sources=sources,
    )
    for sym, q in quotes.items():
        bucket, key, name, cur = symbol_meta[sym]
        getattr(snapshot, bucket)[key] = NamedQuote(
            name=name, currency=cur, symbol=sym, **q.model_dump())

    missing = [s for s in symbol_meta if s not in quotes]
    if missing:
        log.warning("symbols missing after all provider tiers",
                    extra={"missing": missing})
    return snapshot


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

    _set_github_output("market_closed", "false")

    log.info("building snapshot", extra={"report_type": report_type})
    snapshot = build_snapshot(report_type)

    if snapshot.fetch_coverage < MIN_COVERAGE:
        log.error("fetch coverage below threshold — refusing to write snapshot",
                  extra={"coverage": snapshot.fetch_coverage, "min": MIN_COVERAGE})
        sys.exit(1)  # workflow fails; report can rerun or go pure-search mode

    snapshot_path = data_dir / "market_snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    log.info("snapshot saved", extra={
        "path": str(snapshot_path),
        "coverage": snapshot.fetch_coverage,
        "tw": len(snapshot.tw_stocks),
        "us": len(snapshot.us_markets),
        "fx": len(snapshot.forex),
    })


if __name__ == "__main__":
    main()
