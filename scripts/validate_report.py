#!/usr/bin/env python3
"""Validate report prices against market_snapshot.json.

Flow:
  1. Read the latest report from reports/
  2. Call Gemini (JSON mode, temp=0) to extract all stated prices → report_summary.json
  3. Compare extracted prices against market_snapshot.json within TOLERANCE
  4. Write validation_results.json; exit 1 if any ticker exceeds tolerance
"""

import json
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

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


def _load_snapshot() -> dict:
    p = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


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
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"[validate] extraction parse error: {exc}\nRaw: {response.text[:300]}", file=sys.stderr)
        return {}


# ── Structure check ───────────────────────────────────────────────────────────

def check_structure(report_text: str, report_type: str) -> dict:
    """Check required sections are present and count missing-data markers."""
    anchors = REQUIRED_SECTIONS.get(report_type, [])
    missing = [a for a in anchors if a not in report_text]
    present = len(anchors) - len(missing)

    missing_marker_count = report_text.count("⚠️ 未取得")
    total_fields_estimate = max(report_text.count("|") // 4, 1)
    missing_pct = round(missing_marker_count / total_fields_estimate * 100, 1)

    # Truncation = last anchor in the list is absent
    truncated = bool(anchors) and (anchors[-1] not in report_text)

    result = {
        "sections_total":   len(anchors),
        "sections_present": present,
        "sections_missing": missing,
        "truncated":        truncated,
        "missing_data_count": missing_marker_count,
        "missing_data_pct":   missing_pct,
        "char_count":       len(report_text),
    }

    print(f"\n{'='*56}")
    print(f"STRUCTURE CHECK: {report_type}")
    print(f"  sections     : {present}/{len(anchors)}")
    print(f"  truncated    : {'⚠️  YES' if truncated else 'no'}")
    print(f"  ⚠️ 未取得    : {missing_marker_count} occurrences (~{missing_pct}% of fields)")
    print(f"  char count   : {len(report_text):,}")
    if missing:
        print(f"  missing      : {missing}")
    print("="*56)

    return result


# ── Comparison ─────────────────────────────────────────────────────────────────

def compare(extracted: dict[str, float], snapshot: dict) -> dict:
    ground_truth: dict[str, float] = {}
    for section in ("tw_stocks", "us_markets", "forex"):
        for key, data in snapshot.get(section, {}).items():
            ground_truth[key] = data["price"]

    passed, failed, unchecked = [], [], []
    for ticker, reported in extracted.items():
        if ticker in ground_truth:
            actual = ground_truth[ticker]
            diff = abs(reported - actual) / actual if actual != 0 else 0.0
            row = {
                "ticker":    ticker,
                "reported":  reported,
                "actual":    actual,
                "diff_pct":  round(diff * 100, 2),
            }
            (passed if diff <= TOLERANCE else failed).append(row)
        else:
            unchecked.append({"ticker": ticker, "reported": reported})

    return {"passed": passed, "failed": failed, "unchecked": unchecked}


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: validate_report.py <report_type>", file=sys.stderr)
        sys.exit(1)

    report_type = sys.argv[1]
    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()

    if not os.getenv("GEMINI_API_KEY"):
        print("[validate] GEMINI_API_KEY not set — skipping", file=sys.stderr)
        sys.exit(0)

    report_text = _latest_report(report_type)
    if not report_text:
        print(f"[validate] no report found for {report_type} — skipping")
        sys.exit(0)

    # ── Structure check (no API call needed) ─────────────────────────────────
    structure = check_structure(report_text, report_type)

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    # ── Price extraction ──────────────────────────────────────────────────────
    print(f"[validate] extracting prices from report ({len(report_text):,} chars) …")
    extracted = extract_prices(report_text, model)
    print(f"[validate] found {len(extracted)} prices in report")

    (data_dir / "report_summary.json").write_text(
        json.dumps({"report_type": report_type, "extracted_prices": extracted},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    snapshot = _load_snapshot()
    if not snapshot:
        print("[validate] no market_snapshot.json — skipping price comparison")
        (data_dir / "validation_results.json").write_text(
            json.dumps({"structure": structure, "price_check": None},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.exit(1 if structure["truncated"] else 0)

    price_results = compare(extracted, snapshot)

    print(f"\n{'='*56}")
    print(f"PRICE CHECK: {report_type}")
    print(f"  ✓ pass      : {len(price_results['passed'])}")
    print(f"  ✗ fail (>{TOLERANCE*100:.0f}%): {len(price_results['failed'])}")
    print(f"  ? unchecked : {len(price_results['unchecked'])}")

    if price_results["failed"]:
        print(f"\n⚠️  Price deviations > {TOLERANCE*100:.0f}%:")
        for row in price_results["failed"]:
            print(f"  {row['ticker']:8s}  reported={row['reported']:<12}  "
                  f"actual={row['actual']:<12}  diff={row['diff_pct']}%")
    print("="*56)

    (data_dir / "validation_results.json").write_text(
        json.dumps({"structure": structure, "price_check": price_results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    has_price_failures = bool(price_results["failed"])
    has_structure_failures = structure["truncated"] or bool(structure["sections_missing"])

    if has_price_failures or has_structure_failures:
        reasons = []
        if has_structure_failures:
            reasons.append(f"structure incomplete (truncated={structure['truncated']}, "
                           f"missing={structure['sections_missing']})")
        if has_price_failures:
            reasons.append(f"{len(price_results['failed'])} price(s) exceeded {TOLERANCE*100:.0f}% tolerance")
        print(f"\n[validate] FAIL — {'; '.join(reasons)} — exit 1")
        sys.exit(1)
    else:
        print("[validate] all checks passed ✓")


if __name__ == "__main__":
    main()
