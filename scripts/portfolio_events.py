#!/usr/bin/env python3
"""Authoritative portfolio company event monitoring pipeline.

Features:
- Search-grounded event verification using Gemini API + Google Search tool
- Per-holding tracking across all portfolio holdings
- Structured event evidence model (PortfolioEventFact)
- Deterministic 12-hour caching with freshness rules (price jump >=7%, near-term event, check failure)
- Fail-closed: search or API errors yield EVENT_CHECK_FAILED, never silent NO_MATERIAL_CHANGE
- Private: runtime event facts and cache are kept out of public reports
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet, InvalidToken

from models import PortfolioContext, PortfolioEventFact, PortfolioEventStatus

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "portfolio_event_cache.json.enc"
FACTS_AUDIT_PATH = ROOT / "data" / "portfolio_event_facts.json"
DEFAULT_CACHE_TTL_HOURS = 12
EVENT_SEARCH_TIMEOUT_MS = 60_000


def _cache_key() -> bytes | None:
    key = os.getenv("PORTFOLIO_KEY", "").strip()
    return key.encode() if key else None


def load_event_cache(cache_path: Path = CACHE_PATH) -> dict[str, Any]:
    """Load event cache. Production default is Fernet-encrypted; test paths may be JSON."""
    if not cache_path.exists():
        return {"updated_at": None, "items": {}}
    try:
        if cache_path.suffix == ".enc":
            key = _cache_key()
            if not key:
                log.warning("PORTFOLIO_KEY missing; encrypted event cache unavailable")
                return {"updated_at": None, "items": {}}
            raw = Fernet(key).decrypt(cache_path.read_bytes())
            return json.loads(raw.decode("utf-8"))
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as exc:
        log.warning("failed to load portfolio event cache", extra={"error": str(exc)})
        return {"updated_at": None, "items": {}}


def save_event_cache(cache_data: dict[str, Any], cache_path: Path = CACHE_PATH) -> None:
    """Persist event cache. Production default writes only ciphertext."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cache_data, ensure_ascii=False, indent=2).encode("utf-8")
        if cache_path.suffix == ".enc":
            key = _cache_key()
            if not key:
                log.warning("PORTFOLIO_KEY missing; refusing to persist plaintext event cache")
                return
            cache_path.write_bytes(Fernet(key).encrypt(payload))
        else:
            cache_path.write_text(payload.decode("utf-8"), encoding="utf-8")
    except Exception as exc:
        log.warning("failed to save portfolio event cache", extra={"error": str(exc)})


def is_entry_fresh(
    entry: dict[str, Any],
    now: datetime,
    ttl_hours: float = DEFAULT_CACHE_TTL_HOURS,
    force_refresh: bool = False,
) -> bool:
    """Evaluate whether a cached event check entry is still fresh and reusable."""
    if force_refresh:
        return False
    status = entry.get("event_status")
    # Never reuse a failed check
    if status == "EVENT_CHECK_FAILED":
        return False
    checked_at_str = entry.get("checked_at")
    if not checked_at_str:
        return False
    try:
        checked_at = datetime.fromisoformat(checked_at_str)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
    except Exception:
        return False

    if now - checked_at > timedelta(hours=ttl_hours):
        return False

    # If there's an upcoming event within 48 hours, refresh to check if it concluded
    for up in entry.get("upcoming", []):
        evt_date_str = up.get("event_date") if isinstance(up, dict) else None
        if evt_date_str:
            try:
                evt_date = datetime.fromisoformat(evt_date_str)
                if evt_date.tzinfo is None:
                    evt_date = evt_date.replace(tzinfo=timezone.utc)
                if abs((evt_date - now).total_seconds()) <= 48 * 3600:
                    return False
            except Exception:
                pass

    return True


