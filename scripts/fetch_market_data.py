#!/usr/bin/env python3
"""Fetch verified market prices via yfinance → data/market_snapshot.json.

Run before generate_report.py so the report has anchored, verified prices.
If this script fails, generate_report.py falls back to pure search mode.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

TPE = timezone(timedelta(hours=8))

# ── Ticker configuration ───────────────────────────────────────────────────────
# Add / remove symbols here to customize the snapshot.

TW_STOCKS: dict[str, str] = {
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2308": "台達電",
    "2382": "廣達",
    "2327": "國巨",
}

US_MARKETS: dict[str, tuple[str, str, str]] = {
    # key: (yfinance_symbol, display_name, currency_hint)
    "SPX":  ("^GSPC",    "S&P 500",      ""),
    "NDX":  ("^NDX",     "NASDAQ 100",   ""),
    "DJI":  ("^DJI",     "Dow Jones",    ""),
    "VIX":  ("^VIX",     "VIX",          ""),
    "TNX":  ("^TNX",     "US10Y Yield",  "percent"),
    "DXY":  ("DX-Y.NYB", "美元指數",     ""),
    "BTC":  ("BTC-USD",  "Bitcoin",      "USD"),
    "TSM":  ("TSM",      "台積電 ADR",   "USD"),
    "NVDA": ("NVDA",     "NVIDIA",       "USD"),
}

FOREX: dict[str, tuple[str, str, str]] = {
    "USDTWD": ("TWD=X", "USD/TWD", "TWD"),
}


# ── Core fetch ─────────────────────────────────────────────────────────────────

def _fetch_one(symbol: str) -> dict | None:
    """Return {price, prev_close, change_pct, data_date} or None on failure."""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            print(f"  [warn] {symbol}: no data returned", file=sys.stderr)
            return None
        last   = float(hist["Close"].iloc[-1])
        prev   = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
        change = (last - prev) / prev * 100 if prev != 0 else 0.0
        return {
            "price":      round(last,   4),
            "prev_close": round(prev,   4),
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
    for code, name in TW_STOCKS.items():
        data = _fetch_one(f"{code}.TW")
        if data:
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

    print(f"[fetch] building snapshot for {report_type} …")
    snapshot = build_snapshot(report_type)

    out = data_dir / "market_snapshot.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[fetch] saved → {out} "
        f"(TW:{len(snapshot['tw_stocks'])} "
        f"US:{len(snapshot['us_markets'])} "
        f"FX:{len(snapshot['forex'])})"
    )


if __name__ == "__main__":
    main()
