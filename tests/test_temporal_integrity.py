#!/usr/bin/env python3
"""Regression test suite for ADR / market-data temporal alignment and deterministic contracts."""

import unittest
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from adr_engine import calculate_tsm_adr_premium
from instrument_registry import resolve_instrument
from market_session import (
    get_most_recent_completed_session,
    get_target_market_date,
    is_nyse_trading_day,
    is_tw_trading_day,
    NY,
    TPE,
)
from models import InstrumentSpec, NamedQuote, QuoteObservation, Snapshot
from quote_quality import validate_observation
from structured_reports import build_public_draft, validate_public_draft
from market_context import build_market_context


def _make_obs(
    symbol: str,
    market_date: str,
    price: float = 100.0,
    prev_close: float = 99.0,
    session: str = "PREVIOUS_CLOSE",
    quality: str = "VALID",
    currency: str = "USD",
    provider: str = "test_provider",
    retrieved_at: datetime | None = None,
    market: str = "US",
) -> QuoteObservation:
    now = retrieved_at or datetime.now(tz=timezone.utc)
    return QuoteObservation(
        quote_id=f"{symbol}:{market_date}:{session}:{provider}",
        instrument_id=symbol,
        canonical_symbol=symbol,
        price=price,
        currency=currency,
        session=session,
        market_date=market_date,
        observed_at=now,
        provider_timestamp=now,
        retrieved_at=now,
        provider=provider,
        quote_type="OFFICIAL_CLOSE",
        is_delayed=True,
        quality_status=quality,
        previous_regular_close=prev_close,
        change_pct=round((price - prev_close) / prev_close * 100, 2),
        market=market,
    )


