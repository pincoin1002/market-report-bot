#!/usr/bin/env python3
"""Regenerate a real production artifact with product contracts; zero sends."""
import argparse
import json
import subprocess
import sys
import types
from pathlib import Path

from event_source_evidence import verify_event_publication
from generate_report import deliver_validated_report
from models import MarketContext, MarketReportDraft, PortfolioActionBrief, Snapshot
from portfolio_context import load_authoritative_portfolio
from replay_report_run import artifact_file
from structured_reports import build_action_brief, build_public_draft, render_action_brief, validate_action_brief, validate_public_draft
from twse_market_evidence import fetch_twse_close_evidence
from market_session import get_previous_completed_session_date
from validate_report import validate_numeric_provenance, validate_render_matches_draft, validate_rendered_report_structure


def replay_product(artifact: Path, output: Path, *, official_data=False, event_sources: Path | None = None,
                   evidence_dir: Path | None = None, before_ref: str | None = None):
    snapshot = Snapshot.model_validate_json(artifact_file(artifact, "market_snapshot.json").read_text())
    context = MarketContext.model_validate_json(artifact_file(artifact, "market_context.json").read_text())
    original_draft = MarketReportDraft.model_validate_json(artifact_file(artifact, "market_report_draft.json").read_text())
    original_brief = PortfolioActionBrief.model_validate_json(artifact_file(artifact, "portfolio_action_brief.json").read_text())
    original_quotes = snapshot.model_dump(mode="json")["quote_observations"]
    events = list(original_brief.event_facts)
    cached_evidence = None
    if evidence_dir:
        cached_evidence = Snapshot.model_validate_json((evidence_dir / "market_snapshot.json").read_text())
        cached_brief = PortfolioActionBrief.model_validate_json((evidence_dir / "portfolio_action_brief.json").read_text())
        by_event = {(fact.instrument_id, fact.title): fact for fact in cached_brief.event_facts}
        for index, fact in enumerate(events):
            if evidence := by_event.get((fact.instrument_id, fact.title)):
                events[index] = fact.model_copy(update={"source_url": evidence.source_url,
                    "source_published_at": evidence.source_published_at,
                    "publication_date_verified": evidence.publication_date_verified})
        if cached_evidence.taiex_summary:
            summary = cached_evidence.taiex_summary
            if summary.session_date != context.market_date or abs(summary.close - context.quotes["TAIEX"].price) > .005:
                raise ValueError("stored official evidence conflicts with original session")
            snapshot.taiex_summary = context.taiex_summary = summary
        if cached_evidence.institutional_flows:
            flows = cached_evidence.institutional_flows
            if flows.session_date != context.market_date:
                raise ValueError("stored official flow session mismatch")
            snapshot.institutional_flows = context.institutional_flows = flows
    if event_sources:
        source_urls = json.loads(event_sources.read_text())
        for index, fact in enumerate(events):
            if fact.title in source_urls:
                verified = verify_event_publication({**fact.model_dump(mode="json"), "source_url": source_urls[fact.title]})
                events[index] = type(fact).model_validate({**fact.model_dump(mode="json"), "source_url": verified["source_url"],
                    "source_published_at": verified.get("source_published_at"),
                    "publication_date_verified": verified.get("publication_date_verified", False)})
    if official_data:
        previous = get_previous_completed_session_date("TW", context.market_date)
        evidence = fetch_twse_close_evidence(context.market_date, previous)
        if evidence and evidence.taiex_summary:
            quote = context.quotes["TAIEX"]
            if abs(evidence.taiex_summary.close - quote.price) > .005:
                raise ValueError("official close conflicts with original validated snapshot")
            snapshot.taiex_summary = evidence.taiex_summary
            context.taiex_summary = evidence.taiex_summary
        if evidence and evidence.institutional_flows:
            snapshot.institutional_flows = evidence.institutional_flows
            context.institutional_flows = evidence.institutional_flows
    portfolio = load_authoritative_portfolio()
    if len(portfolio.positions) != original_brief.total_positions:
        raise ValueError("canonical portfolio differs from this historical replay; use its original private snapshot")
    draft = build_public_draft(context)
    brief = build_action_brief(context, portfolio, verified_events=events, upcoming_events=original_brief.upcoming_events)
    private_text = render_action_brief(brief)
    checks = {
        "draft": validate_public_draft(draft, context),
        "numeric_provenance": validate_numeric_provenance(draft.rendered_markdown, context),
        "structure": validate_rendered_report_structure(draft.rendered_markdown, context.report_type),
        "render": validate_render_matches_draft(draft.rendered_markdown, draft),
        "private_advice": validate_action_brief(brief, context, portfolio),
    }
    private_tokens = ("【我的持股】", "持股行情", "行情覆蓋", "portfolio", "FULL", f"{len(portfolio.positions)}/{len(portfolio.positions)}")
    checks["public_private_boundary"] = (not any(token in draft.rendered_markdown for token in private_tokens), "public portfolio metadata")
    checks["quotes_unchanged"] = (original_quotes == snapshot.model_dump(mode="json")["quote_observations"], "quote mutation")
    simulation = set()
    first = deliver_validated_report(draft.rendered_markdown, context.report_type, context, draft, snapshot,
        "replay", f"{context.report_type}:{context.market_date.replace('-', '')}", dry_run=True, simulated_deliveries=simulation, at=context.generated_at)
    second = deliver_validated_report(draft.rendered_markdown, context.report_type, context, draft, snapshot,
        "replay", f"{context.report_type}:{context.market_date.replace('-', '')}", dry_run=True, simulated_deliveries=simulation, at=context.generated_at)
    checks["delivery_simulation"] = (first["public_delivery"] == "SIMULATED" and second["public_delivery"] == "SKIPPED", "idempotent no-send simulation")
    output.mkdir(parents=True, exist_ok=True)
    (output / "public_before.md").write_text(original_draft.rendered_markdown, encoding="utf-8")
    if before_ref:
        code = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[1]), "show", f"{before_ref}:scripts/structured_reports.py"], check=True, capture_output=True, text=True).stdout
        module = types.ModuleType("historical_product_renderer")
        sys.modules[module.__name__] = module
        exec(compile(code, "historical_product_renderer", "exec"), module.__dict__)
        (output / "private_before.md").write_text(module.render_action_brief(original_brief), encoding="utf-8")
    (output / "public_after.md").write_text(draft.rendered_markdown, encoding="utf-8")
    (output / "private_after.md").write_text(private_text, encoding="utf-8")
    for name, model in (("market_snapshot", snapshot), ("market_context", context), ("market_report_draft", draft), ("portfolio_action_brief", brief)):
        (output / f"{name}.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")
    result = {"checks": {key: "PASS" if value[0] else "FAIL" for key, value in checks.items()},
              "errors": {key: value[1] for key, value in checks.items() if not value[0]},
              "event_coverage": f"{brief.event_checked_positions}/{brief.event_total_positions}",
              "suppressed_events": brief.suppressed_events,
              "actionable_events": [item.ticker for item in brief.action_queue], "telegram_messages_sent": 0,
              "official_market_available": context.taiex_summary is not None and context.taiex_summary.advancing is not None,
              "official_flows_available": context.institutional_flows is not None,
              "overall": "PASS" if all(value[0] for value in checks.values()) else "FAIL"}
    (output / "product_quality_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--official-data", action="store_true")
    parser.add_argument("--event-sources", type=Path)
    parser.add_argument("--evidence-dir", type=Path, help="Reuse saved official/source-date evidence for a deterministic no-network replay")
    parser.add_argument("--before-ref")
    parser.add_argument("--no-send", action="store_true", required=True)
    args = parser.parse_args()
    result = replay_product(args.artifact, args.output, official_data=args.official_data, event_sources=args.event_sources,
                            evidence_dir=args.evidence_dir, before_ref=args.before_ref)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["overall"] == "PASS" else 1)
