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

TPE = timezone(timedelta(hours=8))

# ── Taiwan public holidays (YYYY-MM-DD, TSE trading holidays only) ─────────────
# Update this list each year.
TW_HOLIDAYS: set[str] = {
    # 2026
    "2026-01-01",  # 元旦
    "2026-01-27",  # 春節前
    "2026-01-28",  # 春節
    "2026-01-29",  # 春節
    "2026-01-30",  # 春節
    "2026-02-02",  # 春節補假
    "2026-02-27",  # 和平紀念日補假
    "2026-02-28",  # 和平紀念日
    "2026-04-03",  # 兒童節補假
    "2026-04-04",  # 兒童節 / 清明
    "2026-05-01",  # 勞動節
    "2026-06-19",  # 端午節
    "2026-09-18",  # 中秋節補假
    "2026-09-19",  # 中秋節
    "2026-10-09",  # 國慶日補假
    "2026-10-10",  # 國慶日
    "2026-12-25",  # 行憲紀念日
    # 2025
    "2025-01-01",
    "2025-01-27",
    "2025-01-28",
    "2025-01-29",
    "2025-01-30",
    "2025-01-31",
    "2025-02-28",
    "2025-04-03",
    "2025-04-04",
    "2025-05-01",
    "2025-05-31",  # 端午節
    "2025-10-10",
}

# ── US market holidays (NYSE / NASDAQ) ────────────────────────────────────────
# Source: https://www.nyse.com/markets/hours-calendars
US_HOLIDAYS: set[str] = {
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-11-27",  # Black Friday (early close — treated as full holiday)
    "2026-12-24",  # Christmas Eve (early close — treated as full holiday)
    "2026-12-25",  # Christmas Day
    # 2025
    "2025-01-01",
    "2025-01-20",
    "2025-02-17",
    "2025-04-18",
    "2025-05-26",
    "2025-06-19",
    "2025-07-04",
    "2025-09-01",
    "2025-11-27",
    "2025-12-25",
}

# ── Ticker configuration ───────────────────────────────────────────────────────

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
    "SPX":  ("^GSPC",    "S&P 500",     ""),
    "NDX":  ("^NDX",     "NASDAQ 100",  ""),
    "DJI":  ("^DJI",     "Dow Jones",   ""),
    "VIX":  ("^VIX",     "VIX",         ""),
    "TNX":  ("^TNX",     "US10Y Yield", "percent"),
    "DXY":  ("DX-Y.NYB", "美元指數",    ""),
    "BTC":  ("BTC-USD",  "Bitcoin",     "USD"),
    "TSM":  ("TSM",      "台積電 ADR",  "USD"),
    "NVDA": ("NVDA",     "NVIDIA",      "USD"),
}

FOREX: dict[str, tuple[str, str, str]] = {
    "USDTWD": ("TWD=X", "USD/TWD", "TWD"),
}

# ── Holiday / weekend helpers ──────────────────────────────────────────────────

def _holiday_name(date_str: str) -> str:
    names = {
        "2026-01-01": "元旦",
        "2026-02-28": "和平紀念日",
        "2026-04-04": "兒童節/清明",
        "2026-05-01": "勞動節",
        "2026-06-19": "端午節",
        "2026-09-19": "中秋節",
        "2026-10-10": "國慶日",
        "2026-12-25": "行憲紀念日",
    }
    return names.get(date_str, "國定假日")


def is_tw_market_closed(report_type: str) -> bool:
    """Return True if the Taiwan stock market is closed today."""
    if report_type in ("us_open", "us_close"):
        return False  # US-only reports skip TW holiday check
    today = datetime.now(tz=TPE)
    today_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        print(f"[fetch] {today_str} is a weekend — TW market closed, skipping report")
        return True
    if today_str in TW_HOLIDAYS:
        print(f"[fetch] {today_str} is {_holiday_name(today_str)} — TW market closed, skipping report")
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
    if today_str in US_HOLIDAYS:
        print(f"[fetch] {today_str} is a US holiday — US market closed, sending notice")
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