class TemporalIntegrityRegressionTest(unittest.TestCase):
    """Regression test suite for temporal contracts A through H and generalization."""

    # ── Test A: Target US close 2026-09-08, result from 2026-09-04 -> reject DATE_MISMATCH
    def test_A_target_us_close_mismatch_rejected(self):
        spec = resolve_instrument("TSM")
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        stale_obs = _make_obs("TSM", market_date="2026-09-04", price=165.0, retrieved_at=retrieved, market="US")
        
        validated = validate_observation(stale_obs, spec, expected_date="2026-09-08")
        
        self.assertEqual(validated.quality_status, "DATE_MISMATCH")
        self.assertTrue(any("does not match expected session date 2026-09-08" in n for n in validated.quality_notes))

    # ── Test B: Correct 2026-09-08 close -> accept
    def test_B_correct_us_close_accepted(self):
        spec = resolve_instrument("TSM")
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        valid_obs = _make_obs("TSM", market_date="2026-09-08", price=170.0, retrieved_at=retrieved, market="US")
        
        validated = validate_observation(valid_obs, spec, expected_date="2026-09-08")
        
        self.assertEqual(validated.quality_status, "VALID")
        self.assertEqual(validated.market_date, "2026-09-08")

    # ── Test C: Monday Taiwan report after US Friday -> correctly use Friday close
    def test_C_monday_tw_report_uses_friday_us_close(self):
        # 2026-09-14 is Monday. 07:50 TPE is Sunday 2026-09-13 19:50 NY.
        monday_morning_tpe = datetime(2026, 9, 14, 7, 50, tzinfo=TPE)
        
        us_target_date = get_target_market_date("tw_open", "US", now=monday_morning_tpe)
        self.assertEqual(us_target_date, "2026-09-11")  # Friday
        
        tw_target_date = get_target_market_date("tw_open", "TW", now=monday_morning_tpe)
        self.assertEqual(tw_target_date, "2026-09-11")  # Friday TW close

    # ── Test D: US holiday -> use previous actual completed market session
    def test_D_us_holiday_resolves_to_actual_completed_session(self):
        # 2026-09-07 was Labor Day (NYSE holiday).
        # Report runs Tuesday 2026-09-08 07:50 TPE (Monday evening 2026-09-07 19:50 EDT).
        tuesday_morning_tpe = datetime(2026, 9, 8, 7, 50, tzinfo=TPE)
        target_date = get_target_market_date("tw_open", "US", now=tuesday_morning_tpe)
        # Labor day was closed, so previous completed US regular close was Friday 2026-09-04
        self.assertEqual(target_date, "2026-09-04")

        # Also test after-midnight Taipei:
        # Report runs Wednesday 2026-09-09 01:00 TPE (Tuesday 2026-09-08 13:00 EDT).
        # At 13:00 EDT, Tuesday session is NOT completed yet (< 16:00 EDT).
        # Completed regular session must still be Friday 2026-09-04 (since Monday was holiday).
        wed_0100_tpe = datetime(2026, 9, 9, 1, 0, tzinfo=TPE)
        target_after_midnight = get_target_market_date("tw_open", "US", now=wed_0100_tpe)
        self.assertEqual(target_after_midnight, "2026-09-04")

        # But at 08:00 TPE on Wednesday 2026-09-09 (Tuesday 2026-09-08 20:00 EDT):
        # Tuesday regular session completed at 16:00 EDT!
        wed_0800_tpe = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        target_morning = get_target_market_date("tw_open", "US", now=wed_0800_tpe)
        self.assertEqual(target_morning, "2026-09-08")

    # ── Test E: Conflicting search result date vs deterministic quote -> deterministic quote/date wins
    def test_E_deterministic_contract_wins_over_conflict(self):
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        tsm_obs = _make_obs("TSM", market_date="2026-09-08", price=170.0, retrieved_at=retrieved)
        snap = Snapshot(
            generated_at=retrieved,
            report_type="tw_open",
            quote_observations={"TSM": tsm_obs},
        )
        context = build_market_context(snap, "tw_open", run_id="test")
        draft = build_public_draft(context)
        
        # Valid draft has verified deterministic quote
        ok, reason = validate_public_draft(draft, context)
        self.assertTrue(ok, reason)
        self.assertEqual(draft.price_references[0].value, 170.0)
        self.assertEqual(draft.price_references[0].quote_id, tsm_obs.quote_id)

    # ── Test F: Stale TSM quote -> ADR premium suppressed with DATA_BLOCKED — TEMPORAL_MISMATCH
    def test_F_stale_tsm_quote_suppresses_adr_premium(self):
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        # Stale TSM from 2026-09-04 when target is 2026-09-08
        tsm_obs = _make_obs("TSM", market_date="2026-09-04", price=165.0, quality="DATE_MISMATCH", retrieved_at=retrieved)
        tw_obs = _make_obs("2330", market_date="2026-09-08", price=1000.0, currency="TWD", market="TW", retrieved_at=retrieved)
        fx_obs = _make_obs("USDTWD", market_date="2026-09-09", price=32.0, currency="TWD", market="TW", retrieved_at=retrieved)

        ok, rendered, payload = calculate_tsm_adr_premium(
            tsm_obs=tsm_obs,
            tw_obs=tw_obs,
            fx_obs=fx_obs,
            target_us_date="2026-09-08",
            target_tw_date="2026-09-08",
        )

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "DATA_BLOCKED")
        self.assertIn("DATA_BLOCKED — TEMPORAL_MISMATCH", rendered)
        self.assertIn("TSM ADR date mismatch", rendered)
        self.assertNotIn("溢折價率：", rendered)

    # ── Test G: Valid TSM + 2330 + FX timestamps -> premium calculated with exact dates
    def test_G_valid_tsm_premium_calculated(self):
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        # 1 TSM ADR ($170) = 5 common shares (2330 @ 1000 TWD) at FX 32.00
        # Share TWD price = (170 * 32.0) / 5 = 1088 TWD
        # Premium = (1088 - 1000) / 1000 = +8.80%
        tsm_obs = _make_obs("TSM", market_date="2026-09-08", price=170.0, quality="VALID", retrieved_at=retrieved)
        tw_obs = _make_obs("2330", market_date="2026-09-08", price=1000.0, currency="TWD", market="TW", retrieved_at=retrieved)
        fx_obs = _make_obs("USDTWD", market_date="2026-09-09", price=32.0, currency="TWD", market="TW", retrieved_at=retrieved)

        ok, rendered, payload = calculate_tsm_adr_premium(
            tsm_obs=tsm_obs,
            tw_obs=tw_obs,
            fx_obs=fx_obs,
            target_us_date="2026-09-08",
            target_tw_date="2026-09-08",
        )

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "VALID")
        self.assertEqual(payload["premium_pct"], 8.8)
        self.assertEqual(payload["ratio"], 5.0)
        self.assertEqual(payload["tsm_twd_per_share"], 1088.0)
        
        # Verify exact dates printed in output
        self.assertIn("TSM ADR:\n  2026-09-08 US close ($170.00)", rendered)
        self.assertIn("2330:\n  2026-09-08 TW close (1,000.0 TWD)", rendered)
        self.assertIn("USD/TWD:\n  2026-09-09 08:00 TPE (32.0000)", rendered)
        self.assertIn("溢折價率：+8.80%", rendered)

    # ── Test H: No quote may be labelled PREVIOUS_CLOSE without explicit trading_date proof
    def test_H_no_quote_labelled_previous_close_without_trading_date_proof(self):
        spec = resolve_instrument("TSM")
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        
        # Quote with old/unverified market_date
        obs = _make_obs("TSM", market_date="2026-09-01", price=160.0, session="PREVIOUS_CLOSE", retrieved_at=retrieved)
        
        # Without proof matching target completed session (2026-09-08), it cannot remain VALID
        validated = validate_observation(obs, spec, expected_date="2026-09-08")
        self.assertNotEqual(validated.quality_status, "VALID")
        self.assertEqual(validated.quality_status, "DATE_MISMATCH")

    # ── Section 7: Generalization across S&P 500, NASDAQ, Dow, SOX, TSM, UMC, ASX
    def test_generalization_all_key_instruments_validated(self):
        test_symbols = ["SPX", "NDX", "DJI", "SOX", "TSM", "UMC", "ASX"]
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        target_us_date = "2026-09-08"

        for sym in test_symbols:
            spec = resolve_instrument(sym)
            self.assertEqual(spec.market, "US", f"{sym} should be configured with market US")
            
            # Stale quote from earlier session
            stale = _make_obs(sym, market_date="2026-09-04", price=100.0, currency=spec.currency, retrieved_at=retrieved, market="US")
            val_stale = validate_observation(stale, spec, expected_date=target_us_date)
            self.assertEqual(
                val_stale.quality_status, "DATE_MISMATCH",
                f"{sym} stale quote was not rejected as DATE_MISMATCH"
            )
            
            # Valid quote matching target session
            valid = _make_obs(sym, market_date="2026-09-08", price=100.0, currency=spec.currency, retrieved_at=retrieved, market="US")
            val_valid = validate_observation(valid, spec, expected_date=target_us_date)
            self.assertEqual(
                val_valid.quality_status, "VALID",
                f"{sym} matching quote was not accepted as VALID"
            )

    # ── Synthetic TW_OPEN report proving stale ADR data is rejected
    def test_synthetic_tw_open_stale_adr_rejected(self):
        from generate_report import _build_snapshot_block
        retrieved = datetime(2026, 9, 9, 8, 0, tzinfo=TPE)
        tsm_stale = _make_obs("TSM", market_date="2026-09-04", price=165.0, quality="DATE_MISMATCH", retrieved_at=retrieved)
        tw_obs = _make_obs("2330", market_date="2026-09-08", price=1000.0, currency="TWD", market="TW", retrieved_at=retrieved)
        fx_obs = _make_obs("USDTWD", market_date="2026-09-09", price=32.0, currency="TWD", market="TW", retrieved_at=retrieved)
        spx_obs = _make_obs("SPX", market_date="2026-09-08", price=5500.0, currency="", market="US", retrieved_at=retrieved)
        
        snapshot = Snapshot(
            generated_at=retrieved,
            report_type="tw_open",
            fetch_coverage=1.0,
            market_context_coverage=0.75,
            sources={"TSM": "test", "2330": "test", "USDTWD": "test", "SPX": "test"},
            tw_stocks={"2330": NamedQuote(name="台積電", currency="TWD", symbol="2330.TW", price=1000.0, prev_close=990.0, change_pct=1.01, data_date="2026-09-08")},
            us_markets={"SPX": NamedQuote(name="S&P 500", currency="", symbol="^GSPC", price=5500.0, prev_close=5480.0, change_pct=0.36, data_date="2026-09-08")},
            forex={"USDTWD": NamedQuote(name="USD/TWD", currency="TWD", symbol="TWD=X", price=32.0, prev_close=32.1, change_pct=-0.31, data_date="2026-09-09")},
            quote_observations={"TSM": tsm_stale, "2330": tw_obs, "USDTWD": fx_obs, "SPX": spx_obs},
            data_quality={"TSM": "DATE_MISMATCH"},
        )
        
        block = _build_snapshot_block(snapshot)
        self.assertIn("DATA_BLOCKED — TEMPORAL_MISMATCH", block)
        self.assertIn("TSM ADR date mismatch (got 2026-09-04, expected 2026-09-08 US close)", block)
        self.assertIn("數據阻斷清單（DATA_BLOCKED", block)
        self.assertIn("- **TSM**: DATE_MISMATCH", block)
        # TSM must not appear in verified pricing tables before Quote Semantics
        pricing_table_part = block.split("**Quote Semantics")[0]
        self.assertNotIn("| TSM |", pricing_table_part)


if __name__ == "__main__":
    unittest.main()
