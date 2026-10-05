#!/usr/bin/env python3
"""Regression test suite for Action Brief semantics and readability (Part K 1-18)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from market_context import build_market_context
from models import (
    MarketContext,
    PortfolioActionBrief,
    PortfolioActionItem,
    PortfolioContext,
    PortfolioQuoteCoverage,
    PositionContext,
    QuoteObservation,
    Snapshot,
)
from portfolio_context import EncryptedPortfolioProvider
from structured_reports import (
    build_action_brief,
    build_public_draft,
    render_action_brief,
    validate_action_brief,
    validate_public_draft,
)


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


def _snapshot(quotes: dict[str, QuoteObservation], report_type: str = "tw_close") -> Snapshot:
    now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    return Snapshot(
        generated_at=now,
        report_type=report_type,
        fetch_coverage=1.0,
        market_context_coverage=1.0,
        portfolio_quote_coverage=PortfolioQuoteCoverage(
            expected_positions=len(quotes),
            covered_positions=len(quotes),
            coverage_ratio=1.0,
            as_of=now,
            status="FULL",
        ),
        quote_observations=quotes,
        us_markets={},
    )


def _make_23_portfolio() -> PortfolioContext:
    tickers = [
        "0050", "006208", "2330", "2454", "2317", "2382",
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK.B", "JNJ", "V", "WMT",
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA",
    ]
    positions = [
        PositionContext(
            position_id=f"pos_{t}",
            instrument_id=t,
            ticker=t,
            name=f"Stock {t}",
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
        source="TEST",
        positions=positions,
    )


class ActionBriefSemanticsSuiteTest(unittest.TestCase):
    def setUp(self):
        self.portfolio_23 = _make_23_portfolio()
        self.quotes_23 = {p.ticker: _obs(p.ticker, price=100.0, prev=99.0) for p in self.portfolio_23.positions}
        self.snapshot_23 = _snapshot(self.quotes_23)
        self.context_23 = build_market_context(self.snapshot_23, "tw_close", run_id="test_run")

    def test_01_user_facing_contains_no_no_material_change(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertNotIn("NO_MATERIAL_CHANGE", rendered)
        self.assertNotIn("NO MATERIAL CHANGE", rendered)

    def test_02_user_facing_contains_no_size_not_computed(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertNotIn("SIZE_NOT_COMPUTED", rendered)

    def test_03_user_facing_contains_no_previous_close(self):
        quotes_pc = {p.ticker: _obs(p.ticker, session="PREVIOUS_CLOSE") for p in self.portfolio_23.positions}
        snap = _snapshot(quotes_pc, report_type="tw_open")
        ctx = build_market_context(snap, "tw_open", run_id="test_pc")
        brief = build_action_brief(ctx, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertNotIn("PREVIOUS_CLOSE", rendered)
        self.assertIn("前一交易日收盤資料", rendered)

    def test_04_user_facing_contains_no_data_blocked(self):
        partial_quotes = dict(self.quotes_23)
        del partial_quotes["0050"]
        snap = _snapshot(partial_quotes)
        ctx = build_market_context(snap, "tw_close", run_id="test_partial")
        brief = build_action_brief(ctx, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertNotIn("DATA_BLOCKED", rendered)
        self.assertIn("【資料說明】", rendered)
        self.assertIn("0050", rendered)

    def test_05_all_23_normal_does_not_print_23_ticker_names(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        for p in self.portfolio_23.positions:
            self.assertNotIn(f"{p.ticker} —", rendered)
            self.assertNotIn(f"{p.ticker}:", rendered)
        # Should not have a long list of tickers
        self.assertNotIn("0050,", rendered)
        self.assertNotIn("006208,", rendered)

    def test_06_all_23_normal_summarizes_quote_coverage(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertIn("23/23 持股行情已驗證", rendered)

    def test_07_under_7pct_move_alone_does_not_prove_no_material_event(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        item = next(i for i in brief.no_material_change if i.ticker == "2330")
        self.assertEqual(item.price_status, "PRICE_NORMAL")
        self.assertEqual(item.event_status, "EVENT_UNCHECKED")
        self.assertIn("今日價格未觸發監控門檻；公司事件面尚未完成驗證。", item.summary)
        self.assertNotIn("未偵測到足以升級的新資訊", item.summary)
        self.assertNotIn("沒有重大事件", item.summary)

    def test_08_at_or_above_7pct_move_produces_price_watch(self):
        quotes = dict(self.quotes_23)
        quotes["2330"] = _obs("2330", price=108.0, prev=100.0)  # +8%
        snap = _snapshot(quotes)
        ctx = build_market_context(snap, "tw_close", run_id="test_large_move")
        brief = build_action_brief(ctx, self.portfolio_23)
        self.assertEqual(len(brief.watchlist), 1)
        item = brief.watchlist[0]
        self.assertEqual(item.ticker, "2330")
        self.assertEqual(item.status, "WATCH")
        self.assertEqual(item.price_status, "PRICE_WATCH")
        rendered = render_action_brief(brief)
        self.assertIn("【需要關注】", rendered)
        self.assertIn("2330", rendered)
        self.assertIn("+8.0%", rendered)

    def test_09_event_unchecked_state_cannot_render_no_material_change(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertNotIn("NO_MATERIAL_CHANGE", rendered)
        self.assertNotIn("沒有重大事件", rendered)
        self.assertNotIn("未偵測到足以升級的新資訊", rendered)

    def test_10_verified_material_event_creates_watch_or_action_review_independent_of_price(self):
        # Move is 0%, but verified material event exists
        events = [
            {
                "ticker": "AMZN",
                "status": "ACTION_REVIEW",
                "severity": "HIGH",
                "material": True,
                "summary": "公司大幅下修 AWS guidance，影響成長假設。",
                "reason_code": "GUIDANCE_CUT",
                "details": "Q3 財報展望顯示雲端業務增速放緩至 8%。",
                "impact": "影響長期現金流估計。",
            }
        ]
        brief = build_action_brief(self.context_23, self.portfolio_23, verified_events=events)
        self.assertEqual(len(brief.action_queue), 1)
        item = brief.action_queue[0]
        self.assertEqual(item.ticker, "AMZN")
        self.assertEqual(item.status, "ACTION_REVIEW")
        self.assertEqual(item.event_status, "EVENT_MATERIAL_FOUND")
        rendered = render_action_brief(brief)
        self.assertIn("【值得重新檢視】", rendered)
        self.assertIn("AMZN", rendered)
        self.assertIn("公司大幅下修 AWS guidance", rendered)

    def test_11_upcoming_events_does_not_use_sizing_state(self):
        upcoming = [
            {"ticker": "NVDA", "event": "財報發表會", "date": "10/25 美股盤後", "why": "重點看 Data Center 營收"},
        ]
        brief = build_action_brief(self.context_23, self.portfolio_23, upcoming_events=upcoming)
        for ev in brief.upcoming_events:
            ev_str = json.dumps(ev) if isinstance(ev, dict) else str(ev)
            self.assertNotIn("SIZE_NOT_COMPUTED", ev_str)
        rendered = render_action_brief(brief)
        self.assertNotIn("SIZE_NOT_COMPUTED", rendered)
        self.assertIn("NVDA｜財報發表會｜10/25 美股盤後｜重點看 Data Center 營收", rendered)

    def test_12_no_verified_upcoming_events_natural_language_empty_state(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        self.assertIn("【近期事件】", rendered)
        self.assertIn("• 目前沒有已驗證、需要特別準備的事件。", rendered)
        self.assertNotIn("search-grounded", rendered)

    def test_13_sizing_explanation_only_for_actual_action_requiring_sizing(self):
        # Case A: no action queue
        brief_no_action = build_action_brief(self.context_23, self.portfolio_23)
        rendered_no_action = render_action_brief(brief_no_action)
        self.assertNotIn("不提供精確買賣股數", rendered_no_action)
        self.assertNotIn("SIZE_NOT_COMPUTED", rendered_no_action)

        # Case B: action queue has an item
        events = [{"ticker": "NVDA", "status": "ACTION_REVIEW", "summary": "重大事件", "material": True}]
        brief_with_action = build_action_brief(self.context_23, self.portfolio_23, verified_events=events)
        rendered_with_action = render_action_brief(brief_with_action)
        self.assertIn("不提供精確買賣股數", rendered_with_action)
        self.assertNotIn("SIZE_NOT_COMPUTED", rendered_with_action)

    def test_14_private_holdings_still_do_not_enter_public_report(self):
        draft = build_public_draft(self.context_23, "Public narrative commentary")
        valid, _ = validate_public_draft(draft, self.context_23)
        self.assertTrue(valid)
        # Check that private quantities and cost basis never appear in rendered markdown
        for p in self.portfolio_23.positions:
            self.assertNotIn(f"{p.quantity:g} 股", draft.rendered_markdown)
            self.assertNotIn(f"成本 {p.cost_basis}", draft.rendered_markdown)

    def test_15_quote_coverage_failure_remains_fail_closed(self):
        # If quote is missing or invalid, item must be DATA_BLOCKED
        partial_quotes = dict(self.quotes_23)
        del partial_quotes["2330"]
        snap = _snapshot(partial_quotes)
        ctx = build_market_context(snap, "tw_close", run_id="test_fail_closed")
        brief = build_action_brief(ctx, self.portfolio_23)
        item_2330 = next(i for i in brief.data_issues if "2330" in i)
        self.assertIn("quote unavailable or invalid", item_2330)

    def test_16_action_brief_json_may_retain_internal_enums(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        dumped = brief.model_dump_json()
        self.assertIn("NO_MATERIAL_CHANGE", dumped)
        self.assertIn("PRICE_NORMAL", dumped)
        self.assertIn("EVENT_UNCHECKED", dumped)

    def test_17_telegram_renderer_does_not_leak_internal_enums(self):
        quotes = dict(self.quotes_23)
        quotes["2330"] = _obs("2330", price=108.0, prev=100.0)  # Watch item
        events = [{"ticker": "NVDA", "status": "ACTION_REVIEW", "summary": "重大事件", "material": True}]
        snap = _snapshot(quotes)
        ctx = build_market_context(snap, "tw_close", run_id="test_all_enums")
        brief = build_action_brief(ctx, self.portfolio_23, verified_events=events)
        rendered = render_action_brief(brief)

        forbidden_enums = [
            "NO_MATERIAL_CHANGE",
            "ACTION_REVIEW",
            "DATA_BLOCKED",
            "SIZE_NOT_COMPUTED",
            "PREVIOUS_CLOSE",
            "REGULAR",
            "VALID",
            "LIMITED",
            "QUOTE_UNAVAILABLE_OR_INVALID",
            "NO_MATERIAL_EVENT",
            "LARGE_DAILY_MOVE",
            "ACTION QUEUE",
            "WATCHLIST",
            "NO MATERIAL CHANGE",
        ]
        for token in forbidden_enums:
            self.assertNotIn(token, rendered, f"Leaked internal enum: {token}")

    def test_18_current_23_position_portfolio_produces_concise_output(self):
        brief = build_action_brief(self.context_23, self.portfolio_23)
        rendered = render_action_brief(brief)
        # Should be concise: <= 500 characters, ~10-20 seconds reading time
        self.assertLessEqual(len(rendered), 500)
        lines = [line for line in rendered.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 20)
        ok, reason = validate_action_brief(brief, self.context_23, self.portfolio_23)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
