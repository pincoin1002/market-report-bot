#!/usr/bin/env python3
"""Validate structured V2 market reports before delivery.

Primary truth is data/market_context.json + data/market_report_draft.json.
Rendered Markdown checks are smoke checks only; LLM price extraction is retired.
"""

import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from logging_config import setup_logging
from models import MarketContext, MarketReportDraft, PriceCheck, StructureCheck
from structured_reports import validate_public_draft

log = logging.getLogger("validate")

REQUIRED_SECTIONS = {
    "tw_open": ["Executive Market State", "What Changed Since Last Report", "Top Market Drivers", "Rotation / Regime", "Watch Into Close"],
    "tw_close": ["Executive Market State", "What Changed Since Last Report", "Top Market Drivers", "Rotation / Regime", "Watch Into Close"],
    "us_open": ["Executive Market State", "What Changed Since Last Report", "Top Market Drivers", "Rotation / Regime", "Watch Into Close"],
    "us_close": ["Executive Market State", "What Changed Since Last Report", "Top Market Drivers", "Rotation / Regime", "Watch Into Close"],
}


def _latest_report(report_type: str) -> str | None:
    files = sorted((Path(__file__).parent.parent / "reports").glob(f"{report_type}_*.md"), reverse=True)
    return files[0].read_text(encoding="utf-8") if files else None


def _load_context() -> MarketContext | None:
    path = Path(__file__).parent.parent / "data" / "market_context.json"
    if not path.exists():
        return None
    try:
        return MarketContext.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        log.error("market_context.json invalid", exc_info=True)
        return None


def _load_draft() -> MarketReportDraft | None:
    path = Path(__file__).parent.parent / "data" / "market_report_draft.json"
    if not path.exists():
        return None
    try:
        return MarketReportDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        log.error("market_report_draft.json invalid", exc_info=True)
        return None


def check_structure(report_text: str, report_type: str) -> StructureCheck:
    anchors = REQUIRED_SECTIONS.get(report_type, [])
    missing = [anchor for anchor in anchors if anchor not in report_text]
    missing_marker_count = report_text.count("⚠️ 未取得")
    total_fields_estimate = max(report_text.count("|") // 4, 1)
    return StructureCheck(
        sections_total=len(anchors),
        sections_present=len(anchors) - len(missing),
        sections_missing=missing,
        truncated=bool(anchors) and anchors[-1] not in report_text,
        missing_data_count=missing_marker_count,
        missing_data_pct=round(missing_marker_count / total_fields_estimate * 100, 1),
        char_count=len(report_text),
    )


def validate_render_matches_draft(report_text: str, draft: MarketReportDraft) -> tuple[bool, str]:
    draft_json = draft.model_dump_json()
    for ref in draft.price_references:
        if ref.kind in ("ACTION_TRIGGER", "TECHNICAL_LEVEL", "VALUATION"):
            continue
        if ref.canonical_symbol not in report_text:
            return False, f"{ref.canonical_symbol} missing from rendered report"
        if ref.quote_id and ref.quote_id not in draft_json:
            return False, f"{ref.canonical_symbol} quote_id missing from draft"
    return True, "OK"


def validate(report_type: str) -> tuple[bool, dict]:
    report_text = _latest_report(report_type)
    if not report_text:
        return False, {"reason": "no report found"}

    structure = check_structure(report_text, report_type)
    context = _load_context()
    draft = _load_draft()
    if context is None or draft is None:
        return False, {"reason": "structured artifacts unavailable", "structure": structure.model_dump()}
    if draft.report_type != report_type or context.report_type != report_type:
        return False, {"reason": "report_type mismatch", "structure": structure.model_dump()}

    public_ok, public_reason = validate_public_draft(draft, context)
    render_ok, render_reason = validate_render_matches_draft(report_text, draft)
    structure_ok = not structure.truncated and not structure.sections_missing
    ok = public_ok and render_ok and structure_ok
    return ok, {
        "report_type": report_type,
        "structure": structure.model_dump(),
        "structured": {"passed": public_ok, "reason": public_reason},
        "render": {"passed": render_ok, "reason": render_reason},
        "price_check": PriceCheck().model_dump(),
    }


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        print("Usage: validate_report.py <report_type>", file=sys.stderr)
        sys.exit(1)
    report_type = sys.argv[1]
    ok, payload = validate(report_type)
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "report_summary.json").write_text(
        json.dumps({"report_type": report_type, "validator": "structured_v2"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (data_dir / "validation_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not ok:
        log.error("validation FAIL", extra={"payload": payload})
        sys.exit(1)
    log.info("validation PASS", extra={"report_type": report_type})


if __name__ == "__main__":
    main()
