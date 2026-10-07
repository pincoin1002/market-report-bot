#!/usr/bin/env python3
"""Current PIOS -> full private decisions, with optional fresh quotes and zero sends."""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from generate_report import send_decision_telegram
from market_context import build_market_context
from models import MarketContext, PortfolioActionBrief, Snapshot
from portfolio_context import PIOSPortfolioProvider, load_authoritative_portfolio
from portfolio_decisions import (build_decision_brief, decision_telegram_chunks, load_decision_evidence,
                                 render_decision_brief, validate_decision_brief)


def replay(artifact: Path, output: Path, refresh_quotes=False, saved_market: Path | None = None):
    provider = PIOSPortfolioProvider()
    original_hash = hashlib.sha256(provider.path.read_bytes()).hexdigest() if provider.path.exists() else None
    portfolio = load_authoritative_portfolio()
    if portfolio.source != "PIOS_PORTFOLIO_SNAPSHOT" or not portfolio.positions:
        raise ValueError("current authoritative PIOS is required")
    old_brief = PortfolioActionBrief.model_validate_json((artifact / "portfolio_action_brief.json").read_text())
    if saved_market:
        snapshot = Snapshot.model_validate_json((saved_market / "market_snapshot.json").read_text())
        context = MarketContext.model_validate_json((saved_market / "market_context.json").read_text())
        now = max([context.generated_at, *(q.retrieved_at for q in context.quotes.values())])
    elif refresh_quotes:
        from fetch_market_data import build_snapshot
        # Private quote retrieval only: no public report, dispatch, or delivery.
        snapshot = build_snapshot("tw_close")
        now = datetime.now(tz=timezone.utc)
        context = build_market_context(snapshot, "tw_close", now=now)
    else:
        snapshot = Snapshot.model_validate_json((artifact / "market_snapshot.json").read_text())
        context = MarketContext.model_validate_json((artifact / "market_context.json").read_text())
        now = max([context.generated_at, *(q.retrieved_at for q in context.quotes.values())])
    brief = build_decision_brief(context, portfolio, events=old_brief.event_facts,
                                evidence=load_decision_evidence(), as_of=now)
    valid, reason = validate_decision_brief(brief, context, portfolio)
    if not valid:
        raise ValueError(reason)
    chunks = decision_telegram_chunks(brief)
    send_result = send_decision_telegram(brief, dry_run=True)
    if not send_result.get("simulated") or send_result.get("message_ids"):
        raise RuntimeError("no-send contract violated")
    if original_hash and hashlib.sha256(provider.path.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError("PIOS file was modified")
    output.mkdir(parents=True, exist_ok=True)
    (output / "private_decision_brief.txt").write_text(render_decision_brief(brief), encoding="utf-8")
    (output / "portfolio_decision_brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    (output / "market_snapshot.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    (output / "market_context.json").write_text(context.model_dump_json(indent=2), encoding="utf-8")
    (output / "telegram_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"as_of": brief.as_of.isoformat(), "positions": brief.expected_positions,
              "decisions": len(brief.decisions), "summary_counts": brief.summary_counts,
              "quoted": brief.quote_covered_positions, "event_checked": brief.event_checked_positions,
              "telegram_chunks": len(chunks), "telegram_messages_sent": 0,
              "pios_mutated": False, "validation": "PASS"}
    (output / "replay_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/decision_brief_replay"))
    parser.add_argument("--refresh-quotes", action="store_true")
    parser.add_argument("--saved-market", type=Path, help="Repeat a verified quote freeze without network")
    parser.add_argument("--no-send", action="store_true", required=True)
    args = parser.parse_args()
    if args.refresh_quotes and args.saved_market:
        parser.error("choose refresh-quotes or saved-market")
    print(json.dumps(replay(args.artifact, args.output, args.refresh_quotes, args.saved_market), ensure_ascii=False, indent=2))
