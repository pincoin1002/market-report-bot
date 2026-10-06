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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from cryptography.fernet import Fernet, InvalidToken

from models import PortfolioContext, PortfolioEventFact, PortfolioEventStatus

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "portfolio_event_cache.json.enc"
FACTS_AUDIT_PATH = ROOT / "data" / "portfolio_event_facts.json"
DEFAULT_CACHE_TTL_HOURS = 12
EVENT_SEARCH_TIMEOUT_MS = 45_000
EVENT_BATCH_SIZE = 4
EVENT_TRANSIENT_RETRIES = 2
EVENT_RETRY_BACKOFF_SECONDS = 2


class EventSearchError(RuntimeError):
    """Safe, non-sensitive event-search contract failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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

    if not timedelta(0) <= now - checked_at < timedelta(hours=ttl_hours):
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
    "title": "Short display title in Traditional Chinese",
    "raw_source_title": "Exact original article title for source verification",
    "display_title_zh": "Traditional Chinese event title",
    "event_date": "YYYY-MM-DD",
    "published_at": "ISO-8601 publication/filing timestamp for recent events, or null for upcoming-only calendar events",
    "summary": "Traditional Chinese factual summary, only sourced facts",
    "fact_summary": "Traditional Chinese factual summary, distinct from interpretation",
    "investment_interpretation": "Traditional Chinese investment interpretation explicitly framed as inference",
    "uncertainty_note": "Traditional Chinese uncertainty; authorization is not execution and does not guarantee a price rise",
    "impact": "Traditional Chinese interpretation, not a verified fact",
    "severity": "LOW" or "MEDIUM" or "HIGH",
    "source_name": "Official IR / Bloomberg / SEC / etc.",
    "source_url": "Direct event article URL, never a search redirect or generic homepage",
    "source_type": "OFFICIAL_FILING" or "PRIMARY_NEWS" or "SEARCH_GROUNDED",
    "is_upcoming": false
  }}
]"""


def _parse_event_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _claim_within_requested_window(result: dict[str, Any], now: datetime) -> bool:
    """Deterministically enforce the 48h recent / 7d upcoming contract."""
    is_upcoming = bool(result.get("is_upcoming", False))
    event_date = _parse_event_datetime(result.get("event_date"))
    published_at = _parse_event_datetime(result.get("published_at"))

    if is_upcoming:
        if event_date is None:
            return False
        delta = event_date.date() - now.astimezone(timezone.utc).date()
        return 0 <= delta.days <= 7

    if result.get("event_status") != "EVENT_MATERIAL_FOUND":
        return True

    evidence_time = _parse_event_datetime(result.get("source_published_at")) or published_at or event_date
    if evidence_time is None:
        return False
    age = now.astimezone(timezone.utc) - evidence_time
    return timedelta(0) <= age <= timedelta(hours=48)


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
        raise EventSearchError("EMPTY_RESPONSE")

    # A response is not considered search-grounded merely because the search
    # tool was requested. Require actual grounding metadata from Gemini.
    candidates = getattr(response, "candidates", None) or []
    grounding_chunks = []
    if candidates:
        grounding_metadata = getattr(candidates[0], "grounding_metadata", None)
        grounding_chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
    if not grounding_chunks:
        raise EventSearchError("NO_GROUNDING_METADATA")

    text = response.text.strip()
    # Strip markdown code blocks if wrapped
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Search-grounded responses occasionally wrap an otherwise valid JSON
        # array in a short prose prefix/suffix. Parse only the outermost array;
        # never attempt to repair or invent missing JSON fields.
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise EventSearchError("INVALID_JSON") from None
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            raise EventSearchError("INVALID_JSON") from None
    if not isinstance(parsed, list):
        raise EventSearchError("NON_ARRAY_RESPONSE")
    from event_source_evidence import verify_event_publication
    return [verify_event_publication(item) if item.get("event_status") == "EVENT_MATERIAL_FOUND" or item.get("is_upcoming")
            else item for item in parsed]


