#!/usr/bin/env python3
"""Quote providers with tiered failover.

Order: batch yfinance (one request for all symbols) → per-symbol yfinance with
retry → free HTTP fallbacks (TWSE OpenAPI for .TW in one call, Yahoo chart API
via plain requests for everything else — survives yfinance library breakage).
"""

import logging
import math
from datetime import datetime, timezone
from typing import Protocol

import requests
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from market_session import NY, classify_us_session
from models import InstrumentSpec, Quote, QuoteObservation, Session
from quote_quality import validate_observation

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


class YahooExtendedHoursProvider:
    """Timestamped Yahoo chart path for US equities/ETFs.

    It uses minute bars with includePrePost=true. If no extended-hours trade is
    available, callers should fall back to the daily-close chain and label that
    result PREVIOUS_CLOSE/CLOSED_REFERENCE.
    """
    name = "yahoo_chart_extended"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def _one(self, spec: InstrumentSpec, expected_session: Session) -> QuoteObservation | None:
        symbol = spec.provider_symbols["yfinance"]
        resp = requests.get(self.URL.format(symbol=symbol),
                            params={"range": "5d", "interval": "1m", "includePrePost": "true"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result.get("meta", {})
        currency = meta.get("currency") or spec.currency
        previous_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
        quote = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp") or []
        closes = quote.get("close") or []
        obs_time = None
        price = None
        for ts, close in reversed(list(zip(timestamps, closes))):
            if close is None:
                continue
            value = float(close)
            if math.isfinite(value) and value > 0:
                obs_time = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(NY)
                price = value
                break
        if obs_time is None or price is None or previous_close <= 0:
            return None
        actual_session = classify_us_session(obs_time, extended_quote_available=True)
        if actual_session not in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            return None
        change_pct = (price - previous_close) / previous_close * 100
        quote_id = f"{spec.canonical_symbol}:{obs_time.strftime('%Y-%m-%dT%H:%M:%S%z')}:{actual_session}:{self.name}"
        obs = QuoteObservation(
            quote_id=quote_id,
            instrument_id=spec.canonical_symbol,
            canonical_symbol=spec.canonical_symbol,
            price=round(price, spec.price_precision),
            currency=currency,
            session=actual_session,
            market_date=obs_time.strftime("%Y-%m-%d"),
            observed_at=obs_time,
            provider_timestamp=obs_time,
            retrieved_at=datetime.now(tz=NY),
            provider=self.name,
            quote_type="TRADE",
            is_delayed=bool(meta.get("exchangeTimezoneName")),
            quality_status="VALID",
            previous_regular_close=round(previous_close, spec.price_precision),
            change_pct=round(change_pct, 2),
        )
        return validate_observation(obs, spec, expected_session=expected_session)

    def fetch_many(self, specs: list[InstrumentSpec], expected_session: Session) -> dict[str, QuoteObservation]:
        out: dict[str, QuoteObservation] = {}
        for spec in specs:
            if spec.market != "US" or spec.asset_type not in ("EQUITY", "ETF"):
                continue
            try:
                obs = self._one(spec, expected_session)
                if obs:
                    out[spec.canonical_symbol] = obs
            except Exception:
                log.warning("extended-hours fetch failed", extra={"symbol": spec.canonical_symbol})
        return out


def observation_from_daily_quote(spec: InstrumentSpec, q: Quote, provider: str,
                                 session: Session, retrieved_at: datetime) -> QuoteObservation:
    observed_at = datetime.fromisoformat(f"{q.data_date[:10].replace('/', '-')}T00:00:00+00:00")
    quote_id = f"{spec.canonical_symbol}:{q.data_date}:{session}:{provider}"
    obs = QuoteObservation(
        quote_id=quote_id,
        instrument_id=spec.canonical_symbol,
        canonical_symbol=spec.canonical_symbol,
        price=round(q.price, spec.price_precision),
        currency=spec.currency,
        session=session,
        market_date=q.data_date,
        observed_at=observed_at,
        provider_timestamp=None,
        retrieved_at=retrieved_at,
        provider=provider,
        quote_type="OFFICIAL_CLOSE" if session in ("REGULAR", "PREVIOUS_CLOSE", "CLOSED_REFERENCE") else "REFERENCE",
        is_delayed=True,
        quality_status="VALID",
        previous_regular_close=round(q.prev_close, spec.price_precision),
        change_pct=q.change_pct,
    )
    return validate_observation(obs, spec, expected_session=session)


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


def fetch_session_observations(specs: list[InstrumentSpec], expected_session: Session
                               ) -> tuple[dict[str, QuoteObservation], dict[str, str]]:
    observations: dict[str, QuoteObservation] = {}
    sources: dict[str, str] = {}
    if expected_session in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
        extended = YahooExtendedHoursProvider().fetch_many(specs, expected_session)
        for symbol, obs in extended.items():
            observations[symbol] = obs
            sources[specs_by_symbol(specs)[symbol].provider_symbols["yfinance"]] = obs.provider

    remaining_specs = [s for s in specs if s.canonical_symbol not in observations]
    provider_symbols = [s.provider_symbols["yfinance"] for s in remaining_specs]
    daily_quotes, daily_sources = fetch_with_failover(provider_symbols)
    provider_to_spec = {s.provider_symbols["yfinance"]: s for s in remaining_specs}
    fallback_session: Session = "PREVIOUS_CLOSE" if expected_session in ("PREMARKET", "CLOSED_REFERENCE") else expected_session
    if expected_session == "AFTER_HOURS":
        fallback_session = "PREVIOUS_CLOSE"
    retrieved_at = datetime.now(tz=timezone.utc)
    for provider_symbol, q in daily_quotes.items():
        spec = provider_to_spec[provider_symbol]
        provider = daily_sources.get(provider_symbol, "unknown")
        observations[spec.canonical_symbol] = observation_from_daily_quote(
            spec, q, provider, fallback_session, retrieved_at)
        sources[provider_symbol] = provider
    return observations, sources


def specs_by_symbol(specs: list[InstrumentSpec]) -> dict[str, InstrumentSpec]:
    return {spec.canonical_symbol: spec for spec in specs}