def _build_search_prompt(instruments: list[dict[str, str]]) -> str:
    instrument_lines = "\n".join(
        f"- {item['ticker']} | name={item.get('name', '')} | asset_type={item.get('asset_type', '')}"
        for item in instruments
    )
    return f"""You are a professional financial portfolio event monitor.
Use Google Search to check ONLY the last 48 hours for material developments and the next 7 days for confirmed upcoming events for these instruments:
{instrument_lines}

Interpret events by instrument type:
- Company equity/ADR: earnings, guidance, filings, M&A, management, regulatory/legal action, major product/customer/supplier or capital-allocation events.
- ETF: material index/rebalance/methodology, distribution, closure/liquidation, split, fee or regulatory changes. Do not invent company-style earnings events for an ETF.
- Crypto/token/stablecoin: material protocol/network, exploit/security, tokenomics, listing/delisting, issuer/reserve, regulatory/legal or governance developments. Do not invent corporate earnings events for a token.

Rules:
1. Prefer sources in this order: company/issuer IR or official filing; regulator/exchange/protocol/issuer primary source; Reuters/Bloomberg/WSJ or similarly high-quality financial news; other sources only as corroboration.
2. "EVENT_CHECKED_NO_MATERIAL_CHANGE" means the grounded search actually covered that instrument and found no material item in the last 48 hours and no confirmed upcoming event in the next 7 days.
3. If an instrument cannot be checked reliably, return "EVENT_CHECK_FAILED". Never use omission as evidence of no event.
4. For any MATERIAL or UPCOMING event, source_name and source_url are mandatory.
5. If an upcoming event is identified, set is_upcoming: true.
6. Output exactly one or more JSON entries per instrument as needed, with NO markdown backticks or extra commentary:

[
  {{
    "ticker": "NVDA",
    "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE" or "EVENT_MATERIAL_FOUND",
    "event_type": "EARNINGS" or "GUIDANCE" or "M&A" or "MANAGEMENT" or "REGULATORY" or "PRODUCT" or "NONE",
    "title": "Short title in Traditional Chinese or English",
    "event_date": "YYYY-MM-DD",
    "summary": "Concise factual summary (1-2 sentences)",
    "impact": "Brief impact on investment thesis",
    "severity": "LOW" or "MEDIUM" or "HIGH",
    "source_name": "Official IR / Bloomberg / SEC / etc.",
    "source_url": "URL if available",
    "source_type": "OFFICIAL_FILING" or "PRIMARY_NEWS" or "SEARCH_GROUNDED",
    "is_upcoming": false
  }}
]"""


