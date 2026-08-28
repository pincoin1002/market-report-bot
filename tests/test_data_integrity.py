import sys
import unittest
import os
import inspect
import subprocess
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_market_data
from generate_report import validate_portfolio_quotes, validate_private_advice_text
from instrument_registry import build_universe, resolve_instrument
from market_context import build_market_context
from market_session import classify_us_session, report_market_date, us_open_should_run
from models import PortfolioActionBrief, PortfolioActionItem, PriceReference, QuoteObservation, Snapshot, Trigger
from portfolio_context import EncryptedPortfolioProvider, PortfolioContextProvider
from providers import observation_from_daily_quote
from quote_quality import validate_observation
from structured_reports import (
    build_action_brief, build_public_draft, render_action_brief,
    validate_action_brief, validate_public_draft,
)
from validate_report import check_structure, validate_render_matches_draft
from models import Quote


NY = ZoneInfo("America/New_York")


def _obs(symbol: str, quality: str = "VALID", session: str = "REGULAR",
         price: float = 100, prev: float = 99, currency: str = "USD") -> QuoteObservation:
    now = datetime.now(tz=timezone.utc)
    return QuoteObservation(
        quote_id=f"{symbol}:2026-08-26:{session}:test",
        instrument_id=symbol,
        canonical_symbol=symbol,
        price=price,
        currency=currency,
        session=session,
        market_date="2026-08-26",
        observed_at=now,
        provider_timestamp=None,
        retrieved_at=now,
        provider="test",
        quote_type="OFFICIAL_CLOSE",
        is_delayed=True,
        quality_status=quality,
        previous_regular_close=prev,
        change_pct=round((price - prev) / prev * 100, 2),
    )


def _snapshot(observations: dict[str, QuoteObservation],
              coverage: float = 1.0) -> Snapshot:
    return Snapshot(
        generated_at=datetime.now(tz=timezone.utc),
        report_type="us_close",
        fetch_coverage=1.0,
        market_context_coverage=1.0,
        portfolio_quote_coverage=coverage,
        quote_observations=observations,
    )


class InstrumentRegistryTest(unittest.TestCase):
    def test_ambiguous_symbols_are_deterministic(self):
        goog = resolve_instrument("GOOG")
        googl = resolve_instrument("GOOGL")
        dram = resolve_instrument("DRAM")

        self.assertEqual(goog.display_name, "Alphabet Class C")
        self.assertEqual(googl.display_name, "Alphabet Class A")
        self.assertEqual(goog.economic_entity, googl.economic_entity)
        self.assertEqual(dram.asset_type, "ETF")
        self.assertEqual(dram.display_name, "Roundhill Memory ETF")

    def test_portfolio_symbols_extend_fetch_universe(self):
        portfolio = {
            "tw_positions": [{"ticker": "006208.TW", "name": "富邦台50", "shares": 130, "cost_basis": 116}],
            "us_positions": [{"ticker": "VOO", "name": "VOO", "shares": 1.4, "cost_basis": 681.94}],
        }

        universe = build_universe(portfolio)

        self.assertIn("006208", universe)
        self.assertIn("VOO", universe)
        self.assertEqual(universe["006208"].provider_symbols["yfinance"], "006208.TW")
        self.assertTrue(universe["006208"].is_portfolio_critical)
        self.assertTrue(universe["VOO"].is_portfolio_critical)


