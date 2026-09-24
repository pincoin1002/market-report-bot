import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from market_context import build_market_context
from models import PortfolioQuoteCoverage, QuoteObservation, Snapshot
from structured_reports import build_public_draft
from validate_report import validate_numeric_provenance, validate_rendered_report_structure


class September24TrialOutputTest(unittest.TestCase):
    def test_regenerated_20260924_report(self):
        retrieved = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
        values = {
            "TAIEX": (48024.6, 48157.29, -0.28),
            "2330": (2475.0, 2500.0, -1.00),
            "2317": (250.5, 256.0, -2.15),
            "2454": (5285.0, 5185.0, 1.93),
            "2308": (1910.0, 1900.0, 0.53),
            "2382": (338.5, 341.5, -0.88),
            "2303": (154.0, 160.0, -3.75),
            "3711": (699.0, 693.0, 0.87),
            "2356": (59.9, 61.0, -1.80),
            "3231": (184.5, 180.0, 2.50),
            "2383": (5050.0, 5020.0, 0.60),
            "4958": (475.5, 473.0, 0.53),
            "USDTWD": (31.85, 31.68, 0.53),
        }
        observations = {}
        for symbol, (price, previous, change) in values.items():
            observations[symbol] = QuoteObservation(
                quote_id=f"{symbol}:2026-09-24:REGULAR:fixture",
                instrument_id=symbol,
                canonical_symbol=symbol,
                price=price,
                currency="TWD",
                session="REGULAR",
                market_date="2026-09-24",
                observed_at=retrieved,
                provider_timestamp=None,
                retrieved_at=retrieved,
                provider="fixture-20260924",
                quote_type="OFFICIAL_CLOSE",
                is_delayed=True,
                quality_status="VALID",
                previous_regular_close=previous,
                change_pct=change,
                market="TW",
            )

        snapshot = Snapshot(
            generated_at=retrieved,
            report_type="tw_close",
            report_market_date="2026-09-24",
            portfolio_source="PIOS_PORTFOLIO_SNAPSHOT",
            quote_observations=observations,
            portfolio_quote_coverage=PortfolioQuoteCoverage(
                expected_positions=23,
                covered_positions=23,
                coverage_ratio=1.0,
                as_of=retrieved,
                status="FULL",
            ),
        )
        context = build_market_context(snapshot, "tw_close", run_id="trial-20260924", now=retrieved)
        rendered = build_public_draft(context).rendered_markdown

        struct_ok, struct_errors = validate_rendered_report_structure(rendered, "tw_close")
        numeric_ok, numeric_errors = validate_numeric_provenance(rendered, context)
        self.assertTrue(struct_ok, struct_errors)
        self.assertTrue(numeric_ok, numeric_errors)
        self.assertIn("持股行情覆蓋完整（23/23）", rendered)
        self.assertIn("OBSERVED — 權值壓力", rendered)
        self.assertIn("上一完成交易日", rendered)
        self.assertNotIn("46,000", rendered)
        self.assertEqual(rendered.count("# 台股收盤日報"), 1)

        print("\n=== REGENERATED_2026-09-24_REPORT ===")
        print(rendered)
        print("=== END_REGENERATED_2026-09-24_REPORT ===\n")


if __name__ == "__main__":
    unittest.main()
