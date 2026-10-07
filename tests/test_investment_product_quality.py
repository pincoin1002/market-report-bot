"""Product regressions: dated evidence, Chinese commentary, public privacy."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from models import (MarketContext, PortfolioContext, PositionContext, QuoteObservation,
                    PortfolioEventFact, PortfolioQuoteCoverage, TaiexMarketSummary, InstitutionalFlows)
from portfolio_events import is_entry_fresh, fetch_portfolio_events
from structured_reports import build_action_brief, build_public_draft, render_action_brief, validate_action_brief
from event_source_evidence import parse_publication_evidence
from investment_language import format_price
from investment_language import chinese_text
from validate_report import validate_numeric_provenance
from twse_market_evidence import fetch_twse_close_evidence, MI_INDEX_URL, _institutional_flows
from test_report_integrity_hotfix import market_payload, flow_payload

NOW = datetime(2026, 10, 6, 6, 32, tzinfo=timezone.utc)


def event(**updates):
    values = dict(instrument_id="NVDA", ticker="NVDA", checked_at=NOW,
                  event_status="EVENT_MATERIAL_FOUND", event_type="CAPITAL_ALLOCATION",
                  title="NVIDIA Announces Share Repurchase Authorization Increase",
                  summary="The board increased its share repurchase authorization.",
                  impact="Potentially boosting earnings per share and stock price.",
                  severity="HIGH", event_date="2026-10-06", published_at=NOW - timedelta(hours=1),
                  source_published_at=NOW - timedelta(hours=1), publication_date_verified=True,
                  source_name="NVIDIA Newsroom", source_url="https://investor.nvidia.com/article")
    values.update(updates)
    return PortfolioEventFact(**values)


class InvestmentProductQualityTest(unittest.TestCase):
    def setUp(self):
        self.portfolio = PortfolioContext(snapshot_id="synthetic", as_of=NOW, source="TEST", positions=[
            PositionContext(position_id=t, instrument_id=t, ticker=t, name=t, quantity=1,
                            currency="USD", asset_type=a)
            for t, a in (("NVDA", "EQUITY"), ("BONK", "CRYPTO"), ("VOO", "ETF"), ("2330", "EQUITY"))])
        self.context = MarketContext(run_id="quality", report_type="tw_close", market_date="2026-10-06",
                                    generated_at=NOW, market_session="REGULAR")
        for symbol, price, change, asset in (("NVDA", 147.5, 0.1, "US"), ("BONK", .00000375, -7.01, "GLOBAL"),
            ("VOO", 108, 8, "US"), ("2330", 2585, .39, "TW"), ("TAIEX", 49822.55, .22, "TW"),
            ("2454", 4920, -4.74, "TW"), ("2303", 147.5, -3.28, "TW"), ("2383", 5995, 5.18, "TW"), ("2308", 2050, 2.24, "TW")):
            self.context.quotes[symbol] = QuoteObservation(quote_id=f"{symbol}:test", instrument_id=symbol,
                canonical_symbol=symbol, price=price, previous_regular_close=price / (1 + change / 100),
                change_pct=change, change_interval="ROLLING_24H" if symbol == "BONK" else "PREVIOUS_CLOSE",
                currency="TWD" if asset == "TW" else "USD", market=asset, session="REGULAR",
                market_date="2026-10-06", observed_at=NOW, provider_timestamp=None, retrieved_at=NOW,
                provider="fixture", quote_type="OFFICIAL_CLOSE", quality_status="VALID")

    def brief(self, facts=None):
        return build_action_brief(self.context, self.portfolio, verified_events=facts or [])

    def test_01_eight_day_event_cannot_trigger_watch(self):
        brief = self.brief([event(severity="MEDIUM", source_published_at=NOW - timedelta(days=8))])
        self.assertFalse(any(i.ticker == "NVDA" for i in brief.watchlist))

    def test_02_eight_day_event_cannot_trigger_review(self):
        self.assertFalse(self.brief([event(source_published_at=NOW - timedelta(days=8))]).action_queue)

    def test_03_fresh_cache_does_not_refresh_event_age(self):
        stale = event(source_published_at=NOW - timedelta(days=8))
        entry = {"checked_at": NOW.isoformat(), "event_status": "EVENT_MATERIAL_FOUND", "facts": [stale.model_dump(mode="json")]}
        self.assertTrue(is_entry_fresh(entry, NOW))
        with patch("portfolio_events.load_event_cache", return_value={"items": {"NVDA": entry}}):
            facts, _ = fetch_portfolio_events(self.portfolio, now=NOW, network_scope="none")
        self.assertFalse(self.brief(facts).action_queue)

    def test_04_recent_verified_material_event_can_trigger_review(self):
        self.assertEqual(self.brief([event()]).action_queue[0].ticker, "NVDA")

    def test_05_future_event_only_in_upcoming(self):
        upcoming = event(is_upcoming=True, event_date="2026-10-10")
        brief = self.brief([upcoming])
        self.assertFalse(brief.action_queue)
        self.assertFalse(any(i.ticker == "NVDA" for i in brief.watchlist))
        self.assertEqual(len(brief.upcoming_events), 1)

    def test_06_more_than_seven_days_upcoming_rejected(self):
        self.assertFalse(self.brief([event(is_upcoming=True, event_date="2026-10-14")]).upcoming_events)

    def test_07_actionable_event_date_visible(self):
        self.assertIn("NVDA｜2026-10-06｜", render_action_brief(self.brief([event()])))

    def test_08_actionable_source_visible(self):
        self.assertIn("來源：NVIDIA 投資人關係網站", render_action_brief(self.brief([event()])))

    def test_09_title_chinese(self):
        self.assertIn("庫藏股回購授權", render_action_brief(self.brief([event()])))

    def test_10_explanation_chinese(self):
        self.assertIn("授權不代表立即買回", render_action_brief(self.brief([event()])))

    def test_11_no_english_investment_sentence(self):
        text = render_action_brief(self.brief([event()]))
        self.assertNotIn("Potentially boosting", text)
        self.assertNotIn("The board increased", text)

    def test_12_fact_and_interpretation_separate(self):
        text = render_action_brief(self.brief([event()]))
        self.assertIn("已驗證事件：", text)
        self.assertIn("投資含義：", text)
        self.assertIn("不確定性：", text)

    def test_13_crypto_template(self):
        item = next(i for i in self.brief().watchlist if i.ticker == "BONK")
        self.assertIn("協議升級", item.next_step)
        self.assertNotIn("財報", item.next_step)
        self.assertNotIn("營運展望", item.next_step)

    def test_14_etf_template(self):
        item = next(i for i in self.brief().watchlist if i.ticker == "VOO")
        self.assertIn("成分股再平衡", item.next_step)

    def test_15_equity_template(self):
        obs = self.context.quotes["NVDA"].model_copy(update={"change_pct": 8})
        self.context.quotes["NVDA"] = obs
        item = next(i for i in self.brief().watchlist if i.ticker == "NVDA")
        self.assertIn("財報、營運展望、公司公告", item.next_step)

    def test_16_crypto_interval(self):
        self.assertIn("BONK｜24 小時 -7.0%", render_action_brief(self.brief()))

    def test_17_equity_interval(self):
        self.context.quotes["NVDA"] = self.context.quotes["NVDA"].model_copy(update={"change_pct": 8})
        self.assertIn("NVDA｜單日 +8.0%", render_action_brief(self.brief()))

    def test_18_small_prices(self):
        self.assertEqual(format_price(.00000375), "0.00000375")
        self.assertEqual(format_price(2585), "2,585")
        self.assertEqual(format_price(147.5), "147.5")
        self.assertNotIn("e-06", render_action_brief(self.brief()))

    def test_19_public_no_holding_counts(self):
        self.context.portfolio_quote_coverage = PortfolioQuoteCoverage(expected_positions=23, covered_positions=23, coverage_ratio=1, as_of=NOW, status="FULL")
        text = build_public_draft(self.context).rendered_markdown
        for value in ("23/23", "持股行情", "行情覆蓋", "portfolio", "FULL"):
            self.assertNotIn(value, text)

    def test_20_no_public_portfolio_section(self):
        self.assertNotIn("【我的持股】", build_public_draft(self.context).rendered_markdown)

    def test_21_private_coverage_retained(self):
        self.assertIn("4/4 持股行情已驗證", render_action_brief(self.brief()))

    def test_22_no_raw_internal_tokens(self):
        text = render_action_brief(self.brief([event()]))
        for token in ("FULL", "EVENT_CHECK_FAILED", "EVENT_UNCHECKED", "ACTION_REVIEW", "WATCH", "DATA_BLOCKED", "PREVIOUS_CLOSE", "ROLLING_24H"):
            self.assertNotIn(token, text)

    def test_23_stale_not_counted(self):
        self.assertEqual(self.brief([event(source_published_at=NOW - timedelta(days=8))]).event_checked_positions, 0)

    def test_24_failed_not_counted(self):
        self.assertEqual(self.brief([event(event_status="EVENT_CHECK_FAILED")]).event_checked_positions, 0)

    def test_25_current_no_event_check_counted(self):
        clean = event(event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE", title=None, event_date=None)
        self.assertEqual(self.brief([clean]).event_checked_positions, 1)

    def test_26_direction_contradiction_rejected(self):
        bad = "# 台股收盤\n## 今天盤面重點\n- 多個大型權值同步走弱，與加權指數表現一致。"
        self.assertFalse(validate_numeric_provenance(bad, self.context)[0])
        self.assertNotIn("同步走弱，與加權指數表現一致", build_public_draft(self.context).rendered_markdown)

    def test_27_no_repeated_daily_movers_in_what_changed(self):
        self.assertFalse(build_public_draft(self.context).material_changes)
        self.context.taiex_summary = TaiexMarketSummary(close=49822.55, point_change=110.51, change_pct=.22,
            previous_change_pct=2.55, advancing=462, declining=519, advancing_prev=364, declining_prev=631)
        changes = build_public_draft(self.context).material_changes
        self.assertTrue(any("前日" in line for line in changes))
        self.assertFalse(any("權值單日變化" in line for line in changes))

    def test_28_tomorrow_watch_includes_abnormal_movers(self):
        watch = " ".join(build_public_draft(self.context).watch_signals)
        self.assertIn("2383", watch)
        self.assertIn("2454", watch)
        self.assertIn("2303", watch)

    def test_29_official_statistics_render(self):
        self.context.taiex_summary = TaiexMarketSummary(close=49822.55, point_change=110.51, change_pct=.22,
            turnover_ntd_billions=10217.23, advancing=462, declining=519, unchanged=100)
        self.context.institutional_flows = InstitutionalFlows(foreign_buy_sell_ntd_billions=-66.57,
            investment_trust_buy_sell_ntd_billions=-158.51, dealer_buy_sell_ntd_billions=17.21, total_buy_sell_ntd_billions=-207.87)
        text = build_public_draft(self.context).rendered_markdown
        self.assertIn("10,217.23 億元", text)
        self.assertIn("平盤 100", text)
        self.assertIn("投信賣超 158.51", text)

    def test_30_unavailable_flows_do_not_erase_official_market(self):
        def get(url, params):
            if url == MI_INDEX_URL:
                return market_payload(params["date"])
            raise ValueError("not published")
        with patch("twse_market_evidence._get_json", side_effect=get):
            evidence = fetch_twse_close_evidence("2026-10-06", "2026-10-05", NOW)
        self.assertIsNotNone(evidence.taiex_summary)
        self.assertIsNone(evidence.institutional_flows)

    def test_31_source_date_overrides_model_date(self):
        html = '<h1>NVIDIA Announces Share Repurchase Authorization Increase</h1><meta property="article:published_time" content="2026-09-28T10:00:00Z">'
        actual = parse_publication_evidence(html, event().title)
        self.assertFalse(self.brief([event(source_published_at=actual)]).action_queue)

    def test_32_old_serialized_brief_cannot_bypass_consumption(self):
        brief = self.brief([event()])
        brief = brief.model_copy(update={"as_of": NOW + timedelta(days=8)})
        self.assertNotIn("庫藏股回購", render_action_brief(brief))

    def test_33_strict_public_provenance(self):
        self.test_27_no_repeated_daily_movers_in_what_changed()
        draft = build_public_draft(self.context)
        ok, errors = validate_numeric_provenance(draft.rendered_markdown, self.context)
        self.assertTrue(ok, errors)
        self.assertFalse(validate_numeric_provenance(draft.rendered_markdown.replace("+2.55%", "-2.55%"), self.context)[0])

    def test_34_small_price_cannot_validate_zero_or_other_tiny_price(self):
        # Explicit validator probe: private-only BONK is no longer selected for public reports.
        text = "# 報價驗證\n| 標的 | 報價 |\n| Bonk (BONK) | 0.00000375 |"
        self.assertTrue(validate_numeric_provenance(text, self.context)[0])
        for invalid in ("0.00", "0.00000750"):
            self.assertFalse(validate_numeric_provenance(text.replace("0.00000375", invalid), self.context)[0])

    def test_35_previous_flow_failure_does_not_erase_current(self):
        flows = _institutional_flows(flow_payload("20261006"), {"data": []}, "2026-10-06", "2026-10-05", NOW)
        self.assertIsNotNone(flows.foreign_buy_sell_ntd_billions)
        self.assertIsNone(flows.foreign_buy_sell_prev_ntd_billions)

    def test_36_new_event_check_can_follow_quote_snapshot(self):
        later = NOW + timedelta(minutes=3)
        facts = [event(checked_at=later, source_published_at=NOW)]
        brief = build_action_brief(self.context, self.portfolio, verified_events=facts, as_of=later)
        self.assertEqual(len(brief.action_queue), 1)
        self.assertEqual(brief.as_of, later)

    def test_37_simplified_commentary_converted(self):
        self.assertEqual(chinese_text("董事会发布公告", "公告"), "董事會發布公告")

    def test_38_model_date_cannot_certify_itself(self):
        self.assertFalse(self.brief([event(publication_date_verified=False)]).action_queue)

    def test_39_mixed_english_investment_words_do_not_leak(self):
        self.assertEqual(chinese_text("AWS guidance 下修", "公告"), "AWS 營運展望 下修")
        self.assertEqual(chinese_text("Data Center 营收", "公告"), "資料中心 營收")
        self.assertEqual(chinese_text("公司 potentially 上漲", "仍需核對"), "仍需核對")


if __name__ == "__main__":
    unittest.main()
