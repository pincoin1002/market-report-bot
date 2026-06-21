#!/usr/bin/env python3
"""Fetch verified market prices via yfinance → data/market_snapshot.json.

Run before generate_report.py so the report has anchored, verified prices.
If this script fails, generate_report.py falls back to pure search mode.
Exit code 2 = market closed today (holiday / weekend) → skip report generation.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

TPE = timezone(timedelta(hours=8))

# ── Taiwan public holidays (YYYY-MM-DD) ───────────────────────────────────────
# Update this list each year.  Covers TSE trading holidays only.
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
    "2026-09-19",  # 中秋節（週六補假至週五）
    "2026-10-09",  # 國慶日補假
    "2026-10-10",  # 國慶日
    "2026-12-25",  # 行憲紀念日
    # 2025 (for reference / backfill)
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
        "DXY":  ("DX-Y.NYB", "美元指數",      ""),
        "BTC":  ("BTC-USD",  "Bitcoin",      "USD"),
        "TSM":  ("TSM",      "台積電 ADR",    "USD"),
        "NVDA": ("NVDA",     "NVIDIA",       "USD"),
}

FOREX: dict[str, tuple[str, str, str]] = {
        "USDTWD": ("TWD=X", "USD/TWD", "TWD"),
}

# ── Holiday / weekend check ────────────────────────────────────────────────────

def is_tw_market_closed(report_type: str) -> bool:
        """Return True if the Taiwan market is closed today.
            For US-only report types (us_open / us_close) skip the TW holiday check.
                """
    if report_type in ("us_open", "us_close"):
                return False  # US schedules handle their own holidays
    today = datetime.now(tz=TPE)
    today_str = today.strftime("%Y-%m-%d")
    # Weekends
    if today.weekday() >= 5:
                print(f"[fetch] today is {today_str} (weekend) — market closed, skipping report")
                return True
            # Public holidays
            if today_str in TW_HOLIDAYS:
                        print(f"[fetch] today is {today_str} ({_holiday_name(today_str)}) — market closed, skipping report")
                        return True
                    return False

def _holiday_name(date_str: str) -> str:
        names = {
                    "2026-06-19": "端午節",
                    "2026-01-01": "元旦",
                    "2026-02-28": "和平紀念日",
                    "2026-04-04": "兒童節/清明",
                    "2026-05-01": "勞動節",
                    "2026-09-19": "中秋節",
                    "2026-10-10": "國慶日",
                    "2026-12-25": "行憲紀念日",
        }
        return names.get(date_str, "國定假日")

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

    # ── Holiday / weekend guard ────────────────────────────────────────────────
        if is_tw_market_closed(report_type):
                    # Signal the workflow to skip report generation
                    _set_github_output("market_closed", "true")
                    today_str = datetime.now(tz=TPE).strftime("%Y-%m-%d")
                    print(f"[fetch] EXIT 2 — market closed on {today_str}, report skipped")
                    sys.exit(2)

        _set_github_output("market_closed", "false")

    print(f"[fetch] building snapshot for {report_type} …")
    snapshot = build_snapshot(report_type)

    snapshot_path = data_dir / "market_snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tw_count = len(snapshot["tw_stocks"])
    us_count = len(snapshot["us_markets"])
    fx_count = len(snapshot["forex"])
    print(f"[fetch] snapshot saved → {snapshot_path} "
                    f"(TW:{tw_count} US:{us_count} FX:{fx_count})")


if __name__ == "__main__":
        main()