def _safe_query_once(
    instruments: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Run one grounded query and return a safe error code on failure."""
    try:
        return _query_gemini_search(instruments, model=model, api_key=api_key), None
    except EventSearchError as exc:
        return None, exc.code
    except Exception as exc:
        return None, type(exc).__name__


def _query_with_transient_retry(
    instruments: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Retry only transient transport/server failures; never semantic failures."""
    safe_codes: list[str] = []
    for attempt in range(EVENT_TRANSIENT_RETRIES + 1):
        results, code = _safe_query_once(instruments, model=model, api_key=api_key)
        if results is not None:
            return results, safe_codes
        safe_codes.append(code or "UNKNOWN")
        # JSON/grounding/schema failures are deterministic contract failures.
        if code in {"INVALID_JSON", "NON_ARRAY_RESPONSE", "NO_GROUNDING_METADATA", "EMPTY_RESPONSE"}:
            break
        if attempt < EVENT_TRANSIENT_RETRIES:
            time.sleep(EVENT_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None, safe_codes


def _query_batch_with_bounded_fallback(
    instruments: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Small-batch grounded search with bounded retry and one split fallback.

    Primary batches are deliberately small. Transient transport/server failures
    get at most two retries. Contract failures get one split into smaller groups.
    No recursive retry is allowed.
    """
    results, codes = _query_with_transient_retry(
        instruments, model=model, api_key=api_key
    )
    if results is not None:
        return results, [], codes

    if len(instruments) <= 1:
        return [], [item["ticker"] for item in instruments], codes

    midpoint = (len(instruments) + 1) // 2
    combined: list[dict[str, Any]] = []
    failed: list[str] = []
    all_codes = list(codes)
    for half in (instruments[:midpoint], instruments[midpoint:]):
        if not half:
            continue
        half_results, half_codes = _query_with_transient_retry(
            half, model=model, api_key=api_key
        )
        all_codes.extend(half_codes)
        if half_results is None:
            failed.extend(item["ticker"] for item in half)
        else:
            combined.extend(half_results)
    return combined, failed, all_codes

def fetch_portfolio_events(
    portfolio: PortfolioContext,
    model: str = "gemini-2.0-flash",
    now: datetime | None = None,
    force_refresh_tickers: set[str] | None = None,
    api_key: str | None = None,
    cache_path: Path = CACHE_PATH,
    network_scope: Literal["all", "forced_only", "none"] = "all",
) -> tuple[list[PortfolioEventFact], list[dict[str, Any] | str]]:
    """Fetch/cache event evidence.

    network_scope="all" is for the dedicated background refresher.
    network_scope="forced_only" is for report delivery: fresh cache is reused
    and only explicitly forced tickers may make live search calls.
    network_scope="none" is cache-only.
    """
    current_time = now or datetime.now(tz=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    force_set = force_refresh_tickers or set()
    cache_data = load_event_cache(cache_path)
    cached_items = cache_data.get("items", {})
    cache_changed = False

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
        force_refresh = ticker in force_set
        if entry and is_entry_fresh(entry, current_time, force_refresh=force_refresh):
            # Cache hit: reconstruct facts
            entry_facts = entry.get("facts", [])
            for f in entry_facts:
                fact_obj = PortfolioEventFact.model_validate({
                    **f, "instrument_id": pos.instrument_id, "ticker": ticker,
                    "checked_at": f.get("checked_at") or entry.get("checked_at"),
                    "event_status": f.get("event_status", "EVENT_UNCHECKED"),
                })
                all_facts.append(fact_obj)
            for up in entry.get("upcoming", []):
                upcoming_events.append(up)
        else:
            may_query = (
                network_scope == "all"
                or (network_scope == "forced_only" and force_refresh)
            )
            if may_query:
                tickers_to_query.append(ticker)
            else:
                all_facts.append(PortfolioEventFact(
                    instrument_id=pos.instrument_id,
                    ticker=ticker,
                    checked_at=current_time,
                    event_status="EVENT_UNCHECKED",
                    summary="事件快取尚未更新或已超過有效時間，本次日報不等待外部搜尋。",
                    source_name="EventCache",
                    source_type="CACHE",
                ))

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
            # Small batches reduce blast radius of transient grounding failures.
            batch_size = EVENT_BATCH_SIZE
            for i in range(0, len(tickers_to_query), batch_size):
                batch = tickers_to_query[i:i + batch_size]
                try:
                    batch_instruments = [ticker_metadata[t] for t in batch]
                    results, prefailed_tickers, safe_error_codes = _query_batch_with_bounded_fallback(
                        batch_instruments,
                        model=model,
                        api_key=key,
                    )
                    if safe_error_codes:
                        log.warning(
                            "event search used bounded fallback: %s (batch_size=%d, failed_count=%d)",
                            ",".join(safe_error_codes),
                            len(batch),
                            len(prefailed_tickers),
                        )

                    # Organize results by ticker
                    results_by_ticker: dict[str, list[dict[str, Any]]] = {}
                    for r in results:
                        t = str(r.get("ticker", "")).upper()
                        results_by_ticker.setdefault(t, []).append(r)

                    for ticker in batch:
                        if ticker in prefailed_tickers:
                            inst_id = ticker_positions.get(ticker, ticker)
                            all_facts.append(PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status="EVENT_CHECK_FAILED",
                                summary="事件搜尋經有限重試後仍未完成驗證。",
                                source_name="GeminiSearch",
                                source_type="SEARCH_GROUNDED",
                            ))
                            continue
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
                        invalid_provenance = any(
                            (
                                (r.get("event_status") == "EVENT_MATERIAL_FOUND" or r.get("is_upcoming"))
                                and (not r.get("source_name") or not r.get("source_url") or not r.get("publication_date_verified"))
                            )
                            for r in t_results
                        )
                        invalid_window = any(
                            not _claim_within_requested_window(r, current_time)
                            for r in t_results
                            if r.get("event_status") == "EVENT_MATERIAL_FOUND" or r.get("is_upcoming")
                        )
                        if invalid_provenance or invalid_window:
                            reason = (
                                "搜尋結果缺少可驗證來源或原始發布日期，本次事件未完成驗證。"
                                if invalid_provenance
                                else "搜尋結果超出事件監控時間窗或缺少日期證據，本次事件未完成驗證。"
                            )
                            fact_obj = PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status="EVENT_CHECK_FAILED",
                                summary=reason,
                                source_name="GeminiSearch",
                                source_type="SEARCH_GROUNDED",
                            )
                            all_facts.append(fact_obj)
                            continue

                        cached_facts_for_ticker = []
                        cached_upcoming_for_ticker = []
                        for res in t_results:
                            is_up = bool(res.get("is_upcoming", False))
                            severity = str(res.get("severity", "LOW")).upper()
                            raw_status = res.get("event_status") or (
                                "EVENT_MATERIAL_FOUND" if severity in ("MEDIUM", "HIGH", "CRITICAL") else "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                            )
                            # Upcoming-only calendar items are not current
                            # material changes. LOW-severity observations also
                            # cannot trigger a portfolio WATCH by definition.
                            ev_status: PortfolioEventStatus = (
                                "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                                if is_up or (raw_status == "EVENT_MATERIAL_FOUND" and severity == "LOW")
                                else raw_status
                            )
                            fact_obj = PortfolioEventFact(
                                instrument_id=inst_id,
                                ticker=ticker,
                                checked_at=current_time,
                                event_status=ev_status,
                                event_type=res.get("event_type"),
                                title=res.get("title"),
                                event_date=res.get("event_date"),
                                published_at=_parse_event_datetime(res.get("published_at")),
                                source_published_at=_parse_event_datetime(res.get("source_published_at")),
                                publication_date_verified=res.get("publication_date_verified", False),
                                display_title_zh=res.get("display_title_zh", ""),
                                raw_source_title=res.get("raw_source_title", ""),
                                fact_summary=res.get("fact_summary", ""),
                                investment_interpretation=res.get("investment_interpretation", ""),
                                uncertainty_note=res.get("uncertainty_note", ""),
                                checked_window_start=current_time - timedelta(hours=48),
                                checked_window_end=current_time,
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
                                    **fact_obj.model_dump(mode="json"),
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
                        cache_changed = True
                except Exception as exc:
                    log.warning(
                        "event batch processing failed after bounded search: %s (batch_size=%d)",
                        type(exc).__name__,
                        len(batch),
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

            # Persist only when verified evidence actually changed.
            # Provider failures must not create random ciphertext churn.
            if cache_changed:
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
