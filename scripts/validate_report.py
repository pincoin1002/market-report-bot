#!/usr/bin/env python3
"""Validate report prices against market_snapshot.json.

Flow:
  1. Read the latest report from reports/
  2. Call Gemini (JSON mode, temp=0) to extract all stated prices → report_summary.json
  3. Compare extracted prices against market_snapshot.json within TOLERANCE
  4. Write validation_results.json; exit 1 if any ticker exceeds tolerance
"""

import json
import logging
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import ValidationError

from logging_config import setup_logging
from models import (PriceCheck, PriceCheckRow, Snapshot, StructureCheck,
                    ValidationResult)

log = logging.getLogger("validate")

TOLERANCE = 0.05  # 5 % — flag prices that deviate more than this

# Required section anchors per report type.
# Each string must appear somewhere in the report for the section to be "present".
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "tw_open": [
        "## 1.", "## 2.", "## 3.", "## 4.", "## 5.",
        "## 6.", "## 7.", "## 8.", "## 9.", "## 10.",
        "## 11.", "## 12.", "## 13.", "## 14.", "## 15.",
        "Tomorrow Key Signals", "Rotation Assessment", "AI Trend Health Check",
    ],
    "tw_close": [
        "## 1.", "## 2.", "## 3.", "## 4.", "## 5.",
        "## 6.", "## 7.", "## 8.", "## 9.", "## 10.",
        "## 11.", "## 12.", "## 13.", "## 14.", "## 15.",
        "Tomorrow Key Signals", "Rotation Assessment", "AI Trend Health Check",
    ],
    "us_open": [
        "## 1.", "## 2.", "## 3.", "## 4.", "## 5.",
        "## 6.", "## 7.", "## 8.", "## 9.", "## 10.",
        "## 11.", "## 12.", "## 13.", "## 14.", "## 15.",
        "Tomorrow Key Signals", "Rotation Assessment", "AI Trend Health Check",
    ],
    "us_close": [
        "## 1.", "## 2.", "## 3.", "## 4.", "## 5.",
        "## 6.", "## 7.", "## 8.", "## 9.", "## 10.",
        "## 11.", "## 12.", "## 13.", "## 14.", "## 15.",
        "## 16.", "## 17.", "## 18.", "## 19.", "## 20.",
        "## 21.", "## 22.",
        "Tomorrow Key Signals", "Rotation Assessment", "AI Trend Health Check",
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _latest_report(report_type: str) -> str | None:
    reports_dir = Path(__file__).parent.parent / "reports"
    files = sorted(reports_dir.glob(f"{report_type}_*.md"), reverse=True)
    return files[0].read_text(encoding="utf-8") if files else None


def _load_snapshot() -> Snapshot | None:
    p = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    if not p.exists():
        return None
    try:
        return Snapshot.model_validate_json(p.read_text(encoding="utf-8"))
    except ValidationError:
        log.error("market_snapshot.json invalid — skipping price comparison",
                  exc_info=True)
        return None


# ── Extraction ─────────────────────────────────────────────────────────────────

def extract_prices(report_text: str, model: str) -> dict[str, float]:
    """Use Gemini JSON mode to pull every explicitly stated price out of the report."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = f"""從以下市場報告中找出所有「明確寫出具體數字」的股票代號 / 指數 / 資產價格。

規則：
- 只抽取報告中明確出現的數字，絕對不要推算
- 台股用代號（如 "2330"），美股/指數用 ticker（如 "NVDA", "SPX", "VIX", "TNX"）
- 同一 ticker 出現多次時，取最後一次
- 輸出純 JSON，格式：{{"2330": 1045.0, "SPX": 5850.2, "VIX": 18.5}}
- 若找不到任何價格，輸出 {{}}
報告：
---
{report_text}
---"""

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=2000,
        ),
    )

    if not response.text:
        return {}
    try:
        return {k: float(v) for k, v in json.loads(response.text).items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        log.error("extraction parse error", exc_info=True,
                  extra={"raw": response.text[:300]})
        return {}


# ── Structure check ───────────────────────────────────────────────────────────

def check_structure(report_text: str, report_type: str) -> StructureCheck:
    """Check required sections are present and count missing-data markers."""
    anchors = REQUIRED_SECTIONS.get(report_type, [])
    missing = [a for a in anchors if a not in report_text]
    present = len(anchors) - len(missing)

    missing_marker_count = report_text.count("⚠️ 未取得")
    total_fields_estimate = max(report_text.count("|") // 4, 1)
    missing_pct = round(missing_marker_count / total_fields_estimate * 100, 1)

    # Truncation = last anchor in the list is absent
    truncated = bool(anchors) and (anchors[-1] not in report_text)

    result = StructureCheck(
        sections_total=len(anchors),
        sections_present=present,
        sections_missing=missing,
        truncated=truncated,
        missing_data_count=missing_marker_count,
        missing_data_pct=missing_pct,
        char_count=len(report_text),
    )

    log.info("structure check", extra={
        "report_type": report_type,
        "sections": f"{present}/{len(anchors)}",
        "truncated": truncated,
        "missing_data_count": missing_marker_count,
        "missing_data_pct": missing_pct,
        "char_count": len(report_text),
        "missing": missing,
    })
    return result


# ── Comparison ─────────────────────────────────────────────────────────────────

def compare(extracted: dict[str, float], snapshot: Snapshot) -> PriceCheck:
    ground_truth = snapshot.ground_truth()

    result = PriceCheck()
    for ticker, reported in extracted.items():
        if ticker in ground_truth:
            actual = ground_truth[ticker]
            diff = abs(reported - actual) / actual if actual != 0 else 0.0
            row = PriceCheckRow(ticker=ticker, reported=reported, actual=actual,
                                diff_pct=round(diff * 100, 2))
            (result.passed if diff <= TOLERANCE else result.failed).append(row)
        else:
            result.unchecked.append({"ticker": ticker, "reported": reported})
    return result


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        print("Usage: validate_report.py <report_type>", file=sys.stderr)
        sys.exit(1)

    report_type = sys.argv[1]
    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()

    if not os.getenv("GEMINI_API_KEY"):
        log.warning("GEMINI_API_KEY not set — skipping")
        sys.exit(0)

    report_text = _latest_report(report_type)
    if not report_text:
        log.warning("no report found — skipping", extra={"report_type": report_type})
        sys.exit(0)

    # ── Structure check (no API call needed) ─────────────────────────────────
    structure = check_structure(report_text, report_type)

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    # ── Price extraction ──────────────────────────────────────────────────────
    log.info("extracting prices from report", extra={"chars": len(report_text)})
    extracted = extract_prices(report_text, model)
    log.info("prices extracted", extra={"count": len(extracted)})

    (data_dir / "report_summary.json").write_text(
        json.dumps({"report_type": report_type, "extracted_prices": extracted},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    snapshot = _load_snapshot()
    if snapshot is None:
        log.warning("no usable market_snapshot.json — skipping price comparison")
        result = ValidationResult(structure=structure, price_check=None)
        (data_dir / "validation_results.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8")
        sys.exit(1 if structure.truncated else 0)

    price_results = compare(extracted, snapshot)

    log.info("price check", extra={
        "report_type": report_type,
        "passed": len(price_results.passed),
        "failed": len(price_results.failed),
        "unchecked": len(price_results.unchecked),
    })
    for row in price_results.failed:
        log.warning("price deviation exceeds tolerance", extra={
            "ticker": row.ticker, "reported": row.reported,
            "actual": row.actual, "diff_pct": row.diff_pct})

    result = ValidationResult(structure=structure, price_check=price_results)
    (data_dir / "validation_results.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8")

    has_price_failures = bool(price_results.failed)
    has_structure_failures = structure.truncated or bool(structure.sections_missing)

    if has_price_failures or has_structure_failures:
        log.error("validation FAIL", extra={
            "truncated": structure.truncated,
            "sections_missing": structure.sections_missing,
            "price_failures": len(price_results.failed)})
        sys.exit(1)
    log.info("all checks passed")


if __name__ == "__main__":
    main()
