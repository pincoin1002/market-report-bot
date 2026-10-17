#!/usr/bin/env python3
"""One immutable, encrypted, public-only Oct17 catch-up. Never fetches data.

No plaintext input or rendered content is logged or uploaded. Uses the normal
validator, atomic remote claim, Telegram sender and receipt implementation.
Any send/receipt uncertainty leaves the remote claim blocking future sends.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

import delivery_state
from generate_report import deliver_validated_report, public_telegram_payload, _split_message
from market_session import us_close_session_contract
from models import MarketContext, MarketReportDraft, QuoteObservation
from structured_reports import validate_public_draft
from validate_report import (validate_numeric_provenance, validate_render_matches_draft,
                             validate_rendered_report_structure)

TARGET = {"market": "US", "session": "close", "trading_date": "2026-10-16",
          "publication_date": "2026-10-17", "receipt_key": "us_close:20261017"}
EXPECTED_SHA256 = "0caf5775f741807210b4f6fcd2645c195fd830d369f038885656f5ab2479fb6e"
SOURCE_SNAPSHOT_SHA256 = "b520d79b07290edd406cbe94d0b0c39b1733ef9d7c5730fa48d9c299c93e966f"
SOURCE_MANIFEST = {"snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
                   "original_draft_sha256": "7d9d1b4a1f79ee940d684c359e4851225649a5cc41dd40ebbb14b963f1fa1668",
                   "original_report_sha256": "6f4af1234cd339a7722209e209393400529392557b4194f8f7a7b32f3741d365",
                   "source_fetch_coverage": 1.0, "source_quote_count": 82,
                   "events": "UNVERIFIED_NO_EVENT_CLAIMS"}
FROZEN_AT = "2026-10-17T16:39:56.055406+08:00"
RETRIEVED_AT = "2026-10-17T08:36:12.397921+00:00"
PUBLIC_SYMBOLS = frozenset(("SPX", "NDX", "DJI", "SOX", "VIX", "TNX", "US2Y", "DXY",
                            "NVDA", "AAPL", "MSFT", "GOOGL", "CL"))
OUTCOME_PATH = Path(__file__).resolve().parents[1] / "data/frozen_recovery_outcome.json"


class RecoveryBlocked(ValueError):
    """Only fixed, non-sensitive reason codes may cross the logging boundary."""


def fail(code):
    raise RecoveryBlocked(code)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def validate_payload(raw: bytes):
    if len(raw) > 45000 or hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
        fail("FROZEN_PAYLOAD_CHECKSUM_MISMATCH")
    p = json.loads(raw)
    if set(p) != {"schema_version", "target", "source_manifest", "frozen_at", "quotes",
                  "draft", "telegram_sha256"} or p["schema_version"] != "1" or p["target"] != TARGET:
        fail("FROZEN_TARGET_OR_SCHEMA_INVALID")
    if p["frozen_at"] != FROZEN_AT:
        fail("FROZEN_TIMESTAMP_INVALID")
    if p["source_manifest"] != SOURCE_MANIFEST:
        fail("FROZEN_SOURCE_INVALID")
    at = datetime.fromisoformat(FROZEN_AT)
    contract = us_close_session_contract(at, publication_date=TARGET["publication_date"])
    if contract["status"] != "READY" or contract["us_trading_date"] != TARGET["trading_date"]:
        fail("FROZEN_CALENDAR_INVALID")
    if set(p["quotes"]) != PUBLIC_SYMBOLS:
        fail("FROZEN_PUBLIC_UNIVERSE_INVALID")
    quotes = {}
    for symbol, raw_quote in p["quotes"].items():
        if set(raw_quote) != set(QuoteObservation.model_fields):
            fail("FROZEN_QUOTE_FIELDS_INVALID")
        q = QuoteObservation.model_validate(raw_quote)
        if (q.canonical_symbol != symbol or q.market_date != TARGET["trading_date"]
                or q.provider != "yfinance_batch" or q.quality_status != "VALID"
                or q.quote_type != "OFFICIAL_CLOSE" or q.session != "REGULAR"
                or q.market != ("GLOBAL" if symbol == "CL" else "US")
                or q.retrieved_at != datetime.fromisoformat(RETRIEVED_AT)
                or q.observed_at != q.retrieved_at or q.provider_timestamp is not None
                or q.change_interval != "PREVIOUS_CLOSE" or q.quality_notes
                or q.corporate_action_note is not None
                or q.quote_id != f"{symbol}:2026-10-16:REGULAR:yfinance_batch"):
            fail("FROZEN_QUOTE_PROVENANCE_INVALID")
        quotes[symbol] = q
    # Freshness means historical session provenance, never a claim of live data.
    context = MarketContext(run_id="us_close:2026-10-16", report_type="us_close",
                            market_date="2026-10-16", generated_at=at,
                            market_session="REGULAR", quotes=quotes, degraded_mode=True)
    d = p["draft"]
    if set(d) != set(MarketReportDraft.model_fields):
        fail("FROZEN_DRAFT_FIELDS_INVALID")
    draft = MarketReportDraft.model_validate(d)
    if (draft.run_id != context.run_id or draft.report_type != "us_close"
            or draft.portfolio_section is not None or draft.taiex_summary is not None
            or draft.institutional_flows is not None or draft.drivers or draft.event_calendar
            or draft.why_drivers or draft.rotation
            or not draft.headline.startswith("補報｜美股收盤日報 2026-10-16")
            or "新聞尚未完成驗證" not in draft.rendered_markdown):
        fail("FROZEN_DRAFT_SCOPE_INVALID")
    checks = (validate_public_draft(draft, context)[0],
              validate_numeric_provenance(draft.rendered_markdown, context)[0],
              validate_render_matches_draft(draft.rendered_markdown, draft)[0],
              validate_rendered_report_structure(draft.rendered_markdown, "us_close")[0])
    if not all(checks):
        fail("FROZEN_REPORT_VALIDATION_FAILED")
    text = public_telegram_payload(draft.rendered_markdown, "us_close", at=at)
    if hashlib.sha256(text.encode()).hexdigest() != p["telegram_sha256"] or len(_split_message(text)) != 1:
        fail("FROZEN_TELEGRAM_PAYLOAD_INVALID")
    return context, draft, at


def _outcome(status, *, ids=None, accepted=None, error_type=None, reason=None):
    # Safe operational evidence only. Never serialize the input or exception text.
    result = {"status": status, "receipt_key": TARGET["receipt_key"],
              "payload_sha256": EXPECTED_SHA256, "telegram_message_ids": ids or [],
              "telegram_api_accepted": accepted, "at": datetime.now(timezone.utc).isoformat()}
    if error_type:
        result["error_type"] = error_type
    if reason:
        result["reason"] = reason
    OUTCOME_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTCOME_PATH.write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps(result, sort_keys=True), flush=True)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\nFrozen catch-up operational receipt (no report content):\n```json\n"
                    + json.dumps(result, sort_keys=True) + "\n```\n")
    return result


def execute(raw: bytes, mode: str):
    context, draft, at = validate_payload(raw)
    key = TARGET["receipt_key"]
    if mode == "dry_run":
        simulated = set()
        for _ in range(2):
            result = deliver_validated_report(draft.rendered_markdown, "us_close", context, draft,
                         None, "frozen-public-only", key, dry_run=True,
                         simulated_deliveries=simulated, at=at, public_only=True, single_attempt=True)
        if result["public_delivery"] != "SKIPPED":
            fail("FROZEN_DRY_RUN_DEDUP_FAILED")
        return _outcome("DRY_RUN_VALIDATED_NO_SEND", accepted=False)
    if mode not in {"deliver_once", "verify_delivered"}:
        fail("FROZEN_MODE_INVALID")
    if (os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_REPOSITORY") != "pincoin1002/market-report-bot"
            or os.getenv("GITHUB_REF") != "refs/heads/main"
            or os.getenv("DELIVERY_RECEIPT_REMOTE_CHECK") != "true"):
        fail("FROZEN_PRODUCTION_GUARD_INVALID")
    # Query latest main even if checkout has an old/local receipt. DELIVERING or
    # malformed receipt raises uncertainty and prevents any attempt.
    receipt = delivery_state.remote_receipt(key)
    if receipt is not None:
        return _outcome("ALREADY_DELIVERED_BLOCKED", ids=receipt["telegram_message_ids"], accepted=True)
    if mode == "verify_delivered":
        fail("FROZEN_RECEIPT_NOT_FOUND")
    if delivery_state.already_delivered(key):
        fail("FROZEN_LOCAL_REMOTE_RECEIPT_CONFLICT")
    destinations = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
    if len(destinations) != 1 or not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        fail("FROZEN_SINGLE_DESTINATION_AUTH_REQUIRED")
    try:
        result = deliver_validated_report(draft.rendered_markdown, "us_close", context, draft,
                   None, "frozen-public-only", key, at=at, public_only=True, single_attempt=True)
        if result["public_delivery"] != "SENT":
            _outcome("AMBIGUOUS_DELIVERY_STOP_NO_RETRY", accepted=None)
            fail("FROZEN_DELIVERY_UNCONFIRMED")
        receipt = delivery_state.remote_receipt(key)
        if receipt is None or receipt["telegram_message_ids"] != result["message_ids"]:
            fail("FROZEN_RECEIPT_VERIFICATION_FAILED")
        return _outcome("DELIVERED_RECEIPT_VERIFIED", ids=result["message_ids"], accepted=True)
    except Exception:
        local = delivery_state.load_state().get("delivered", {}).get(key, {}).get("telegram_result", {})
        _outcome("AMBIGUOUS_DELIVERY_STOP_NO_RETRY", ids=local.get("message_ids", []),
                 accepted=True if local.get("ok") else None)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, help="Local no-send only")
    args = parser.parse_args()
    try:
        if args.payload:
            raw, mode = args.payload.read_bytes(), "dry_run"
        else:
            if os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch":
                fail("FROZEN_MANUAL_INTAKE_REQUIRED")
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            inputs = event["inputs"]
            encrypted = inputs["encrypted_payload"]
            if not isinstance(encrypted, str) or len(encrypted) > 62000:
                fail("FROZEN_ENVELOPE_INVALID")
            raw = Fernet(os.environ["PORTFOLIO_KEY"].encode()).decrypt(encrypted.encode())
            mode = inputs["mode"]
        execute(raw, mode)
        return 0
    except Exception as exc:
        # Deliberately never print traceback, input, HTTP body or secret values.
        previous = json.loads(OUTCOME_PATH.read_text()) if OUTCOME_PATH.exists() else {}
        if previous.get("status") != "AMBIGUOUS_DELIVERY_STOP_NO_RETRY":
            _outcome("BLOCKED_STOP_NO_RETRY", error_type=type(exc).__name__,
                     reason=str(exc) if isinstance(exc, RecoveryBlocked) else None)
        return 1


if __name__ == "__main__":
    sys.exit(main())