def _query_gemini_search(
    instruments: list[dict[str, str]],
    model: str = "gemini-2.0-flash",
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Query Gemini with Google Search for a batch of typed instruments."""
    from google import genai
    from google.genai import types

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured")

    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=EVENT_SEARCH_TIMEOUT_MS),
    )
    prompt = _build_search_prompt(instruments)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
            max_output_tokens=8192,
        ),
    )
    if not response or not response.text:
        raise ValueError("empty response from Gemini search")

    # A response is not considered search-grounded merely because the search
    # tool was requested. Require actual grounding metadata from Gemini.
    candidates = getattr(response, "candidates", None) or []
    grounding_chunks = []
    if candidates:
        grounding_metadata = getattr(candidates[0], "grounding_metadata", None)
        grounding_chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
    if not grounding_chunks:
        raise ValueError("Gemini response contained no Google Search grounding metadata")

    text = response.text.strip()
    # Strip markdown code blocks if wrapped
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()

    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("expected JSON array from Gemini search")
    return parsed


def fetch_portfolio_events(
    portfolio: PortfolioContext,
    model: str = "gemini-2.0-flash",
    now: datetime | None = None,
    force_refresh_tickers: set[str] | None = None,
    api_key: str | None = None,
    cache_path: Path = CACHE_PATH,
) -> tuple[list[PortfolioEventFact], list[dict[str, Any] | str]]:
    """Fetch or load cached event monitoring evidence for all holdings in portfolio."""
    current_time = now or datetime.now(tz=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    force_set = force_refresh_tickers or set()
    cache_data = load_event_cache(cache_path)
    cached_items = cache_data.get("items", {})

    all_facts: list[PortfolioEventFact] = []
    upcoming_events: list[dict[str, Any] | str] = []

    tickers_to_query: list[str] = []
    ticker_positions: dict[str, str] = {}  # ticker -> instrument_id
    ticker_metadata: dict[str, dict[str, str]] = {}

    for pos in portfolio.positions:
        ticker = pos.ticker.upper()
        ticker_positions[ticker] = pos.instrument_id
        ticker_metadata[ticker] = {
            "ticker": ticker,
            "name": pos.name,
            "asset_type": pos.asset_type or "UNKNOWN",
        }
        entry = cached_items.get(ticker)
        if entry and is_entry_fresh(entry, current_time, force_refresh=(ticker in force_set)):
            # Cache hit: reconstruct facts
            entry_facts = entry.get("facts", [])
            for f in entry_facts:
                fact_obj = PortfolioEventFact(
                    instrument_id=pos.instrument_id,
                    ticker=ticker,
                    checked_at=datetime.fromisoformat(f.get("checked_at", current_time.isoformat())),
                    event_status=f.get("event_status", "EVENT_CHECKED_NO_MATERIAL_CHANGE"),
                    event_type=f.get("event_type"),
                    title=f.get("title"),
                    event_date=f.get("event_date"),
                    summary=f.get("summary", ""),
                    impact=f.get("impact", ""),
                    severity=f.get("severity", "LOW"),
                    source_name=f.get("source_name", "CACHE"),
                    source_url=f.get("source_url", ""),
                    source_type=f.get("source_type", "SEARCH_GROUNDED"),
                    is_upcoming=f.get("is_upcoming", False),
                )
                all_facts.append(fact_obj)
            for up in entry.get("upcoming", []):
                upcoming_events.append(up)
        else:
            tickers_to_query.append(ticker)

    key = api_key or os.environ.get("GEMINI_API_KEY")

    if tickers_to_query:
        if not key:
            log.warning("GEMINI_API_KEY missing; event checks marked as EVENT_CHECK_FAILED")
            for ticker in tickers_to_query:
                inst_id = ticker_positions.get(ticker, ticker)
                fact_obj = PortfolioEventFact(
                    instrument_id=inst_id,
                    ticker=ticker,
                    checked_at=current_time,
                    event_status="EVENT_CHECK_FAILED",
                    summary="新聞搜尋與事件檢查服務未設定憑證或無法使用，本次未完成驗證。",
                    source_name="System",
                    source_type="SEARCH_GROUNDED",
                )
                all_facts.append(fact_obj)
        else:
            # Batch query in chunks of 8
            batch_size = 8
            for i in range(0, len(tickers_to_query), batch_size):
                batch = tickers_to_query[i:i + batch_size]
                try:
                    batch_instruments = [ticker_metadata[t] for t in batch]
                    results = _query_gemini_search(batch_instruments, model=model, api_key=key)
                    # Organize results by ticker
                    results_by_ticker: dict[str, list[dict[str, Any]]] = {}
                    for r in results:
                        t = str(r.get("ticker", "")).upper()
                        results_by_ticker.setdefault(t, []).append(r)

                    for ticker in batch:
                        inst_id = ticker_positions.get(ticker, ticker)
                        t_results = results_by_ticker.get(ticker, [])
                        if not t_results:
                            # Omission is not evidence of absence. A missing
                            # ticker from a grounded batch must fail closed.
                            fact_obj = PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status="EVENT_CHECK_FAILED",
                                summary="搜尋批次未回傳此標的結果，本次事件未完成驗證。",
                                source_name="GeminiSearch",
                                source_type="SEARCH_GROUNDED",
                            )
                            all_facts.append(fact_obj)
                            continue

                        # Material/upcoming claims require explicit provenance
                        # in addition to batch-level grounding metadata.
                        missing_provenance = any(
                            (
                                (r.get("event_status") == "EVENT_MATERIAL_FOUND" or r.get("is_upcoming"))
                                and (not r.get("source_name") or not r.get("source_url"))
                            )
                            for r in t_results
                        )
                        if missing_provenance:
                            fact_obj = PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status="EVENT_CHECK_FAILED",
                                summary="搜尋結果缺少可驗證來源，本次事件未完成驗證。",
                                source_name="GeminiSearch",
                                source_type="SEARCH_GROUNDED",
                            )
                            all_facts.append(fact_obj)
                            continue

                        cached_facts_for_ticker = []
                        cached_upcoming_for_ticker = []
                        for res in t_results:
                            is_up = bool(res.get("is_upcoming", False))
                            ev_status: PortfolioEventStatus = res.get("event_status") or (
                                "EVENT_MATERIAL_FOUND" if res.get("severity") in ("MEDIUM", "HIGH") else "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                            )
                            fact_obj = PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status=ev_status,
                                event_type=res.get("event_type"),
                                title=res.get("title"),
                                event_date=res.get("event_date"),
                                summary=res.get("summary", ""),
                                impact=res.get("impact", ""),
                                severity=res.get("severity", "LOW"),
                                source_name=res.get("source_name", "GoogleSearch"),
                                source_url=res.get("source_url", ""),
                                source_type=res.get("source_type", "SEARCH_GROUNDED"),
                                is_upcoming=is_up,
                            )
                            all_facts.append(fact_obj)
                            fact_dict = fact_obj.model_dump(mode="json")
                            cached_facts_for_ticker.append(fact_dict)

                            if is_up:
                                up_entry = {
                                    "ticker": ticker,
                                    "event": res.get("title") or res.get("event_type") or "公司日程",
                                    "date": res.get("event_date") or "",
                                    "why": res.get("summary") or res.get("impact") or "",
                                    "source": res.get("source_name", "GoogleSearch"),
                                }
                                upcoming_events.append(up_entry)
                                cached_upcoming_for_ticker.append(up_entry)

                        # Update cache entry
                        primary_status = (
                            "EVENT_MATERIAL_FOUND"
                            if any(f["event_status"] == "EVENT_MATERIAL_FOUND" for f in cached_facts_for_ticker)
                            else "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                        )
                        cached_items[ticker] = {
                            "ticker": ticker,
                            "instrument_id": inst_id,
                            "checked_at": current_time.isoformat(),
                            "event_status": primary_status,
                            "facts": cached_facts_for_ticker,
                            "upcoming": cached_upcoming_for_ticker,
                        }
                except Exception as exc:
                    log.warning(
                        "batch event query failed",
                        extra={"batch_size": len(batch), "error_type": type(exc).__name__},
                    )
                    for ticker in batch:
                        inst_id = ticker_positions.get(ticker, ticker)
                        fact_obj = PortfolioEventFact(
                            instrument_id=inst_id,
                            ticker=ticker,
                            checked_at=current_time,
                            event_status="EVENT_CHECK_FAILED",
                            summary="新聞搜尋連線或解析失敗，本次事件未完成驗證。",
                            source_name="GeminiSearch",
                            source_type="SEARCH_GROUNDED",
                        )
                        all_facts.append(fact_obj)

            # Persist updated cache
            cache_data["updated_at"] = current_time.isoformat()
            cache_data["items"] = cached_items
            save_event_cache(cache_data, cache_path)

    # Persist runtime audit file
    try:
        FACTS_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        audit_payload = {
            "generated_at": current_time.isoformat(),
            "facts": [f.model_dump(mode="json") for f in all_facts],
            "upcoming": upcoming_events,
        }
        FACTS_AUDIT_PATH.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("failed to write portfolio event facts audit", extra={"error": str(exc)})

    return all_facts, upcoming_events