class PortfolioAdviceValidationTest(unittest.TestCase):
    def test_quote_validation_canonicalizes_tickers(self):
        raw = {
            "tw_positions": [{"ticker": "2330.TW", "name": "台積電", "shares": 1, "cost_basis": 100}],
            "us_positions": [{"ticker": "GOOG", "name": "Alphabet", "shares": 1, "cost_basis": 100}],
        }
        snapshot = _snapshot({"2330": _obs("2330"), "GOOG": _obs("GOOG")})

        ok, reason = validate_portfolio_quotes(raw, snapshot)

        self.assertTrue(ok, reason)

    def test_quote_validation_fails_closed_on_missing_or_bad_quotes(self):
        raw = {
            "tw_positions": [{"ticker": "2330", "name": "台積電", "shares": 1, "cost_basis": 100}],
            "us_positions": [{"ticker": "GOOG", "name": "Alphabet", "shares": 1, "cost_basis": 100}],
        }

        ok_missing, reason_missing = validate_portfolio_quotes(raw, _snapshot({"2330": _obs("2330")}, 0.5))
        ok_stale, reason_stale = validate_portfolio_quotes(
            raw, _snapshot({"2330": _obs("2330"), "GOOG": _obs("GOOG", "STALE")})
        )

        self.assertFalse(ok_missing)
        self.assertIn("低於 100%", reason_missing)
        self.assertFalse(ok_stale)
        self.assertIn("GOOG: STALE", reason_stale)

    def test_private_advice_rejects_exact_buy_size_without_cash(self):
        raw = {
            "available_cash": None,
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
        }
        advice = "💼 持股 Action Brief\nNVDA — ACTION_REVIEW\n建議買進 5 股。"

        ok, reason = validate_private_advice_text(advice, raw, _snapshot({"NVDA": _obs("NVDA")}))

        self.assertFalse(ok)
        self.assertIn("available_cash", reason)

    def test_private_advice_rejects_sell_quantity_above_position(self):
        raw = {
            "available_cash": 1000,
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
        }
        advice = "💼 持股 Action Brief\nNVDA — ACTION_REVIEW\n建議賣出 11 股。"

        ok, reason = validate_private_advice_text(advice, raw, _snapshot({"NVDA": _obs("NVDA")}))

        self.assertFalse(ok)
        self.assertIn("超過持股", reason)


class SnapshotBuildTest(unittest.TestCase):
    def test_snapshot_requires_portfolio_quote_coverage(self):
        raw = {
            "tw_positions": [{"ticker": "006208", "name": "富邦台50", "shares": 130, "cost_basis": 116}],
            "us_positions": [{"ticker": "AMZN", "name": "Amazon", "shares": 1, "cost_basis": 220}],
        }
        requested_symbols: list[str] = []

        def fake_fetch(symbols):
            requested_symbols.extend(symbols)
            del symbols

        with patch.object(fetch_market_data.portfolio_store, "load_portfolio", return_value=raw):
            def fake_observations(specs, expected_session):
                del expected_session
                requested_symbols.extend([s.provider_symbols["yfinance"] for s in specs])
                return {s.canonical_symbol: _obs(s.canonical_symbol, currency=s.currency) for s in specs}, {
                    s.provider_symbols["yfinance"]: "test" for s in specs
                }
            with patch.object(fetch_market_data, "fetch_session_observations", side_effect=fake_observations):
                snapshot = fetch_market_data.build_snapshot("us_close")

        self.assertIn("006208.TW", requested_symbols)
        self.assertIn("AMZN", requested_symbols)
        self.assertEqual(snapshot.portfolio_quote_coverage, 1.0)
        self.assertIn("006208", snapshot.quote_observations)
        self.assertIn("AMZN", snapshot.quote_observations)

    def test_private_portfolio_coverage_gap_does_not_block_public_fetch(self):
        source = inspect.getsource(fetch_market_data.main)
        self.assertIn("public report may proceed; private advice will block", source)
        self.assertNotIn("portfolio quote coverage below 100% — refusing to write snapshot", source)


