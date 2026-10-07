#!/usr/bin/env python3
"""Comprehensive test suite for portfolio event monitoring pipeline (Part I 1-15)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_report import run_portfolio_advice
from market_context import build_market_context
from models import (
    MarketContext,
    PortfolioActionBrief,
    PortfolioActionItem,
    PortfolioContext,
    PortfolioEventFact,
    PortfolioQuoteCoverage,
    PositionContext,
    QuoteObservation,
    Snapshot,
)
from portfolio_events import (
    EventSearchError,
    _query_batch_with_bounded_fallback,
    fetch_portfolio_events,
    is_entry_fresh,
    load_event_cache,
    save_event_cache,
)
from structured_reports import (
    build_action_brief,
    render_action_brief,
    validate_action_brief,
)
import refresh_portfolio_events


def _obs(symbol: str, price: float = 100.0, prev: float = 99.0, session: str = "REGULAR") -> QuoteObservation:
    now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    return QuoteObservation(
        quote_id=f"{symbol}:obs:test",
        instrument_id=symbol,
        canonical_symbol=symbol,
        price=price,
        currency="USD",
        session=session,
        market_date="2026-09-29",
        observed_at=now,
        provider_timestamp=now,
        retrieved_at=now,
        provider="test",
        quote_type="OFFICIAL_CLOSE",
        is_delayed=False,
        quality_status="VALID",
        previous_regular_close=prev,
        change_pct=round((price - prev) / prev * 100, 2),
    )


def _make_23_portfolio() -> PortfolioContext:
    # Synthetic 23-position universe. Do not mirror the user's private
    # production holdings in this public repository test fixture.
    tickers = [
        "TEST01", "TEST02", "TEST03", "TEST04", "2330", "TEST05",
        "TEST06", "AMZN", "TEST07", "TEST08", "TEST09", "NVDA",
        "TEST10", "TSLA", "TEST11", "TEST12", "TEST13", "TEST14",
        "TEST15", "TEST16", "TEST17", "TEST18", "TEST19",
    ]
    positions = [
        PositionContext(
            position_id=f"pos_{t}",
            instrument_id=t,
            ticker=t,
            name=f"Holding {t}",
            quantity=10.0,
            cost_basis=100.0,
            currency="USD",
            asset_type="EQUITY",
        )
        for t in tickers
    ]
    return PortfolioContext(
        snapshot_id="snap_23",
        as_of=datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc),
        source="PIOS_PORTFOLIO_SNAPSHOT",
        positions=positions,
    )


class PortfolioEventMonitoringSuiteTest(unittest.TestCase):
    def setUp(self):
        self.portfolio = _make_23_portfolio()
        self.quotes = {p.ticker: _obs(p.ticker) for p in self.portfolio.positions}
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        self.snapshot = Snapshot(
            generated_at=now,
            report_type="tw_close",
            fetch_coverage=1.0,
            market_context_coverage=1.0,
            portfolio_quote_coverage=PortfolioQuoteCoverage(
                expected_positions=len(self.quotes),
                covered_positions=len(self.quotes),
                coverage_ratio=1.0,
                as_of=now,
                status="FULL",
            ),
            quote_observations=self.quotes,
        )
        self.context = build_market_context(self.snapshot, "tw_close", run_id="test_run", now=self.snapshot.generated_at)

    def test_01_production_run_portfolio_advice_passes_real_event_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_facts = [
                PortfolioEventFact(
                    instrument_id="NVDA",
                    ticker="NVDA",
                    checked_at=self.context.generated_at,
                    event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE",
                    summary="未發現重大異常",
                )
            ]
            dummy_upcoming = [{"ticker": "NVDA", "event": "財報", "date": "10/25", "why": "重點展望"}]
            with patch("generate_report.fetch_portfolio_events", return_value=(dummy_facts, dummy_upcoming)) as mock_fetch, \
                 patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio), \
                 patch("generate_report.validate_portfolio_quotes", return_value=(True, "OK")), \
                 patch("generate_report.build_action_brief", wraps=build_action_brief) as mock_build_brief, \
                 patch("generate_report.send_advice_telegram"), \
                 patch("generate_report.send_advice_email"):
                run_portfolio_advice("report text", "tw_close", self.snapshot, "gemini-2.0-flash", deliver=False)
                mock_fetch.assert_called_once()
                mock_build_brief.assert_called_once()
                # Verify verified_events and upcoming_events were passed into build_action_brief
                _, kwargs = mock_build_brief.call_args
                self.assertEqual(kwargs.get("verified_events"), dummy_facts)
                self.assertEqual(kwargs.get("upcoming_events"), dummy_upcoming)

    def test_02_zero_of_23_event_coverage_cannot_render_full_clear_language(self):
        # 0 of 23 holdings event-checked
        brief = build_action_brief(self.context, self.portfolio, verified_events=[])
        self.assertEqual(brief.event_checked_positions, 0)
        self.assertEqual(brief.event_total_positions, 23)
        rendered = render_action_brief(brief)
        self.assertIn("公司事件面本次尚未完成驗證。", rendered)
        self.assertNotIn("今天沒有需要升級檢視的事件", rendered)
        self.assertNotIn("已完成檢查", rendered)

    def test_03_one_of_23_event_coverage_cannot_render_full_clear_language(self):
        # 1 of 23 holdings checked
        fact = PortfolioEventFact(
            instrument_id="2330",
            ticker="2330",
            checked_at=self.context.generated_at,
            event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE",
            summary="法說會前無重大異常",
        )
        brief = build_action_brief(self.context, self.portfolio, verified_events=[fact])
        self.assertEqual(brief.event_checked_positions, 1)
        self.assertEqual(brief.event_total_positions, 23)
        rendered = render_action_brief(brief)
        self.assertIn("公司事件目前完成 1/23；其餘 22 檔不做事件結論。", rendered)
        self.assertNotIn("今天沒有需要升級檢視的事件", rendered)
        self.assertFalse(brief.events_verified)

    def test_04_22_of_23_event_coverage_cannot_render_full_clear_language(self):
        # 22 of 23 holdings checked, 1 missing
        facts = [
            PortfolioEventFact(
                instrument_id=p.instrument_id,
                ticker=p.ticker,
                checked_at=self.context.generated_at,
                event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE",
            )
            for p in self.portfolio.positions[:-1]
        ]
        brief = build_action_brief(self.context, self.portfolio, verified_events=facts)
        self.assertEqual(brief.event_checked_positions, 22)
        self.assertEqual(brief.event_total_positions, 23)
        rendered = render_action_brief(brief)
        self.assertIn("公司事件目前完成 22/23；其餘 1 檔不做事件結論。", rendered)
        self.assertNotIn("今天沒有需要升級檢視的事件", rendered)
        self.assertFalse(brief.events_verified)

    def test_05_23_of_23_checked_no_material_full_clear_language_allowed(self):
        facts = [
            PortfolioEventFact(
                instrument_id=p.instrument_id,
                ticker=p.ticker,
                checked_at=self.context.generated_at,
                event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE",
            )
            for p in self.portfolio.positions
        ]
        brief = build_action_brief(self.context, self.portfolio, verified_events=facts)
        self.assertEqual(brief.event_checked_positions, 23)
        self.assertEqual(brief.event_total_positions, 23)
        self.assertTrue(brief.events_verified)
        rendered = render_action_brief(brief)
        self.assertIn("公司事件 23/23 已完成檢查，今天沒有需要升級檢視的事件。", rendered)
        ok, reason = validate_action_brief(brief, self.context, self.portfolio)
        self.assertTrue(ok, reason)

    def test_06_search_failure_produces_event_check_failed_never_no_material_change(self):
        failed_fact = PortfolioEventFact(
            instrument_id="NVDA",
            ticker="NVDA",
            checked_at=self.context.generated_at,
            event_status="EVENT_CHECK_FAILED",
            summary="新聞搜尋連線失敗，本次事件未完成驗證。",
        )
        brief = build_action_brief(self.context, self.portfolio, verified_events=[failed_fact])
        nvda_item = next(i for i in brief.no_material_change if i.ticker == "NVDA")
        self.assertEqual(nvda_item.event_status, "EVENT_CHECK_FAILED")
        self.assertIn("EVENT_CHECK_FAILED", nvda_item.reason_codes)
        self.assertNotIn("NO_MATERIAL_EVENT", nvda_item.reason_codes)
        self.assertEqual(brief.event_check_failed_positions, 1)
        rendered = render_action_brief(brief)
        self.assertIn("【資料說明】", rendered)
        self.assertIn("NVDA：事件資料本次未完成驗證。", rendered)

    def test_07_under_7pct_price_plus_material_event_produces_watch_or_action_review(self):
        # AMZN moved 0%, but major guidance cut occurred
        event = PortfolioEventFact(
            instrument_id="AMZN",
            ticker="AMZN",
            checked_at=self.context.generated_at,
            event_status="EVENT_MATERIAL_FOUND",
            event_type="GUIDANCE_CUT",
            title="AWS 展望下修",
            source_url="https://example.com/amazon-announcement",
            source_name="Amazon 投資人關係網站",
            source_published_at=self.context.generated_at,
            publication_date_verified=True,
            summary="公司大幅下修下一季度 AWS 營收增長指引至 8%",
            impact="影響中長期估值假設",
            severity="HIGH",
        )
        brief = build_action_brief(self.context, self.portfolio, verified_events=[event])
        self.assertEqual(len(brief.action_queue), 1)
        item = brief.action_queue[0]
        self.assertEqual(item.ticker, "AMZN")
        self.assertEqual(item.status, "ACTION_REVIEW")
        self.assertEqual(item.event_status, "EVENT_MATERIAL_FOUND")
        rendered = render_action_brief(brief)
        self.assertIn("【值得重新檢視】", rendered)
        self.assertIn("AMZN｜2026-09-29｜AWS 展望下修", rendered)

    def test_08_at_or_above_7pct_price_plus_no_verified_event_produces_price_watch_with_event_uncertainty(self):
        quotes = dict(self.quotes)
        quotes["TSLA"] = _obs("TSLA", price=108.0, prev=100.0)  # +8%
        snap = Snapshot(
            generated_at=datetime.now(tz=timezone.utc),
            report_type="tw_close",
            quote_observations=quotes,
        )
        ctx = build_market_context(snap, "tw_close", run_id="test_price_only")
        brief = build_action_brief(ctx, self.portfolio, verified_events=[])
        self.assertEqual(len(brief.watchlist), 1)
        item = brief.watchlist[0]
        self.assertEqual(item.ticker, "TSLA")
        self.assertEqual(item.price_status, "PRICE_WATCH")
        self.assertEqual(item.event_status, "EVENT_UNCHECKED")
        rendered = render_action_brief(brief)
        self.assertIn("【需要關注】", rendered)
        self.assertIn("TSLA｜單日 +8.0%", rendered)
        self.assertIn("在事件完成驗證前，不直接產生交易結論。", rendered)

    def test_09_upcoming_earnings_event_renders_naturally(self):
        upcoming = [
            {
                "ticker": "NVDA",
                "event": "美股盤後財報",
                "title": "美股盤後財報",
                "date": "2026-10-03",
                "event_date": "2026-10-03",
                "is_upcoming": True,
                "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE",
                "checked_at": self.context.generated_at,
                "source_published_at": self.context.generated_at,
                "publication_date_verified": True,
                "summary": "重點看資料中心成長、毛利率與下一季營運展望",
                "source_name": "NVIDIA 投資人關係網站",
                "source_url": "https://investor.nvidia.com/calendar",
            }
        ]
        brief = build_action_brief(self.context, self.portfolio, upcoming_events=upcoming)
        rendered = render_action_brief(brief)
        self.assertIn("【近期事件】", rendered)
        self.assertIn("NVDA｜美股盤後財報｜2026-10-03｜重點看資料中心成長、毛利率與下一季營運展望", rendered)

    def test_10_upcoming_event_requires_provenance(self):
        upcoming = [
            {
                "ticker": "NVDA",
                "event": "法說會",
                "date": "10/20",
                "why": "AI晶片營運展望",
                "source": "Company IR",
            }
        ]
        brief = build_action_brief(self.context, self.portfolio, upcoming_events=upcoming)
        self.assertFalse(brief.upcoming_events)

    def test_11_no_raw_event_enums_leak_to_telegram(self):
        facts = [
            PortfolioEventFact(
                instrument_id="NVDA",
                ticker="NVDA",
                checked_at=self.context.generated_at,
                event_status="EVENT_CHECK_FAILED",
                summary="搜尋連線中斷",
            ),
            PortfolioEventFact(
                instrument_id="AMZN",
                ticker="AMZN",
                checked_at=self.context.generated_at,
                event_status="EVENT_MATERIAL_FOUND",
                title="收購要約",
                source_url="https://example.com/amazon-announcement",
                source_published_at=self.context.generated_at,
                publication_date_verified=True,
                summary="公司宣布重要併購",
                severity="HIGH",
            ),
        ]
        brief = build_action_brief(self.context, self.portfolio, verified_events=facts)
        rendered = render_action_brief(brief)
        forbidden = [
            "EVENT_CHECKED_NO_MATERIAL_CHANGE",
            "EVENT_MATERIAL_FOUND",
            "EVENT_CHECK_FAILED",
            "EVENT_UNCHECKED",
            "NO_MATERIAL_CHANGE",
            "SIZE_NOT_COMPUTED",
            "PREVIOUS_CLOSE",
            "DATA_BLOCKED",
            "ACTION_REVIEW",
            "NO_MATERIAL_EVENT",
        ]
        for token in forbidden:
            self.assertNotIn(token, rendered, f"Leaked raw token: {token}")

    def test_12_event_facts_stay_private(self):
        # Verify that data artifacts are git-ignored
        for path in ["data/portfolio_event_facts.json", "data/portfolio_event_cache.json"]:
            ignored = subprocess.run(
                ["git", "-C", str(ROOT), "check-ignore", path],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(ignored, path)

    def test_13_caching_and_freshness_avoids_unnecessary_duplicate_checks(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "test_cache.json"
            now = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)

            # Pre-populate cache for 2330
            save_event_cache(
                {
                    "updated_at": now.isoformat(),
                    "items": {
                        "2330": {
                            "ticker": "2330",
                            "instrument_id": "2330",
                            "checked_at": now.isoformat(),
                            "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE",
                            "facts": [{
                                "ticker": "2330",
                                "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE",
                                "checked_at": now.isoformat(),
                                "summary": "無重大事件",
                            }],
                            "upcoming": [],
                        }
                    },
                },
                cache_path=cache_file,
            )

            # Test entry freshness
            cache = load_event_cache(cache_file)
            entry = cache["items"]["2330"]
            self.assertTrue(is_entry_fresh(entry, now + timedelta(hours=2)))
            # Stale after 13 hours
            self.assertFalse(is_entry_fresh(entry, now + timedelta(hours=13)))
            # Force refresh invalidates
            self.assertFalse(is_entry_fresh(entry, now + timedelta(hours=2), force_refresh=True))

            # Calling fetch_portfolio_events with pre-populated cache does NOT call API for 2330
            with patch("portfolio_events._query_gemini_search") as mock_query:
                single_holding_portfolio = PortfolioContext(
                    snapshot_id="test",
                    positions=[self.portfolio.positions[4]],  # 2330
                )
                facts, _ = fetch_portfolio_events(
                    single_holding_portfolio,
                    now=now + timedelta(hours=1),
                    cache_path=cache_file,
                    api_key="fake-key",
                )
                mock_query.assert_not_called()
                self.assertEqual(len(facts), 1)
                self.assertEqual(facts[0].ticker, "2330")
                self.assertEqual(facts[0].event_status, "EVENT_CHECKED_NO_MATERIAL_CHANGE")

    def test_14_quote_coverage_and_event_coverage_reported_separately(self):
        facts = [
            PortfolioEventFact(
                instrument_id=p.instrument_id,
                ticker=p.ticker,
                checked_at=self.context.generated_at,
                event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE",
            )
            for p in self.portfolio.positions[:10]
        ]
        brief = build_action_brief(self.context, self.portfolio, verified_events=facts)
        rendered = render_action_brief(brief)
        # Check that both distinct coverage lines exist
        self.assertIn("23/23 持股行情已驗證，價格面沒有重大異常。", rendered)
        self.assertIn("公司事件目前完成 10/23；其餘 13 檔不做事件結論。", rendered)

    def test_15_all_23_synthetic_holdings_covered_by_event_universe(self):
        # Public CI must never depend on or disclose the private PIOS snapshot.
        synthetic_portfolio = self.portfolio
        expected_tickers = {p.ticker for p in synthetic_portfolio.positions}
        self.assertEqual(len(expected_tickers), 23)

        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "empty_cache.json"
            facts, _ = fetch_portfolio_events(
                synthetic_portfolio,
                cache_path=cache_file,
                api_key=None,  # triggers safe EVENT_CHECK_FAILED
            )
            covered_tickers = {f.ticker for f in facts}
            self.assertEqual(expected_tickers, covered_tickers)
            self.assertEqual(len(facts), 23)
            self.assertTrue(all(f.event_status == "EVENT_CHECK_FAILED" for f in facts))


    def test_16_omitted_ticker_from_search_batch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "event_cache.json"
            small = PortfolioContext(
                snapshot_id="small",
                positions=self.portfolio.positions[:2],
            )
            grounded_results = [{
                "ticker": small.positions[0].ticker,
                "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE",
                "summary": "checked",
                "is_upcoming": False,
            }]
            with patch("portfolio_events._query_gemini_search", return_value=grounded_results):
                facts, _ = fetch_portfolio_events(
                    small,
                    api_key="fake-key",
                    cache_path=cache_file,
                    now=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
                )
            by_ticker = {f.ticker: f for f in facts}
            self.assertEqual(by_ticker[small.positions[0].ticker].event_status, "EVENT_CHECKED_NO_MATERIAL_CHANGE")
            self.assertEqual(by_ticker[small.positions[1].ticker].event_status, "EVENT_CHECK_FAILED")

    def test_17_material_or_upcoming_event_without_provenance_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "event_cache.json"
            single = PortfolioContext(
                snapshot_id="single",
                positions=[self.portfolio.positions[11]],  # NVDA
            )
            result = [{
                "ticker": "NVDA",
                "event_status": "EVENT_MATERIAL_FOUND",
                "event_type": "GUIDANCE",
                "title": "Guidance changed",
                "summary": "Material claim but no source URL",
                "severity": "HIGH",
                "source_name": "Some source",
                "source_url": "",
                "is_upcoming": False,
            }]
            with patch("portfolio_events._query_gemini_search", return_value=result):
                facts, upcoming = fetch_portfolio_events(
                    single,
                    api_key="fake-key",
                    cache_path=cache_file,
                    now=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
                )
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].event_status, "EVENT_CHECK_FAILED")
            self.assertEqual(upcoming, [])

    def test_18_material_event_surfaces_even_when_quote_is_unavailable(self):
        quotes = dict(self.quotes)
        quotes.pop("AMZN")
        snap = Snapshot(
            generated_at=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
            report_type="tw_close",
            quote_observations=quotes,
        )
        ctx = build_market_context(snap, "tw_close", run_id="event_without_quote", now=snap.generated_at)
        event = PortfolioEventFact(
            instrument_id="AMZN",
            ticker="AMZN",
            checked_at=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
            event_status="EVENT_MATERIAL_FOUND",
            event_type="GUIDANCE",
            title="AWS guidance cut",
            source_published_at=ctx.generated_at,
            publication_date_verified=True,
            summary="Verified guidance change.",
            severity="HIGH",
            source_name="Amazon IR",
            source_url="https://example.com/amazon-ir",
            source_type="COMPANY_PR",
        )
        brief = build_action_brief(ctx, self.portfolio, verified_events=[event])
        amzn = next(i for i in brief.action_queue if i.ticker == "AMZN")
        self.assertEqual(amzn.price_status, "DATA_BLOCKED")
        self.assertEqual(amzn.event_status, "EVENT_MATERIAL_FOUND")
        self.assertEqual(brief.covered_positions, 22)
        self.assertEqual(brief.event_checked_positions, 1)
        ok, reason = validate_action_brief(brief, ctx, self.portfolio)
        self.assertTrue(ok, reason)
        rendered = render_action_brief(brief)
        self.assertIn("AMZN", rendered)
        self.assertIn("22/23 持股行情已驗證", rendered)

    def test_19_large_price_move_forces_event_refresh_in_production_wiring(self):
        quotes = dict(self.quotes)
        quotes["TSLA"] = _obs("TSLA", price=108.0, prev=100.0)
        snap = Snapshot(
            generated_at=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
            report_type="tw_close",
            quote_observations=quotes,
        )
        with patch("generate_report.fetch_portfolio_events", return_value=([], [])) as mock_fetch, \
             patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio), \
             patch("generate_report.load_market_context", return_value=self.context.model_copy(update={"quotes": {**self.context.quotes, "TSLA": quotes["TSLA"]}})), \
             patch("generate_report.validate_portfolio_quotes", return_value=(True, "OK")):
            run_portfolio_advice("report", "tw_close", snap, "gemini-2.0-flash", deliver=False)
        _, kwargs = mock_fetch.call_args
        self.assertIn("TSLA", kwargs.get("force_refresh_tickers", set()))


    def test_20_encrypted_cache_round_trip_uses_portfolio_key(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "portfolio_event_cache.json.enc"
            payload = {
                "updated_at": "2026-10-05T02:00:00+00:00",
                "items": {"TEST01": {"event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE"}},
            }
            key = Fernet.generate_key().decode()
            with patch.dict(os.environ, {"PORTFOLIO_KEY": key}, clear=False):
                save_event_cache(payload, cache_path=cache_file)
                raw = cache_file.read_bytes()
                self.assertNotIn(b"EVENT_CHECKED_NO_MATERIAL_CHANGE", raw)
                loaded = load_event_cache(cache_path=cache_file)
            self.assertEqual(loaded, payload)

    def test_21_all_daily_workflows_persist_only_encrypted_event_cache(self):
        for workflow in (
            ".github/workflows/tw-open.yml",
            ".github/workflows/tw-close.yml",
            ".github/workflows/us-open.yml",
            ".github/workflows/us-close.yml",
        ):
            text = (ROOT / workflow).read_text(encoding="utf-8")
            self.assertIn("portfolio_event_cache.json.enc", text)
            self.assertNotIn("git add data/portfolio_event_cache.json", text)


    def test_22_event_only_watch_is_not_mislabeled_as_price_watch(self):
        event = PortfolioEventFact(
            instrument_id="AMZN",
            ticker="AMZN",
            checked_at=self.context.generated_at,
            event_status="EVENT_MATERIAL_FOUND",
            event_type="REGULATORY",
            title="Regulatory development",
            source_published_at=self.context.generated_at,
            publication_date_verified=True,
            summary="Verified event requiring monitoring.",
            severity="MEDIUM",
            source_name="Regulator",
            source_url="https://example.com/regulator",
            source_type="REGULATORY",
        )
        brief = build_action_brief(self.context, self.portfolio, verified_events=[event])
        rendered = render_action_brief(brief)
        self.assertIn("公司／資產事件需要關注", rendered)
        self.assertNotIn("觸發價格關注", rendered)


    def test_23_contract_failure_uses_one_bounded_split_fallback(self):
        instruments = [
            {"ticker": f"T{i}", "name": f"Test {i}", "asset_type": "EQUITY"}
            for i in range(8)
        ]
        first_half = [{"ticker": f"T{i}", "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE"} for i in range(4)]
        second_half = [{"ticker": f"T{i}", "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE"} for i in range(4, 8)]
        with patch(
            "portfolio_events._query_gemini_search",
            side_effect=[EventSearchError("INVALID_JSON"), first_half, second_half],
        ) as query:
            results, failed, codes = _query_batch_with_bounded_fallback(
                instruments,
                model="gemini-2.5-flash",
                api_key="fake",
            )
        self.assertEqual(query.call_count, 3)
        self.assertEqual(len(results), 8)
        self.assertEqual(failed, [])
        self.assertEqual(codes, ["INVALID_JSON"])

    def test_24_bounded_fallback_fails_closed_without_recursive_retry(self):
        instruments = [
            {"ticker": f"T{i}", "name": f"Test {i}", "asset_type": "EQUITY"}
            for i in range(8)
        ]
        with patch(
            "portfolio_events._query_gemini_search",
            side_effect=[
                EventSearchError("INVALID_JSON"),
                EventSearchError("NO_GROUNDING_METADATA"),
                EventSearchError("INVALID_JSON"),
            ],
        ) as query:
            results, failed, codes = _query_batch_with_bounded_fallback(
                instruments,
                model="gemini-2.5-flash",
                api_key="fake",
            )
        self.assertEqual(query.call_count, 3)
        self.assertEqual(results, [])
        self.assertEqual(len(failed), 8)
        self.assertEqual(
            codes,
            ["INVALID_JSON", "NO_GROUNDING_METADATA", "INVALID_JSON"],
        )


    def test_25_transient_server_error_retries_then_succeeds(self):
        instruments = [
            {"ticker": "T0", "name": "Test 0", "asset_type": "EQUITY"},
            {"ticker": "T1", "name": "Test 1", "asset_type": "EQUITY"},
        ]
        success = [
            {"ticker": "T0", "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE"},
            {"ticker": "T1", "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE"},
        ]
        with patch("portfolio_events.time.sleep"), patch(
            "portfolio_events._query_gemini_search",
            side_effect=[RuntimeError("server"), success],
        ) as query:
            results, failed, codes = _query_batch_with_bounded_fallback(
                instruments,
                model="gemini-2.5-flash",
                api_key="fake",
            )
        self.assertEqual(query.call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(failed, [])
        self.assertEqual(codes, ["RuntimeError"])


    def test_26_report_mode_does_not_search_full_portfolio_when_cache_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "missing.json"
            with patch("portfolio_events._query_gemini_search") as query:
                facts, upcoming = fetch_portfolio_events(
                    self.portfolio,
                    api_key="fake",
                    cache_path=cache_file,
                    network_scope="forced_only",
                )
            query.assert_not_called()
            self.assertEqual(len({f.instrument_id for f in facts}), 23)
            self.assertTrue(all(f.event_status == "EVENT_UNCHECKED" for f in facts))
            self.assertEqual(upcoming, [])

    def test_27_report_mode_live_searches_only_forced_price_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "missing.json"
            target = self.portfolio.positions[0].ticker
            result = [{
                "ticker": target,
                "event_status": "EVENT_CHECKED_NO_MATERIAL_CHANGE",
                "event_type": "NONE",
                "summary": "checked",
                "severity": "LOW",
                "is_upcoming": False,
            }]
            with patch("portfolio_events._query_batch_with_bounded_fallback", return_value=(result, [], [])) as query:
                facts, _ = fetch_portfolio_events(
                    self.portfolio,
                    api_key="fake",
                    cache_path=cache_file,
                    network_scope="forced_only",
                    force_refresh_tickers={target},
                )
            query.assert_called_once()
            checked = [f for f in facts if f.event_status == "EVENT_CHECKED_NO_MATERIAL_CHANGE"]
            unchecked = [f for f in facts if f.event_status == "EVENT_UNCHECKED"]
            self.assertEqual(len(checked), 1)
            self.assertEqual(len(unchecked), 22)

    def test_28_background_event_workflow_exists_and_report_wiring_is_forced_only(self):
        workflow = (ROOT / ".github/workflows/portfolio-events.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "17 */6 * * *"', workflow)
        self.assertIn("refresh_portfolio_events.py", workflow)
        self.assertIn("portfolio_event_cache.json.enc", workflow)
        source = (ROOT / "scripts/generate_report.py").read_text(encoding="utf-8")
        self.assertIn('network_scope="forced_only"', source)


    def test_29_all_failed_search_does_not_create_event_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "portfolio_event_cache.json.enc"
            failed = [p.ticker for p in self.portfolio.positions]
            with patch(
                "portfolio_events._query_batch_with_bounded_fallback",
                return_value=([], failed, ["ServerError"]),
            ):
                facts, upcoming = fetch_portfolio_events(
                    self.portfolio,
                    api_key="fake",
                    cache_path=cache_file,
                    network_scope="all",
                )
            self.assertEqual(len({f.instrument_id for f in facts}), 23)
            self.assertTrue(all(f.event_status == "EVENT_CHECK_FAILED" for f in facts))
            self.assertEqual(upcoming, [])
            self.assertFalse(cache_file.exists())

    def test_30_background_refresh_degrades_without_nonzero_exit(self):
        failed_facts = [
            PortfolioEventFact(
                instrument_id=p.instrument_id,
                ticker=p.ticker,
                checked_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                event_status="EVENT_CHECK_FAILED",
            )
            for p in self.portfolio.positions
        ]
        with patch(
            "refresh_portfolio_events.load_authoritative_portfolio",
            return_value=self.portfolio,
        ), patch(
            "refresh_portfolio_events.fetch_portfolio_events",
            return_value=(failed_facts, []),
        ):
            self.assertEqual(refresh_portfolio_events.main(), 0)


if __name__ == "__main__":
    unittest.main()
