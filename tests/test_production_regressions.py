"""Production-shaped regressions for the 2026-09-29 incident classes."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fetch_market_data import build_portfolio_quote_coverage
from instrument_registry import REGISTRY, resolve_instrument
from market_session import (
    NY, TPE, get_most_recent_completed_session, get_previous_completed_session_date,
    get_target_market_date, is_nyse_trading_day, is_tw_trading_day,
    us_open_intended_datetime, us_open_snapshot_contract_status, us_open_wait_seconds,
)
from models import (
    InstitutionalFlows, MarketContext, MarketReportDraft, NamedQuote,
    PortfolioContext, PositionContext, PortfolioQuoteCoverage, Quote,
    QuoteObservation, TaiexMarketSummary,
)
from portfolio_context import load_authoritative_portfolio
from providers import CoinbaseCryptoProvider, _quote_from_closes
from scheduler.us_open_dispatch import (
    handle_cron_request, scheduler_decision, should_dispatch, send_dispatch_failure_alert,
)
from send_operational_alert import already_alerted, record_alert, send_telegram_alert
from structured_reports import (
    _reader_evidence_line, _render_tw_close_report, build_public_draft,
    derive_evidence_supported_drivers, derive_tomorrow_watch_signals,
    derive_tw_session_deltas, portfolio_report_section, return_direction,
)
from twse_market_evidence import TWSECloseEvidence, fetch_twse_close_evidence
from validate_report import validate_rendered_report_structure

TEST_PORTFOLIO_TICKERS = [
    "0050", "006208", "1519", "2327", "2330", "2383",
    "AMZN", "DRAM", "GOOG", "IBKR", "MU", "NVDA", "QQQ", "TSLA", "VOO", "VST", "VTI",
    "BTC", "ETH", "USDC", "USDT", "BONK", "SXT",
]


def _deterministic_test_portfolio() -> PortfolioContext:
    """Privacy-safe 23-position fixture for CI; never depends on the private PIOS snapshot."""
    positions = []
    for idx, ticker in enumerate(TEST_PORTFOLIO_TICKERS, start=1):
        spec = resolve_instrument(ticker)
        positions.append(PositionContext(
            position_id=f"test-{idx:02d}-{ticker}",
            instrument_id=ticker,
            ticker=ticker,
            name=spec.display_name,
            quantity=float(idx),
            currency=spec.currency,
            asset_type=spec.asset_type,
        ))
    return PortfolioContext(source="CI_SYNTHETIC_FIXTURE", positions=positions)



class USOpenSchedulerContractSuiteTest(unittest.TestCase):
    """Tests 1-11: US Scheduler contracts, timing semantics, and operational failure alerts."""

    def test_01_edt_valid_early_staging_trigger(self):
        # 1. EDT valid early staging trigger (08:00 New York)
        now_edt = datetime(2026, 9, 29, 8, 15, tzinfo=NY)
        self.assertTrue(should_dispatch("edt", now_edt))
        self.assertEqual(scheduler_decision("edt", now_edt), "DISPATCH")

    def test_02_est_valid_early_staging_trigger(self):
        # 2. EST valid early staging trigger (08:15 New York in December)
        now_est = datetime(2026, 12, 29, 8, 15, tzinfo=NY)
        self.assertTrue(should_dispatch("est", now_est))
        self.assertEqual(scheduler_decision("est", now_est), "DISPATCH")

    def test_03_worst_case_delayed_hobby_invocation_before_canonical_snapshot(self):
        # 3. Worst-case delayed Hobby invocation under new 0 12 / 0 13 schedule (08:59:59 NY)
        # Invocation anywhere during 08:00-08:59 NY is still strictly BEFORE 09:05 NY canonical snapshot
        worst_case = datetime(2026, 9, 29, 8, 59, 59, tzinfo=NY)
        self.assertTrue(should_dispatch("edt", worst_case))
        canonical = us_open_intended_datetime(worst_case)
        self.assertLess(worst_case, canonical)
        self.assertEqual(canonical.time().strftime("%H:%M"), "09:05")

    def test_04_delayed_invocation_after_allowed_window_no_normal_dispatch(self):
        # 4. Delayed invocation after allowed window -> no normal dispatch
        # at 09:00:00 or 09:44 EDT
        after_window = datetime(2026, 9, 29, 9, 0, 0, tzinfo=NY)
        late_run = datetime(2026, 9, 29, 9, 44, 45, tzinfo=NY)
        self.assertFalse(should_dispatch("edt", after_window))
        self.assertEqual(scheduler_decision("edt", after_window), "OUTSIDE_STAGING_WINDOW")
        self.assertFalse(should_dispatch("edt", late_run))
        self.assertEqual(scheduler_decision("edt", late_run), "OUTSIDE_STAGING_WINDOW")

        # Must dispatch only terminal alert workflow, never normal report
        with patch("scheduler.us_open_dispatch.dispatch_workflow", return_value=204) as dispatch, patch.dict(
            os.environ,
            {"CRON_SECRET": "cron", "GITHUB_WORKFLOW_DISPATCH_TOKEN": "token"},
            clear=False,
        ):
            status, payload = handle_cron_request("edt", "Bearer cron", now=late_run)
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "SCHEDULER_WINDOW_EXPIRED")
        submitted = dispatch.call_args.args[1]
        self.assertEqual(submitted["inputs"]["scheduler_terminal_state"], "SCHEDULER_WINDOW_EXPIRED")

    def test_05_wrong_dst_slot_ignored(self):
        # 5. Wrong DST slot -> ignore
        # In summer/autumn (EDT), EST slot must be ignored
        now_edt = datetime(2026, 9, 29, 8, 15, tzinfo=NY)
        self.assertFalse(should_dispatch("est", now_edt))
        self.assertEqual(scheduler_decision("est", now_edt), "IGNORED_DST_SLOT")

    def test_06_weekend_market_closed(self):
        # 6. Weekend -> MARKET_CLOSED/no normal report
        saturday = datetime(2026, 10, 3, 8, 15, tzinfo=NY)
        self.assertFalse(should_dispatch("edt", saturday))
        self.assertEqual(scheduler_decision("edt", saturday), "MARKET_CLOSED")

    def test_07_nyse_holiday_market_closed(self):
        # 7. NYSE holiday -> MARKET_CLOSED/no normal report (e.g. 2026-07-03 Independence Day observed)
        holiday = datetime(2026, 7, 3, 8, 15, tzinfo=NY)
        self.assertFalse(should_dispatch("edt", holiday))
        self.assertEqual(scheduler_decision("edt", holiday), "MARKET_CLOSED")

    def test_08_canonical_0905_snapshot_pinned(self):
        # 8. Canonical 09:05 snapshot
        t1 = datetime(2026, 9, 29, 8, 5, tzinfo=NY)
        t2 = datetime(2026, 9, 29, 8, 50, tzinfo=NY)
        self.assertEqual(us_open_intended_datetime(t1).strftime("%H:%M"), "09:05")
        self.assertEqual(us_open_intended_datetime(t2).strftime("%H:%M"), "09:05")

    def test_09_at_or_after_0930_can_never_become_normal_us_open(self):
        # 9. >=09:30 can never become normal US-open
        at_open = datetime(2026, 9, 29, 9, 30, 0, tzinfo=NY)
        after_open = datetime(2026, 9, 29, 9, 45, 0, tzinfo=NY)
        self.assertEqual(us_open_snapshot_contract_status(at_open), "INTENT_EXPIRED")
        self.assertEqual(us_open_snapshot_contract_status(after_open), "INTENT_EXPIRED")

    def test_10_duplicate_external_native_triggers_one_delivery(self):
        # 10. Duplicate external/native triggers -> one delivery guard
        import us_open_intent
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()
            (reports_dir / "us_open_20260929_090500.md").write_text("# Report", encoding="utf-8")
            with patch.object(us_open_intent, "ROOT", Path(tmpdir)):
                self.assertTrue(us_open_intent.has_existing_report("2026-09-29"))
                self.assertFalse(us_open_intent.has_existing_report("2026-09-30"))

    def test_11_failure_operational_alert_exactly_once(self):
        # 11. Failure operational alert -> exactly once
        import send_operational_alert
        with tempfile.TemporaryDirectory() as tmpdir:
            alerts_dir = Path(tmpdir) / "reports" / ".alerts"
            with patch.object(send_operational_alert, "ALERTS_DIR", alerts_dir):
                self.assertFalse(send_operational_alert.already_alerted("us_open", "2026-09-29"))

                # First alert records marker
                send_operational_alert.record_alert("us_open", "2026-09-29", "INTENT_EXPIRED")
                self.assertTrue(send_operational_alert.already_alerted("us_open", "2026-09-29"))

                # Second attempt detects existing marker and skips
                mock_send = Mock(return_value=True)
                with patch("send_operational_alert.send_telegram_alert", mock_send):
                    with patch("sys.argv", ["send_operational_alert.py", "us_open", "INTENT_EXPIRED"]):
                        with patch.dict(os.environ, {"US_OPEN_INTENDED_MARKET_DATE": "2026-09-29"}):
                            with self.assertRaises(SystemExit) as cm:
                                send_operational_alert.main()
                            self.assertEqual(cm.exception.code, 0)
                            mock_send.assert_not_called()

    def test_dispatch_failed_sends_telegram_alert(self):
        # Operational alert on DISPATCH_FAILED in Vercel
        with patch("scheduler.us_open_dispatch.dispatch_workflow", side_effect=RuntimeError("GitHub API down")), patch(
            "scheduler.us_open_dispatch.send_dispatch_failure_alert"
        ) as mock_alert, patch.dict(
            os.environ,
            {"CRON_SECRET": "cron", "GITHUB_WORKFLOW_DISPATCH_TOKEN": "token"},
            clear=False,
        ):
            status, payload = handle_cron_request("edt", "Bearer cron", now=datetime(2026, 9, 29, 8, 15, tzinfo=NY))
            self.assertEqual(status, 502)
            self.assertEqual(payload["status"], "DISPATCH_FAILED")
            mock_alert.assert_called_once()


class PortfolioCrossMarketExhaustiveTest(unittest.TestCase):
    """Tests 12-19: Cross-market portfolio session contracts and diagnostic accounting."""

    def test_12_tw_close_before_us_open_previous_completed_us_session_valid(self):
        # 12. Taiwan close before US open -> previous completed US session valid
        # At 13:30 TPE (01:30 EDT), US market for 2026-09-29 has not opened.
        tw_close_time = datetime(2026, 9, 29, 13, 30, tzinfo=TPE)
        target_us = get_target_market_date("tw_close", "US", now=tw_close_time)
        self.assertEqual(target_us, "2026-09-28")

    def test_13_tw_close_during_current_us_session_previous_close_remains_authority(self):
        # 13. Taiwan-close workflow executes during current US session -> previous completed official US session remains valuation authority
        # At 21:54 TPE (09:54 EDT), 2026-09-29 US session is live and incomplete.
        late_tw_close = datetime(2026, 9, 29, 21, 54, tzinfo=TPE)
        target_us = get_target_market_date("tw_close", "US", now=late_tw_close)
        self.assertEqual(target_us, "2026-09-28")

    def test_14_current_session_live_quote_does_not_block_official_close_fallback(self):
        # 14. Current-session live quote does not block official-close fallback
        # When yfinance returns 10d including live bar 2026-09-29, _quote_from_closes pins to 2026-09-28
        quote = _quote_from_closes([
            ("2026-09-24", 98.0),
            ("2026-09-25", 99.0),
            ("2026-09-28", 100.0),
            ("2026-09-29", 105.0),  # live bar
        ], expected_date="2026-09-28")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.data_date, "2026-09-28")
        self.assertEqual(quote.price, 100.0)

    def test_15_old_completed_session_rejected(self):
        # 15. Old completed session -> reject
        pos = PositionContext(position_id="us-aapl", instrument_id="AAPL", ticker="AAPL", name="Apple", quantity=1.0, currency="USD", asset_type="EQUITY")
        context = PortfolioContext(positions=[pos], source="test")
        spec = resolve_instrument("AAPL")
        obs_old = QuoteObservation(
            quote_id="AAPL:2026-09-25:REGULAR:yfinance",
            instrument_id="AAPL", canonical_symbol="AAPL", price=150.0, currency="USD",
            previous_regular_close=148.0, change_pct=1.35,
            session="REGULAR", market_date="2026-09-25", observed_at=datetime(2026, 9, 25, 16, 0, tzinfo=NY),
            retrieved_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE), provider="yfinance",
            quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID", market="US",
        )
        cov = build_portfolio_quote_coverage(
            context, {"AAPL": obs_old}, {"AAPL": spec},
            datetime(2026, 9, 29, 13, 30, tzinfo=TPE), {"US": "2026-09-28"},
        )
        self.assertEqual(cov.covered_positions, 0)
        self.assertEqual(len(cov.stale), 1)
        self.assertIn("differs from expected 2026-09-28", cov.stale[0].reason)

    def test_16_future_session_rejected(self):
        # 16. Future session -> reject
        pos = PositionContext(position_id="us-aapl", instrument_id="AAPL", ticker="AAPL", name="Apple", quantity=1.0, currency="USD", asset_type="EQUITY")
        context = PortfolioContext(positions=[pos], source="test")
        spec = resolve_instrument("AAPL")
        obs_future = QuoteObservation(
            quote_id="AAPL:2026-09-30:REGULAR:yfinance",
            instrument_id="AAPL", canonical_symbol="AAPL", price=150.0, currency="USD",
            previous_regular_close=148.0, change_pct=1.35,
            session="REGULAR", market_date="2026-09-30", observed_at=datetime(2026, 9, 30, 16, 0, tzinfo=NY),
            retrieved_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE), provider="yfinance",
            quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID", market="US",
        )
        cov = build_portfolio_quote_coverage(
            context, {"AAPL": obs_future}, {"AAPL": spec},
            datetime(2026, 9, 29, 13, 30, tzinfo=TPE), {"US": "2026-09-28"},
        )
        self.assertEqual(cov.covered_positions, 0)
        self.assertEqual(len(cov.stale), 1)

    def test_17_crypto_unchanged_without_exchange_date_constraint(self):
        # 17. Crypto unchanged without exchange calendar date
        pos = PositionContext(position_id="crypto-btc", instrument_id="BTC", ticker="BTC", name="Bitcoin", quantity=1.0, currency="USD", asset_type="CRYPTO")
        context = PortfolioContext(positions=[pos], source="test")
        spec = resolve_instrument("BTC")
        obs_crypto = QuoteObservation(
            quote_id="BTC:2026-09-29T12:00:00Z:REGULAR:coinbase",
            instrument_id="BTC", canonical_symbol="BTC", price=65000.0, currency="USD",
            previous_regular_close=64000.0, change_pct=1.56,
            session="REGULAR", market_date="2026-09-29", observed_at=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE), provider="coinbase",
            quote_type="TRADE", is_delayed=True, quality_status="VALID", market="CRYPTO",
        )
        cov = build_portfolio_quote_coverage(
            context, {"BTC": obs_crypto}, {"BTC": spec},
            datetime(2026, 9, 29, 13, 30, tzinfo=TPE), {"US": "2026-09-28", "TW": "2026-09-29"},
        )
        self.assertEqual(cov.covered_positions, 1)
        self.assertEqual(cov.status, "FULL")

    def test_18_taiwan_instruments_require_current_tw_session_date(self):
        # 18. Taiwan instruments unchanged (requires TW target session date)
        pos = PositionContext(position_id="tw-2330", instrument_id="2330", ticker="2330", name="台積電", quantity=1000.0, currency="TWD", asset_type="EQUITY")
        context = PortfolioContext(positions=[pos], source="test")
        spec = resolve_instrument("2330")
        obs_tw = QuoteObservation(
            quote_id="2330:2026-09-29:REGULAR:twse",
            instrument_id="2330", canonical_symbol="2330", price=2475.0, currency="TWD",
            previous_regular_close=2475.0, change_pct=0.0,
            session="REGULAR", market_date="2026-09-29", observed_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE),
            retrieved_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE), provider="twse",
            quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID", market="TW",
        )
        cov = build_portfolio_quote_coverage(
            context, {"2330": obs_tw}, {"2330": spec},
            datetime(2026, 9, 29, 13, 30, tzinfo=TPE), {"TW": "2026-09-29", "US": "2026-09-28"},
        )
        self.assertEqual(cov.covered_positions, 1)
        self.assertEqual(cov.status, "FULL")

    def test_19_23_position_diagnostic_accounting_sums_correctly(self):
        # 19. 23-position diagnostic accounting sums correctly
        context = _deterministic_test_portfolio()
        positions = context.positions
        self.assertEqual(len(positions), 23)

        # Test partial coverage where 11 are valid and 12 are unresolved
        target_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
        observations = {}
        for pos in positions[:11]:
            mkt = resolve_instrument(pos.ticker).market
            mkt_date = target_dates.get(mkt, "2026-09-29")
            obs = QuoteObservation(
                quote_id=f"{pos.ticker}:{mkt_date}:REGULAR:test",
                instrument_id=pos.ticker, canonical_symbol=pos.ticker, price=100.0,
                previous_regular_close=100.0, change_pct=0.0,
                currency=pos.currency, session="REGULAR", market_date=mkt_date,
                observed_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE),
                retrieved_at=datetime(2026, 9, 29, 13, 30, tzinfo=TPE), provider="test",
                quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID",
                market=mkt,
            )
            observations[pos.ticker] = obs
        universe = {pos.ticker: resolve_instrument(pos.ticker) for pos in positions}
        cov = build_portfolio_quote_coverage(
            context, observations, universe,
            datetime(2026, 9, 29, 13, 30, tzinfo=TPE), target_dates,
        )
        self.assertEqual(cov.expected_positions, 23)
        self.assertEqual(cov.covered_positions + len(cov.missing) + len(cov.stale) + len(cov.unsupported), 23)
        self.assertEqual(cov.covered_positions, 11)


class ReportUXExhaustiveTest(unittest.TestCase):
    """Tests 20-28: Report UX, deterministic wording, and forbidden diagnostic token elimination."""

    def test_20_flat_return_wording_never_says_down_zero(self):
        # 20. 0.00% -> 持平, never 上載/下跌
        self.assertEqual(return_direction(0.0), "持平")
        self.assertEqual(return_direction(0.00001), "持平")
        self.assertEqual(return_direction(-0.00001), "持平")
        self.assertEqual(return_direction(0.01), "上漲")
        self.assertEqual(return_direction(-0.01), "下跌")

    def test_21_22_23_raw_tokens_absent_from_telegram(self):
        # 21, 22, 23: raw OBSERVED, SUPPORTED_ASSOCIATION, UNRESOLVED absent from Telegram output
        raw_observed = "[OBSERVED] 市場結果：加權指數收在 47,631.96 點（-0.82%）。"
        raw_association = "[SUPPORTED_ASSOCIATION] 權值壓力：聯發科 -7.10%；多個大型權值同步走弱。"
        raw_unresolved = "[UNRESOLVED] 三大法人：UNRESOLVED（缺少統計）。"

        t_obs = _reader_evidence_line(raw_observed)
        t_assoc = _reader_evidence_line(raw_association)
        t_unres = _reader_evidence_line(raw_unresolved)

        self.assertNotIn("OBSERVED", t_obs)
        self.assertNotIn("SUPPORTED_ASSOCIATION", t_assoc)
        self.assertNotIn("UNRESOLVED", t_unres)
        self.assertIn("今日官方法人資料尚未通過驗證，暫不判讀。", t_unres)

    def test_24_previous_session_date_shown_instead_of_misleading_yesterday(self):
        # 24. previous-session date shown instead of misleading "昨日"
        # On 2026-09-29, previous TW trading session was 2026-09-24 (Mid-Autumn + Confucius Day holiday)
        prev_date = get_previous_completed_session_date("TW", "2026-09-29")
        self.assertEqual(prev_date, "2026-09-24")

    def test_25_unresolved_nonessential_data_no_debug_clutter(self):
        # 25. unresolved nonessential data does not create debug-log clutter
        line = "[UNRESOLVED] Portfolio coverage change：UNRESOLVED（沒有前一 session artifact）。"
        self.assertIsNone(_reader_evidence_line(line))

    def test_26_arbitrary_support_levels_cannot_render_without_methodology(self):
        # 26. arbitrary support/resistance levels cannot render without methodology
        context = MarketContext(
            run_id="test-watch", generated_at=datetime.now(tz=timezone.utc),
            report_type="tw_close", market_date="2026-09-29", market_session="REGULAR",
            taiex_summary=TaiexMarketSummary(close=47631.96, point_change=-392.64, change_pct=-0.82),
        )
        signals = derive_tomorrow_watch_signals(context)
        for s in signals:
            self.assertNotIn("47,000", s)
            self.assertNotIn("動態參考", s)

    def test_27_28_provenance_validation(self):
        # 27 & 28: turnover/breadth and institutional-flow provenance validation
        summary = TaiexMarketSummary(
            close=47631.96, point_change=-392.64, change_pct=-0.82,
            turnover_ntd_billions=8361.45, advancing=374, declining=586, unchanged=112,
            session_date="2026-09-29", previous_session_date="2026-09-24",
            source="https://www.twse.com.tw/exchangeReport/MI_INDEX",
        )
        flows = InstitutionalFlows(
            foreign_buy_sell_ntd_billions=-62.58, investment_trust_buy_sell_ntd_billions=0.58,
            dealer_buy_sell_ntd_billions=-16.40, total_buy_sell_ntd_billions=-78.40,
            session_date="2026-09-29", previous_session_date="2026-09-24",
            source="https://www.twse.com.tw/fund/BFI82U",
        )
        self.assertEqual(summary.session_date, "2026-09-29")
        self.assertEqual(summary.previous_session_date, "2026-09-24")
        self.assertIn("MI_INDEX", summary.source)
        self.assertEqual(flows.session_date, "2026-09-29")
        self.assertEqual(flows.previous_session_date, "2026-09-24")
        self.assertIn("BFI82U", flows.source)


class Reproduction20260929Test(unittest.TestCase):
    """Part E: Complete 2026-09-29 reproduction from production-shape data."""

    def test_reproduce_20260929_taiwan_close(self):
        # 1. 23 positions from canonical PIOS snapshot
        context_portfolio = _deterministic_test_portfolio()
        positions = context_portfolio.positions
        self.assertEqual(len(positions), 23)

        # 2. Build universe & observations
        # TW: 6 positions (2026-09-29)
        # US: 11 positions (expected completed session: 2026-09-28)
        # Crypto: 6 positions
        retrieved_at = datetime(2026, 9, 29, 15, 0, tzinfo=TPE)
        quotes: dict[str, QuoteObservation] = {}
        named_quotes: dict[str, NamedQuote] = {}

        tw_syms = ["0050", "006208", "1519", "2327", "2330", "2383", "TAIEX", "2317", "2454", "2308", "3711", "2303", "2382", "4958", "2356", "3231"]
        tw_prices = {
            "0050": (195.0, 196.0, -0.51), "006208": (115.0, 115.5, -0.43),
            "1519": (560.0, 565.0, -0.88), "2327": (620.0, 625.0, -0.80),
            "2330": (2475.0, 2475.0, 0.00), "2383": (4920.0, 5050.0, -2.57),
            "TAIEX": (47631.96, 48024.60, -0.82), "2317": (250.5, 250.5, 0.00),
            "2454": (4910.0, 5285.0, -7.10), "2308": (1835.0, 1910.0, -3.93),
            "3711": (687.0, 699.0, -1.72), "2303": (153.5, 154.0, -0.32),
            "2382": (336.5, 338.5, -0.59), "4958": (498.5, 475.5, 4.84),
            "2356": (59.2, 59.9, -1.17), "3231": (185.5, 184.5, 0.54),
        }
        for s in tw_syms:
            p, prev, chg = tw_prices[s]
            spec = resolve_instrument(s)
            obs = QuoteObservation(
                quote_id=f"{s}:2026-09-29:REGULAR:twse", instrument_id=s, canonical_symbol=s,
                price=p, currency="TWD", session="REGULAR", market_date="2026-09-29",
                observed_at=retrieved_at, retrieved_at=retrieved_at, provider="twse",
                quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID",
                previous_regular_close=prev, change_pct=chg, market="TW",
            )
            quotes[s] = obs
            named_quotes[s] = NamedQuote(name=spec.display_name, currency=spec.currency, symbol=s, price=p, prev_close=prev, change_pct=chg, data_date="2026-09-29")

        us_syms = ["AMZN", "DRAM", "GOOG", "IBKR", "MU", "NVDA", "QQQ", "TSLA", "VOO", "VST", "VTI"]
        for s in us_syms:
            spec = resolve_instrument(s)
            obs = QuoteObservation(
                quote_id=f"{s}:2026-09-28:REGULAR:yfinance", instrument_id=s, canonical_symbol=s,
                price=150.0, currency="USD", session="REGULAR", market_date="2026-09-28",
                observed_at=datetime(2026, 9, 28, 16, 0, tzinfo=NY), retrieved_at=retrieved_at,
                provider="yfinance", quote_type="OFFICIAL_CLOSE", is_delayed=True, quality_status="VALID",
                previous_regular_close=148.0, change_pct=1.35, market="US",
            )
            quotes[s] = obs
            named_quotes[s] = NamedQuote(name=spec.display_name, currency="USD", symbol=s, price=150.0, prev_close=148.0, change_pct=1.35, data_date="2026-09-28")

        crypto_syms = ["BTC", "ETH", "USDC", "USDT", "BONK", "SXT"]
        for s in crypto_syms:
            spec = resolve_instrument(s)
            provider = "coinbase_exchange" if s == "BONK" else "coingecko"
            price = 1.0 if "USD" in s else (60000.0 if s == "BTC" else (2600.0 if s == "ETH" else (0.000012 if s == "BONK" else 0.15)))
            prev = price / 1.001
            obs = QuoteObservation(
                quote_id=f"{s}:2026-09-29T06:00:00Z:REGULAR:{provider}", instrument_id=s, canonical_symbol=s,
                price=price, currency="USD", session="REGULAR",
                market_date="2026-09-29", observed_at=retrieved_at, retrieved_at=retrieved_at,
                provider=provider, quote_type="TRADE", is_delayed=True, quality_status="VALID",
                previous_regular_close=prev, change_pct=0.1, market="CRYPTO",
            )
            quotes[s] = obs
            named_quotes[s] = NamedQuote(name=spec.display_name, currency="USD", symbol=s, price=obs.price, prev_close=obs.previous_regular_close, change_pct=0.1, data_date="2026-09-29")

        fx_obs = QuoteObservation(
            quote_id="USDTWD:2026-09-29:REGULAR:yfinance", instrument_id="USDTWD", canonical_symbol="USDTWD",
            price=31.800, currency="TWD", session="REGULAR", market_date="2026-09-29",
            observed_at=retrieved_at, retrieved_at=retrieved_at, provider="yfinance",
            quote_type="TRADE", is_delayed=True, quality_status="VALID", previous_regular_close=31.780,
            change_pct=0.06, market="FOREX",
        )
        quotes["USDTWD"] = fx_obs
        named_quotes["USDTWD"] = NamedQuote(name="USD/TWD", currency="TWD", symbol="USDTWD", price=31.800, prev_close=31.780, change_pct=0.06, data_date="2026-09-29")

        universe = {s: resolve_instrument(s) for s in quotes}
        cov = build_portfolio_quote_coverage(
            context_portfolio, quotes, universe, retrieved_at, {"TW": "2026-09-29", "US": "2026-09-28"}
        )
        self.assertEqual(cov.expected_positions, 23)
        self.assertEqual(cov.covered_positions, 23)
        self.assertEqual(cov.status, "FULL")

        summary = TaiexMarketSummary(
            close=47631.96, point_change=-392.64, change_pct=-0.82,
            turnover_ntd_billions=8361.45, advancing=374, declining=586, unchanged=112,
            advancing_prev=450, declining_prev=420, unchanged_prev=95,
            session_date="2026-09-29", previous_session_date="2026-09-24",
            source="https://www.twse.com.tw/exchangeReport/MI_INDEX",
        )
        flows = InstitutionalFlows(
            foreign_buy_sell_ntd_billions=-62.58, investment_trust_buy_sell_ntd_billions=0.58,
            dealer_buy_sell_ntd_billions=-16.40, total_buy_sell_ntd_billions=-78.40,
            foreign_buy_sell_prev_ntd_billions=-10.0, investment_trust_buy_sell_prev_ntd_billions=5.0,
            dealer_buy_sell_prev_ntd_billions=-2.0, total_buy_sell_prev_ntd_billions=-7.0,
            turnover_prev_ntd_billions=7631.20,
            session_date="2026-09-29", previous_session_date="2026-09-24",
            source="https://www.twse.com.tw/fund/BFI82U",
        )

        context = MarketContext(
            run_id="tw-close-20260929", generated_at=retrieved_at,
            report_type="tw_close", market_date="2026-09-29", market_session="REGULAR",
            quotes=quotes, taiex_summary=summary, institutional_flows=flows,
            portfolio_quote_coverage=cov, portfolio_context=context_portfolio,
        )

        draft = build_public_draft(context)
        report_text = _render_tw_close_report(draft, context)

        # Verification of reproduction
        self.assertIn("47,631.96", report_text)
        self.assertIn("8,361.45", report_text)
        self.assertIn("374/586", report_text)
        self.assertIn("31.800", report_text)
        self.assertIn("台積電 (2330) 單日持平（+0.00%）。", report_text)
        self.assertIn("【相較前一交易日 2026-09-24】", report_text)
        self.assertIn("行情覆蓋：23/23 FULL", report_text)
        self.assertIn("跨市場部位目前沒有統一的起訖估值時間", report_text)
        self.assertNotIn("總資產：NT$", report_text)
        self.assertNotIn("本次組合變動：", report_text)
        self.assertNotIn("相對台股大盤", report_text)
        self.assertNotIn("OBSERVED", report_text)
        self.assertNotIn("SUPPORTED_ASSOCIATION", report_text)
        self.assertNotIn("UNRESOLVED", report_text)
        self.assertNotIn("47,000", report_text)
        self.assertNotIn("下跌 0.00", report_text)
        self.assertNotIn("對大盤具關鍵指引動能", report_text)

        ok, errors = validate_rendered_report_structure(report_text, "tw_close")
        self.assertTrue(ok, f"Structural validation errors: {errors}")


class PortfolioAnalyticsContractSuiteTest(unittest.TestCase):
    def setUp(self):
        from portfolio_context import load_authoritative_portfolio
        self.portfolio = _deterministic_test_portfolio()
        self.retrieved_at = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)

    def _build_valid_23_quotes(self):
        quotes = {}
        # TW quotes (6)
        tw_syms = ["0050", "006208", "1519", "2327", "2330", "2383"]
        for s in tw_syms:
            quotes[s] = QuoteObservation(
                quote_id=f"{s}:2026-09-29:REGULAR:twse", instrument_id=s, canonical_symbol=s,
                price=100.0, currency="TWD", session="REGULAR", market_date="2026-09-29",
                observed_at=self.retrieved_at, retrieved_at=self.retrieved_at,
                provider="twse", quote_type="TRADE", is_delayed=False, quality_status="VALID",
                previous_regular_close=99.0, change_pct=1.01, market="TW",
            )
        # US quotes (11)
        us_syms = ["AMZN", "DRAM", "GOOG", "IBKR", "MU", "NVDA", "QQQ", "TSLA", "VOO", "VST", "VTI"]
        for s in us_syms:
            quotes[s] = QuoteObservation(
                quote_id=f"{s}:2026-09-28:REGULAR:yfinance", instrument_id=s, canonical_symbol=s,
                price=200.0, currency="USD", session="REGULAR", market_date="2026-09-28",
                observed_at=self.retrieved_at, retrieved_at=self.retrieved_at,
                provider="yfinance", quote_type="TRADE", is_delayed=False, quality_status="VALID",
                previous_regular_close=196.0, change_pct=2.04, market="US",
            )
        # Crypto quotes (6)
        crypto_syms = ["BTC", "ETH", "USDC", "USDT", "BONK", "SXT"]
        for s in crypto_syms:
            price = 60000.0 if s == "BTC" else (2600.0 if s == "ETH" else (0.000012 if s == "BONK" else (0.15 if s == "SXT" else 1.0)))
            quotes[s] = QuoteObservation(
                quote_id=f"{s}:2026-09-29T06:00:00Z:REGULAR:coingecko", instrument_id=s, canonical_symbol=s,
                price=price, currency="USD", session="REGULAR", market_date="2026-09-29",
                observed_at=self.retrieved_at, retrieved_at=self.retrieved_at,
                provider="coingecko", quote_type="TRADE", is_delayed=False, quality_status="VALID",
                previous_regular_close=price * 0.99, change_pct=1.01, market="CRYPTO",
            )
        # FX quote
        quotes["USDTWD"] = QuoteObservation(
            quote_id="USDTWD:2026-09-29:REGULAR:yfinance", instrument_id="USDTWD", canonical_symbol="USDTWD",
            price=31.800, currency="TWD", session="REGULAR", market_date="2026-09-29",
            observed_at=self.retrieved_at, retrieved_at=self.retrieved_at,
            provider="yfinance", quote_type="TRADE", is_delayed=False, quality_status="VALID",
            previous_regular_close=31.780, change_pct=0.06, market="FOREX",
        )
        return quotes

    def test_complete_23_valuation_math_and_bp_contributions(self):
        from portfolio_analytics import calculate_portfolio_analytics
        quotes = self._build_valid_23_quotes()
        expected_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
        res = calculate_portfolio_analytics(self.portfolio, quotes, taiex_change_pct=-0.82, expected_dates=expected_dates)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.comparable_positions, 23)
        self.assertEqual(res.expected_positions, 23)
        self.assertGreater(res.total_valuation_twd, 0)
        self.assertGreater(res.previous_valuation_twd, 0)
        self.assertEqual(res.daily_pl_twd, res.total_valuation_twd - res.previous_valuation_twd)

        # Verify sum of basis point contributions equals total return in bp (within rounding tolerance)
        total_bp_from_positions = sum(p.contribution_bp for p in res.positions)
        expected_return_bp = res.daily_return_pct * 100
        self.assertAlmostEqual(total_bp_from_positions, expected_return_bp, delta=0.5)

        # Asset class weights sum to 100%
        weight_sum = res.tw_equities_weight + res.us_equities_weight + res.crypto_weight
        self.assertAlmostEqual(weight_sum, 100.0, delta=0.1)

        # P/L sum equals total daily P/L
        pl_sum = res.tw_equities_pl_twd + res.us_equities_pl_twd + res.crypto_pl_twd
        self.assertAlmostEqual(pl_sum, res.daily_pl_twd, delta=1.0)

        # TAIEX comparison
        self.assertAlmostEqual(res.taiex_diff_pct, round(res.daily_return_pct - (-0.82), 2), places=2)

    def test_missing_fx_fails_closed(self):
        from portfolio_analytics import calculate_portfolio_analytics
        quotes = self._build_valid_23_quotes()
        del quotes["USDTWD"]
        expected_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
        res = calculate_portfolio_analytics(self.portfolio, quotes, taiex_change_pct=-0.82, expected_dates=expected_dates)
        self.assertEqual(res.status, "MISSING_FX")

    def test_incomplete_quotes_fails_closed(self):
        from portfolio_analytics import calculate_portfolio_analytics
        quotes = self._build_valid_23_quotes()
        # Remove US quotes
        for s in ["AMZN", "DRAM", "GOOG"]:
            del quotes[s]
        expected_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
        res = calculate_portfolio_analytics(self.portfolio, quotes, taiex_change_pct=-0.82, expected_dates=expected_dates)
        self.assertEqual(res.status, "INCOMPLETE_QUOTES")

    def test_incomplete_us_session_date_mismatch_fails_closed(self):
        from portfolio_analytics import calculate_portfolio_analytics
        quotes = self._build_valid_23_quotes()
        quotes["AMZN"] = quotes["AMZN"].model_copy(update={"market_date": "2026-09-27"})
        expected_dates = {"TW": "2026-09-29", "US": "2026-09-28"}
        res = calculate_portfolio_analytics(self.portfolio, quotes, taiex_change_pct=-0.82, expected_dates=expected_dates)
        self.assertEqual(res.status, "INCOMPLETE_QUOTES")
        self.assertIn("22/23", res.reason)

    def test_missing_quantity_fails_closed(self):
        from models import PortfolioContext, PositionContext
        from portfolio_analytics import calculate_portfolio_analytics
        quotes = self._build_valid_23_quotes()
        bad_pos = [PositionContext.model_construct(
            position_id="test-pos", instrument_id="2330", ticker="2330",
            name="台積電", currency="TWD", quantity=0.0, market="TW", asset_type="EQUITY",
        )]
        bad_portfolio = PortfolioContext(source="PIOS_TEST", positions=bad_pos)
        res = calculate_portfolio_analytics(bad_portfolio, quotes)
        self.assertEqual(res.status, "MISSING_QUANTITY")


class TelegramReadabilityAndForbiddenTokensTest(unittest.TestCase):
    def test_forbidden_tokens_scrubbed_from_report_and_telegram(self):
        from generate_report import _clean_markdown_for_telegram_report
        raw_markdown = (
            "# 📊 台股收盤｜2026-09-29\n\n"
            "## 【今天盤面重點】\n"
            "- [OBSERVED] 加權指數：收在 47,631.96 點，較前一交易日下跌 392.64 點。\n"
            "- [SUPPORTED_ASSOCIATION] 相對支撐：台達電 (2308) +3.00%；多個大型權值同步上漲，與加權指數表現一致；此為關聯性觀察，未推定單一因果。\n"
            "- 成交金額：836.14 億台幣，較前一 session +73.02 億。\n"
            "- TAIEX session-over-session：收在 47,631.96 點，較前一已完成 TWSE session -392.64 點；比較基準為前一正式收盤。\n"
            "- [UNRESOLVED] 成交金額：缺少已驗證成交統計。\n"
            "- Portfolio relative performance：UNRESOLVED（尚無滿足既有信心合約的跨幣別 canonical portfolio valuation evidence）。\n"
        )
        cleaned = _clean_markdown_for_telegram_report(raw_markdown)

        forbidden_tokens = [
            "OBSERVED",
            "UNRESOLVED",
            "SUPPORTED_ASSOCIATION",
            "session-over-session",
            "前一已完成 TWSE session",
            "較前一 session",
            "canonical",
            "confidence contract",
            "artifact",
            "此為關聯性觀察，未推定單一因果",
            "比較基準為前一正式收盤",
            "億台幣",
        ]
        for token in forbidden_tokens:
            self.assertNotIn(token, cleaned, f"Forbidden token '{token}' found in Telegram output")

        self.assertIn("億元", cleaned)

    def test_large_quote_table_pruned_on_telegram_to_scannable_movers(self):
        from generate_report import _clean_markdown_for_telegram_report
        table_markdown = (
            "## 【市場】\n"
            "| 標的 | 最新報價 | 漲跌幅 | 狀態 |\n"
            "|---|---:|---:|---|\n"
            "| 加權指數 (TAIEX) | 47,940.13 | +0.65% | 正式收盤 |\n"
            "| 台積電 (2330) | 2,480.00 | +0.20% | 正式收盤 |\n"
            "| 鴻海 (2317) | 251.5 | +0.40% | 正式收盤 |\n"
            "| 聯發科 (2454) | 4,920.00 | +0.20% | 正式收盤 |\n"
            "| 台達電 (2308) | 1,890.00 | +3.00% | 正式收盤 |\n"
            "| 廣達 (2382) | 333.5 | -0.89% | 正式收盤 |\n"
            "| 聯電 (2303) | 154.5 | +0.65% | 正式收盤 |\n"
            "| 日月光投控 (3711) | 703.0 | +2.33% | 正式收盤 |\n"
            "| 英業達 (2356) | 59.5 | +0.51% | 正式收盤 |\n"
            "| 緯創 (3231) | 184.5 | -0.54% | 正式收盤 |\n"
            "| 台光電 (2383) | 4,935.00 | +0.30% | 正式收盤 |\n"
            "| 臻鼎-KY (4958) | 507.0 | +1.71% | 正式收盤 |\n"
        )
        cleaned = _clean_markdown_for_telegram_report(table_markdown)
        lines = [line for line in cleaned.splitlines() if line.startswith("•")]
        self.assertLessEqual(len(lines), 6)
        self.assertTrue(any("TAIEX" in line for line in lines))
        self.assertTrue(any("2308" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
