#!/usr/bin/env python3
"""Quote providers with tiered failover.

Order: batch yfinance (one request for all symbols) → per-symbol yfinance with
retry → free HTTP fallbacks (TWSE OpenAPI for .TW in one call, Yahoo chart API
via plain requests for everything else — survives yfinance library breakage).
"""

import logging
from datetime import datetime
from typing import Protocol

import requests
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from models import Quote

log = logging.getLogger("providers")


class QuoteProvider(Protocol):
    name: str

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]: ...


def _quote_from_closes(closes: list[tuple[str, float]]) -> Quote | None:
    """closes: [(YYYY-MM-DD, close), …] ascending. Needs >= 1 row."""
    if not closes:
        return None
    date, last = closes[-1]
    prev = closes[-2][1] if len(closes) >= 2 else last
    change = (last - prev) / prev * 100 if prev else 0.0
    return Quote(price=round(last, 4), prev_close=round(prev, 4),
                 change_pct=round(change, 2), data_date=date)


class YFinanceBatchProvider:
    name = "yfinance_batch"

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        try:
            df = yf.download(symbols, period="5d", group_by="ticker",
                             progress=False, threads=True)
        except Exception:
            log.warning("batch download failed", exc_info=True)
            return out
        if df is None or df.empty:
            return out
        for sym in symbols:
            try:
                series = (df[sym]["Close"] if len(symbols) > 1
                          else df["Close"]).dropna()
                closes = [(idx.strftime("%Y-%m-%d"), float(v))
                          for idx, v in series.items()]
                if (q := _quote_from_closes(closes)):
                    out[sym] = q
            except (KeyError, TypeError, ValueError):
                continue
        return out


class YFinanceSingleProvider:
    name = "yfinance_single"

    @retry(reraise=True, stop=stop_after_attempt(3),
           wait=wait_exponential_jitter(initial=2, max=15))
    def _one(self, symbol: str) -> Quote | None:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        closes = [(idx.strftime("%Y-%m-%d"), float(v))
                  for idx, v in hist["Close"].dropna().items()]
        return _quote_from_closes(closes)

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym in symbols:
            try:
                if (q := self._one(sym)):
                    out[sym] = q
            except Exception:
                log.warning("single fetch failed", extra={"symbol": sym})
        return out


class TWSEProvider:
    """All .TW symbols in ONE call via TWSE OpenAPI (no key required)."""
    name = "twse_openapi"
    URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

    @staticmethod
    def _roc_to_iso(date_str: str) -> str:
        """TWSE dates are ROC format, e.g. '1140703' → '2025-07-03'."""
        if len(date_str) == 7 and date_str.isdigit():
            year = int(date_str[:3]) + 1911
            return f"{year}-{date_str[3:5]}-{date_str[5:7]}"
        return date_str

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = {s.split(".")[0]: s for s in symbols if s.endswith(".TW")}
        if not wanted:
            return {}
        out: dict[str, Quote] = {}
        try:
            resp = requests.get(self.URL, timeout=20)
            resp.raise_for_status()
            rows = resp.json()
        except Exception:
            log.warning("twse openapi failed", exc_info=True)
            return out
        for row in rows:
            code = row.get("Code")
            if code not in wanted:
                continue
            try:
                last = float(row["ClosingPrice"])
                change = float(row.get("Change") or 0)
                prev = last - change
                out[wanted[code]] = Quote(
                    price=round(last, 4), prev_close=round(prev, 4),
                    change_pct=round(change / prev * 100, 2) if prev else 0.0,
                    data_date=self._roc_to_iso(row.get("Date", "")))
            except (KeyError, ValueError):
                continue
        return out


class YahooChartProvider:
    """Yahoo chart API via plain requests — no yfinance dependency, so it keeps
    working when the yfinance library breaks (its most common failure mode).
    Covers equities, indices, futures, forex, and crypto."""
    name = "yahoo_chart"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def _one(self, symbol: str) -> Quote | None:
        resp = requests.get(self.URL.format(symbol=symbol),
                            params={"range": "5d", "interval": "1d"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        raw_closes = result["indicators"]["quote"][0]["close"]
        timestamps = result["timestamp"]
        closes = [(datetime.fromtimestamp(t).strftime("%Y-%m-%d"), float(c))
                  for t, c in zip(timestamps, raw_closes) if c is not None]
        return _quote_from_closes(closes)

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym in symbols:
            try:
                if (q := self._one(sym)):
                    out[sym] = q
            except Exception:
                log.warning("yahoo chart fetch failed", extra={"symbol": sym})
        return out


def fetch_with_failover(symbols: list[str]) -> tuple[dict[str, Quote], dict[str, str]]:
    """Return ({symbol: Quote}, {symbol: provider_name}), trying each tier for
    whatever the previous tiers missed."""
    chain: list[QuoteProvider] = [
        YFinanceBatchProvider(), YFinanceSingleProvider(),
        TWSEProvider(), YahooChartProvider(),
    ]
    quotes: dict[str, Quote] = {}
    sources: dict[str, str] = {}
    remaining = list(symbols)
    for provider in chain:
        if not remaining:
            break
        got = provider.fetch_many(remaining)
        for sym, q in got.items():
            quotes[sym] = q
            sources[sym] = provider.name
        remaining = [s for s in remaining if s not in quotes]
        log.info("provider tier done", extra={
            "provider": provider.name, "hit": len(got), "miss": len(remaining)})
    return quotes, sources
