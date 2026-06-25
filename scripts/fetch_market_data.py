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

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf
import holidays

TPE = timezone(timedelta(hours=8))

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

def is_tw_market_closed(report_type: str) -> bool:
    """Return True if the Taiwan stock market is closed today."""
    if report_type in ("us_open", "us_close"):
        return False  # US-only reports skip TW holiday check
    today = datetime.now(tz=TPE)
    today_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        print(f"[fetch] {today_str} is a weekend — TW market closed, skipping report")
        return True
    
    tw_cal = holidays.Taiwan()
    today_date = today.date()
    if today_date in tw_cal:
        hname = tw_cal.get(today_date)
        print(f"[fetch] {today_str} is {hname} — TW market closed, skipping report")
        return True
    return False


def is_us_market_closed(report_type: str) -> bool:
    """Return True if the US market is closed today (NYSE calendar, EST)."""
    if report_type in ("tw_open", "tw_close"):
        return False  # TW-only reports skip US holiday check
    ET = timezone(timedelta(hours=-5))  # conservative: EST year-round
    today = datetime.now(tz=ET)
    today_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        print(f"[fetch] {today_str} is a US weekend — US market closed, sending notice")
        return True
        
    nyse_cal = holidays.NYSE()
    today_date = today.date()
    if today_date in nyse_cal:
        hname = nyse_cal.get(today_date)
        print(f"[fetch] {today_str} is a US holiday ({hname}) — US market closed, sending notice")
        return True
    return False


def _set_github_output(key: str, value: str) -> None:
    """Write key=value to $GITHUB_OUTPUT (no-op outside GitHub Actions)."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


# ── Core fetch ─────────────────────────────────────────────────────────────────

def _fetch_one(symbol: str) -> dict | None:
    """Return {price, prev_close, change_pct, data_date} or None on failure."""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            print(f"  [warn] {symbol}: no data returned", file=sys.stderr)
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
        change = (last - prev) / prev * 100 if prev != 0 else 0.0
        return {
            "price":      round(last, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change, 2),
            "data_date":  hist.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as exc:
        print(f"  [warn] {symbol}: {exc}", file=sys.stderr)
        return None


def build_snapshot(report_type: str) -> dict:
    snapshot: dict = {
        "generated_at": datetime.now(tz=TPE).isoformat(),
        "report_type":  report_type,
        "tw_stocks":    {},
        "us_markets":   {},
        "forex":        {},
    }

    print("Fetching Taiwan stocks …")
    for symbol, name in TW_STOCKS.items():
        data = _fetch_one(symbol)
        if data:
            code = symbol.split(".")[0]
            snapshot["tw_stocks"][code] = {"name": name, "currency": "TWD", **data}
            print(f"  {code} {name}: {data['price']:,.1f} ({data['change_pct']:+.2f}%)")

    print("Fetching US markets …")
    for key, (symbol, name, currency) in US_MARKETS.items():
        data = _fetch_one(symbol)
        if data:
            snapshot["us_markets"][key] = {
                "name": name, "currency": currency, "symbol": symbol, **data,
            }
            val = f"{data['price']:.2f}%" if currency == "percent" else f"{data['price']:,.2f}"
            print(f"  {key} {name}: {val} ({data['change_pct']:+.2f}%)")

    print("Fetching forex …")
    for key, (symbol, name, currency) in FOREX.items():
        data = _fetch_one(symbol)
        if data:
            snapshot["forex"][key] = {
                "name": name, "currency": currency, "symbol": symbol, **data,
            }
            print(f"  {key} {name}: {data['price']:.4f} ({data['change_pct']:+.2f}%)")

    return snapshot


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: fetch_market_data.py <report_type>", file=sys.stderr)
        sys.exit(1)

    report_type = sys.argv[1]
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    if is_tw_market_closed(report_type):
        _set_github_output("market_closed", "true")
        print(f"[fetch] EXIT 2 — TW market closed, report skipped")
        sys.exit(2)

    if is_us_market_closed(report_type):
        _set_github_output("market_closed", "true")
        print(f"[fetch] EXIT 3 — US market closed, sending notice")
        sys.exit(3)

    _set_github_output("market_closed", "false")

    print(f"[fetch] building snapshot for {report_type} …")
    snapshot = build_snapshot(report_type)

    snapshot_path = data_dir / "market_snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[fetch] snapshot saved → {snapshot_path} "
        f"(TW:{len(snapshot['tw_stocks'])} "
        f"US:{len(snapshot['us_markets'])} "
        f"FX:{len(snapshot['forex'])})"
    )


if __name__ == "__main__":
    main()