class SessionEngineTest(unittest.TestCase):
    def test_previous_close_is_never_called_premarket_without_extended_quote(self):
        dt = datetime(2026, 8, 28, 9, 0, tzinfo=NY)
        self.assertEqual(classify_us_session(dt, extended_quote_available=False), "CLOSED_REFERENCE")

    def test_valid_premarket_observation(self):
        dt = datetime(2026, 8, 28, 9, 0, tzinfo=NY)
        self.assertEqual(classify_us_session(dt, extended_quote_available=True), "PREMARKET")

    def test_delayed_us_open_at_0959_is_regular(self):
        dt = datetime(2026, 8, 28, 9, 59, tzinfo=NY)
        self.assertEqual(classify_us_session(dt, extended_quote_available=True), "REGULAR")

    def test_dst_summer_1300_utc_guard(self):
        dt = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        self.assertTrue(us_open_should_run(dt))

    def test_winter_1400_utc_guard(self):
        dt = datetime(2026, 12, 28, 14, 0, tzinfo=timezone.utc)
        self.assertTrue(us_open_should_run(dt))

    def test_after_hours_observation_label(self):
        dt = datetime(2026, 8, 28, 16, 30, tzinfo=NY)
        self.assertEqual(classify_us_session(dt, extended_quote_available=True), "AFTER_HOURS")

    def test_new_york_holiday(self):
        dt = datetime(2026, 12, 25, 14, 0, tzinfo=timezone.utc)
        self.assertFalse(us_open_should_run(dt))

    def test_us_close_market_date_near_midnight_tpe(self):
        dt = datetime(2026, 8, 28, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(report_market_date("us_close", dt), "2026-08-27")

    def test_close_reports_expect_regular_quotes(self):
        self.assertEqual(fetch_market_data._expected_session("us_close"), "REGULAR")
        self.assertEqual(fetch_market_data._expected_session("tw_close"), "REGULAR")


class QuoteQualityTest(unittest.TestCase):
    def test_suspicious_10x_quote(self):
        spec = resolve_instrument("NVDA")
        obs = _obs("NVDA", price=1000, prev=100)
        checked = validate_observation(obs, spec)
        self.assertEqual(checked.quality_status, "SUSPECT")

    def test_corporate_action_large_move_can_be_valid(self):
        spec = resolve_instrument("NVDA")
        obs = _obs("NVDA", price=1000, prev=100)
        obs = obs.model_copy(update={"corporate_action_note": "provider adjusted for split"})
        checked = validate_observation(obs, spec)
        self.assertEqual(checked.quality_status, "VALID")

    def test_wrong_currency_blocked(self):
        spec = resolve_instrument("NVDA")
        checked = validate_observation(_obs("NVDA", currency="TWD"), spec)
        self.assertEqual(checked.quality_status, "CONFLICTING")

    def test_stale_quote_not_labeled_live(self):
        spec = resolve_instrument("NVDA")
        old = _obs("NVDA", session="REGULAR").model_copy(update={"market_date": "2020-01-01"})
        checked = validate_observation(old, spec)
        self.assertEqual(checked.quality_status, "STALE")

    def test_extreme_real_move_validated_by_second_source(self):
        spec = resolve_instrument("NVDA")
        obs = _obs("NVDA", price=1000, prev=100)
        secondary = _obs("NVDA", price=1005, prev=100, quality="VALID")
        checked = validate_observation(obs, spec, secondary=secondary)
        self.assertEqual(checked.quality_status, "VALID")

    def test_provider_failure_fallback_preserves_previous_close(self):
        spec = resolve_instrument("AMZN")
        obs = observation_from_daily_quote(
            spec, Quote(price=100, prev_close=98, change_pct=2.04, data_date="2026-08-26"),
            "test_daily", "PREVIOUS_CLOSE", datetime.now(tz=timezone.utc),
        )
        self.assertEqual(obs.session, "PREVIOUS_CLOSE")

    def test_malformed_provider_timestamp_rejected_by_schema(self):
        with self.assertRaises(Exception):
            QuoteObservation.model_validate({**_obs("NVDA").model_dump(), "provider_timestamp": "not-a-date"})


class StructuredReportTest(unittest.TestCase):
    def _context(self):
        snap = _snapshot({"NVDA": _obs("NVDA"), "GOOG": _obs("GOOG"), "TNX": _obs("TNX", currency="percent")})
        return build_market_context(snap, "us_close", run_id="test")

    def test_trigger_price_distinct_from_current_quote(self):
        context = self._context()
        draft = build_public_draft(context)
        draft.price_references.append(PriceReference(
            instrument_id="NVDA", canonical_symbol="NVDA", value=90,
            kind="ACTION_TRIGGER", quote_id=None, session=None, as_of=None,
        ))
        ok, reason = validate_public_draft(draft, context)
        self.assertTrue(ok, reason)

    def test_mismatching_price_reference_fails(self):
        context = self._context()
        draft = build_public_draft(context)
        draft.price_references[0] = draft.price_references[0].model_copy(update={"value": 101})
        ok, reason = validate_public_draft(draft, context)
        self.assertFalse(ok)
        self.assertIn("mismatch", reason)

    def test_structured_report_round_trip_render(self):
        context = self._context()
        draft = build_public_draft(context)
        self.assertIn("Executive Market State", draft.rendered_markdown)
        ok, reason = validate_public_draft(draft, context)
        self.assertTrue(ok, reason)

    def test_render_includes_all_quote_references(self):
        context = self._context()
        draft = build_public_draft(context)
        for ref in draft.price_references:
            self.assertIn(ref.canonical_symbol, draft.rendered_markdown)

    def test_optional_module_unavailable_is_marked_not_fabricated(self):
        context = self._context()
        draft = build_public_draft(context)
        self.assertTrue(any(m.state in ("PARTIAL", "UNAVAILABLE") for m in draft.optional_modules))

    def test_market_context_delta_generation(self):
        context = self._context()
        draft = build_public_draft(context)
        self.assertLessEqual(len(draft.material_changes), 5)


class StructuredBriefTest(unittest.TestCase):
    def _portfolio(self):
        return EncryptedPortfolioProvider().load()

    def test_no_material_event_becomes_no_material_change(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
            "tw_positions": [],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        brief = build_action_brief(context, portfolio)
        self.assertEqual(brief.no_material_change[0].status, "NO_MATERIAL_CHANGE")

    def test_unsupported_numeric_trigger_rejected(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        item = PortfolioActionItem(
            instrument_id="NVDA", ticker="NVDA", status="WATCH", quote_id=context.quotes["NVDA"].quote_id,
            reference_price=100, session="REGULAR", as_of=datetime.now(tz=timezone.utc),
            trigger=Trigger(trigger_type="TECHNICAL", condition="below 95", numeric_value=95,
                            basis="LLM intuition", generated_by="LLM", source_ids=[]),
        )
        brief = PortfolioActionBrief(run_id="test", as_of=datetime.now(tz=timezone.utc),
                                     market_session="REGULAR", watchlist=[item])
        ok, reason = validate_action_brief(brief, context, portfolio)
        self.assertFalse(ok)
        self.assertIn("unsupported trigger", reason)

    def test_deterministic_trigger_allowed_with_provenance(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA", price=92, prev=100)}), "us_close", run_id="test")
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        brief = build_action_brief(context, portfolio)
        ok, reason = validate_action_brief(brief, context, portfolio)
        self.assertTrue(ok, reason)

    def test_structured_private_brief_round_trip_render(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "NVDA", "name": "NVIDIA", "shares": 10, "cost_basis": 100}],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        brief = build_action_brief(context, portfolio)
        rendered = render_action_brief(brief)
        self.assertIn("NO MATERIAL CHANGE", rendered)
        self.assertNotRegex(rendered, r"(買進|加碼|賣出|減碼)\s*\d+")

    def test_unknown_instrument_requires_data_blocked(self):
        context = build_market_context(_snapshot({}), "us_close", run_id="test")
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "ZZZZ", "name": "Unknown", "shares": 10, "cost_basis": 100}],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        brief = build_action_brief(context, portfolio)
        self.assertEqual(brief.data_issues[0], "ZZZZ: quote unavailable or invalid")

    def test_public_valid_private_invalid_split(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        draft = build_public_draft(context)
        self.assertTrue(validate_public_draft(draft, context)[0])
        with patch("portfolio_store.load_portfolio", return_value={
            "us_positions": [{"ticker": "GOOG", "name": "Alphabet", "shares": 1, "cost_basis": 100}],
        }):
            portfolio = EncryptedPortfolioProvider().load()
        brief = build_action_brief(context, portfolio)
        self.assertTrue(brief.data_issues)

    def test_encrypted_provider_satisfies_interface(self):
        provider: PortfolioContextProvider = EncryptedPortfolioProvider()
        with patch("portfolio_store.load_portfolio", return_value={"us_positions": []}):
            self.assertEqual(provider.load().positions, [])


class SafetyAndWorkflowTest(unittest.TestCase):
    def test_validation_failure_prevents_delivery_state(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        draft = build_public_draft(context)
        draft.price_references[0] = draft.price_references[0].model_copy(update={"quote_id": "bad"})
        self.assertFalse(validate_public_draft(draft, context)[0])

    def test_google_search_failure_degraded_phrase(self):
        from generate_report import _verified_data_only_report
        text = _verified_data_only_report("SNAPSHOT", "us_open")
        self.assertIn("新聞搜尋目前不可用", text)

    def test_portfolio_plaintext_ignored_by_git(self):
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "portfolio.json"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(ignored, "portfolio.json")

    def test_data_artifacts_ignored_by_git(self):
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "data/portfolio_action_brief.json"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(ignored, "data/portfolio_action_brief.json")

    def test_idempotent_same_day_delivery(self):
        import delivery_state
        with tempfile.TemporaryDirectory() as td:
            old = delivery_state.STATE_PATH
            delivery_state.STATE_PATH = Path(td) / "delivery_state.json"
            try:
                delivery_state.mark_state("us_open:20260828", "DELIVERED")
                self.assertTrue(delivery_state.already_delivered("us_open:20260828"))
            finally:
                delivery_state.STATE_PATH = old

    def test_rendered_validation_detects_missing_symbol(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        draft = build_public_draft(context)
        ok, reason = validate_render_matches_draft("no ticker here", draft)
        self.assertFalse(ok)
        self.assertIn("NVDA", reason)

    def test_v2_structure_accepts_short_high_signal_report(self):
        text = "\n".join([
            "# report", "## 1. Executive Market State",
            "## 2. What Changed Since Last Report", "## 3. Top Market Drivers",
            "## 4. Rotation / Regime", "## 6. Watch Into Close / Next Report",
        ])
        result = check_structure(text, "us_open")
        self.assertFalse(result.truncated)
        self.assertFalse(result.sections_missing)

    def test_workflow_generation_precedes_validation_and_delivery(self):
        text = (ROOT / ".github/workflows/us-open.yml").read_text(encoding="utf-8")
        self.assertLess(text.index("Generate US Open Report"), text.index("Validate report prices"))
        self.assertLess(text.index("Validate report prices"), text.index("Deliver validated US Open Report"))

    def test_generate_only_does_not_use_duplicate_report_skip(self):
        text = (ROOT / "scripts/generate_report.py").read_text(encoding="utf-8")
        self.assertIn("if not args.generate_only and check_report_already_generated(report_type):", text)

    def test_delivery_skips_same_day_duplicate_before_send(self):
        import generate_report

        source = inspect.getsource(generate_report.main)
        self.assertIn("same-day report already existed before this run", source)
        self.assertIn("skipping duplicate delivery", source)

    def test_critical_workflow_validation_is_blocking(self):
        for name in ("tw-open.yml", "tw-close.yml", "us-open.yml", "us-close.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            block = text[text.index("Validate report prices"):text.index("Deliver validated")]
            self.assertNotIn("continue-on-error: true", block)

    def test_dual_cron_only_correct_utc_window_runs(self):
        summer_wrong = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        winter_wrong = datetime(2026, 12, 28, 13, 0, tzinfo=timezone.utc)
        self.assertFalse(us_open_should_run(summer_wrong))
        self.assertFalse(us_open_should_run(winter_wrong))

    def test_action_trigger_is_not_quote_compared(self):
        context = build_market_context(_snapshot({"NVDA": _obs("NVDA")}), "us_close", run_id="test")
        draft = build_public_draft(context)
        draft.price_references = [PriceReference(
            instrument_id="NVDA", canonical_symbol="NVDA", value=1,
            kind="ACTION_TRIGGER", quote_id="not-a-quote-id", session="REGULAR",
            as_of=datetime.now(tz=timezone.utc),
        )]
        ok, reason = validate_public_draft(draft, context)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
