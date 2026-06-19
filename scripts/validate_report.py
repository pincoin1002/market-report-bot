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

報告（前 6000 字）：
---
{report_text[:6000]}
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

    print(f"[validate] extracting prices from report ({len(report_text):,} chars) …")
    extracted = extract_prices(report_text, model)
    print(f"[validate] found {len(extracted)} prices in report")

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    (data_dir / "report_summary.json").write_text(
        json.dumps({"report_type": report_type, "extracted_prices": extracted},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    snapshot = _load_snapshot()
    if not snapshot:
        print("[validate] no market_snapshot.json — skipping comparison")
        sys.exit(0)

    results = compare(extracted, snapshot)

    print(f"\n{'='*56}")
    print(f"VALIDATION RESULT: {report_type}")
    print(f"  ✓ pass      : {len(results['passed'])}")
    print(f"  ✗ fail (>{TOLERANCE*100:.0f}%): {len(results['failed'])}")
    print(f"  ? unchecked : {len(results['unchecked'])}")

    if results["failed"]:
        print(f"\n⚠️  Price deviations > {TOLERANCE*100:.0f}%:")
        for row in results["failed"]:
            print(f"  {row['ticker']:8s}  reported={row['reported']:<12}  "
                  f"actual={row['actual']:<12}  diff={row['diff_pct']}%")
    print("="*56)

    (data_dir / "validation_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    if results["failed"]:
        print(f"\n[validate] {len(results['failed'])} price(s) exceeded {TOLERANCE*100:.0f}% tolerance — exit 1")
        sys.exit(1)
    else:
        print("[validate] all checked prices within tolerance ✓")


if __name__ == "__main__":
    main()
