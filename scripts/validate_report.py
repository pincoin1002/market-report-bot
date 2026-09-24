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
from instrument_registry import resolve_instrument

log = logging.getLogger("validate")


REQUIRED_SECTIONS = {
    "tw_open": [("Executive Market State", "市場核心概況")],
    "tw_close": [("今日市場", "Executive Market State", "市場核心概況")],
    "us_open": [("Executive Market State", "市場核心概況")],
    "us_close": [("Executive Market State", "市場核心概況")],
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
    anchors_list = REQUIRED_SECTIONS.get(report_type, [])
    missing = []
    for anchor_group in anchors_list:
        if isinstance(anchor_group, (tuple, list)):
            if not any(a in report_text for a in anchor_group):
                missing.append(anchor_group[0])
        else:
            if anchor_group not in report_text:
                missing.append(anchor_group)
    missing_marker_count = report_text.count("⚠️ 未取得")
    total_fields_estimate = max(report_text.count("|") // 4, 1)
    return StructureCheck(
        sections_total=len(anchors_list),
        sections_present=len(anchors_list) - len(missing),
        sections_missing=missing,
        truncated=bool(missing),
        missing_data_count=missing_marker_count,
        missing_data_pct=round(missing_marker_count / total_fields_estimate * 100, 1),
        char_count=len(report_text),
    )


def validate_rendered_report_structure(report_text: str, report_type: str) -> tuple[bool, list[str]]:
    """Strict structural and semantic validation for rendered public reports."""
    import re
    errors: list[str] = []
    lines = report_text.splitlines()

    # 1. Exactly one top-level title
    h1_lines = [line for line in lines if line.startswith("# ")]
    if len(h1_lines) != 1:
        errors.append(f"Expected exactly 1 top-level H1 title, found {len(h1_lines)}: {h1_lines}")

    # 2. Coherent hierarchy: check for nested or restarting numbering
    h2_lines = [line.strip() for line in lines if line.startswith("## ")]
    seen_numbers = []
    for h2 in h2_lines:
        match = re.match(r"^##\s+(\d+)\.", h2)
        if match:
            num = int(match.group(1))
            if seen_numbers and num <= seen_numbers[-1]:
                errors.append(f"Non-monotonic or restarting section numbering: found ## {num}. after ## {seen_numbers[-1]}.")
            seen_numbers.append(num)

    # 3. Check for duplicated section titles
    if len(h2_lines) != len(set(h2_lines)):
        dupes = [h for h in set(h2_lines) if h2_lines.count(h) > 1]
        errors.append(f"Duplicated section headers: {dupes}")

    # 4. Forbidden internal diagnostic tokens
    forbidden_tokens = [
        "VALID", "PARTIAL", "DATA_BLOCKED", "DATEMISMATCH", "DATE_MISMATCH",
        "MarketContext", "弱資料模組", "UNAVAILABLE", "NO_MATERIAL_CHANGE",
        "SIZE_NOT_COMPUTED", "QUOTE_UNAVAILABLE_OR_INVALID",
    ]
    for token in forbidden_tokens:
        pattern = rf"(?:\b|\||\s){re.escape(token)}(?:\b|\||\s)"
        if re.search(pattern, report_text):
            errors.append(f"Forbidden internal diagnostic token found: '{token}'")

    # 5. Forbidden placeholder sentences
    forbidden_placeholders = [
        "僅列 verified quote",
        "弱資料不硬填",
        "等待下一份",
        "僅在可靠搜尋結果可得時呈現",
        "本段不以模型記憶補完",
        "無達 materiality 門檻",
    ]
    for ph in forbidden_placeholders:
        if ph in report_text:
            errors.append(f"Forbidden placeholder text found: '{ph}'")

    # 6. Check for DATE_MISMATCH ticker dump
    if re.search(r"-\s+[A-Z0-9]+:\s*DATE_MISMATCH", report_text):
        errors.append("DATE_MISMATCH ticker dump found in report")

    # 7. Check for impossible session timestamps
    if report_type.startswith("tw_"):
        pattern = r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})(?::\d{2})?\s*(UTC[+-]\d{2}:?\d{2}|[+-]\d{2}:?\d{2}|[A-Za-z]{3,4})?"
        for match in re.finditer(pattern, report_text):
            hour = int(match.group(2))
            minute = int(match.group(3))
            tz = match.group(4) or "UTC"
            matching_lines = [l for l in lines if match.group(0) in l]
            if matching_lines and ("REGULAR" in matching_lines[0] or "正式" in matching_lines[0]):
                if tz == "UTC":
                    if hour > 5 or (hour == 5 and minute > 35):
                        errors.append(f"Impossible regular session quote timestamp in UTC: {match.group(0)}")
                elif "+08" in tz or "TPE" in tz:
                    if hour > 13 or (hour == 13 and minute > 35):
                        errors.append(f"Impossible regular session quote timestamp in TPE: {match.group(0)}")

    # 8. Check units for turnover & institutional numbers
    if "成交金額" in report_text:
        turnover_lines = [l for l in lines if "成交金額" in l]
        for tl in turnover_lines:
            if re.search(r"\d+[\d,.]*", tl) and "億" not in tl and "—" not in tl and "未取得" not in tl:
                errors.append(f"Turnover missing 億 unit: '{tl}'")

    # 9. Material usefulness check for tw_close
    if report_type == "tw_close":
        if not ("加權指數" in report_text or "TAIEX" in report_text):
            errors.append("tw_close report missing TAIEX / 加權指數")
        if not ("台積電" in report_text or "2330" in report_text):
            errors.append("tw_close report missing 台積電 / 2330")

    return len(errors) == 0, errors


