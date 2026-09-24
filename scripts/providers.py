#!/usr/bin/env python3
"""Quote providers with tiered failover.

Order: batch yfinance (one request for all symbols) → per-symbol yfinance with
retry → free HTTP fallbacks (TWSE OpenAPI for .TW in one call, Yahoo chart API
via plain requests for everything else — survives yfinance library breakage).
"""

import logging
import math
from datetime import datetime, time, timezone
from typing import Protocol

import requests
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from market_session import NY, TPE, classify_us_session
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


class CoinGeckoCryptoProvider:
    """Read-only fallback for registered crypto assets after Yahoo tiers fail.

    CoinGecko's simple-price payload supplies a USD price, 24-hour change and
    its own update timestamp.  A response without that timestamp/change, or
    one older than the bounded freshness window, is deliberately ignored so a
    stale value cannot be relabelled as a current portfolio quote.
    """
    name = "coingecko_simple_price"
    URL = "https://api.coingecko.com/api/v3/simple/price"
    MAX_AGE_SECONDS = 15 * 60

    def fetch_many(self, specs: list[InstrumentSpec]) -> dict[str, QuoteObservation]:
        crypto_specs = [
            spec for spec in specs
            if spec.asset_type == "CRYPTO" and spec.provider_symbols.get("coingecko")
        ]
        if not crypto_specs:
            return {}
        now = datetime.now(tz=timezone.utc)
        by_id = {spec.provider_symbols["coingecko"]: spec for spec in crypto_specs}
        try:
            response = requests.get(
                self.URL,
                params={
                    "ids": ",".join(sorted(by_id)),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_last_updated_at": "true",
                },
                headers={"User-Agent": "market-report-bot/1.0"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            log.warning("CoinGecko crypto fallback failed", exc_info=True)
            return {}

        observations: dict[str, QuoteObservation] = {}
        for coin_id, spec in by_id.items():
            row = payload.get(coin_id)
            if not isinstance(row, dict):
                continue
            try:
                price = float(row["usd"])
                change_pct = float(row["usd_24h_change"])
                updated_at = datetime.fromtimestamp(float(row["last_updated_at"]), tz=timezone.utc)
                previous = price / (1 + change_pct / 100)
            except (KeyError, TypeError, ValueError, OSError, OverflowError, ZeroDivisionError):
                continue
            if (not math.isfinite(price) or price <= 0 or not math.isfinite(change_pct)
                    or not math.isfinite(previous) or previous <= 0
                    or updated_at > now or (now - updated_at).total_seconds() > self.MAX_AGE_SECONDS):
                log.warning("CoinGecko crypto quote rejected for freshness/validity",
                            extra={"symbol": spec.canonical_symbol})
                continue
            observation = QuoteObservation(
                quote_id=f"{spec.canonical_symbol}:{updated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}:REGULAR:{self.name}",
                instrument_id=spec.canonical_symbol,
                canonical_symbol=spec.canonical_symbol,
                price=round(price, spec.price_precision),
                currency="USD",
                session="REGULAR",
                market_date=updated_at.strftime("%Y-%m-%d"),
                observed_at=updated_at,
                provider_timestamp=updated_at,
                retrieved_at=now,
                provider=self.name,
                quote_type="TRADE",
                is_delayed=True,
                quality_status="VALID",
                previous_regular_close=round(previous, spec.price_precision),
                change_pct=round(change_pct, 2),
                quality_notes=["24-hour change supplied by CoinGecko"],
                market=spec.market,
            )
            observations[spec.canonical_symbol] = validate_observation(observation, spec)
        return observations


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

    def fetch_many(self, specs: list[InstrumentSpec], expected_session: Session,
                   expected_dates: dict[str, str] | None = None) -> dict[str, QuoteObservation]:
        out: dict[str, QuoteObservation] = {}
        for spec in specs:
            if spec.market != "US" or spec.asset_type not in ("EQUITY", "ETF"):
                continue
            try:
                obs = self._one(spec, expected_session)
                if obs:
                    exp_date = (expected_dates or {}).get(spec.canonical_symbol) or (expected_dates or {}).get(spec.market)
                    if exp_date and obs.market_date != exp_date:
                        obs = validate_observation(obs, spec, expected_session=expected_session, expected_date=exp_date)
                    out[spec.canonical_symbol] = obs
            except Exception:
                log.warning("extended-hours fetch failed", extra={"symbol": spec.canonical_symbol})
        return out


def observation_from_daily_quote(spec: InstrumentSpec, q: Quote, provider: str,
                                 session: Session, retrieved_at: datetime,
                                 expected_date: str | None = None) -> QuoteObservation:
    quote_id = f"{spec.canonical_symbol}:{q.data_date}:{session}:{provider}"
    obs = QuoteObservation(
        quote_id=quote_id,
        instrument_id=spec.canonical_symbol,
        canonical_symbol=spec.canonical_symbol,
        price=round(q.price, spec.price_precision),
        currency=spec.currency,
        session=session,
        market_date=q.data_date,
        observed_at=retrieved_at,
        # Daily providers supply only EOD close + market date without an intraday trade timestamp.
        # Do NOT manufacture 13:30 or 16:00. provider_timestamp must remain None.
        provider_timestamp=None,
        retrieved_at=retrieved_at,
        provider=provider,
        quote_type="OFFICIAL_CLOSE" if session in ("REGULAR", "PREVIOUS_CLOSE", "CLOSED_REFERENCE") else "REFERENCE",
        is_delayed=True,
        quality_status="VALID",
        previous_regular_close=round(q.prev_close, spec.price_precision),
        change_pct=q.change_pct,
        market=spec.market,
    )
    return validate_observation(obs, spec, expected_session=session, expected_date=expected_date)



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


def fetch_session_observations(specs: list[InstrumentSpec], expected_session: Session,
                               expected_dates: dict[str, str] | None = None,
                               completed_close_markets: set[str] | None = None,
                               ) -> tuple[dict[str, QuoteObservation], dict[str, str]]:
    """Fetch observations under an explicit completed-session contract.

    Markets listed in completed_close_markets use their most recent completed
    official daily close from expected_dates. Extended-hours trades must not
    pre-empt that close. Continuous crypto fallback behavior is unchanged.
    """
    observations: dict[str, QuoteObservation] = {}
    sources: dict[str, str] = {}
    completed = {market.upper() for market in (completed_close_markets or set())}

    extended_specs = [
        spec for spec in specs
        if spec.market.upper() not in completed
    ]
    if expected_session in ("PREMARKET", "REGULAR", "AFTER_HOURS") and extended_specs:
        extended = YahooExtendedHoursProvider().fetch_many(
            extended_specs, expected_session, expected_dates=expected_dates
        )
        by_symbol = specs_by_symbol(specs)
        for symbol, obs in extended.items():
            observations[symbol] = obs
            sources[by_symbol[symbol].provider_symbols["yfinance"]] = obs.provider

    remaining_specs = [spec for spec in specs if spec.canonical_symbol not in observations]
    provider_symbols = [spec.provider_symbols["yfinance"] for spec in remaining_specs]
    daily_quotes, daily_sources = fetch_with_failover(provider_symbols)
    provider_to_spec = {spec.provider_symbols["yfinance"]: spec for spec in remaining_specs}
    fallback_session: Session = (
        "PREVIOUS_CLOSE"
        if expected_session in ("PREMARKET", "CLOSED_REFERENCE")
        else expected_session
    )
    if expected_session == "AFTER_HOURS":
        fallback_session = "PREVIOUS_CLOSE"
    retrieved_at = datetime.now(tz=timezone.utc)

    for provider_symbol, quote in daily_quotes.items():
        spec = provider_to_spec[provider_symbol]
        provider = daily_sources.get(provider_symbol, "unknown")
        expected_date = (
            (expected_dates or {}).get(spec.canonical_symbol)
            or (expected_dates or {}).get(spec.market)
        )
        quote_session = fallback_session
        if spec.market.upper() in completed and expected_date:
            if spec.market.upper() == "US":
                local_date = retrieved_at.astimezone(NY).strftime("%Y-%m-%d")
            elif spec.market.upper() == "TW":
                local_date = retrieved_at.astimezone(TPE).strftime("%Y-%m-%d")
            else:
                local_date = retrieved_at.strftime("%Y-%m-%d")
            quote_session = "REGULAR" if expected_date == local_date else "PREVIOUS_CLOSE"

        observations[spec.canonical_symbol] = observation_from_daily_quote(
            spec,
            quote,
            provider,
            quote_session,
            retrieved_at,
            expected_date=expected_date,
        )
        sources[provider_symbol] = provider

    remaining_crypto_specs = [
        spec for spec in specs
        if spec.asset_type == "CRYPTO" and spec.canonical_symbol not in observations
    ]
    crypto_observations = CoinGeckoCryptoProvider().fetch_many(remaining_crypto_specs)
    for symbol, observation in crypto_observations.items():
        observations[symbol] = observation
        spec = specs_by_symbol(specs)[symbol]
        sources[spec.provider_symbols["coingecko"]] = observation.provider
    if remaining_crypto_specs:
        log.info("crypto fallback tier done", extra={
            "provider": CoinGeckoCryptoProvider.name,
            "hit": len(crypto_observations),
            "miss": len(remaining_crypto_specs) - len(crypto_observations),
        })
    return observations, sources

def specs_by_symbol(specs: list[InstrumentSpec]) -> dict[str, InstrumentSpec]:
    return {spec.canonical_symbol: spec for spec in specs}
