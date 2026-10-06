#!/usr/bin/env python3
"""Replay production artifacts or an as-of real snapshot through no-send delivery.

This command never calls fetch providers, Gemini, Telegram, or email. Artifact
mode preserves production observations; snapshot mode can reframe a completed
session for a new report type only when its dated quotes match the calendar.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from generate_report import (_verified_data_only_report, deliver_validated_report,
                             public_telegram_payload)
from market_context import build_market_context
from market_session import get_target_market_date
from models import MarketContext, MarketReportDraft, PortfolioActionBrief, Snapshot
from portfolio_context import load_authoritative_portfolio
from structured_reports import (build_action_brief, build_public_draft, render_action_brief,
                                validate_action_brief, validate_public_draft)
from validate_report import (validate_numeric_provenance,
                             validate_render_matches_draft,
                             validate_rendered_report_structure)

FORBIDDEN = ("DATA_BLOCKED", "DATE_MISMATCH", "DEBUG:", "## ⚠️ 系統提供的市場快照", "=== 今日市場報告 ===")


def artifact_file(root: Path, name: str) -> Path:
    direct = root / name
    if direct.is_file():
        return direct
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise ValueError(f"expected one {name} under artifact, found {len(matches)}")
    return matches[0]


def replay(report_type: str, *, artifact: Path | None = None,
           snapshot_path: Path | None = None, as_of: datetime | None = None,
           print_public: bool = False, print_private: bool = False) -> dict:
    if artifact:
        snapshot = Snapshot.model_validate_json(artifact_file(artifact, "market_snapshot.json").read_text(encoding="utf-8"))
        context = MarketContext.model_validate_json(artifact_file(artifact, "market_context.json").read_text(encoding="utf-8"))
        draft = MarketReportDraft.model_validate_json(artifact_file(artifact, "market_report_draft.json").read_text(encoding="utf-8"))
        if context.report_type != report_type or draft.report_type != report_type:
            raise ValueError("artifact report type mismatch")
        source = "production_artifact"
    else:
        if not snapshot_path or not as_of or as_of.tzinfo is None:
            raise ValueError("snapshot mode requires --snapshot and timezone-aware --as-of")
        original = Snapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        market = "TW" if report_type.startswith("tw_") else "US"
        expected_primary = get_target_market_date(report_type, market, now=as_of)
        primary = [obs for obs in original.quote_observations.values()
                   if obs.market == market and obs.quality_status == "VALID"]
        if not primary or any(obs.market_date != expected_primary for obs in primary):
            raise ValueError(f"no matching completed {market} session in supplied snapshot: expected {expected_primary}")
        # Reframe only the session label of an already observed official close.
        # Prices, trading dates, timestamps, providers, and quote IDs are unchanged.
        reframed = {}
        for symbol, observation in original.quote_observations.items():
            expected_date = get_target_market_date(report_type, observation.market, now=as_of) if observation.market in {"TW", "US"} else None
            if report_type == "tw_open" and expected_date == observation.market_date and observation.session == "REGULAR":
                observation = observation.model_copy(update={"session": "PREVIOUS_CLOSE"})
            reframed[symbol] = observation
        snapshot = original.model_copy(update={"report_type": report_type, "generated_at": as_of,
                                               "report_market_date": expected_primary,
                                               "quote_observations": reframed})
        context = build_market_context(snapshot, report_type, now=as_of, degraded_mode=True)
        draft = build_public_draft(context, _verified_data_only_report("", report_type))
        source = "completed_real_snapshot_as_of"

    text = draft.rendered_markdown
    draft_ok, draft_reason = validate_public_draft(draft, context)
    numeric_ok, numeric_errors = validate_numeric_provenance(text, context)
    structure_ok, structure_errors = validate_rendered_report_structure(text, report_type)
    match_ok, match_reason = validate_render_matches_draft(text, draft)
    forbidden = [token for token in FORBIDDEN if token in text]
    render_ok = structure_ok and match_ok and not forbidden
    key_date = (as_of or context.generated_at).strftime("%Y%m%d") if report_type == "tw_open" else context.market_date.replace("-", "")
    key = f"{report_type}:{key_date}"
    simulated: set[str] = set()
    first = deliver_validated_report(text, report_type, context, draft, snapshot, "replay", key,
                                     dry_run=True, simulated_deliveries=simulated, at=as_of or context.generated_at)
    second = deliver_validated_report(text, report_type, context, draft, snapshot, "replay", key,
                                      dry_run=True, simulated_deliveries=simulated, at=as_of or context.generated_at)
    delivery_ok = first["public_delivery"] == "SIMULATED" and second["public_delivery"] == "SKIPPED"
    private_status = "SKIPPED"
    private_text = ""
    if artifact:
        try:
            brief = PortfolioActionBrief.model_validate_json(artifact_file(artifact, "portfolio_action_brief.json").read_text(encoding="utf-8"))
            private_text = render_action_brief(brief)
            private_status = "PASS" if private_text.strip() else "FAIL"
        except (ValueError, OSError):
            private_status = "BLOCKED"
    elif snapshot_path:
        try:
            portfolio = load_authoritative_portfolio()
            brief = build_action_brief(context, portfolio, verified_events=[], upcoming_events=[])
            private_ok, _ = validate_action_brief(brief, context, portfolio)
            private_text = render_action_brief(brief) if private_ok else ""
            private_status = "PASS" if private_ok and private_text.strip() else "BLOCKED"
        except Exception:
            private_status = "BLOCKED"
    result = {"report_type": report_type, "source": source, "as_of": (as_of or context.generated_at).isoformat(),
              "input": "PASS", "draft": "PASS" if draft_ok else "FAIL",
              "numeric_provenance": "PASS" if numeric_ok else "FAIL",
              "render": "PASS" if render_ok else "FAIL",
              "public_delivery_simulation": "PASS" if delivery_ok else "FAIL",
              "private_advice": private_status,
              "overall": "PASS" if all((draft_ok, numeric_ok, render_ok, delivery_ok)) else "FAIL",
              "errors": {"draft": draft_reason if not draft_ok else "", "numeric": numeric_errors,
                         "structure": structure_errors, "match": match_reason if not match_ok else "",
                         "forbidden": forbidden},
              "public_telegram_chunks": len(first.get("telegram", {}).get("message_lengths", [])),
              "telegram_messages_sent": 0}
    if print_public:
        print("PUBLIC_WOULD_SEND_BEGIN")
        print(public_telegram_payload(text, report_type, at=as_of or context.generated_at))
        print("PUBLIC_WOULD_SEND_END")
    if print_private:
        print("PRIVATE_ACTION_BRIEF_BEGIN")
        print(private_text or "UNAVAILABLE: no validated private Action Brief artifact in this replay input")
        print("PRIVATE_ACTION_BRIEF_END")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-type", choices=("tw_open", "tw_close", "us_open", "us_close"), required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--artifact", type=Path)
    inputs.add_argument("--snapshot", type=Path)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--no-send", action="store_true", required=True)
    parser.add_argument("--print-public", action="store_true")
    parser.add_argument("--print-private", action="store_true")
    args = parser.parse_args()
    result = replay(args.report_type, artifact=args.artifact, snapshot_path=args.snapshot,
                    as_of=args.as_of, print_public=args.print_public, print_private=args.print_private)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