def validate_numeric_provenance(report_text: str, context: MarketContext) -> tuple[bool, list[str]]:
    """Strict numeric provenance validation: all numbers in public output must originate from validated structured data."""
    import re
    errors: list[str] = []

    # 1. Symbol price provenance: public numeric value == structured MarketContext value
    for symbol, obs in context.quotes.items():
        if obs.quality_status != "VALID":
            continue
        spec = resolve_instrument(symbol)
        names = [symbol]
        if spec.display_name and spec.display_name != symbol:
            names.append(spec.display_name)

        # Specifically forbid corruptions like 2454 = 1485
        if symbol == "2454":
            if "1485" in report_text or "1,485" in report_text:
                errors.append(f"CRITICAL: 2454 corrupted as 1485 instead of structured price {obs.price}")

        # Check line where symbol is displayed
        matching_lines = [l for l in report_text.splitlines() if any(n in l for n in names) and "|" in l]
        for line in matching_lines:
            # Expected price representations
            price_variants = [
                f"{obs.price:,.2f}",
                f"{obs.price:,.1f}",
                f"{obs.price:g}",
                f"{int(obs.price):,}" if obs.price == int(obs.price) else "",
                str(int(obs.price)) if obs.price == int(obs.price) else "",
                f"{obs.price:.2f}",
            ]
            price_variants = [v for v in price_variants if v]
            if not any(v in line for v in price_variants):
                errors.append(f"Price for {symbol} ({obs.price}) not found matching in rendered line: '{line}'")

    # 2. Narrative numeric integrity: numbers in narrative must be traceable to structured data
    allowed_numbers: set[float] = set()
    for p in context.market_date.split("-"):
        if p.isdigit():
            allowed_numbers.add(float(p))
    for n in range(1, 10):
        allowed_numbers.add(float(n))
    for symbol, obs in context.quotes.items():
        if symbol.isdigit():
            allowed_numbers.add(float(symbol))
        allowed_numbers.add(round(float(obs.price), 2))
        allowed_numbers.add(float(int(obs.price)))
        allowed_numbers.add(round(float(obs.previous_regular_close), 2))
        allowed_numbers.add(float(int(obs.previous_regular_close)))
        allowed_numbers.add(round(float(obs.change_pct), 2))
        allowed_numbers.add(round(abs(float(obs.change_pct)), 2))
        delta = round(abs(obs.price - obs.previous_regular_close), 2)
        allowed_numbers.add(delta)
        allowed_numbers.add(float(int(delta)))

    if context.taiex_summary:
        ts = context.taiex_summary
        for val in [ts.open, ts.high, ts.low, ts.close, ts.point_change, ts.change_pct,
                    ts.turnover_ntd_billions, ts.advancing, ts.declining, ts.unchanged]:
            if val is not None:
                allowed_numbers.add(round(float(val), 2))
                allowed_numbers.add(round(abs(float(val)), 2))
                allowed_numbers.add(float(int(abs(val))))
        # Intraday range & retracement tenths
        if ts.high and ts.low and ts.high > ts.low:
            rng = round(ts.high - ts.low, 2)
            allowed_numbers.add(rng)
            allowed_numbers.add(float(int(rng)))
            faded = round(ts.high - ts.close, 2)
            allowed_numbers.add(faded)
            allowed_numbers.add(float(int(faded)))
            tenth = int(round((faded / rng) * 10))
            allowed_numbers.add(float(tenth))
        # Only dynamically derived index levels are valid narrative numbers.
        increment = 1000.0 if ts.close >= 10_000 else 100.0
        allowed_numbers.add((ts.close // increment) * increment)
        allowed_numbers.add(round(ts.close / increment) * increment)

    if context.institutional_flows:
        fl = context.institutional_flows
        for val in [fl.foreign_buy_sell_ntd_billions, fl.investment_trust_buy_sell_ntd_billions,
                    fl.dealer_buy_sell_ntd_billions, fl.total_buy_sell_ntd_billions,
                    fl.foreign_futures_net_oi, fl.foreign_futures_oi_change,
                    fl.foreign_buy_sell_prev_ntd_billions, fl.turnover_prev_ntd_billions]:
            if val is not None:
                allowed_numbers.add(round(float(val), 2))
                allowed_numbers.add(round(abs(float(val)), 2))
                allowed_numbers.add(float(int(abs(val))))
        if fl.turnover_prev_ntd_billions is not None and context.taiex_summary and context.taiex_summary.turnover_ntd_billions:
            t_delta = round(abs(context.taiex_summary.turnover_ntd_billions - fl.turnover_prev_ntd_billions), 2)
            allowed_numbers.add(t_delta)
            allowed_numbers.add(float(int(t_delta)))
        if fl.foreign_buy_sell_ntd_billions is not None and fl.foreign_buy_sell_prev_ntd_billions is not None:
            foreign_delta = round(abs(fl.foreign_buy_sell_ntd_billions - fl.foreign_buy_sell_prev_ntd_billions), 2)
            allowed_numbers.add(foreign_delta)
            allowed_numbers.add(float(int(foreign_delta)))

    # Methodology thresholds are not price targets; dynamic report levels must
    # still originate from the current deterministic context above.
    allowed_numbers.update([30.0, 500.0, 1000.0, 100.0, 30000.0])

    in_narrative = False
    for line in report_text.splitlines():
        line_s = line.strip()
        if line_s.startswith("## ") and any(k in line_s for k in ("Top Market Drivers", "今日走勢", "Rotation", "輪動", "Events", "事件", "今日關鍵驅動", "相較昨日", "明日觀察")):
            in_narrative = True
            continue
        elif line_s.startswith("## ") and any(k in line_s for k in ("Executive Market State", "市場核心概況", "What Changed", "今日市場", "法人與資金", "權值與族群", "持股資料狀態")):
            in_narrative = False
            continue
        elif line_s.startswith("# "):
            in_narrative = False
            continue

        if in_narrative and line_s and not line_s.startswith("|"):
            text_to_check = re.sub(r"^[-*•\d.]+\s*", "", line_s)
            text_to_check = re.sub(r"\([0-9A-Za-z.]+\)", "", text_to_check)
            found_nums = re.findall(r"(?<![A-Za-z0-9_])(\d+(?:,\d+)*(?:\.\d+)?)(?![A-Za-z0-9_])", text_to_check)
            for raw_num in found_nums:
                clean_num = float(raw_num.replace(",", ""))
                if clean_num not in allowed_numbers:
                    errors.append(f"Ungrounded numeric claim '{raw_num}' in narrative without structured data provenance: '{line_s}'")


    return len(errors) == 0, errors


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
    struct_ok, struct_errors = validate_rendered_report_structure(report_text, report_type)
    prov_ok, prov_errors = validate_numeric_provenance(report_text, context)
    structure_ok = not structure.truncated and not structure.sections_missing
    ok = public_ok and render_ok and structure_ok and struct_ok and prov_ok
    return ok, {
        "report_type": report_type,
        "structure": structure.model_dump(),
        "structural_validation": {"passed": struct_ok, "errors": struct_errors},
        "numeric_provenance": {"passed": prov_ok, "errors": prov_errors},
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
